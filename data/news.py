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

        Fail-fast behaviour
        -------------------
        If the stream thread exits for any reason (auth failure, network error,
        normal return) a sentinel is placed on the queue and the main thread
        raises RuntimeError so the process exits non-zero and systemd can
        restart it.  A 120-second timeout on queue.get() provides a secondary
        check: if the thread is dead and no sentinel arrived, we raise then too.
        """
        ticker_set = set(tickers)
        event_queue: queue.Queue = queue.Queue()
        _STOP = object()   # sentinel: stream thread has exited

        ws = NewsDataStream(self._api_key, self._api_secret)

        async def _handler(article) -> None:
            ingested_at = utcnow()   # true wall-clock ingest time on live feed
            matching = [s for s in article.symbols if s in ticker_set]
            for ticker in matching:
                event_queue.put(self._make_event(article, ticker, ingested_at))

        # Subscribe to all news; filter to our universe in _handler above.
        # Avoids per-symbol 400 "invalid syntax" errors from Alpaca.
        ws.subscribe_news(_handler, "*")

        def _run() -> None:
            try:
                ws.run()
            except Exception as exc:
                log.error("alpaca news stream exited with error: %s", exc, exc_info=True)
            finally:
                # Always wake the main thread so it detects the exit rather
                # than blocking on queue.get() indefinitely.
                event_queue.put(_STOP)

        t = threading.Thread(target=_run, daemon=True, name="alpaca-news-ws")
        t.start()

        while True:
            try:
                item = event_queue.get(timeout=120)
            except queue.Empty:
                # 120 s with no data — verify the thread is still alive.
                if not t.is_alive():
                    raise RuntimeError(
                        "alpaca news stream thread exited without sentinel "
                        "— raising so systemd can restart the process"
                    )
                # Thread alive, just quiet (market closed / no news). Keep waiting.
                log.debug("news stream alive but quiet (120 s timeout) — continuing")
                continue
            if item is _STOP:
                raise RuntimeError(
                    "alpaca news stream thread exited "
                    "— raising so systemd can restart the process"
                )
            yield item

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
