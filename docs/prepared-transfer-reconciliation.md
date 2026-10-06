# Prepared native transfer receipts

## Current bundled transfer endpoint

For ordinary native factory roles, `NativeFactory` records an `approach` phase,
then a `transfer_rpc` phase around the Lua `campaign.transfer` call. The special
qualified bootstrap-output extractor uses a separate endpoint and is not
covered by this inventory map. See the [Lua endpoint](../src/jev_factorio/lua/factory.lua#L253-L303)
and its [Python adapter](../src/jev_factorio/backends/native_factory.py#L387-L408), plus the
[controller's receipt-recovery gates](../src/jev_factorio/controller.py#L1228-L1380).

The regular endpoint reads insert material from the bound agent's
`character_main` inventory. For extraction, it reads the machine's output
inventory, falling back to the machine's chest inventory, and inserts into
`character_main`. Insert destinations are selected in this order:

1. Coal for a burner machine goes to its `fuel` inventory, including coal for a
   furnace.
2. Other furnace inputs go to `furnace_source`.
3. Lab items go to `lab_input`.
4. Assembling-machine and rocket-silo items go to
   `assembling_machine_input`.
5. Other burner-machine items go to `fuel`.

Before changing inventories, Lua checks that the receipt key is unused, the
bound player can reach the machine, the selected source contains the requested
quantity, and the selected destination exists with room for the full requested
quantity. In particular, a short destination fails before `source.remove`,
`target.insert`, or receipt assignment. That endpoint-level rejection has no
transfer effect and creates no receipt.

After preflight, Lua asks the source to remove the requested amount and inserts
the amount actually removed. If the destination accepts less, Lua attempts to
return the difference to the source. When that refund succeeds, it stores a
receipt containing the inserted quantity, role, item, machine unit, direction,
and tick, then raises the
`Partial transfer; inspect receipt before continuing` assertion. A failed
refund raises before receipt assignment, after the source removal and partial
insertion; the missing receipt therefore does not prove that nothing moved.
Receipts are kept in a bounded 128-entry Lua order. The endpoint's plain Lua
assertion is not a typed Python capacity-rejection result: the controller still
handles generic transfer exceptions conservatively, and an error string or
missing receipt alone does not authorize replay or classify nonmutation. Typed
capacity classification and transfer-budget behavior remain separate
followups in [#478](https://github.com/jevplays-games/jev-factorio-agent/issues/478)
and [#479](https://github.com/jevplays-games/jev-factorio-agent/issues/479).

The controller's current behavior is narrower than the endpoint's local
preflight fact. It verifies a complete matching receipt through the ordinary
postcondition path. An incomplete receipt is reconciled only for the original
new attempt and matching plan step, action, receipt, role, item, direction,
pinned machine unit, and receipt tick. The current FLE observation must also
report `player_bound: true`. The transfer receipt has no session or actor field,
and the controller does not compare an actor unit with the receipt; session
identity is checked separately between checkpoint memory and the current
observation. An additional source-retained inventory/reservation check applies
only to a zero-quantity receipt, not to a positive partial receipt. An accepted
incomplete receipt fails the plan and replans without calling the transfer
endpoint again. After `transfer_rpc` is entered, arbitrary exceptions, lost
replies, or absent and mismatched receipts remain ambiguous. Do not interpret a
generic capacity exception as a controller-proven safe retry. This
documentation describes the present code and does not claim native acceptance.

## Prepared-dispatch recovery

Native insert and extract actions write a prepared attempt to the checkpoint
before dispatch. The adapter then checkpoints `approach` and `transfer_rpc`
phase changes as they happen; the outer dispatcher changes
`pending.dispatch` only after the adapter returns or throws. A process can stop
after a durable `transfer_rpc` phase and before that outer transition, leaving
the write-ahead row at `prepared`.

On resume, the controller first runs the ordinary full transfer verifier. An
exact full receipt therefore remains an ordinary `verified` outcome. If the
full transfer is incomplete, a prepared attempt may reconcile only when its
durable transfer-RPC phase has a valid `started`, `returned`, or `failed`
status, and the fresh observation confirms all of the following:

- this is the same new attempt, plan step, receipt, role, item, and direction;
- the controller's observation path has matched checkpoint session to current
  snapshot session; the transfer receipt itself has no session field;
- the current FLE observation reports `player_bound: true`, meaning the fair
  player is bound to the stored agent character, and the current machine unit
  matches the unit pinned to the attempt. The receipt carries no actor-unit
  identity, and the controller does not compare one against it;
- the receipt quantity is an integer from zero up to (but not including) the
  requested quantity, and its tick is between the attempt start and current
  observation ticks;
- only for a zero-quantity receipt, a zero-quantity insert still has every
  reserved source item, or a zero-quantity extract still has its complete
  source output. This retained-source/reservation condition is not applied to a
  positive partial quantity.

The phase records that dispatch reached the RPC stage; it never establishes
the transfer effect by itself. The matching receipt is checked against the
original attempt and current role, item, direction, machine unit, and tick; the
fresh player-binding and pinned-machine checks separately gate reconciliation.
The current Lua transfer endpoint writes a partial receipt before raising. Its
partial and zero outcomes normally finish with a durable `failed` phase. A
successful full endpoint call can finish `returned`; a process loss after that
saved phase can still leave `pending.dispatch` at `prepared`. The reducer does
not depend on one phase status when the exact effect is independently
verified.

A partial or zero receipt ends the original attempt with an explicitly
incomplete reconciliation outcome, retains its receipt and history, charges
the existing plan failure budget once, and replans the remaining work. It does
not mark the step successful and does not call the transfer endpoint again.
Unchanged ambiguous attempts retain their existing behavior.

A prepared action with no transfer-RPC phase keeps the separate one-time retry
path only when no receipt exists and the action-specific source condition
still holds: an insert's full reservation and inventory are retained, or an
extract's requested output remains at its machine under the empty-reservation
condition. Absence alone does not authorize that retry: it requires this
transfer step's original new FLE attempt for the same action and receipt, a
dispatch phase of `started`, an approach phase of `started` or `returned`, no
transfer-RPC phase or observation error, `player_bound: true`, the matching
pinned machine unit, and a currently allowed step. Outside that narrowly gated
no-RPC path, missing or mismatched receipts, future or pre-attempt ticks,
observations with `player_bound` false, changed machine units, legacy attempts,
and non-FLE observations do not reconcile the effect. Pending ownership
remains; when the pending poll or timeout budget expires, the controller marks
an unresolved non-idle action uncertain rather than replaying it. A malformed
phase or session mismatch is rejected during checkpoint validation or by the
controller's current-observation session check before another dispatch.
These offline tests exercise the real `NativeFactory` phase wrapper
and Lua transfer function through an in-memory Lua runtime; they do not claim a
live Factorio run or native acceptance.
