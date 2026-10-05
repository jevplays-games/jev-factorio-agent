"""Supervisor repair gates for loader-authorized empty owner migrations."""

import json
from copy import deepcopy
from dataclasses import asdict

import pytest

pytest_plugins = ("test_supervisor",)

from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.input_controller import input_loop_type
from jev_factorio.memory import (
    CampaignMemory,
    checkpoint_memory_type,
    load_checkpoint_data,
)
from jev_factorio.outpost_controller import outpost_loop_type
from jev_factorio.successor_controller import successor_loop_type
from jev_factorio.supervisor import atomic_json
from test_supervisor import operational_result


MIGRATIONS = {
    "output": ("output_ownership_enabled", "explicit_empty_ownership_at_idle_boundary"),
    "outpost": ("mining_outposts_enabled", "explicit_capability_at_idle_boundary"),
    "successor": ("successors_enabled", "explicit_idle_boundary_capability"),
}


def _memory_type(family):
    if family == "output":
        loop_type = buffered_loop_type(HierarchicalLoop)
    elif family == "outpost":
        loop_type = outpost_loop_type(input_loop_type(buffered_loop_type(HierarchicalLoop)))
    else:
        base = input_loop_type(buffered_loop_type(BackgroundWorkLoop))
        loop_type = successor_loop_type(base)
    return loop_type.memory_type


def _previous(history=None):
    return asdict(CampaignMemory(
        "fresh", "rocket_launch", status="running", last_tick=300,
        history=deepcopy(history if history is not None else [
            {"kind": "prior_receipt", "receipt": "keep-me"},
        ]),
        failures={"historical-plan": 2},
    ))


def _loader_current(family, previous):
    memory_type = _memory_type(family)
    memory = memory_type.from_bytes(
        json.dumps(previous).encode(), "fresh", "rocket_launch")
    return memory_type, asdict(memory)


def _assert_composed_loaders_accept(family, previous, current, memory_type):
    # Keep the legacy capture valid under its original reader and normalize both
    # captures through the exact composed reader and repair's union reader.
    CampaignMemory.from_bytes(json.dumps(previous).encode(), "fresh", "rocket_launch")
    memory_type.from_bytes(json.dumps(current).encode(), "fresh", "rocket_launch")
    union = checkpoint_memory_type({
        key: None for key in previous.keys() | current.keys()
    })
    load_checkpoint_data(previous, "fresh", "rocket_launch", memory_type=union)
    load_checkpoint_data(current, "fresh", "rocket_launch", memory_type=union)


def _assert_current_loaders_accept(previous, current, memory_type):
    memory_type.from_bytes(json.dumps(previous).encode(), "fresh", "rocket_launch")
    memory_type.from_bytes(json.dumps(current).encode(), "fresh", "rocket_launch")
    union = checkpoint_memory_type({
        key: None for key in previous.keys() | current.keys()
    })
    load_checkpoint_data(previous, "fresh", "rocket_launch", memory_type=union)
    load_checkpoint_data(current, "fresh", "rocket_launch", memory_type=union)


def _set_candidate(supervisor, tmp_path, previous, current):
    atomic_json(supervisor.config.checkpoint, current)
    before = supervisor.config.checkpoint.read_bytes()
    result = operational_result(supervisor, tmp_path)
    # The shared test fixture deliberately pins source_identity so this suite
    # isolates validate_repair from production capture/launch bookkeeping.
    accepted = supervisor.validate_repair(result, previous, ("head", "diff"))
    assert supervisor.config.checkpoint.read_bytes() == before
    return accepted


@pytest.mark.parametrize("family", tuple(MIGRATIONS))
def test_repair_accepts_each_single_loader_recorded_empty_migration(
        supervisor, tmp_path, family):
    previous = _previous()
    memory_type, current = _loader_current(family, previous)
    kind, reason = MIGRATIONS[family]

    _assert_composed_loaders_accept(family, previous, current, memory_type)
    matching = [row for row in current["history"] if row.get("kind") == kind]
    assert matching == [{"kind": kind, "tick": previous["last_tick"], "reason": reason}]
    assert current["history"][:len(previous["history"])] == previous["history"]
    assert current["failures"] == previous["failures"]
    assert _set_candidate(supervisor, tmp_path, previous, current)


