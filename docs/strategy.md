# Strategy (v1.0.0-baseline)

The v1 strategy is intentionally the simplest thing that is still a real
news-sentiment system. Its job is to be a clean, interpretable baseline that
every later change is measured against — not to be good yet.

## Universe

Eight names across three categories, defined in `config/strategy.yaml` under
`categories` (the single source of truth; the flat list is derived in
`core/universe.py`):

- **Equities:** AAPL, MSFT, NVDA, AMZN
- **Crypto:** BTC/USD, ETH/USD (spot — cannot be shorted on Alpaca)
- **Commodities:** GLD, USO (ETF proxies, not the underlying)

The universe is deliberately small so the daily logs stay interpretable. The
*mix* across categories is not prescribed anywhere — it emerges from what the
strategy reads in the market, bounded only by the concentration limits in
[risk.md](risk.md).

## Signal pipeline

Two stages, both in `core/signals.py`:

**Scoring.** Each headline is scored by a `SentimentScorer`. v1 uses
`LexiconScorer`, a deliberately dumb keyword counter returning a score in
`[-1, 1]` and a confidence that grows with the number of keywords found. It is a
placeholder reference — its only purpose is to be the boring baseline a smarter
scorer (FinBERT, or an LLM with a **fixed** rubric) must beat out-of-sample. Keep
any scorer's rubric frozen per strategy version, or historical results become
incomparable.

**Aggregation.** `build_signal` combines recent headlines per ticker with
exponential recency decay: `news_window_seconds` (30 min) bounds what counts, and
`news_half_life_seconds` (10 min) halves a headline's weight every ten minutes. So
the signal reflects *fresh* sentiment that fades, not a stale running average.

## Decision logic

`core/strategy.py` (`decide`) is a pure function from Signal → Decision.

**Entry (from flat)** requires all three of: confidence ≥ `min_confidence`
(0.35), |score| ≥ `entry_score_threshold` (0.40), and volume confirmation ≥
`volume_zscore_gate` (1.5). Only strong, confident sentiment *with* the market
actually reacting opens a position. Size is `max_position_pct` (5%) of equity.

**Exit** is sentiment reversal: if sentiment flips strongly against an open
position (misaligned and |score| ≥ entry threshold), it closes. On top of that, a
per-category **stop-loss** is attached at entry as a broker-side bracket order —
see [risk.md](risk.md).

## Parameters

See [configuration.md](configuration.md) for the full annotated list.

## Known limitations (read before trusting a backtest)

- **Volume gate depends on stubbed data.** `_volume_zscore` (runner) and
  `volume_z_at` (backtest) currently return 0, which is below the 1.5 gate — so
  until the price-bar plumbing is wired, the bot filters out *everything* and
  never enters. This is the highest-priority wiring point.
- **Lexicon scorer is a placeholder**, not a real sentiment model.
- **Crypto cannot be shorted on Alpaca**, but the sizing logic will produce a
  negative (short) quantity on a strongly bearish crypto signal. A long-only flag
  per ticker (or a broker-layer filter) is needed before shorting logic is trusted
  on BTC/ETH.
- **Backtest stops are checked at news-event timestamps**, not every bar, so stop
  modeling is approximate until bar-level stepping is added (see [risk.md](risk.md)).

A good first change to run through the [change pipeline](change-pipeline.md) is
adding a configurable stop/exit refinement — but only after the volume-bar
plumbing exists, since nothing trades without it.
