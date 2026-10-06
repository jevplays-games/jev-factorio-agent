# Feed the producer before observing buffer flow

The V35 native capture shows a paid, correctly placed and fueled output arm,
an empty iron furnace, seven carried iron ore, and no native flow proof. The
planner proposed only waiting for three positive transport samples, so strict
JEV rejected a wait that could not make progress. The paid buffer branch returned
before the ordinary producer-input planning branch could run.

PR #275 covered installation and arm-fuel start evidence. PR #499 preserves their
current parent recipe purpose. Neither supplies an empty producer before waiting
for commissioning. This is a planning prerequisite error, separate from a stale
observation or low-confidence choice.

Before waiting for unverified flow, compare current source output, held items,
paid furnace inputs and an in-flight craft. Stored chest inventory cannot create
another positive transport sample. If no transportable supply remains, use the
ordinary bounded production planner with the original parent recipe path. Also
service a furnace whose paid inputs cannot advance without fuel. Existing stock
or powered production continues through the unchanged flow-observation path.

The captured case now proposes gathering three ore to reach the existing ten-ore
recipe target, followed by its normal paid transfer. It does not claim transport
has occurred. Native identity, conservation, three positive samples, receipt
checks and the prohibition on prematurely extracting buffered stock are unchanged.
No native Lua changes, policy fallback, new attempt allowance or history reset.

Regression tests replay the accepted native capture and label hypothetical
acquisition, stocked/in-flight producers, fuel starvation and completed flow
separately. Live acceptance must verify the input receipt, actual native flow
proof and subsequent useful progress; mock planning alone is insufficient.
