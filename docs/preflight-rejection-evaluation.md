# Logging-core preflight rejection evaluation

The logging-core evaluator distinguishes a bounded `factory_connect` rejection
from an action whose result is still ambiguous. It accepts the rejection only
when the event follows a matching `action_returned` error and preserves the
prepared action's trace, action, decision, plan, step, attempt, session,
observation, role, and parameters. The return must have the producer's
`invalid_data` error facts, and the rejection must carry a known preflight code,
`action_origin: current_trace`, and `mutation_started: false`. The controller
must be hierarchical. Duplicate, out-of-order, contradictory, missing, or
mismatched rejection evidence cannot resolve an action.

This classification is not an acknowledgment that a backend accepted work.
The action remains `verified: false` with `acknowledged: null`; it contributes
zero verified work. The run summary reports `preflight_rejected_actions`
separately and excludes only these strictly matched rejections from
`unverified_actions`. Generic errors, lost replies, missing rejection events,
and ordinary prepared actions remain unverified. Rejections remain visible in
the event stream and in the correlated action row as `preflight_rejected` and
`preflight_rejection_code`. `preflight_rejected: false` means no valid matching
rejection event was recorded; it does not prove that a missing event could not
have been lost. `preflight_rejected_actions` counts only accepted evidence.

The `runs` projection exports prepared, returned, verified, unverified, and
preflight-rejected action counts. The `actions` projection exports the rejection
flag and bounded code. These are descriptive accounting fields, not paired
performance metrics and not evidence of useful gameplay or native acceptance.
The additive table columns are published as `jev-factorio.tables.v2`; older
reports retain their original v1 schema. Non-logging-core rows that cannot
express this rejection contract export null for its new status fields rather than
an inferred rejection count. A logging-core action without a valid rejection
event remains unresolved and is counted as unverified. JSONL, DuckDB, and
optional Parquet use the same declared columns.

Logging-core hash-chain verification establishes consistency with the retained
source bytes, not authorship. This semantic reducer therefore does not claim a
cryptographically authenticated producer or native-game result; the original
event files and producer provenance remain necessary when interpreting a run.
