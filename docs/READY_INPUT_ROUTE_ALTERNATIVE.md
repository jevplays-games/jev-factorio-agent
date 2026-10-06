# Preserve production when an optional route is ready to build

V38 completed the tracked gear and burner-inserter crafts repaired in PRs
[#510](https://github.com/jevplays-games/jev-factorio-agent/pull/510) and
[#511](https://github.com/jevplays-games/jev-factorio-agent/pull/511), then assembled
25 belts and 15 coal. At tick 17643170 the proposed input route became buildable.
Strict JEV rejected that construction action. Its other candidates were
speculative lookahead work, and the controller settled blocked again.

This is an uncovered transition in
[#282](https://github.com/jevplays-games/jev-factorio-agent/pull/282), which exposed
manual production beside a proposed route's kit acquisition. Its trigger used
the kit marker. The first build no longer has that marker, so completing the kit
removed the ordinary production alternative. A fresh planner that defers only
unstarted input-route investment derives a valid transfer of 20 carried iron ore
to the owned furnace for the current 20-logistic-science-pack objective.

The planner now recognizes the first build of a current, unpaid proposed route
as the same optional investment boundary. It preserves the construction proposal
and offers ordinary production before lookahead candidates fill the bounded
frontier. The worker inherits the existing capital-planning mode, so it cannot
replace an ordinary continuation with previously excluded capital proposals.

Building, ready, faulted or paid route states do not gain this alternative.
Native ownership, geometry and observation validation remain in force. This
change does not approve construction, choose manual work automatically, change
strict JEV confidence, clear failures, or modify native dispatch checks.

For efficient diagnosis, compare the frontier immediately before and after kit
completion, not just the first rejected kit prerequisite. Replay the accepted
observation through the full BackgroundWorkLoop composition. Confirm the manual
candidate has the current parent recipe path, passes independent evidence
validation, and survives the request's byte and candidate limits. Exercise
building and malformed paid-proposal states before deployment.

The regression capture is `tests/fixtures/native-v38-ready-input-kit.json`.
`tests/test_ready_input_route_alternative.py` is offline, with no model calls or
actor dispatch. Preserve the previous rejection and acceptance samples during a
changed-source handoff. Require a new strict JEV choice and native verification;
offline candidate availability alone does not prove sustained gameplay.

Related: [#251](https://github.com/jevplays-games/jev-factorio-agent/issues/251)
and [#495](https://github.com/jevplays-games/jev-factorio-agent/issues/495).
