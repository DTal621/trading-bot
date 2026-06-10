"""
strategy.py — the frozen decision core.

Signal (+ current position + config) -> Decision. Pure function, no I/O, no
clock-reading, no network. That purity is what lets the SAME code run live and
in backtest and produce identical decisions given identical inputs.

It reads a config dict (loaded from the versioned strategy.yaml) but it does NOT
modify it. The live bot never tunes itself. Changes to thresholds happen by
editing the spec, bumping the version, and passing the walk-forward gate — never
at runtime from recent P&L.
"""
from __future__ import annotations

from datetime import datetime

from core.schema import Action, Decision, Signal, config_hash


def decide(
    signal: Signal,
    current_qty: float,
    equity: float,
    config: dict,
    now: datetime,
    decision_id: str,
) -> Decision:
    p = config["params"]
    version = config["version"]
    chash = config_hash(config)

    entry_thresh = p["entry_score_threshold"]
    min_conf = p["min_confidence"]
    vol_z_gate = p["volume_zscore_gate"]
    max_pos_value = equity * p["max_position_pct"]

    vol_z = signal.features.get("volume_zscore", 0.0)
    last_price = signal.features.get("last_price", 0.0)

    def mk(action: Action, qty: float, reason: str) -> Decision:
        return Decision(
            decision_id=decision_id, ts=now, ticker=signal.ticker,
            action=action, target_qty=qty, reason=reason,
            signal=signal, strategy_version=version, config_hash=chash,
        )

    # --- exit logic first: holding an existing position changes everything ----
    if current_qty != 0:
        # Flip / fade: sentiment reversed against our position -> get flat.
        aligned = (current_qty > 0 and signal.score > 0) or \
                  (current_qty < 0 and signal.score < 0)
        if not aligned and abs(signal.score) >= entry_thresh:
            return mk(Action.EXIT, 0.0, "sentiment reversed against open position")
        return mk(Action.HOLD, current_qty, "holding; no exit trigger")

    # --- entry logic: require BOTH sentiment strength AND confirmation --------
    if signal.confidence < min_conf:
        return mk(Action.HOLD, 0.0, f"confidence {signal.confidence:.2f} < {min_conf}")
    if abs(signal.score) < entry_thresh:
        return mk(Action.HOLD, 0.0, f"|score| {abs(signal.score):.2f} < {entry_thresh}")
    if vol_z < vol_z_gate:
        return mk(Action.HOLD, 0.0, f"no volume confirmation (z={vol_z:.2f})")
    if last_price <= 0:
        return mk(Action.HOLD, 0.0, "no valid price")

    qty = (max_pos_value / last_price)
    if signal.score < 0:
        qty = -qty  # short (equities/options only; spot crypto can't short on Alpaca)
    side = "long" if qty > 0 else "short"
    return mk(Action.BUY if qty > 0 else Action.SELL, qty,
              f"{side} entry: score={signal.score:.2f} conf={signal.confidence:.2f} z={vol_z:.2f}")
