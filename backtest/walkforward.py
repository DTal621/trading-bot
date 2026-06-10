"""
walkforward.py — the promotion gate. This is what turns "the bot is learning"
from a self-deception into a disciplined process.

The rule: you may optimize parameters on an IN-SAMPLE window, but a change only
earns promotion if it ALSO improves on the immediately-following OUT-OF-SAMPLE
window that the optimizer never saw. Roll the windows forward and require the
edge to hold up repeatedly. Anything that only shines in-sample is overfitting,
full stop.

Workflow for a proposed change (e.g. a new entry threshold, a new scorer):
  1. candidate config gets a NEW version string.
  2. run_walk_forward() evaluates it across rolling splits.
  3. promote() only returns True if out-of-sample performance clears your bar
     on a MAJORITY of folds AND beats the incumbent out-of-sample.
Only then does the new spec become the live spec. The live bot is never the
place where tuning happens.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

# A backtest_fn runs Backtester over [start, end) with a given config and
# returns the report dict. Inject it so this module stays pure/testable.
BacktestFn = Callable[[dict, datetime, datetime], dict]


@dataclass
class Fold:
    train_start: datetime
    train_end: datetime    # == test_start
    test_end: datetime


def make_folds(start: datetime, end: datetime,
               train_days: int, test_days: int) -> list[Fold]:
    folds, cur = [], start
    train, test = timedelta(days=train_days), timedelta(days=test_days)
    while cur + train + test <= end:
        folds.append(Fold(cur, cur + train, cur + train + test))
        cur += test          # roll forward by the test window (anchored walk-forward)
    return folds


def run_walk_forward(config: dict, start: datetime, end: datetime,
                     backtest_fn: BacktestFn,
                     train_days: int = 90, test_days: int = 30) -> list[dict]:
    """Returns the OUT-OF-SAMPLE report for each fold. In-sample is for tuning only."""
    results = []
    for fold in make_folds(start, end, train_days, test_days):
        # (If auto-tuning: fit params on [train_start, train_end) here, then:)
        oos = backtest_fn(config, fold.train_end, fold.test_end)
        oos["fold_test_start"] = fold.train_end.isoformat()
        results.append(oos)
    return results


def promote(candidate_oos: list[dict], incumbent_oos: list[dict],
            min_trades_per_fold: int = 20,
            min_win_rate: float = 0.50) -> tuple[bool, str]:
    """
    Conservative gate. Tighten these bars over time; never loosen them to make a
    favourite idea pass. A change that can't clear an honest out-of-sample bar is
    a change that will cost you money live.
    """
    usable = [f for f in candidate_oos if f["trades"] >= min_trades_per_fold]
    if len(usable) < max(1, len(candidate_oos) // 2):
        return False, "too few trades out-of-sample to judge — insufficient evidence"

    pass_folds = sum(1 for f in usable if f["win_rate"] >= min_win_rate and f["net_pnl"] > 0)
    if pass_folds <= len(usable) / 2:
        return False, f"only {pass_folds}/{len(usable)} OOS folds cleared the bar"

    cand_net = sum(f["net_pnl"] for f in candidate_oos)
    inc_net = sum(f["net_pnl"] for f in incumbent_oos) if incumbent_oos else float("-inf")
    if cand_net <= inc_net:
        return False, "does not beat incumbent out-of-sample"

    return True, f"promoted: {pass_folds}/{len(usable)} folds, OOS net {cand_net:.2f}"
