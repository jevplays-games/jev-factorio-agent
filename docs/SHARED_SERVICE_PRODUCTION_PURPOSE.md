# Preserve production purpose when identical actions are combined

The October 7 V43 campaign made useful progress for more than an hour, then held
at concrete research 56%. Factorio and the controller remained alive, but no
craft, furnace, or research counter was advancing. This interval does not meet
the 24-hour useful-operation requirement.

The retained decision at tick `18921559` selected iron-furnace refuelling with
choice confidence **0.33**, below the unchanged **0.45** floor. Both refuelling
and a lookahead iron gather passed their candidate gates. The remaining unseen
copper transfer was subsequently rejected. These recorded outcomes are retained;
they are not permission to dispatch, replay a request, or lower a threshold.

## Reproduced evidence loss

The ordinary production planner and an optional, unstarted input-route kit both
needed exactly the same action: insert five carried coal into the owned iron
furnace. Their action ID, costs, parameters, and verification semantics matched.
The existing duplicate-action rule kept the kit's target of 25 transport belts
and discarded the ordinary target of 20 automation science packs. JEV therefore
received the construction purpose instead of the immediate production purpose
for this shared prerequisite.

The repair retains the ordinary candidate only when the physical action is
identical, the prior purpose belongs to a proposed input-route kit, and the
alternative is immediate work for that kit's exact parent production target.
It preserves the existing action ID and candidate count. Different quantities
still receive distinct identities; a building route retains its serial owner.
Unrelated targets, lookahead work, and another kit cannot replace the purpose.

This differs from [the earlier serial-service repair](INPUT_ROUTE_SERIAL_SERVICE.md):
that defect discarded an entire required service frontier. Here the action was
present, but combining duplicate actions discarded its ordinary purpose.

## Efficient diagnosis and verification

1. Verify native work counters and pending receipts before calling a foreground
   hold a stopped campaign. Retain the original observation interval.
2. Read the original settled decision from `controller.json.safety/two-stage-decisions`
   as well as the latest checkpoint. The last rejected alternative can otherwise
   hide the original low-confidence choice.
3. Pair the accepted native observation with the decision's exact native digest.
   Replay planning offline with the existing ownership and exhausted failure
   history. Compare ordinary production against the composed optional-route path.
4. Test the corrected purpose, unchanged physical action, candidate budgets,
   ownership, and confidence rejection before a governed source handoff. Retain
   the failed interval and verify fresh useful native receipts after deployment.

`tests/fixtures/native-v43-shared-service-purpose.json` retains the original
accepted observation, matching decision, remaining rejected alternative, and
planner memory. Regression tests call neither JEV nor the live game. They
establish the evidence correction; they do not predict JEV's next answer.

Strict JEV choice remains authoritative. Better production evidence does not
guarantee a confident selection or establish uninterrupted operation. No
scheduler fallback, confidence reduction, history reset, or same-request reroll
is introduced. Keep issue #495 open until the full native acceptance interval
actually succeeds.
