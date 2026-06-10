# Documentation

A news-sentiment trading bot for Alpaca, built around one non-negotiable idea and
a few rules that follow from it.

**The idea:** the strategy that trades live is the *exact same code* that gets
validated offline, and the live strategy never tunes itself. Everything in the
design exists to keep that true and to keep a human in control of every change.

## The rules that follow

1. **Shared core.** Signal and decision logic are imported unchanged by both the
   live loop and the backtest. They are never forked. A live-vs-backtest
   disagreement is therefore always a data bug, never a logic mismatch.
2. **The bot does not learn in production.** It runs a frozen, versioned spec and
   logs every decision. "Learning" happens offline and is gated.
3. **Hard limits are code, not prompts.** Risk guardrails run in the execution
   path and cannot be bypassed by the strategy or by an LLM.
4. **Changes pass gates in order:** walk-forward validation → operator approval →
   a shadow A/B trial → an operator keep/revert decision. No step is skippable.
5. **Secrets stay out of the model.** Code references only environment variable
   names, never secret values.

## Map

| Doc | What it covers |
|-----|----------------|
| [architecture.md](architecture.md) | Components, the two loops, data flow, the shared-core wall |
| [strategy.md](strategy.md) | The v1 sentiment strategy, signal pipeline, params, limitations |
| [risk.md](risk.md) | Hard guardrails, evaluation order, per-category stops, kill switch |
| [change-pipeline.md](change-pipeline.md) | Proposal lifecycle, state machine, validation, trial |
| [operations.md](operations.md) | Hosting, scheduling, Telegram, cost |
| [configuration.md](configuration.md) | Every knob in `config/strategy.yaml` |
| [agent.md](agent.md) | How Claude Code, `CLAUDE.md`, rules, and skills operate the repo |

## Status

This is a working skeleton on **paper trading**. Several integration points are
deliberately stubbed and marked in code; see the "Wiring points" section of
[operations.md](operations.md) for the list and what each needs before the bot
can place its first paper trade.
