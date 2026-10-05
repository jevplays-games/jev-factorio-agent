# Preserve start evidence when a pickup becomes a service visit

After the current-material repair, the native campaign completed its lab,
placed it, researched steam power, and continued producing. It then held at a
new two-step service visit: collect one ready iron plate and insert five carried
coal. The science target needed that plate, but every start-evidence field was
empty. JEV marked the only offered plan unsupported. No confidence floor changed.

The planner retained `output_pickup` provenance while grouping transfers, but
`_output_pickup_start_evidence` rejected any plan with more than one step. The
existing paid-service proof covered ingredient insert plus fuel, not pickup plus
fuel. The repair extends exactly recompiled paid-service evidence to this second
shape. It does not change the plan, transfer order, scheduling budgets, or native
execution checks.

The producer requires fresh coherent/atomic observation, snapshot-local planner
admission, exact service recompilation, carried fuel budget, owned source,
observed output, and unused receipts. The question builder independently binds
those fields before explaining that the present start decision concerns the
first pickup. Later refueling still requires a fresh check and its own receipt;
no future transfer, science production, or goal completion is claimed.

Emit the additional field only when its proof qualifies. Full-suite replay caught
an initial empty-field addition that pushed near-limit historical packets over
their byte budget and pruned alternatives. Omitting the absent field preserves
those frontiers without increasing limits or removing existing evidence.

## Efficient recovery check

1. Retain the blocked checkpoint, failed response, full offered plan and native
   observation. Look for a service wrapper around otherwise valid pickup provenance.
2. Recompile from a fresh read-only native observation with the normal atomic
   crafting-inventory decorator. Do not manufacture private admission or inventory
   verification flags from serialized annotations.
3. Check exact plan identity, both receipts, fuel affordability, and source output.
   Test missing/changed ownership, quantities, receipts, stock, and dependency paths.
4. A producer-only diagnostic still failed missing-start confidence at 0.51. Keep
   that result. After independent question-side binding, a distinct diagnostic on
   the same facts passed: choice 0.67, usefulness probability 0.91, missing-start
   score 0.43. This is not native action authority or continuous-run qualification.
5. Publish and merge tested source. Use the existing owner admission for one fresh
   evaluation from the exact checkpoint. Verify both native transfers and continued
   useful work, then check the live Program display. Preserve any further hold.
