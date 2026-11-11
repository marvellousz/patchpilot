"""A checked-out copy of the target repo. All agent file access goes through here."""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

IGNORED_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}
MAX_READ_LINES = 400
MAX_SEARCH_RESULTS = 50


class WorkspaceError(Exception):
    pass


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    @classmethod
    def clone(cls, repo: str, dest: Path, token: str | None = None) -> "Workspace":
        """Clone `owner/name` from GitHub. The token is used for the clone only, not stored."""
        auth = f"x-access-token:{token}@" if token else ""
        _git(Path.cwd(), "clone", "--quiet", f"https://{auth}github.com/{repo}.git", str(dest))
        _git(dest, "remote", "set-url", "origin", f"https://github.com/{repo}.git")
        return cls(dest)

    @classmethod
    def copy_local(cls, source: Path, dest: Path) -> "Workspace":
        """Copy a local repo (for offline development), making it a git repo if needed."""
        shutil.copytree(source, dest, ignore=shutil.ignore_patterns(*(IGNORED_DIRS - {".git"})))
        if not (dest / ".git").exists():
            _git(dest, "init", "--quiet")
            _git(dest, "add", "-A")
            _git(dest, "-c", "user.name=patchpilot", "-c", "user.email=patchpilot@localhost",
                 "commit", "--quiet", "-m", "baseline")
        return cls(dest)

    def _resolve(self, path: str) -> Path:
        full = (self.root / path).resolve()
        if not full.is_relative_to(self.root):
            raise WorkspaceError(f"Path is outside the repository: {path}")
        if ".git" in full.relative_to(self.root).parts:
            raise WorkspaceError("The .git directory is off limits.")
        return full

    def _walk(self, path: str):
        base = self._resolve(path)
        if base.is_file():
            yield base
            return
        for p in sorted(base.rglob("*")):
            if p.is_file() and not IGNORED_DIRS.intersection(p.relative_to(self.root).parts):
                yield p

    def list_files(self, path: str = ".") -> list[str]:
        return [str(p.relative_to(self.root)) for p in self._walk(path)]

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        full = self._resolve(path)
        if not full.is_file():
            raise WorkspaceError(f"No such file: {path}")
        lines = full.read_text(errors="replace").splitlines()
        start = max(start, 1)
        end = min(end or len(lines), len(lines), start + MAX_READ_LINES - 1)
        numbered = [f"{n:>4} | {lines[n - 1]}" for n in range(start, end + 1)]
        if end < len(lines):
            numbered.append(f"... ({len(lines) - end} more lines)")
        return "\n".join(numbered)

    def search(self, pattern: str, path: str = ".") -> list[str]:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise WorkspaceError(f"Bad regex: {e}") from e
        hits = []
        for p in self._walk(path):
            try:
                text = p.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{p.relative_to(self.root)}:{n}: {line.strip()}")
                    if len(hits) >= MAX_SEARCH_RESULTS:
                        return hits
        return hits

    def write_file(self, path: str, content: str) -> None:
        full = self._resolve(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)

    def replace_in_file(self, path: str, old: str, new: str) -> None:
        full = self._resolve(path)
        if not full.is_file():
            raise WorkspaceError(f"No such file: {path}")
        text = full.read_text()
        count = text.count(old)
        if count != 1:
            raise WorkspaceError(f"`old` must match exactly once in {path}, found {count} matches.")
        full.write_text(text.replace(old, new))

    def diff(self) -> str:
        _git(self.root, "add", "-A", "--intent-to-add")
        return _git(self.root, "diff")

    def changed_files(self, diff_filter: str = "") -> list[str]:
        """Changed paths; `diff_filter` takes git's letters, e.g. "A" added, "MD" modified/deleted."""
        _git(self.root, "add", "-A", "--intent-to-add")
        args = ["diff", "--name-only"] + ([f"--diff-filter={diff_filter}"] if diff_filter else [])
        return _git(self.root, *args).split()

    def numstat(self) -> dict[str, tuple[int, int]]:
        """Lines added and removed per changed file."""
        _git(self.root, "add", "-A", "--intent-to-add")
        stats = {}
        for line in _git(self.root, "diff", "--numstat").splitlines():
            added, removed, path = line.split("\t", 2)
            stats[path] = (int(added) if added != "-" else 0, int(removed) if removed != "-" else 0)
        return stats

    def file_hash(self, path: str) -> str | None:
        full = self._resolve(path)
        return hashlib.sha256(full.read_bytes()).hexdigest() if full.is_file() else None

    def commit_and_push(self, branch: str, message: str, repo: str, token: str) -> None:
        """Commit all changes on a new branch and push it. Uses the machine's git identity."""
        _git(self.root, "checkout", "--quiet", "-b", branch)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "--quiet", "-m", message)
        _git(self.root, "push", "--quiet",
             f"https://x-access-token:{token}@github.com/{repo}.git", f"{branch}:{branch}")
