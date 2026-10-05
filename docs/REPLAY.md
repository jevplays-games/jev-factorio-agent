# Offline replay and audit

`python -m jev_factorio.replay` reconstructs captured decision evidence. It does
not run the controller, recompute the policy, simulate the world, or dispatch an
action. Every report includes `offline: true`, `replay_authorized: false`,
`behavior_reexecuted: false`, and `authenticity_established: false`.

## Integration status

There are three explicitly selectable formats (`--format`, default `auto`):

* Read existing records now, preserve their evidence, and report the information
  that was never captured. A legacy trace cannot become a complete research
  record retroactively.
* `research-v1` reads canonical PR5 writer output and PR9 causal payloads.
  It verifies the original ASCII canonical bytes, manifest binding, chain,
  lifecycle, and integrity seal before constructing decision views. Original
  events and hashes remain source evidence; views are never rehashed as evidence.
  Missing manifest, seal, causal identity, postcondition verdict, or candidate-set
  reference remains an explicit gap. Known dangling action/model/observation
  references fail the audit. A sealed run does not imply complete causal evidence.
  The producer manifest is canonical ASCII JSON with one trailing newline;
  its digest covers the canonical JSON bytes without that newline. It requires
  schema `jev-factorio.manifest.v1`, integer `schema_version: 1`, and a canonical
  UUID `run_id`. The first event binds that digest as
  `run_started.payload.manifest_hash`. Invalid manifest bytes or fields, and a
  valid digest-bound manifest whose UUID differs from the source run, are input
  errors (exit 2). Event-chain and integrity-seal mismatches are invalid evidence
  (exit 1). A missing manifest file remains a gap. When the manifest is present,
  an absent or incorrect `run_started.payload.manifest_hash` is invalid evidence
  (exit 1).
  The supported CLI's `controller_initialized` and `controller_stopped` records
  are validated as run-level evidence and copied into `run_evidence`; they do
  not receive invented decision or trace identities. Action returns,
  verifications, and pending-expiry records must match the prepared trace,
  decision, session, observation, and attempt bindings before they can affect
  an action view. Cross-step verification remains attributable only when the
  producer explicitly records `action_origin: "current_trace"` with the same
  attempt and plan-step identity.
* `proposed-v1` retains the synthetic fixture contract documented below. Its
  segment envelope and UTF-8 hash encoding are not the canonical producer format.
  Automatic selection distinguishes the producer's explicit `schema_version`;
  it never silently renames payload fields or synthesizes unavailable references.

Offline replay tests exercise the canonical writer, actual flat and
hierarchical mock CLI producers, and generated writer/trace evidence. They do
not establish native gameplay, provider behavior, authenticity, or complete
controller integration acceptance.

No gameplay, supervisor, checkpoint, provider, or environment configuration is
changed. The existing evaluation command remains untouched. The separate
unmerged attempt-diagnostics work is not a dependency: hierarchical schema-2
records can be preserved as legacy evidence, but this reader does not infer
unique attempts or causal links from their optional fields.

## Usage

After installation (or with `PYTHONPATH=src`):

```sh
# A self-contained synthetic example, never a native Factorio result.
python -m jev_factorio.replay tests/fixtures/replay/event-v1

# A research run directory also reads the optional integrity.json seal.
python -m jev_factorio.replay runs/example --format research-v1 --output replay-report.json

# Historical gameplay records are explicitly incomplete evidence.
python -m jev_factorio.replay gameplay.jsonl --allow-incomplete

# Bind the audit to a head digest retained independently of the input files.
python -m jev_factorio.replay runs/example --expected-head 'sha256:<64 lowercase hex digits>'
```

The last command uses a placeholder; use the actual independently retained
head. A standalone JSONL input deliberately does not discover nearby manifests,
checkpoints, `.env` files, URLs, or artifact references. A directory is the
explicit opt-in to reading its manifest. Output goes to stdout unless
`--output` is supplied. Output files use **exclusive creation**: an existing
file, input, checkpoint, or symlink is never overwritten.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Reconstruction completed without findings, or gaps explicitly accepted with `--allow-incomplete`. |
| 1 | Invalid evidence: source-chain or seal failure, or a causal inconsistency. |
| 2 | Input/output usage failure, unreadable input, malformed/noncanonical or schema-invalid research manifest, invalid manifest fields/values/identity, invalid bounds, or an existing output file. |
| 3 | Incomplete evidence, including legacy logs, missing references, or a captured crash prefix. |

`--allow-incomplete` changes only the exit code for gaps. It never changes the
report's status, fills absent evidence, or masks an integrity/causality error.

The implementation builds its report in memory. Defaults are 8 MiB per JSONL
record, 128 MiB per input, and 100,000 records. Larger captures require deliberate
bounds via `--max-line-bytes`, `--max-input-bytes`, and `--max-events`. Exceeding a
bound fails visibly; no suffix is silently discarded. Increase limits only when
sufficient memory is available. Manifests are limited to the smaller of the
line limit and 1 MiB. Integers are limited to 128 literal characters.

