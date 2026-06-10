"""
periodic.py — weekly / monthly roll-ups from the decision log.

Same discipline as the daily report: it summarizes, it does not change anything,
and it keeps reminding you that short windows are noise. Weekly/monthly views
exist to surface trends across many trades, not to make a verdict on a fortnight.
"""
from __future__ import annotations

from datetime import datetime

from core.logstore import DecisionLog


def build_period_report(log: DecisionLog, start: datetime, end: datetime, label: str) -> str:
    def in_window(iso: str) -> bool:
        return start <= datetime.fromisoformat(iso) <= end

    decisions = [d for d in log.read("decision") if in_window(d["ts"])]
    outs = [o for o in log.read("outcome") if in_window(o["exit_ts"])]
    net = sum(o["net_pnl"] for o in outs)
    wins = [o for o in outs if o["net_pnl"] > 0]
    stops = [o for o in outs if o.get("reason") == "stop"]

    # per-version breakdown (so an in-trial change is visible)
    by_ver: dict[str, float] = {}
    dec_ver = {d["decision_id"]: d["strategy_version"] for d in log.read("decision")}
    for o in outs:
        v = dec_ver.get(o["decision_id"], "?")
        by_ver[v] = by_ver.get(v, 0.0) + o["net_pnl"]

    lines = [
        f"# {label} report — {start.date()} to {end.date()}",
        "",
        f"Closed trades: {len(outs)}  |  net P&L: {net:.2f}  |  "
        f"win rate: {(len(wins) / len(outs)) if outs else 0:.0%}",
        f"Stop-loss exits: {len(stops)}  |  sentiment exits: {len(outs) - len(stops)}",
        f"Decisions logged: {len(decisions)}",
        "",
        "P&L by strategy version:",
        *[f"  {v}: {p:.2f}" for v, p in sorted(by_ver.items(), key=lambda kv: -kv[1])],
        "",
        "Reminder: one period is noisy. Read the trend across many of these before",
        "concluding anything; the walk-forward gate, not a good week, decides edge.",
    ]
    return "\n".join(lines)
