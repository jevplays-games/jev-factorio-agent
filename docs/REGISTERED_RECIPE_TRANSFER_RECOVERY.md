# Registered recipe machines and transfer evidence

After PR #448 recovered the shared-pole connection, the original campaign
gathered coal, fueled its boiler, loaded its copper furnace, and collected
copper plates. At tick 12949035 it held a proposed transfer of 20 copper plates
to cable assembler 2580: `Candidate evidence insufficient`.

The native observation showed the registered `recipe:copper-cable` machine,
its selected recipe, empty input, carried plates, and electrical network 17.
The refill evidence incorrectly required an owned `production_sites` entry.
That module surveys ore-smelting cells; it does not enumerate every registered
recipe machine. Output-pickup evidence had the same assumption.

Transfer evidence now accepts the current registered electric recipe role,
with exact unit, selected recipe, current native tick/session, and matching
native machine category. The independent model-facts catalog includes the
machine prototype for output pickups as well as refills. Ore furnaces still
require their owned site entry. An explicit conflicting site record cannot
fall back to registration. Current registration is not evidence of historical
payment, future output, or completion.

Keep the existing checks for actor binding, inventory, amounts, planner path,
recipe eligibility, planned receipt, and fresh post-dispatch verification. JEV
still decides usefulness and the final choice at the unchanged thresholds.
The original rejected answer remains rejected in regression tests.

For this boundary, capture the rejected request, accepted observation and
matching answer before changing anything. Compare the native recipe role with
the actual scope of the site registry. Qualify both input and subsequent
output handling through the composed controller, using its real request-byte
limit. Preserve the checkpoint and decision ledger; after review and merge,
use the existing source-change admission for a genuinely changed decision
contract. Do not clear the blocked state, replay old answers, or refill the
machine manually. Verify new JEV-approved work and actual OBS Program output.

The fixture retains the original accepted observation and request. Its native
catalog was captured read-only later in the same session at tick 12966656;
tests verify the researched technology set is unchanged. The later output
pickup is explicitly a simulated scenario. Production deployment and extended
stability remain separate checks.
