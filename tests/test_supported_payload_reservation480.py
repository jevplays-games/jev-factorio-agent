"""Supported launch evidence alone may create a carried-payload reservation."""
from copy import deepcopy

import pytest

from jev_factorio import launch_readiness
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.memory import CampaignMemory
from jev_factorio.planning.demand import SupplyLedger
from jev_factorio.planning.goals import goal_order
from jev_factorio.planning.ready_work import ReadyWorkPlanner
from jev_factorio.skills import Plan, Step
from jev_factorio.telemetry import make_attempt
from test_factory import machine
from test_launch_readiness import scenario


def _invalid_profile(state, case):
    row = state.factory.get("launch_readiness")
    if case == "absent":
        state.factory.pop("launch_readiness", None)
    elif case == "unsupported":
        row["supported"] = False
    elif case == "version_drift":
        row["version"] = "2.1.0"
    elif case == "session_drift":
        row["session_id"] = "another-session"
    elif case == "tick_drift":
        row["tick"] = state.tick - 1
    elif case == "actor_invalid":
        row["actor_unit"] = True
    elif case == "malformed_silo":
        row["silo"] = None
    else:
        raise AssertionError(case)


def _ordinary_transfer(state, *, owner="retained:ordinary-satellite-transfer"):
    role = "modded:satellite-consumer"
    state.factory["entities"][role] = machine(
        "assembling-machine-2", unit_number=90, input={})
    step = Step(
        "factory_insert", "transfer", item="satellite", costs={"satellite": 1},
        parameters={"role": role, "item": "satellite", "quantity": 1,
                    "receipt": "ordinary-satellite-transfer"})
    return Plan(owner, "rocket_launch", "Ordinary modded recipe input", (step,))


@pytest.mark.parametrize("case", [
    "unsupported", "version_drift", "session_drift", "tick_drift",
    "actor_invalid", "malformed_silo", "absent",
])
def test_invalid_launch_profile_does_not_reserve_payload_or_block_ordinary_supply(case):
    data, state = scenario(inventory={"satellite": 2})
    _invalid_profile(state, case)

    assert launch_readiness.reserved(state) == {}
    assert launch_readiness.affordable("factory_insert", {"satellite": 1}, state)
    ledger = SupplyLedger.capture(state, data)
    assert ledger.reserved.get("satellite", 0) == 0
    assert ledger.carried["satellite"] == 2

    work = ReadyWorkPlanner(data, state, "rocket_launch")
    assert work.ledger.reserved.get("satellite", 0) == 0
    assert work.ledger.carried["satellite"] == 2

    plan = _ordinary_transfer(state)
    restored = Plan.from_dict(plan.to_dict())
    assert restored.steps[0].allowed(state)


def test_valid_profile_and_existing_attempts_keep_the_payload_protected():
    data, state = scenario(inventory={"satellite": 1})
    row = state.factory["launch_readiness"]
    assert launch_readiness.reserved(state) == {"satellite": 1}

    row["attempts"]["load"] = {"receipt": "launch:load:10"}
    assert launch_readiness.reserved(state) == {"satellite": 1}
    row["attempts"]["launch"] = {"receipt": "launch:submit:10"}
    assert launch_readiness.reserved(state) == {"satellite": 1}

    ledger = SupplyLedger.capture(state, data)
    assert ledger.reserved["satellite"] == 1
    assert ledger.carried["satellite"] == 0
    assert not launch_readiness.affordable("factory_insert", {"satellite": 1}, state)
    assert not _ordinary_transfer(state).steps[0].allowed(state)


def test_loaded_payload_victory_and_explicit_paid_reservation_remain_distinct():
    data, loaded = scenario(inventory={"satellite": 2}, cargo={"satellite": 1})
    assert launch_readiness.reserved(loaded) == {}
    assert SupplyLedger.capture(loaded, data).carried["satellite"] == 2

    data, victory = scenario(inventory={"satellite": 2})
    victory.victory = True
    assert launch_readiness.reserved(victory) == {}

    data, unsupported = scenario(inventory={"satellite": 2})
    unsupported.factory["launch_readiness"]["supported"] = False
    # This reservation is an independently retained commitment, not a new
    # reservation inferred from the now-unsupported launch profile.
    ledger = SupplyLedger.capture(unsupported, data, reserved={"satellite": 1})
    assert ledger.reserved["satellite"] == 1
    assert ledger.carried["satellite"] == 1