Python callers can use:

```python
from jev_factorio.replay import replay_log

report = replay_log("runs/example")
assert report.to_dict()["replay_authorized"] is False
for decision in report.decisions:
    print(decision)
```

`ReplayInputError` covers unreadable inputs and invalid API options. Evidence
findings are available on `report.findings`; `report.status` is `complete`,
`incomplete`, or `invalid`. Reports are deterministic for the same bytes and
options; the reader does not inject its current time, machine identity, or
credentials. Reports preserve input payloads, which may already contain
sensitive information. Treat them with the same access controls as the logs.

## What the audit does and does not establish

The reader checks JSON structure, the event hash chain, causal reference order,
candidate membership, and consistency of prepared actions with captured plan
steps. It reconstructs observations, offered candidates, exact request state and
questions, raw responses or provider errors, the selection, committed plans,
dispatch evidence, and verification evidence. Reports contain source-line
locators and the accepted event journal, including operational/manual repair
records. Unknown event types are preserved and produce an incomplete finding.

**Complete** means complete under this reconstruction contract, not that the
agent's strategy was correct or that Factorio independently reached the claimed
state. The report explicitly lists `policy_selection`, `postcondition_truth`,
and `world_evolution` as `not_recomputed`. Verification booleans and evidence
remain *recorded claims*. A malformed provider reply remains visible even when
the recorded policy correctly rejected it. No response, receipt, counter,
confidence, token usage, timing, success, or missing observation is invented.

A prepared action is not proof of dispatch. A returned call is not proof of its
postcondition. A lost acknowledgment can coexist with later recorded positive
verification; both facts remain visible. Neither authorizes retry. Repeated
polls, reused domain plan names, and historical `step_verified` entries are not
converted into additional unique attempts.

When a captured plan step is available, its action and parameters must match the
prepared action; an absent step, malformed step, or mismatch cannot support a
verified action. If the plan commitment or step details are unavailable, the
reader retains an incomplete gap and any observed return, but leaves the action
unverified rather than treating missing plan evidence as a match.

For legacy files each JSONL record has a line locator, not a fabricated
`decision_id`. `decision_count` is null; `legacy_record_count` and
`selection_recorded` expose the actual evidence. Candidate plans are recovered
from `decision.state.candidate_plans`, questions/responses from the recorded
Decision, and flat choices from `questions.next_action.criteria`. Dispatch
parameters, durable preparations, fresh pre-dispatch observations, and unique
causal IDs that were not logged remain unknown. Hybrid fallback may select a
plan outside the smaller model-offered set; that is a gap, not automatically an
invalid choice.

## Proposed event-v1 contract

Each line is exactly one UTF-8 JSON object followed by a newline. Duplicate JSON
keys, non-finite numbers (including overflowing exponent notation), blank
records, malformed objects, and mixed formats/versions are rejected. The reader
stops at the first invalid envelope/hash and retains the verified prefix.
A complete JSON final line without a newline remains a reported gap.

Required envelope fields:

| Field | Contract |
| --- | --- |
| `schema` | Exactly `jev-factorio.event.v1`. |
| `run_id` | Nonempty string, invariant throughout the file. |
| `segment_id` | Nonempty string, explicitly separates revisions/treatments. |
| `sequence` | Integer, starts at 1 and increases by exactly 1; bool is invalid. |
| `event_type` | Named event below. Unknown names are retained, not certified. |
| `time` | Object with offset-aware UTC `utc`, nonnegative integer `monotonic_ns`, and optional nonnegative integer/null `factorio_tick`. |
| `correlation` | Object of string/null identifiers, including `decision_id`, `model_call_id`, `plan_id`, and `action_id` as applicable. |
| `payload` | Captured JSON object, including explicit references below. |
| `prev_hash` | Null for sequence 1, otherwise the previous `event_hash`. |
| `event_hash` | `sha256:` followed by 64 lowercase hex digits. |

`line` is reserved for the reader's source locator and is rejected in input
envelopes. Monotonic clocks are retained but **not compared across processes**;
sequence and explicit dependencies determine journal order. Factorio ticks are
not unique decision identifiers and do not drive joins.

A run directory's `manifest.json` must contain integer `schema_version: 1` and
the matching `run_id`. Its canonical digest must appear as
`run_started.payload.manifest_sha256`. Missing manifests/digests are gaps;
mismatching identity/digests are errors. This binding does not prove that the
manifest's claimed revision/environment is truthful or sufficient to reproduce
the game.

### Hash encoding

Compute SHA-256 over UTF-8 JSON with sorted object keys, compact separators
(`,` and `:`), `ensure_ascii=False`, and `allow_nan=False`. For an event, omit
**only** its `event_hash` field; include `prev_hash` and every other field.
For a manifest, include the entire object. There is no newline in the hash
input. Digests use the `sha256:` prefix. The fixture has an independently
retained expected head asserted by a regression test.

