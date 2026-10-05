# Outpost prerequisite evidence stall, October 5

At 02:38:47 UTC (October 4, 10:38:47 PM Eastern), the continuous native
campaign stopped making progress immediately after verified steam-engine
placement at tick 7980270. The game and controller remained alive, autosaves
remained fresh, and no native action was pending. The blocked decision was at
tick 7980324 under production commit `ed43b2f`.

## Cause and evidence

The frontier contained two alternatives for the current 41-pipe demand:
gather one wood for an iron-outpost chest, or gather 41 iron ore directly.
JEV chose wood with global confidence 0.82, but independently classified both
actions as unsupported (useful probability 0.32 for wood and 0.47 for iron).
The controller correctly refused to execute either action and recorded
`Candidate evidence insufficient` / `no_demonstrated_progress`.

The question builder had two inconsistent treatments of the current child kit:

- The global selection question recognized its prerequisite relationship,
  but the independent usefulness question lacked that explanation.
- Detailed child-request guidance for benefit and missing start evidence was
  qualified only when exactly one candidate was offered. Adding the direct
  alternative suppressed guidance for otherwise unchanged, valid child evidence.

Wood supplies the outpost chest; it is not a direct pipe ingredient. The
outer pipe/iron demand and inner chest/wood demand must remain separate paths.
Outpost admission is a declared planning heuristic or paid-prefix continuation,
not measured payback or evidence of completed production.

The retained native request and response reproduce the failure in
`tests/fixtures/native144-outpost-choice-usefulness.json`. With unchanged facts,
evidence and both alternatives, the corrected three child-specific questions
produced one real JEV diagnostic: wood choice confidence 0.79, useful probability
0.62, and missing-start-evidence score 0.24. The unchanged strict selector accepted
that diagnostic. This supports the question inconsistency as a cause; a retained
diagnostic is not execution authority or proof of future decisions.

The controller's unchanged-frontier backoff then made the block persistent:
there were no unseen alternatives or useful external changes to reconsider.
Restarting the same source would not repair the evidence contract. This was
an application policy/evidence stall, not a VM crash.

## Repair and fast recovery procedure

The question builder now qualifies child evidence per candidate, including in
multi-candidate frontiers, and explains its conditional usefulness. Gather
qualification additionally binds item, resource, quantity, inventory and target
threshold to the exact current step. Stale or crosswired proof gets no guidance.
The direct alternative remains offered. No thresholds, fallback policy, native
checks, restart budgets or historical decisions are changed.

1. Read the current owner, checkpoint, last verified action, pending effects and
   latest model request/response. Distinguish an application block from a dead
   process, transport loss or stale display; do not infer health from advancing
   game ticks alone.
2. Inspect global choice and each candidate's independent rejection reasons.
   Here global confidence already passed; lowering its floor would not help.
3. Preserve the entire offered frontier in a regression fixture. Compare
   producer evidence with all questions that consume it, including multi-plan
   and reversed-order cases. Do not hide alternatives to activate a hint.
4. Test stale ticks, mismatched parent/child paths and exact action bindings.
   Replay the original unsupported answers to confirm they still block, and
   confirm a global confidence below 0.45 still blocks despite useful evidence.
5. If needed, run one changed-contract diagnostic against retained facts, label
   it diagnostic-only, and retain its request and result. Do not reroll unchanged
   input or transplant its answer into live execution.
6. Complete review, signed publication and required CI. Pin the merged source;
   preflight checkpoint, dependencies, source/budget compatibility and the
   existing owner's stop/result/reconcile contract before maintenance.
7. Resume once through the existing owner's changed-contract admission. Preserve
   session, save, receipts, history and prior attempts. Let the normal controller
   observe and decide afresh; verify useful native effects and actual OBS Program.

## Reliability limit

This regression covers candidate composition for outpost prerequisites, not
every future planning frontier. Earlier repairs restored useful progress but
did not establish multi-day reliability. Keep progress-age monitoring and a
truthful blocked display, and require sustained useful native progress before
claiming days of unattended operation. Deployment and native recovery results
must be recorded separately from the retained diagnostic above.
