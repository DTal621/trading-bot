"""
fills.py — realistic fill simulation. Where paper dreams meet real costs.

A sentiment strategy that looks great on mid-price fills and zero fees is almost
always an illusion. Model, at minimum:
  - the bid/ask spread you'd actually cross,
  - fees (Alpaca crypto is maker/taker 0.15%/0.25%; US equities/options $0 commission
    but you still pay the spread + any regulatory fees),
  - latency: you don't trade at the price *at* the headline, you trade a few
    seconds later, often after the move has started.
Set these PESSIMISTICALLY. If the edge survives pessimistic costs, it might be real.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    spread_bps: float = 5.0          # half-spread you cross, in basis points
    taker_fee_rate: float = 0.0025   # 0.25% — Alpaca crypto taker; 0 for US equities
    slippage_bps: float = 3.0        # extra adverse move during your latency window

    def fill_price(self, mid: float, side: str) -> float:
        adverse = (self.spread_bps + self.slippage_bps) / 10_000.0
        return mid * (1 + adverse) if side == "buy" else mid * (1 - adverse)

    def fees(self, notional: float) -> float:
        return abs(notional) * self.taker_fee_rate
