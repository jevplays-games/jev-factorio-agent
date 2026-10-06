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

## Efficient diagnosis and recovery

1. Read the owner status, current checkpoint and last settled decision. Distinguish
   a dead process from rejected assessment, rejected choice and stale evidence.
2. Retain the accepted native observation, exact offered plans, answers and
   failure counters. Limit guest-agent responses; never return entire logs.
3. Replay candidate compilation offline with the composed production planner,
   retained capital failures and the changed-source planning boundary. Inspect
   each rejection and the recipe or ownership proof it lacks.
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
