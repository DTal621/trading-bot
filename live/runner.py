"""
runner.py — the live loop. Thin on purpose.

It does nothing but: pull context -> call the SHARED core (signals + strategy)
-> submit -> log. All the intelligence lives in core/. If you find yourself
adding decision logic here, stop — it belongs in core/strategy.py so the
backtest sees it too.

The strategy spec is loaded ONCE at startup and frozen for the run. The loop
never re-reads it, never tunes it. To change behaviour you stop the bot, edit
the spec, bump the version, re-validate, redeploy.
"""
from __future__ import annotations

import os
import statistics
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Optional

import yaml

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

from core.schema import utcnow, Action
from core.signals import LexiconScorer, build_signal
from core.strategy import decide
from core.broker import Broker
from core.logstore import DecisionLog
from core.guardrails import Guardrails, RiskLimits, PortfolioState, Verdict
from core.universe import flat_universe, stop_price
from data.news import NewsSource


# ── Bar cache & volume z-score ─────────────────────────────────────────────────

class BarCache:
    """
    Fetches and caches 1-minute bars per ticker for volume z-score computation.

    Design choices:
      - 1-minute bars for both equities and crypto (consistent granularity).
        Equities use StockHistoricalDataClient; crypto uses CryptoHistoricalDataClient.
        The ticker's category in config drives routing; no heuristics.
      - Lookback = (N+1)*3 minutes (3× headroom over the N+1 bars we actually
        need). For the default N=20 this is 63 minutes — covers any short intraday
        gap (brief halt, thin period) without over-fetching. We take the last N+1
        bars from whatever comes back.
      - Cache TTL = 1 minute (one bar period). Multiple news events arriving in
        the same minute all read the same cached slice; only the first event per
        ticker per minute triggers a fetch.
      - All failure modes (API error, zero bars, market closed) return 0.0 —
        the volume gate in strategy.py then requires a real confirmation signal
        before entering, which is the safe default.

    SDK surface verified 2026-06:
      StockHistoricalDataClient.get_stock_bars(StockBarsRequest)  -> BarSet
      CryptoHistoricalDataClient.get_crypto_bars(CryptoBarsRequest) -> BarSet
      BarSet.data[symbol] -> List[Bar]  (sorted ascending by timestamp)
      Bar: .volume float, .timestamp datetime
      StockBarsRequest / CryptoBarsRequest: symbol_or_symbols, timeframe, start
      TimeFrame.Minute  (1-minute convenience constant)
    """

    _STD_FLOOR = 1e-9   # treat population std below this as zero to avoid div/0

    def __init__(self, api_key: str, api_secret: str, config: dict) -> None:
        self._stock_client = StockHistoricalDataClient(api_key, api_secret)
        self._crypto_client = CryptoHistoricalDataClient(api_key, api_secret)
        # N bars in the trailing reference window; +1 for the "current" bar
        self._n: int = config.get("params", {}).get("volume_zscore_lookback", 20)
        # Route crypto tickers to the crypto client
        self._crypto_tickers: set[str] = set(
            config.get("categories", {}).get("crypto", {}).get("tickers", [])
        )
        # ticker -> (bars sorted ascending, fetched_at)
        self._cache: dict[str, tuple[list, datetime]] = {}
        self._ttl = timedelta(minutes=1)

    def zscore(self, ticker: str) -> float:
        """
        Return (current_bar_volume - window_mean) / window_std.

          current bar     = bars[-1]            most recent completed 1-min bar
          trailing window = bars[-(N+1) : -1]   the N bars before it

        Returns 0.0 when:
          - fewer than N+1 bars are available (pre-market, thin history, etc.)
          - window std is near zero (no volume variance → meaningless z-score)
          - any fetch error occurred
        """
        bars = self._get_bars(ticker)
        n = self._n

        if len(bars) < n + 1:
            return 0.0

        current_vol: float = bars[-1].volume
        window_vols: list[float] = [b.volume for b in bars[-(n + 1):-1]]

        mean = statistics.mean(window_vols)
        std = statistics.pstdev(window_vols)   # population std: window is our full reference

        if std < self._STD_FLOOR:
            return 0.0

        return (current_vol - mean) / std

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _get_bars(self, ticker: str) -> list:
        """Return cached bars, refreshing if the entry is older than one bar period."""
        entry = self._cache.get(ticker)
        if entry is not None:
            bars, fetched_at = entry
            if utcnow() - fetched_at < self._ttl:
                return bars
        bars = self._fetch(ticker)
        self._cache[ticker] = (bars, utcnow())
        return bars

    def _fetch(self, ticker: str) -> list:
        """
        Fetch the most recent N+1 completed 1-minute bars.

        The Alpaca bars API returns bars oldest-first. Passing `start` with no
        `limit` returns all bars from that start time; we take the last N+1.
        (Using `limit` alone caps from the beginning of the window, not the end,
        so we can't use it to get the most recent N+1 bars directly.)

        All bars returned by Alpaca are completed bars — the current in-progress
        bar is never included, so we never act on a bar from the future.
        """
        n = self._n
        # 3× headroom: for N=20 → 63 min lookback. Handles short intraday gaps.
        start = utcnow() - timedelta(minutes=(n + 1) * 3)

        try:
            if ticker in self._crypto_tickers:
                req = CryptoBarsRequest(
                    symbol_or_symbols=ticker,
                    timeframe=TimeFrame.Minute,
                    start=start,
                )
                bar_set = self._crypto_client.get_crypto_bars(req)
            else:
                req = StockBarsRequest(
                    symbol_or_symbols=ticker,
                    timeframe=TimeFrame.Minute,
                    start=start,
                )
                bar_set = self._stock_client.get_stock_bars(req)
        except Exception:
            return []

        bars = bar_set.data.get(ticker, [])
        # Defensive sort (API should already return ascending, but be explicit)
        # then trim to the last N+1 so the cache stays bounded.
        return sorted(bars, key=lambda b: b.timestamp)[-(n + 1):]


