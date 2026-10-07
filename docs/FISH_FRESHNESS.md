# Fish movement and unrelated production decisions

On October 7 the V41 controller stopped useful work after feeding science to
the lab, researching concrete from 24% to 32%, and refueling a boiler. Its
process stayed alive. Repeated strict two-stage decisions settled as
`stale_evidence`, including attempts invalidated before any provider call.

The retained native observations at ticks 18185798 and 18185923 differ after
the existing semantic normalization only in a nearby fish's coordinates:
`(27.109375, 44.75)` became `(26.1328125, 44.81640625)`. At tick 18185552 that
fish was outside the bounded observation. The prepared decision offered boiler
fuel and copper gathering, neither of which depended on the fish. The
whole-world freshness digest nevertheless invalidated it. These exact accepted
observations and the saved decision are in `native-v41-fish-churn.json`.

The earlier boiler-fluid repair in PRs #499/#500 was present and working. It
covered numeric boiler fluid quantities; it did not cover moving fish. PR #68
introduced launch-readiness observations, including currently reachable fish.
This identifies a separate freshness defect, not a VM outage or proof that the
controller's other JEV confidence holds are solved.

## Scope of the repair

New strict two-stage decisions choose a versioned projection from the complete
candidate frontier. When no step performs launch work or verifies a launch
effect, `boiler-fluid-presence-non-launch-fish-v2` omits only the reachable-fish
encounter from both model facts and native freshness checks. It also enters the
durable input/state fingerprints. Thus movement, appearance, or disappearance
of an unrelated fish cannot invalidate these actions or grant another model
attempt after rejection.

Launch actions retain `boiler-fluid-presence-v1` and the exact fish observation.
A saved record cannot attach the omission contract to a launch action. Existing
v1 and unmarked decisions retain their original semantics. Raw observations,
native action guards, current inventory, actor/force/surface identities, launch
faults, paid cargo, pending attempts, and receipts remain available and exact.
Malformed launch observations fail closed before omission. The JEV confidence
floor, eligibility gates, strict model choice, and pending-call durability rules
are unchanged.

## Efficient diagnosis and recovery

1. Check the actual owner/child process identities and latest useful native
   receipt. An advancing game tick or healthy service alone is not progress.
2. Read the saved two-stage phase/outcome and paired accepted native
   observations around that decision. For `stale_evidence`, compare the exact
   versioned semantic digests and report the differing field paths. Current
   observations taken after the fish swims away can be stable; they do not
   explain the earlier invalidation.
3. Check merged history and deployed source before repeating an earlier fix.
   Capture a regression fixture, keep legacy decisions readable, and test both
   launch and ordinary production through the actual controller/checkpoint
   path. Include strict rejection and ambiguous provider restart cases.
4. Deploy through the existing signed owner at a reconciled, quiescent boundary.
   Preserve the session, receipts, failure budgets, and complete archived and
   active attempt history. Do not reroll, edit the checkpoint, or restart the VM.
5. Verify fresh native useful work and the actual OBS Program output. Retain
   the failed acceptance interval. A code deployment is an intervention, so a
   subsequent 24-hour interval needs its own baseline and complete evidence.

Captured replay proves the specific hash failure and correction. Synthetic
controller tests prove the stated durability boundaries. Neither constitutes
24 hours of autonomous native gameplay; issue #495 remains open until that
separate production requirement is verified.
