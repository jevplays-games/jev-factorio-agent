"""Source reevaluation must retain the ordinary capital safety frontier."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.input_controller import input_loop_type
from jev_factorio.outpost_controller import outpost_loop_type
from jev_factorio.planning import capital
from jev_factorio.state import GameSnapshot
from test_capital_investments import Backend
from test_registered_furnace_service import native


def captured():
    saved = json.loads((Path(__file__).parent / 'fixtures/native-v29-source-capital-frontier.json').read_text())
    snapshot = GameSnapshot(**saved['snapshot'])
    _, catalog = native()
    assert saved['accepted_validation']['payload']['accepted'] is True
    assert sorted(name for name, tech in catalog.technologies.items() if tech['researched']) == sorted(snapshot.researched)
    snapshot._atomic_inventory_verified = snapshot._coherent_observation_verified = (snapshot.session_id, snapshot.tick)
    class NoModel:
        def evaluate(self, *args): raise AssertionError('Planner inspection cannot call a model')
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    loop = kind(Backend(catalog, snapshot), NoModel(), policy='jev', target='rocket_launch',
                factory_scheduling='ready-work', tick_seconds=0)
    loop.catalog = catalog
    # Retain the captured planner state; this is not a full checkpoint admission.
    loop.memory = loop.memory_type(snapshot.session_id, 'rocket_launch',
        active_goal='rocket_launch', last_tick=snapshot.tick, status='blocked', reason=saved['reason'],
        capital_investment=deepcopy(saved['capital_investment']),
        failures=deepcopy(saved['failures']), reservations=deepcopy(saved['reservations']))
    return loop, snapshot


def test_native_expired_commitment_is_accounted_before_recovery_candidates():
    loop, snapshot = captured()
    original = deepcopy(loop.memory.capital_investment)
    assert snapshot.tick > original['deadline_tick']
    loop._source_reevaluation_planning = True
    plans, _ = loop._work_candidates(snapshot)
    assert plans and loop.memory.capital_investment is None
    assert loop.memory.failures[original['spec']['key']] == 2
    assert not any((p.materials or {}).get(capital.MARKER, {}).get('spec', {}).get('key')
                   == original['spec']['key'] for p in plans)
    assert any(e['kind'] == 'capital_abandoned' and e['reason'] == 'bounded_investment_deadline'
               for e in loop.memory.history)
    assert loop.backend.calls == []


def test_source_planning_cannot_bypass_execution_barrier():
    loop, snapshot = captured()
    before = deepcopy(loop.memory.capital_investment)
    loop._source_reevaluation_planning = True
    loop._capital_fault = True
    loop._work_candidates(snapshot)
    assert loop.memory.capital_investment == before and loop.backend.calls == []


@pytest.mark.parametrize('status', ['blocked', 'uncertain', 'completed'])
def test_only_blocked_state_can_use_source_planning_scope(status):
    assert capital.frontier_active(status, False, True) is (status == 'blocked')
    assert not capital.frontier_active(status, False, False)


@pytest.mark.parametrize('persistent', [False, True])
def test_real_source_reevaluation_scopes_planner_flag_and_clears_it_on_error(tmp_path, monkeypatch, persistent):
    from test_blocked_reevaluation import _make_loop, _bootstrap_plan
    backend, _, _, _, loop = _make_loop(tmp_path, monkeypatch,
        selection=lambda s: [_bootstrap_plan(s)], persistent=persistent,
        blocked_reason='Furnace fuel service requires current owned source identity')
    def inspect(snapshot):
        assert loop._source_reevaluation_planning is True
        raise RuntimeError('stop at inspected planning boundary')
    loop._work_candidates = inspect
    with pytest.raises(RuntimeError, match='inspected planning boundary'):
        loop.step()
    assert loop._source_reevaluation_planning is False and backend.actions == []
