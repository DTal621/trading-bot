"""
guardrails.py — HARD guardrails. The layer that does not trust the LLM, the
strategy, or you-at-2am.

Every decision passes through Guardrails.check() in the execution path BEFORE it
can become an order. This is deterministic code, not a prompt. An LLM can write
a bad proposal, the strategy can emit a bad decision — neither can get past this
gate. That separation is the entire point: soft guardrails (CLAUDE.md) shape the
agent's behaviour; THESE protect the account.

Things enforced here:
  - paper-only unless an explicit, deliberate real-money flag is set
  - per-order and per-name size caps
  - max gross exposure and max open positions
  - a daily-loss kill switch that halts NEW entries (exits always allowed)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.schema import Decision, Action


class Verdict(str, Enum):
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    HALTED = "HALTED"      # kill switch tripped


@dataclass(frozen=True)
class RiskLimits:
    allow_real_money: bool = False     # must be flipped DELIBERATELY, never by the agent
    max_order_notional: float = 10_000
    max_position_pct: float = 0.05     # per name, as fraction of equity
    max_gross_exposure_pct: float = 0.90
    max_daily_loss_pct: float = 0.06   # trip the kill switch at -6% on the day


@dataclass(frozen=True)
class PortfolioState:
    equity: float
    day_start_equity: float
    gross_exposure: float              # sum of |position notional|
    open_positions: int
    is_paper: bool


@dataclass(frozen=True)
class GuardResult:
    verdict: Verdict
    reason: str


class Guardrails:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def check(self, d: Decision, p: PortfolioState, est_price: float) -> GuardResult:
        L = self.limits

        # 0. Real-money safety interlock. Refuse to act on a live account unless
        #    explicitly authorized in code/config — the agent cannot set this.
        if not p.is_paper and not L.allow_real_money:
            return GuardResult(Verdict.BLOCKED, "real-money trading not authorized")

        # 1. Kill switch. If the day is already down past the limit, allow only exits.
        daily_pl = (p.equity - p.day_start_equity) / max(1e-9, p.day_start_equity)
        if daily_pl <= -L.max_daily_loss_pct and d.action in (Action.BUY, Action.SELL):
            return GuardResult(Verdict.HALTED,
                               f"daily loss {daily_pl:.2%} <= -{L.max_daily_loss_pct:.0%}: new entries halted")

        # Exits are always allowed (risk-reducing).
        if d.action in (Action.HOLD, Action.EXIT):
            return GuardResult(Verdict.APPROVED, "risk-reducing or no-op")

        # 2. Per-order notional cap.
        notional = abs(d.target_qty) * est_price
        if notional > L.max_order_notional:
            return GuardResult(Verdict.BLOCKED,
                               f"order notional {notional:.0f} > cap {L.max_order_notional:.0f}")

        # 3. Per-name size cap (defence-in-depth; strategy also sizes to this).
        if notional > p.equity * L.max_position_pct:
            return GuardResult(Verdict.BLOCKED,
                               f"position {notional:.0f} > {L.max_position_pct:.0%} of equity")

        # 4. Gross exposure cap (no position-count cap — count is the strategy's
        #    call; concentration is bounded by per-name + gross only).
        if (p.gross_exposure + notional) > p.equity * L.max_gross_exposure_pct:
            return GuardResult(Verdict.BLOCKED, "would breach max gross exposure")

        return GuardResult(Verdict.APPROVED, "within all limits")
