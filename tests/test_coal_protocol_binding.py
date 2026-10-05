"""Observed coal protocol must match immutable controller treatment.

These tests use actual composed controller/checkpoint paths with deterministic
API doubles. They do not qualify native coal adoption or deployment.
"""
from copy import deepcopy
import json

import pytest

from jev_factorio.coal_controller import coal_loop_type
from jev_factorio.solid_controller import solid_loop_type
from jev_factorio.skills import Plan, Step
from coal_supply_fixtures import TARGETS
from test_coal_kit_funding import Backend as KitBackend, controller as kit_controller
from test_coal_supply_integration import Backend as SourceBackend, controller as source_controller
from test_solid_route_integration import FoundationScenario


def _set_protocol(snapshot, version):
    data = snapshot.factory["coal_supply"]
    data.pop("admission", None)
    data["protocol"] = version
    if version == 2:
        data["admission"] = {
            "protocol": 1,
            **{key: data[key] for key in (
                "session_id", "tick", "actor_index", "surface_index", "force_index")},
            "qualified": False,
            "reason": "electric_conversion_and_construction_cost_unknown",
        }
    return data


def _new_coal_loop(backend, path, economic, *, resume=False, kind=None, policy=True):
    options = {"coal_economic_admission": economic}
    if kind is not None:
        options["kind"] = kind
    return kit_controller(
        backend, path, resume=resume, policy=policy, target="rocket_launch", **options)


def _unbound(memory):
    return (
        memory.coal_targets == []
        and memory.coal_epoch == {}
        and memory.coal_commitments == {}
        and memory.coal_supply_schema == 1
        and memory.coal_economic_admission is False
    )


@pytest.mark.parametrize(("economic", "protocol"), [(False, 1), (True, 2)])
def test_matching_protocol_binds_real_composed_checkpoint(tmp_path, economic, protocol):
    backend = KitBackend()
    _set_protocol(backend.state, protocol)
    loop = _new_coal_loop(backend, tmp_path, economic)

    snapshot = loop._observe()

    assert loop._coal_fault is False
    assert loop.memory.coal_supply_schema == (2 if economic else 1)
    assert loop.memory.coal_economic_admission is economic
    assert loop.memory.coal_targets == TARGETS
    assert loop.memory.coal_epoch == {
        key: snapshot.factory["coal_supply"][key]
        for key in ("actor_index", "surface_index", "force_index")
    }
    loaded = loop.memory_type.load(backend.checkpoint, backend.state.session_id, "rocket_launch")
    assert loaded.coal_supply_schema == loop.memory.coal_supply_schema
    assert loaded.coal_epoch == loop.memory.coal_epoch
    assert backend.calls == []


def test_protocol_drift_preserves_nonempty_kit_funding_checkpoint(tmp_path):
    backend = KitBackend()
    producer = _new_coal_loop(backend, tmp_path, False)
    record = producer.step()
    assert record["verified"]
    funding_before = deepcopy(producer.memory.coal_funding)
    assert funding_before and funding_before["held"] == funding_before["kit"]
    checkpoint_before = backend.checkpoint.read_bytes()
    saved_before = producer.memory_type.load(
        backend.checkpoint, backend.state.session_id, "rocket_launch")
    calls_before = deepcopy(backend.calls)

    _set_protocol(backend.state, 2)
    observed_after = deepcopy(backend.state)
    with pytest.raises(ValueError, match="protocol"):
        producer._observe()

    assert backend.state == observed_after
    assert backend.calls == calls_before
    assert backend.checkpoint.read_bytes() == checkpoint_before
    loaded = producer.memory_type.load(backend.checkpoint, backend.state.session_id, "rocket_launch")
    assert loaded.coal_funding == funding_before == saved_before.coal_funding
    assert loaded.failures == saved_before.failures
    assert loaded.history == saved_before.history
    assert loaded.attempt_outcomes == saved_before.attempt_outcomes
    assert loaded.coal_epoch == saved_before.coal_epoch


