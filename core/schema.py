"""
schema.py — the backbone of the whole system.

Every decision the bot makes is recorded as a structured record with enough
context to (a) replay it in backtest, (b) explain it in the daily report, and
(c) evaluate it honestly offline. If it isn't in the log, it didn't happen.

Two timestamps appear everywhere on purpose:
  - published_at: when the world learned the news (point-in-time truth)
  - ingested_at:  when *we* learned it (latency / look-ahead reality)
Backtests must only ever use published_at to decide eligibility, but should
simulate the delay between the two. Confusing these is the #1 source of
look-ahead bias in news strategies.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
import json
import hashlib


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    EXIT = "EXIT"


@dataclass(frozen=True)
class NewsEvent:
    """A single piece of incoming news, normalized across sources."""
    event_id: str
    ticker: str
    headline: str
    source: str
    published_at: datetime
    ingested_at: datetime
    summary: str = ""

    @property
    def latency_seconds(self) -> float:
        return (self.ingested_at - self.published_at).total_seconds()


@dataclass(frozen=True)
class Signal:
    """The output of the scorer for one ticker at one moment in time."""
    ts: datetime
    ticker: str
    score: float                 # normalized sentiment, e.g. [-1, 1]
    confidence: float            # [0, 1]
    features: dict = field(default_factory=dict)  # everything that fed the score
    # keep the raw drivers so the daily report can explain *why*
    contributing_event_ids: tuple = ()


@dataclass(frozen=True)
class Decision:
    """
    A trade decision. This is the unit of record. Note strategy_version and
    config_hash: every decision is stamped with exactly which frozen spec
    produced it, so you can later filter "show me everything spec v3 did" and
    evaluate a version on its own out-of-sample track record.
    """
    decision_id: str
    ts: datetime
    ticker: str
    action: Action
    target_qty: float
    reason: str
    signal: Signal
    strategy_version: str
    config_hash: str


@dataclass(frozen=True)
class OrderRecord:
    decision_id: str
    broker_order_id: Optional[str]
    submitted_at: datetime
    requested_qty: float
    side: str
    order_type: str
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None   # attached broker-side stop (bracket), if any


@dataclass(frozen=True)
class Fill:
    broker_order_id: str
    filled_at: datetime
    filled_qty: float
    fill_price: float
    fees: float


@dataclass(frozen=True)
class Outcome:
    """Computed OFFLINE after the fact — never available at decision time."""
    decision_id: str
    entry_price: float
    exit_price: float
    entry_ts: datetime
    exit_ts: datetime
    qty: float
    gross_pnl: float
    fees: float
    net_pnl: float
    reason: str = ""              # "signal" | "stop" — why the position closed

    @property
    def holding_seconds(self) -> float:
        return (self.exit_ts - self.entry_ts).total_seconds()


# --- serialization helpers ----------------------------------------------------

def _default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    raise TypeError(f"not serializable: {type(o)}")


def to_jsonl(record) -> str:
    return json.dumps(asdict(record), default=_default, sort_keys=True)


def config_hash(config: dict) -> str:
    """Deterministic fingerprint of a strategy spec, stamped onto every decision."""
    blob = json.dumps(config, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]
