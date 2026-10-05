"""Legacy harvest durations are estimates from remaining bounded work only."""

import math

import pytest

from jev_factorio.planning.decision_support import candidate_evidence, scheduling_context
from jev_factorio.planning.ready_work import ReadyWorkPlanner
from jev_factorio.planning.scheduling import (
    RAW_TICKS_PER_ITEM,
    SERVICE_TICKS,
    TRAVEL_TICKS_PER_TILE,
)
from jev_factorio.skills import Plan, Step, compile_plans
from test_factory import catalog, recipe, snapshot


def _targeted_snapshot(item, inventory):
    state = snapshot()
    state.inventory[item] = inventory
    state.nearby_resources[item] = 4
    state.player_position = (0, 0)
    state.factory.setdefault("fair_resource_targets", {})[item] = {
        "position": {"x": 3, "y": 4},
    }
    return state


@pytest.mark.parametrize(
    ("item", "walk_action", "mine_action", "goal"),
    [
        ("coal", "walk_to_coal", "mine_coal", "stockpile_fuel"),
        ("iron-ore", "walk_to_iron", "mine_iron", "bootstrap_mining"),
    ],
)
@pytest.mark.parametrize(
    ("inventory", "threshold", "remaining"),
    [(0, 5, 5), (2, 5, 3), (5, 5, 0), (2, 10, 8), (10, 10, 0)],
)
def test_legacy_mining_estimate_counts_only_unmet_threshold(
    item, walk_action, mine_action, goal, inventory, threshold, remaining
):
    state = _targeted_snapshot(item, inventory)
    plan = Plan(
        f"{goal}:{item}:{threshold}",
        goal,
        "Reach an observed inventory threshold",
        (
            Step(walk_action, "near", item),
            Step(mine_action, "inventory", item, threshold),
        ),
    )

    row = candidate_evidence(state, catalog(), [plan])[plan.id]
    expected = (
        5 * TRAVEL_TICKS_PER_TILE
        + 2 * SERVICE_TICKS
        + remaining * RAW_TICKS_PER_ITEM
    )

    assert row["actor_ticks_estimate"] == math.ceil(expected)
    assert row["processed_units"] == remaining


@pytest.mark.parametrize(
    ("item", "walk_action", "mine_action", "goal"),
    [
        ("coal", "walk_to_coal", "mine_coal", "stockpile_fuel"),
        ("iron-ore", "walk_to_iron", "mine_iron", "bootstrap_mining"),
    ],
)
@pytest.mark.parametrize(
    ("inventory", "remaining"), [(0, 10), (2, 8), (5, 5), (10, 0)],
)
def test_repeated_legacy_thresholds_do_not_double_count_prior_harvest(
    item, walk_action, mine_action, goal, inventory, remaining
):
    state = _targeted_snapshot(item, inventory)
    plan = Plan(
        f"{goal}:{item}:repeated",
        goal,
        "Reach repeated inventory thresholds",
        (
            Step(walk_action, "near", item),
            Step(mine_action, "inventory", item, 5),
            Step(mine_action, "inventory", item, 5),
            Step(mine_action, "inventory", item, 10),
        ),
    )

    row = candidate_evidence(state, catalog(), [plan])[plan.id]
    base_service_and_travel = 5 * TRAVEL_TICKS_PER_TILE + 4 * SERVICE_TICKS

    assert row["actor_ticks_estimate"] == math.ceil(
        base_service_and_travel + remaining * RAW_TICKS_PER_ITEM
    )
    assert row["processed_units"] == remaining


def test_compiled_stockpile_controls_include_raw_work_and_keep_prerequisite_first():
    state = _targeted_snapshot("coal", 0)
    plans, blocker = compile_plans("stockpile_fuel", state)
    assert not blocker

    support = scheduling_context(state, catalog(), plans, "stockpile_fuel")

    assert [support["candidate_evidence"][plan.id]["actor_ticks_estimate"]
            for plan in plans] == [1300, 2200]
    assert support["deterministic_ranking"][0] == plans[0].id
    assert support["candidate_evidence"][plans[0].id]["work_scope"] == "immediate"
    assert support["candidate_evidence"][plans[1].id]["work_scope"] == "lookahead"


def test_factory_gather_keeps_its_existing_raw_work_estimate():
    state, data = snapshot(), catalog()
    data.recipes["lab"] = recipe("lab", {"iron-plate": 1})
    plan = ReadyWorkPlanner(data, state, "rocket_launch")._need("lab", 1)
    assert plan.steps[0].action == "factory_gather"
    assert plan.steps[0].item == "stone"
    assert plan.steps[0].parameters["quantity"] == 5

    row = candidate_evidence(state, data, [plan])[plan.id]

    assert row["actor_ticks_estimate"] == (
        10 * TRAVEL_TICKS_PER_TILE + SERVICE_TICKS + 5 * RAW_TICKS_PER_ITEM
    )


@pytest.mark.parametrize(
    ("item", "walk_action", "mine_action", "goal"),
    [
        ("coal", "walk_to_coal", "mine_coal", "stockpile_fuel"),
        ("iron-ore", "walk_to_iron", "mine_iron", "bootstrap_mining"),
    ],
)
def test_legacy_mining_with_missing_target_keeps_duration_unknown(
    item, walk_action, mine_action, goal
):
    state = _targeted_snapshot(item, 0)
    state.factory["fair_resource_targets"].pop(item)
    plan = Plan(
        f"{goal}:{item}:unknown-target",
        goal,
        "Reach an observed inventory threshold",
        (
            Step(walk_action, "near", item),
            Step(mine_action, "inventory", item, 5),
        ),
    )

    row = candidate_evidence(state, catalog(), [plan])[plan.id]

    assert row["actor_ticks_estimate"] is None
    assert row["travel_tiles_lower_bound"] is None
    assert row["unknowns"]
