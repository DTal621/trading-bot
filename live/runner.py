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

import uuid
from collections import defaultdict, deque
from datetime import timedelta

import yaml

from core.schema import utcnow, Action
from core.signals import LexiconScorer, build_signal
from core.strategy import decide
from core.broker import Broker
from core.logstore import DecisionLog
from core.guardrails import Guardrails, RiskLimits, PortfolioState, Verdict
from core.universe import flat_universe, stop_price
from data.news import NewsSource


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run(config_path: str, broker: Broker, news: NewsSource, log: DecisionLog,
        shadow_config: dict | None = None):
    config = load_config(config_path)          # frozen for the lifetime of the run
    tickers = flat_universe(config)
    half_life = config["params"]["news_half_life_seconds"]
    window = timedelta(seconds=config["params"]["news_window_seconds"])
    scorer = LexiconScorer()
    guardrails = Guardrails(RiskLimits(**config.get("risk_limits", {})))
    day_start_equity = broker.equity()

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
            "volume_zscore": _volume_zscore(event.ticker),  # implement from your bars
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


def _gross_exposure(broker: Broker) -> float:
    return sum(abs(q) * broker.last_price(t) for t, q in broker.positions().items())


def _volume_zscore(ticker: str) -> float:
    # Compute (current_volume - mean) / std over a trailing window from your bars.
    return 0.0
