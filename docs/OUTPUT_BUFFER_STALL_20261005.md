# Output stock hidden behind unbuilt infrastructure

After the boiler recovery, the campaign completed Automation research and
smelted 20 copper plates. At tick 9510047 the only offered action was a
five-coal gather, rejected by JEV's independent usefulness check. The controller
remained alive and the persistent monitor displayed the policy hold.

The output-buffer planner intercepted the plate demand before the normal
furnace pickup. It proposed a buffer and required both the next component and
five coal before placing even its chest. Native placement consumes only the
component; coal is needed later by the paid burner inserter. The coal request
carried no corresponding construction purpose in the model evidence. A running
process and completed coal trips therefore did not establish durable progress.

An unbuilt proposal now yields to a ready batch of existing furnace output.
The normal pickup compiler retains its parent recipe path and native receipt
checks. A partial or installed buffer still owns its existing commissioning
and no-racing rules. Placement requires the component; fuel acquisition and
transfer occur through the existing service path after the inserter exists.
No confidence threshold, independent judgment, native payment or flow gate
changes.

## Recovery proof after history rotation

The preflight also found that the 64-event history had dropped the most recent
craft completion, while its verified attempt and native receipt remained.
Keep the latest completion event and latest craft outcome within the existing
64-entry bounds. Newer crafts replace those retained witnesses; this does not
grow history, clear failures or reset provider budgets.

For an older checkpoint whose completion event already rotated away, only the
latest unique verified craft attempt can supply a binding. The native read-only
attachment returns that exact completed receipt and the current native recipe.
Reconstruct the canonical committed Step, including receipt ID, recipe, batches,
paid inputs, inventory threshold and timeout, and compare its SHA-256 with the
retained attempt. Recipe-derived output quantities, actor/session, completion
clocks, callbacks and empty queue must independently agree. The two native
snapshots must remain equal. Unknown or changed steps still require reconciliation;
no outputs are credited, event manufactured or receipt replaced.

## Fast recovery checks

1. Preserve the current owner, checkpoint and rejected request/response. Confirm
   the hold has no pending action or background job; do not reroll JEV.
2. Compare carried stock, furnace output, and output-buffer state. An unbuilt
   proposal with enough paid output for the current batch identifies this ordering
   defect. Never bypass an already installed inserter or incomplete flow proof.
3. Compile the fixed planner against a fresh coherent observation without dispatch.
   Verify the pickup retains its native start evidence. The live diagnostic at
   tick 9551202 produced the copper pickup with that evidence.
4. Before stopping the owner, test the complete CLI and native attachment from
   merged immutable source. If recent history rotated, verify the retained craft
   Step hash against the current receipt and native recipe as described above.
5. Use one changed-contract recovery through the existing owner and writer lock.
   Preserve all earlier attempts and admissions. Verify the actual pickup, further
   production/research and the monitor on OBS Program after launch.

Regression tests cover component placement without future fuel, batched pickup,
paid inserter commissioning, changed native craft inputs/outputs/baseline/recipe,
step hash and later failed attempts, and 200 ordinary events within the same
history bound. Native read-only qualification is separate from resumed gameplay
and does not prove uninterrupted multi-day operation.


## Follow-up: valid input proof suppressed by nonurgent fuel

V16 resumed with a verified 20-copper pickup at tick 9623787, then crafted a
furnace and mining drill and gathered 40 iron ore. It held again at tick 9633258
while offering an ore insert followed by a coal insert at the owned iron furnace.
The furnace contained three coal. The first input proof was present, but its
independent question qualification required urgency 3 and an observed-low-fuel
reason. The ranking code correctly assigns those only below two coal. JEV
therefore received generic start questions without the qualified service hint
and rejected the plan for missing start evidence. The existing controller later
resumed on changed native evidence and verified the two service transfers at
ticks 9644072 and 9644271 without an operator restart; the suppression defect
still needs correction to avoid waiting for that incidental change.

Validate urgency and reasons against the observed fuel range instead: one coal
has the low-fuel ranking; two through four coal have ordinary priority. The
current paid ore input remains useful without an urgent refuel. This does not
increase priority, change JEV thresholds, assume receiver capacity, or authorize
the later fuel transfer. Exact owned machine, native recipe path, carried budget,
unused receipts, session/tick and per-step native guards still apply.

For this symptom, inspect `candidate_rejections` and compare the presence of
`paid_service_input_start_evidence` with its inclusion in the independent
`needs_observation`, usefulness and benefit questions. Check both sides of the
fuel threshold; a ranking annotation must not silently suppress valid start
evidence. Preserve the rejected request and do not repeat it unchanged.

Regression coverage includes every admitted fuel level (one through four),
contradictory urgency/reason annotations, existing ownership/payment/receipt
negatives, and the recorded V16 request. Offline qualification is not a JEV
approval; production recovery still requires merged-source preflight and a
fresh independent decision.


## Follow-up: construction kit inherited the producer's output target

The campaign crafted 20 science packs and started logistic-science research,
then acquired components for an assembler intended to make later science.
At tick 9669167 its next action collected one paid iron plate from the owned
furnace. The recursive path correctly began at `assembling-machine-1`, but the
local objective still named `automation-science-pack`. The output-pickup proof
correctly rejected that mismatch, and JEV rejected unsupported progress.

While compiling an unbuilt producer's kit, temporarily focus on one machine.
Restore the caller's focus even if compilation fails. Keep the original capital
specification, stage, deadline, failure budget, native action and receipt intact.
The model sees kit progress separately from the planner's projected production
payoff; neither acquiring a plate nor completing the kit proves science output.
Installed-machine supply continues to use the producer's ordinary target.

For a construction hold, compare `local_target.item` with the first item in the
native dependency path. A machine-kit path must not pretend that the eventual
product directly consumes the kit's inputs. Validate the actual owned output,
recipe path and tick, then recompile the current capital continuation without
dispatch. A fresh native diagnostic at tick 9689426 restored the one-plate pickup
proof with the same action/receipt. Model approval and gameplay remain separate
acceptance checks.
