# Completed-iteration timing — #96

This is bounded, non-authoritative instrumentation of the existing sequential
hierarchical controller, not permission to parallelize an actor or shared RCON
client. It supplies wall, whole-Python-process CPU and current-thread CPU clocks.
It does not measure native-server CPU, wire latency, host steal or Python-only
CPU in another process. Historical issue measurements remain historical.

## Publication and boundaries

The ledger is enabled for hierarchical `ready-work`, or `serial` with observation
profiling. Flat-controller records retain their previous behavior. The existing
initial observation → planning → fresh preconditions → action → verification
ordering, pending/receipt barriers, provider policy and actor-loop sleep remain.

A decorated step owns one context-local stack (maximum depth 64) and a fixed
label vocabulary. Nested inclusive durations overlap; each parent also has an
exclusive duration after subtracting its immediate children. The sum of all
exclusive rows must equal the root interval for each supported clock. Root
exclusive time is the remaining measured controller work, including profiler
bookkeeping; it is not guessed network or native time. Clocks that fail, regress
or cross Python threads invalidate the partition instead of yielding zero costs.
The internal thread identity is never serialized. Concurrent/reentrant actors
are not supported or authorized by this ledger.

`previous_iteration_timing` in the **next** gameplay record describes the previous
completed decorated step, including its actual record write, console output and
final causal event. At the next step's start the completed interval can also be
paired with its following gap. The gap is partitioned into actual intentional
actor-loop sleep and the remaining gap. Requested sleep and observed sleep are
separate. Interrupted sleep preserves its exception and records an unsuccessful
sleep call. Native action-completion polling is `action_poll_wait`, not actor-loop
sleep or the supervisor watchdog interval.

The first record has no completed prior iteration. The final step/tail cannot be
published until another record exists, and remains **unobserved**, not zero. An
abrupt process exit loses private timing data, not authoritative action state.
A failed step may be reported by a later record; missing record indices are
counted rather than filled with synthetic observations. The report separately
shows indices before its first captured timing sample; that subset also remains
in the existing total for unrepresented indices and is not proof of failed steps.
Long pauses and caller
work between steps remain part of the observed gap and must be explained when
comparing workloads.

## Measured boundaries

Fixed labels cover initial/fresh/post observations, planning, selection,
dispatch, verification, trace capture/normalization/redaction/emission, record
construction, legacy JSON encoding/writing/console output, checkpoint comparison,
copy/serialization/write/file sync/replace/directory sync, research redaction,
validation/serialization/hashing/assembly/write/fsync, and operational sidecar
JSON encoding/writing/sync/replace. Filesystem operations not explicitly wrapped
remain in their measured parent's exclusive category; startup, sealing and
shutdown outside a decorated step are not relabeled as iteration work.

For a reviewed one-step diagnostic run, `--setup-timing-file NEW_FILE` writes a
separate content-free `jev.setup-timing.v1` result at process exit. Ordered
wall/process-CPU cuts partition setup from before selected-checkpoint preflight
through preflight completion, research/dashboard writer readiness, FLE backend
attachment and native installation readback, controller construction, output
attachment and the `controller_initialized` event. Preflight is recorded before
writer readiness because rejected preflight must not enter either run writer.
Missing cuts or regressing clocks yield a partial result. The file is exclusive
and diagnostic only; failure to write it does not change action or checkpoint
outcomes. It contains no commands, paths,
session IDs or native response data. It also publishes the final completed
one-step timing ledger when profiling is enabled, with the following gap marked
unknown. This does not add a final gameplay record or infer an unobserved sleep.
New `jev.setup-attribution.v1` records use the preflight-first phase order. The
offline latency report continues to accept the exact historical phase order for
already sealed version-1 runs; it rejects mixed or reordered phase sequences.
Setup intervals still do not separate native server time from RCON transport or
guest scheduling pressure.
The setup record ends at `controller_initialized`; it does not measure later
step, post-step sleep, teardown or its own exit-time encoding and file sync.
On FLE runs a nested backend partition separates instance construction and
session checks, installed-campaign readback, and fair-action adapter setup;
these backend rows sit inside the `dashboard_ready` to `backend_ready` phase and
must not be added to that phase. This phase follows the research and dashboard
writer-readiness phases after preflight. No command or native result content is
recorded.

