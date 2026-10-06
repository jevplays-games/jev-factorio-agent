# Tracked input-route kit evidence recovery

The V37r2 campaign made verified pickups, furnace transfers, gear/belt crafts,
and raw-material gathers after the buffer repair, then settled at strict JEV
rejection again. Its accepted observation at tick 17589229 proposes one gear
costing two iron plates for one burner inserter in the current iron input-route
kit. The outer demand is 20 logistic science packs.

Two composition mismatches stripped this purpose before JEV saw the action:

1. The controller rejected exhausted capital proposals and recompiled ordinary
   work with `_economic_acquiring=True`. Evidence validation recompiled only the
   default planner, which proposed capital again and could not find that action.
2. BackgroundWorkLoop converted the ordinary handcraft into a receipt-tracked
   job. Evidence validation required literal equality with the untracked step.

The resulting request had neither a local target nor the kit/recipe dependency.
Strict rejection therefore did not demonstrate that the correctly described
action was unhelpful. Existing kit tests used an untracked controller and an
earlier gather observation, so they did not exercise these composed conditions.

Validation now reproduces both bounded planner modes and accepts the exact
existing tracking conversion: one deterministic item craft, an unchanged recipe,
batch count, item, target, costs, timeout and verification identity, plus a valid
32-character lowercase receipt. It mints no receipt and changes no candidate.
The same conversion check is shared with existing utility-purpose validation.
Kit ownership, current observation identity, parent path and recompiled material
annotations must still match. This does not establish completed route flow or
completed science production, and does not grant dispatch permission.

For the next incident:

1. Capture the accepted observation, checkpoint and actual model request before
   recovery. Classify a stopped process separately from a settled JEV rejection.
2. Replay the composed controller with a no-model client. Compare its candidate
   with the ordinary planner and the final `candidate_evidence` request fields.
3. Reproduce the missing evidence before editing; test the full transformation,
   including malformed receipts and changed costs, identity and parent demand.
4. Run focused and Linux regression tests. Qualify the signed changed source
   against the retained checkpoint with the provider and actor disabled.
5. Use the existing single-writer owner handoff only at a settled boundary.
   Preserve pending work, receipts, attempts, exhausted capital keys and history.
6. Require a fresh JEV choice and native receipt after deployment. Verify the
   actual OBS Program and monitor. Record any interrupted acceptance window;
   a short recovery is not evidence of 24 hours without intervention.

Regression: `tests/test_tracked_route_kit_purpose.py` replays the accepted native
capture through BackgroundWorkLoop, input routes, output buffers and outposts.
It runs offline and cannot call JEV or dispatch gameplay. Related work: issues
[#495](https://github.com/jevplays-games/jev-factorio-agent/issues/495),
PRs [#507](https://github.com/jevplays-games/jev-factorio-agent/pull/507) and
[#508](https://github.com/jevplays-games/jev-factorio-agent/pull/508).
