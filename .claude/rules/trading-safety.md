# trading-safety.md — detailed rules (imported by CLAUDE.md)

## The change pipeline (the only legal path to altered behaviour)
```
observation → ChangeProposal (PENDING_VALIDATION)
            → walk-forward gate (VALIDATING)
                ├─ fail → VALIDATION_FAILED (dead end, kept as data)
                └─ pass → AWAITING_APPROVAL  (evidence sent to operator)
            → operator approves? ── no → REJECTED
                                   └ yes → deploy new version, DEPLOYED_TRIAL
            → 2-week shadow A/B trial (previous version runs logged-only)
            → trial report to operator
                ├─ keep   → ADOPTED  (candidate stays live)
                └─ cancel → REVERTED (live pointer rolled back to incumbent)
```
No step may be skipped. The agent runs validation and prepares reports; the
HUMAN performs approve / keep / cancel. The agent never does those.

## Evidence standards (refuse to promote without these)
- A claim needs hundreds of trades, not days, behind it.
- Improvement must hold OUT-OF-SAMPLE on a majority of walk-forward folds.
- A candidate must beat the incumbent out-of-sample, not in-sample.
- The 2-week trial is monitoring/confirmation, NOT proof — it checks that live
  matches the backtest (slippage, fills, breakage) and wins the same-tape A/B.
  Never treat 2 weeks of P&L as evidence the edge is real; that is the gate's job.

## Real money
- Going live is a DELIBERATE human action via the go-live checklist. The agent
  never flips `allow_real_money`, never targets a live endpoint, and never lets
  real-money trading happen as a side effect. When the human goes live, re-examine
  every risk limit — paper-sized caps are usually wrong for real capital.
- After go-live, trial new changes in shadow or on a tiny capital slice first; a
  bad change in a live trial loses real money for the whole window.

## Things the agent must never do on its own
- Approve, adopt, or revert a change (those are the human's calls).
- Loosen `core/guardrails.py` limits or disable the kill switch.
- Lower modelled costs in `fills.py` to rescue a backtest.
- Apply any proposal directly to the live config, bypassing the pipeline.
