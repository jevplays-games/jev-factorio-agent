"""Candidate-local objective regressions for independent research work."""
from copy import deepcopy
import json

import pytest

from jev_factorio.judgments import question_batch, select_plan
from jev_factorio.planning.background_work import independent_candidates
from jev_factorio.planning.decision_support import scheduling_context
from test_deadline_scheduling import scenario
from test_factory import recipe


def dual_science_frontier():
    state, catalog = scenario(available=0)
    catalog.technologies["study"]["ingredients"] = [
        {"name": "red", "amount": 1}, {"name": "green", "amount": 1},
    ]
    catalog.recipes["green"] = recipe("green", {"stone": 1})
    state.inventory.update(red=0, green=0, stone=0, **{"iron-plate": 0})
    state.factory["entities"]["utility:lab"]["input"] = {"red": 0, "green": 0}
    plans = independent_candidates("rocket_launch", state, catalog)
    support = scheduling_context(state, catalog, plans, "rocket_launch")
    by_id = {plan.id: plan for plan in plans}
    ranked = [by_id[plan_id] for plan_id in support["deterministic_ranking"]]
    request = {"facts": state.for_jev(), **support}
    return state, catalog, plans, ranked, support, request


def test_ranked_distinct_targets_are_bound_to_their_own_questions():
    _, _, _, ranked, support, request = dual_science_frontier()
    before = deepcopy(request)
    packet, questions, offered = question_batch(request, ranked, max_bytes=48000)

    targets = {plan.materials["local_objective"]["item"] for plan in offered}
    assert targets == {"red", "green"}
    objective = packet["local_objective"]
    assert objective["primary_target"] is None
    candidate_targets = objective["candidate_targets"]
    assert set(candidate_targets) == {plan.id for plan in offered}
    for plan in offered:
        expected = plan.materials["local_objective"]
        assert candidate_targets[plan.id] == expected
        row = packet["candidate_evidence"][plan.id]
        assert row["local_target"] == expected
        for suffix in ("/benefit", "/useful_progress"):
            instructions = questions[plan.id + suffix]["instructions"]
            assert "local_objective.candidate_targets" in instructions
            assert json.dumps(plan.id) in instructions
            assert "advance `local_objective`" not in instructions
    assert "candidate_targets" in questions["candidate"]["instructions"]
    assert "primary_target" in objective["instruction"]
    assert packet["candidate_evidence"] == {
        plan.id: support["candidate_evidence"][plan.id] for plan in offered
    }
    assert request == before
    assert len(json.dumps({"state": packet, "questions": questions},
                          ensure_ascii=False, allow_nan=False).encode()) < 48000


@pytest.mark.parametrize("budget", [{"max_candidates": 1}, {"max_bytes": 20000}])
def test_pruning_rebinds_the_objective_to_the_candidate_that_remains(budget):
    _, _, _, ranked, _, request = dual_science_frontier()
    options = {"max_bytes": 48000, **budget}
    packet, questions, offered = question_batch(request, ranked, **options)
    assert len(offered) == 1
    plan = offered[0]
    expected = plan.materials["local_objective"]
    assert expected["item"] == "red"
    assert packet["local_objective"]["primary_target"] == expected
    assert "candidate_targets" not in packet["local_objective"]
    instructions = questions[plan.id + "/benefit"]["instructions"]
    assert "local_objective.candidate_targets" not in instructions
    assert "local_target" in instructions or "local_objective" in instructions
    assert "green" not in questions[plan.id + "/benefit"]["instructions"]


def test_missing_stale_or_cross_candidate_target_evidence_is_not_promoted():
    _, _, plans, ranked, support, request = dual_science_frontier()
    red = next(plan for plan in ranked if plan.materials["local_objective"]["item"] == "red")
    green = next(plan for plan in ranked if plan.materials["local_objective"]["item"] == "green")
    cases = ("missing", "malformed", "stale", "wrong_goal", "swapped", "all_stale")
    for case in cases:
        local_plans = deepcopy(ranked)
        local_request = deepcopy(request)
        local_red = next(plan for plan in local_plans if plan.id == red.id)
        local_green = next(plan for plan in local_plans if plan.id == green.id)
        if case == "missing":
            local_red.materials["local_objective"] = None
        elif case == "malformed":
            local_red.materials["local_objective"]["inventory_target"] = True
        elif case == "stale":
            local_red.materials["work_intent"]["observed_tick"] -= 1
        elif case == "wrong_goal":
            local_red.materials["local_objective"]["ultimate_goal"] = "stockpile_fuel"
        elif case == "all_stale":
            for plan in local_plans:
                plan.materials["work_intent"]["observed_tick"] -= 1
        else:
            local_request["candidate_evidence"][red.id]["local_target"] = deepcopy(
                local_green.materials["local_objective"])
        packet, questions, offered = question_batch(local_request, local_plans,
                                                     max_bytes=48000)
        objective = packet["local_objective"]
        assert objective["primary_target"] is None
        assert red.id not in objective["candidate_targets"]
        assert "unavailable for this candidate" in questions[red.id + "/benefit"]["instructions"]
        assert "green" not in questions[red.id + "/benefit"]["instructions"]
        if case == "all_stale":
            assert objective["candidate_targets"] == {}
            assert "unavailable for this candidate" in questions[green.id + "/benefit"]["instructions"]


def test_initial_context_does_not_promote_all_stale_targets_to_shared_primary():
    state, catalog, _, ranked, _, _ = dual_science_frontier()
    stale_plans = deepcopy(ranked)
    for plan in stale_plans:
        plan.materials["work_intent"]["observed_tick"] -= 1

    support = scheduling_context(state, catalog, stale_plans, "rocket_launch")

    objective = support["local_objective"]
    assert objective["primary_target"] is None
    assert objective["candidate_targets"] == {}
    assert "missing or mismatched entry does not establish a target" in objective["instruction"]


def test_same_target_frontier_keeps_legacy_single_objective_behavior():
    state, catalog = scenario(available=0)
    plans = independent_candidates("rocket_launch", state, catalog)
    support = scheduling_context(state, catalog, plans, "rocket_launch")
    assert {plan.materials["local_objective"]["item"] for plan in plans} == {"red"}
    packet, questions, offered = question_batch(
        {"facts": state.for_jev(), **support}, plans, max_bytes=48000)
    assert offered
    assert packet["local_objective"]["primary_target"]["item"] == "red"
    assert "candidate_targets" not in packet["local_objective"]
    assert all("advance `local_objective`" in questions[p.id + "/benefit"]["instructions"]
               for p in offered)


def test_actual_offline_selection_uses_the_prepared_candidate_target_context():
    from jev_factorio.jev_client import MockJevClient

    _, _, _, ranked, _, request = dual_science_frontier()
    prepared = question_batch(request, ranked, max_bytes=48000)
    decision = select_plan(MockJevClient(), request, ranked, max_bytes=48000,
                           prepared_batch=prepared)
    assert decision.state["local_objective"]["primary_target"] is None
    assert set(decision.state["local_objective"]["candidate_targets"]) == set(
        decision.state["candidate_plans"])
    if decision.plan_id is not None:
        assert decision.plan_id in decision.state["local_objective"]["candidate_targets"]
        assert decision.diagnostics["outcome"] == "selected"
