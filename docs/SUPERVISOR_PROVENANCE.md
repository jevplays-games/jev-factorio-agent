# Supervisor provenance

This change gives the supervisor, each gameplay child, incidents, and repair
attempts a shared research identity. It is not a claim of autonomous completion
or a new controller policy. No telemetry-only Factorio observation or model call
is added, and the existing checkpoint, pending-action, fairness, review/merge,
world/session and original-cutoff gates remain in place.

## Compatibility and scope

The research logging core in PR #5 owns the canonical research event stream.
This feature uses a small, independent `provenance.py` bridge and
adds fields to the existing supervisor audit and existing gameplay records. It
does **not** introduce a competing general-purpose research event store, overwrite
an experiment manifest, implement a hash chain, or claim replay completeness.
Core and causal gameplay logging can join this journal using the validated
context IDs; the supervisor journal is not a sealed causal event stream.

`--log-file` remains unchanged. Without a supervised context, both controllers
produce their existing records without additional fields. With supervision,
records add `run_id`, `segment_id`, `execution_id`, and `code_revision`.
The context is frozen when a controller is constructed.

The supervisor remains the only writer of `<state-dir>/events.jsonl`.
Gameplay children do not append to that stream. Use a separate supervision
subdirectory rather than sharing an event-file path with a research-core writer.

## Shared run identity

A fresh supervisor generates a UUID and durably stores it in `supervisor.json`.
To join a named experiment, append either option to the normal supervisor command:

```sh
--run-id trial-017
```

or adopt the ID from an existing JSON manifest, without changing that file:

```sh
--run-manifest /absolute/path/to/run/manifest.json
```

A manifest must be a JSON object containing a valid `run_id`. IDs are 1–128 ASCII
letters, digits, or `_.:-`, beginning with a letter or digit. A conflicting
explicit ID, changed manifest content, or changed persisted ID is rejected before
recovering or launching child processes. The adopted manifest's canonical JSON
SHA-256 is persisted; it is checked on subsequent starts even when the option is
omitted. This digest is an integrity check, not a signature.

The gameplay subprocess receives `JEV_FACTORIO_PROVENANCE`, a JSON object with
only the four context fields above. Do not manually override that variable in a
supervised child. It is removed from repair and verification subprocesses so their
offline tests cannot be mislabeled as gameplay evidence. Repair prompts separately
include the run ID, segment ID, incident ID, and attempt number.

Upgrading a legacy supervisor assigns an ID only to newly written evidence.
`provenance_started.legacy_history=true` explicitly marks incomplete historical
coverage. Existing audit lines, checkpoint contents, and cutoff are not rewritten.
An existing incident receives an ID without replacing its original baseline.

## Audit events and correlation

New supervisor events retain the legacy `at` and `event` fields and add:

- `schema = jev-factorio.supervisor-event.v1`, unique `event_id`, and monotonic
  per-run `sequence` (starting at 1 for newly instrumented history).
- `run_id`, `session_id`, `segment_id`, and `incident_id` where applicable.
- UTC `utc`, process-local `monotonic_ns`, and `writer_pid`. Order by sequence;
  do not subtract monotonic values across machines or reboot boundaries.

Important event names include `incident_started`, `repair_required`, `repair_started`,
`repair_finished`, `repair_interrupted`, `process_prepared`, `process_started`,
`segment_started`, `code_revision_changed`, `source_provenance_changed`, and
`manual_intervention`.

When an accepted code repair also creates a source-revision boundary, one durable
outbox row records both facts: its `event` is `segment_started`,
`code_revision_changed`, or `source_provenance_changed`, and it also carries
`repair_finished: true`, `accepted: true`, and
`intervention_type: code_repair`. Rejected attempts and accepted repairs without
a source-revision boundary use `event: repair_finished`. Consumers should use
the explicit `repair_finished` field to identify completion rather than assume
that every completion row has `event: repair_finished`. Older journal rows with
`event: repair_finished` remain valid under their original schema even if they
do not carry the explicit marker; do not retroactively require it.

An incident ID survives rejected attempts and supervisor restarts. Attempts have
a persisted counter and reference their incident. A restarted, unfinished attempt
is recorded as interrupted with an **unknown outcome**, not accepted. A completed
attempt records exit status, declared repair kind, independently accepted status,
the result artifact's exact-byte SHA-256, observed source revisions, and whether
the result supplied all correlation fields. Keep the referenced `repair-N.json`
artifacts with the audit; hashes alone cannot reconstruct their contents.

Repair results should echo `run_id`, `incident_id`, and `attempt`. Explicit
mismatches are rejected. Legacy results that omit those keys remain subject to
all existing acceptance checks and are labeled `correlation_complete=false`.
A result artifact replaced during validation cannot be accepted as the originally
fingerprinted evidence.

## Recovery categories and revision segments

