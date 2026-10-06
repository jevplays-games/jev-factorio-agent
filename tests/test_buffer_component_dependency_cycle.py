"""Replay the accepted native observation preceding the V30 planner exit."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from jev_factorio.planning.output_buffers import OutputBufferPlanner
from jev_factorio.planning.input_routes import InputRoutePlanner
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from jev_factorio.state import GameSnapshot
from test_registered_furnace_service import native


def captured():
    saved = json.loads((Path(__file__).parent / 'fixtures/native-v30-output-buffer-cycle.json').read_text())
    snapshot = GameSnapshot(**saved['snapshot'])
    _, catalog = native()
    assert saved['accepted_validation']['payload']['accepted'] is True
    assert sorted(name for name, row in catalog.technologies.items() if row['researched']) == sorted(snapshot.researched)
    snapshot._atomic_inventory_verified = snapshot._coherent_observation_verified = (snapshot.session_id, snapshot.tick)
    return snapshot, catalog


@pytest.mark.parametrize('kind', [OutputBufferPlanner, InputRoutePlanner, MiningOutpostPlanner])
def test_partial_native_buffer_can_acquire_inserter_without_false_parent_cycle(kind):
    snapshot, catalog = captured()
    before = deepcopy(snapshot.__dict__)
    planner = kind(catalog, snapshot, 'rocket_launch')
    # The campaign exhausted its capital attempts; its controller recompiles
    # ordinary production with exactly this existing acquisition guard.
    planner._economic_acquiring = True
    plans = planner.candidates()
    assert plans
    step = plans[0].steps[0]
    assert step.action == 'factory_craft'
    assert step.parameters == {'recipe': 'iron-gear-wheel', 'batches': 1}
    assert step.costs == {'iron-plate': 2}
    assert step.allowed(snapshot) and not step.satisfied(snapshot)
    assert snapshot.__dict__ == before


def test_true_cycle_within_component_recipe_still_fails_closed():
    snapshot, catalog = captured()
    catalog.recipes['iron-gear-wheel']['ingredients'] = [
        {'type': 'item', 'name': 'burner-inserter', 'amount': 1}]
    planner = OutputBufferPlanner(catalog, snapshot, 'rocket_launch')
    planner._economic_acquiring = True
    with pytest.raises(ValueError, match='(?i)cycl'):
        planner.candidates()
    assert planner._acquiring_buffer is False


def test_composed_controller_replays_native_failure_history_without_resetting_it():
    from jev_factorio.background import BackgroundWorkLoop
    from jev_factorio.buffer_controller import buffered_loop_type
    from jev_factorio.input_controller import input_loop_type
    from jev_factorio.outpost_controller import outpost_loop_type
    from test_capital_investments import Backend
    snapshot, catalog = captured()
    saved = json.loads((Path(__file__).parent / 'fixtures/native-v30-output-buffer-cycle.json').read_text())
    class NoModel:
        def evaluate(self, *args):
            raise AssertionError('Planner replay must not call a model')
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    loop = kind(Backend(catalog, snapshot), NoModel(), policy='jev', target='rocket_launch',
                factory_scheduling='ready-work', tick_seconds=0)
    loop.catalog = catalog
    loop.memory = loop.memory_type(snapshot.session_id, 'rocket_launch',
                                   last_tick=snapshot.tick, **saved['planner_memory'])
    failures = deepcopy(loop.memory.failures)
    plans, _ = loop._work_candidates(snapshot)
    assert any(plan.steps[0].action in {'factory_craft', 'factory_craft_job'}
               and plan.steps[0].parameters.get('recipe') == 'iron-gear-wheel'
               and plan.steps[0].costs == {'iron-plate': 2} for plan in plans)
    assert loop.memory.failures == failures
    assert loop.memory.capital_investment is None
    assert loop.backend.calls == []
