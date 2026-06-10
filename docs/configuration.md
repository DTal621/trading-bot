# Configuration reference

All behaviour is driven by `config/strategy.yaml`. Changing it is how behaviour
changes — but never in place on a running bot. Bump `version`, validate via
walk-forward, get approval, trial it. See [change-pipeline.md](change-pipeline.md).

## `version`
A string stamped onto every decision (`strategy_version`) so each spec can be
evaluated on its own out-of-sample track record. Treat it like a git tag for
behaviour. Bump it for any change.

## `categories`
The single source of truth for the universe. Each category has `tickers` and a
`stop_loss_pct`. The flat ticker list and the per-ticker category/stop lookups are
derived in `core/universe.py`.

```yaml
categories:
  equities:    { stop_loss_pct: 0.06, tickers: [AAPL, MSFT, NVDA, AMZN] }
  crypto:      { stop_loss_pct: 0.12, tickers: ["BTC/USD", "ETH/USD"] }
  commodities: { stop_loss_pct: 0.04, tickers: [GLD, USO] }
```

## `params`
| Key | Default | Meaning |
|-----|---------|---------|
| `entry_score_threshold` | 0.40 | Minimum |aggregate sentiment| to act. |
| `min_confidence` | 0.35 | Below this, ignore (thin/ambiguous news). |
| `volume_zscore_gate` | 1.5 | Required volume confirmation before entering. |
| `max_position_pct` | 0.05 | Position size as a fraction of equity. |
| `news_window_seconds` | 1800 | Trailing window of news that counts (30 min). |
| `news_half_life_seconds` | 600 | Recency decay half-life (10 min). |

## `backtest`
| Key | Default | Meaning |
|-----|---------|---------|
| `starting_equity` | 100000 | Backtest starting capital. |
| `assumed_latency_seconds` | 3 | Delay between a headline and when the bot could act. |

## `risk_limits` (enforced in `core/guardrails.py`)
| Key | Default | Meaning |
|-----|---------|---------|
| `allow_real_money` | false | Real-money interlock; the agent must never flip it. |
| `max_order_notional` | 10000 | Absolute per-order ceiling. |
| `max_position_pct` | 0.05 | Per-name cap (defense-in-depth). |
| `max_gross_exposure_pct` | 0.90 | Total deployed-capital cap. |
| `max_daily_loss_pct` | 0.06 | Kill-switch threshold for new entries. |

There is intentionally **no `max_open_positions`** — position count is the
strategy's choice, bounded only by the per-name and gross caps.

## `change_workflow`
| Key | Default | Meaning |
|-----|---------|---------|
| `require_human_approval` | true | A validated change still needs operator approval. |
| `trial_days` | 14 | Shadow A/B trial length after approval. |
| `shadow_ab` | true | Run the previous version logged-only during the trial. |

## `llm_review`
| Key | Default | Meaning |
|-----|---------|---------|
| `model` | claude-sonnet-4-5 | Model for the daily review (Haiku-class is fine and cheaper). API-billed. |
