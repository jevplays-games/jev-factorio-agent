# Opt-in complete solid and coal treatment

The production CLI and supervisor accept `--production-treatment /absolute/path/treatment.json` for a reviewed, explicit solid-route and coal-source configuration. The file is versioned and immutable for a supervised campaign:

```json
{
  "schema": "jev-factorio.production-treatment.v1",
  "solid_intents": [
    {"source": "coal:burner-a:chest", "target": "burner-a", "item": "coal", "destination": "fuel"},
    {"source": "coal:burner-b:chest", "target": "burner-b", "item": "coal", "destination": "fuel"}
  ],
  "coal_targets": ["burner-a", "burner-b"],
  "solid_science_policy": false,
  "coal_kit_policy": false
}
```

These names are illustrative. An owner must choose roles from a qualified native observation and predeclare any additional disjoint downstream input intents. Coal targets require their exact dedicated corridors. The controller still checks current native identities, payments, power, receipts, and flow before action. `coal_kit_policy` enables the existing explicit-bundle kit acquisition; it does not claim autonomous payback admission. Unknown economic cost remains a blocker for #101.

For an isolated economic-admission qualification, use schema `jev-factorio.production-treatment.v2` with the same fields plus `"coal_economic_admission": true` and `"coal_kit_policy": true`. V2 binds the admission flag into the canonical digest, run manifest, gameplay configuration, and coal checkpoint schema 2. It requests native coal observation protocol 2; current evidence remains unqualified and the controller defers new optional kits. A v1 checkpoint cannot be upgraded in place. The complete-capture path retains admission evidence and requires protocol 2 for this treatment.

The CLI requires hierarchical FLE ready-work, a production goal, and a checkpoint. It validates the treatment and, on resume, the complete composed checkpoint before attaching FLE. A legacy checkpoint cannot silently acquire this treatment, and removing the treatment on resume is rejected. The supervisor stores the canonical treatment SHA-256 in its immutable gameplay configuration and checks the file again at launch. The research manifest records the same digest when `--run-dir` is used. Fresh isolated native qualification and an owner-approved immutable handoff remain required before a production campaign can use it; this command does not migrate an existing campaign or extend its cutoff.

`python -m jev_factorio.complete_capture` creates a private `jev-factorio.complete-capture.v3` bundle from a stopped gameplay log, predeclared `jev-factorio.integration-trial.v2` or `.v3`, preflight report, initial/final checkpoints, and save. V3 preserves the composed solid controller's `solid_funding_schema=1` and `solid_funding` record on every emitted row, including an explicit `null`; active funding is validated against its declared intent and the matching native proposed route in both observations. It also requires paid solid ownership to remain a retained prefix and binds the first/final native route layout, source, target, item, paid parts, and receipts to their checkpoints. The source-tick, preflight, and actor checks apply to every before/after observation.

V2 capture archives remain byte-for-byte historical evidence. V3 verification rejects a v2 manifest as an unsupported capture version; it does not relabel, rewrite, or infer missing funding and route-continuity evidence. Use the historical v2 verifier for archived v2 bundles. Existing controller producers already emit the v3 funding fields; the v3 allowlist validates and preserves them rather than requiring producer changes. Fixtures must model the producer's schema and native adapter presence explicitly. Re-capture requires the original inputs and creates a new bundle; it never overwrites an archive.

The trial binds the treatment digest, ordered intents, coal targets, source and model declarations, workload, capacity profile, save/checkpoint digests, VM labels, and original cutoff. The read-only preflight can validate a composed solid/coal checkpoint, but its fixed native query does not inspect those paid owners. It reports `solid_preflight_not_supported` and `coal_preflight_not_supported`, and cannot mark this treatment ready by itself. The capture requires those explicit unknowns and matching checkpoint/VM labels; a separate native ownership/recovery qualification is required. The projection retains paid route/source ownership, receipts, coal/solid flow and fault evidence, before/after native observations, and model-call attribution. It rejects unknown ownership fields and missing schema-enabled native transport adapters. `verify()` checks internal binding, checksums, and row count. The existing `acceptance_capture.v1` rejection boundary stays in force for older trials. Neither capture nor `integration_evidence` grants native acceptance or deployment authority; the external gates and independent raw-evidence review remain required.
