"""
trial.py — the 2-week confirmation trial, run as a shadow A/B.

When a change is approved and deployed, the NEW version trades live and the
PREVIOUS version runs in SHADOW: it makes decisions and logs them tagged with
its version, but submits no orders. Both see the same market over the same
window, so the comparison is a real A/B rather than "this fortnight vs some past
fortnight" — which would just be comparing two different market regimes.

The trial answers "does live behave like the backtest promised, and does it beat
the version it replaced, on the same tape?" — NOT "is this strategy good in the
abstract". Two weeks is too short for the latter; the walk-forward gate already
did that job.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from core.logstore import DecisionLog


@dataclass
class TrialWindow:
    candidate_version: str
    incumbent_version: str
    start: datetime
    end: datetime

    def is_complete(self, now: datetime) -> bool:
        return now >= self.end


def start_trial(candidate_version: str, incumbent_version: str,
                now: datetime, trial_days: int) -> TrialWindow:
    return TrialWindow(candidate_version, incumbent_version,
                       start=now, end=now + timedelta(days=trial_days))


def _stats_for_version(log: DecisionLog, version: str, window: TrialWindow) -> dict:
    """Aggregate realized outcomes for one version inside the trial window.
    Decisions are stamped with strategy_version; outcomes link by decision_id."""
    dec_versions = {d["decision_id"]: d["strategy_version"]
                    for d in log.read("decision")}
    outs = []
    for o in log.read("outcome"):
        if dec_versions.get(o["decision_id"]) != version:
            continue
        ts = datetime.fromisoformat(o["exit_ts"])
        if window.start <= ts <= window.end:
            outs.append(o)
    net = sum(o["net_pnl"] for o in outs)
    wins = [o for o in outs if o["net_pnl"] > 0]
    return {"version": version, "trades": len(outs), "net_pnl": net,
            "win_rate": (len(wins) / len(outs)) if outs else None}


def build_trial_report(log: DecisionLog, window: TrialWindow, now: datetime) -> str:
    cand = _stats_for_version(log, window.candidate_version, window)
    inc = _stats_for_version(log, window.incumbent_version, window)
    done = window.is_complete(now)
    delta = (cand["net_pnl"] - inc["net_pnl"])
    lines = [
        f"# Trial report — candidate {window.candidate_version}",
        f"Window: {window.start.date()} → {window.end.date()} "
        f"({'COMPLETE' if done else 'IN PROGRESS'})",
        "",
        f"  Live (candidate, traded):  {cand['trades']} trades, net {cand['net_pnl']:.2f}, "
        f"win {cand['win_rate']}",
        f"  Shadow (incumbent, logged-only): {inc['trades']} trades, net {inc['net_pnl']:.2f}, "
        f"win {inc['win_rate']}",
        f"  A/B delta (candidate - incumbent): {delta:.2f}",
        "",
        "Note: a 2-week A/B is monitoring, not proof. Watch for live-vs-backtest",
        "divergence (slippage, fills, breakages), not just the P&L sign.",
        "",
        "Decision: reply 'keep' to ADOPT (candidate stays live) or 'cancel' to",
        "REVERT (roll the live pointer back to the incumbent version).",
    ]
    return "\n".join(lines)
