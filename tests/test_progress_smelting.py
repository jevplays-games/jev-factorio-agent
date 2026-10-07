"""Accepted V40 furnace observations; no game or model calls."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from jev_factorio.progress_watch import smelting_sample, smelting_progress, classify, banner
from test_progress_watch import sample


def pairs():
    source = json.loads((Path(__file__).parent/'fixtures/native-v40-smelting-progress.json').read_bytes())
    result = []
    for observation, validation in source['pairs']:
        payload = observation['payload']
        binding = dict(session_id=payload['session_id'],
            execution_id=payload['supervisor_provenance']['execution_id'],
            checkpoint_tick=payload['factorio_tick'],
            now=datetime.fromisoformat(validation['time']['utc'].replace('Z', '+00:00')).timestamp())
        result.append((observation, validation, binding))
    return result


def native_states():
    states = []
    for observation, validation, binding in pairs():
        current = sample(session_id=binding['session_id'], at=binding['now'],
            checkpoint_status='blocked', reason='Candidate evidence insufficient',
            last_progress_at=binding['now']-300,
            native_smelting=smelting_sample(observation, validation, **binding))
        states.append(classify(current, states[-1] if states else {}, binding['now'], binding['session_id']))
    return current, states, binding


def test_native_smelting_is_activity_without_claiming_an_action_or_resetting_hold_age():
    current, (first, second), binding = native_states()
    assert first['status'] == 'blocked'
    assert second['status'] == 'smelting' and not second['attention']
    assert second['foreground_status'] == 'blocked'
    assert second['blocked_since'] == first['blocked_since']
    assert second['progress_tick'] == first['progress_tick']
    assert second['last_progress_at'] == first['last_progress_at']
    assert 'Foreground waiting' in banner(second)
    assert second['automatic_recovery_allowed'] is False
    now = binding['now']+120
    later = classify({**current, 'at': now}, second, now, binding['session_id'])
    assert later['status'] == 'blocked' and later['attention']
    assert later['blocked_since'] == first['blocked_since']


@pytest.mark.parametrize('fault', ['validation', 'pair', 'session', 'execution', 'tick',
                                  'stale', 'paused', 'world', 'schema'])
def test_smelting_requires_fresh_accepted_bound_observations(fault):
    observation, validation, binding = pairs()[0]
    if fault == 'validation': validation['payload']['accepted'] = False
    elif fault == 'pair': validation['payload']['observation_id'] = 'other'
    elif fault == 'session': validation['payload']['session_id'] = 'other'
    elif fault == 'execution': binding['execution_id'] = 'other'
    elif fault == 'tick': binding['checkpoint_tick'] -= 1
    elif fault == 'stale': binding['now'] += 31
    elif fault == 'paused': observation['payload']['snapshot']['factory']['acceptance_runtime']['tick_paused'] = True
    elif fault == 'world': observation['payload']['snapshot']['world_kind'] = 'mock'
    else: observation['payload']['snapshot']['factory']['observation_snapshot_schema'] = 1
    with pytest.raises(ValueError): smelting_sample(observation, validation, **binding)


@pytest.mark.parametrize('fault', ['decrease', 'bool_count', 'same_tick', 'unit', 'recipe',
                                  'idle', 'frozen', 'long_gap', 'missing', 'duplicate'])
def test_counter_or_identity_changes_cannot_hide_a_stall(fault):
    current, (first, second), binding = native_states()
    before = deepcopy(first['smelting_progress'])
    new = deepcopy(current['native_smelting'])
    changed = next(row for row in new['machines'] if any(
        row['unit_number'] == old['unit_number'] and row['products_finished'] > old['products_finished']
        for old in before['machines']))
    old = next(row for row in before['machines'] if row['unit_number'] == changed['unit_number'])
    if fault == 'decrease': changed['products_finished'] = old['products_finished']-1
    elif fault == 'bool_count': changed['products_finished'] = True
    elif fault == 'same_tick': new['observed_tick'] = before['observed_tick']
    elif fault == 'unit': changed['unit_number'] += 100000
    elif fault == 'recipe': changed['recipe'] = 'unrelated'
    elif fault == 'idle': changed['crafting'] = False
    elif fault == 'frozen': changed['products_finished'] = old['products_finished']
    elif fault == 'missing': new = None
    elif fault == 'duplicate': new['machines'].append(deepcopy(changed))
    now = binding['now'] if fault != 'long_gap' else first['at']+121
    result = classify({**current, 'at': now, 'native_smelting': new}, first, now, binding['session_id'])
    assert result['status'] == 'blocked' and result['attention']


@pytest.mark.parametrize('change,expected', [
    ({'checkpoint_status': 'uncertain'}, 'uncertain'),
    ({'reason': 'native reconciliation required'}, 'blocked'),
    ({'owner_alive': False}, 'stopped'), ({'child_alive': False}, 'stopped'),
    ({'owner_phase': 'stopped_by_service_owner'}, 'stopped'),
    ({'checkpoint_status': 'completed'}, 'completed'),
    ({'checkpoint_status': 'running'}, 'progressing'),
])
def test_furnaces_never_override_faults_stops_or_completion(change, expected):
    current, (first, _), binding = native_states()
    result = classify({**current, **change}, first, binding['now'], binding['session_id'])
    assert result['status'] == expected
    assert not result['automatic_recovery_allowed']


def test_idle_recipe_has_no_activity_and_unknown_furnace_role_fails_closed():
    observation, validation, binding = pairs()[1]
    entities = observation['payload']['snapshot']['factory']['entities']
    for row in entities.values(): row['recipe'] = ''
    projected = smelting_sample(observation, validation, **binding)
    assert projected['machines'] == []
    next(iter(entities.values()))['name'] = 'wooden-chest'
    with pytest.raises(ValueError): smelting_sample(observation, validation, **binding)
