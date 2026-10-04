# Fresh native attachment recovery

The October4 continuous game installed current `factory.lua`, which includes
connector ownership in its normal observation. Its native manifest had
`profile=false`; solid/coal treatment modules and the legacy observer bridge
were absent. The generic attachment check nevertheless required that bridge,
so an ordinary controller restart would fail before resumed gameplay.

For the exact current module set (fair actions, factory, launch readiness,
observation/v2, craft jobs, output buffers, input routes, production sites,
mining outposts and connector ownership), attachment now first verifies all
native callback identities and installed Lua hashes. A separate read-only native
command then compares the normal factory snapshot with the direct connector
ledger in the same tick, bound to the original session and actor. Only an idle,
empty connector ledger qualifies. No bridge, callback, receipt, actor binding
or native qualification marker is installed or rewritten.

Legacy migrated profiles and nonempty connector ownership retain their existing
qualification/reconciliation requirements. A changed source asset, unexpected
module, replaced callback, foreign session, active transaction, paid route or
mismatched snapshot fails closed. This repair does not reconstruct a runtime
lost in a server restart or reconcile a world save older than the checkpoint.

Validation:34 focused attachment tests passed, including executed Lua checks.
A read-only candidate probe of session `a0072f75985343a8abd9e73993d0c5d5`, actor2543,
qualified matching empty snapshots at tick6787481 with no bridge installed.
That probe is attachment evidence, not proof of resumed useful gameplay.
