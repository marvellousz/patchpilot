import argparse
import os
import tempfile
from pathlib import Path

from patchpilot.config import llm_chain
from patchpilot.llm import LLM
from patchpilot.pipeline import fix_issue
from patchpilot.tools.sandbox import ensure_image, run_tests
from patchpilot.tools.workspace import Workspace


def ping() -> None:
    for ep in llm_chain():
        try:
            reply = LLM([ep]).chat([{"role": "user", "content": "Reply with exactly: pong"}])
            print(f"{ep.label}: {(reply.content or '').strip()}")
        except Exception as e:
            print(f"{ep.label}: FAILED {str(e)[:200]}")


def sandbox(repo: str) -> None:
    ensure_image()
    with tempfile.TemporaryDirectory(prefix="patchpilot-") as tmp:
        ws = Workspace.copy_local(Path(repo), Path(tmp) / "repo")
        print(run_tests(ws.root).summary())


def demo_account(email: str, password: str, repo: str) -> None:
    """Create (or reset) a login with `repo` already connected using GITHUB_TOKEN from .env."""
    from patchpilot.github_client import GitHub
    from patchpilot.store import Store

    token = os.environ["GITHUB_TOKEN"].strip()
    info = GitHub(token).get_repo(repo)
    if not info.get("permissions", {}).get("push"):
        raise SystemExit(f"GITHUB_TOKEN can't write to {repo}")
    store = Store()
    email = email.strip().lower()
    user_id = store.user_id(email)
    if user_id is None:
        user_id = store.create_user(email, password)
    else:
        store.set_password(user_id, password)
    store.add_repo(user_id, info["full_name"], token)
    print(f"{email} can sign in with {info['full_name']} connected.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="patchpilot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping", help="Check that the configured LLM providers respond")
    sb = sub.add_parser("sandbox", help="Run a local repo's tests inside the sandbox")
    sb.add_argument("repo", help="Path to a local repository")
    fx = sub.add_parser("fix", help="Repair a GitHub issue (dry run unless --open-pr)")
    fx.add_argument("repo", help="owner/name")
    fx.add_argument("issue", type=int)
    fx.add_argument("--open-pr", action="store_true", help="Push a branch and open the PR (or post the escalation)")
    fx.add_argument("--local", type=Path, help="Use a local checkout instead of cloning (dry run only)")
    sv = sub.add_parser("serve", help="Run the webhook receiver and live dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--dry-run", action="store_true", help="Run the agent but don't open PRs or comment")
    da = sub.add_parser("demo-account", help="Create a login with a repo already connected via GITHUB_TOKEN")
    da.add_argument("email")
    da.add_argument("password")
    da.add_argument("repo", help="owner/name")
    args = parser.parse_args()

    if args.command == "ping":
        ping()
    elif args.command == "sandbox":
        sandbox(args.repo)
    elif args.command == "fix":
        if args.local and args.open_pr:
            parser.error("--local cannot be combined with --open-pr")
        outcome = fix_issue(args.repo, args.issue, local=args.local, open_pr=args.open_pr)
        raise SystemExit(0 if outcome.status == "fixed" else 1)
    elif args.command == "serve":
        import uvicorn

        from patchpilot.server import create_app

        # Trust X-Forwarded-* only from the local tunnel, so client IPs and https are seen correctly.
        uvicorn.run(create_app(open_pr=not args.dry_run), host=args.host, port=args.port,
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    elif args.command == "demo-account":
        demo_account(args.email, args.password, args.repo)
