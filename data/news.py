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

SDK surface verified against alpaca-py source + live probe 2026-06:
  NewsClient.get_news(NewsRequest)        -> NewsSet  (auto-paginates)
  NewsSet.data['news']                    -> List[News]
  News: .id int, .headline str, .source str, .summary str|None,
        .created_at datetime (tz-aware UTC), .symbols List[str]
  NewsRequest.symbols                     -> Optional[str] (comma-separated)
  NewsRequest.sort                        -> Optional[str] "ASC"|"DESC"
                                             sorts by updated_at — we re-sort
                                             by created_at ourselves
  NewsDataStream.subscribe_news(coro, *syms)
  NewsDataStream.run()                    -> synchronous, blocks on asyncio loop
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Protocol, Iterator, Iterable
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
from alpaca.data.live.news import NewsDataStream

from core.schema import NewsEvent, utcnow


# ── Protocol ───────────────────────────────────────────────────────────────────

class NewsSource(Protocol):
    def historical(self, tickers: Iterable[str],
                   start: datetime, end: datetime) -> Iterator[NewsEvent]: ...
    def stream(self, tickers: Iterable[str]) -> Iterator[NewsEvent]: ...


# ── Alpaca implementation ──────────────────────────────────────────────────────

class AlpacaNews:
    """
    Alpaca / Benzinga news — REST history + WebSocket stream.

    assumed_latency_s is stamped as the gap between published_at and
    ingested_at on historical events (we didn't actually ingest them in real
    time, so we simulate a realistic latency). Live stream events record the
    true wall-clock ingest time.
    """

    def __init__(self, api_key: str, api_secret: str,
                 assumed_latency_s: float = 2.0) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self.assumed_latency_s = assumed_latency_s
        self._rest = NewsClient(api_key, api_secret)

    # ── Public interface ───────────────────────────────────────────────────────

    def historical(
        self,
        tickers: Iterable[str],
        start: datetime,
        end: datetime,
    ) -> Iterator[NewsEvent]:
        """
        Yield all NewsEvents for *tickers* in [start, end], sorted ascending
        by published_at (= article.created_at).

        Multi-ticker articles emit one event per symbol that appears in the
        requested ticker set so per-ticker aggregation stays clean.

        get_news auto-paginates across all pages before returning — we then
        sort the full result by created_at because the API sort key is
        updated_at, which may differ from the original publication time.
        """
        ticker_set = set(tickers)
        symbols_param = ",".join(ticker_set)

        req = NewsRequest(
            symbols=symbols_param,
            start=start,
            end=end,
            sort="ASC",          # fetch oldest-first; we re-sort below
            include_content=False,
        )
        news_set = self._rest.get_news(req)
        articles = news_set.data.get("news", [])

        # Re-sort by created_at (published time) — API sorts by updated_at.
        articles_sorted = sorted(articles, key=lambda a: a.created_at)

        for article in articles_sorted:
            # Only emit for tickers we actually track; skip symbols not in set.
            matching = [s for s in article.symbols if s in ticker_set]
            if not matching:
                # Article was tagged to a superset; none match our universe.
                continue
            # Simulate realistic ingest latency on historical data.
            # The backtest engine uses ingested_at to decide when the signal
            # was actually actionable — never earlier than published_at.
            ingested_at = (article.created_at
                           + timedelta(seconds=self.assumed_latency_s))
            for ticker in matching:
                yield self._make_event(article, ticker, ingested_at)

    # Reconnect policy for stream().
    _STREAM_MAX_ATTEMPTS = 5
    _STREAM_BACKOFF_S    = [5, 15, 30, 60, 120]   # delay *before* each retry

    def stream(self, tickers: Iterable[str]) -> Iterator[NewsEvent]:
        """
        Yield live NewsEvents from Alpaca's WebSocket news feed.

        The WebSocket runs async internally; we bridge it to a synchronous
        iterator via a thread-safe queue so the caller can use a plain for-loop
        without knowing about asyncio.

        Multi-ticker articles emit one event per symbol in the tracked set,
        identical to historical() — same normalization path, same guarantees.

        Subscribe / symbol-format notes
        --------------------------------
        We subscribe to "*" (all news) rather than per-symbol.  Per-symbol
        subscription can trigger a 400 "invalid syntax" rejection from Alpaca
        for certain symbol formats (e.g. crypto pairs such as BTC/USD).  The
        SDK logs the 400 and keeps the thread alive, but the subscription is
        silently dead and no events ever flow.  Subscribing to "*" and filtering
        locally in the handler is equivalent and avoids the format issue entirely.

        Reconnect / fail-fast behaviour
        --------------------------------
        On disconnect the method attempts an in-process reconnect with
        exponential backoff (up to _STREAM_MAX_ATTEMPTS times).  If any event
        is received on a connection the attempt counter resets, so a transient
        blip after a long healthy run does not consume retries.  After all
        attempts are exhausted a RuntimeError is raised so the process exits
        non-zero and systemd can restart it.

        A dead stream thread can never leave the main loop blocked:
          - The _run wrapper always puts a _STOP sentinel (try/finally).
          - queue.get(timeout=120) + t.is_alive() check provides a secondary
            guard if the sentinel is somehow lost.
        Both of these break out of the inner drain loop and drive the reconnect
        logic — silent-hang behaviour is structurally impossible.
        """
        ticker_set = set(tickers)
        _STOP = object()   # sentinel: this connection's thread has exited

        attempt = 0

        while attempt < self._STREAM_MAX_ATTEMPTS:
            # Backoff before every retry (not before the first attempt).
            if attempt > 0:
                delay = self._STREAM_BACKOFF_S[
                    min(attempt - 1, len(self._STREAM_BACKOFF_S) - 1)
                ]
                log.warning(
                    "news stream disconnected — reconnect attempt %d/%d in %ds",
                    attempt, self._STREAM_MAX_ATTEMPTS, delay,
                )
                time.sleep(delay)

            # Fresh queue and WebSocket object for each attempt.  Capture them
            # via default args (not closures) so each attempt's callbacks are
            # bound to that attempt's queue, not whatever the variable points to
            # when the callback eventually fires.
            event_queue: queue.Queue = queue.Queue()
            ws = NewsDataStream(self._api_key, self._api_secret)

            async def _handler(
                article,
                _q=event_queue,
                _ts=ticker_set,
            ) -> None:
                ingested_at = utcnow()
                matching = [s for s in article.symbols if s in _ts]
                for ticker in matching:
                    _q.put(self._make_event(article, ticker, ingested_at))

            # Subscribe to all news; filter to our universe in _handler.
            ws.subscribe_news(_handler, "*")

            def _run(_ws=ws, _q=event_queue) -> None:
                try:
                    _ws.run()
                except Exception as exc:
                    log.error(
                        "news stream exited with error: %s", exc, exc_info=True
                    )
                finally:
                    # Always unblock the drain loop so it can reconnect or exit.
                    _q.put(_STOP)

            t = threading.Thread(
                target=_run, daemon=True,
                name=f"alpaca-news-ws-{attempt + 1}",
            )
            t.start()
            log.info(
                "alpaca news stream started (attempt %d/%d)",
                attempt + 1, self._STREAM_MAX_ATTEMPTS,
            )
            attempt += 1

            # ── Drain this connection ──────────────────────────────────────────
            while True:
                try:
                    item = event_queue.get(timeout=120)
                except queue.Empty:
                    # 120 s with no data — check the thread is still alive.
                    if not t.is_alive():
                        log.warning(
                            "news stream thread died without sentinel "
                            "— triggering reconnect"
                        )
                        break   # back to outer reconnect loop
                    # Thread alive, market closed / no news. Keep waiting.
                    log.debug(
                        "news stream alive but quiet (120 s timeout) — continuing"
                    )
                    continue

                if item is _STOP:
                    log.warning("news stream thread exited — triggering reconnect")
                    break   # back to outer reconnect loop

                # Real event received: connection is healthy, reset attempt
                # counter so this blip doesn't consume a permanent retry slot.
                attempt = 0
                yield item
            # ── end drain loop ─────────────────────────────────────────────────

        raise RuntimeError(
            f"alpaca news stream failed after {self._STREAM_MAX_ATTEMPTS} attempts "
            "— exiting non-zero so systemd can restart the process"
        )

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _make_event(self, article, ticker: str, ingested_at: datetime) -> NewsEvent:
        """
        Build one NewsEvent for a single (article, ticker) pair.

        event_id is scoped per (article, ticker) so the same article tagged to
        multiple symbols produces distinct, non-colliding IDs.

        published_at = article.created_at (Benzinga publication time).
        ingested_at  = caller-supplied (simulated latency for historical,
                       true wall-clock for live stream).
        """
        return NewsEvent(
            event_id=f"{article.id}:{ticker}",
            ticker=ticker,
            headline=article.headline or "",
            source=article.source or "alpaca-benzinga",
            published_at=article.created_at,        # point-in-time truth
            ingested_at=ingested_at,
            summary=article.summary or "",
        )
