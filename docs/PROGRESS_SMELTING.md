# Observe smelting while foreground decisions wait

During the V40 campaign, the controller repeatedly waited for iron or copper
plates after a verified furnace input transfer. Its foreground strict JEV
decision was blocked while native furnaces continued producing. The observer
already distinguished advancing craft and research counters from a stalled
foreground; it did not yet track furnace production.

The read-only observer now accepts the same paired, recent, accepted native
observation contract for registered iron, copper and steel furnaces. It records
machine unit, name, role, recipe, force, observation tick and completed-batch
counter. Two consecutive observations must show a counter increase on the same
machine. A new machine, timestamp or world tick alone is not progress.

The display reports smelting only while the furnace remains actively crafting
and its counter has advanced within the existing stall threshold. It retains
the foreground hold reason and original blocked-since time. Missing or stale
records, stopped counters, recipe/identity changes, native uncertainty, unrelated
holds and stopped processes cannot acquire smelting credit. A long observation
gap requires a new baseline.

This does not increment the completed-action watermark, refresh the last
verified-action time, restart anything, or certify useful 24-hour operation.
The accepted V40 observations in native-v40-smelting-progress.json are a
regression fixture, not a substitute for production readback.

For rollout, retain the signed owner/execution pin and bounded event-log reader,
copy only the three furnace roles and fields consumed by smelting_sample,
then stage and sign a separate observer release. Verify two advancing native
samples and actual OBS Program output. The game controller and its acceptance
window do not need replacement.