This encoding is the explicit Python JSON v1 contract, not a claim of RFC 8785
canonicalization. A writer using a different canonicalization must get a
versioned adapter and test vectors rather than silently sharing this label.

Hash chains detect edits/reordering relative to the recorded chain, not a
full-chain rewrite by someone able to replace all evidence. An externally
retained `--expected-head` detects a different prefix/head; its trust depends on
how that expected value was retained. Without an external anchor, a missing
`run_finished` makes whole-record suffix loss visible as incompleteness, but a
forged complete stream cannot be ruled out. `authenticity_established` always
remains false.

### Causal payloads

All causal events except shared observations need `correlation.decision_id`.
IDs for observations, candidate sets, model attempts, and action attempts are
unique within a segment. A plan's domain `id` may repeat across decisions; its
commitment is scoped by `(segment_id, decision_id, plan_id)`. Keep the same
causal decision/action identity for delayed verification, even when it occurs
on a later controller polling iteration.

| Event | Required payload/references for full reconstruction |
| --- | --- |
| `observation` | `observation_id`, exact `state` object. |
| `candidate_set_created` | `candidate_set_id`, earlier `observation_id`, `candidates` list of objects with unique `id` fields. |
| `model_request` | Correlated `model_call_id`; earlier `observation_id`, `candidate_set_id`; exact `state` and `questions` objects. |
| `model_response` | Same `model_call_id`; raw `answers`, with any captured model/usage metadata preserved. |
| `provider_error` | Same `model_call_id`; recorded sanitized error data instead of a response. Each attempt has one terminal result. |
| `decision` | Earlier `observation_id`, `candidate_set_id`, explicit `model_called` boolean, `model_call_id` or null, and selected `plan_id`/`action` or null. Each decision identity has one selection. |
| `plan_committed` | Correlated `plan_id`; `plan` object with matching `id` and full `steps`. |
| `action_prepared` | Correlated unique `action_id`; earlier `observation_id`, `action`, exact `parameters` object; for a plan, `plan_id` and integer `step_index`. |
| `action_dispatched` | Same `action_id`; optional captured dispatch boundary, never manufactured from preparation. |
| `action_returned` / `dispatch_error` | Same `action_id`; recorded return/error. Retries require new action IDs. |
| `verification` | Same `action_id`, later `observation_id`, boolean `verified`, and any captured evidence/predicate data. Predicate text is never executed. |
| `decision_finished` | Optional explicit terminal `status` (`abstained`, `aborted`, `verified_without_dispatch`, `dispatched`) and recorded `reason`. Required to explain selected work with neither dispatch nor verification. |

For work already satisfied without a dispatch, a verification can instead use
`scope: "plan"`, `plan_id`, and `observation_id`, with no `action_id`. An
observation-only verdict uses `scope: "observation"` and `observation_id`.
These are retained separately from action verifications, not converted into
fictional mutations.

A run starts with `run_started` and ends with `run_finished`. A new segment must
start with `segment_started` or `code_revision_changed` and name its
`previous_segment_id`. Revision changes without a new segment, segment
reentry, and events after `run_finished` are errors. Cross-segment references
are not guessed: missing old-segment evidence remains a gap. A mixed-segment
report is not a clean single-treatment experimental aggregate.

`checkpoint_written`, `incident_started`, `incident_finished`,
`operational_repair`, `manual_intervention`, and `goal_completed` are retained
as run evidence. Their narrative contents are not executed or treated as
proof of independent native-world verification. Local/external blob references
are not fetched by this adapter; missing inline payloads remain gaps until an
explicit, hash-verified blob contract is integrated with the writer.

## Validation

```sh
PYTHONPATH=src python -m pytest tests/test_replay.py -q
PYTHONPATH=src python -m pytest tests/test_replay_controller_integration.py -q
PYTHONPATH=src python -m pytest tests/test_replay_research.py -q
PYTHONPATH=src:tests python -m pytest tests/test_replay418_regressions.py -q
PYTHONPATH=src python -m pytest tests/ -q
python -m compileall -q src
```

The first file tests synthetic v1/legacy evidence, integrity, causal mismatches,
crash prefixes, malformed-field mutations, limits, output safety, and a fresh
process with live-client imports, sockets, and process execution forbidden.
The integration file uses the repository's actual `MockBackend`,
`MockJevClient`, flat loop, and hierarchical loop to generate legacy records,
then forbids further client/backend calls while replaying them.
The research integration file additionally generates canonical writer/trace
evidence and actual flat/hierarchical controller runs, checks original hash
identity, and rejects modified bytes and resealed contradictory causal claims.
It requires the canonical core and final causal controller integration; no
conditional skips replace that dependency gate.
Python 3.10/3.12 CI discovers these files automatically. Synthetic and mock tests
are not native Factorio or provider-performance measurements.
