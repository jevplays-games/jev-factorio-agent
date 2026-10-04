# Prepared native transfer receipts

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
- the checkpoint reload is bound to the session reported by the current
  observation (the transfer receipt itself has no session field);
- the current FLE observation still has the player bound and the same pinned
  machine unit;
- the receipt quantity is an integer from zero up to (but not including) the
  requested quantity, and its tick is between the attempt start and current
  observation ticks;
- a zero-quantity insert still has every reserved source item, or a
  zero-quantity extract still has its complete source output.

The phase records that dispatch reached the RPC stage; it never establishes
the transfer effect by itself. The native receipt and live owner/machine
identity prove the bounded effect. The current Lua transfer endpoint writes a
partial receipt before raising. Its partial and zero outcomes normally finish
with a durable `failed` phase. A successful full endpoint call can finish
`returned`; a process loss after that saved phase can still leave
`pending.dispatch` at `prepared`. The reducer does not depend on one phase status when the exact
effect is independently verified.

A partial or zero receipt ends the original attempt with an explicitly
incomplete reconciliation outcome, retains its receipt and history, charges
the existing plan failure budget once, and replans the remaining work. It does
not mark the step successful and does not call the transfer endpoint again.
Unchanged ambiguous attempts retain their existing behavior.

A prepared action with no transfer-RPC phase keeps the separate one-time retry
path only when no receipt exists and the exact source remains reserved and
available. Missing or mismatched receipts, future or pre-attempt ticks,
unbound actors, changed machine units, legacy attempts, and non-FLE observations
remain uncertain with pending ownership retained. A malformed phase or a
session mismatch is rejected while loading the checkpoint, before another
dispatch. These offline tests exercise the real `NativeFactory` phase wrapper
and Lua transfer function through an in-memory Lua runtime; they do not claim a
live Factorio run or native acceptance.
