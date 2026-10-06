# Registered furnace identity stop

At 04:45 UTC on October 6, the V28 controller exited normally at a fail-closed
planner boundary: `Furnace fuel service requires current owned source identity`.
The owner stayed held. Factorio, rotating autosaves and the broadcast remained
running; the progress monitor correctly reported the missing controller.

The accepted observation at tick 13591566 contains registered
`recipe:steel-plate`, stone furnace unit 2588, with empty fuel and an empty
selected recipe. Native furnace recipes are selected automatically from input.
The production-site survey contains only iron and copper ore furnaces, by design.
Ready-work fuel servicing incorrectly required every recipe furnace to appear
in that survey. Compiling a secondary recipe branch therefore aborted the
entire candidate frontier, including the current red-science work.

The repair uses surveyed ownership for ore furnaces and current native role
registration plus catalog capability for other burner smelting furnaces. An
idle furnace can have an empty recipe; a conflicting selected recipe, missing
unit, unsupported category or unresearched recipe does not qualify. Existing
freshness checks and grouped fuel quantities remain in place. Current identity
is not evidence of historical payment or permission to execute a transfer.

The same identity rule supplies grouped fuel-transfer evidence. A partial
transfer still lacks the stronger full-deficit proof, as before. Strict JEV
choice, native action checks, reservations and failure budgets are unchanged.

Native resume preflight exposed the same scope assumption in a second place:
the output-buffer module retains unspent proposals for iron, copper **and
steel**, while connector reattachment allowed only two ore-furnace roles.
Reattachment now permits the existing steel proposal and three output offers,
while ore-site ownership remains restricted to iron and copper. Every proposal
must still reference its original registered entity, position and unit, contain
no paid parts or unknown fields, and remain unchanged across both readbacks.
No survey, ownership registry or proposal is cleared to make resume succeed.

For efficient diagnosis and recovery:

1. Read the owner status, child liveness, terminal result and exact checkpoint.
   Preserve pending actions, native receipts and the full recovery history.
   Distinguish controller exit from a stalled decision or a failed game server.
2. Capture the last accepted observation and its validation. Reproduce candidate
   compilation with that snapshot and a compatible native catalog. Here all
   four ready-work planner variants failed with the same ownership exception.
   The catalog was captured later at tick 13728431; the regression verifies
   that researched technologies match the earlier observation.
3. Check the actual scope of the registry being consulted. Native role
   registration in `lua/factory.lua` and the ore-only roles in
   `production_sites.py` establish why this particular requirement was wrong.
   Do not populate a fake survey entry or edit the checkpoint to running.
4. Test the captured frontier, composed model boundary, ordinary fuel bounds,
   negative identity cases and explicit source recovery. This exact error is
   eligible only for a reviewed changed-contract reevaluation. It is not an
   automatic polling/retry reason; unchanged or previously consumed contracts
   remain ineligible.
5. Merge after validation, then use [the recovery runbook](STATUS_RECOVERY.md)
   to stage a signed reviewed source and owner. Reconcile the terminal
   checkpoint with the native session before consuming the one-use admission.
   Stop the held owner through its service, without duplicating a controller.
6. Verify a new strict JEV decision and a useful native action. Update the
   monitor's owner/execution pins, inspect actual OBS Program and confirm fresh
   autosaves. Record the observed interval; one recovery is not a multi-day soak.

The fixture records native state. Tests that add carried coal or alter identity
are explicitly simulated scenarios. Planner inspection does not claim that JEV
has selected those candidates or that the game has executed them.
