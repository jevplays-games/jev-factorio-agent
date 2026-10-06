"""The production controller composition must reach the retained verifier."""
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.input_controller import input_loop_type
from jev_factorio.outpost_controller import outpost_loop_type
from jev_factorio.planning.catalog import Catalog
from test_shared_paid_connectors import FIXTURE, Pages, retained


def composed(tmp_path):
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    memory, snapshot, _ = retained()
    memory = kind.memory_type(**asdict(memory))
    path = tmp_path / 'controller.json'
    memory.save(path)
    catalog = Catalog.from_dict(json.loads(
        FIXTURE.with_name('native-v22-capital-power.json').read_text())['catalog'])

    def forbidden(*args, **kwargs):
        pytest.fail('Retained verification must not dispatch or request a new decision')

    backend = SimpleNamespace(craft_jobs_supported=True, output_buffers_supported=True,
        input_routes_supported=True, mining_outposts_supported=True,
        enable_factory=lambda: catalog, observe=lambda: deepcopy(snapshot), execute=forbidden)
    loop = kind(backend, SimpleNamespace(is_mock=False, decide=forbidden, query=forbidden),
                policy='jev', factory_scheduling='ready-work', checkpoint=str(path),
                resume_controller=True, tick_seconds=.01)
    # Replay only the retained native detail pages, with no adapter installation.
    backend._factory = Pages(memory.connector_ownership)
    return loop, memory, snapshot


def test_full_production_composition_resumes_and_verifies_without_redispatch(tmp_path):
    loop, before, _ = composed(tmp_path)
    loop.run(steps=1)
    assert loop.memory.pending is None and loop.memory.attempt is None
    assert loop.memory.connector_ownership == before.connector_ownership
    outcome = next(row for row in loop.memory.attempt_outcomes if row['id'] == before.attempt['id'])
    assert outcome['outcome'] == 'verified'
    assert not loop.memory.reservations


@pytest.mark.parametrize('fault', ['_capital_fault', '_buffer_fault', '_buffer_save_poisoned',
                                  '_input_fault', '_outpost_fault', '_save_poisoned'])
def test_shared_connector_does_not_bypass_composed_faults(tmp_path, fault):
    loop, memory, snapshot = composed(tmp_path)
    loop.memory = memory
    setattr(loop, fault, True)
    assert loop._execution_barrier(snapshot)
    assert loop.memory.pending == memory.pending
