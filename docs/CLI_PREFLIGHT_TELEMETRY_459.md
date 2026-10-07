# CLI preflight and run telemetry

The hierarchical CLI treats checkpoint selection as an admission boundary.
Source compatibility, treatment binding, retained recovery/archive state, the
selected composed checkpoint schema, and the decision client are validated
before it creates a `ResearchLog` or opens dashboard `EventWriter` output.
The selected checkpoint byte capture is then bound through the composed memory
type's constructor checks and first restore. The full composed loader validates
that capture, including any content-addressed recovery archive, and the CLI
rejects a path replacement before backend attachment when it can detect one at
the writer boundary. A later replacement is rejected at restore or before the
first controller checkpoint write; a different valid file is never restored
over the selected state. A preflight-rejected invocation leaves an existing
dashboard run unchanged and does not create dashboard or research run files.

After admission, the CLI opens the optional research and dashboard writers and
continues through backend and controller initialization. Once initialized, the
normal `run_started` and terminal events are preserved, including an error from
a run that has already crossed that boundary. Async provider cleanup still runs
before either writer is sealed. Setup-timing output is registered only after
admission, so an invalid selected checkpoint cannot leave a partial timing
artifact.

When setup timing is enabled, its ordered boundaries record preflight completion
before research and dashboard writer readiness. A writer-creation failure can
therefore retain the completed preflight interval while leaving setup partial;
rejected preflight still creates no timing artifact.

The regression tests use the public `python -m jev_factorio` entry point with
the mock backend. They check rejected and admitted runs, existing and absent
dashboard paths, research logs, and a controlled post-admission failure. These
tests also replace the checkpoint with valid and malformed bytes at writer and
first-observation boundaries, verify that no action consumes the replacement,
and retain an unchanged-checkpoint resume control. These offline checks do not
establish native Factorio acceptance.
