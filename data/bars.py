"""
bars.py — point-in-time bar store for backtesting.

Loads Alpaca 1-minute bars for a set of tickers over a fixed window once at
construction, then serves fast O(log n) point-in-time lookups that never
return a bar dated after the query timestamp.

The two public callables — price_at and volume_z_at — match the signatures
Backtester expects:

    price_at(ticker: str, ts: datetime) -> float
    volume_z_at(ticker: str, ts: datetime) -> float

Both are methods of BarStore and can be passed directly:

    store = BarStore.load(api_key, secret, config, start, end)
    bt = Backtester(config, cost, store.price_at, store.volume_z_at)

Anti-look-ahead guarantee: bisect_right on the timestamp index returns only
bars with timestamp <= ts. The Alpaca API only returns *completed* bars — the
bar whose interval contains ts is not included until the interval closes — so
the response itself never leaks the future. The bisect guard is a second line
of defence that also handles clock-skew and any edge cases in the API response.

SDK surface verified 2026-06:
  StockHistoricalDataClient.get_stock_bars(StockBarsRequest) -> BarSet
  CryptoHistoricalDataClient.get_crypto_bars(CryptoBarsRequest) -> BarSet
  BarSet.data[symbol] -> List[Bar]  (ascending by timestamp)
  Bar: .close float, .volume float, .timestamp datetime (tz-aware UTC)
  Multi-symbol requests confirmed working for historical windows.
"""
from __future__ import annotations

import bisect
from datetime import datetime
from typing import Callable

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

from core.bar_features import volume_zscore


class BarStore:
    """
    Immutable point-in-time bar store for one backtest window.

    Internal structure per ticker:
      _bars[ticker]       — List[Bar] sorted ascending by timestamp
      _timestamps[ticker] — List[datetime] parallel to _bars, for bisect

    _bars_up_to(ticker, ts) slices [: bisect_right(ts)] and returns the last
    n+1 bars from that prefix — enough for price_at (needs 1) and
    volume_z_at (needs n+1).
    """

    def __init__(self, bars: dict[str, list], n: int) -> None:
        """
        bars — {ticker: sorted List[Bar]}  (populated by BarStore.load)
        n    — trailing window length for volume z-score (from config)
        """
        self._bars = bars
        self._n = n
        # Pre-build timestamp lists for O(log n) bisect lookups.
        self._timestamps: dict[str, list[datetime]] = {
            ticker: [b.timestamp for b in ticker_bars]
            for ticker, ticker_bars in bars.items()
        }

    # ── Public callables ───────────────────────────────────────────────────────

    def price_at(self, ticker: str, ts: datetime) -> float:
        """
        Close price of the most recent completed bar at or before ts.
        Returns 0.0 if no bar exists for ticker up to ts.
        """
        bars = self._bars_up_to(ticker, ts, need=1)
        return float(bars[-1].close) if bars else 0.0

    def volume_z_at(self, ticker: str, ts: datetime) -> float:
        """
        Volume z-score using the same computation as the live BarCache —
        both delegate to core.bar_features.volume_zscore so they are
        guaranteed to produce identical numbers given the same bars.

        Returns 0.0 for insufficient history, zero std, or missing ticker.
        """
        bars = self._bars_up_to(ticker, ts, need=self._n + 1)
        return volume_zscore(bars, self._n)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _bars_up_to(self, ticker: str, ts: datetime, need: int) -> list:
        """
        Return the last `need` bars with timestamp <= ts.

        bisect_right(tss, ts) returns the insertion index after any bar
        with timestamp == ts, so the slice [: idx] includes bars *at* ts
        (already-completed bar whose interval ends at ts) and excludes any
        bar whose interval starts after ts.
        """
        tss = self._timestamps.get(ticker)
        if not tss:
            return []
        idx = bisect.bisect_right(tss, ts)
        if idx == 0:
            return []
        bars = self._bars[ticker]
        # Slice only what we need so we don't copy the full history list.
        return bars[max(0, idx - need): idx]

    # ── Factory ────────────────────────────────────────────────────────────────

    @classmethod
    def load(
        cls,
        api_key: str,
        api_secret: str,
        config: dict,
        start: datetime,
        end: datetime,
    ) -> "BarStore":
        """
        Fetch bars for every ticker in the config universe over [start, end]
        and return a ready-to-query BarStore.

        Equities and commodities are fetched in one batch request;
        crypto in a second batch. Both use 1-minute bars to match the
        granularity of the live BarCache.

        start / end should be the full backtest window. Add a warm-up buffer
        (e.g. start - timedelta(minutes=n*3)) before calling load() so that
        point-in-time lookups early in the window have enough trailing bars
        to compute a z-score. The warm-up bars are stored and available but
        the backtest engine will never emit events before its own start time.
        """
        n = config.get("params", {}).get("volume_zscore_lookback", 20)
        categories = config.get("categories", {})

        equity_tickers: list[str] = []
        crypto_tickers: list[str] = []
        for cat_name, cat in categories.items():
            tickers = cat.get("tickers", [])
            if cat_name == "crypto":
                crypto_tickers.extend(tickers)
            else:
                equity_tickers.extend(tickers)

        stock_client = StockHistoricalDataClient(api_key, api_secret)
        crypto_client = CryptoHistoricalDataClient(api_key, api_secret)

        bars: dict[str, list] = {}

        if equity_tickers:
            req = StockBarsRequest(
                symbol_or_symbols=equity_tickers,
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
            )
            bar_set = stock_client.get_stock_bars(req)
            for ticker, ticker_bars in bar_set.data.items():
                # API returns ascending; sort defensively in case of edge cases.
                bars[ticker] = sorted(ticker_bars, key=lambda b: b.timestamp)

        if crypto_tickers:
            req = CryptoBarsRequest(
                symbol_or_symbols=crypto_tickers,
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
            )
            bar_set = crypto_client.get_crypto_bars(req)
            for ticker, ticker_bars in bar_set.data.items():
                bars[ticker] = sorted(ticker_bars, key=lambda b: b.timestamp)

        return cls(bars, n)