class _ControllerBackend:
    def __init__(self, state, catalog):
        self.state = state
        self.catalog = catalog
        self.executed = []
        self.mock_clock_actions = []

    def enable_factory(self):
        return self.catalog

    def observe(self):
        return deepcopy(self.state)

    def execute(self, action, parameters):
        self.executed.append((action, deepcopy(parameters)))
        role, item, quantity = parameters["role"], parameters["item"], parameters["quantity"]
        self.state.inventory[item] -= quantity
        entity = self.state.factory["entities"][role]
        entity["input"][item] = entity["input"].get(item, 0) + quantity
        self.state.tick += 1
        row = self.state.factory.get("launch_readiness")
        if isinstance(row, dict):
            row["tick"] = self.state.tick
        self.state.factory["receipts"][parameters["receipt"]] = {
            "role": role, "extracting": False, "unit_number": entity["unit_number"],
            "item": item, "quantity": quantity,
        }

    def act(self, action):
        self.mock_clock_actions.append(action)


def _memory_for(plan, state, *, pending=False):
    order = goal_order("rocket_launch")
    completed_goals = {goal: state.tick for goal in order[:-1]}
    memory = CampaignMemory(
        session_id=state.session_id, target="rocket_launch", active_goal="rocket_launch",
        completed_goals=completed_goals, active_plan=plan.to_dict(), step_index=0,
        reservations={plan.id: {"satellite": 1}}, last_tick=state.tick,
    )
    if pending:
        memory.pending = {"started_tick": state.tick, "polls": 0,
                          "action": plan.steps[0].action, "dispatch": "ambiguous"}
        memory.attempt = make_attempt(
            state.session_id, "rocket_launch", memory.active_plan, 0, memory.pending)
    return memory