@pytest.mark.parametrize(("economic", "protocol"), [(False, 2), (True, 1)])
def test_mismatched_first_observation_cannot_bind_coal_treatment_or_epoch(
        tmp_path, economic, protocol):
    backend = KitBackend()
    _set_protocol(backend.state, protocol)
    loop = _new_coal_loop(backend, tmp_path, economic)

    with pytest.raises(ValueError, match="protocol"):
        loop._observe()

    assert _unbound(loop.memory)
    assert loop.memory.coal_kit_policy is False
    assert loop.memory.status != "running"
    assert not backend.checkpoint.exists()
    assert backend.calls == []


@pytest.mark.parametrize(("malformation", "economic"), [
    ("bool_protocol", False), ("string_protocol", False),
    ("unsupported_protocol", False), ("missing_v2_admission", True),
    ("misbound_v2_admission", True),
])
def test_malformed_protocol_shape_cannot_bind_new_checkpoint(tmp_path, malformation, economic):
    backend = KitBackend()
    data = backend.state.factory["coal_supply"]
    if malformation == "bool_protocol":
        data["protocol"] = True
    elif malformation == "string_protocol":
        data["protocol"] = "1"
    elif malformation == "unsupported_protocol":
        data["protocol"] = 3
    elif malformation == "missing_v2_admission":
        _set_protocol(backend.state, 2)
        data.pop("admission")
    else:
        _set_protocol(backend.state, 2)
        data["admission"]["tick"] -= 1

    loop = _new_coal_loop(backend, tmp_path, economic)

    with pytest.raises(ValueError):
        loop._observe()

    assert _unbound(loop.memory)
    assert not backend.checkpoint.exists()
    assert backend.calls == []


@pytest.mark.parametrize("economic", [False, True])
def test_resume_rejects_repeated_protocol_drift_without_dispatch_or_checkpoint_change(
        tmp_path, economic):
    backend = KitBackend()
    expected_protocol = 2 if economic else 1
    _set_protocol(backend.state, expected_protocol)
    producer = _new_coal_loop(backend, tmp_path, economic)
    producer._observe()
    checkpoint_before = backend.checkpoint.read_bytes()
    saved_before = producer.memory_type.load(
        backend.checkpoint, backend.state.session_id, "rocket_launch")
    _set_protocol(backend.state, 1 if economic else 2)
    observed_after = deepcopy(backend.state)
    calls_before = deepcopy(backend.calls)

    for _ in range(2):
        resumed = _new_coal_loop(backend, tmp_path, economic, resume=True)
        with pytest.raises(ValueError, match="protocol"):
            resumed.step()
        assert backend.checkpoint.read_bytes() == checkpoint_before
        assert backend.state == observed_after
        assert backend.calls == calls_before
        loaded_after = resumed.memory_type.load(
            backend.checkpoint, backend.state.session_id, "rocket_launch")
        assert loaded_after.coal_epoch == saved_before.coal_epoch
        assert loaded_after.coal_supply_schema == saved_before.coal_supply_schema
        assert loaded_after.history == saved_before.history
        assert loaded_after.failures == saved_before.failures
        assert loaded_after.pending == saved_before.pending
        assert loaded_after.attempt == saved_before.attempt
        assert loaded_after.coal_commitments == saved_before.coal_commitments
        assert loaded_after.coal_funding == saved_before.coal_funding


