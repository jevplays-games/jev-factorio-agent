# Construction judgment investigation (not deployment-ready)

The continuous campaign's signed source `4e29e7d` verified gathering five stone
at tick 6840816, then stopped making useful progress at the following furnace
craft decision. Native inventory contains the five stone required for one
furnace, and the current actor, recipe, crafting queue and receipt protocol
allow the craft to start. The missing role is `recipe:iron-plate`; a different
owned furnace serves `recipe:copper-plate`.

The existing planner path is lab -> iron gear wheel -> iron plate -> stone
furnace. The last edge denotes production infrastructure, not a recipe
ingredient. The deployed gather fix exposes this distinction only for raw
material acquisition. This draft carries the distinction through paid crafting
and placement, discloses native recipes and scoped branch quantities, and binds
the evidence to current session, tick, inventory, exact step and start facts.
Future placement, fueling, output and local-target completion remain unverified.

This is an investigation, **not a proven recovery**. Four distinct read-only
diagnostic requests against the retained frontier using `jev-1.13.0` all
selected `unsupported` for the independent usefulness judgment:

| Revision | Added or changed evidence | P(useful) | P(unsupported) |
|---|---|---:|---:|
| 1 | Native construction dependency edges | 0.30 | 0.70 |
| 2 | Paid native craft recipe and branch demand quantities | 0.26 | 0.74 |
| 3 | Question distinguishes construction from later production | 0.44 | 0.56 |
| 4 | Choice descriptions use the same construction scope | 0.48 | 0.52 |

The last response's candidate confidence was 0.68 and benefit was 0.77, but
neither replaces the independent usefulness choice. No diagnostic dispatched a
game action. No gate threshold, rejection ledger or production treatment was
changed. The draft was not deployed, and the live controller remains blocked
with its original failure history intact. An identical-request reroll is not a
valid next experiment.

Focused local validation: 179 tests passed across construction evidence,
raw-machine evidence, intermediate-craft usefulness, judgments, craft jobs and
executed Lua craft-job tests. These establish deterministic behavior and
negative-case handling, not real-model acceptance or native progression.

Before promotion, identify a further concrete evidence or planning defect,
retain the counterexample and all diagnostic results, and validate independent
judgments without forcing a label. Any subsequent live recovery still needs
signed reviewed source, current native attachment proof, exact-checkpoint
one-use admission through the existing owner, and fresh useful actions across
the construction and production sequence. Passing one craft alone would not
qualify multi-day reliability.
