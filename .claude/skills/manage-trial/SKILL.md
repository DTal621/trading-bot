---
name: manage-trial
description: Use after the operator approves a validated change. Deploys the new version, runs a 2-week shadow A/B trial (previous version logged-only), then prepares the trial report for the operator to keep or cancel. The agent prepares and reports; the human decides.
---

# Skill: manage-trial

Runs the deploy → shadow A/B → report lifecycle. The agent never decides keep or
cancel — it deploys, monitors, and reports; the operator decides.

## On approval (ops/change_pipeline.approve)
1. Build the approved candidate version and `Deployer.deploy()` it — it is now
   live. `deploy()` returns the previous version; that becomes the SHADOW.
2. Start the trial window (`change_workflow.trial_days`, default 14) and
   transition the proposal to DEPLOYED_TRIAL with both versions + window stored.
3. Run the live loop with `shadow_config` = the incumbent version. The shadow
   evaluates the same signals and logs `shadow_decision`s but submits NO orders.

## When the window closes (ops/change_pipeline.finalize)
4. Build the A/B trial report (`ops/trial.build_trial_report`): candidate (traded)
   vs incumbent (shadow) over the SAME tape, plus a live-vs-backtest sanity note.
5. Send it to the operator. Wait for 'keep' or 'cancel'.
   - keep   → ADOPTED. Candidate stays live.
   - cancel → `Deployer.revert_to(incumbent)` and REVERTED. Clean rollback,
     because every version is kept and every decision is version-stamped.

## Hard rules
- Never auto-keep or auto-cancel; that is the operator's decision.
- Read the trial as monitoring, not proof. A two-week P&L sign is weak evidence;
  the divergence between live and backtest is the signal that matters.
- After go-live (real money), prefer a tiny-capital or shadow-only trial first.