def test_controller_reload_dispatches_ordinary_transfer_only_for_unsupported_profile(tmp_path):
    outcomes = {}
    for supported in (False, True):
        data, state = scenario(inventory={"satellite": 1})
        if not supported:
            state.factory["launch_readiness"]["supported"] = False
        plan = _ordinary_transfer(state, owner=f"retained:ordinary:{supported}")
        checkpoint = tmp_path / f"controller-{supported}.json"
        _memory_for(plan, state).save(checkpoint)
        backend = _ControllerBackend(state, data)
        loop = HierarchicalLoop(
            backend, policy="deterministic", target="rocket_launch",
            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

        record = loop.step()
        outcomes[supported] = (backend.executed, record, loop.memory)

    assert len(outcomes[False][0]) == 1
    assert outcomes[False][1]["verified"] is True
    assert outcomes[False][2].pending is None
    assert outcomes[False][2].reservations == {}
    assert outcomes[True][0] == []
    assert outcomes[True][2].active_plan is None


def test_reload_keeps_existing_pending_reservation_when_profile_is_unsupported(tmp_path):
    data, state = scenario(inventory={"satellite": 1})
    state.factory["launch_readiness"]["supported"] = False
    plan = _ordinary_transfer(state, owner="retained:ambiguous-ordinary-transfer")
    checkpoint = tmp_path / "pending-controller.json"
    original = _memory_for(plan, state, pending=True)
    original.save(checkpoint)
    backend = _ControllerBackend(state, data)
    loop = HierarchicalLoop(
        backend, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    loop.step()

    assert backend.executed == []
    assert backend.mock_clock_actions == ["idle"]
    assert loop.memory.pending["dispatch"] == "ambiguous"
    assert loop.memory.reservations[plan.id] == {"satellite": 1}
    assert loop.memory.attempt["id"] == original.attempt["id"]


def _craft_actor_for(state):
    row = state.factory["launch_readiness"]
    return {
        "session_id": state.session_id,
        "player_index": 1,
        "unit_number": row["actor_unit"],
        "surface_index": row["surface_index"],
        "force_index": row["force_index"],
    }


def _enable_craft_actor(state, **overrides):
    actor = _craft_actor_for(state)
    actor.update(overrides)
    state.factory["craft_jobs_protocol"] = 1
    state.factory["craft_job_actor"] = actor


@pytest.mark.parametrize("snapshot_version", ["2.0.78", "2.1.0", 2077])
def test_present_snapshot_version_conflict_does_not_create_payload_reservation(snapshot_version):
    data, state = scenario(inventory={"satellite": 2})
    state.game_version = snapshot_version

    assert launch_readiness.reserved(state) == {}
    assert launch_readiness.affordable("factory_insert", {"satellite": 1}, state)
    ledger = SupplyLedger.capture(state, data)
    assert ledger.reserved.get("satellite", 0) == 0
    assert ledger.carried["satellite"] == 2
    work = ReadyWorkPlanner(data, state, "rocket_launch")
    assert work.ledger.reserved.get("satellite", 0) == 0
    assert work.ledger.carried["satellite"] == 2
    assert _ordinary_transfer(state).steps[0].allowed(state)

    # A pre-existing paid reservation is retained, but the contradictory
    # evidence must not add a second inferred unit.
    retained = SupplyLedger.capture(state, data, reserved={"satellite": 1})
    assert retained.reserved["satellite"] == 1
    assert retained.carried["satellite"] == 1


@pytest.mark.parametrize("field", [
    "session_id", "unit_number", "surface_index", "force_index",
])
def test_present_craft_actor_conflict_does_not_create_payload_reservation(field):
    data, state = scenario(inventory={"satellite": 2})
    expected = _craft_actor_for(state)[field]
    _enable_craft_actor(state, **{field: "other-session" if field == "session_id" else expected + 1})

    assert launch_readiness.reserved(state) == {}
    assert launch_readiness.affordable("factory_insert", {"satellite": 1}, state)
    ledger = SupplyLedger.capture(state, data)
    assert ledger.reserved.get("satellite", 0) == 0
    assert ledger.carried["satellite"] == 2
    work = ReadyWorkPlanner(data, state, "rocket_launch")
    assert work.ledger.reserved.get("satellite", 0) == 0
    assert work.ledger.carried["satellite"] == 2
    assert _ordinary_transfer(state).steps[0].allowed(state)

    retained = SupplyLedger.capture(state, data, reserved={"satellite": 1})
    assert retained.reserved["satellite"] == 1
    assert retained.carried["satellite"] == 1


def test_matching_and_legacy_missing_cross_bindings_keep_reservation_behavior():
    data, matching = scenario(inventory={"satellite": 1})
    matching.game_version = "2.0.77"
    _enable_craft_actor(matching)
    assert launch_readiness.reserved(matching) == {"satellite": 1}
    assert SupplyLedger.capture(matching, data).reserved["satellite"] == 1

    data, legacy = scenario(inventory={"satellite": 1})
    assert legacy.game_version is None
    assert "craft_jobs_protocol" not in legacy.factory
    assert "craft_job_actor" not in legacy.factory
    assert launch_readiness.reserved(legacy) == {"satellite": 1}
    assert SupplyLedger.capture(legacy, data).reserved["satellite"] == 1


@pytest.mark.parametrize("case", [
    "protocol_missing", "actor_missing", "unknown_protocol", "malformed_actor",
])
def test_malformed_present_craft_observer_does_not_create_payload_reservation(case):
    data, state = scenario(inventory={"satellite": 2})
    actor = _craft_actor_for(state)
    if case == "protocol_missing":
        state.factory["craft_job_actor"] = actor
    elif case == "actor_missing":
        state.factory["craft_jobs_protocol"] = 1
    elif case == "unknown_protocol":
        state.factory["craft_jobs_protocol"] = 2
        state.factory["craft_job_actor"] = actor
    else:
        state.factory["craft_jobs_protocol"] = 1
        actor["unit_number"] = True
        state.factory["craft_job_actor"] = actor

    assert launch_readiness.reserved(state) == {}
    ledger = SupplyLedger.capture(state, data)
    assert ledger.reserved.get("satellite", 0) == 0
    assert ledger.carried["satellite"] == 2


@pytest.mark.parametrize("conflict", ["snapshot_version", "craft_actor"])
def test_controller_reload_does_not_block_ordinary_work_on_cross_bound_profile(tmp_path, conflict):
    data, state = scenario(inventory={"satellite": 1})
    if conflict == "snapshot_version":
        state.game_version = "2.0.78"
    else:
        _enable_craft_actor(state, unit_number=state.factory["launch_readiness"]["actor_unit"] + 1)
    plan = _ordinary_transfer(state, owner=f"retained:cross-bound:{conflict}")
    checkpoint = tmp_path / f"cross-bound-{conflict}.json"
    _memory_for(plan, state).save(checkpoint)
    backend = _ControllerBackend(state, data)
    loop = HierarchicalLoop(
        backend, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    record = loop.step()

    assert len(backend.executed) == 1
    assert record["verified"] is True
    assert loop.memory.pending is None
    assert loop.memory.reservations == {}


@pytest.mark.parametrize("conflict", ["snapshot_version", "craft_actor"])
def test_cross_bound_profile_does_not_erase_retained_pending_reservation(tmp_path, conflict):
    data, state = scenario(inventory={"satellite": 1})
    if conflict == "snapshot_version":
        state.game_version = "2.0.78"
    else:
        _enable_craft_actor(state, force_index=state.factory["launch_readiness"]["force_index"] + 1)
    plan = _ordinary_transfer(state, owner=f"retained:ambiguous-cross-bound:{conflict}")
    checkpoint = tmp_path / f"pending-cross-bound-{conflict}.json"
    original = _memory_for(plan, state, pending=True)
    original.save(checkpoint)
    backend = _ControllerBackend(state, data)
    loop = HierarchicalLoop(
        backend, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    loop.step()

    assert backend.executed == []
    assert loop.memory.pending["dispatch"] == "ambiguous"
    assert loop.memory.reservations[plan.id] == {"satellite": 1}
    assert loop.memory.attempt["id"] == original.attempt["id"]
