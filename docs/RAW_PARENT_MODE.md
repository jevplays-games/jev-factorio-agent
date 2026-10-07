# Reconstruct current raw demand in the controller's actual planner mode

V40 resumed after the outpost-objective repair, gathered and smelted materials,
delivered twenty automation science packs, and prepared sixteen transport belts.
It then reached a genuine strict choice hold: both the construction-fuel gather
and direct iron gather passed assessment, but JEV's choice confidence was 0.38
against the retained 0.45 floor. The fallback batch was rejected. This is not a
VM or process failure and must not be retried unchanged.

The captured frontier exposes a reproducible evidence defect. After exhausted
capital proposals, the controller compiles ordinary work with
_economic_acquiring=True. The kit-purpose validator already reconstructs both
bounded modes (PR #510), but _candidate_local_raw_demand used only the default
mode. That default produces different capital candidates and drops the exact
current twenty-iron-ore alternative, so its parent-demand witness disappears.

Reconstruct both existing modes while requiring the same candidate ID, complete
steps, local objective, recipe path, work intent, shortages and batch accounting.
All native/current-parent checks remain in place. This does not revive an
exhausted capital option, rank/select an action, reset attempts, or lower a gate.
It restores the existing independently qualified raw-demand explanation.

The capture and offline tests retain the actual exhausted failures, verify
packet bounds and both local targets, reject mutated plans and stale/missing
native bindings, and confirm that the recorded 0.38 answer still fails.
Improved model confidence and long-running gameplay require separate live
verification; the evidence defect does not establish the model's internal
reason for uncertainty. Shared-parent comparisons from PRs #305/#307 cover
different transfer/pickup pairs and are not broadened here.

Recovery uses the existing signed single-owner changed-source handoff at a
settled, empty action boundary. Preserve the campaign, receipts, failure budgets
and interrupted acceptance evidence. Issue #495 remains open until actual
24-hour useful operation is established.