Native command, batch and JSON-decode spans wrap the actual public adapter/client
path. A depth guard counts one **logical client call** across nested
`NativeFactory`/`ProfiledRcon`/`SessionRcon` wrappers, not three calls. A batch is
one logical batch call, **not** a packet count or an atomic game snapshot.
Request/response counts are UTF-8 content bytes when their type and encoding are
known, not protocol framing or compressed wire bytes. Missing size remains an
explicit unknown count. No Lua, endpoint, response content, IDs or exception text
enters the ledger. Sequential delegated requests contribute their known content
bytes once; an unknown delegate marks that logical call's byte evidence incomplete
without discarding known bytes from its other delegates. Delegate results and
exception objects remain unchanged.

The action-time FLE `get_entity` lookup used by `NativeFactory.entity` is measured
inclusively under `fle_helper`; each client call it makes through `SessionRcon`
remains separately counted as a logical native command. These nested inclusive
durations are not additive. This span covers the helper call, not a decomposition
of its internal work. FLE retry counts, helper backoff, response decoding inside
the dependency, and native/network time separation remain **unknown**. A test
containing two attempts and a wait does not establish that every installed FLE
retry path is covered.

The installed FLE source used for this audit is pinned to v0.4.3 commit
`6439e18b7870770454cf91eb36b3d1e6412724f4`. Check a local runtime environment
with the read-only compatibility command:

```sh
PYTHONPATH=src python -m jev_factorio.fle_compat_audit
```

It checks the installed version and hashes of the controller and helper modules
that define processing retries, entity lookup, and helper polling. It emits no
installation path or direct-URL metadata; a match establishes source identity,
not retry attribution or native performance.

## Durability and compatibility

The ledger uses bounded dictionaries and one completed/pending cycle. It creates
no background queue, timer thread, additional fsync, native request or checkpoint
field. Checkpoint and research writes remain synchronous. A timed operation's
failure still propagates and retains its existing fail-closed pending/ownership
behavior. Serial records without profiling keep their prior fields. Complete
numeric timing is an optional new gameplay field; old records are not assigned
zero overhead. Performance counters keep their original inclusive semantics.

## Reports and reproducible fixtures

```sh
PYTHONPATH=src python -m jev_factorio.latency_report /private/path/gameplay.jsonl
PYTHONPATH=src python benchmarks/benchmark_iteration_timing.py --samples 20
PYTHONPATH=src python -m pytest tests/test_iteration_timing*.py
```

The offline report rejects malformed labels, impossible partitions, duplicate or
regressing completed-iteration indices, and mixed process/source/treatment
streams. It exposes counts and median/middle-pair and nearest-rank p95
distributions. Phase distributions are **per-iteration aggregate** inclusive or
exclusive values, not per-call latency percentiles. The `iteration_and_following_gap`
series combines disjoint intervals only. Legacy UTC observation/record intervals
are kept separately, with their overlap and incomplete emission boundary intact.
Checkpoint written-byte/write totals retain the original performance scope.
The report also preserves file/directory/parent sync calls, installed-file
verification reads/bytes, and the producer's I/O and capture/encoding coverage
counts. Each present checkpoint timing has a per-record median and p95; these
are inclusive aggregate costs, not individual-write percentiles or additive
phases. In particular, legacy `serialize_ns` includes capture and JSON encoding,
so do not add it to `capture_ns` or `json_encode_ns`. Missing legacy fields remain
absent, never zero-valued samples; each distribution's count and the coverage
counters identify how much evidence was actually present. Failed checkpoint
attempts retain their reported operation costs without inventing successful
written bytes. All checkpoint measurements end before record construction;
research/sidecar phase call counts distinguish attempts and failures, but do not
claim precise bytes written during a partial write.

The on/off benchmark alternates paired synthetic paid-route episodes through the
real composed controller and real local checkpoint/research writers. It verifies
identical native command sequences, paid commitments, pending/reservation/failure
state and file/directory sync counts. New diagnostic bytes are expected, so it
does not claim identical legacy files. Tests distinguish busy Python computation
from a sleeping fixture using broad CPU-versus-wall attribution assertions, not
fragile tiny elapsed-time speed gates.

## Acceptance boundary

No mock, fake client, Lua fixture, local filesystem benchmark or report proves
native speedup, safe shared-client concurrency, deployed capacity, sustained
science/research, or paid coal/downstream flow. #96 still needs the installed
helper/retry/native availability audit and exact-source native comparison. #92
still requires reviewed deployment, matched predeclared workloads, original
campaign/treatment/cutoff preservation, actual paid flow, crash reconciliation,
and the full 30-minute useful-progress window. Do not extend or reset that window
or suppress a treatment preflight to obtain a passing result.
