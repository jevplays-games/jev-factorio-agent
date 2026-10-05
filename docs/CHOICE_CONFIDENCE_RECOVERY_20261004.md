# Scheduling ambiguity at the continuous campaign frontier

At tick 7278308, the continuous campaign offered eight iron-gear batches and
gathering five copper ore for its current one-lab target. Both alternatives
passed usefulness (0.66 and 0.65), but the overall choice confidence was 0.24,
below the existing 0.45 floor. Services and autosaves remained healthy. The
controller backed off with 74 recorded attempts once native smelting stopped
changing its observations. The compatible deployment of merged `e115da6`
preserved those attempts and made no new model call.

The request already included `deterministic_ranking`, but the candidate question
asked for the best action without explaining that scheduling preference when
several alternatives were independently useful. The repair explains the
existing order as a tie-breaker for comparably supported actions. It explicitly
allows contrary current facts to override the order and does not make rank
evidence of eligibility or success. Both actions and observe remain available;
the selector, thresholds, independent judgments and native checks are unchanged.

The explanation requires a complete, current ranking matching the existing
compiler function, valid known costs and the current selection schema. Missing,
stale, malformed or inconsistent ranking data adds no preference. Single-action
requests receive no comparative explanation.

One distinct real-model diagnostic retained the exact tick-7278308 context,
candidate plans and all other questions. Only the candidate scheduling
explanation changed. The 34,127-byte request remained within the existing
48,000-byte limit. JEV chose the gear craft with 0.62 confidence and 0.75 choice
probability; usefulness was 0.73 for crafting and 0.67 for gathering. Exact
retained-answer replay through the unchanged selector accepted the craft.
This supports the scheduling-ambiguity hypothesis; it is not a native receipt,
a repeated sample of the old request, or proof of multi-day reliability.

Regression tests preserve both choices and all independent vetoes, including
low overall confidence, model abstention, unsupported progress, missing start
evidence and uncertain disruption. Production recovery must use reviewed signed
source and a fresh exact-checkpoint admission through the existing owner. It
must retain the previous failed requests and verify native progress afterward.

## Production result and missing overlap evidence

PR #357 was merged as `62b4436` and deployed through the existing owner with
one exact-checkpoint admission. Its real production request at tick 7484808
included the scheduling explanation, but JEV reported 0.44 confidence (craft
probability 0.63). The unchanged 0.45 gate rejected it. No native action followed.
The earlier 0.62 diagnostic was therefore insufficient evidence of recovery.
The rejected attempt and consumed admission remain part of the campaign history.

The request also omitted an explicit comparison of action ordering. A
receipt-tracked background craft can start before an independent gather; the
native queue may then run during that later gathering. Gathering first leaves
the craft unstarted throughout the gather. The new `independent_gather_overlap`
evidence explains this conditional opportunity only for two current immediate
prerequisites of the same local target, with current craft-start witnesses,
available inputs, a receipt identity, an observed gather target, no gather costs,
and no conflict with the craft's locked outputs. Unknown travel, urgency, stale
evidence, different targets or missing readiness suppress the comparison.
Inputs are explicitly available, not yet paid. Native admission, a fresh
observation, a later JEV decision and the output receipt remain necessary.
No elapsed-time saving or completed production is asserted. Both alternatives,
observe, ranking and every judgment gate remain unchanged. If the request budget
removes the gather, it also removes the comparison without mutating input state.

A single changed-evidence diagnostic reused the exact tick-7484808 request,
including all questions and both candidates; its only addition was this evidence
field. The request was 35,326 bytes. JEV selected crafting with 0.71 confidence,
0.81 choice probability and 0.86 usefulness probability. Exact retained-answer
replay passed the unchanged selector. This is still diagnostic evidence, not a
live action or multi-day reliability result. The focused suite passed 344 tests.

## Native recovery and the following material-bill frontier

PR #359 was deployed as `38082d1` through one consumed maintenance admission.
At tick 7574005, the actual JEV request selected the gear craft with 0.53
confidence. The native job paid 16 iron plates and its completion receipt
verified eight gears at tick 7574399. A separate pickup of ten iron plates was
verified at tick 7574351. The original choice hold was therefore cleared with
native progress, not merely a passing diagnostic.

The next frontier stopped again: two complete current-bill crafts and copper
gathering received overall confidence 0.33. The remaining one-plate pickup was
offered in a second bounded batch and rejected for missing start evidence and
unsupported progress. Both rejected requests were retained. This is a new
decision hold after useful progress; it does not qualify sustained operation.

The original overlap comparison required exactly two options and an immediate
recursive craft dependency. It consequently omitted the same scheduling
relationship for the next frontier's complete current-bill crafts. The extension
compares each supported craft with one independent gather, retaining all other
options. A lookahead craft qualifies only with the existing same-tick current
material-bill proof, matching product and target, observed carried inventory,
positive unfilled demand and sufficient native expected output. Discretionary
stockpiling does not qualify. Active-research horizons are excluded because their
shared bill can contain science demand beyond the gather's local target.
Each pair still independently requires all craft,
gather and output-lock witnesses. Compiler scopes, ranking and all gates stay
unchanged; the evidence neither forces crafting nor asserts simultaneous jobs.

One diagnostic added only these comparisons to the exact three-candidate request
at tick 7574399, retaining every question. JEV selected copper gathering with
0.66 confidence and 0.75 choice probability; exact retained-answer replay passed
the unchanged selector. Its 45,075 bytes fit the unchanged 48,000-byte budget.
The focused suite passed 357 tests. Production deployment and subsequent native
progress remain separate acceptance steps for this extension.

## Fast recurrence triage

1. Read the existing owner, child PID, checkpoint reason, latest useful tick and
   service state together. A live process and advancing game tick do not prove
   useful controller progress. Preserve the campaign identity and ownership lock.
2. Capture the last actual model request and answer, including offered candidates,
   request-budget pruning and each rejected gate. Compare those with the deployed
   source and current native observation before attributing a stop to infrastructure.
3. Keep the OBS Program status accurate. Preview selection is insufficient;
   read back Program and stream state and inspect its screenshot.
4. For a semantic block, repair missing, independently checked evidence. Validate
   the changed request against the retained one and run one diagnostic per distinct
   repair. Retain a rejected answer; do not repeat unchanged requests until one
   happens to pass, lower confidence floors or discard failure history.
5. Test unchanged vetoes and evidence rejection cases, publish signed source,
   complete CI and merge, then deploy the merged tree through the existing owner.
   A consumed maintenance admission cannot be replayed. Preserve the exact stopped
   checkpoint, source lineage, receipts and all unresolved ownership.
6. Verify fresh native postconditions and subsequent useful decisions. If the live
   request rejects the repair, record that outcome and restore the blocked display.
   A passing offline diagnostic, service restart or successful merge is not recovery.
