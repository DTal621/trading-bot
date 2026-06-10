"""
proposals.py — the change backlog and its state machine.

A change moves through gates in a fixed order. It can never skip a gate and it
can never reach the live config except by passing ALL of them:

  PENDING_VALIDATION   LLM/human created it; nothing tested yet
        |  (walk-forward runs)
        v
  VALIDATION_FAILED  <--x  did not clear the out-of-sample gate (dead end)
        |  (passed)
        v
  AWAITING_APPROVAL    evidence attached, sent to the human via Telegram
        |                         |
        |  (human: no) -----------> REJECTED
        |  (human: yes)
        v
  DEPLOYED_TRIAL       now LIVE; previous version runs in SHADOW for an A/B
        |                         |
        |  (trial report: keep) ->|--> ADOPTED   (stays live)
        |  (trial report: cancel)----> REVERTED  (config rolled back)

The human approval gate sits AFTER the walk-forward gate on purpose: you approve
on out-of-sample evidence, not on a hunch. The trial is confirmation/monitoring,
never the proof.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
import json
import uuid

from core.schema import utcnow


class ProposalStatus(str, Enum):
    PENDING_VALIDATION = "PENDING_VALIDATION"
    VALIDATING = "VALIDATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REJECTED = "REJECTED"             # human said no
    DEPLOYED_TRIAL = "DEPLOYED_TRIAL"  # live + shadow A/B running
    ADOPTED = "ADOPTED"               # kept after trial
    REVERTED = "REVERTED"             # rolled back after trial


# Legal transitions. Anything not listed is rejected by the backlog.
_ALLOWED = {
    ProposalStatus.PENDING_VALIDATION: {ProposalStatus.VALIDATING},
    ProposalStatus.VALIDATING: {ProposalStatus.AWAITING_APPROVAL, ProposalStatus.VALIDATION_FAILED},
    ProposalStatus.AWAITING_APPROVAL: {ProposalStatus.DEPLOYED_TRIAL, ProposalStatus.REJECTED},
    ProposalStatus.DEPLOYED_TRIAL: {ProposalStatus.ADOPTED, ProposalStatus.REVERTED},
}


class IllegalTransition(Exception):
    pass


@dataclass
class ChangeProposal:
    proposal_id: str
    created_at: datetime
    source: str                  # "llm_review" | "human"
    param_path: str              # e.g. "params.entry_score_threshold"
    current_value: object
    proposed_value: object
    direction: str               # "increase" | "decrease" | "replace"
    hypothesis: str
    evidence_summary: str
    sample_size: int
    status: ProposalStatus = ProposalStatus.PENDING_VALIDATION

    @staticmethod
    def new(**kw) -> "ChangeProposal":
        return ChangeProposal(proposal_id=str(uuid.uuid4()), created_at=utcnow(), **kw)


class ProposalBacklog:
    """Append-only event log of proposals + status transitions, with extra
    payloads (walk-forward evidence, trial windows, base version for revert)
    carried on the transition records."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, p: ChangeProposal) -> None:
        self._write({"event": "add", "proposal": _ser(p)})

    def transition(self, proposal_id: str, to: ProposalStatus, payload: dict | None = None) -> None:
        cur = ProposalStatus(self._current_status(proposal_id))
        if to not in _ALLOWED.get(cur, set()):
            raise IllegalTransition(f"{cur.value} -> {to.value} not allowed")
        self._write({"event": "transition", "proposal_id": proposal_id,
                     "to": to.value, "at": utcnow().isoformat(), "payload": payload or {}})

    def by_status(self, status: ProposalStatus) -> list[dict]:
        added, latest = self._reconstruct()
        return [added[i] | {"status": s} for i, s in latest.items()
                if s == status.value and i in added]

    def get(self, proposal_id: str) -> dict | None:
        added, latest = self._reconstruct()
        if proposal_id not in added:
            return None
        return added[proposal_id] | {"status": latest[proposal_id]}

    def payload(self, proposal_id: str, key: str):
        """Most recent transition payload value for a key (e.g. trial_started_at)."""
        val = None
        for rec in self._read():
            if rec["event"] == "transition" and rec["proposal_id"] == proposal_id:
                if key in rec.get("payload", {}):
                    val = rec["payload"][key]
        return val

    # ── Trial-prompt tracking ──────────────────────────────────────────────────

    def mark_trial_prompt_sent(self, proposal_id: str) -> None:
        """
        Record that the Keep/Cancel trial prompt was sent for this proposal.
        Written as a lightweight non-transition event so it survives restarts
        without needing a new ProposalStatus value.
        """
        self._write({
            "event": "trial_prompt_sent",
            "proposal_id": proposal_id,
            "at": utcnow().isoformat(),
        })

    def trial_prompt_sent(self, proposal_id: str) -> bool:
        """Return True if the Keep/Cancel prompt has already been sent."""
        for rec in self._read():
            if (rec.get("event") == "trial_prompt_sent"
                    and rec.get("proposal_id") == proposal_id):
                return True
        return False

    def _current_status(self, proposal_id: str) -> str:
        _, latest = self._reconstruct()
        return latest.get(proposal_id, ProposalStatus.PENDING_VALIDATION.value)

    def _reconstruct(self):
        added: dict[str, dict] = {}
        latest: dict[str, str] = {}
        for rec in self._read():
            if rec["event"] == "add":
                p = rec["proposal"]
                added[p["proposal_id"]] = p
                latest[p["proposal_id"]] = p["status"]
            elif rec["event"] == "transition":
                latest[rec["proposal_id"]] = rec["to"]
        return added, latest

    def _write(self, obj: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(obj, default=str) + "\n")

    def _read(self):
        if not self.path.exists():
            return
        with self.path.open() as f:
            for line in f:
                yield json.loads(line)


def _ser(p: ChangeProposal) -> dict:
    d = p.__dict__.copy()
    d["created_at"] = p.created_at.isoformat()
    d["status"] = p.status.value
    return d
