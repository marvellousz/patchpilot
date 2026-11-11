"""Runs commands against a workspace inside a throwaway Docker container with no network."""

import os
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

IMAGE = "patchpilot-sandbox:latest"
SANDBOX_DIR = Path(__file__).resolve().parents[3] / "sandbox"
MAX_OUTPUT_CHARS = 3000


@dataclass
class CommandResult:
    command: str
    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self) -> str:
        status = "TIMED OUT" if self.timed_out else f"exit code {self.exit_code}"
        return f"$ {self.command}\n[{status}]\n{self.output}"


def _truncate(text: str) -> str:
    # Keep both ends: the start shows what ran, the end shows the failure summary.
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n... [{len(text) - MAX_OUTPUT_CHARS} chars truncated] ...\n{text[-half:]}"


def ensure_image() -> None:
    exists = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    if exists.returncode != 0:
        subprocess.run(["docker", "build", "-q", "-t", IMAGE, str(SANDBOX_DIR)], check=True)


def run_command(workspace_root: Path, command: str, timeout: int = 120) -> CommandResult:
    name = f"patchpilot-{uuid.uuid4().hex[:8]}"
    docker_cmd = [
        "docker", "run", "--rm", "--name", name,
        "--network", "none",
        "--memory", "512m", "--cpus", "1", "--pids-limit", "256",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp",
        "-v", f"{Path(workspace_root).resolve()}:/work",
        "-w", "/work",
        IMAGE, "sh", "-c", command,
    ]
    try:
        proc = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)
        return CommandResult(command, 124, f"Command exceeded {timeout}s and was killed.", True)
    return CommandResult(command, proc.returncode, _truncate(proc.stdout + proc.stderr))


def run_tests(workspace_root: Path, target: str = "", timeout: int = 120) -> CommandResult:
    command = "python -m pytest -q -p no:cacheprovider"
    if target:
        command += f" {shlex.quote(target)}"
    return run_command(workspace_root, command, timeout)
