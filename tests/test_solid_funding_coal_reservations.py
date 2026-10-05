"""Composed coal ownership must contribute to ordinary kit admission evidence."""
from copy import deepcopy
from dataclasses import asdict
import json
import pytest
from jev_factorio import coal_supply as coal, solid_routes as solid
from jev_factorio.coal_controller import coal_loop_type
from jev_factorio.solid_controller import SolidRouteMixin
from jev_factorio.solid_funding_evidence import funding_history_issues
from test_solid_kit_acquisition import kit_loop
from coal_supply_fixtures import fixture, TARGETS, INTENTS, paid_source, paid_corridor


def composed_kit_loop(tmp_path, corridor_paid=False):
    loop, backend = kit_loop(tmp_path)
    state = fixture()
    first = coal.sources(state)['alpha']
    paid_source(state, {'target': 'alpha', 'layout': first['layout'], 'part': 'chest', 'receipt': 'coal-anchor'})
    if corridor_paid:
        route = next(iter(state.factory['solid_routes']['routes'].values()))
        paid_corridor(state, {'route': route['route'], 'layout': route['layout'],
                             'part': route['steps'][0]['part'], 'receipt': 'coal-corridor-anchor'})
    backend.state.factory['entities'].update(deepcopy(state.factory['entities']))
    backend.state.factory['solid_routes']['routes'].update(deepcopy(state.factory['solid_routes']['routes']))
    backend.state.factory['coal_supply'] = deepcopy(state.factory['coal_supply'])
    backend.state.factory['coal_supply']['tick'] = backend.state.tick
    backend.state.inventory.update(coal.remaining_kit(coal.sources(backend.state), backend.state))
    # Use the actual composed reservation and observer methods; restrict only
    # candidate choice to the ordinary policy route to exercise concurrent locks.
    kind = coal_loop_type(type(loop))
    loop.__class__ = kind
    loop.memory = kind.memory_type(**asdict(loop.memory))
    loop._coal_targets = TARGETS
    loop._coal_fault = False
    loop._coal_protocol_fault = False
    loop._coal_protocol_rejected_observation = False
    loop._coal_protocol_defer_save = False
    loop._coal_protocol_error = "Coal-source protocol is invalid or differs from configured treatment"
    loop._coal_evidence = {}
    loop._coal_kit_policy = False
    loop._coal_economic_admission = False
    loop._coal_admission_evidence = {}
    loop._coal_admission_cache_key = None
    loop._coal_kit_evidence = {}
    loop._solid_intents = [*loop._solid_intents, *INTENTS]
    loop.memory.solid_commitments = {key: solid.commitment(route) for key, route in
        backend.state.factory['solid_routes']['routes'].items() if route['parts']}
    loop.memory.coal_targets = TARGETS
    loop.memory.coal_epoch = {k: backend.state.factory['coal_supply'][k] for k in ('actor_index', 'surface_index', 'force_index')}
    loop.memory.coal_commitments = {target: coal.commitment(row) for target, row in coal.sources(backend.state).items()}
    loop._compile_candidates = lambda snapshot: SolidRouteMixin._compile_candidates(loop, snapshot)
    backend.kit_after = lambda: backend.state.factory['coal_supply'].update(tick=backend.state.tick)
    loop._observe()
    return loop, backend


@pytest.mark.parametrize('corridor_paid', [False, True])
def test_composed_kit_commit_preserves_coal_source_and_future_corridor_locks(tmp_path, corridor_paid):
    loop, backend = composed_kit_loop(tmp_path, corridor_paid)
    initial = asdict(loop.memory)
    record = loop.step(); final = asdict(loop.memory)
    assert record['verified'], record['outcome']
    commit = next(e for e in record['history'] if e['kind'] == 'solid_kit_committed')
    assert commit['acquisition']['reserved']['electric-mining-drill'] > 0
    assert not funding_history_issues(initial, [record], final)
    for history in (record['history'], final['history']):
        next(e for e in history if e['kind'] == 'solid_kit_committed')['acquisition']['reserved'].pop('electric-mining-drill')
    assert funding_history_issues(initial, [record], final)


@pytest.mark.parametrize('kind', [None, 7, False, []])
def test_checkpoint_history_non_string_kind_does_not_crash_model_filter(tmp_path, kind):
    loop, _ = kit_loop(tmp_path)
    loop._observe()
    loop.memory.history.append({'kind': kind, 'definition': {'diagnostic': True}})
    restored = loop.memory_type.from_bytes(json.dumps(asdict(loop.memory)).encode(), loop.memory.session_id, loop.target)
    loop.memory = restored
    history = loop._model_history()
    assert history[-1] == {'kind': kind}
