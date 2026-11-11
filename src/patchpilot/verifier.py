"""Quality gates. The model proposes; these checks decide whether a phase is actually done."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from patchpilot.tools.sandbox import CommandResult, run_tests
from patchpilot.tools.workspace import Workspace

MAX_SOURCE_FILES = 3
MAX_SOURCE_LINES = 60
PYTEST_TESTS_FAILED = 1  # exit code for "tests ran and some failed", as opposed to collection errors


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    return p.parts[0] in ("tests", "test") or p.name.startswith("test_") or p.name.endswith("_test.py")


def pytest_summary(output: str) -> str:
    matches = re.findall(r"\d+ (?:passed|failed|error)[^\n]*", output)
    return matches[-1].strip() if matches else "no summary"


@dataclass
class GateResult:
    ok: bool
    message: str


class Verifier:
    def __init__(self, workspace: Workspace):
        self.ws = workspace
        self.repro_file: str | None = None
        self.repro_hash: str | None = None
        self.repro_before: CommandResult | None = None
        self.symptom_reason = ""
        self.repro_after: CommandResult | None = None
        self.suite_after: CommandResult | None = None
        self.source_stats: dict[str, tuple[int, int]] = {}

    def check_reproduction(self, test_file: str) -> GateResult:
        changed = self.ws.changed_files()
        if test_file not in changed:
            return GateResult(False, f"{test_file} is not a new or modified file in the repository.")
        if not is_test_file(test_file):
            return GateResult(False, f"{test_file} is not a test file (put it in tests/ as test_*.py).")
        source = [f for f in changed if not is_test_file(f)]
        if source:
            return GateResult(False, f"Only add tests while reproducing. Revert your changes to: {source}")

        result = run_tests(self.ws.root, test_file)
        if result.exit_code != PYTEST_TESTS_FAILED:
            return GateResult(False, (
                f"The reproduction must fail with a test failure (pytest exit code 1) but got exit "
                f"code {result.exit_code}. Collection or import errors don't count.\n{result.output}"
            ))

        self.repro_file = test_file
        self.repro_hash = self.ws.file_hash(test_file)
        self.repro_before = result
        return GateResult(True, f"{test_file} fails on the current code ({pytest_summary(result.output)}).")

    def clear_reproduction(self) -> None:
        self.repro_file = self.repro_hash = self.repro_before = None

    def check_fix(self) -> GateResult:
        if self.repro_file is None:
            return GateResult(False, "Submit a verified reproduction first.")
        if self.ws.file_hash(self.repro_file) != self.repro_hash:
            return GateResult(False, f"The reproduction test {self.repro_file} was changed. Restore it.")

        touched_tests = [f for f in self.ws.changed_files("MD") if is_test_file(f) and f != self.repro_file]
        if touched_tests:
            return GateResult(False, f"Don't modify or delete existing tests: {touched_tests}")

        stats = {f: s for f, s in self.ws.numstat().items() if not is_test_file(f)}
        if not stats:
            return GateResult(False, "No source files were changed.")
        lines = sum(a + r for a, r in stats.values())
        if len(stats) > MAX_SOURCE_FILES or lines > MAX_SOURCE_LINES:
            return GateResult(False, (
                f"The fix touches {len(stats)} source files / {lines} lines; the limit is "
                f"{MAX_SOURCE_FILES} files / {MAX_SOURCE_LINES} lines. Make it smaller."
            ))

        repro = run_tests(self.ws.root, self.repro_file)
        if not repro.passed:
            return GateResult(False, f"The reproduction test still fails:\n{repro.output}")
        suite = run_tests(self.ws.root)
        if not suite.passed:
            return GateResult(False, f"The full test suite fails:\n{suite.output}")

        self.repro_after, self.suite_after, self.source_stats = repro, suite, stats
        return GateResult(True, f"All gates passed ({pytest_summary(suite.output)}).")
