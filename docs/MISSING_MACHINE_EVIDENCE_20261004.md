# Missing production-machine evidence

On October4, the new continuous campaign gathered36 iron ore and smelted10
copper plates, then rejected its remaining stone5 candidate. The native request
contained `lab -> iron-gear-wheel -> iron-plate -> stone-furnace -> stone`, but
presented that mixed path as recipe inputs. A furnace is a production machine,
not an ingredient of iron plate. The owned copper furnace did not fill the
planner's missing `recipe:iron-plate` role.

The evidence builder now validates each recipe-input edge and the missing
machine edge separately against the current native catalog, unlocked recipes,
carried inventory, production roles, current local target and bounded gather.
It explains the resulting construction prerequisite to the independent choice,
usefulness and benefit questions. Existing eligibility thresholds, native
verification and blocked-attempt accounting remain unchanged.

Validation:128 focused synthetic tests passed initially. A retained-state
`jev-1.13.0` diagnostic with the complete revised contract passed the existing
selection gates. This diagnostic did not execute an action and does not prove
live recovery or days of uninterrupted operation. Its first intermediate
version still failed choice confidence; both attempts remain in private
incident evidence. Deployment must use the changed-contract, exact-checkpoint
recovery gate and preserve the prior rejection ledger.
