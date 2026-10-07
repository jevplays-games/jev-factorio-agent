# Latency attribution

`--profile-latency` adds content-free setup, controller-step, and model-call-gap
timing to a new hierarchical research run. It is opt-in and requires
`--run-dir`; it does not change decisions, API call count, retries, controller
cadence, or native actions. Profiling adds clock reads and extra research-event
data, so timing results describe the profiled run and should not be presented as
an uninstrumented performance benchmark.

```bash
.venv/bin/python -u -m jev_factorio --controller hierarchical --backend fle \
  --resume --target rocket_launch --policy hybrid --until-complete \
  --resume-controller --tick-seconds 2 --checkpoint runs/campaign-state.json \
  --log-file runs/campaign.jsonl --run-dir runs/campaign-timing-001 \
  --profile-latency

PYTHONPATH=src .venv/bin/python -m jev_factorio.latency_report \
  runs/campaign-timing-001
```

Each invocation claims a new research directory. The offline report verifies the
event hash chain and seal before reading, then verifies them again after reading
to detect concurrent changes. For a still-running or interrupted capture, pass
`--allow-incomplete`; the current chain still must verify, and the report labels
the run incomplete. No game or provider connection is opened by the report.

The `controller_initialized` event records a startup window and ordered setup
phases. The startup window runs from the sample immediately before constructing
the `run_started` event to the sample immediately before constructing the
`controller_initialized` event. Setup phases divide the
ordered initialization boundaries. New setup-attribution records place
`preflight_ready` before research and dashboard writer readiness. FLE backend
phases are nested inside the `dashboard_ready` to `backend_ready` phase and must
not be added to that outer phase. The report also accepts the exact historical
phase order for already sealed version-1 records.

Each `model_response` after the first can include an `inter_request_timing`
window. The report first verifies that each request and response form one
ordered call with the same nonempty call ID, trace, controller, session, and
decision context. The timing clocks sample after the prior response event is
emitted, then sample after the next request event is emitted and immediately
before the next client `evaluate` call. These windows are pairwise
non-overlapping and include controller work, request-event construction and
durable append, plus small endpoint-sampling overhead. They do not include the
client `evaluate` call. A final unmatched request in an unsealed run is reported
as unavailable; a sealed run with an unmatched request is rejected.
They can include controller work between calls, including action execution,
native polling, verification, checkpoint writes, observation, and loop sleep;
the window is not an API-only delay.

The corresponding `model_request` may also include the prior decorated-step
timing context. Exclusive phase rows partition that step; inclusive rows show
nested measurements and are not additive. The between-step gap is divided into
intentional sleep and other measured time. Checkpoint file and directory sync
phases are nested within checkpoint writes. Research-event redaction,
validation, serialization, hashing, and fsync are nested within event append.
Each response also records wall, process-CPU, and thread-CPU clock samples
around the client `evaluate` call, including small endpoint-sampling overhead.
This is the client operation boundary, not an isolated provider-server or
network measurement. The inter-request window is a separate
non-overlapping interval, but it overlaps the prior/current step and gap views;
do not add those distributions together.

`wall_ns` is elapsed monotonic time. `process_cpu_ns` includes Python work on
all threads in the process; `thread_cpu_ns` covers only the sampling thread.
CPU use is not a measure of waiting and cannot identify provider-server time,
network delay, Factorio-server CPU, or host scheduling as a cause. Missing or
regressing clocks remain unavailable instead of being estimated. Incomplete
phase partitions remain unknown.

The report is an attribution aid, not native acceptance evidence. It does not
prove that a source change is deployed, that gameplay improved, or that a
particular residual is network or API latency. Compare source pins and native
run evidence separately, and use the profiled run to identify which measured
boundary needs a focused follow-up.
