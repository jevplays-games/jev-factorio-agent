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
deadline, attempts and failures. No Lua changes or policy fallback are involved.
