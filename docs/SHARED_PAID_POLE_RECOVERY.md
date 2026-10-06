# Shared paid poles and exact connector recovery

At 00:39 UTC on October 6, the continuous campaign stopped after constructing
the engine-to-copper-cable electrical route. The completed route recorded seven
new paid poles and two external references. Those two poles, units 2575 and
2576, were exact paid cells of the existing engine-to-lab route. The controller
required every cell to be paid by the current route, so it held the returned
action even though the campaign already owned the reused poles. This is separate
from earlier archive, storage, and strict-choice incidents.

The retained native fixture contains the accepted post-dispatch observation and
checkpoint projection at tick 12715105. Route-local `owned=false`, `paid=7`, and
`external=2` remain correct and are never rewritten. Coverage now accepts an
external electricity pole only when another completed receipt directly records
payment for that exact unit at that exact position, with matching source entity,
actor, session, surface, force, kind, and fluid. References cannot attest one
another. Pipes and unknown same-force entities retain their previous checks.
Later routes may reuse a directly paid cell of an already shared route; no
recursive ownership inference is needed.

The last pole-crafting receipt must also remain qualified during attachment.
The previous completed-craft selector excluded every foreground pending action;
this exact shared-pole boundary may retain its uniquely verified earlier craft.
Its completion must precede the connector dispatch. The native recipe, original
craft-step fingerprint, paid receipt, and completion clocks still have to match.
No craft output is credited again, including output already spent on poles.

## Efficient recovery

1. Preserve the held checkpoint, pending action, original attempt, terminal
   result, writer lock, source pin, all native receipts, and recent autosave.
   A completed route with external references is not by itself proof of loss.
2. Compare every bounded native detail page against every saved route. Require
   stable summaries before and after attachment, unchanged endpoint identities,
   and no active, partial, faulted, missing, or uncheckpointed route. Check each
   external pole's direct paid anchor. Stop if any unit or position differs.
3. Review and merge the repair, then use the existing signed
   [compatible-source admission](COMPATIBLE_SOURCE_RECOVERY.md). The narrowly
   eligible boundary is an uncertain, returned, single-step shared-pole action
   with its matching original attempt and no overlapping pending work. Bind the
   exact checkpoint, source, owner, provider state, archive, and epoch lineage.
   Preserve the pending action through migration; grant no source reevaluation.
4. Resume once through the existing owner. The ordinary pending verifier must
   observe the original endpoints on the same native electrical network before
   finishing the original attempt and releasing its reservation. An ownership
   match alone cannot clear the stop. Never redispatch construction, relabel a
   pole as newly paid, or edit checkpoint status to running.
5. Verify one original-attempt completion, unchanged payment receipts and strict
   JEV settings, then fresh useful work. Inspect the actual OBS Program output
   and persistent monitor. A successful recovery is not multi-day soak evidence.

Tests replay the retained native facts, exercise the installed Lua read-only
attachment, and reject changed units, positions, source/owner identities,
unpaid anchors, incomplete routes, disconnected networks, and unrelated holds.
The compatible-source test checks that migration retains the exact pending
action and budget state. Synthetic negative controls and third-route examples
are explicitly marked; production deployment is a separate verification step.
