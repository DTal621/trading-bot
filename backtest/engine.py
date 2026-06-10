"""
engine.py — event-driven backtest that replays history WITHOUT peeking ahead.

The defining property: it calls the exact same core.signals.build_signal and
core.strategy.decide that the live runner calls. The only differences are where
data comes from (a sorted historical stream) and how fills happen (CostModel).
If the backtest and live ever disagree on a decision given identical inputs,
that's a bug in your data plumbing, not your strategy — and that's exactly the
class of bug this architecture is designed to make visible.

Anti-look-ahead rules enforced here:
  - events are consumed in published_at order;
  - a news event only becomes 'visible' to the strategy at
    published_at + assumed_latency (you never react before you'd have the data);
  - price features at decision time use only bars up to the decision timestamp.
"""
from __future__ import annotations

import uuid
from collections import defaultdict, deque
from datetime import timedelta
from typing import Iterable, Callable

from core.schema import Action, Decision, Signal, Outcome, utcnow
from core.signals import LexiconScorer, build_signal
from core.strategy import decide
from core.universe import stop_price
from backtest.fills import CostModel


class Backtester:
    def __init__(self, config: dict, cost: CostModel,
                 price_at: Callable[[str, "datetime"], float],
                 volume_z_at: Callable[[str, "datetime"], float]):
        self.config = config
        self.cost = cost
        self.price_at = price_at        # point-in-time price lookup (no future bars!)
        self.volume_z_at = volume_z_at
        self.scorer = LexiconScorer()
        self.equity = config["backtest"]["starting_equity"]
        self.positions: dict[str, float] = defaultdict(float)
        self.entry: dict[str, tuple] = {}   # ticker -> (entry_price, entry_ts, qty)
        self.stops: dict[str, float] = {}   # ticker -> stop trigger price
        self.decisions: list[Decision] = []
        self.outcomes: list[Outcome] = []

    def run(self, events: Iterable["NewsEvent"]):
        p = self.config["params"]
        window = timedelta(seconds=p["news_window_seconds"])
        half_life = p["news_half_life_seconds"]
        latency = timedelta(seconds=self.config["backtest"]["assumed_latency_seconds"])
        recent: dict[str, deque] = defaultdict(deque)

        for event in events:                       # MUST be sorted by published_at
            decision_ts = event.published_at + latency   # when we could actually act
            recent[event.ticker].append(event)
            cutoff = decision_ts - window
            while recent[event.ticker] and recent[event.ticker][0].published_at < cutoff:
                recent[event.ticker].popleft()

            price = self.price_at(event.ticker, decision_ts)

            # Honor the broker-side stop here so backtest and live agree on when a
            # stop fires. LIMITATION: this only checks at this ticker's event
            # timestamps, not every bar — tick/bar-accurate stops need bar-level
            # stepping (pending the bar plumbing). Documented, not silent.
            if self._stop_breached(event.ticker, price):
                self._close(event.ticker, self.stops[event.ticker], decision_ts,
                            str(uuid.uuid4()), reason="stop")
                continue

            feats = {"last_price": price,
                     "volume_zscore": self.volume_z_at(event.ticker, decision_ts)}
            signal = build_signal(event.ticker, decision_ts,
                                  list(recent[event.ticker]), self.scorer, feats, half_life)

            decision = decide(signal, self.positions[event.ticker], self.equity,
                              self.config, decision_ts, str(uuid.uuid4()))
            self.decisions.append(decision)
            self._apply(decision, price, decision_ts)

        return self._report()

    def _apply(self, d: Decision, price: float, ts):
        if d.action == Action.HOLD or price <= 0:
            return
        if d.action == Action.EXIT and self.positions[d.ticker] != 0:
            self._close(d.ticker, price, ts, d.decision_id, reason="signal")
            return
        if d.action in (Action.BUY, Action.SELL) and self.positions[d.ticker] == 0:
            side = "buy" if d.target_qty > 0 else "sell"
            fill = self.cost.fill_price(price, side)
            self.equity -= self.cost.fees(abs(d.target_qty) * fill)
            self.positions[d.ticker] = d.target_qty
            self.entry[d.ticker] = (fill, ts, d.target_qty)
            sp = stop_price(d.ticker, fill, d.target_qty, self.config)
            if sp is not None:
                self.stops[d.ticker] = sp

    def _stop_breached(self, ticker: str, price: float) -> bool:
        sp = self.stops.get(ticker)
        qty = self.positions.get(ticker, 0.0)
        if sp is None or qty == 0 or price <= 0:
            return False
        return price <= sp if qty > 0 else price >= sp

    def _close(self, ticker: str, ref_price: float, ts, decision_id: str, reason: str = ""):
        entry_price, entry_ts, qty = self.entry[ticker]
        side = "sell" if qty > 0 else "buy"
        exit_fill = self.cost.fill_price(ref_price, side)
        gross = (exit_fill - entry_price) * qty
        fees = self.cost.fees(abs(qty) * exit_fill)
        self.equity += gross - fees
        self.outcomes.append(Outcome(
            decision_id=decision_id, entry_price=entry_price, exit_price=exit_fill,
            entry_ts=entry_ts, exit_ts=ts, qty=qty,
            gross_pnl=gross, fees=fees, net_pnl=gross - fees, reason=reason))
        self.positions[ticker] = 0.0
        self.entry.pop(ticker, None)
        self.stops.pop(ticker, None)

    def _report(self) -> dict:
        n = len(self.outcomes)
        wins = [o for o in self.outcomes if o.net_pnl > 0]
        net = sum(o.net_pnl for o in self.outcomes)
        return {
            "trades": n,
            "win_rate": (len(wins) / n) if n else 0.0,
            "net_pnl": net,
            "final_equity": self.equity,
            "avg_win": (sum(o.net_pnl for o in wins) / len(wins)) if wins else 0.0,
            "decisions": len(self.decisions),
        }
