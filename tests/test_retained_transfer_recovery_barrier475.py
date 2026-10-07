"""Public retained-transfer recovery barriers over checkpointed owners.

These controls use a receipt-faithful offline backend and the real composed
controller/load/save paths; they do not call a provider or native game.
"""
from copy import deepcopy
import json

import pytest

from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.input_controller import input_loop_type
from jev_factorio.state import GameSnapshot

from attempt_helpers import ReceiptBackend
from input_routes_fixtures import (
    SOURCE as INPUT_SOURCE,
    fixture as input_fixture,
    full as full_input_route,
)
from test_attempts import prepared_native_transfer_checkpoint
from test_output_buffer_integration import setup as output_fixture


def _input_backend():
    raw = input_fixture()
    full_input_route(raw)
    raw.session_id = "receipt-session"
    raw.factory["input_routes"].update(session_id=raw.session_id, tick=raw.tick)
    raw.factory["output_buffers"].update(session_id=raw.session_id, tick=raw.tick)
    state = GameSnapshot(
        **vars(raw), world_kind="fle", researched=[],
        nearby_resources={"iron-ore": 1, "coal": 1},
    )
    state.inventory["automation-science-pack"] = 20
    state.factory["player_bound"] = True
    state.factory["entities"]["utility:lab"] = {"unit_number": 7, "input": {}}
    backend = ReceiptBackend()
    backend.state = state
    backend.input_routes_supported = True
    backend._repair_after_return = None
    original_execute = backend.execute

    def execute(action, parameters):
        result = original_execute(action, parameters)
        backend.state.factory["input_routes"]["tick"] = backend.state.tick
        backend.state.factory["output_buffers"]["tick"] = backend.state.tick
        if backend._repair_after_return is not None:
            backend._repair_after_return()
        return result

    backend.execute = execute
    route = state.factory["input_routes"]["sources"][INPUT_SOURCE]
    backend._route_original_unit = route["parts"]["inserter"]["unit_number"]
    return backend


def _output_backend(*, ready=True):
    backend = ReceiptBackend()
    backend.state.world_kind = "fle"
    backend.state.factory["player_bound"] = True
    backend.state.factory["player_connected"] = True
    backend.state.factory["crafting_queue"] = 0
    state, production_catalog, _ = output_fixture(ready=ready)
    backend.state.factory["entities"].update(deepcopy(state.factory["entities"]))
    backend.state.factory["output_buffers"] = deepcopy(state.factory["output_buffers"])
    output = backend.state.factory["output_buffers"]
    output.update(session_id=backend.state.session_id, tick=backend.state.tick)
    backend.output_buffers_supported = True
    backend.enable_factory = lambda: production_catalog
    backend._repair_after_return = None
    original_execute = backend.execute

    def execute(action, parameters):
        result = original_execute(action, parameters)
        output["tick"] = backend.state.tick
        if backend._repair_after_return is not None:
            backend._repair_after_return()
        return result

    backend.execute = execute
    source = next(iter(output["sources"].values()))
    backend._output_paid = deepcopy(source["parts"])
    backend._output_source = source["source"]
    return backend


def _prepared_checkpoint(tmp_path, backend, family):
    path = tmp_path / "checkpoint.json"
    prepared_native_transfer_checkpoint(tmp_path, backend)
    data = json.loads(path.read_text())
    if family == "input":
        route = backend.state.factory["input_routes"]["sources"][INPUT_SOURCE]
        data["input_routes_schema"] = 1
        data["input_commitments"] = {
            INPUT_SOURCE: {
                "layout": route["layout"],
                "source_unit": route["source_unit"],
                "parts": deepcopy(route["parts"]),
            }
        }
        loop_type = input_loop_type(HierarchicalLoop)
        target = "bootstrap_mining"
    else:
        data["target"] = "rocket_launch"
        data["output_buffers_schema"] = 1
        data["output_commitments"] = {
            backend._output_source: {
                "layout": backend.state.factory["output_buffers"]["sources"][backend._output_source]["layout"],
                "source_unit": backend.state.factory["output_buffers"]["sources"][backend._output_source]["source_unit"],
                "parts": deepcopy(backend._output_paid),
            }
        }
        loop_type = buffered_loop_type(HierarchicalLoop)
        target = "rocket_launch"
    path.write_text(json.dumps(data))
    # Exercise the actual strict typed extension loader and durable save before
    # the public controller resumes the retained attempt.
    typed = loop_type.memory_type.load(path, backend.state.session_id, target)
    typed.save(path)
    return path, loop_type, target


