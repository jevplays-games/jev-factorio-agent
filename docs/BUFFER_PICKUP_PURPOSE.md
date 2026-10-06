# Buffer collection after commissioning

The accepted V36 observation at tick 17479783 has a fully commissioned iron
buffer, one plate in its paid chest, nine carried plates, and an active assembler
investment. JEV refuses the offered speculative alternatives.

Two composition errors remove the useful current candidate. When the ordinary
planner proposes a different investment, the capital frontier rejects it but
previously rebuilt ordinary work only if no investment was active. Retained
lookahead alternatives then hide current work. Recompile ordinary work after a
conflicting proposal even with an active kit, applying the same held-inventory
and failure checks. The existing low-boiler-fuel branch and priorities remain.

Separately, output and input-route planners collect buffered stock before the
base recipe planner attaches its parent path. Preserve that path at the early
collection boundary. Evidence independently checks current session/tick, paid
chest and arm identities, native three-sample conservation proof, ready stock,
receipt identity, and catalog-derived remaining recipe demand. Include the
original furnace recipe in the bounded model catalog so the evidence can be
recomputed from model-facing facts.

Replay the accepted capture with `tests/test_buffer_pickup_purpose.py`. Negative
cases change ownership, stock, flow, timing, demand and catalog facts. A candidate
and its forecast are not completion: strict JEV choice, native transfer checks
and receipt verification still apply. Preserve the investment's original
deadline, attempts and failures. No installed runtime Lua changes or policy fallback are involved.

## Attachment after the first completed buffer

The V36-to-V37 preflight exposed a second boundary: the native input observer
creates an unspent input-route proposal once output flow is proven. Attachment
previously required both input cells and offers to be empty. Its assertion failed
even though no input components were built or paid for. An empty RCON reply then
surfaced as a JSON decoding error.

For checkpoint-owned paid output buffers, qualify these unspent proposals with
read-only guards. Require no input cells or paid parts, exact source and output
identity, completed flow, bounded typed steps and geometry, and no registered
input entities. Compare the proposal snapshot across connector readbacks.
Malformed, paid, partial or changing proposals remain rejected. The qualifier
never calls a stateful survey, clears offers, changes ownership, or installs Lua.
`tests/test_native_unspent_input_attachment.py` executes the guards in Lua and
checks that successful and rejected reads leave the native state unchanged.
