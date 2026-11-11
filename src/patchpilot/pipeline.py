"""End-to-end run: issue -> sandbox -> agent -> Proof Pack -> PR (or escalation comment)."""

import os
import time
from pathlib import Path

from patchpilot.agent import Outcome, RepairAgent
from patchpilot.github_client import GitHub
from patchpilot.llm import LLM
from patchpilot.memory import RunMemory
from patchpilot.proofpack import render_escalation, render_proof_pack
from patchpilot.tools.sandbox import ensure_image, run_tests
from patchpilot.tools.workspace import Workspace
from patchpilot.verifier import pytest_summary

RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"


def fix_issue(repo: str, number: int, local: Path | None = None, open_pr: bool = False,
              token: str | None = None) -> Outcome:
    token = (token or os.environ["GITHUB_TOKEN"]).strip()
    gh = GitHub(token)
    issue = gh.get_issue(repo, number)

    run_dir = RUNS_DIR / f"{repo.replace('/', '-')}-{number}-{time.strftime('%Y%m%d-%H%M%S')}"
    memory = RunMemory(run_dir)
    memory.note("issue", repo=repo, number=number, title=issue["title"], url=issue["html_url"], open_pr=open_pr)
    print(f"Issue #{number}: {issue['title']}\nRun dir: {run_dir}")
    try:
        outcome = _run(gh, repo, number, issue, run_dir, memory, local, open_pr, token)
    except Exception as e:
        memory.note("outcome", status="failed", reason=f"{type(e).__name__}: {e}"[:500])
        raise
    return outcome


def _run(gh: GitHub, repo: str, number: int, issue: dict, run_dir: Path, memory: RunMemory,
         local: Path | None, open_pr: bool, token: str) -> Outcome:
    if open_pr:
        gh.comment(repo, number, "PatchPilot is investigating this issue. It will reproduce the bug "
                                 "before changing any code.")
    if local:
        ws = Workspace.copy_local(local, run_dir / "repo")
    else:
        ws = Workspace.clone(repo, run_dir / "repo", token)
    ensure_image()

    baseline = run_tests(ws.root)
    memory.note("baseline", passed=baseline.passed, summary=pytest_summary(baseline.output))
    print(f"Baseline tests: {pytest_summary(baseline.output)}")

    llm = LLM()
    agent = RepairAgent(llm, ws, memory, issue)
    outcome = agent.run()
    print(f"\nOutcome: {outcome.status.upper()} {outcome.reason}")

    if outcome.status == "fixed":
        proof = render_proof_pack(issue, outcome, agent.verifier, memory, ws.diff(),
                                  llm.last_model or "unknown model", agent.steps)
        (run_dir / "proof.md").write_text(proof)
        print(f"Proof Pack: {run_dir / 'proof.md'}")
        if open_pr:
            branch = f"patchpilot/issue-{number}-{run_dir.name.rsplit('-', 2)[-1]}"
            base = gh.get_repo(repo)["default_branch"]
            ws.commit_and_push(branch, f"Fix #{number}: {issue['title']}", repo, token)
            pr = gh.create_pr(repo, branch, base, f"Fix #{number}: {issue['title']}", proof)
            gh.comment(repo, number, f"PatchPilot opened #{pr['number']} with a verified fix. Ready for human review.")
            memory.note("pr", url=pr["html_url"], number=pr["number"])
            print(f"Pull request: {pr['html_url']}")
    else:
        report = render_escalation(issue, outcome, agent.verifier, memory)
        (run_dir / "escalation.md").write_text(report)
        if open_pr:
            gh.comment(repo, number, report)
            memory.note("escalation_posted")
            print("Posted escalation comment on the issue.")

    # Recorded last so the dashboard shows a run as finished only once the PR/comment exists.
    memory.note("outcome", status=outcome.status, reason=outcome.reason)
    return outcome