def _loop(backend, path, loop_type, target):
    return loop_type(
        backend,
        policy="deterministic",
        target=target,
        factory_scheduling="ready-work",
        checkpoint=str(path),
        resume_controller=True,
        tick_seconds=0,
        max_pending_polls=1,
    )


def _make_fault(backend, family):
    if family == "input":
        def fault():
            backend.state.factory["entities"]["input:inserter"]["unit_number"] = 999
        def repair():
            backend.state.factory["entities"]["input:inserter"]["unit_number"] = backend._route_original_unit
        return fault, repair

    output = backend.state.factory["output_buffers"]["sources"][backend._output_source]
    paid = backend._output_paid["chest"]
    role = paid["role"]
    original_unit = backend.state.factory["entities"][role]["unit_number"]

    def fault():
        backend.state.factory["entities"][role]["unit_number"] = original_unit + 1000

    def repair():
        backend.state.factory["entities"][role]["unit_number"] = original_unit

    return fault, repair


@pytest.mark.parametrize("family", ["input", "output"])
def test_retained_transfer_barrier_preserves_paid_owner_until_fresh_reconciliation(
    family, tmp_path,
):
    backend = _input_backend() if family == "input" else _output_backend()
    path, loop_type, target = _prepared_checkpoint(tmp_path, backend, family)
    fault, repair = _make_fault(backend, family)
    backend._repair_after_return = fault

    loop = _loop(backend, path, loop_type, target)
    before = loop_type.memory_type.load(path, backend.state.session_id, target)
    attempt_id = before.attempt["id"]
    reserved = deepcopy(before.reservations)
    result = loop.step()

    # This is the red assertion on the assigned base: current source verifies
    # and clears the returned attempt despite the newly active composed barrier.
    assert result["verified"] is False
    assert result["status"] == "uncertain"
    assert "synthetic transfer" in result["outcome"]
    assert result["outcome"].endswith("pending retained for reconciliation")
    assert loop.memory.pending["dispatch"] == "returned"
    assert loop.memory.attempt["id"] == attempt_id
    assert loop.memory.reservations == reserved
    assert loop.memory.step_index == 0
    assert len(backend.calls) == 1
    saved = loop_type.memory_type.load(path, backend.state.session_id, target)
    assert saved.pending == loop.memory.pending
    assert saved.attempt["id"] == attempt_id
    assert saved.reservations == reserved

    # Repeated calls on a sticky-fault instance and a fresh reload while the
    # owner is still invalid must neither verify nor dispatch again.
    repeated = loop.step()
    assert not repeated["verified"]
    assert loop.memory.pending["dispatch"] == "returned"
    assert loop.memory.attempt["id"] == attempt_id
    assert len(backend.calls) == 1
    resumed_blocked = _loop(backend, path, loop_type, target)
    blocked = resumed_blocked.step()
    assert not blocked["verified"] and blocked["status"] == "uncertain"
    assert resumed_blocked.memory.pending["dispatch"] == "returned"
    assert resumed_blocked.memory.attempt["id"] == attempt_id
    assert len(backend.calls) == 1

    # Once a new composed controller sees exact restored owner evidence, it
    # may verify this same returned receipt, but must not repeat the mutation.
    repair()
    resumed_valid = _loop(backend, path, loop_type, target)
    completed = resumed_valid.step()
    assert completed["verified"]
    assert completed["attempt_outcomes"][-1]["id"] == attempt_id
    assert resumed_valid.memory.pending is None
    assert len(backend.calls) == 1


