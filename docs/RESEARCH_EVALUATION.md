# Research evaluator (implementation plan PR 4)

## Integration status

The evaluator has separate readers for canonical logging-core runs and the
original proposed producer contract documented below. The latter remains useful
for explicit synthetic fixtures; it is not the canonical writer format. Legacy
gameplay JSONL retains its separate compatibility path.

`tests/test_research_core_evaluation.py` exercises the actual `ResearchLog`
producer and instrumented offline controllers. These tests require the producer
dependencies rather than skipping absent integration code. Mock runs remain
synthetic evidence, not native gameplay or performance evidence.

## Canonical logging-core source

Canonical runs retain their original `manifest.json`, `events.jsonl`, and, for a
sealed lifecycle, `integrity.json`. Evaluation reports
`source_format: logging-core-v1`, validates with the producer verifier, and
preserves original manifest/event objects, canonical hashes, and source-file
hashes. The reader verifies a private temporary copy of the exact captured bytes,
then projects those same captured records. The copy is removed after verification;
source files are never modified. It does not rewrite the producer's envelope into
the proposed format.
The producer's compact, sorted ASCII JSON encoding differs from the proposed
contract's UTF-8 encoding for non-ASCII strings; the schema label alone cannot
justify choosing a hash algorithm.

`run_finished` with `outcome: returned` establishes a returned process lifecycle.
It does not establish target completion. `native_victory_event_observed` is a
separate observation-level fact: a qualifying non-mock observation can set that
flag without completing a target. A non-mock `rocket_launch` target requires a
`goal_completed` milestone tied to a qualifying native-victory observation and
its verified goal predicate. An observation alone does not create a milestone,
set `target_achieved`, or make a run benchmark-eligible. Backend operation return
likewise does not prove postcondition success or explicit backend acknowledgment.
Unsealed valid prefixes remain incomplete.
An attributed action verification requires its return before the verifying
observation, consistent action and controller-step identities, and a matching
explicit predicate, plan, step index, and pending start. Delayed `pending_poll`
verification may belong to a later decision. An ambiguous dispatch error only
counts when that later pending predicate verifies the effect; it remains flagged
as ambiguous and does not become a positive backend acknowledgment.
Unknown session, world identity, experiment condition, seed, and initial-world
hashes remain null. A configured mock backend alone does not invent an observed
world identity. Missing model usage and resolved model identity remain unknown.
Lifecycle-only logs without causal instrumentation cannot establish that no
model calls occurred: their token totals remain null, even though the recorded
subtotal is zero.
Missing experiment/world provenance excludes canonical runs from benchmark
cohorts while allowing inspection.

Causal identifiers are scoped by the producer's `trace_id`. Derived event tables
project that identity into their `segment_id` column for compatibility; this is
not proof of a process restart or a resumed run segment.

An integrity seal and matching hash chain detect accidental or unrehashable
modification; they do not authenticate authorship or prevent a complete rehashed
rewrite. Evaluation always reports `authenticated: false`. Native gameplay
acceptance and externally trusted signatures or hash anchors are separate
requirements.

## Commands

Supervised campaigns can opt into canonical capture with
`python -m jev_factorio.supervisor ... --research-dir runs/campaign-evidence`.
Each gameplay invocation receives a fresh exclusive `invocation-<uuid>` directory.
World and controller resume flags remain unchanged. Existing gameplay logs are
not converted, backfilled, or relabelled as canonical evidence. An invocation
interrupted by the supervisor may retain an unsealed, incomplete prefix.
Evaluate each directory with the commands below; continuation invocations are
not independent trials. Missing experiment provenance remains an exclusion.

An output directory inside the active checkout must itself be covered by a Git
ignore rule and contain no tracked files. The supervisor rejects the checkout,
its ancestors, nonignored in-checkout paths, and paths whose ignore status cannot
be checked. Put tracked research inputs outside the output directory; Git ignore
rules do not hide tracked files from source-provenance snapshots. External output
directories remain supported. These checks validate the output destination and
do not turn source-snapshot failures into a clean revision.

