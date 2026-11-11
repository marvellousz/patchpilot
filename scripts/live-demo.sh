#!/usr/bin/env bash
# Serve PatchPilot on localhost and expose it through a Cloudflare quick tunnel.
# The public https://<random>.trycloudflare.com URL is printed by cloudflared below.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v cloudflared >/dev/null; then
  echo "cloudflared is not installed. See 'Live demo' in README.md." >&2
  exit 1
fi

uv run patchpilot serve --port 8000 &
server=$!
trap 'kill "$server" 2>/dev/null' EXIT

cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000