@pytest.mark.parametrize("family", ["input", "output"])
def test_valid_retained_transfer_recovery_still_verifies_once(family, tmp_path):
    backend = _input_backend() if family == "input" else _output_backend()
    path, loop_type, target = _prepared_checkpoint(tmp_path, backend, family)
    loop = _loop(backend, path, loop_type, target)

    result = loop.step()

    assert result["verified"]
    assert len(backend.calls) == 1
    assert loop.memory.pending is None
    saved = loop_type.memory_type.load(path, backend.state.session_id, target)
    assert saved.pending is None
    assert saved.attempt is None
    assert saved.attempt_outcomes[-1]["outcome"] == "verified"


@pytest.mark.parametrize("family", ["input", "output"])
def test_observation_failure_after_return_keeps_exact_receipt_for_reload(family, tmp_path):
    backend = _input_backend() if family == "input" else _output_backend()
    path, loop_type, target = _prepared_checkpoint(tmp_path, backend, family)
    loop = _loop(backend, path, loop_type, target)
    backend.mode = "lost_observation"

    with pytest.raises(TimeoutError):
        loop.step()

    saved = loop_type.memory_type.load(path, backend.state.session_id, target)
    attempt_id = saved.attempt["id"]
    assert saved.pending["dispatch"] == "returned"
    assert len(backend.calls) == 1
    resumed = _loop(backend, path, loop_type, target)
    result = resumed.step()
    assert result["verified"]
    assert result["attempt_outcomes"][-1]["id"] == attempt_id
    assert resumed.memory.pending is None
    assert len(backend.calls) == 1


@pytest.mark.parametrize("failure_point", ["observer_save", "record_save"])
def test_barrier_persistence_errors_remain_primary_and_owners_remain_durable(
    failure_point, tmp_path, monkeypatch,
):
    backend = _output_backend()
    path, loop_type, target = _prepared_checkpoint(tmp_path, backend, "output")
    fault, _ = _make_fault(backend, "output")
    backend._repair_after_return = fault
    loop = _loop(backend, path, loop_type, target)
    original_save = loop_type.memory_type.save
    expected = OSError(f"primary {failure_point}")
    barrier_saves = 0

    def fail_at_boundary(memory, destination):
        nonlocal barrier_saves
        if loop._buffer_fault:
            barrier_saves += 1
            if failure_point == "observer_save" or barrier_saves == 2:
                raise expected
        return original_save(memory, destination)

    monkeypatch.setattr(loop_type.memory_type, "save", fail_at_boundary)
    with pytest.raises(OSError) as caught:
        loop.step()

    assert caught.value is expected
    assert len(backend.calls) == 1
    saved = loop_type.memory_type.load(path, backend.state.session_id, target)
    assert saved.pending["dispatch"] == "returned"
    assert saved.attempt is not None
    assert saved.reservations
    if failure_point == "observer_save":
        assert saved.status == "running"
        # The checkpoint is the last durable returned receipt before the
        # failing observation save; reconstruction must recheck live owners.
        monkeypatch.setattr(loop_type.memory_type, "save", original_save)
        resumed = _loop(backend, path, loop_type, target)
        blocked = resumed.step()
        assert not blocked["verified"] and blocked["status"] == "uncertain"
        assert resumed.memory.pending["dispatch"] == "returned"
        assert len(backend.calls) == 1
    else:
        assert saved.status == "uncertain"
        assert barrier_saves == 2
    assert loop.memory.pending["dispatch"] == "returned"
    assert loop.memory.attempt is not None
    assert loop.memory.attempt["id"] == saved.attempt["id"]
    assert loop.memory.reservations == saved.reservations
    assert loop.memory.status == "uncertain"
