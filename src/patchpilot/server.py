"""PatchPilot web app: accounts, connected repositories, the GitHub webhook and live run views.

Users sign in, connect a repository with their own GitHub token, and start a repair run on any open
issue (or label it PATCHPILOT_LABEL when the repo's webhook points here). Runs execute one at a time
in a background worker; the dashboard reads their memory logs from runs/. A run is visible to every
user who connected its repository, since their token already grants write access to it.
"""

import hashlib
import hmac
import json
import os
import queue
import re
import threading
import time
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from patchpilot.github_client import GitHub, GitHubError
from patchpilot.pipeline import RUNS_DIR, fix_issue
from patchpilot.store import SESSION_SECONDS, Store

DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")
STALE_AFTER_SECONDS = 15 * 60
SESSION_COOKIE = "pp_session"
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
REPO_NAME = re.compile(r"(?:https?://github\.com/)?([\w.-]+/[\w.-]+?)(?:\.git)?/?")
MIN_PASSWORD = 8
MAX_FAILED_LOGINS = 10
FAILED_LOGIN_WINDOW = 15 * 60
DAY_SECONDS = 24 * 3600


class Credentials(BaseModel):
    email: str
    password: str
    invite: str = ""


class Throttle:
    """Counts failures per key (email, client IP) and blocks a key after too many in the window."""

    def __init__(self, limit: int = MAX_FAILED_LOGINS, window: float = FAILED_LOGIN_WINDOW):
        self.limit, self.window = limit, window
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = time.time() - self.window
        self.failures[key] = [t for t in self.failures.get(key, []) if t > cutoff]
        return self.failures[key]

    def blocked(self, *keys: str) -> bool:
        with self.lock:
            return any(len(self._recent(k)) >= self.limit for k in keys)

    def fail(self, *keys: str) -> None:
        with self.lock:
            for k in keys:
                self._recent(k).append(time.time())


class RepoIn(BaseModel):
    repo: str
    token: str


class FixIn(BaseModel):
    open_pr: bool = True


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_repo(text: str) -> str | None:
    m = REPO_NAME.fullmatch(text.strip())
    return m.group(1) if m else None


def read_events(run_dir: Path) -> list[dict]:
    events = []
    path = run_dir / "memory.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a line still being written
    return events


def summarize(run_dir: Path) -> dict:
    events = read_events(run_dir)
    first = {}
    for e in events:
        first.setdefault(e["kind"], e)
    outcome = next((e for e in reversed(events) if e["kind"] == "outcome"), None)
    if outcome:
        status = outcome["status"]
    elif time.time() - (run_dir / "memory.jsonl").stat().st_mtime > STALE_AFTER_SECONDS:
        status = "incomplete"
    else:
        status = "running"
    issue = first.get("issue", {})
    return {
        "id": run_dir.name,
        "repo": issue.get("repo"),
        "number": issue.get("number"),
        "title": issue.get("title"),
        "status": status,
        "pr": first.get("pr", {}).get("url"),
        "started": events[0]["time"] if events else None,
    }


def run_dirs() -> list[Path]:
    if not RUNS_DIR.exists():
        return []
    return sorted((d for d in RUNS_DIR.iterdir() if (d / "memory.jsonl").exists()), reverse=True,
                  key=lambda d: d.name.rsplit("-", 2)[-2:])


def _run_dir(run_id: str) -> Path:
    if not re.fullmatch(r"[\w.-]+", run_id) or not (RUNS_DIR / run_id / "memory.jsonl").exists():
        raise HTTPException(404, "No such run")
    return RUNS_DIR / run_id


