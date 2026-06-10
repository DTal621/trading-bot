# Deployment runbook

The ordered sequence to take this from repo to a running paper bot. Do the steps
in order; each assumes the previous one is done.

Principle that overrides everything: this runs on **paper** until a deliberate
go-live (step 8). And secret *values* are never typed into a model prompt — they
go into the GitHub UI or the instance `.env` by hand.

---

## 1. Bring the code into the repo

The private repo and a connected Claude Code instance already exist. Drop the
project files into the repo working tree (or merge them in), keeping the layout
intact, then commit:

```
git add .
git commit -m "import sentiment bot skeleton"
git push
```

Verify `.gitignore` is present so `.env`, caches, and local artifacts stay out.

## 2. Finish the wiring points with Claude Code

Several integration points are stubbed (see the table in
[`../docs/operations.md`](../docs/operations.md)). Have the connected Claude Code
instance complete them against the existing Alpaca connection, in dependency
order. Suggested prompts, one at a time, testing after each:

1. *"Wire `core/broker.py` `AlpacaPaperBroker` to the current alpaca-py SDK,
   paper endpoint only: implement positions(), equity(), last_price(),
   is_market_open(), and submit() — submit a bracket order with the attached
   stop_price when present. Verify method names against current alpaca-py docs."*
2. *"Wire `data/news.py` `AlpacaNews` to Alpaca's news API: historical() yielding
   NewsEvents sorted ascending by published_at, and stream() for live. Emit one
   NewsEvent per symbol on multi-ticker articles."*
3. *"Implement `_volume_zscore` in `live/runner.py` from trailing bars via the
   alpaca-py data client (point-in-time only)."*
4. *"Implement point-in-time `price_at` and `volume_z_at` for the backtest using
   historical bars — never use a bar dated after the lookup timestamp."*
5. *"Implement `_build_backtest_fn` in `cli.py`: load Alpaca historical news+bars
   for the window, run `backtest.engine.Backtester`, return its report dict."*
6. *"Add stop-fill reconciliation: when a broker-side bracket stop fills, log a
   synthetic EXIT decision + outcome so the decision log stays complete."*
7. *"In the daily-report job, detect DEPLOYED_TRIAL proposals whose trial window
   has closed and send the Keep/Cancel prompt once."*

Keep `core/strategy.py` pure and never fork the shared core (the `CLAUDE.md` rules
cover this). Commit and push after each works.

## 3. Confirm the test suite / smoke checks pass

Run the module compile and the existing smoke checks locally before relying on
anything. Nothing should place an order until step 7.

## 4. GitHub Actions secrets and workflows

In the repo settings, ensure these Actions secrets exist (set via the GitHub UI):
`ANTHROPIC_API_KEY`, `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`. Confirm the four workflows appear under the Actions tab. Note
that scheduled workflows only begin running once they exist on the default branch.

## 5. Provision the instance

Follow [`HOSTING.md`](HOSTING.md) to create the Oracle Always Free VM, clone the
repo to `/opt/sentiment_bot`, install requirements, and give the box write access
so `sync-logs` can push `state/`.

## 6. Configure the instance environment

Copy `deploy/env.example` to `/opt/sentiment_bot/.env`, `chmod 600`, and fill in
the values by hand on the box. Then install and enable the services:

```
sudo cp deploy/sentiment-*.service deploy/sentiment-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentiment-bot sentiment-telegram sentiment-sync.timer
```

## 7. First-run checklist (paper)

- [ ] `systemctl status sentiment-bot` is active; logs show news being ingested.
- [ ] `state/decisions.jsonl` is being written and `sync-logs` is pushing it.
- [ ] A daily-report run (trigger `daily-report` manually) posts to Telegram.
- [ ] File a trivial test proposal, run `walk-forward` manually, and confirm: it
      either fails the gate or arrives in Telegram with evidence; an Approve tap
      deploys a new version and opens a trial; Keep/Cancel resolves it; a Cancel
      cleanly reverts the live version.
- [ ] Confirm equity orders only fire in market hours and crypto runs 24/7.

Let it run on paper for a long, boring stretch before considering anything else.

## 8. Going live (later — deliberate, not now)

Real money is a conscious act, never the agent's and never a side effect:

- [ ] Long, validated paper track record reviewed.
- [ ] Every risk limit re-examined for real capital (paper-sized caps are usually
      wrong for live).
- [ ] Separate live API keys, distinct from paper keys.
- [ ] Set `risk_limits.allow_real_money: true` by hand, deliberately.
- [ ] Trial the first live changes in shadow or on a tiny capital slice first.

## Repo visibility

The repo starts private. The `docs/` are written for a public audience, but
`state/` accumulates trade logs — scrub or relocate trade history out of the repo
(e.g. to object storage) before flipping the repo public.