@pytest.mark.parametrize("family", tuple(MIGRATIONS))
def test_repair_accepts_single_new_migration_at_history_capacity(
        supervisor, tmp_path, family):
    full_history = [
        {"kind": "prior_receipt", "ordinal": index}
        for index in range(64)
    ]
    previous = _previous(full_history)
    memory_type, current = _loader_current(family, previous)
    kind, reason = MIGRATIONS[family]

    _assert_composed_loaders_accept(family, previous, current, memory_type)
    assert len(current["history"]) == 64
    migration_kinds = {event_kind for event_kind, _ in MIGRATIONS.values()}
    appended = [row for row in current["history"] if row.get("kind") in migration_kinds]
    # The ordinary history window stays at 64: nested migrations evict exactly
    # as many oldest rows as they append. This checks the loader's retained
    # window, not byte-for-byte preservation of rows already rolled out.
    assert len(appended) == {"output": 1, "outpost": 2, "successor": 2}[family]
    assert any(row == {"kind": kind, "tick": previous["last_tick"], "reason": reason}
               for row in appended)
    assert current["history"][:-len(appended)] == previous["history"][len(appended):]
    assert _set_candidate(supervisor, tmp_path, previous, current)


@pytest.mark.parametrize("family", tuple(MIGRATIONS))
def test_repair_rejects_old_historical_migration_receipt_as_fresh_authority(
        supervisor, tmp_path, family):
    kind, reason = MIGRATIONS[family]
    previous = _previous([{
        "kind": kind, "tick": 300, "reason": reason,
    }])
    memory_type, current = _loader_current(family, previous)

    # The composed legacy loader appends its migration receipt after the old
    # row, so both exact readers accept this duplicate-history candidate.
    _assert_composed_loaders_accept(family, previous, current, memory_type)
    assert sum(row.get("kind") == kind for row in previous["history"]) == 1
    assert sum(row.get("kind") == kind for row in current["history"]) == 2
    assert not _set_candidate(supervisor, tmp_path, previous, current)


@pytest.mark.parametrize("family", tuple(MIGRATIONS))
@pytest.mark.parametrize("mutation", (
    "duplicate", "missing", "wrong_tick", "wrong_key", "wrong_reason",
))
def test_repair_requires_one_new_exact_migration_receipt(
        supervisor, tmp_path, family, mutation):
    previous = _previous()
    memory_type, current = _loader_current(family, previous)
    kind, _ = MIGRATIONS[family]
    event = next(row for row in current["history"] if row.get("kind") == kind)
    index = current["history"].index(event)

    if mutation == "duplicate":
        current["history"].append(deepcopy(event))
    elif mutation == "missing":
        current["history"].pop(index)
    elif mutation == "wrong_tick":
        current["history"][index]["tick"] += 1
    elif mutation == "wrong_key":
        current["history"][index]["unreviewed_identity"] = "extra"
    else:
        current["history"][index]["reason"] = "different-migration-contract"

    _assert_composed_loaders_accept(family, previous, current, memory_type)
    assert not _set_candidate(supervisor, tmp_path, previous, current)


def _with_paid_owner(family, current):
    current = deepcopy(current)
    if family == "output":
        current["output_commitments"] = {
            "recipe:iron-plate": {
                "source_unit": 17, "layout": "output:17",
                "parts": {"chest": {
                    "role": "paid:chest", "unit_number": 18,
                    "receipt": "paid-output-receipt", "paid": 1,
                }},
            },
        }
    elif family == "outpost":
        from test_mining_outposts import commission, full, row, state_fixture

        state, _ = state_fixture()
        full(state)
        commission(state)
        paid = row(state)
        current["outpost_commitments"] = {
            "iron-ore": {key: deepcopy(paid[key]) for key in (
                "layout", "surface_index", "force_index", "steps", "parts", "flow",
            )},
        }
    else:
        from test_successors import GROWTH
        from jev_factorio.successor_controller import _empty_receipts

        project = {
            "anchor": "cell-site:iron",
            "predecessor_unit": 500,
            "source_unit": 17,
            "started_tick": 0,
            "deadline_tick": 216000,
            "status": "paused",
        }
        receipt = _empty_receipts()
        receipt["output_layout"] = "output:17"
        receipt["output"]["chest"] = {
            "role": "out:chest", "receipt": "paid-successor-output",
            "unit_number": 19, "paid": 1,
        }
        current["successor_projects"] = {GROWTH: project}
        current["successor_receipts"] = {GROWTH: receipt}
    return current


@pytest.mark.parametrize("family", tuple(MIGRATIONS))
def test_repair_rejects_paid_owner_even_with_a_valid_migration_receipt(
        supervisor, tmp_path, family):
    previous = _previous()
    memory_type, empty_current = _loader_current(family, previous)
    current = _with_paid_owner(family, empty_current)

    _assert_composed_loaders_accept(family, previous, current, memory_type)
    assert not _set_candidate(supervisor, tmp_path, previous, current)


@pytest.mark.parametrize("family", tuple(MIGRATIONS))
def test_repair_accepts_unchanged_paid_owner_in_current_composed_schema(
        supervisor, tmp_path, family):
    legacy = _previous()
    memory_type, current = _loader_current(family, legacy)
    current = _with_paid_owner(family, current)
    previous = deepcopy(current)

    _assert_current_loaders_accept(previous, current, memory_type)
    assert _set_candidate(supervisor, tmp_path, previous, current)
