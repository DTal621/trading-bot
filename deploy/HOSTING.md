# Hosting & scheduling

## Where things run

```
Oracle Always Free VM (always-on)        GitHub Actions (scheduled cron)
  - run-live      (trading loop)            - daily-report    22:00 UTC daily
  - telegram-listen (approvals)             - weekly-report   Sat 12:00 UTC
  - sync-logs     (every 30 min)            - monthly-report  1st 12:00 UTC
        |                                    - walk-forward    Sun 06:00 UTC / on demand
        |  push state/ to repo                       |
        +---------------> git repo (shared state) <--+  commit reports + results
                                |
                          you, via Telegram (approve / reject / keep / cancel)
                                |
                          instance listener actuates (deploy / revert)
```

The repo is the contract: the instance pushes `state/` (logs, proposals, versions)
up; Actions read it and push reports/validation back; your Telegram taps are
handled by the always-on listener because Actions can't wait for a human.

## Instance setup (Oracle Always Free)

1. Create an Always Free `VM.Standard.E2.1.Micro`, image Ubuntu. (Card
   verification at signup; Always Free shapes don't charge. No SLA — `state/` is
   pushed to git, so a lost VM loses nothing permanent.)
2. SSH in, then:
   ```
   sudo apt update && sudo apt install -y python3-pip git
   sudo git clone <your-repo> /opt/sentiment_bot
   cd /opt/sentiment_bot && pip install -r requirements.txt
   cp deploy/env.example .env && chmod 600 .env   # fill in keys
   ```
3. Give the box push access (deploy key with write, or a fine-scoped PAT) so
   `sync-logs` can push `state/`.
4. Install the services:
   ```
   sudo cp deploy/sentiment-*.service deploy/sentiment-sync.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now sentiment-bot sentiment-telegram sentiment-sync.timer
   ```

## GitHub Actions setup

Add these repo secrets (Settings → Secrets and variables → Actions):
`ANTHROPIC_API_KEY`, `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`. The workflows in `.github/workflows/` then run on their crons
(and via "Run workflow" on demand).

## Cost

The VM and Actions are free. The only spend is the daily LLM review API call —
a couple thousand input tokens and ~1.5k out, well under a dollar a month, billed
to your Anthropic API account, NOT your Claude subscription. Walk-forward,
reports, and approvals use no LLM. Run the review with Haiku to make it cheaper.

## Caveats worth knowing

- Actions cron is best-effort and can be delayed under load; it also auto-disables
  after ~60 days of no repo activity. Fine for reports; never for the live loop.
- Equity orders only fire during market hours (the loop checks); crypto is 24/7.
- All crons above are UTC. Adjust for your market hours / timezone.
