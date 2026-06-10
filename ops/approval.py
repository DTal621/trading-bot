"""
approval.py — the notification + approval transport.

Telegram is just a way to show you a proposal WITH its walk-forward evidence and
collect a yes/no. It owns no logic about whether a change is good — that lives in
the walk-forward gate and your judgement. Swapping Telegram for email/Slack/CLI
changes nothing downstream.

Security: the listener honors commands ONLY from the configured chat_id. Without
that, anyone who found the bot could approve a change. The check is in
_parse_updates and is not optional.
"""
from __future__ import annotations

from typing import Protocol
from dataclasses import dataclass

_ACTIONS = {"approve", "reject", "keep", "cancel"}


@dataclass
class ApprovalRequest:
    proposal_id: str
    summary: str            # human-readable: param, change, hypothesis
    evidence: str           # formatted walk-forward out-of-sample result
    kind: str = "change"    # "change" | "trial_result"


class ApprovalChannel(Protocol):
    def send_for_approval(self, req: ApprovalRequest) -> None: ...
    def send_report(self, text: str) -> None: ...
    def send_trial_decision(self, proposal_id: str, text: str) -> None: ...
    def fetch_decisions(self, known_ids: set[str]) -> list[tuple[str, str]]: ...
    def poll_decision(self, proposal_id: str) -> str | None: ...


def format_proposal(p: dict, walkforward: dict) -> ApprovalRequest:
    folds = walkforward.get("folds", [])
    passed = walkforward.get("passed_folds", "?")
    oos_net = walkforward.get("oos_net_pnl", "?")
    summary = (f"[{p['param_path']}] {p['direction']} "
               f"{p['current_value']} -> {p['proposed_value']}\n"
               f"Hypothesis: {p['hypothesis']}\n"
               f"Evidence cited: {p['evidence_summary']} (n={p['sample_size']})")
    evidence = (f"Walk-forward (out-of-sample): {passed}/{len(folds)} folds passed, "
                f"OOS net {oos_net}. Approving deploys it to a 2-week shadow A/B "
                f"trial, not straight to permanent.")
    return ApprovalRequest(proposal_id=p["proposal_id"], summary=summary, evidence=evidence)


class TelegramApproval:
    """Real Telegram Bot API transport (HTTP via requests). Token in env, never repo."""
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self._offset = 0
        self._decisions: dict[str, str] = {}

    # --- outbound ---
    def send_for_approval(self, req: ApprovalRequest) -> None:
        text = f"PROPOSAL {req.proposal_id[:8]}\n\n{req.summary}\n\n{req.evidence}"
        self._send(text, [("Approve", f"approve:{req.proposal_id}"),
                          ("Reject", f"reject:{req.proposal_id}")])

    def send_report(self, text: str) -> None:
        self._send(text)

    def send_trial_decision(self, proposal_id: str, text: str) -> None:
        self._send(text, [("Keep", f"keep:{proposal_id}"),
                          ("Cancel", f"cancel:{proposal_id}")])

    # --- inbound ---
    def poll_decision(self, proposal_id: str) -> str | None:
        return self._decisions.get(proposal_id)

    def fetch_decisions(self, known_ids: set[str]) -> list[tuple[str, str]]:
        import requests
        url = self.API.format(token=self.bot_token, method="getUpdates")
        resp = requests.get(url, params={"offset": self._offset, "timeout": 30}, timeout=40)
        updates = resp.json().get("result", [])
        pairs = self._parse_updates(updates, known_ids)
        for pid, action in pairs:
            self._decisions[pid] = action
        return pairs

    def _parse_updates(self, updates: list, known_ids: set[str]) -> list[tuple[str, str]]:
        """Pure parser (testable without network). Honors owner chat_id only.
        Accepts button callbacks ('approve:<id>') and text commands ('approve <id8>')."""
        out: list[tuple[str, str]] = []
        for u in updates:
            self._offset = max(self._offset, u.get("update_id", -1) + 1)

            cq = u.get("callback_query")
            if cq:
                chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
                if chat != self.chat_id:
                    continue
                hit = self._match(cq.get("data", ""), known_ids, sep=":")
                if hit:
                    out.append(hit)
                continue

            msg = u.get("message")
            if msg:
                if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
                    continue
                hit = self._match(msg.get("text", ""), known_ids, sep=" ")
                if hit:
                    out.append(hit)
        return out

    @staticmethod
    def _match(raw: str, known_ids: set[str], sep: str):
        parts = raw.strip().split(sep, 1)
        if len(parts) != 2:
            return None
        action, token = parts[0].strip().lower(), parts[1].strip()
        if action not in _ACTIONS:
            return None
        # exact id (button) or short-prefix match (typed command)
        if token in known_ids:
            return (token, action)
        matches = [i for i in known_ids if i.startswith(token)]
        return (matches[0], action) if len(matches) == 1 else None

    def _send(self, text: str, buttons=None) -> None:
        import requests
        payload = {"chat_id": self.chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": label, "callback_data": data} for label, data in buttons]]}
        url = self.API.format(token=self.bot_token, method="sendMessage")
        requests.post(url, json=payload, timeout=20)


class ManualApproval:
    """CLI/test fallback so the whole pipeline runs before Telegram exists."""
    def __init__(self):
        self.outbox: list[str] = []
        self.inbox: dict[str, str] = {}

    def send_for_approval(self, req: ApprovalRequest) -> None:
        self.outbox.append(f"APPROVAL NEEDED {req.proposal_id}\n{req.summary}\n{req.evidence}")

    def send_report(self, text: str) -> None:
        self.outbox.append(text)

    def send_trial_decision(self, proposal_id: str, text: str) -> None:
        self.outbox.append(f"TRIAL DECISION {proposal_id}\n{text}")

    def fetch_decisions(self, known_ids: set[str]) -> list[tuple[str, str]]:
        return []

    def poll_decision(self, proposal_id: str) -> str | None:
        return self.inbox.get(proposal_id)
