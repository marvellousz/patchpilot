"""Run memory: issue facts, hypotheses, gate results and test evidence, persisted as JSONL."""

import json
import time
from pathlib import Path


class RunMemory:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "memory.jsonl"
        self.events: list[dict] = []

    def note(self, kind: str, **data) -> dict:
        event = {"time": time.strftime("%H:%M:%S"), "kind": kind, **data}
        self.events.append(event)
        with self.path.open("a") as f:
            f.write(json.dumps(event) + "\n")
        return event

    def of_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]