Supervised capture on POSIX pins the accepted output parent with an inherited
directory descriptor before resolving the gameplay environment. The child checks
that the selected path still names that directory before creating a run, then
creates the run files relative to opened directory descriptors for the complete
manifest/event/seal lifecycle. A replacement detected before child open fails
closed; later path changes cannot redirect those file writes into another tree.
This does not prevent another process from renaming the pinned directory itself.
Each newly created output-path component and invocation directory is fsynced in
its containing directory before the writer proceeds; an fsync error aborts
capture. This describes the requested sync calls, not a universal power-loss
guarantee.
Supervised capture currently fails closed on Windows because equivalent
descriptor-relative file creation is not implemented there. Direct CLI capture
with `--run-dir` remains available on Windows and POSIX.

Legacy behavior and the existing API remain available:

```sh
python -m jev_factorio.evaluation gameplay.jsonl
```

A research input is a directory containing `manifest.json` and `events.jsonl`, or
an event JSONL with a sibling manifest. A non-sibling manifest can be supplied for
one proposed-contract input with `--manifest PATH`. Canonical logging-core inputs
require their original sibling filenames and directory layout so the producer
verifier can validate the manifest, event stream, and seal together. Reading
never writes to the source run:

```sh
python -m jev_factorio.evaluation runs/trial-001
python -m jev_factorio.evaluation runs/trial-001 --output-dir reports/trial-001
```

Flat JSONL tables and typed DuckDB views require no additional dependencies.
For actual Parquet files, install the optional extra and request them explicitly:

```sh
python -m pip install -e '.[evaluation]'
python -m jev_factorio.evaluation runs/trial-001 \
  --output-dir reports/trial-001-columnar --parquet
```

Pair **condition labels**, not filenames or adjacent records:

```sh
python -m jev_factorio.evaluation runs/A-0 runs/B-0 runs/A-1 runs/B-1 \
  --pair deterministic jev --output-dir reports/experiment-001 --parquet
```

All output directories must be new. Existing reports, input directories, and
input files are never overwritten. Validation and cohort checks precede output;
a temporary sibling directory is published only after every export succeeds.
A failed export is removed. Failures exit with status 2; integrity failures have
no permissive switch. `--allow-mixed-treatments` allows *inspection*, not pooling
or relabeling mixed/intervened trials as clean benchmark evidence.

Legacy logs cannot be mixed with integrity-checked event inputs, used as paired
research inputs, or given an invented manifest. A plain legacy invocation returns
the original list of legacy summary objects. It does not gain chain validation.
The new event path does not reinterpret old verified-record counts as unique
attempt counts.

## Outputs

```text
report/
  summary.json           # runs, replicated groups, exclusions, paired deltas
  integrity.json         # validated source hashes, head hashes, completion flags
  table_schema.json      # versioned flat schemas, including empty tables
  artifacts.json         # hashes of derived files; excludes its own hash
  views.sql              # explicitly typed DuckDB views
  tables/
    runs.jsonl
    events.jsonl
    decisions.jsonl
    model_calls.jsonl
    actions.jsonl
    milestones.jsonl
    interventions.jsonl
    pairs.jsonl
    *.parquet            # only when --parquet was requested successfully
```

JSONL and Parquet use the same fixed columns. Missing numeric measurements remain
null, not zero. Nested summary fields exposed in tables use explicit `*_json`
string columns. Parquet schemas specify int64, float64, boolean, and string even
for entirely empty tables. The unmodified input stream remains the primary
record; tables are projections, not a replacement evidence format.

Run DuckDB from the report directory (relative paths in `views.sql` are deliberate):

```sql
.read views.sql
SELECT run_id, verified_actions, input_tokens, token_usage_complete FROM runs;
SELECT kind, count(*) FROM interventions GROUP BY kind;
SELECT metric, avg(delta), count(delta) FROM pairs GROUP BY metric;
```

No dynamic log content is interpolated into SQL identifiers or filenames. No
provider request body, API credential, raw error, or environment dump is added to
these tables. Source capture/redaction remains the producer's responsibility.

## Producer contract: manifest v1

Required fields:

- `schema_version: 1`, nonempty `run_id`, `session_id`, `controller`, `policy`,
  `target`, and `backend` strings.
- `git`: full lowercase 40- or 64-digit Git `commit`, boolean `dirty`, optional
  `patch_sha256`. Dirty code without a patch hash is ineligible.
- `world`: `seed` (integer or nonempty string), `settings_sha256`, and
  `initial_save_sha256`. Missing initial-world hashes are explicitly ineligible.
- Optional `runtime` object, with `packages_sha256` required for eligibility.
  Record Python, platform, and other relevant environment information there.