def test_protocol_drift_cannot_replay_paid_source_or_change_ownership(tmp_path):
    backend = SourceBackend()
    producer = source_controller(backend, tmp_path)
    record = producer.step()
    assert record["verified"]
    assert len(backend.calls) == 1
    checkpoint_before = backend.checkpoint.read_bytes()
    saved_before = producer.memory_type.load(
        backend.checkpoint, backend.state.session_id, "rocket_launch")
    assert saved_before.coal_commitments
    assert any(saved["parts"] for saved in saved_before.coal_commitments.values())

    _set_protocol(backend.state, 2)
    observed_after = deepcopy(backend.state)
    resumed = source_controller(backend, tmp_path, resume=True)
    with pytest.raises(ValueError, match="protocol"):
        resumed.step()

    assert backend.calls == [(action, parameters) for action, parameters in backend.calls]
    assert len(backend.calls) == 1
    assert backend.state == observed_after
    assert backend.checkpoint.read_bytes() == checkpoint_before
    loaded = resumed.memory_type.load(backend.checkpoint, backend.state.session_id, "rocket_launch")
    assert loaded.coal_commitments == saved_before.coal_commitments
    assert loaded.coal_epoch == saved_before.coal_epoch
    assert loaded.attempt_outcomes == saved_before.attempt_outcomes
    assert loaded.history == saved_before.history
    assert loaded.failures == saved_before.failures


def test_protocol_drift_cannot_dispatch_or_replace_prepared_attempt(tmp_path):
    backend = SourceBackend()
    backend.prepared_once = True
    producer = source_controller(backend, tmp_path)
    record = producer.step()
    assert not record["verified"]
    assert producer.memory.pending is not None
    assert producer.memory.attempt is not None
    assert len(backend.calls) == 1
    checkpoint_before = backend.checkpoint.read_bytes()
    saved_before = producer.memory_type.load(
        backend.checkpoint, backend.state.session_id, "rocket_launch")
    pending_before = deepcopy(saved_before.pending)
    attempt_before = deepcopy(saved_before.attempt)

    _set_protocol(backend.state, 2)
    observed_after = deepcopy(backend.state)
    resumed = source_controller(backend, tmp_path, resume=True)
    with pytest.raises(ValueError, match="protocol"):
        resumed.step()

    assert len(backend.calls) == 1
    assert backend.state == observed_after
    assert backend.checkpoint.read_bytes() == checkpoint_before
    loaded = resumed.memory_type.load(backend.checkpoint, backend.state.session_id, "rocket_launch")
    assert loaded.pending == pending_before
    assert loaded.attempt == attempt_before
    assert loaded.history == saved_before.history
    assert loaded.attempt_outcomes == saved_before.attempt_outcomes
    assert loaded.coal_epoch == saved_before.coal_epoch


def test_recurring_protocol_drift_preserves_pending_attempt_checkpoint(tmp_path):
    backend = SourceBackend()
    backend.prepared_once = True
    loop = source_controller(backend, tmp_path)
    record = loop.step()
    assert record["verified"] is False
    assert loop.memory.pending is not None
    assert loop.memory.attempt is not None
    assert len(backend.calls) == 1

    checkpoint_before = backend.checkpoint.read_bytes()
    saved_before = loop.memory_type.load(
        backend.checkpoint, backend.state.session_id, "rocket_launch")
    calls_before = deepcopy(backend.calls)
    _set_protocol(backend.state, 2)
    observed_after = deepcopy(backend.state)

    with pytest.raises(ValueError, match="protocol"):
        loop._observe()

    assert backend.state == observed_after
    assert backend.calls == calls_before
    assert backend.checkpoint.read_bytes() == checkpoint_before
    saved_after = loop.memory_type.load(
        backend.checkpoint, backend.state.session_id, "rocket_launch")
    assert saved_after.pending == saved_before.pending
    assert saved_after.attempt == saved_before.attempt
    assert saved_after.attempt_outcomes == saved_before.attempt_outcomes
    assert saved_after.coal_commitments == saved_before.coal_commitments
    assert saved_after.coal_epoch == saved_before.coal_epoch
    assert saved_after.coal_supply_schema == saved_before.coal_supply_schema
    assert saved_after.history == saved_before.history
    assert saved_after.failures == saved_before.failures


