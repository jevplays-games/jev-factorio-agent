# Ready-work horizon craft evidence

The V23 campaign remained policy-held after its power witness was repaired.
Its first accepted observation at tick 11401189 exposed a separate mismatch in
the evidence for an offered transport-belt craft. The local target was 20 green
science packs, and the actor already carried 20 belts. The ordinary recursive
material bill therefore had no belt batches. Ready-work's existing bounded
research horizon required 36 green packs and 35 red packs, creating a target of
36 belts and an offered eight-batch craft producing 16 more belts.

The planner attached the horizon marker correctly, but the evidence validator
compared its eight batches against the smaller recursive bill. It dropped the
shared-bill witness despite the matching carried inputs and native craft facts.
This explains that missing witness; it does not prove that it caused JEV's
separate low-confidence choice of the wood action.

The validator now recomputes the existing bounded horizon from the same snapshot,
catalog and supply ledger. It checks the precise ingredient target and recipe
batch count against that recomputation, then retains the existing current-input,
actor, recipe, receipt and output checks. The evidence names the workload demands
explicitly. Forecast stock remains distinct from paid inventory and verified
output. Unsupported expansion, changed research demand, fabricated targets or
altered native costs cannot qualify the witness. No action, candidate generation,
choice threshold or historical answer is changed.

`native-v23-horizon-bill.json` retains the accepted observation, original model
state and rejected answer. Its catalog is the earlier native export already
retained by the capital-power fixture, with the same game version; the regression
recomputes the original 36-belt target and eight batches. Negative cases exercise
stale or altered facts, and replaying the original full answer domain still
returns `low choice confidence`. That offline gate regression uses a larger
request budget to preserve the entire recorded answer domain; a separate test
checks the unchanged 48,000-byte production budget and prefix priority. Added
evidence can cause the existing byte limiter to defer later candidates.

Deployment requires the normal signed changed-source admission at a reconciled
boundary. Do not retry the existing source to obtain a different vote. After
deployment, verify whether JEV actually selects an action and whether native
verification records fresh useful progress. Passing this regression does not
establish either result or uninterrupted multi-day operation.
