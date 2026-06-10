"""
broker.py — execution behind a thin interface.

The Broker Protocol is what the live runner depends on. The backtest depends on
a *different* implementation of the same shape (a simulated fill model). Neither
the strategy nor the runner knows or cares which one is plugged in — that's how
you keep execution swappable and the core logic identical across both worlds.

All SDK-specific code lives exclusively in this file so the rest of the codebase
never imports alpaca-py directly. If the SDK surface changes, only this file
needs to change.

Verified against alpaca-py source (github.com/alpacahq/alpaca-py) 2026-06.
"""
from __future__ import annotations

from typing import Protocol
from datetime import datetime

# ── alpaca-py SDK imports ──────────────────────────────────────────────────────
# TradingClient: paper=True pins the base URL to paper-api.alpaca.markets.
# StockHistoricalDataClient: data plane — same keys, no paper flag.
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, PositionSide
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

from core.schema import Action, OrderRecord, Decision, utcnow


# ── Protocol ──────────────────────────────────────────────────────────────────

class Broker(Protocol):
    def positions(self) -> dict[str, float]: ...   # ticker -> signed qty
    def equity(self) -> float: ...
    def last_price(self, ticker: str) -> float: ...
    def submit(self, decision: Decision, stop_price: float | None = None) -> OrderRecord: ...
    def is_market_open(self) -> bool: ...


# ── Alpaca paper implementation ────────────────────────────────────────────────

class AlpacaPaperBroker:
    """
    Concrete Alpaca PAPER broker — wired to the current alpaca-py SDK.

    paper=True on TradingClient hard-codes the base URL to the paper endpoint.
    There is intentionally no way to construct a live instance from this class —
    live trading requires a separate class with its own review gate.

    SDK surface verified against alpaca-py source 2026-06:
      TradingClient.get_all_positions()      -> List[Position]
      TradingClient.get_account()            -> TradeAccount  (.equity: str)
      TradingClient.get_clock()              -> Clock         (.is_open: bool)
      TradingClient.submit_order(req)        -> Order         (.id: UUID)
      TradingClient.close_position(symbol)   -> Order
      StockHistoricalDataClient
        .get_stock_latest_trade(req)         -> Dict[str, Trade]  (.price: float)
      Position: .symbol str, .qty str (always positive), .side PositionSide
      MarketOrderRequest: symbol, qty, side, time_in_force, order_class,
                          stop_loss (StopLossRequest), take_profit
      StopLossRequest: stop_price float
    """

    def __init__(self, api_key: str, api_secret: str) -> None:
        # paper=True is the only constructor this class exposes — see docstring.
        self._trading = TradingClient(api_key, api_secret, paper=True)
        # Data client shares the same keys; no paper flag (single data plane).
        self._data = StockHistoricalDataClient(api_key, api_secret)

    # ── Broker interface ───────────────────────────────────────────────────────

    def positions(self) -> dict[str, float]:
        """Return open positions as ticker -> signed qty (+long / -short)."""
        result: dict[str, float] = {}
        for p in self._trading.get_all_positions():
            signed = float(p.qty) if p.side == PositionSide.LONG else -float(p.qty)
            result[p.symbol] = signed
        return result

    def equity(self) -> float:
        """Total portfolio equity (cash + market value of positions)."""
        return float(self._trading.get_account().equity)

    def last_price(self, ticker: str) -> float:
        """Last trade price for a single equity ticker."""
        req = StockLatestTradeRequest(symbol_or_symbols=ticker)
        trades = self._data.get_stock_latest_trade(req)
        return float(trades[ticker].price)

    def is_market_open(self) -> bool:
        """True only during regular equity market hours."""
        return bool(self._trading.get_clock().is_open)

    def submit(self, decision: Decision, stop_price: float | None = None) -> OrderRecord:
        """
        Translate a Decision into an Alpaca order and submit it.

        Routing logic:
          EXIT  → close_position(symbol): flattens the position direction-agnostically
                  without needing to know the side — broker resolves it server-side.
          BUY / SELL, no stop  → plain MarketOrderRequest (order_class simple).
          BUY / SELL, stop set → MarketOrderRequest with order_class=BRACKET and
                                  a StopLossRequest so the stop is enforced broker-side
                                  even if this process is down.

        Bracket stop note: when the broker-side stop fills (as a separate child
        order) the position-reconciliation loop must detect the position has closed
        and write a synthetic EXIT decision + Outcome to the log so the record
        stays complete.
        """
        now = utcnow()

        # ── EXIT: close whatever is open, direction-agnostic ──────────────────
        if decision.action == Action.EXIT:
            order = self._trading.close_position(decision.ticker)
            return OrderRecord(
                decision_id=decision.decision_id,
                broker_order_id=str(order.id),
                submitted_at=now,
                requested_qty=float(order.qty) if order.qty else 0.0,
                side="sell",          # close_position always liquidates
                order_type="market",
                stop_price=None,
            )

        # ── ENTRY (BUY / SELL) ────────────────────────────────────────────────
        side = OrderSide.BUY if decision.target_qty > 0 else OrderSide.SELL
        qty = abs(decision.target_qty)

        if stop_price is not None:
            # Bracket: parent market order + attached broker-side stop.
            # order_class=BRACKET requires stop_loss; take_profit is optional.
            req = MarketOrderRequest(
                symbol=decision.ticker,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=stop_price),
            )
            order_type = "bracket"
        else:
            req = MarketOrderRequest(
                symbol=decision.ticker,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            order_type = "market"

        order = self._trading.submit_order(req)

        return OrderRecord(
            decision_id=decision.decision_id,
            broker_order_id=str(order.id),   # UUID -> str
            submitted_at=now,
            requested_qty=qty,
            side=side.value,                 # "buy" | "sell"
            order_type=order_type,
            stop_price=stop_price,
        )