| Situation | Recorded category and behavior |
| --- | --- |
| Accepted operational repair | `operational_recovery`; existing unchanged-source check still required; no segment rotation for unchanged source |
| Accepted code repair | `code_repair`; all existing code verification gates plus agreement with the independently sampled post-repair commit |
| Failed, rejected, timed-out or interrupted attempt | `repair_attempt`; does not clear the repair gate or replace the immutable incident baseline |
| Explicit human report | `manual_intervention`, `actor_type=human`; creates a segment boundary, even without a source change |
| Source change discovered before gameplay without a repair attribution | `unattributed_change`, `actor_type=unknown`; never inferred to be human |
| Orphan process-group recovery | `operational_recovery`, `actor_type=supervisor`; not a declaration of game-action success |

Source fingerprints include HEAD, staged/index entries, tracked file bytes and
modes, and nonignored untracked files. Ignored credentials are not included.
The configured working directory must be the Git checkout root; a nested
directory produces unknown provenance rather than a partial source fingerprint.
Untracked supervisor outputs/checkpoints and their atomic temporary siblings
are excluded from the fingerprint so
writing telemetry does not itself create a code-change signal. Tracked files
cannot be hidden by those exclusions. Symlinks are hashed as links, not followed.
Git output/file contents from this new fingerprinting code are not exported.

Fingerprints are sampled before gameplay launches and before and after repair attempts,
including rejection and interruption. A known source change rotates `segment_id`
while preserving `run_id`; events carry before/after revisions. Dirty changes
within the same commit are also detected. An unavailable Git checkout, timeout,
unreadable/racing file, or unsupported submodule produces `code_revision=null`.
Known-to-unknown and unknown-to-known transitions are uncertainty boundaries, not
assertions that a code change was proven. The initial segment is not itself an
intervention.

Each repair attempt persists its own `source_before`; the incident baseline
remains immutable for acceptance checks across retries. Prompt or process launch
failures close the open attempt before another attempt starts. Existing
background jobs, attempt identity, input commitments, reservation locks, and
their retained history cannot be erased by a repair acknowledgement.
The final fingerprint check compares the untracked file set as well as HEAD and
index, rejecting a concurrent source-file addition or removal as unknown.

These are **observed boundaries**, not proof of when or by whom every file changed.
There is no continuous filesystem observer. Treat mid-process edits, unreported
human game input, unavailable fingerprints, and uninstrumented history as research
limitations. Do not assume that a running supervisor hot-reloads repaired code;
its PID identifies the event writer, while the segment revision describes the
source sampled for gameplay. This is not a reproducible environment/container
fingerprint or a Factorio save/RNG snapshot.

## Record a human intervention without resuming gameplay

Stop the supervisor first. Keep the human evidence locally, for example:

```json
{
  "actor": "operator-1",
  "reason": "checkpoint_reconciliation",
  "evidence": ["Inspected the original pending receipt and retained pending unchanged"]
}
```

Allowed reasons: `checkpoint_reconciliation`, `game_intervention`,
`infrastructure_change`, `code_change`, `other`.

Run the **same supervisor command with the same identity and cutoff arguments**,
adding:

```sh
--record-manual-intervention /absolute/path/to/manual-report.json
```

This requires an existing supervised run and the same exclusive supervisor lock.
If durable supervision state still names a process, the report is rejected
without recovering, stopping, or clearing that process. Recover supervision
separately before recording the intervention. Manual reporting also leaves an
open repair attempt unchanged.
It records the declaration and returns without gameplay or repair dispatch. It
samples source with a separate ten-second bound even after the gameplay cutoff,
without extending that cutoff. It never clears `repair_required`, pending work,
failure budgets, or incident history.
The event is explicitly a declaration, not independent verification of what the
human did. The report's canonical SHA-256 and evidence count are recorded instead
of exporting arbitrary evidence text. Keep the report alongside the run artifacts.
The event time is the recording time, not an asserted intervention occurrence time.

## Durability and failure behavior

Each audit transition first atomically persists its next state plus an outbox
event in `supervisor.json`. It then appends/fsyncs the event, fsyncs the directory,
and clears the outbox atomically. On restart, an exact already-written tail is
recognized by event ID and content; an event not yet appended is written once.
Segment changes and accepted-repair state therefore remain linked to recoverable
provenance across both crash windows.

A partial JSONL tail, mismatched run, missing tail history, ID collision, sequence
mismatch, or I/O failure stops new child launches. No automatic truncation repairs
or retroactive rewriting occur. Preserve damaged files and reconcile them
explicitly. This local single-writer protocol does not authenticate a malicious
rewrite of the whole state and journal; full-chain integrity belongs to the
research logging core. Existing private repair prompts, checkpoint copies and
verification logs retain their preexisting confidentiality requirements.

## Validation

```sh
PYTHONPATH=src python -m pytest tests/test_supervisor.py tests/test_supervisor_provenance.py tests/test_provenance.py -q
PYTHONPATH=src python -m pytest tests/test_provenance_gameplay.py -q
PYTHONPATH=src python -m pytest tests/ -q
python -m compileall -q src
```

Tests exercise run/manifest identity, child propagation, legacy migration, source
fingerprints with real temporary Git repositories, all intervention categories,
manual record-only mode and lock exclusion, stale acknowledgements, audit failure,
and injected crashes before/after append. Paired mock-controller tests compare
all gameplay/model calls and all record contents after removing the added context.
They are offline tests, not native Factorio validation or performance results.
