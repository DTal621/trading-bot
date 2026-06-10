"""
cli.py — the single entrypoint every scheduled or long-running job calls.

Subcommands map to WHERE they run:

  INSTANCE (always-on, via systemd):
    run-live          the trading loop
    telegram-listen   catch approve/reject/keep/cancel taps and actuate
    sync-logs         push state/ to the repo so Actions can read it

  GITHUB ACTIONS (scheduled cron):
    daily-report      LLM review + daily report -> commit + Telegram
    weekly-report     weekly roll-up
    monthly-report    monthly roll-up
    walk-forward      validate pending proposals on out-of-sample history

Shared state lives under state/ and is the contract between instance and Actions.
Anything needing live broker/news/Telegram is wired from env; the report jobs run
without a broker because they only read logs.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from core.logstore import DecisionLog
from core.proposals import ProposalBacklog, ProposalStatus
from ops.deploy import Deployer
from ops.approval import TelegramApproval, ManualApproval
from reports.daily import build_daily_report
from reports.periodic import build_period_report
from analysis.llm_review import review, anthropic_call

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
LOG_PATH = STATE / "decisions.jsonl"
BACKLOG_PATH = STATE / "proposals.jsonl"
VERSIONS = STATE / "versions"
LIVE_PTR = STATE / "LIVE"
OUT = ROOT / "reports" / "out"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _config() -> dict:
    """Live (deployed) config if one is set, else the repo's strategy.yaml."""
    dep = Deployer(VERSIONS, LIVE_PTR)
    try:
        return dep.load_live()
    except Exception:
        return yaml.safe_load((ROOT / "config" / "strategy.yaml").read_text())


def _approval():
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    return TelegramApproval(tok, chat) if (tok and chat) else ManualApproval()


def _call_model(cfg: dict):
    if os.environ.get("ANTHROPIC_API_KEY"):
        return anthropic_call(cfg.get("llm_review", {}).get("model", "claude-sonnet-4-5"))
    # No key in this environment: skip the LLM, emit a valid empty review.
    return lambda system, user: '{"observations": ["LLM review skipped (no API key)."], "proposals": []}'


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"wrote {path}")


# --- GitHub Actions jobs ------------------------------------------------------

def daily_report(_args) -> None:
    cfg, log = _config(), DecisionLog(LOG_PATH)
    backlog, appr = ProposalBacklog(BACKLOG_PATH), _approval()
    res = review(log, backlog, cfg, _call_model(cfg))
    rep = build_daily_report(log, date.today(), backlog, res["observations"])
    _write(OUT / "daily" / f"{date.today().isoformat()}.md", rep)
    appr.send_report(rep)


def _period(label: str, days: int) -> None:
    log = DecisionLog(LOG_PATH)
    end = _now()
    start = end - timedelta(days=days)
    rep = build_period_report(log, start, end, label)
    _write(OUT / label.lower() / f"{end.date().isoformat()}.md", rep)
    _approval().send_report(rep)


def weekly_report(_a):  _period("weekly", 7)
def monthly_report(_a): _period("monthly", 30)


def walk_forward(_args) -> None:
    from ops.change_pipeline import validate
    cfg = _config()
    backlog = ProposalBacklog(BACKLOG_PATH)
    dep = Deployer(VERSIONS, LIVE_PTR)
    appr = _approval()
    pending = backlog.by_status(ProposalStatus.PENDING_VALIDATION)
    if not pending:
        print("no pending proposals to validate")
        return
    # WIRING POINT: supply a backtest_fn backed by Alpaca historical news+bars.
    # In Actions, ALPACA_API_KEY / ALPACA_API_SECRET come from repo secrets.
    backtest_fn = _build_backtest_fn(cfg)
    start = _now() - timedelta(days=400)
    end = _now()
    for p in pending:
        msg = validate(p["proposal_id"], backlog, cfg, appr, backtest_fn, start, end, dep)
        print(f"{p['proposal_id'][:8]}: {msg}")


