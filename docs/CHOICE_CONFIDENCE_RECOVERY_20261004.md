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
