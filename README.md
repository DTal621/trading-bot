# sentiment_bot — architecture sketch

A skeleton for a news-sentiment trading bot on Alpaca paper trading, built around
one principle: **the thing you validate offline must be byte-for-byte the thing
that trades live**, and **the bot never tunes itself in production.**

This is scaffolding, not a finished system. The Alpaca SDK wiring (`core/broker.py`,
`data/news.py`) is left as clearly-marked integration points — verify those against
the current `alpaca-py` docs, since that surface changes.

📖 **Full documentation is in [`docs/`](docs/README.md)** — architecture, strategy,
risk, the change pipeline, operations, configuration, and agent operation.

## The two loops (and the wall between them)

```
                 ┌─────────────────────── core/ (SHARED) ───────────────────────┐
                 │  signals.build_signal()      strategy.decide()  [pure, no I/O] │
                 └───────────────▲───────────────────────────▲───────────────────┘
                                 │                            │
        ┌────────────────────────┘                            └────────────────────────┐
        │ LIVE LOOP                                            RESEARCH / OFFLINE LOOP   │
        │ live/runner.py                                       backtest/engine.py        │
        │  stream → signal → decide → submit → log              replay point-in-time data│
        │  - loads frozen config ONCE                           backtest/fills.py         │
        │  - never tunes itself                                  (pessimistic costs)      │
        │  - logs EVERYTHING                                    backtest/walkforward.py   │
        │                                                        (the promotion GATE)     │
        └──────────────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
                          core/logstore.py  ──►  reports/daily.py
                       (append-only truth)      (learning surface, no auto-changes)
```

Because `live/runner.py` and `backtest/engine.py` both call the same
`core.signals` + `core.strategy`, a disagreement between live and backtest given
identical inputs is always a *data-plumbing* bug, never a strategy mismatch. That
property is the whole reason for the layout.

## What "learning" means here (read this part twice)

The appealing idea — "the bot trades, learns each day, improves itself" — quietly
overfits to noise if you let the live system adapt on recent P&L. So learning is
split:

- **The bot does not learn in production.** It runs `config/strategy.yaml` frozen,
  and logs every decision with the `strategy_version` + `config_hash` that produced it.
- **You (and an offline agent) learn from the logs.** `reports/daily.py` surfaces
  what happened and forms *hypotheses* — it never changes the live spec.
- **Changes are gated.** A proposed change gets a new version and must clear
  `backtest/walkforward.py`'s out-of-sample promotion gate before it can go live.

So the daily report is a lab notebook, not a dashboard of a self-improving oracle.
That is the version of "the bot teaches me" that actually builds real intuition
instead of teaching you to trust randomness.

## Layout

```
config/strategy.yaml      frozen, versioned spec (the only place behaviour changes)
core/schema.py            data + logging records (the backbone)
core/signals.py           news → Signal     (SHARED live+backtest)
core/strategy.py          Signal → Decision (SHARED, pure function)
core/broker.py            Broker protocol + Alpaca paper sketch
core/logstore.py          append-only JSONL truth
data/news.py              news source (Alpaca/Benzinga) interface
live/runner.py            thin live loop
backtest/engine.py        event-driven, point-in-time backtest
backtest/fills.py         fee + slippage model (set pessimistically)
backtest/walkforward.py   walk-forward folds + promotion gate
reports/daily.py          daily report / hypothesis surface
ops/approval.py           notify + approve transport (Telegram sketch + manual)
ops/deploy.py             versioned deploy + clean revert
ops/trial.py              2-week shadow A/B trial + report
ops/change_pipeline.py    orchestrates gate -> approval -> trial -> finalize
```

## How a change actually reaches the live config

Nothing edits the live spec in place. A change runs a fixed gauntlet, and the
human owns every decision that isn't pure validation:

```
proposal -> walk-forward gate -> [human approves, with evidence attached]
         -> deploy new version + 2-week shadow A/B trial
         -> trial report -> [human keeps or cancels]
```

The walk-forward gate sits BEFORE human approval on purpose, so you approve on
out-of-sample evidence rather than a hunch. The 2-week trial runs the previous
version in shadow (logged, not traded) for a same-tape A/B — it is monitoring and
confirmation, never the proof that an edge is real. Revert is lossless because
every version is kept and every decision is stamped with its version.

## Order to build it in

1. Wire `data/news.py` + `core/broker.py` to current `alpaca-py` (paper only).
2. Get `backtest/engine.py` running on historical news — confirm point-in-time
   correctness (it must use `published_at`, never ingest time).
3. Establish the **baseline** numbers with pessimistic costs. Write them down.
4. Run `live/runner.py` on paper. Confirm live decisions match backtest decisions
   on replayed inputs.
5. Only now start proposing changes — each one through `walkforward.promote()`.

## Guardrails worth keeping

- Keep `core/strategy.py` a pure function. No clocks, no network, no globals.
- Never fork "a backtest version" of the signal/strategy logic.
- Set costs in `fills.py` pessimistically; if the edge dies, better to learn it here.
- Treat sample size as the enemy of self-deception: hundreds of trades before you
  believe anything, and out-of-sample or it doesn't count.
- Paper only until you have a long, boring, validated track record. Not financial advice.
```
