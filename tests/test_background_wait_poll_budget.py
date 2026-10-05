"""Bounded passive wait windows for an already acknowledged background craft."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from jev_factorio.planning.background_work import background_wait
from jev_factorio.skills import Plan, Step
from jev_factorio.telemetry import make_attempt
from jev_factorio.compatible_recovery import scope as compatible_recovery_scope
from jev_factorio.memory import load_checkpoint
from test_background_work import ReceiptBackend, controller


def _wait_loop(tmp_path):
    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    original_compile = loop._compile_candidates

    def compile_long_deadline(current):
        job = loop._job()
        if job:
            return [background_wait(loop.memory.active_goal, job, current.tick)], ""
        plans, blocker = original_compile(current)
        plan = plans[0]
        step = replace(plan.steps[0], timeout_ticks=144000)
        return [replace(plan, steps=(step,))], blocker

    loop._compile_candidates = compile_long_deadline
    started = loop.step()
    assert started["background_job"]
    waited = loop.step()
    assert waited["status"] == "running"
    assert loop.memory.background_job is not None
    assert loop.memory.active_plan["steps"][0]["action"] == "factory_wait"
    assert loop.memory.active_plan["id"] == (
        "background-wait:" + loop.memory.background_job["parameters"]["receipt"])
    assert loop.memory.pending["dispatch"] == "returned"
    return backend, loop


def _roll_one_local_window(loop):
    limit = loop.max_pending_polls
    assert limit > 1
    plan = Plan.from_dict(loop.memory.active_plan)
    previous = (plan.materials or {}).get("background_wait_rollover")
    previous_rollover = (previous["tick"] if isinstance(previous, dict)
                         else loop.memory.pending["started_tick"])
    before_attempt = deepcopy(loop.memory.attempt)
    before_job = deepcopy(loop.memory.background_job)
    before_start = loop.memory.pending["started_tick"]
    before_background_attempt = deepcopy(loop.memory.background_attempt)
    plan_id = loop.memory.active_plan["id"]
    for _ in range(limit + 1):
        result = loop.step()
        assert result["status"] == "running"
        assert loop.memory.pending is not None
        plan = Plan.from_dict(loop.memory.active_plan)
        rollover = (plan.materials or {}).get("background_wait_rollover")
        rollover = rollover.get("tick") if isinstance(rollover, dict) else None
        if type(rollover) is int and rollover > previous_rollover:
            return (result, before_attempt, before_job, before_start,
                    before_background_attempt, plan_id)
    raise AssertionError("bounded active-wait poll window did not roll")


def test_exact_background_wait_rolls_bounded_windows_when_world_progresses_across_reload(tmp_path):
    backend, loop = _wait_loop(tmp_path)
    loop.max_pending_polls = 4  # Accelerate the production bound without skipping public polls.
    attempt = deepcopy(loop.memory.attempt)
    job = deepcopy(loop.memory.background_job)
    craft_attempt = deepcopy(loop.memory.background_attempt)
    started_tick = loop.memory.pending["started_tick"]
    plan_id = loop.memory.active_plan["id"]
    assert job["deadline_tick"] == 144010

    def advance_still_running_job(current_backend):
        current_backend.state.tick += 1
        native_job = current_backend.state.factory["craft_job"]
        if native_job["status"] == "running" and native_job["finished"] < 9:
            native_job["finished"] += 1
            native_job["last_progress_tick"] = current_backend.state.tick
            item = "automation-science-pack"
            current_backend.state.inventory[item] = current_backend.state.inventory.get(item, 0) + 1
            current_backend.state.factory["produced"][item] = (
                current_backend.state.factory["produced"].get(item, 0) + 1)

    backend.before_observation = advance_still_running_job
    last_tick = started_tick
    # Exhaust three bounded windows through real public steps. The craft is
    # making partial progress, but its exact completion predicate stays false.
    for _ in range(3):
        (result, same_attempt, _, same_start,
         same_background_attempt, same_plan) = _roll_one_local_window(loop)
        assert result["status"] == "running"
        assert same_attempt["id"] == attempt["id"]
        assert same_start == started_tick
        assert same_background_attempt["id"] == craft_attempt["id"]
        assert same_plan == plan_id
        assert loop.memory.active_plan["id"] == plan_id
        assert loop.memory.pending["action"] == "factory_wait"
        assert loop.memory.pending["started_tick"] == started_tick
        assert 0 < loop.memory.pending["polls"] < loop.max_pending_polls
        assert loop.memory.attempt["id"] == attempt["id"]
        assert loop.memory.background_attempt["id"] == craft_attempt["id"]
        current_job = loop.memory.background_job
        for key in ("session_id", "goal", "plan_id", "parameters", "actor",
                    "started_tick", "deadline_tick", "inputs", "outputs", "baseline"):
            assert current_job[key] == job[key]
        assert 0 < current_job["finished"] < current_job["parameters"]["batches"]
        assert loop.memory.failures.get(plan_id, 0) == 0
        assert backend.state.tick > last_tick
        assert backend.state.factory["craft_job"]["finished"] < 10
        last_tick = backend.state.tick
        assert [action for action, _ in backend.calls].count("factory_craft_job") == 1

    expected_checkpoint_scope = compatible_recovery_scope(loop.memory)
    checkpoint_memory = load_checkpoint(
        Path(loop.checkpoint), backend.state.session_id, loop.target)
    assert compatible_recovery_scope(checkpoint_memory) == expected_checkpoint_scope
    resumed = controller(backend, tmp_path, resume=True)
    resumed.max_pending_polls = 4
    # Reinstall only the deterministic candidate source; all subsequent
    # observation, admission, wait verification and dispatch use the real loop.
    resumed_compile = resumed._compile_candidates
    def compile_long_deadline(current):
        job = resumed._job()
        if job:
            return [background_wait(resumed.memory.active_goal, job, current.tick)], ""
        plans, blocker = resumed_compile(current)
        plan = plans[0]
        step = replace(plan.steps[0], timeout_ticks=144000)
        return [replace(plan, steps=(step,))], blocker
    resumed._compile_candidates = compile_long_deadline
    resumed_record = resumed.step()  # Loads the checkpoint, then polls the same wait once.
    assert resumed_record["status"] == "running"
    assert resumed.memory.attempt["id"] == attempt["id"]
    assert resumed.memory.background_attempt["id"] == craft_attempt["id"]
    for key in ("session_id", "goal", "plan_id", "parameters", "actor",
                "started_tick", "deadline_tick", "inputs", "outputs", "baseline"):
        assert resumed.memory.background_job[key] == job[key]
    (result, _, _, resumed_start, _, resumed_plan_id) = _roll_one_local_window(resumed)
    assert result["status"] == "running"
    assert resumed_plan_id == plan_id
    assert resumed.memory.pending["started_tick"] == started_tick == resumed_start
    assert resumed.memory.attempt["id"] == attempt["id"]
    assert resumed.memory.background_attempt["id"] == craft_attempt["id"]
    assert 0 < resumed.memory.background_job["finished"] < job["parameters"]["batches"]
    assert resumed.memory.failures.get(plan_id, 0) == 0

    backend.complete()
    result = resumed.step()
    assert result["status"] == "completed"
    assert resumed.memory.background_job is None
    assert resumed.memory.pending is None
    assert resumed.memory.failures.get(plan_id, 0) == 0
    assert [action for action, _ in backend.calls].count("factory_craft_job") == 1


def test_frozen_world_wait_stops_after_one_bounded_window_and_retains_paid_job(tmp_path):
    backend, loop = _wait_loop(tmp_path)
    backend.act = lambda action: None
    loop.max_pending_polls = 4
    job = deepcopy(loop.memory.background_job)
    craft_attempt = deepcopy(loop.memory.background_attempt)
    plan_id = loop.memory.active_plan["id"]
    started_tick = loop.memory.pending["started_tick"]

    for _ in range(loop.max_pending_polls):
        result = loop.step()

    assert result["status"] == "running"
    assert "observation budget" in result["outcome"]
    assert backend.state.tick == started_tick
    assert loop.memory.active_plan is None
    assert loop.memory.pending is None
    assert loop.memory.background_job == job
    assert loop.memory.background_attempt["id"] == craft_attempt["id"]
    assert loop.memory.failures.get(plan_id) == 1
    assert [action for action, _ in backend.calls].count("factory_craft_job") == 1


@pytest.mark.parametrize("corruption", [
    "negative_tick", "boolean_tick", "future_tick", "stale_receipt",
    "wrong_plan", "wrong_attempt", "null_marker",
])
def test_invalid_persisted_wait_rollover_witness_fails_closed(tmp_path, corruption):
    backend, loop = _wait_loop(tmp_path)
    loop.max_pending_polls = 4
    plan = Plan.from_dict(loop.memory.active_plan)
    receipt = loop.memory.background_job["parameters"]["receipt"]
    attempt_id = loop.memory.attempt["id"]
    started_tick = loop.memory.pending["started_tick"]
    # Move the current observation forward once so a correctly bound witness
    # has a real checkpoint tick to compare against.
    backend.state.tick = started_tick + 1
    advanced = loop.step()
    assert advanced["status"] == "running"
    valid_tick = loop.memory.last_tick
    marker = {
        "schema": 1, "receipt": receipt, "plan_id": plan.id,
        "attempt_id": attempt_id, "tick": valid_tick,
    }
    if corruption == "negative_tick":
        marker["tick"] = -1
    elif corruption == "boolean_tick":
        marker["tick"] = True
    elif corruption == "future_tick":
        marker["tick"] = valid_tick + 1
    elif corruption == "stale_receipt":
        marker["receipt"] = "stale-receipt"
    elif corruption == "wrong_plan":
        marker["plan_id"] = "unrelated-wait"
    elif corruption == "wrong_attempt":
        marker["attempt_id"] = "unrelated-attempt"
    materials = dict(plan.materials or {})
    materials["background_wait_rollover"] = (
        None if corruption == "null_marker" else marker)
    loop.memory.active_plan["materials"] = materials
    loop.memory.pending["polls"] = loop.max_pending_polls - 1
    loop._save()

    with pytest.raises(ValueError, match="rollover witness"):
        load_checkpoint(Path(loop.checkpoint), backend.state.session_id, loop.target)

    result = loop.step()

    assert "observation budget" in result["outcome"]
    assert loop.memory.pending is None
    assert loop.memory.active_plan is None
    assert loop.memory.background_job is not None
    assert [action for action, _ in backend.calls].count("factory_craft_job") == 1


def test_background_wait_rollover_never_crosses_the_native_job_deadline(tmp_path):
    backend, loop = _wait_loop(tmp_path)
    job = deepcopy(loop.memory.background_job)
    pending = deepcopy(loop.memory.pending)
    backend.state.tick = job["deadline_tick"]

    result = loop.step()

    assert result["status"] == "uncertain"
    assert loop.memory.background_job["failed"]
    assert loop.memory.pending == pending
    assert [action for action, _ in backend.calls].count("factory_craft_job") == 1


def test_unrelated_wait_does_not_inherit_background_poll_rollover(tmp_path):
    backend, loop = _wait_loop(tmp_path)
    plan = Plan("unrelated-wait", loop.target, "Unrelated passive wait", (
        Step("factory_wait", "crafting_idle", timeout_ticks=1800),
    ))
    loop.memory.active_plan = plan.to_dict()
    loop.memory.pending = {"started_tick": backend.state.tick,
                           "polls": loop.max_pending_polls - 1,
                           "action": "factory_wait", "dispatch": "returned"}
    loop.memory.attempt = make_attempt(
        backend.state.session_id, loop.target, plan.to_dict(), 0, loop.memory.pending,
        process_id=loop._process_id,
    )
    loop._save()

    result = loop.step()

    assert result["status"] == "running"
    assert loop.memory.pending is None
    assert loop.memory.failures.get(plan.id) == 1
    assert loop.memory.background_job is not None
    assert [action for action, _ in backend.calls].count("factory_craft_job") == 1
