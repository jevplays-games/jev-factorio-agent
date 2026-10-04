# Acknowledged background crafting and research resupply

## Dependency and scope

This is the next efficiency increment after PR #28. It is based on
`fd40516d59eca41d73eab6ae7857b751d8573260` and targets
`feat/production-efficiency-ready-work` while that PR remains open. Land/reconcile
#28 first, then retarget this change to main; do not merge it into an active
campaign as a deployment shortcut.

The new optional controller can gather and transfer independent materials while
one positively acknowledged native handcraft batch continues. It also considers
research resupply before the lab becomes empty. It does not add belts, inserters,
a smelting cell, parallel mutation dispatch, autonomous job cancellation, new
worlds, or faster-than-native crafting. Native throughput is not yet measured.

## Activation

For a separately authorized hierarchical FLE campaign, add both flags:

```text
--factory-scheduling ready-work --background-work
```

Existing commands without `--background-work` retain the original controller and
checkpoint representation. The simple mock CLI does not simulate the native
catalog and is deliberately rejected for this option. Tests use an explicitly
synthetic receipt-capable backend with the real controller instead. Binding,
normal speed/reach, native controls, and resource-site checks remain in force.

The autonomous supervisor accepts these flags and forwards them on every
gameplay launch. Its repair validation preserves background jobs, attempts, and
input-route commitments. Diagnostics and research evidence understand these
extensions. Enable them only for an explicitly authorized campaign; preserve
checkpoints and native receipts across restarts. Optional furnace automation
also requires `--furnace-output-buffers`; `--furnace-input-belts` requires that
output-buffer flag. Native throughput improvements remain unverified.

## Execution and evidence contract

1. A bounded, single-step, deterministic item craft becomes `factory_craft_job`
   with an opaque request ID. Normal plan IDs and failure counters are retained.
2. The existing fresh precondition check, input reservation, and prepared
   write-ahead checkpoint precede dispatch. The native adapter requires all
   immediate ingredients, an empty queue, and full `begin_crafting` acceptance.
   It checks the actual inventory debit; it never inserts synthetic products.
3. Native receipt identity includes session, player, character, surface, force,
   recipe, requested/accepted batches, paid inputs, expected output, and its
   initial inventory. Craft events count finished batches, not enqueue success.
4. Only `dispatch=returned` plus a matching running receipt can transfer a craft
   from foreground pending state to `background_job`. The job, deadline, paid
   inputs, output locks, and foreground clearing are saved together. Inputs are
   released from player reservations only because the native queue already paid
   them. Future output is not spendable inventory.
5. While the job runs, allow only independent gather/insert/extract operations
   and passive observation. All items matching its output are locked, including
   pre-existing stock. No second handcraft, new construction, or job-output
   consumption is allowed. Candidates are rechecked against actual inventory
   and job locks after the existing fresh observation, just before dispatch.
6. Completion requires matching native finished-batch evidence, the original
   deadline, and all expected output still present. A backend success message,
   inventory alone, or a forecast cannot verify completion. Cancellation, an
   unexpected queue/event/actor, regressed evidence, missing output, or timeout
   leaves the job and any foreground mutation intact and enters uncertainty.

The event fires before output insertion, so a later observation verifies output.
The opt-in observation wrapper captures the main inventory and receipt in the
same Lua evaluation/tick, replacing FLE's earlier inventory sample without an
extra RCON round trip. This avoids treating a new craft event plus stale inventory
as lost output. Research and resource lookahead never mutate that real snapshot.

