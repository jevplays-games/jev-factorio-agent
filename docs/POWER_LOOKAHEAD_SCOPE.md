# Future power service was presented as immediate demand

The V44 campaign completed a useful hour, advanced concrete research from 56%
to 64%, and continued production. After completing 12 gears at native tick
`19238123`, it entered another sustained decision hold. The game processes
remained alive, but no craft, furnace, research, or pending action was advancing.
The successful hour does not satisfy the 24-hour acceptance requirement.

## What the retained decisions show

The first settled decision, tick `19238196`, included two individually qualified
actions: transfer 20 carried iron ore into the current iron furnace, or take one
coal to the boiler. JEV selected the boiler with confidence `0.20`, below the
unchanged `0.45` floor. Its probabilities were `0.47` for the boiler, `0.43` for
iron, and `0.10` for observing. The exact rejected input is
`23cab270d24c1f975351a59a1878c244fa7e2befc5fdc6c857163c58cfa19a3a`.

The later decision at `19238634` contains the remaining optional input-route
build and lookahead copper/iron work. All were rejected. Inspecting only that
latest checkpoint would incorrectly suggest that immediate iron loading was
never offered. Both decisions and the accepted observation bound to the first
decision's native digest are retained in the regression fixture.

## Evidence defect

The boiler had four coal. Its annotated consumer was the idle copper-cable
assembler: a configured recipe, no inputs, and no active craft. The ready-work
planner explicitly marked this boiler top-up as `lookahead`. Its direct power
path contained only `item:copper-cable`, while the current production path was
logistics science through inserters and iron plates.

The power evidence builder checked the live machine, configured recipe, native
topology, and exact power step. It then promoted the action to `immediate` and
described its consumer as current production demand. Those checks establish
the physical power relationship; they do not change an explicitly speculative
planner path into the current production prerequisite. The choice request thus
presented both options as immediate even though the planner distinguished them.

The repair requires any explicit work intent to be current, well formed, and
`immediate` before producing or accepting an immediate power-prerequisite
witness. Legacy serial plans without work intent retain their existing checks.
Immediate power service, startup, research prerequisites, topology validation,
paid inputs, and native postconditions remain required. The future boiler action
remains offered with its original physical steps and lookahead scope.

This corrects the evidence claim. It does not prove that the incorrect scope
alone caused the model's `0.20` response, or guarantee a confident future choice.
No scheduler fallback, candidate removal, threshold change, model reroll, or
failure-history reset is part of the repair. A `0.20` choice still rejects.

## Saved requests and live recovery

The initial focused checks missed a saved-request compatibility case. Full CI
caught eight failures: seven expectations of the superseded scope promotion,
and one historical two-stage request whose questions no longer reconstructed
exactly. Fresh requests now carry `power_intent_scope: 1` in their selection
contract. Historical unmarked requests reconstruct their original evidence and
questions unchanged; this does not reopen a settled refusal or authorize an
action. Invalid version markers reject. Current requests independently reject
retained false power evidence. The historical fish-observation fixture and the
exact V44 rejected choice both validate without rewriting their bytes.

The version marker identifies the request format, not the quality of its native
evidence. Missing or stale facts still follow the existing candidate refusal
path. They must not raise a format error that obscures the actual rejection;
only an unsupported or malformed version marker does that.

The first V45 decision still held: iron loading won with confidence `0.40`,
below `0.45`, against an optional input-route build. That refusal is retained.
The same controller subsequently resumed without another intervention: iron
loading verified at `19376810`, iron collection at `19381533`, copper loading
at `19382724`, and copper collection at `19387127`. These receipts demonstrate
recovery, not uninterrupted operation or completion of the 24-hour requirement.
The deployed initial source and later compatibility correction must be tracked
separately; CI success alone does not prove the final source is deployed.

## Relationship to earlier repairs

- [PR #280](https://github.com/jevplays-games/jev-factorio-agent/pull/280) prepared
  lab science before power service and bounded boiler fuel trips. This case is
  a speculative assembler consumer whose evidence changes its scope.
- [PR #463](https://github.com/jevplays-games/jev-factorio-agent/pull/463) retained
  immediate work when speculative urgency hid it. Here the immediate iron
  transfer was already offered; the inaccurate claim concerned its competitor.
- [PR #357](https://github.com/jevplays-games/jev-factorio-agent/pull/357) explained
  the existing scheduling tie-breaker. That explanation was present in this
  rejected request, so repeating that prompt change is not a new repair.
- [PR #530](https://github.com/jevplays-games/jev-factorio-agent/pull/530) retained
  the ordinary production purpose of an identical optional-kit service action.
  It does not qualify or promote power-consumer scope.

## Efficient diagnosis and recovery

1. Check native craft, smelting, research, pending work, and process identity.
   A foreground wait during advancing native work is different from this hold.
2. Preserve the first settled refusal and later alternative batches. Match the
   original accepted observation to the decision's native digest. Keep bounded
   diagnostics; the collector here reads at most a 32 MiB event tail.
3. Compare the planner's work intent with the evidence and actual choice packet.
   Reproduce with the original capital failures, receipts, and ownership.
4. Test the exact capture, unchanged offered actions and 48 KB request boundary,
   immediate-power positive cases, stale/malformed intent, retained false
   evidence, and continued low-confidence rejection. Offline tests must make no
   provider or native gameplay calls and leave campaign memory unchanged.
5. Follow [the signed-source recovery runbook](STATUS_RECOVERY.md). Reconcile
   quiescent state, preserve all failed attempts, and admit changed source through
   the existing owner. Never replay an ambiguous admission or a held model call.
6. Verify fresh useful native receipts and actual OBS Program output. Preserve
   this failed monitoring interval separately from any intervened deployment.

[Issue #495](https://github.com/jevplays-games/jev-factorio-agent/issues/495)
remains open until a complete 24-hour useful-operation interval is verified.
