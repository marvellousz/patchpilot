import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from patchpilot import server
from patchpilot.store import Store

SECRET = "test-secret"
TOKEN = "github_pat_test_1234"


def _labeled(label="patchpilot", number=7, repo="acme/shop"):
    return {
        "action": "labeled",
        "label": {"name": label},
        "issue": {"number": number, "title": "Bug"},
        "repository": {"full_name": repo},
    }


def _post(client, payload, event="issues", secret=SECRET):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body,
                       headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sig})


class FakeGitHub:
    """Stands in for the GitHub API: TOKEN can push to acme/shop, which has issue 7."""

    def __init__(self, token):
        self.token = token

    def get_repo(self, repo):
        if self.token != TOKEN or repo.lower() != "acme/shop":
            raise server.GitHubError("404")
        return {"full_name": "acme/shop", "permissions": {"push": True}}

    def list_issues(self, repo):
        return [{"number": 7, "title": "Bug", "html_url": "https://github.com/acme/shop/issues/7",
                 "user": {"login": "dev"}, "created_at": "2026-11-07T00:00:00Z", "labels": []}]

    def get_issue(self, repo, number):
        if number != 7:
            raise server.GitHubError("404")
        return {"number": 7, "title": "Bug"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    for name in ("PATCHPILOT_SECRET_KEY", "PATCHPILOT_INVITE_CODE", "PATCHPILOT_RUNS_PER_DAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(server, "GitHub", FakeGitHub)
    calls = []
    store = Store(tmp_path / "data")
    app = server.create_app(store=store,
                            runner=lambda repo, number, open_pr, token: calls.append((repo, number, open_pr, token)))
    return app, calls, store, tmp_path


def _signed_in(app, email="dev@example.com"):
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": email, "password": "correct horse"}).status_code == 200
    return client


def _connect(client):
    res = client.post("/api/repos", json={"repo": "https://github.com/acme/shop", "token": TOKEN})
    assert res.status_code == 200, res.text
    return res.json()


def _write_run(tmp_path, repo="acme/shop", number=7):
    run = tmp_path / "runs" / f"{repo.replace('/', '-')}-{number}-20261108-101500"
    run.mkdir(parents=True)
    events = [
        {"time": "10:15:00", "kind": "issue", "repo": repo, "number": number, "title": "Bug"},
        {"time": "10:16:00", "kind": "outcome", "status": "fixed", "reason": ""},
    ]
    (run / "memory.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return run


def test_signup_login_logout(env):
    app, *_ = env
    client = _signed_in(app)
    assert client.get("/api/me").json()["email"] == "dev@example.com"
    assert client.post("/api/signup", json={"email": "dev@example.com", "password": "another one"}).status_code == 409
    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/login", json={"email": "dev@example.com", "password": "wrong pass"}).status_code == 401
    assert client.post("/api/login", json={"email": "DEV@example.com", "password": "correct horse"}).status_code == 200
    assert client.get("/api/me").status_code == 200


def test_api_requires_sign_in(env):
    app, *_ = env
    client = TestClient(app)
    for path in ("/api/me", "/api/repos", "/api/runs"):
        assert client.get(path).status_code == 401


def test_connect_repo_never_returns_the_token(env):
    app, _, store, tmp_path = env
    client = _signed_in(app)
    repo = _connect(client)
    assert repo["full_name"] == "acme/shop" and repo["token_hint"] == "1234"
    assert TOKEN not in json.dumps(client.get("/api/repos").json())
    assert TOKEN.encode() not in (tmp_path / "data" / "patchpilot.db").read_bytes()
    bad = client.post("/api/repos", json={"repo": "acme/shop", "token": "github_pat_wrong"})
    assert bad.status_code == 400


def test_fix_queues_a_run_with_the_users_token(env):
    app, calls, *_ = env
    client = _signed_in(app)
    repo = _connect(client)
    assert client.get(f"/api/repos/{repo['id']}/issues").json()[0]["number"] == 7
    assert client.post(f"/api/repos/{repo['id']}/issues/7/fix", json={"open_pr": False}).status_code == 200
    app.state.jobs.join()
    assert calls == [("acme/shop", 7, False, TOKEN)]
    assert client.post(f"/api/repos/{repo['id']}/issues/99/fix", json={}).status_code == 404


def test_users_cannot_touch_each_others_repos(env):
    app, *_ = env
    repo = _connect(_signed_in(app, "a@example.com"))
    other = _signed_in(app, "b@example.com")
    assert other.get(f"/api/repos/{repo['id']}/issues").status_code == 404
    assert other.post(f"/api/repos/{repo['id']}/issues/7/fix", json={}).status_code == 404
    assert other.delete(f"/api/repos/{repo['id']}").status_code == 404


def test_runs_are_visible_only_for_connected_repos(env):
    app, _, _, tmp_path = env
    run = _write_run(tmp_path)
    _write_run(tmp_path, repo="someone/else")
    client = _signed_in(app)
    assert client.get("/api/runs").json()["runs"] == []
    assert client.get(f"/api/runs/{run.name}").status_code == 404
    _connect(client)
    runs = client.get("/api/runs").json()["runs"]
    assert [r["repo"] for r in runs] == ["acme/shop"] and runs[0]["status"] == "fixed"
    assert len(client.get(f"/api/runs/{run.name}").json()["events"]) == 2
    assert client.get("/api/runs/..%2Fetc").status_code == 404


def test_label_queues_a_run_for_a_connected_repo(env):
    app, calls, *_ = env
    _connect(_signed_in(app))
    client = TestClient(app)
    assert _post(client, _labeled()).json() == {"queued": "acme/shop#7"}
    assert "ignored" in _post(client, _labeled(repo="someone/else")).json()
    app.state.jobs.join()
    assert calls == [("acme/shop", 7, True, TOKEN)]


def test_other_labels_and_events_are_ignored(env):
    app, calls, *_ = env
    client = TestClient(app)
    assert "ignored" in _post(client, _labeled(label="bug")).json()
    assert "ignored" in _post(client, {"zen": "hi"}, event="ping").json()
    app.state.jobs.join()
    assert calls == []


def test_bad_signature_is_rejected(env):
    app, calls, *_ = env
    assert _post(TestClient(app), _labeled(), secret="wrong").status_code == 401
    assert calls == []


def test_signup_needs_the_invite_code_when_set(env, monkeypatch):
    _, _, store, _ = env
    monkeypatch.setenv("PATCHPILOT_INVITE_CODE", "judges-2026")
    client = TestClient(server.create_app(store=store, runner=lambda *a, **k: None))
    assert client.get("/api/config").json() == {"invite_required": True}
    body = {"email": "j@example.com", "password": "correct horse"}
    assert client.post("/api/signup", json={**body, "invite": "nope"}).status_code == 403
    assert client.post("/api/signup", json={**body, "invite": "judges-2026"}).status_code == 200


def test_repeated_failed_logins_are_throttled(env):
    app, *_ = env
    _signed_in(app)
    client = TestClient(app)
    wrong = {"email": "dev@example.com", "password": "wrong pass"}
    for _ in range(server.MAX_FAILED_LOGINS):
        assert client.post("/api/login", json=wrong).status_code == 401
    right = {"email": "dev@example.com", "password": "correct horse"}
    assert client.post("/api/login", json=right).status_code == 429


def test_runs_per_day_limit(env, monkeypatch):
    _, calls, store, _ = env
    monkeypatch.setenv("PATCHPILOT_RUNS_PER_DAY", "1")
    app = server.create_app(store=store, runner=lambda repo, number, open_pr, token: calls.append(number))
    client = _signed_in(app)
    repo = _connect(client)
    assert client.get("/api/me").json()["runs_left"] == 1
    assert client.post(f"/api/repos/{repo['id']}/issues/7/fix", json={}).json()["runs_left"] == 0
    app.state.jobs.join()
    assert client.post(f"/api/repos/{repo['id']}/issues/7/fix", json={}).status_code == 429
    assert calls == [7]


def test_session_cookie_is_secure_over_https(env):
    app, *_ = env
    client = TestClient(app, base_url="https://testserver")
    res = client.post("/api/signup", json={"email": "s@example.com", "password": "correct horse"})
    assert "secure" in res.headers["set-cookie"].lower()


def test_parse_repo():
    assert server.parse_repo("acme/shop") == "acme/shop"
    assert server.parse_repo("https://github.com/acme/shop.git") == "acme/shop"
    assert server.parse_repo(" https://github.com/acme/shop/ ") == "acme/shop"
    assert server.parse_repo("not a repo") is None
