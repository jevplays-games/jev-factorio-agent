# Empty boiler stop and completed connector reattachment

At 08:21 UTC on October 5, the continuous campaign stopped at tick 9208817,
immediately after the lab electrical connection verified at tick 9208757.
The persistent monitor detected the controller exit and displayed stopped in
OBS Program. The game and VM stayed up; the owner deliberately held the run.

The controller reason was `Current native boiler identity and coal stock are
required`. A direct read of the existing boiler proved name `boiler`, unit 2550,
and zero coal. Factorio's inventory observer emits a sparse map: empty stock is
`fuel: {}`, not `fuel: {"coal": 0}`. The native boiler branch required the latter,
unlike the furnace branch. Earlier boiler tests always supplied an explicit coal
key, so they missed a newly placed empty boiler.

The planner now treats an omitted coal key in a present inventory map as zero.
Missing/non-map fuel telemetry, invalid counts, and wrong entity identities
remain errors. This only creates the existing bounded coal acquisition/service
candidate; JEV approval and native transfer checks remain required.

Recovery exposed a second defect: direct native attachment accepted only an
empty ordinary connector ledger. This campaign had three completed, fully paid
routes (water, steam, electricity) with 28 cells retained in its checkpoint.
The empty-ledger assertion returned no JSON and prevented reattachment. It was
not an RCON connectivity failure and clearing the ledger would destroy proof.

Resume now passes the connector binding from the already validated checkpoint
capture. For the exact current full installed profile, attachment checks the
source hashes, actor, session, callback chain, and qualified settled factory state;
reads the completed connector summaries; verifies every paid cell and endpoint
against the checkpoint; and checks the summary again after detail paging.
Settled factory state permits the original iron/copper furnaces and strictly
unpaid output/outpost proposals, with matching native entity identities and
bounded known schemas. Output/input/outpost component ownership, pending work,
faults and receipts must remain empty. Proposals are never surveyed, cleared or
adopted by this check. The two snapshot reads must retain the same qualified state.
It installs no Lua and changes no owner, receipt, payment, or world state.
Active, partial, faulted, external, changed, missing, or additional routes remain
reconciliation failures. Other native profiles retain their existing guards.

The craft module also retains its most recent completed job. A `job == nil`
requirement therefore rejects ordinary post-craft resumptions. This path accepts
a completed receipt only when the captured checkpoint contains both its exact
completion event and a unique verified attempt: receipt, plan, output totals and
clock bounds must agree. Native actor identity, paid/full acceptance, completion,
empty queue and unchanged event callbacks are checked independently. A missing
historical binding still requires reconciliation. The check does not credit the
outputs again or require them to remain unspent in the current inventory.

The live preflight found two settled furnaces, two unpaid output proposals, two
unpaid outpost proposals and the completed 38-pole craft. The candidate read-only
qualifier verified that state and all 28 paid connector cells at tick 9355527
without changing the checkpoint. This is attachment evidence, not resumed gameplay.

The exact boiler planner error can be revisited once with the existing
changed-contract recovery authorization. It is **not** added to automatic
recoverable reasons. The original reason is retained in the consumed
authorization; the selection attempt uses the normal null reason before JEV
has judged anything. Existing budgets, fingerprints, attempt history, provider
state, and strict choice confidence are preserved.

## Fast diagnosis and recovery

1. Read the persistent monitor status, owner status, checkpoint, and child result.
   Preserve their bytes/hashes. Confirm the existing owner holds, no controller
   child survives, and all pending/action/background fields are empty.
2. Read the named boiler's native unit and fuel inventory without changing it.
   An existing unit and empty map establish this defect; absent identity does not.
3. Compare native connector summaries and bounded detail pages to the captured
   checkpoint. Never clear receipts, reinstall callbacks, or manufacture a witness.
4. Test and merge the repair. Stage the immutable merged source and signed owner;
   verify native attachment with the checkpoint binding and the composed checkpoint
   parser before stopping the held owner. Preserve the old checkpoint/config.
5. Admit one source-bound recovery under the existing single-writer lock, pinning
   the exact checkpoint hash and terminal result. Start through the same service.
   Never replay an already consumed admission.
6. Verify the same session, retained histories and paid cells, fresh JEV judgment,
   and a receipt-verified useful action. Check the actual OBS Program and monitor
   transitions. A live process alone is insufficient evidence of recovery.

Regression coverage includes sparse empty inventory, malformed telemetry,
checkpoint cell/payment/identity mismatches, actual bundled Lua guards, owner-state
preservation, and one-use recovery with retained attempt history. These checks
do not establish uninterrupted multi-day reliability; ongoing monitoring remains
necessary.