class BackgroundCoalBackend(KitBackend):
    craft_jobs_supported = True

    def __init__(self):
        super().__init__("output")
        from test_background_work import science_catalog

        self.data = science_catalog()
        self.state.inventory["iron-plate"] = 10
        self.state.factory.update(craft_jobs_protocol=1, craft_job_actor={
            "session_id": self.state.session_id, "player_index": 1, "unit_number": 9,
            "surface_index": 1, "force_index": 1,
        })
        self.state.factory.setdefault("produced", {}).setdefault("automation-science-pack", 0)

    def execute(self, action, parameters):
        if action != "factory_craft_job":
            return super().execute(action, parameters)
        saved = json.loads(self.checkpoint.read_text())
        assert saved["pending"]["action"] == action
        assert saved["pending"]["dispatch"] == "prepared"
        self.calls.append((action, deepcopy(parameters)))
        batches = parameters["batches"]
        self.state.inventory["iron-plate"] -= batches
        actor = self.state.factory["craft_job_actor"]
        self.state.factory["crafting_queue"] = 1
        self.state.factory["craft_job"] = {
            **actor, "id": parameters["receipt"], "recipe": parameters["recipe"],
            "requested": batches, "accepted": batches, "finished": 0,
            "started_tick": self.state.tick, "last_progress_tick": self.state.tick,
            "inputs": {"iron-plate": batches},
            "outputs": {"automation-science-pack": batches},
            "baseline": {"automation-science-pack": 0}, "status": "running",
            "queue_valid": True, "paid": True,
        }
        return "deterministic synthetic background receipt"



def _background_kind():
    from jev_factorio.background import BackgroundWorkLoop

    class Production(BackgroundWorkLoop, FoundationScenario):
        def _compile_candidates(self, snapshot):
            if self.memory.background_job:
                have = snapshot.inventory.get("iron-ore", 0)
                return [Plan("gather-iron", self.memory.active_goal, "Independent ore", (
                    Step("factory_gather", "inventory", "iron-ore", have + 5,
                         parameters={"resource": "iron-ore", "quantity": 5}),
                ))], ""
            plan = Plan("craft-science", self.memory.active_goal, "Craft ten science", (
                Step("factory_craft", "inventory", "automation-science-pack", 10,
                     costs={"iron-plate": 10}, timeout_ticks=1800,
                     parameters={"recipe": "automation-science-pack", "batches": 10}),
            ))
            return [self._tracked_plan(plan, snapshot)], ""

    return coal_loop_type(solid_loop_type(Production))


def test_protocol_drift_preserves_composed_background_job_and_attempt(tmp_path):
    backend = BackgroundCoalBackend()
    kind = _background_kind()
    producer = _new_coal_loop(backend, tmp_path, False, kind=kind, policy=False)
    producer.memory.active_goal = "bootstrap_mining"
    record = producer.step()
    assert record["verified"] is False
    assert producer.memory.background_job is not None
    assert producer.memory.background_attempt is not None
    assert producer.memory.background_step is not None
    assert len(backend.calls) == 1
    checkpoint_before = backend.checkpoint.read_bytes()
    saved_before = producer.memory_type.load(
        backend.checkpoint, backend.state.session_id, "rocket_launch")

    _set_protocol(backend.state, 2)
    observed_after = deepcopy(backend.state)
    resumed = _new_coal_loop(
        backend, tmp_path, False, resume=True, kind=kind, policy=False)
    with pytest.raises(ValueError, match="protocol"):
        resumed.step()

    assert len(backend.calls) == 1
    assert backend.state == observed_after
    assert backend.checkpoint.read_bytes() == checkpoint_before
    loaded = resumed.memory_type.load(backend.checkpoint, backend.state.session_id, "rocket_launch")
    assert loaded.background_job == saved_before.background_job
    assert loaded.background_attempt == saved_before.background_attempt
    assert loaded.background_step == saved_before.background_step
    assert loaded.coal_epoch == saved_before.coal_epoch
    assert loaded.coal_supply_schema == saved_before.coal_supply_schema
    assert loaded.history == saved_before.history
    assert loaded.attempt_outcomes == saved_before.attempt_outcomes
    assert loaded.failures == saved_before.failures
