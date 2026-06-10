"""
bar_features.py — pure bar-based feature computations shared by live and backtest.

Nothing in here does I/O, reads a clock, or knows how bars were obtained.
Both the live BarCache and the backtest BarStore call volume_zscore() with
their own bar lists — this guarantees identical numbers given identical bars,
which is the only acceptable definition of "live/backtest parity."
"""
from __future__ import annotations

import statistics
from typing import Any

# Treat population std below this as zero. Guards against:
#   - identical volumes (std == 0.0 exactly)
#   - near-zero float precision noise (e.g. all volumes ~1e-12)
# Both cases produce a meaningless z-score; returning 0.0 lets the
# strategy's volume_zscore_gate treat it as "no confirmation" — the
# correct conservative default.
_STD_FLOOR = 1e-9


def volume_zscore(bars: list[Any], n: int) -> float:
    """
    Compute (current_bar_volume - window_mean) / window_std.

    bars       — list of bar objects sorted ascending by timestamp.
                 Must have a .volume float attribute.
    n          — number of trailing bars that form the reference window.

    Indexing:
      current bar     = bars[-1]            most recent completed bar
      trailing window = bars[-(n+1) : -1]   the n bars before it

    Returns 0.0 when:
      - fewer than n+1 bars are available  (insufficient history)
      - population std of the window is < _STD_FLOOR  (flat / no volume)
    """
    if len(bars) < n + 1:
        return 0.0

    current_vol: float = bars[-1].volume
    window_vols: list[float] = [b.volume for b in bars[-(n + 1):-1]]

    mean = statistics.mean(window_vols)
    std = statistics.pstdev(window_vols)   # population std; window is our full reference

    if std < _STD_FLOOR:
        return 0.0

    return (current_vol - mean) / std
