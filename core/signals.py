"""
signals.py — turn news + market context into a Signal.

This module is imported UNCHANGED by both the live runner and the backtest
engine. That is the whole point: the thing you validate offline is byte-for-byte
the thing that trades live. Never fork this logic into a "backtest version".

The scorer is intentionally pluggable. Start with the simple lexicon baseline so
you have a clean, fast, deterministic reference. Swap in FinBERT or an LLM scorer
later — but keep the SCORING RUBRIC FIXED per strategy version, because a moving
rubric makes every historical result uncomparable.
"""
from __future__ import annotations

from typing import Protocol, Sequence
from datetime import datetime

from core.schema import NewsEvent, Signal


class SentimentScorer(Protocol):
    """Maps a headline to a raw sentiment in [-1, 1] plus a confidence in [0, 1]."""
    def score(self, event: NewsEvent) -> tuple[float, float]: ...


class LexiconScorer:
    """
    Deliberately dumb baseline. Deterministic, instant, no model weights to drift.
    Its only job is to be the reference every smarter scorer must beat out-of-sample.
    """
    POS = {"beats", "surge", "record", "upgrade", "partnership", "approval", "growth"}
    NEG = {"misses", "plunge", "lawsuit", "downgrade", "probe", "hack", "halt", "recall"}

    def score(self, event: NewsEvent) -> tuple[float, float]:
        words = event.headline.lower().split()
        pos = sum(w in self.POS for w in words)
        neg = sum(w in self.NEG for w in words)
        if pos == neg == 0:
            return 0.0, 0.0
        raw = (pos - neg) / (pos + neg)
        conf = min(1.0, (pos + neg) / 3.0)
        return raw, conf


def build_signal(
    ticker: str,
    ts: datetime,
    recent_events: Sequence[NewsEvent],
    scorer: SentimentScorer,
    market_features: dict,
    half_life_seconds: float,
) -> Signal:
    """
    Aggregate the recent news for one ticker into a single point-in-time Signal.

    Two things worth noting for correctness:
      - We weight by recency (exponential decay) so stale headlines fade out.
        Sentiment 'level' decays; what tends to carry edge is the *fresh surprise*.
      - `market_features` (volume z-score, realized vol, spread...) ride along in
        the Signal so the strategy can require confirmation and the daily report
        can attribute outcomes to signal vs. regime.
    """
    if not recent_events:
        return Signal(ts=ts, ticker=ticker, score=0.0, confidence=0.0,
                      features=dict(market_features), contributing_event_ids=())

    num = 0.0
    den = 0.0
    conf_acc = 0.0
    ids = []
    for ev in recent_events:
        age = max(0.0, (ts - ev.published_at).total_seconds())
        w = 0.5 ** (age / half_life_seconds)
        raw, conf = scorer.score(ev)
        num += w * raw * conf
        den += w * conf
        conf_acc += w * conf
        ids.append(ev.event_id)

    score = (num / den) if den > 0 else 0.0
    confidence = min(1.0, conf_acc / max(1, len(recent_events)))
    feats = dict(market_features)
    feats["n_events"] = len(recent_events)
    feats["agg_score"] = score
    return Signal(ts=ts, ticker=ticker, score=score, confidence=confidence,
                  features=feats, contributing_event_ids=tuple(ids))