Relevant official API contracts: [craft events](https://lua-api.factorio.com/latest/events.html#on_player_crafted_item),
[queue events](https://lua-api.factorio.com/latest/events.html#on_pre_player_crafted_item),
and [LuaPlayer](https://lua-api.factorio.com/latest/classes/LuaPlayer.html#begin_crafting).
Those moving documentation pages are not proof of native execution on the
repository's Factorio 2.0.77 runtime. Version-specific native acceptance remains
required; unsupported or missing receipt evidence fails closed.

## Recovery and checkpoint compatibility

`BackgroundMemory` adds named `background_schema`, `background_job`, and
`background_attempt` fields to the schema-2 checkpoint. New admissions use
background extension schema 3 and also retain the exact admitted step in
`background_step`. Empty or completed background extensions remain schema 2;
the schema-3 requirement applies while a job owns an active attempt and step.
All checkpoints written by this revision include the `background_step` field,
including `null` when no job is active. A reader from before this field was
introduced rejects the checkpoint's unknown field even when its extension
number is still schema 2. The extension number alone does not promise
cross-revision compatibility.
Admission transfers the pending attempt identity to the background job;
acknowledgement alone creates no verified outcome. Observed completion records
that identity without changing any concurrent foreground attempt. Background
latency remains unknown across this handoff.

For schema-3 jobs with an attempt, checkpoint loading verifies that the saved
step matches its fingerprint and binds to the job's recipe parameters, paid
inputs, output item, plan, and fixed deadline. Admission performs these checks
against the live foreground step before it clears pending state. Older schema-2
jobs did not retain the full step, so loading accepts them only when the attempt
hash matches the canonical tracked-craft step reconstructed from the job's
receipt fields; other schema-2 jobs fail closed as unprovable. Schema-1 jobs
without an attempt retain their documented unknown-attempt migration behavior
and do not receive a fabricated attempt. No migration rewrites attempt identity
or history. A reader that supports only extension schemas 1 and 2 rejects an
active schema-3 checkpoint. A reader predating `background_step` rejects both
new schema-2 and schema-3 checkpoints because its checkpoint constructor does
not accept that field; it must not resume or rewrite them while discarding
`background_step`.

Composed checkpoint loading treats `background_step` as a background-extension
field even if its schema/job/attempt markers are missing, so an orphan step is
rejected by the background validator instead of being ignored by a base-memory
reader. Supervisor repair/source/terminal gates also retain it as an ownership
field. New compatible-source scopes explicitly include the step; previously
authorized scopes without that key continue to validate against their exact
stored authorization preimage and are not rewritten to add a null field.
Offline reconciliation diagnostics expose the exact step. Acceptance captures
retain it in their complete validated checkpoint artifacts; the bounded
gameplay-row projection intentionally omits it and records that omission, so
the projection does not replace the checkpoint as the ownership authority.

When a returned craft remains foreground-pending and a later poll proves its
receipt is running, the controller traces the unsatisfied pending-poll predicate
before transferring the same attempt to background ownership. This uses the
existing poll snapshot and dispatch; it does not poll or dispatch again.

Legacy schema-1 checkpoints load without rewriting the source. An existing
background extension at version 1 retains its job and explicitly unknown attempt
identity until completion; no dispatch identity or timing is fabricated. New
admissions use extension version 3. Offline diagnostics understand both background
and input-route extensions and preserve their evidence in the report, including
the exact schema-3 background step. A new
background checkpoint is refused by an older reader rather than silently losing
work. Do not remove extension fields, downgrade the version, or
restore an old checkpoint against a newer world for rollback.

Atomic replacement and file fsync persist each job transition. This controller
also fsyncs the containing directory on POSIX. A persistence error poisons the
current controller instance: it must be reconstructed before further work. The
Windows directory-durability guarantee is not claimed.

Crash windows are conservative. A prepared/ambiguous craft never becomes a
background job merely because its running receipt exists. It may be verified
when completion is observed, but is never blindly replayed. A persisted returned
command with a lost post-dispatch observation can be admitted on the next
observation without another enqueue. A persisted background job is observed
before any new mutation after restart. Locks release only after verification is
saved. Uncertainty does not clear another action's pending dispatch.

Native receipts are held in the existing ephemeral FLE runtime. Reattaching the
tracker preserves its receipt and avoids double-registering its own handlers.
A server/runtime reset can destroy that evidence and is NOT silently repaired or
replayed from the Python checkpoint. The controller stops on missing evidence.
Only the current native job receipt is retained; this is not a global exactly-once
service or protection against malicious scripts rewriting game state/evidence.

## Restart reconciliation before resuming a campaign

Use `--reconcile-only` when a resumed checkpoint contains a paid background job
that should be settled before autonomous decisions continue. This mode requires
hierarchical FLE `--resume`, `--resume-controller`, the existing checkpoint,
`--factory-scheduling ready-work`, and `--background-work`. It performs one fresh
normal controller observation, passes any persisted craft through the same native
receipt and output checks, then durably saves the resulting checkpoint. It does
not resolve model credentials, ask Jev, plan, dispatch, clear uncertain work,
or retry a craft. A valid completed receipt adds its existing attempt identity to
verified outcomes once;
missing or mismatched evidence remains retained and uncertain. The console result
contains only controller status, observation tick, and a bounded background
state; it omits receipt and actor identifiers.

Before running this one-shot command operationally, use the existing supervisor's
single-writer/external flock and native attachment-receipt preflight. Do not start
a second controller or bypass receipt reconciliation. After checking the reported
status and preserving the checkpoint, a separate launch can use `--until-complete`
to continue until the configured target is verified or reaches the controller's
existing blocked/uncertain terminal state. That launch has no step or duration
cutoff and remains subject to the same failure and durability guards.

## Bounded useful work and research prefetch

The ready-work material frontier is reused for independent actions. A private
lookahead copy may credit expected job output to expose later prerequisites, but
real dispatch cannot spend it. At most eight candidates are returned, with at
most 32 additional material probes across research packs.

For current research, target at most 20 packs per ingredient, capped by the
estimated remaining research demand. Start ordinary resupply planning at five
remaining packs or fewer. During another acknowledged craft, consider independent
pack requirements earlier. Prioritize the most depleted pack input and boiler
fuel maintenance; skip the locked output and avoid unnecessary gathering when
carried stock can already be delivered. These initial thresholds are engineering
heuristics, not calibrated travel-time or throughput guarantees.

## Validation and rollout gates

Run offline:

```sh
python -m pip install -e '.[test]'
python -m pytest tests/test_craft_jobs.py tests/test_craft_jobs_lua.py tests/test_background_work.py -q
python -m pytest tests/ -q
python -m compileall -q src tests
```

Coverage includes receipt identity/shape, paid inputs, event counts, cancellation,
partial acceptance, actor and queue changes, output locks, atomic inventory,
request duplication, adapter reattachment, controller reconstruction, lost
acknowledgment, lost observation, checkpoint faults, real pre-dispatch drift,
small remaining research demand, immutable lookahead, and early CLI rejection.
Lua tests execute the actual tracker against a mocked Lua game; these are not
Factorio gameplay tests. Hosted full-suite and browser results are recorded in
the PR separately from local focused tests and skipped optional dependencies.

Before native use: review the exact head and reconcile overlapping instrumentation
and checkpoint consumers. In authorized isolated worlds compare serial, ready-work,
and ready-work plus background work with pinned conditions. Measure verified
milestone time in ticks AND wall time, useful throughput, starvation, manual
transfers, walking, job uncertainty, and JEV/fallback attribution. Do not declare
success merely because the player moved while crafting or took fewer actions.

## Next increments

- Native acceptance plus compatible supervisor/evidence consumers and scheduler
  priority hardening, before enabling this on an existing autonomous campaign.
- Inventory-paid furnace output inserter and buffer, with orientation/energy
  checks and sustained observed downstream flow.
- Complete drill/belt/furnace input/output cell, including fuel/power, material
  reservations, collision-safe layout, and flow verification.
- Recurring assembler versus handcraft economics and JEV near-milestone ranking,
  evaluated against independent paired throughput and completion measurements.