def _build_backtest_fn(_cfg):
    """
    Return a backtest_fn(config, start, end) -> report dict suitable for
    walk-forward validation.

    Called once before the fold loop; the returned closure is invoked once per
    fold (typically ~8 folds over a 400-day window).  Each call:

      1. Fetches historical news events for [start, end] via AlpacaNews.historical().
         Events are re-sorted ascending by published_at after fetching so the engine
         always receives a strictly ordered stream — point-in-time discipline starts here.

      2. Fetches 1-minute bars for the same window (plus a warm-up buffer so the
         first events have trailing history for the volume z-score) via BarStore.load().
         price_at / volume_z_at use only bars timestamped <= the decision timestamp,
         enforced by bisect_right inside BarStore — no future data leaks.

      3. Constructs Backtester with a pessimistic CostModel (5 bps spread,
         3 bps slippage, 0.25% taker fee) and the two point-in-time callables.

      4. Runs the engine and returns its report dict.

    Credential priority: ALPACA_API_SECRET (set in walk-forward.yml via repo
    secrets) then ALPACA_SECRET_KEY (local .env).  Both names are kept so the
    same code runs in Actions and locally without changes.
    """
    from data.news import AlpacaNews
    from data.bars import BarStore
    from backtest.engine import Backtester
    from backtest.fills import CostModel
    from core.universe import flat_universe

    api_key = os.environ.get("ALPACA_API_KEY", "")
    # Walk-forward workflow (Actions) exports ALPACA_API_SECRET; local .env uses
    # ALPACA_SECRET_KEY.  Accept either so no env-specific branch is needed.
    api_secret = (os.environ.get("ALPACA_API_SECRET")
                  or os.environ.get("ALPACA_SECRET_KEY", ""))

    # Construct the news client once — it is stateless across historical() calls.
    news_src = AlpacaNews(api_key, api_secret)

    def backtest_fn(config: dict, start: datetime, end: datetime) -> dict:
        tickers = flat_universe(config)
        n = config.get("params", {}).get("volume_zscore_lookback", 20)

        print(f"[backtest] {start.date()} → {end.date()}  tickers={len(tickers)}")

        # ── bars ──────────────────────────────────────────────────────────────
        # Extend start backward by one full lookback window so that events at
        # the very beginning of the fold already have N trailing bars available.
        warmup = timedelta(minutes=(n + 1) * 3)
        store = BarStore.load(api_key, api_secret, config, start - warmup, end)

        # ── news ──────────────────────────────────────────────────────────────
        # historical() already sorts by published_at, but sort explicitly here
        # so the guarantee is local to this function and survives any future
        # change to the news source implementation.
        events = sorted(
            news_src.historical(tickers, start, end),
            key=lambda e: e.published_at,
        )
        print(f"[backtest]   {len(events)} news events loaded")

        # ── engine ────────────────────────────────────────────────────────────
        bt = Backtester(
            config=config,
            cost=CostModel(),          # pessimistic defaults: 5 bps spread,
                                       # 3 bps slippage, 0.25% taker fee
            price_at=store.price_at,   # close of last bar with timestamp <= ts
            volume_z_at=store.volume_z_at,  # z-score using only bars <= ts
        )
        report = bt.run(events)
        print(f"[backtest]   trades={report['trades']}  "
              f"win_rate={report['win_rate']:.1%}  net_pnl={report['net_pnl']:.2f}")
        return report

    return backtest_fn


# --- Instance jobs ------------------------------------------------------------

def run_live(_args) -> None:
    print("run-live: wire AlpacaPaperBroker + AlpacaNews from env, then call "
          "live.runner.run(config_path, broker, news, log[, shadow_config]).")
    raise SystemExit("not wired: provide broker + news (see core/broker.py, data/news.py)")


def telegram_listen(_args) -> None:
    import time
    from ops.change_pipeline import approve, finalize
    appr = _approval()
    if isinstance(appr, ManualApproval):
        raise SystemExit("telegram-listen needs TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID")
    cfg = _config()
    backlog = ProposalBacklog(BACKLOG_PATH)
    dep = Deployer(VERSIONS, LIVE_PTR)
    log = DecisionLog(LOG_PATH)
    trial_days = cfg.get("change_workflow", {}).get("trial_days", 14)
    print("telegram-listen: polling for approve/reject/keep/cancel ...")
    while True:
        awaiting = {p["proposal_id"] for p in backlog.by_status(ProposalStatus.AWAITING_APPROVAL)}
        trialing = {p["proposal_id"] for p in backlog.by_status(ProposalStatus.DEPLOYED_TRIAL)}
        acted = False
        for pid, action in appr.fetch_decisions(awaiting | trialing):
            status = (backlog.get(pid) or {}).get("status")
            if status == ProposalStatus.AWAITING_APPROVAL.value and action in ("approve", "reject"):
                print(approve(pid, backlog, cfg, appr, dep, trial_days))
                acted = True
            elif status == ProposalStatus.DEPLOYED_TRIAL.value and action in ("keep", "cancel"):
                print(finalize(pid, backlog, dep, log, appr))
                acted = True
        if acted:
            sync_logs(None)   # push the new state so the repo + Actions stay in sync
        time.sleep(2)


def sync_logs(_args) -> None:
    """Commit + push state/ so Actions can read the latest logs."""
    STATE.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(ROOT), "add", "state"], check=False)
    msg = f"sync state {_now().isoformat(timespec='seconds')}"
    r = subprocess.run(["git", "-C", str(ROOT), "commit", "-m", msg], capture_output=True)
    if r.returncode != 0:
        print("nothing to commit")
        return
    subprocess.run(["git", "-C", str(ROOT), "push"], check=False)
    print("pushed state")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="sentiment-bot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in {
        "daily-report": daily_report, "weekly-report": weekly_report,
        "monthly-report": monthly_report, "walk-forward": walk_forward,
        "run-live": run_live, "telegram-listen": telegram_listen, "sync-logs": sync_logs,
    }.items():
        sub.add_parser(name).set_defaults(func=fn)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
