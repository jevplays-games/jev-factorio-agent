# Paid buffer component demand

The V31r4 campaign made ten automation science packs after the two-stage JEV
deployment, then stopped at tick 17142305 with `all_candidates_rejected`.
The controller and game remained alive. JEV judged the remaining iron-ore
transfer unsupported; the copper lookahead also lacked current start evidence.

The iron action was acquiring an inserter for an already paid partial output
buffer. Its flattened explanatory path was:

    automation-science-pack -> iron-gear-wheel -> burner-inserter
        -> iron-gear-wheel -> iron-plate -> iron-ore

An inserter is not an ingredient of a gear. The earlier cycle fix correctly
allowed separate component acquisition, but left this mixed path in the
decision evidence. The ordinary recursive recipe validator could not qualify
it. Splitting assessment from choice does not repair missing action evidence.

The compiler now retains two distinct demands for this paid construction case:

* The immediate local target is one missing burner inserter. Its input path is
  `burner-inserter -> iron-gear-wheel -> iron-plate -> iron-ore`.
* The parent science demand retains its separate native recipe path, current
  quantities and the source buffer's exact paid parts, units and receipts.

Both are revalidated from current native facts and an independently projected
recipe catalog. The action, quantity, costs and native receipt remain unchanged.
Gathering, pickup and crafting can retain the same component purpose as stock
changes. Utility-power prerequisites keep their existing dedicated evidence.
This does not establish construction, flow, science output or a JEV approval.

V32 then exposed another request inconsistency. After filtering, a science-pack
craft retained the earlier burner-inserter primary objective and construction
instruction. JEV accepted the craft's usefulness but returned choice confidence
0.43, below the unchanged 0.45 floor. The existing controller later resumed
without intervention. The contradictory request is proven; its exact effect on
JEV's confidence cannot be established from that single response.

New scheduling requests version candidate-objective binding. Different component
and production targets receive separate, current candidate entries even when
only one science type is due. After an alternative batch, byte/candidate limit,
or assessment rejection removes candidates, the retained targets are revalidated.
A shared remaining target replaces the removed candidate's objective and wording;
missing or mismatched targets are never borrowed. Qualified shared-parent
comparisons retain their independent evidence requirements. Unversioned saved
requests still reconstruct exactly as originally sent, including any old defect;
answers, hashes and uncertain provider calls are never rewritten or replayed.

After the 20-pack batch reached the lab, another hold occurred before JEV could
assess a construction action. Retained observations differed only in the boiler's
steam telemetry (176.00373578071594 to 175.84818482398987). Comparing the whole
snapshot made normal boiler activity invalidate every new two-stage decision.

New two-stage requests use a versioned projection for the identified boiler's
fluid telemetry: the model and freshness comparison both receive fluid names and
empty/present stock, without numeric quantities. The planner does not consume
these quantities. Raw native observations remain intact, and native action
checks still run against fresh observations. Unit identity, fluid ports, fuel,
status, inventory, every other entity and fluid appearing/disappearing or becoming
empty still invalidate freshness. Malformed telemetry is rejected. Unversioned
saved decisions keep the old exact comparison. This also prevents boiler churn
from authorizing another attempt after a strict rejection; it is not a retry rule.

The resumed fuel frontier exposed two more evidence mismatches. The planner had
correctly promoted a proven boiler-power prerequisite to immediate work, but the
target binder still required its original lookahead scope. Binding version 3
recognizes that promotion only through the existing action-bound power witness;
version 2 records reconstruct unchanged. Separately, furnace-fuel purpose assumed
zero inventory items were present in the native map and every furnace appeared
in an ore-site survey. The existing registered-furnace identity check now supports
the direct steel-to-assembler recipe edge, while omitted zero inventory counts
as zero. Registered identity is labeled separately from surveyed ownership;
neither establishes historical payment or completed output. Captured-frontier
tests retain all action costs and prove stale/conflicting identities still fail.

The next accepted capture found the same missing purpose at the transition from
acquiring the buffer component to placing it. Native start evidence proved the
carried inserter, paid chest and planned placement receipt, but did not explain
the current science demand. `buffer_commissioning_demand` now retains the separate
parent recipe path for placement and direct arm fueling. Its projected witness
recomputes enabled recipes, carried intermediate stock, the leaf shortfall and
current paid identities. It is attached to the exact action, parameters and costs.
The question uses it only alongside the independently qualified native start
proof. Paid history is not measured payback, and placement, fuel transfer, flow
and later production still need their own verification. Already-saved requests
without this marker keep their original questions. Fuel acquisition and waiting
do not inherit permission from this witness.

## Efficient diagnosis and recovery

1. Read the owner status, current checkpoint and last settled decision. Distinguish
   a dead process from rejected assessment, rejected choice and stale evidence.
2. Retain the accepted native observation, exact offered plans, answers and
   failure counters. Limit guest-agent responses; never return entire logs.
3. Replay candidate compilation offline with the composed production planner,
   retained capital failures and the changed-source planning boundary. Inspect
   each rejection and the recipe or ownership proof it lacks. Compare the final
   choice request's objective with every retained candidate's own target; inspect
   both stages rather than assuming their contexts stayed aligned.
   Check the transitions from component acquisition to placement and commissioning;
   a valid construction receipt alone does not establish current parent demand.
4. Verify the corrected request under the actual 48,000-byte bound. Test negative
   ownership, receipt, catalog, stock and demand cases as well as hypothetical
   receipt-qualified continuations. A mock answer is not native acceptance.
5. Publish signed source and use the existing one-use changed-contract admission
   at a quiescent checkpoint. Preserve old answers, archives, receipts, failure
   budgets and the world. Do not reroll the old batch or lower the 0.45 floor.
6. Verify new useful native receipts and the actual OBS Program. Start a fresh
   24-hour observation window after recovery; an alive process or advancing game
   clock does not prove sustained useful progress.

The captured fixture records the accepted observation after the hold. Tests
explicitly distinguish that native capture from simulated future inventory.
The original strict rejection remains retained in the campaign logs. Issue #495
stays open until actual sustained acceptance; merging these changes alone does
not establish that the recurring reliability problem is resolved.
