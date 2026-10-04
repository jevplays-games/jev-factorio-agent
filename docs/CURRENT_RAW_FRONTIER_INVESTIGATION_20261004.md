# Current raw-material alternatives beside a ready handcraft

Combined model admission passed; native deployment verification is separate.

A read-only observation of the continuous campaign at tick 6978504 reproduced
the blocked furnace frontier. Its current one-lab material bill still required
five copper ore after crediting inventory and paid machine production. Both
ReadyWorkPlanner and OutputBufferPlanner returned only the ready handcraft.
The speculative worker also suppressed the five-ore trip because it was below
the optional ten-unit collection batch. Other probes either returned the same
furnace craft or an ore insert that was not allowed with zero carried copper ore.

This draft distinguishes an immediate target's raw deficit from an optional
horizon trip. Beside an unstarted, paid handcraft, it offers bounded direct
gathering for the current target's net material bill. It retains the craft as a
choice and does not spend its ingredients, add a new production treatment, bypass
JEV, or change an execution/verification gate. Placement, service and pending
work stay serial. Native observations must be coherent and current; the path
uses selected, enabled recipes active in the current bill. Optional outpost
construction and exploration are not introduced as alternatives.

The diagnostic generated a normal copper-ore gather candidate with path
lab -> electronic circuit -> copper cable -> copper plate -> copper ore.
Both it and the furnace craft fit the existing 48,000-byte request limit
(37,113 bytes); the smaller default of the standalone question builder is not
the live controller's configured selection budget.

One real-model diagnostic using `jev-1.13.0` selected the gathering candidate
with probability 0.51, versus 0.46 for crafting and 0.03 for observe. It judged
gathering useful with probability 0.66 and low observation need, but reported
only 0.26 confidence in the candidate choice. The unchanged selection floor is
0.45. Replaying the retained answer through the exact `select_plan` function,
with identical context and questions, returned `low choice confidence` and no
selected plan. The furnace's independent usefulness still failed.

This isolates three distinct liveness constraints: a rejected primary craft,
hidden current-input alternatives, and insufficient confidence in the overall
choice even when the alternative's individual usefulness passes. The experiment
does not establish why the model assigns those probabilities or justify forcing
its answer. No action was dispatched, no rejection history cleared and no
confidence threshold lowered.

Combining this planner change with the construction evidence from PR341 produced
a different, fully retained request at the same observed frontier. Both
candidates fit in 40,958 bytes. JEV selected the craft with 0.89 choice confidence
and 0.74 useful probability; exact replay through the unchanged selector accepted
it. The gathering alternative also passed individual usefulness. No confidence
gate or model was changed, and no previous failure was discarded. This admits
the combined source for a reviewed recovery attempt; it does not establish a
native action or multi-day reliability by itself.

Validation includes focused Linux tests across current raw alternatives, ready work,
output-buffer integration, input-route integration and raw-machine evidence.
The new cases cover small deficits, input preservation, paid-stock accounting,
current-target scope, candidate budget, stale/missing start facts and serial
placement. Windows integration attempts hit the existing checkpoint filesystem
identity failure before the upstream Windows identity repair was merged. The
combined integration test also confirms that offering both repaired candidates
does not bypass the overall choice-confidence gate.
