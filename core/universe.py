"""
universe.py — read the categorized universe from config.

`categories` in strategy.yaml is the single source of truth. Everything else
(the flat ticker list the runner/news feed iterate, the per-ticker category, and
the per-ticker stop-loss) is derived here so there's no second list to drift.
"""
from __future__ import annotations


def flat_universe(config: dict) -> list[str]:
    out: list[str] = []
    for cat in config["categories"].values():
        out.extend(cat["tickers"])
    return out


def _ticker_map(config: dict) -> dict[str, str]:
    m = {}
    for name, cat in config["categories"].items():
        for t in cat["tickers"]:
            m[t] = name
    return m


def category_of(ticker: str, config: dict) -> str | None:
    return _ticker_map(config).get(ticker)


def stop_loss_pct(ticker: str, config: dict) -> float | None:
    cat = category_of(ticker, config)
    if cat is None:
        return None
    return config["categories"][cat].get("stop_loss_pct")


def stop_price(ticker: str, entry_price: float, side_qty: float, config: dict) -> float | None:
    """Stop trigger price for a position. Long stops below entry, short above."""
    pct = stop_loss_pct(ticker, config)
    if pct is None or entry_price <= 0:
        return None
    return entry_price * (1 - pct) if side_qty > 0 else entry_price * (1 + pct)
