# An optional input route hid existing output service

The V42 native campaign stopped making useful progress after a coal gather at
tick `18514696`. At the next decision, `18514770`, JEV rejected every candidate:
an unstarted input-route build, a lookahead copper transfer, and a lookahead iron
gather. There was no native action or background craft left running. This was a
genuine decision hold, separate from the fish freshness defect in
[the previous incident](FISH_FRESHNESS.md).

## Cause

The accepted observation contained 15 carried coal, an existing paid output arm
with one coal, and current demand for 20 logistics science packs. The normal
production planner computed its existing four-coal output-arm service, retaining
the paid owner and the science-to-inserter-to-iron recipe path.

`InputRoutePlanner.candidates()` also considered an optional, fully acquired
input-route kit. Its worker correctly deferred that unstarted route and found
the output-arm service. However, the worker marked the result as serial
infrastructure work, and the caller discarded all such results. The optional
construction proposal consequently hid the service required by the ordinary
production path. The retained assessment had no opportunity to approve that
service; the remaining speculative actions lacked its current-demand evidence.

This boundary was not covered by the earlier repairs:

- [PR #512](https://github.com/jevplays-games/jev-factorio-agent/pull/512) exposed
  ordinary production beside a ready, unpaid input route. Its captured case had
  a directly available ore transfer and no intervening serial buffer service.
- [PR #518](https://github.com/jevplays-games/jev-factorio-agent/pull/518) corrected
  the ordinary-work mode used to validate current raw demand. It could not
  validate an action that the planner had already discarded.

Both changes were present in V42. Replaying the exact accepted native observation
with the original exhausted capital counters reproduces the three rejected
offers. The new regression fixture retains that observation, its acceptance
record, planner memory, and the settled two-stage decision. Its native digest
matches the decision binding.

## Repair and limits

When deferring an optional input route exposes a serial production prerequisite,
retain the worker's complete serial frontier. Do not combine it with the
optional proposal or speculative siblings. Other unpaid-route alternatives keep
their existing bounded ordering; an already building route retains its owner.

In the captured state, the resulting offer is the existing four-coal service for
`output-arm:2547`. This is an offer to JEV, not a dispatch. Assessment, exact JEV
choice, the 0.45 confidence floor, native preconditions, receipts, and fresh
postconditions are unchanged. Existing construction/service policy and its
thresholds are unchanged. No rejection, failure count, or pending work is reset.

Hypothetical continuation checks account for the coal spent on the arm, retain
the next furnace fuel transfer, and then expose a qualified immediate iron
gather with its complete current recipe path. These offline checks establish
planner behavior, not a native receipt or a promise that JEV will approve.

## Efficient diagnosis and recovery

1. Check the native craft, research, furnace, and pending-action state before
   treating a foreground hold as a stopped factory.
2. Preserve the settled decision and its bound accepted observation. Inspect
   the actual offered plans, rejection reasons, inventory, ownership, and
   current demand; a later snapshot alone is insufficient for reproduction.
3. Replay the composed planner with its original capital failures and ownership.
   Compare the optional route's normal and deferred worker results. Here, the
   decisive evidence was a valid service discarded solely because the worker
   required serial execution.
4. Add the captured regression and continuation cases. Run the neighboring
   input-route, buffer-demand, raw-demand, and strict decision tests. Verify that
   low-confidence model choices still reject and that offline probes issue no
   model or native gameplay calls.
5. Follow [the recovery runbook](STATUS_RECOVERY.md) for signed source review,
   quiescent reconciliation, deployment under the existing owner, and fresh
   native completion evidence. Never reroll the saved rejected decision, replay
   an ambiguous admission, or clear its history to test a repair.
6. Verify the actual OBS Program and persistent monitor after recovery. Preserve
   the failed acceptance interval and start a distinct interval for an
   intervened deployment; do not combine elapsed time across repairs.

Keep [issue #495](https://github.com/jevplays-games/jev-factorio-agent/issues/495)
open until 24 hours of useful native operation without manual recovery are
verified. Passing regression tests or a shorter successful interval is not that
acceptance result.
