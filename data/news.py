"""
news.py — news ingestion behind one interface, two backends.

Alpaca's news feed (Benzinga) covers both equities and crypto over one endpoint,
with a REST history (for backtest) and a websocket stream (for live). The
interface below hides which one you're using so the rest of the system is
agnostic.

CRITICAL for backtest integrity: historical() must yield events ordered by
published_at, and the engine must only act on an event once simulated wall-clock
has advanced past published_at + assumed_latency. Sorting or filtering by
ingest time silently leaks the future.
"""
from __future__ import annotations

from typing import Protocol, Iterator, Iterable
from datetime import datetime

from core.schema import NewsEvent


class NewsSource(Protocol):
    def historical(self, tickers: Iterable[str],
                   start: datetime, end: datetime) -> Iterator[NewsEvent]: ...
    def stream(self, tickers: Iterable[str]) -> Iterator[NewsEvent]: ...


class AlpacaNews:
    """Sketch over Alpaca's Benzinga-backed news API. Wire to current alpaca-py."""
    def __init__(self, api_key: str, api_secret: str, assumed_latency_s: float = 2.0):
        self.assumed_latency_s = assumed_latency_s
        self.client = None  # <- wire me (alpaca.data NewsClient)

    def historical(self, tickers, start, end) -> Iterator[NewsEvent]:
        # for raw in self.client.get_news(...sorted ascending by published...):
        #     yield self._normalize(raw)
        raise NotImplementedError

    def stream(self, tickers) -> Iterator[NewsEvent]:
        raise NotImplementedError

    def _normalize(self, raw) -> NewsEvent:
        # Map Benzinga fields -> NewsEvent. For each symbol on a multi-ticker
        # article, emit a separate event so per-ticker aggregation stays clean.
        ...