# ── Config loading ─────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ── Main run loop ──────────────────────────────────────────────────────────────

def run(config_path: str, broker: Broker, news: NewsSource, log: DecisionLog,
        shadow_config: Optional[dict] = None):
    config = load_config(config_path)          # frozen for the lifetime of the run
    tickers = flat_universe(config)
    half_life = config["params"]["news_half_life_seconds"]
    window = timedelta(seconds=config["params"]["news_window_seconds"])
    scorer = LexiconScorer()
    guardrails = Guardrails(RiskLimits(**config.get("risk_limits", {})))
    day_start_equity = broker.equity()

    # Bar cache: one fetch per ticker per minute, shared across all news events.
    # Credentials come from env — the same vars cli.py uses to wire the broker.
    bar_cache = BarCache(
        api_key=os.environ.get("ALPACA_API_KEY", ""),
        api_secret=os.environ.get("ALPACA_SECRET_KEY", ""),
        config=config,
    )

    recent: dict[str, deque] = defaultdict(deque)

    for event in news.stream(tickers):
        log.append("news", event)
        recent[event.ticker].append(event)

        now = utcnow()
        cutoff = now - window
        while recent[event.ticker] and recent[event.ticker][0].published_at < cutoff:
            recent[event.ticker].popleft()

        if not broker.is_market_open():
            continue

        market_features = {
            "last_price": broker.last_price(event.ticker),
            "volume_zscore": bar_cache.zscore(event.ticker),
        }
        signal = build_signal(event.ticker, now, list(recent[event.ticker]),
                              scorer, market_features, half_life)
        log.append("signal", signal)

        positions = broker.positions()
        decision = decide(
            signal=signal,
            current_qty=positions.get(event.ticker, 0.0),
            equity=broker.equity(),
            config=config,
            now=now,
            decision_id=str(uuid.uuid4()),
        )
        log.append("decision", decision)

        # SHADOW: during a trial the previous version evaluates the SAME signal
        # and we log its decision, but it never reaches the broker. This is the
        # A/B leg — same tape, no orders.
        if shadow_config is not None:
            shadow = decide(
                signal=signal,
                current_qty=positions.get(event.ticker, 0.0),
                equity=broker.equity(),
                config=shadow_config,
                now=now,
                decision_id=str(uuid.uuid4()),
            )
            log.append("shadow_decision", shadow)

        if decision.action in (Action.BUY, Action.SELL, Action.EXIT):
            state = PortfolioState(
                equity=broker.equity(),
                day_start_equity=day_start_equity,
                gross_exposure=_gross_exposure(broker),
                open_positions=sum(1 for q in positions.values() if q != 0),
                is_paper=True,   # this skeleton is paper-only by construction
            )
            guard = guardrails.check(decision, state, market_features["last_price"])
            log.append("guardrail", {"decision_id": decision.decision_id,
                                     "verdict": guard.verdict.value, "reason": guard.reason})
            if guard.verdict != Verdict.APPROVED:
                continue  # blocked or halted — never reaches the broker
            # On an ENTRY, attach a broker-side stop from the ticker's category.
            sp = None
            if decision.action in (Action.BUY, Action.SELL):
                sp = stop_price(event.ticker, market_features["last_price"],
                                decision.target_qty, config)
            order = broker.submit(decision, stop_price=sp)
            log.append("order", order)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _gross_exposure(broker: Broker) -> float:
    return sum(abs(q) * broker.last_price(t) for t, q in broker.positions().items())
