"""
reconcile.py — broker-side stop fill reconciliation.

The problem: when a bracket stop child fills on Alpaca, the position closes
entirely at the broker without passing through our decision logic. The log
would be missing that exit — the decision log would show an open entry with
no corresponding EXIT, and the Outcome would never be written.

Detection strategy: compare in-memory open entries against broker.positions().
If a ticker is in _entries but has no live position, the broker closed it —
almost certainly the bracket stop child that was submitted alongside the entry.

Why not the order-update WebSocket? Polling is simpler, more resilient to
reconnect edge cases, and 60-second latency on a stop-exit log entry is
acceptable — we are not using reconciliation to make trading decisions, only
to keep the audit log complete.

Idempotency guarantee:
  - clear_entry() is called when the runner submits a signal-driven EXIT, so
    that ticker is removed from _entries before maybe_reconcile() runs.
  - Once a stop exit is written and clear_entry() is called, the ticker is
    gone from _entries and can never be double-logged in the same process.
  - On restart, _seed_from_log() replays decisions in chronological order and
    skips any ticker whose last decision is already an EXIT, so a stop exit
    that was logged before a crash is never written again.

The synthetic records written to the log:
  kind="decision"  action=EXIT  reason="stop (broker-side bracket fill)"
  kind="outcome"   reason="stop"
Both carry the same decision_id (a fresh UUID) so the daily report and
offline evaluator can join them.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus, OrderType, OrderStatus

from core.schema import (
    Action, Decision, Signal, Outcome,
    config_hash as make_config_hash, utcnow,
)
from core.logstore import DecisionLog
from core.broker import Broker


# Stop-type orders — any of these filling indicates a bracket stop fired.
_STOP_TYPES = frozenset({
    OrderType.STOP,
    OrderType.STOP_LIMIT,
    OrderType.TRAILING_STOP,
})


@dataclass
class _EntryRecord:
    """Everything needed to reconstruct an Outcome once a stop exit is detected."""
    decision_id: str
    entry_price: float   # signal.features["last_price"] at decision time
    entry_ts: datetime
    qty: float           # signed: positive = long, negative = short


class StopReconciler:
    """
    Detects broker-side stop fills and writes synthetic EXIT + Outcome records.

    Lifecycle:
      1. Constructed once in run() with env credentials.
      2. register_entry() called after every approved BUY/SELL submission.
      3. clear_entry() called when runner submits a signal-driven EXIT
         (before maybe_reconcile() on that same iteration).
      4. maybe_reconcile() called at the end of every news event loop; it
         is a no-op unless RECONCILE_INTERVAL seconds have elapsed.
    """

    RECONCILE_INTERVAL = timedelta(seconds=60)

    def __init__(self, api_key: str, api_secret: str,
                 broker: Broker, log: DecisionLog) -> None:
        # Read-only TradingClient for get_orders; paper=True is fixed —
        # this class must never point at a live endpoint.
        self._client = TradingClient(api_key, api_secret, paper=True)
        self._broker = broker
        self._entries: dict[str, _EntryRecord] = {}
        # First reconcile runs immediately (last_poll set one interval in the past).
        self._last_poll: datetime = utcnow() - self.RECONCILE_INTERVAL
        self._seed_from_log(log)

    # ── Runner-facing API ──────────────────────────────────────────────────────

    def register_entry(self, ticker: str, decision_id: str,
                       entry_price: float, entry_ts: datetime,
                       qty: float) -> None:
        """Register a new open position for reconciliation monitoring."""
        self._entries[ticker] = _EntryRecord(decision_id, entry_price, entry_ts, qty)

    def clear_entry(self, ticker: str) -> None:
        """
        Remove a ticker from watch — call this when the runner submits a
        signal-driven EXIT so the reconciler does not also write a stop exit
        for the same position closure.  Must be called before maybe_reconcile().
        """
        self._entries.pop(ticker, None)

    def maybe_reconcile(self, config: dict, log: DecisionLog) -> None:
        """
        Call on every news event iteration.  Polls Alpaca and writes synthetic
        records at most once per RECONCILE_INTERVAL, so the overhead per event
        is a single datetime comparison.
        """
        if utcnow() - self._last_poll < self.RECONCILE_INTERVAL:
            return
        self._last_poll = utcnow()
        self._reconcile(config, log)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _seed_from_log(self, log: DecisionLog) -> None:
        """
        Rebuild _entries from the persisted log so the reconciler survives
        restarts.

        Replays decisions in append order (= chronological).  Each BUY/SELL
        opens an entry; each EXIT closes it.  What remains after the full
        replay is the set of positions that were open when the process last
        stopped — exactly what the reconciler should watch.
        """
        raw: dict[str, dict] = {}

        for rec in log.read("decision"):
            ticker = rec.get("ticker", "")
            action = rec.get("action", "")
            if action in ("BUY", "SELL"):
                raw[ticker] = rec
            elif action == "EXIT":
                raw.pop(ticker, None)

        for ticker, rec in raw.items():
            price = float(
                rec.get("signal", {}).get("features", {}).get("last_price", 0.0) or 0.0
            )
            try:
                entry_ts = datetime.fromisoformat(rec["ts"])
            except (KeyError, ValueError, TypeError):
                entry_ts = utcnow()
            qty = float(rec.get("target_qty", 0.0))

            if price > 0 and qty != 0:
                self._entries[ticker] = _EntryRecord(
                    decision_id=rec.get("decision_id", str(uuid.uuid4())),
                    entry_price=price,
                    entry_ts=entry_ts,
                    qty=qty,
                )

        if self._entries:
            print(f"[reconcile] seeded {len(self._entries)} open "
                  f"entries from log: {list(self._entries)}")

    def _reconcile(self, config: dict, log: DecisionLog) -> None:
        """
        For each watched ticker no longer held at the broker, write a synthetic
        EXIT + Outcome to the log and stop watching the ticker.
        """
        if not self._entries:
            return

        try:
            live = self._broker.positions()   # ticker -> signed qty
        except Exception as e:
            print(f"[reconcile] positions poll failed: {e}")
            return

        now = utcnow()
        for ticker, entry in list(self._entries.items()):
            if abs(live.get(ticker, 0.0)) > 1e-9:
                continue   # position still open

            # Position is gone without a signal-driven EXIT.
            exit_price, exit_ts = self._find_stop_fill(ticker, entry.entry_ts, now)
            if exit_price <= 0:
                print(f"[reconcile] {ticker}: position closed but fill price "
                      f"unavailable — will retry next interval")
                continue   # leave in _entries; try again next poll

            self._write_stop_exit(ticker, entry, exit_price, exit_ts, config, log)
            self.clear_entry(ticker)

    def _find_stop_fill(self, ticker: str, since: datetime,
                        now: datetime) -> tuple[float, datetime]:
        """
        Query Alpaca for the stop order that closed this position.

        Uses nested=True so bracket legs are embedded under their parent; also
        checks top-level stop orders in case the stop appears un-nested.

        Falls back to last_price if no stop order is found — this keeps
        the log complete (with a price approximation) rather than silently
        dropping the exit.
        """
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=since,
                symbols=[ticker],
                nested=True,
                limit=50,
            )
            orders = self._client.get_orders(req)
        except Exception as e:
            print(f"[reconcile] get_orders failed for {ticker}: {e}")
            return self._fallback_price(ticker, now)

        for order in orders:
            # Stop legs embedded inside a bracket parent
            for leg in (order.legs or []):
                if (leg.type in _STOP_TYPES
                        and leg.status == OrderStatus.FILLED
                        and leg.filled_avg_price):
                    price = float(leg.filled_avg_price)
                    ts = leg.filled_at or now
                    return price, ts

            # Top-level stop orders (appear when stop fires as standalone)
            if (order.type in _STOP_TYPES
                    and order.status == OrderStatus.FILLED
                    and order.filled_avg_price):
                price = float(order.filled_avg_price)
                ts = order.filled_at or now
                return price, ts

        print(f"[reconcile] {ticker}: no filled stop order found after "
              f"{since.isoformat()}; using last_price as exit proxy")
        return self._fallback_price(ticker, now)

    def _fallback_price(self, ticker: str, now: datetime) -> tuple[float, datetime]:
        try:
            return self._broker.last_price(ticker), now
        except Exception:
            return 0.0, now

    def _write_stop_exit(self, ticker: str, entry: _EntryRecord,
                         exit_price: float, exit_ts: datetime,
                         config: dict, log: DecisionLog) -> None:
        """
        Append synthetic Decision (EXIT) + Outcome to the log.

        decision_id is a fresh UUID — unique per stop exit, shared by both
        records so the evaluator can join Decision → Outcome by decision_id.

        The Signal is a placeholder (score=0, confidence=0) tagged
        synthetic_stop_exit=True so reports can filter it out of signal
        attribution while still counting it in P&L.
        """
        decision_id = str(uuid.uuid4())

        null_signal = Signal(
            ts=exit_ts,
            ticker=ticker,
            score=0.0,
            confidence=0.0,
            features={"synthetic_stop_exit": True},
            contributing_event_ids=(),
        )
        decision = Decision(
            decision_id=decision_id,
            ts=exit_ts,
            ticker=ticker,
            action=Action.EXIT,
            target_qty=0.0,
            reason="stop (broker-side bracket fill)",
            signal=null_signal,
            strategy_version=config.get("version", "unknown"),
            config_hash=make_config_hash(config),
        )

        gross = (exit_price - entry.entry_price) * entry.qty
        # Alpaca charges no commission on equity stop fills; crypto taker fee
        # applies but is small and not easily recoverable here without the
        # exact fill notional from the order — log 0 and note the approximation.
        outcome = Outcome(
            decision_id=decision_id,
            entry_price=entry.entry_price,
            exit_price=exit_price,
            entry_ts=entry.entry_ts,
            exit_ts=exit_ts,
            qty=entry.qty,
            gross_pnl=gross,
            fees=0.0,
            net_pnl=gross,
            reason="stop",
        )

        log.append("decision", decision)
        log.append("outcome", outcome)
        print(f"[reconcile] stop exit logged: {ticker}  "
              f"entry={entry.entry_price:.4f}  exit={exit_price:.4f}  "
              f"pnl={gross:+.2f}  qty={entry.qty:+.4f}")