- Optional `experiment_id`, `trial_id`, `condition`, `replicate` (nonnegative
  integer), and `pair_id`. Absent experiment metadata is never inferred; it is
  reported as an exclusion from experiment summaries, while run inspection works.
- Optional `world_kind`, `requested_model`, `resolved_model`, `confidence_floor`,
  `factorio_version`, `fle_version`, `configuration`, and `treatment`.

All `*_sha256` fields described above are `sha256:` followed by 64 lowercase hex
digits. `configuration` and `treatment` objects should record prespecified budgets,
termination rules, repair permissions, model settings, and experiment-specific
controls. These and the code/runtime/controller fields enter the treatment
fingerprint. World seeds and run identifiers do not.

Distinct run IDs alone do not establish independent replicates. Reusing a world
session across inputs is rejected: capture independently reset sessions rather
than treating a continuation as another trial. A future reset-proof/lineage
contract can relax this conservatively; this version does not invent one.

## Producer contract: event v1

Every line is one UTF-8 JSON object terminated by a newline. No blank records,
duplicate JSON keys, nonfinite numbers, unsupported versions, or torn final line
are accepted. The line limit is 16 MiB. Required envelope fields:

```json
{
  "schema": "jev-factorio.event.v1",
  "run_id": "trial-001",
  "segment_id": "segment-001",
  "sequence": 1,
  "event_type": "run_started",
  "time": {
    "utc": "2026-09-21T07:00:00.000000+00:00",
    "monotonic_ns": 1000000000,
    "factorio_tick": 0
  },
  "correlation": {},
  "payload": {"manifest_sha256": "sha256:<64 lowercase hex digits>"},
  "prev_hash": null,
  "event_hash": "sha256:<64 lowercase hex digits>"
}
```

The placeholders above are illustrative, not valid digests. Sequence starts at 1
and is contiguous for the entire run, including restarts. The first event must be
`run_started`; its payload binds the canonical manifest hash. A resumed process
must not emit another `run_started`.

Canonical bytes are specifically this Python JSON encoding, **not RFC 8785/JCS**:

```python
json.dumps(value, sort_keys=True, separators=(",", ":"),
           ensure_ascii=False, allow_nan=False).encode("utf-8")
```

`event_hash = "sha256:" + SHA256(canonical(event without event_hash)).hexdigest()`.
The previous hash is included in those bytes; the genesis previous hash is null.
Manifest hashing uses the same canonical encoding. Raw source-file hashes are
also retained, so whitespace changes alter the captured-file fingerprint even
when canonical event hashes remain valid.

Timestamps must explicitly be UTC. Counts, ticks, monotonic values, durations, and
token counters use nonnegative signed-64-bit integers, never booleans. Latency
comes only from a producer-recorded `payload.duration_ns` on the measured span.
Monotonic clocks are not subtracted across processes or segments. UTC start/end
span is labeled `wall_elapsed_seconds`; a backwards UTC step makes it unknown.

New segments begin with `segment_started`, a fresh `segment_id`, and
`payload.provenance` including `git`, `controller`, `policy`, `target`, and
`requested_model`. A new segment without explicit provenance is rejected.
Changes to code/config/model/condition fields mark a mixed treatment. Plain
process recovery may start a segment with unchanged provenance.

## Event payloads and causal accounting

| Event | Consumer requirements / meaning |
|---|---|
| `observation` | Unique `correlation.observation_id`, object `payload.state`; recorded session/world identity must agree. The qualifying non-mock native rocket-launch snapshot sets `native_victory_event_observed`, independently of milestone completion. |
| `model_request` | Unique `model_call_id`, `payload.requested_model`; retries require new call IDs. `decision_id` is optional for legacy producers. |
| `model_response`, `provider_error` | Preceding request with same call ID; one completion. Optional `decision_id`, `resolved_model`, `usage.input_tokens`, `usage.output_tokens`, `duration_ns`. When request and completion both carry `decision_id`, they must match. If one is absent, it remains unknown; the evaluator does not invent or backfill it. |
| `decision` | Unique `decision_id`, `payload.source`, `payload.action`; any model reference must already have completed and every recorded request/completion decision identity must agree with this decision. |
| `action_prepared` | Unique `action_id`, preceding `decision_id`, and the same action named by that decision. Preparation is not proof of dispatch. |
| `action_returned` | Preceding preparation and boolean `payload.ok`; acknowledgment is not postcondition success. |
| `verification` | Known action ID, **post-preparation** observation ID, boolean `payload.verified`. Repeated positive verification never recounts an action. |
| `goal_completed` | Known observation ID, `payload.goal`, `payload.verified: true`; first completion per goal is counted. A rocket-launch target additionally requires the goal milestone to reference the qualifying native-victory observation. |
| `incident_started`, `operational_repair` | Counted separately; operational recovery is not relabeled as a code change. |
| `code_revision_changed`, `manual_intervention` | Make a run mixed/intervened and ineligible, even when inspection is explicitly allowed. |
| `run_finished` | Last event, status one of completed/failed/blocked/timeout/cancelled/stopped; optional string reason. |
| `candidate_set_created`, `plan_committed`, `checkpoint_written` | Preserved as indexed events; this PR does not implement the later replay/audit stage. |

