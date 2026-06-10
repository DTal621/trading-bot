"""
broker.py — execution behind a thin interface.

The Broker Protocol is what the live runner depends on. The backtest depends on
a *different* implementation of the same shape (a simulated fill model). Neither
the strategy nor the runner knows or cares which one is plugged in — that's how
you keep execution swappable and the core logic identical across both worlds.

NOTE: the Alpaca SDK surface changes over time. Treat AlpacaPaperBroker as a
wiring sketch and verify method names/params against the current alpaca-py docs
before relying on it. Keep all SDK-specific code inside this file.
"""
from __future__ import annotations

from typing import Protocol, Optional
from datetime import datetime

from core.schema import OrderRecord, Decision, utcnow


class Broker(Protocol):
    def positions(self) -> dict[str, float]: ...          # ticker -> signed qty
    def equity(self) -> float: ...
    def last_price(self, ticker: str) -> float: ...
    def submit(self, decision: Decision, stop_price: float | None = None) -> OrderRecord: ...
    def is_market_open(self) -> bool: ...


class AlpacaPaperBroker:
    """
    Wiring sketch for Alpaca's PAPER endpoint. Fill in against current alpaca-py.
    Keep this pointed at the paper base URL until you've run for a long time.
    """
    def __init__(self, api_key: str, api_secret: str):
        # from alpaca.trading.client import TradingClient
        # self.client = TradingClient(api_key, api_secret, paper=True)
        self.client = None  # <- wire me
        raise NotImplementedError("wire AlpacaPaperBroker to current alpaca-py SDK")

    def positions(self) -> dict[str, float]:
        # return {p.symbol: float(p.qty) * (1 if p.side == 'long' else -1)
        #         for p in self.client.get_all_positions()}
        ...

    def equity(self) -> float:
        # return float(self.client.get_account().equity)
        ...

    def last_price(self, ticker: str) -> float:
        ...

    def is_market_open(self) -> bool:
        # return self.client.get_clock().is_open
        ...

    def submit(self, decision: Decision, stop_price: float | None = None) -> OrderRecord:
        # Translate Decision -> Alpaca order request, submit, capture broker id.
        # If stop_price is given on an ENTRY, submit a BRACKET order so the stop is
        # enforced broker-side (it still fires if this process is down). When a
        # broker-side stop fills, your position-reconciliation poll must log it as
        # a stop exit (synthetic EXIT decision + outcome) so the log stays complete.
        side = "buy" if decision.target_qty > 0 else "sell"
        # order_class="bracket", stop_loss={"stop_price": stop_price} when stop_price set
        return OrderRecord(
            decision_id=decision.decision_id,
            broker_order_id=None,       # fill from submitted order
            submitted_at=utcnow(),
            requested_qty=abs(decision.target_qty),
            side=side,
            order_type="bracket" if stop_price else "market",
            stop_price=stop_price,
        )
