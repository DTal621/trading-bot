"""
logstore.py — append-only structured logging. The single source of truth.

Every NewsEvent, Signal, Decision, OrderRecord and Fill is appended as a JSON
line, stamped with strategy_version and config_hash where relevant. This file is
what the daily report reads, what the offline evaluator replays, and what the
walk-forward harness mines for hypotheses. Append-only on purpose: you never
rewrite history, so live results can't be quietly massaged.

JSONL is fine to start. Move to SQLite/Parquet once volume grows; keep the
record types identical so nothing downstream changes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator
import json

from core.schema import to_jsonl


class DecisionLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, kind: str, record) -> None:
        line = json.dumps({"kind": kind, "data": json.loads(to_jsonl(record))})
        with self.path.open("a") as f:
            f.write(line + "\n")

    def read(self, kind: str | None = None) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open() as f:
            for line in f:
                rec = json.loads(line)
                if kind is None or rec["kind"] == kind:
                    yield rec["data"]