`payload.provenance` can record code/config metadata on any event. Unsupported
event types are errors rather than silently omitted data. Missing result events
can coexist with later observational verification (lost acknowledgments). Unknown
or pre-action observations cannot justify a successful action. This reducer
validates the provenance of captured verifier assertions; it does **not** rerun a
postcondition engine or independently interrogate Factorio.

Model calls count requests, including failures and unresolved requests. Missing
usage is null, with known subtotals reported separately as `*_tokens_recorded`.
No-call runs have a known zero total. Input and output completeness are separate;
`token_usage_complete` requires both. Observational/wait actions are excluded from
productive verified actions. Target completion needs logged verified milestone
evidence; a live `rocket_launch` needs an observed
`native:base-game-rocket-launch` source. Mock outcomes remain synthetic even when a
mock snapshot claims that source. No stdout/status label alone proves victory.

## Replicated and paired summaries

The unit of analysis is a run, never a decision, model call, verification record,
or segment. Duplicate run IDs, sessions, condition/trial cells, and
condition/seed/replicate cells are rejected. A condition label mapping to multiple
code/config fingerprints or resolved model versions is rejected by default.
Inspection mode reports these groups separately and excludes them from statistics.

Pair matching uses `(experiment_id, pair_id)` when explicitly supplied, otherwise
`(experiment_id, seed, replicate)`. Each side must have complete metadata and
compatible seed, replicate, initial-save hash, settings hash, target, world kind,
backend, runtime, and recorded Factorio/FLE versions. Different conditions may
legitimately use different policies/code/models; those differences are the
comparison, not a reason to pool them. Incomplete or mismatched pairs remain in
`unmatched` with reasons. Unavailable token totals produce null deltas, not zero.

Deltas are always **treatment minus baseline**; positive is not automatically
better. Group statistics report total/observed/missing counts, mean, sample
standard deviation (null for fewer than two measurements), min, and max.
Ineligible runs and condition drift remain visible in denominators and exclusions.
These are descriptive summaries, not significance tests, independence proofs,
confidence intervals, or evidence that incomplete-trial attrition is ignorable.

## Integrity boundaries and validation

Hash validation detects mismatched content, reordering, gaps, wrong links, and
torn lines. A hash-valid prefix without `run_finished` is explicitly incomplete.
**An unanchored SHA-256 chain does not authenticate its author, detect every
valid-prefix truncation, or prevent a complete rehashed rewrite.** A separately
trusted terminal hash/signature is needed for that stronger claim. This evaluator
never reports `authenticated: true`.

Use captured/closed logs. A second hash pass detects ordinary concurrent changes;
it is not a distributed lock or defense against a malicious writer. The first
implementation retains decoded events and projected rows in memory (O(events));
it does not claim constant-memory processing of arbitrarily large campaigns.

```sh
PYTHONPATH=src python -m pytest tests/test_research_evaluation.py tests/test_research_core_evaluation.py -q
python -m compileall -q src
# Full optional export verification:
python -m pip install -e '.[evaluation]' pytest duckdb
PYTHONPATH=src python -m pytest tests/test_research_evaluation.py tests/test_research_core_evaluation.py -q
```

The existing full-repository CI suite remains unchanged in scope; a separate
Python 3.10/3.12 export job installs PyArrow and DuckDB so round-trip tests run
instead of skipping. Optional dependency skips must be reported separately.

Implementation references: Apache Arrow's official Python Parquet guide
(https://arrow.apache.org/docs/python/parquet.html) and DuckDB's official JSON
loading reference (https://duckdb.org/docs/stable/data/json/loading_json).
