# September 25 campaign throughput implementation

Implements the code-side recommendations in the
[production progress analysis](https://github.com/CompleteDotTech/jev-factorio-agent/blob/evidence/campaign-progress-20260925/docs/FACTORIO_PROGRESS_ANALYSIS_2026-09-25.md),
against analyzed revision `a88963db68031b61988effb1bbe655667caa12bb`.
The evidence branch stays analysis-only. No running game, save, production
configuration, supervisor cutoff, ownership record or failure budget is changed
by this source integration. **Native improvements are not yet established.**

## What is implemented

| Priority | Implementation | Remaining native acceptance |
| --- | --- | --- |
| P0 observations | Content-free RPC/helper/payload/decoding profiles, optional single-RPC campaign plus resource discovery, native scan/discovery/serialization timers, validated positive discovery cache | Matched save/configuration observation p50/p95; actor/session/depletion/receipt fault matrix |
| P0 current science | Optional current-chain priority and bounded reserve targets from recipe estimates plus demand-to-verified-lab-receipt lead samples | Sustained positive required science consumption and fewer empty inputs over a recorded window of at least 30 minutes |
| P1 ore automation | Reuse the existing paid mining-outpost capability for the first isolated dev pilot; preserve its receipt, ownership, flow and mutual-exclusion gates | Paid ore extraction, fuel, drill-to-chest topology, verified hauling into the existing furnace and positive smelting; no native pilot has been executed for this change |
| P1 batching/service | Preserve urgent partial science deliveries; existing recipe recursion supplies steel/cable/circuits/engines; reserve/stack/remaining caps; same-cell rejection diagnostics | Lower decisions per useful consumed science, no starvation or duplicate transfers |
| P1 monitoring | Session/process-local 30-minute rolling progress, science consumption/delivery rates, starvation/repair/wait/travel/observation classifications | Observe alerts against a real stalled and a progressing campaign |
| P2 eligibility | Concrete rejected background candidates; current coverage/lead/margin; blocked plans, exact active step and retained paid role; optional collection-only research lookahead | Measure missed safe opportunities before enabling the coverage-margin experiment |
| P2 host | Synchronized, allowlisted Linux memory/swap and CPU/memory/I/O pressure counters | Correlate native/RPC samples with host pressure; change one scoped host resource in dev, never infer paging from elapsed time alone |

## Explicit options and safe defaults

All five new options default to disabled. Existing commands retain the previous
observation and planning paths. Both the controller CLI and supervisor support:

* `--campaign-diagnostics`: top-level progress, eligibility, investment and host
  evidence without adding diagnostics to Jev's model-facing snapshot.
* `--profile-observations`: hierarchical FLE observation timing; implies diagnostics.
* `--consolidated-observations`: **dev pilot**, implies profiling and diagnostics.
* `--lead-time-supply`: **dev pilot**, requires `--factory-scheduling ready-work`
  and implies diagnostics. Supply priority applies to the current rocket research chain.
* `--coverage-margin-lookahead`: **dev pilot**, requires lead-time supply and
  at least two valid lead samples for every current science type before it can
  admit partial-coverage lookahead.

A supervisor normalizes implied flags, persists enabled treatment fields,
forwards them on every gameplay launch, and records them in repair instructions.
Its existing gameplay configuration remains immutable. Enabling or disabling a
pilot requires an authorized, stopped, audited handoff preserving the original
session, checkpoint and deadline. Do not edit supervisor JSON or reset budgets.
Older all-disabled supervisor configurations remain comparable without adding
new false-valued fields. Research manifests explicitly record all new flags;
older manifests that omit them remain readable.

## Observation contract and limits

The opt-in `ObservedFactory` retains the original `NativeFactory.execute`, fair
actor guard, fresh pre-dispatch observation, inventory reads, authoritative
receipts and post-dispatch verification. It calls the current decorated
`campaign.observe` at every observation, preserving buffer/route/outpost/successor
extensions. It does not cache inventories, production counters, research,
receipts, player state or the campaign snapshot.

The consolidated native read returns the campaign snapshot and the five solid
resource targets together. It removes redundant preliminary coal/iron discovery
passes. Water/oil and FLE inventory/entity helper reads remain fresh. Positive
resource discovery alone is cached for at most 1,800 native ticks. Each reuse
checks native entity validity, minability, remaining resource, available unit
identity and normal cursor selectability. A chest or other entity obscuring a
previously cached node invalidates that hit before a plan is committed. Discovery
may probe cursor selection, but never starts walking or mining; harvest still
uses the committed first target and enforces native reach and selection.
Session, character, surface, exploration radius, epoch/tick regression
and attempted topology/mining operations invalidate reuse. Absence is never
cached. External construction does not turn a still-valid resource location into
a mutation authorization; TTL bounds discovery refresh and execution still
checks native reach and identity independently.

The existing catalog is loaded once by `NativeFactory`, not each observation.
The cache never widens the furnace input-route survey or creates terrain. The
campaign's existing force-wide scan still exists and is now measurable; this is
not a claim that its complexity has been eliminated. Consolidated payloads are
limited to 8 MiB and malformed/ambiguous identity envelopes fail closed.

`observation_profiles` distinguish RPC, opaque FLE helper, native and JSON decode
costs. Native `LuaProfiler` values are optional: unsupported/localized output is
unknown, never zero. RPC time includes server and transport; helper/native/RPC
values are inclusive and **must not be added to the disjoint controller phases**.
FLE helpers are timed even if they retain a private original RCON reference.
`local_unattributed_ns` is elapsed residual, not CPU time; decoding covers the
instrumented envelopes, not every library's internal parser. Existing performance
metrics continue to expose checkpoint I/O. No Lua commands, responses, addresses,
credentials, environment values or native error strings enter these profiles.

## Supply and coverage policy

`ReplenishmentHistory` starts a clock when current science becomes due and ends
it only at an independently satisfied lab-transfer receipt. Replayed attempt IDs,
wrong native units, missing receipts, ambiguous results, and other sessions do
not create delivery or timing credit. Game ticks and monotonic wall seconds are
recorded separately. At most 16 samples per item are retained; stale samples do
not authorize lookahead. Samples are process-local and start cold after restart.
This deliberately measures complete replenishment opportunities, including
observation and travel, rather than only the fast transfer RPC.

Lead-time reserve targets cover estimated/measured lead plus declared safety,
never exceed 200, the item stack size or remaining current research, and fall
back to the existing bounded behavior when evidence is unknown. A carried urgent
one-pack tail is delivered immediately, not withheld until a larger batch is
ready. Dependency work reuses `_need`, `SupplyLedger`, the current capability
planner, reservation guards and per-transfer receipts; it is not a second recipe
or concurrent mutation engine. Same-cell visits remain bounded and never spend
expected collections. Diagnostics explain inventory/reservation, geometry,
deadline, duplicate, precondition and step/time-budget rejection.

The ordinary fully-fed future-research gate remains the default. The optional
partial-coverage gate protects every current pack's measured lead plus 600 safety
ticks and a 900-tick service allowance. It can **only extract already-produced
next-stage science at bounded known geometry**. It cannot craft, build, gather,
start/cancel research, append ingredient deliveries to a visit or expand a
speculative ingredient frontier. Unknown coverage or insufficient verified samples
rejects it. This narrow pilot does not promise that arbitrary future work is safe.

## Progress evidence and offline reports

`campaign_progress` uses a 1,800-second monotonic rolling window with at most 4,096
observations. Session/tick/clock/counter regressions start a new window. Missing
counters are unknown, not zero. Alerts are not emitted before the full window;
unknown research progress is not declared absent progress. Rates use consumed
science and independently verified lab deliveries, not action counts. The
wall-time denominator includes controller delays and is not machine capacity.
Stock transfers alone are not new material production. Fish collection is not a
rocket victory. Activity classification is a diagnostic priority, not exclusive
causal attribution. Nothing in the monitor retries, unblocks or stops gameplay.

Run offline on a single process/session/revision/treatment segment:

```sh
python -m jev_factorio.campaign_report /private/trial/gameplay.jsonl > /private/trial/report.json
```

The report rejects mixed epochs/treatments, malformed timestamps and incomplete
lines. It reports disjoint observation/dispatch phases separately from inclusive
RPC/helper/native aggregates. Counter regressions or missing telemetry make the
corresponding rate unknown. A recorded interval must reach 1,800 seconds: setting
a nominal half-hour duration alone does not ensure that timestamp span.

Measurement eligibility requires the exact schema-1 campaign treatment emitted
by the controller: `schema` plus the four boolean flags for lead-time supply,
coverage-margin lookahead, profile observations and consolidated observations.
Paired reports must use distinct valid treatments with exactly one flag changed,
and each arm must have a distinct valid SHA-256 capture fingerprint over the
raw JSONL bytes consumed during that report's parse. The digest is accumulated
from the same stream read as the measurements, so a later replacement of the
path cannot rebind a report to different bytes. Model-call
records require a nonempty resolved model that agrees with the decision's
`model_called` marker; resolved identities must remain stable within each
capture. Deterministic no-call records and older captures without model identity fields remain
supported. Source revisions must contain only a lowercase 40-hex commit and a
lowercase 64-hex source fingerprint.

A matched comparison also hashes the two retained **initial** save files:

```sh
python -m jev_factorio.campaign_report /private/treatment/gameplay.jsonl \
  --baseline /private/baseline/gameplay.jsonl \
  --baseline-save /private/baseline/initial-save.zip \
  --treatment-save /private/treatment/initial-save.zip \
  > /private/comparison.json
```

The files are operator-supplied evidence, not remote-world attestation. Comparable
rates do not prove causation, receipt safety, or absence of production interference.
Both arms must record a nonempty process identity and the same commit and source
fingerprint. Use the same instrumented revision with different feature flags;
a source change makes the comparison ineligible even when the save files match.
Both report modes always return `native_acceptance_proven: false` and
`deployment_authorized: false`. Existing acceptance captures now retain the
campaign treatment marker; the older ore-successor-specific acceptance gate
explicitly rejects these unreviewed treatments instead of silently accepting them
as an old configuration. Preserve raw evidence privately and use the existing
research hash-chain/capture tools for provenance.

## Development trials, in order

Use the existing [native acceptance boundary](NATIVE_ACCEPTANCE.md): isolated
matching live dev session, application-consistent paired save/checkpoint, one
writer, pinned source/model/policy and normal speed. A failed preflight is a
blocker, not permission to initialize, adopt, reset or replace the world. Existing
preflight tooling does not authorize post-extension outpost commitments; do not
weaken that gate to make an outpost trial green.

1. Run the instrumented baseline with diagnostics/profiling. Compare the same
   instrumented commit with only consolidated observations enabled. Keep three
   fresh observation phases and fault-test actor, session, depletion, topology,
   malformed responses, lost acknowledgement and durable receipt identity.
2. With observation configuration matched, compare lead-time supply disabled
   versus enabled over at least 30 recorded minutes. Require sustained useful
   red/green/blue consumption, fewer empty-input observations, improved science
   per actor-minute and per decision, and no recipe starvation or duplicate spend.
3. Pilot **paid mining outposts only**, using the existing
   [outpost workflow](MINING_OUTPOSTS.md), its required buffer/input-route flags,
   and an idle reconciled capability boundary. Keep ore-side successors disabled.
   Prove paid kit receipts, fuel, native drill-to-chest flow, separately verified
   batched hauling into the unchanged furnace, and positive smelting. Inspect
   surviving paid structures. This mode automates mining, not long-haul belts;
   do not increase the local survey radius as a substitute for topology design.
4. Review concrete background rejections and missed opportunities before the
   collection-only coverage-margin experiment. Keep current science protected
   and change no other treatment. Correlate host pressure and subcall timings
   before considering a separately controlled host-resource experiment.

Do not reset old investment failures or replay ambiguous steps. Diagnostics show
blocked plan IDs, active step index, revision, pending dispatch and surviving paid
role; a retry still requires reconciled receipts and a reviewed environment change.
A failed paid structure remains owned. A source merge is not production cutover.

## Offline validation

The new tests exercise Lua cache behavior, malformed snapshots, content-free
profiling, recipe/stack/reservation limits, urgent tails, receipt deduplication,
lookahead leakage barriers, time/counter epochs, report matching, immutable
supervisor treatments and composed native-protocol fixtures with lost outcomes.
They are synthetic and do not replace any native trial above.

```sh
PYTHONPATH=src python -m pytest tests/ --ignore=tests/test_dashboard_browser.py -q
PYTHONPATH=src python -m compileall -q src
```

The browser suite requires the repository's separate Playwright/Chromium setup.
No production settings are enabled by tests or installation.
