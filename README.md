# PatchPilot AI

From GitHub issue to a tested, review-ready pull request.

PatchPilot reads a bug report, reproduces the failure in a sandbox, fixes the root cause with a
regression test, verifies the suite, and opens a PR carrying a **Bug Proof Pack**. A human approves
every merge.

## How it works

```
issue labelled "patchpilot"
  -> clone repo, run baseline tests in the Docker sandbox (no network)
  -> REPRODUCE  agent writes a failing test          gate: fails on current code, only tests changed,
                                                           and the failure matches the reported symptom
  -> FIX        agent makes a minimal source change  gate: repro passes, full suite green, repro test
                                                           untouched, existing tests untouched, small diff
  -> PR with the Bug Proof Pack      (or an escalation comment when the issue can't be reproduced)
```

- **Planner**: an LLM tool-calling loop ([agent.py](src/patchpilot/agent.py)).
- **Repository tools**: list/read/search/edit files in an isolated checkout ([workspace.py](src/patchpilot/tools/workspace.py)).
- **Execution tools**: pytest and shell commands in a throwaway container with no network and resource limits ([sandbox.py](src/patchpilot/tools/sandbox.py)).
- **Verifier**: the gates above; the model proposes, code decides ([verifier.py](src/patchpilot/verifier.py)).
- **Memory**: issue facts, hypotheses, gate results and evidence in `runs/<id>/memory.jsonl`, plus the full transcript.

The model never sees the GitHub token: pushing and opening PRs happen outside the agent loop, and only after every gate passes.

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync --extra dev
cp .env.example .env    # add GROQ_API_KEY / GEMINI_API_KEY, GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET
uv run patchpilot ping  # check each model in LLM_CHAIN responds
```

`LLM_CHAIN` lists models in order of preference. Free-tier limits are per model, so PatchPilot
moves to the next one when a model is rate-limited.

The GitHub token is a fine-grained token on the target repo with **Contents**, **Issues** and
**Pull requests** set to read and write.

## Usage

```bash
# Dry run: reproduce and fix locally, write runs/<id>/proof.md, push nothing
uv run patchpilot fix owner/repo 42

# Full run: push a branch, open the PR (or post the escalation comment)
uv run patchpilot fix owner/repo 42 --open-pr

# Web app + webhook receiver at http://127.0.0.1:8000
uv run patchpilot serve
```

### Web app

Sign up, then connect a repository with a fine-grained personal access token scoped to that repo
(**Contents**, **Issues** and **Pull requests**: read and write). PatchPilot checks the token with
GitHub, lists the repo's open issues, and starts a run when you click **Fix with PatchPilot**. The run
page shows the stage pipeline, the agent trace and the Proof Pack as they happen.

- Accounts, sessions and connected repos live in SQLite under `data/`. Passwords are hashed with scrypt.
- Tokens are encrypted with a key from `PATCHPILOT_SECRET_KEY`, or one generated into `data/secret.key`.
  They're used server-side for cloning, pushing and the GitHub API, and never sent to the browser or the model.
- A run is visible to every user who connected its repository.
- `serve --dry-run` makes every run a dry run, whatever the UI asks for.

### Live demo from a laptop

The server runs locally (it needs Docker for the sandbox) and a Cloudflare quick tunnel gives it a
public HTTPS URL. No Cloudflare account is needed.

```bash
# once: install cloudflared (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
curl -fsSL -o ~/.local/bin/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x ~/.local/bin/cloudflared

# once: a login for judges with the demo repo already connected (uses GITHUB_TOKEN from .env)
uv run patchpilot demo-account judges@patchpilot.dev '<password>' owner/demo-repo

./scripts/live-demo.sh   # prints https://<random>.trycloudflare.com
```

Settings in `.env` for a public server:

- `PATCHPILOT_INVITE_CODE`: required to sign up. Leave it unset to allow open sign-up.
- `PATCHPILOT_RUNS_PER_DAY` (default 5): runs each account can start per 24 hours, so the free
  model quota lasts. `0` removes the limit.

Failed sign-ins are throttled per email and per IP, and the session cookie is `Secure` over HTTPS.
The quick-tunnel URL changes every time it starts, so update the webhook's payload URL if you use one.
Keep the laptop plugged in with sleep turned off while judges have the link.

### Connecting the GitHub webhook

1. Get a public URL that forwards to the server, e.g. create a channel at <https://smee.io> and run
   `npx smee-client --url https://smee.io/<channel> --target http://127.0.0.1:8000/webhook`.
2. In the repo: **Settings -> Webhooks -> Add webhook**. Payload URL: the smee URL. Content type:
   `application/json`. Secret: the `GITHUB_WEBHOOK_SECRET` from `.env`. Events: **Issues** only.
3. Add the `patchpilot` label to an issue. The run uses the token of a user who connected the repo
   (or `GITHUB_TOKEN` from `.env`) and appears on the dashboard.

## Tests

```bash
uv run pytest
```

The verifier tests run pytest inside the sandbox, so Docker must be running.
