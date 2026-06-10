---
name: run-walkforward
description: Use to validate a PENDING_VALIDATION change proposal. Runs rolling out-of-sample walk-forward via backtest/walkforward.py, applies the promotion gate, and on success hands the proposal to the human for approval with its evidence attached. Never deploys on its own.
---

# Skill: run-walkforward

The walk-forward gate. A proposal that fails here is a dead end. A proposal that
passes here is NOT deployed — it is sent to the operator for approval.

## Steps
1. Move the proposal to VALIDATING (`ProposalBacklog.transition`).
2. Build a candidate config via `ops/deploy.Deployer.make_candidate` (copy live
   config, apply the one proposed param, assign a new version). Do not touch the
   live pointer.
3. Run `backtest/walkforward.run_walk_forward` for both candidate and incumbent
   over history with pessimistic `fills.CostModel` (90/30-day folds by default).
4. Call `backtest/walkforward.promote(candidate_oos, incumbent_oos)`.
   - Fail → transition VALIDATION_FAILED with the reason. Keep it.
   - Pass → transition AWAITING_APPROVAL and call the approval channel
     (`ops/approval`) with the out-of-sample evidence attached.
5. STOP. The human approves or rejects. You do not deploy.

## Hard rules
- Never relax `promote()` thresholds to make a favourite idea pass.
- Out-of-sample or it does not count.
- One proposal at a time, so its effect is attributable.
- Always attach evidence to the approval request — never send a bare hypothesis.
