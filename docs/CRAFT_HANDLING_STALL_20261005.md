# Craft handling estimate and strict-choice stall

After the outpost-evidence repair, the native campaign produced a chest and
27 new pipes. At tick 9089547 on October 5, approximately 07:47:40 UTC, it held
again. Both offered actions passed independent usefulness: craft three pipes
from carried iron plates, or gather eight more iron ore. JEV chose the craft,
but global confidence was 0.40, below the unchanged strict floor of 0.45.
No native effect was pending and no machine was producing new evidence.

The scheduling evidence counted eight handled units for the gather but zero
for the ready three-pipe craft. The shared cost-per-handled-unit heuristic thus
compared 1294/8 estimated actor ticks for gathering with 300/1 for crafting.
The questions suggested following that order while the model preferred the
craft. This is a distinct accounting defect from the earlier missing outpost
prerequisite explanation; it does not prove that every low-confidence decision
comes from accounting.

Ready synchronous and receipt-tracked crafts now contribute their deterministic
native recipe's expected product quantity to the handling estimate. The craft
must have matching paid costs, carried inputs, an unlocked handcraftable recipe,
a bound player and empty crafting queue; asynchronous work also requires its
native job protocol. Invalid or unready crafts receive no forecast credit.
Each credited forecast is explicitly marked as uncompleted output. It cannot
be spent, does not enter asynchronous delivered output, and does not authorize
selection or execution. The ranking formula and every JEV/native gate remain
unchanged; both alternatives remain available.

The retained native request/answers are a regression fixture. Replaying its
original 0.40 answer still blocks after correcting the estimate. One separate
real JEV diagnostic, using the same retained facts/questions/frontier with the
corrected estimate and resulting rank, chose the craft at confidence 0.71 and
passed the existing gates. This is diagnostic evidence, not execution authority
or proof of multi-day reliability. Production must observe and decide afresh
after a reviewed merge and a single reconciled changed-contract admission.

For future recurrence, capture the complete candidate set, actual rejection
reason, per-candidate scores and scheduling estimates before considering a
restart. A usefulness rejection and a global-confidence rejection need separate
diagnoses. Monitor the time since a verified useful action alongside heartbeat,
pending-action and blocked-state evidence; an advancing world tick is not useful
factory progress. Never clear a retry budget or reroll unchanged input to make
a confidence hold disappear.
