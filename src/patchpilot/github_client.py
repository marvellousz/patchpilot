"""Minimal GitHub REST client: issues, comments and pull requests."""

import json
import urllib.error
import urllib.request

API = "https://api.github.com"


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def _request(self, method: str, path: str, body: dict | None = None) -> dict | list:
        req = urllib.request.Request(
            f"{API}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise GitHubError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}") from e

    def get_repo(self, repo: str) -> dict:
        return self._request("GET", f"/repos/{repo}")

    def get_issue(self, repo: str, number: int) -> dict:
        return self._request("GET", f"/repos/{repo}/issues/{number}")

    def list_issues(self, repo: str) -> list[dict]:
        issues = self._request("GET", f"/repos/{repo}/issues?state=open&per_page=50")
        return [i for i in issues if "pull_request" not in i]

    def comment(self, repo: str, number: int, body: str) -> dict:
        return self._request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict:
        return self._request("POST", f"/repos/{repo}/pulls",
                             {"title": title, "head": head, "base": base, "body": body})
