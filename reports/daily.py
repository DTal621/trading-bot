"""
daily.py — the daily report. This is your learning surface, and it's carefully
designed to teach you the right lessons, not flatter the bot.

Two rules baked in:
  1. It REPORTS and forms HYPOTHESES; it never changes the live strategy. Every
     suggestion lands in a backlog that must clear the walk-forward gate first.
  2. It constantly reminds you of sample size. A day is noise. The report shows
     cumulative stats next to daily ones so you don't over-read a good Tuesday.

Read it as a lab notebook, not a scoreboard.
"""
from __future__ import annotations

from datetime import date
from statistics import mean, pstdev

from core.logstore import DecisionLog
from core.proposals import ProposalBacklog, ProposalStatus


def build_daily_report(log: DecisionLog, today: date,
                       backlog: ProposalBacklog | None = None,
                       llm_observations: list[str] | None = None) -> str:
    decisions = list(log.read("decision"))
    today_decisions = [d for d in decisions if d["ts"].startswith(today.isoformat())]

    actions = _count(today_decisions, "action")
    holds = actions.get("HOLD", 0)
    trades = sum(v for k, v in actions.items() if k != "HOLD")

    # Why did the bot pass on things? The reasons are where the learning is.
    hold_reasons = _count(
        [d for d in today_decisions if d["action"] == "HOLD"], "reason", top=5)

    by_version = _count(today_decisions, "strategy_version")

    lines = [
        f"# Daily report — {today.isoformat()}",
        "",
        f"Decisions today: {len(today_decisions)} (trades: {trades}, holds: {holds})",
        f"Strategy version(s) running: {dict(by_version)}",
        "",
        "## Why it held (top reasons)",
        *[f"  - {r}: {n}" for r, n in hold_reasons.items()],
        "",
        "## Sample-size reality check",
        f"  Cumulative decisions logged: {len(decisions)}",
        _evidence_caveat(len(decisions)),
        "",
        "## Hypotheses for the backlog (NOT applied automatically)",
        *_hypotheses(today_decisions),
        "",
        "## What the LLM review noticed today",
        *( [f"  - {o}" for o in (llm_observations or [])] or ["  (no LLM review run)"] ),
        "",
        "## Open change proposals awaiting walk-forward (the gate, not the live bot)",
        *_render_proposals(backlog),
        "",
        "Reminder: nothing here changes the live strategy. Promote via walk-forward only.",
    ]
    return "\n".join(lines)


def _render_proposals(backlog: ProposalBacklog | None) -> list[str]:
    if backlog is None:
        return ["  (no backlog wired in)"]
    pending = backlog.by_status(ProposalStatus.PENDING_VALIDATION)
    awaiting = backlog.by_status(ProposalStatus.AWAITING_APPROVAL)
    if not pending and not awaiting:
        return ["  (none pending — good; most days shouldn't produce a change)"]
    out = []
    for p in pending:
        out.append(f"  - [validating next] {p['param_path']} {p['direction']} "
                   f"-> {p['proposed_value']} (n={p['sample_size']}): {p['hypothesis']}")
    for p in awaiting:
        out.append(f"  - [AWAITING YOUR APPROVAL] {p['param_path']} {p['direction']} "
                   f"-> {p['proposed_value']}: cleared walk-forward")
    return out


def _count(records, key, top=None):
    counts = {}
    for r in records:
        v = r.get(key)
        counts[v] = counts.get(v, 0) + 1
    items = sorted(counts.items(), key=lambda kv: -kv[1])
    return dict(items[:top] if top else items)


def _evidence_caveat(n: int) -> str:
    if n < 100:
        return f"  ⚠ {n} decisions is far too few to conclude ANYTHING. Keep collecting."
    if n < 500:
        return f"  ⚠ {n} decisions — directional at best. Resist tuning on this."
    return f"  {n} decisions — enough to start forming testable hypotheses."


def _hypotheses(today_decisions) -> list[str]:
    # Cheap, transparent heuristics that SURFACE questions for offline testing.
    out = []
    holds = [d for d in today_decisions if d["action"] == "HOLD"]
    conf_blocked = [d for d in holds if "confidence" in d.get("reason", "")]
    if len(conf_blocked) > 0.6 * max(1, len(holds)):
        out.append("  - Confidence gate is rejecting most setups. Test a lower "
                   "min_confidence OFFLINE across folds before touching live.")
    vol_blocked = [d for d in holds if "volume" in d.get("reason", "")]
    if len(vol_blocked) > 0.6 * max(1, len(holds)):
        out.append("  - Volume confirmation is the main blocker. Worth a walk-forward "
                   "test of a softer z-gate — but expect more false signals.")
    if not out:
        out.append("  - No strong patterns today. (Good. Most days shouldn't generate "
                   "an actionable hypothesis.)")
    return out
