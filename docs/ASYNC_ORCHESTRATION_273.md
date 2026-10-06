# Async orchestration CLI (issue 273, partial stage)

The asynchronous decision path is an explicit, default-off CLI mode for the
hierarchical controller. It does not change the existing synchronous path.
This documentation covers the current CLI and loop integration only; issue 273
has additional provider, lifecycle, timing, rollout, and native-evidence work
that remains open.

## Enabling the mode

Start the CLI from a synchronous process and supply `--async-decisions`,
`--controller hierarchical`, and `--checkpoint PATH`. The checkpoint is
controller state, not a Factorio save. Async mode requires a model-based policy
and cannot be combined with `--reconcile-only`.

The model source is selected explicitly by `--mock-model`, or from the existing
provider credentials: `TYPESAFE_API_KEY` selects TypeSafe; otherwise both
`CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` select the supported
Cloudflare route. Explicit `--mock-model` always selects the offline mock, even
when provider credentials are exported or loaded from `.env`; that path does
not construct an HTTP provider client. With neither mock selection nor
supported credentials, the CLI rejects startup. The Vercel route is not wired
as an async provider.
`--model` may pin a non-empty provider model ID; it cannot be combined with
`--mock-model`.

The default provider decision deadline is 30 seconds. To change it, use
`--async-decision-timeout-seconds N`, where `N` must be finite and in `(0, 30]`.
This option requires `--async-decisions`. The async client is configured with
one in-flight request and no waiting queue. This stage does not expose a CLI
concurrency or queue setting.

`--duration-hours` must be finite and positive, and its conversion to seconds
must also remain finite. Both checks happen before provider or backend
construction. The public synchronous and asynchronous loop methods reject a
non-finite `duration_seconds` value rather than creating an unbounded monotonic
deadline.

For example, an offline controller smoke run can use:

```text
jev-factorio --backend mock --controller hierarchical --target bootstrap_mining \
  --mock-model --async-decisions --checkpoint /path/to/controller.json --steps 1
```

## Loop and client ownership

The CLI creates the async provider and controller, runs the async loop, and
closes the provider on the same event loop. Setup failures use a fallback close
when the async lifecycle coroutine has not started. The async loop preserves
the step limit, duration deadline, terminal checks, transient request retry
rules, and owner step gate of the synchronous loop. Its wait between steps is
cancellable and does not block the caller's event loop. The synchronous
`AgentLoop.run()` remains in place for existing callers.

Cancellation is local process control. It does not prove that a remote provider
cancelled or never received a request. If a request may have been sent, the
controller's WAL, archive, and checkpoint retain the ambiguous ownership so a
later step does not silently issue a duplicate paid request. This CLI stage
does not claim remote cancellation, automatic retry safety for every failure,
or native Factorio acceptance.

The CLI prints a small `async_decisions` status object and includes the same
non-secret mode/provider/deadline/queue information in the controller
initialization event. It does not print credentials or request contents.
When `--dashboard-events` observes an async run, its backend adapter preserves
the existing identity only for a recognized `MockBackend`; async admission then
uses the same mock session and process-local transport lock as the unobserved
backend. Qualified native attachment and transport attributes continue to be
forwarded by the observer. Unknown wrappers do not gain an inferred actor or
transport identity and remain fail-closed.

## Synchronous resume and rollback

A synchronous hierarchical `--resume-controller` cannot discard async decision
ownership by loading only the controller checkpoint. Before provider-client or
backend construction, the CLI checks the selected capture for async ownership
in its decision pointer, settlement/lineage history, WAL/archive paths, and
provider-health decision binding/outcome. Ordinary checkpoints with no async
evidence retain their existing composed validation and authorized migration
order. Async evidence triggers full composed checkpoint validation and checks the
contents and identities of the provider decision WAL, immutable decision
archives and settlement records, checkpoint selection/plan-lineage history,
and provider-health reservation/outcome. The checkpoint bytes are rechecked
after preflight; the later composed resume check confirms the same captured
checkpoint before backend attachment.
Every archive index acquired by this extra rollback validator is closed on
both successful return and rejection. The guard does not discard retained
archives to make an ordinary or async resume pass.

The guard rejects a selected async plan, unresolved same-session provider WAL
entry, malformed or mismatched evidence, an archive without its required
checkpoint disposition, or a retained selected settlement without valid plan
lineage. It also fails closed when settlement evidence is missing or no longer
matches the archive/WAL history. Rejection leaves the checkpoint and sidecar
bytes untouched and happens before the CLI acquires a provider client or game
backend.

Sidecar presence alone is not a reason to reject rollback. Empty archive/WAL
files, unrelated terminal WAL rows, and fully settled async history with
matching retained checkpoint disposition are compatible. A legacy synchronous
checkpoint with no async state remains valid. The CLI does not clear pointers,
delete archives, rewrite WAL records, or treat a sidecar marker by itself as
proof of settlement. A failed validation requires the existing owner to
reconcile the retained evidence; selecting synchronous mode does not authorize
discarding it.

## Current limits

This is a source-level CLI integration stage. The async mode remains opt-in and
does not establish useful-work parity, lower cost, or a performance benefit.
Inter-step async timing is marked invalid/unknown rather than attributed to a
misleading synchronous sleep bucket. Direct provider-wrapper parity,
configuration and operational status surfaces, timing taxonomy, rollback
qualification, staged rollout, and the matched native comparisons required by
issue 273 remain separate work. Mock and offline tests are not native game or
provider evidence.