def create_app(runner=fix_issue, open_pr: bool = True, store: Store | None = None) -> FastAPI:
    """open_pr=False (serve --dry-run) forces every run to be a dry run, whatever the UI asks for."""
    app = FastAPI(title="PatchPilot")
    store = store or Store()
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "").strip()
    label = os.getenv("PATCHPILOT_LABEL", "patchpilot")
    invite = os.getenv("PATCHPILOT_INVITE_CODE", "").strip()
    runs_per_day = int(os.getenv("PATCHPILOT_RUNS_PER_DAY", "5"))  # per user; 0 = unlimited
    throttle = Throttle()
    jobs: queue.Queue = queue.Queue()
    pending: list[dict] = []  # queued, not yet picked up; holds tokens, so never returned as-is
    lock = threading.Lock()
    app.state.jobs = jobs

    def enqueue(repo: str, number: int, title: str, token: str, open_pr_run: bool) -> bool:
        with lock:
            if any(j["repo"].lower() == repo.lower() and j["number"] == number for j in pending):
                return False
            job = {"repo": repo, "number": number, "title": title, "token": token, "open_pr": open_pr_run and open_pr}
            pending.append(job)
        jobs.put(job)
        print(f"[server] queued {repo}#{number}", flush=True)
        return True

    def worker() -> None:
        while True:
            job = jobs.get()
            with lock:
                pending.remove(job)
            try:
                runner(job["repo"], job["number"], open_pr=job["open_pr"], token=job["token"])
            except Exception as e:  # keep the worker alive; the failure is in the run's memory log
                print(f"[server] run for {job['repo']}#{job['number']} crashed: {e}", flush=True)
            finally:
                jobs.task_done()

    threading.Thread(target=worker, daemon=True).start()

    def current_user(pp_session: str | None = Cookie(None)) -> dict:
        user = store.session_user(pp_session) if pp_session else None
        if not user:
            raise HTTPException(401, "Sign in first")
        return user

    def connected(user: dict) -> set[str]:
        return {r["full_name"].lower() for r in store.list_repos(user["id"])}

    def user_repo(user: dict, repo_id: int) -> dict:
        repo = store.get_repo(user["id"], repo_id)
        if not repo:
            raise HTTPException(404, "No such repository")
        return repo

    def visible_run(user: dict, run_id: str) -> Path:
        run_dir = _run_dir(run_id)
        if (summarize(run_dir)["repo"] or "").lower() not in connected(user):
            raise HTTPException(404, "No such run")
        return run_dir

    def runs_left(user_id: int) -> int | None:
        if not runs_per_day:
            return None
        return max(0, runs_per_day - store.run_requests_since(user_id, time.time() - DAY_SECONDS))

    def account(user_id: int, email: str) -> dict:
        return {"email": email, "can_open_pr": open_pr, "runs_left": runs_left(user_id)}

    def start_session(request: Request, response: Response, user_id: int, email: str) -> dict:
        # Behind the tunnel, uvicorn's proxy headers make the scheme https, so the cookie is Secure.
        response.set_cookie(SESSION_COOKIE, store.create_session(user_id), max_age=SESSION_SECONDS,
                            httponly=True, samesite="lax", secure=request.url.scheme == "https")
        return account(user_id, email)

    def client_key(request: Request) -> str:
        return f"ip:{request.client.host if request.client else 'unknown'}"

    # --- pages and accounts ---

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return DASHBOARD_HTML.read_text()

    @app.get("/api/config")
    def config():
        return {"invite_required": bool(invite)}

    @app.post("/api/signup")
    def signup(body: Credentials, request: Request, response: Response):
        email = body.email.strip().lower()
        if throttle.blocked(client_key(request)):
            raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
        if invite and not hmac.compare_digest(body.invite.strip().encode(), invite.encode()):
            throttle.fail(client_key(request))
            raise HTTPException(403, "That invite code isn't valid")
        if not EMAIL.fullmatch(email):
            raise HTTPException(400, "Enter a valid email address")
        if len(body.password) < MIN_PASSWORD:
            raise HTTPException(400, f"Use a password of at least {MIN_PASSWORD} characters")
        try:
            user_id = store.create_user(email, body.password)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        return start_session(request, response, user_id, email)

    @app.post("/api/login")
    def login(body: Credentials, request: Request, response: Response):
        email = body.email.strip().lower()
        keys = (f"email:{email}", client_key(request))
        if throttle.blocked(*keys):
            raise HTTPException(429, "Too many failed sign-ins. Try again in a few minutes.")
        user_id = store.authenticate(email, body.password)
        if user_id is None:
            throttle.fail(*keys)
            raise HTTPException(401, "Wrong email or password")
        return start_session(request, response, user_id, email)

    @app.post("/api/logout")
    def logout(response: Response, pp_session: str | None = Cookie(None)):
        if pp_session:
            store.delete_session(pp_session)
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(user: dict = Depends(current_user)):
        return account(user["id"], user["email"])

    # --- repositories and issues ---

    @app.get("/api/repos")
    def list_repos(user: dict = Depends(current_user)):
        return store.list_repos(user["id"])

    @app.post("/api/repos")
    def add_repo(body: RepoIn, user: dict = Depends(current_user)):
        name, token = parse_repo(body.repo), body.token.strip()
        if not name:
            raise HTTPException(400, "Enter the repository as owner/name or its github.com URL")
        if not token:
            raise HTTPException(400, "Paste a GitHub personal access token")
        try:
            info = GitHub(token).get_repo(name)
        except (GitHubError, OSError):
            raise HTTPException(400, "GitHub rejected this token for that repository. Check the name and "
                                     "that the token has access to it.") from None
        if not info.get("permissions", {}).get("push"):
            raise HTTPException(400, "This token can read the repository but can't write to it. Give it "
                                     "Contents, Issues and Pull requests: read and write.")
        return store.add_repo(user["id"], info["full_name"], token)

    @app.delete("/api/repos/{repo_id}")
    def remove_repo(repo_id: int, user: dict = Depends(current_user)):
        if not store.delete_repo(user["id"], repo_id):
            raise HTTPException(404, "No such repository")
        return {"ok": True}

    @app.get("/api/repos/{repo_id}/issues")
    def list_issues(repo_id: int, user: dict = Depends(current_user)):
        repo = user_repo(user, repo_id)
        try:
            issues = GitHub(repo["token"]).list_issues(repo["full_name"])
        except (GitHubError, OSError) as e:
            raise HTTPException(502, f"GitHub request failed: {e}"[:300]) from None
        return [{"number": i["number"], "title": i["title"], "url": i["html_url"],
                 "author": (i.get("user") or {}).get("login"), "created_at": i["created_at"],
                 "labels": [label["name"] for label in i.get("labels", [])]} for i in issues]

    @app.post("/api/repos/{repo_id}/issues/{number}/fix")
    def start_fix(repo_id: int, number: int, body: FixIn, user: dict = Depends(current_user)):
        repo = user_repo(user, repo_id)
        if runs_left(user["id"]) == 0:
            raise HTTPException(429, f"This account has used its {runs_per_day} runs for the last 24 hours. "
                                     "The limit keeps the free model quota from running out.")
        try:
            issue = GitHub(repo["token"]).get_issue(repo["full_name"], number)
        except (GitHubError, OSError):
            raise HTTPException(404, "Issue not found") from None
        if "pull_request" in issue:
            raise HTTPException(400, "That number is a pull request, not an issue")
        if not enqueue(repo["full_name"], number, issue["title"], repo["token"], body.open_pr):
            raise HTTPException(409, "This issue is already queued")
        store.record_run_request(user["id"])
        return {"queued": f"{repo['full_name']}#{number}", "runs_left": runs_left(user["id"])}

    # --- runs ---

    @app.get("/api/runs")
    def list_runs(user: dict = Depends(current_user)):
        names = connected(user)
        with lock:
            queued = [{"repo": j["repo"], "number": j["number"], "title": j["title"]}
                      for j in pending if j["repo"].lower() in names]
        runs = [s for s in map(summarize, run_dirs()) if (s["repo"] or "").lower() in names]
        return {"queued": queued, "runs": runs[:50]}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, user: dict = Depends(current_user)):
        run_dir = visible_run(user, run_id)
        return {**summarize(run_dir), "events": read_events(run_dir)}

    @app.get("/api/runs/{run_id}/report", response_class=PlainTextResponse)
    def get_report(run_id: str, user: dict = Depends(current_user)):
        run_dir = visible_run(user, run_id)
        for name in ("proof.md", "escalation.md"):
            if (run_dir / name).exists():
                return (run_dir / name).read_text()
        raise HTTPException(404, "No report yet")

    # --- GitHub webhook ---

    @app.post("/webhook")
    async def webhook(request: Request, x_github_event: str = Header(""),
                      x_hub_signature_256: str | None = Header(None)):
        body = await request.body()
        if not verify_signature(secret, body, x_hub_signature_256):
            raise HTTPException(401, "Invalid or missing signature")
        payload = json.loads(body)
        if (x_github_event == "issues" and payload.get("action") == "labeled"
                and payload.get("label", {}).get("name") == label
                and "pull_request" not in payload.get("issue", {})):
            repo, issue = payload["repository"]["full_name"], payload["issue"]
            token = store.token_for(repo) or os.getenv("GITHUB_TOKEN", "").strip()
            if not token:
                return {"ignored": f"{repo} is not connected"}
            enqueue(repo, issue["number"], issue.get("title", ""), token, True)
            return {"queued": f"{repo}#{issue['number']}"}
        return {"ignored": x_github_event or "unknown"}

    return app
