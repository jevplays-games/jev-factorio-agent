# Preserve a qualified manual outpost alternative's objective

V39 completed twenty logistic science packs and inserted them into the lab,
advancing Concrete research from 8% to 16%. The next production batch stopped
after crafting a wooden chest for a proposed copper mining outpost. This was a
strict decision hold, not a controller crash or VM outage.

The current frontier contained the proposed outpost and a manual copper gather.
The native/catalog validator qualified the gather as an immediate input to the
twenty-automation-science target. Its compiler provenance remained `lookahead`.
The objective projector accepted only a power-specific promotion, so it omitted
the gather from `local_objective.candidate_targets` despite retaining its valid
parent-demand evidence. The request then described that candidate's target as
unavailable. JEV rejected the frontier; repeated unchanged observations correctly
did not grant a new decision attempt.

PRs #252 and #257 established the direct alternative and current parent binding.
PRs #473 and #499 repaired other candidate objective cases. This fixes their
composition for native-qualified outpost alternatives; it does not lower any
JEV gate or claim an outpost's future payback.

## Repair

Objective binding version 4 recognizes the existing typed, same-tick parent
demand proof and verifies the retained parent document against the marker's
purpose. The planner's original scope and executable steps stay intact.

During two-stage choice, an assessed parent that JEV rejected can remain in
`local_objective_parent_plans` as demand evidence. It is absent from
`candidate_plans` and the choice criteria. This prevents the approved manual
alternative losing its purpose when the rejected investment is filtered out.
The full evidence counts toward the request byte limit. An unassessed parent
removed by initial candidate or byte trimming cannot qualify the alternative.
Saved version 2/3 decisions reconstruct their original requests.

## Efficient diagnosis and recovery

1. Capture the accepted native observation, planner memory, complete candidate
   evidence, and actual model request. Check both evidence qualification and
   `local_objective`; valid evidence alone does not prove the final packet is
   coherent. Distinguish the first rejected frontier from later fallback batches.
2. Replay the composed planner and request builder offline. The captured fixture
   `native-v39-outpost-objective.json` records the accepted snapshot and relevant
   planner state without provider credentials or gameplay calls.
3. Test assessment and filtered choice, stale/malformed evidence, changed parent
   purpose, initial request trimming, byte bounds, and legacy durable requests.
4. Deploy using the existing single-owner changed-source handoff. Preserve the
   exact stopped checkpoint, session, failures, attempts, receipts, and native
   ownership. Resume only a settled hold with no unresolved action or job.
5. Confirm a new confident strict JEV choice and fresh native useful progress;
   repin the progress observer while retaining its history-watermark repair.
   Keep the interrupted acceptance window as evidence and start a separately
   identified window for the new controller. Issue #495 remains open until
   an actual 24-hour run meets its acceptance criteria.

Offline tests establish packet correctness, not model agreement or multi-day
gameplay reliability. Production continuation must be verified separately.
