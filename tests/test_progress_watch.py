"""Health labels depend on verified progress, not heartbeat or fresh game ticks."""
import json
from pathlib import Path
import sys

import pytest

from jev_factorio.progress_watch import classify, banner, append_event, run_once


def sample(**changes):
    return dict({'session_id': 'campaign', 'at': 1000, 'progress_tick': 80,
                 'last_progress_at': 900, 'checkpoint_status': 'running',
                 'owner_phase': 'running', 'owner_alive': True, 'child_alive': True,
                 'pending': False}, **changes)


def test_live_process_and_advancing_world_tick_cannot_hide_stall():
    state = classify(sample(at=1050, world_tick=999999), {}, 1050, 'campaign')
    assert state['status'] == 'no_progress' and state['attention']
    assert state['progress_age_seconds'] == 150
    assert not state['automatic_recovery_allowed']


def test_blocked_duration_survives_monitor_restart_and_new_verified_progress_clears_it():
    blocked = sample(checkpoint_status='blocked', reason='low choice confidence')
    first = classify(blocked, {}, 1000, 'campaign')
    prior = json.loads(json.dumps(first))
    second = classify({**blocked, 'at': 1060}, prior, 1060, 'campaign')
    assert second['blocked_age_seconds'] == 60 and second['progress_age_seconds'] == 160
    assert 'low choice confidence' in banner(second)
    recovered = classify(sample(at=1061, progress_tick=81), second, 1061, 'campaign')
    assert recovered['status'] == 'progressing' and not recovered['attention']
    assert recovered['blocked_since'] is None and banner(recovered) == ''


def test_restarting_owner_does_not_refresh_same_historical_progress():
    first = classify(sample(), {}, 1000, 'campaign')
    later = classify(sample(at=1100, last_progress_at=1100), first, 1100, 'campaign')
    assert later['status'] == 'no_progress'
    assert later['last_progress_at'] == 900 and later['progress_age_seconds'] == 200


@pytest.mark.parametrize('change', [dict(session_id='other'), dict(at=800),
                                   dict(at=1010), dict(progress_tick=79),
                                   dict(last_progress_at=float('nan'))])
def test_missing_stale_mismatched_or_regressed_evidence_is_unknown(change):
    previous = classify(sample(), {}, 1000, 'campaign')
    current = classify(sample(**change), previous, 1001, 'campaign')
    assert current['status'] == 'unknown' and current['attention']


def test_wall_clock_regression_is_not_fresh_progress():
    previous = classify(sample(), {}, 1000, 'campaign')
    current = classify(sample(at=990), previous, 990, 'campaign')
    assert current['status'] == 'unknown' and current['reason'] == 'monitor clock regressed'


def test_pending_native_work_is_reported_without_authorizing_replay():
    state = classify(sample(at=1100, pending=True), {}, 1100, 'campaign')
    assert state['pending'] and state['status'] == 'no_progress'
    assert state['automatic_recovery_allowed'] is False


@pytest.mark.parametrize('phase,status,expected', [
    ('stopped_by_service_owner', 'running', 'stopped'), ('completed', 'completed', 'completed')])
def test_intentional_stop_and_completion_are_not_recovery_requests(phase, status, expected):
    state = classify(sample(owner_phase=phase, checkpoint_status=status, child_alive=False),
                     {}, 1000, 'campaign')
    assert state['status'] == expected and not state['attention']


def test_read_failures_and_display_failures_remain_visible(tmp_path):
    if sys.platform == 'win32': pytest.skip('durable Linux directory fsync')
    broken = [sys.executable, '-c', 'raise SystemExit(2)']
    state = run_once(broken, broken, tmp_path, 'campaign')
    assert state['status'] == 'unknown'
    assert state['probe_error'] == state['display_error'] == 'CalledProcessError'
    assert json.loads((tmp_path / 'status.json').read_text()) == state
    assert len((tmp_path / 'transitions.jsonl').read_text().splitlines()) == 1
    run_once(broken, broken, tmp_path, 'campaign')
    assert len((tmp_path / 'transitions.jsonl').read_text().splitlines()) == 1


def test_journal_rotation_is_bounded_and_retains_latest_transition(tmp_path):
    path = tmp_path / 'events'
    for i in range(10): append_event(path, {'event': i}, max_bytes=1, backups=2)
    assert {p.name for p in tmp_path.iterdir()} == {'events', 'events.1', 'events.2'}
    assert json.loads(path.read_text()) == {'event': 9}


# Native watch observations for receipt fd651ce786e54186a538987de8592134:
# 8/20 at tick10927618, then18/20 at10931102. The old monitor falsely
# raised no_progress at1791217322 before completion at10931702.
def craft(**changes):
    return dict(receipt='fd651ce786e54186a538987de8592134', requested=20,
                finished=8, started_tick=10924421, last_progress_tick=10927308,
                deadline_tick=10938821, observed_tick=10927618, **changes)


def test_recorded_long_craft_advances_without_rewriting_completed_action_age():
    first = classify(sample(at=1791217253, last_progress_at=1791217202,
                            background_craft=craft()), {}, 1791217253, 'campaign')
    advanced = {**craft(), 'finished': 18, 'last_progress_tick': 10930922,
                'observed_tick': 10931102}
    second = classify(sample(at=1791217314, last_progress_at=1791217202,
                             background_craft=advanced), first, 1791217314, 'campaign')
    current = classify(sample(at=1791217322, last_progress_at=1791217202,
                              background_craft=advanced), second, 1791217322, 'campaign')
    assert current['status'] == 'progressing' and not current['attention']
    assert current['reason'] == 'tracked craft is advancing'
    assert current['progress_age_seconds'] == 120
    assert current['last_progress_at'] == 1791217202
    assert current['craft_progress']['advanced_at'] == 1791217314
    assert not current['automatic_recovery_allowed'] and banner(current) == ''
    # Serialized state retains the same clock across observer restarts; repeated
    # counts, fresh world ticks and rewritten owner timestamps cannot extend it.
    later = classify(sample(at=1791217434, last_progress_at=1791217434,
                            background_craft={**advanced, 'observed_tick': 10938000}),
                     json.loads(json.dumps(current)), 1791217434, 'campaign')
    assert later['status'] == 'no_progress' and later['attention']
    assert later['craft_progress']['advanced_at'] == 1791217314


def test_new_craft_identity_or_first_observation_is_not_progress():
    first = classify(sample(at=1100, background_craft=craft()), {}, 1100, 'campaign')
    assert first['status'] == 'no_progress'
    other = {**craft(), 'receipt': 'new-job', 'finished': 9, 'last_progress_tick': 10928000,
             'observed_tick': 10928001}
    second = classify(sample(at=1101, background_craft=other), first, 1101, 'campaign')
    assert second['status'] == 'no_progress'
    assert second['craft_progress']['advanced_at'] is None


@pytest.mark.parametrize('change', [
    {'finished': 7}, {'finished': 9}, {'last_progress_tick': 10928000},
    {'deadline_tick': 10940000}, {'requested': 21}, {'observed_tick': 10927300},
    {'finished': True}, {'observed_tick': 10938821}, {'receipt': ''},
])
def test_inconsistent_or_expired_craft_evidence_is_visible(change):
    first = classify(sample(background_craft=craft()), {}, 1000, 'campaign')
    current = classify(sample(at=1001, background_craft={**craft(), **change}),
                       first, 1001, 'campaign')
    assert current['status'] == 'unknown' and current['attention']
    assert not current['automatic_recovery_allowed']


@pytest.mark.parametrize('status', ['blocked', 'uncertain', 'completed'])
def test_advancing_craft_never_hides_controller_terminal_state(status):
    first = classify(sample(background_craft=craft()), {}, 1000, 'campaign')
    advanced = {**craft(), 'finished': 9, 'last_progress_tick': 10928000,
                'observed_tick': 10928001}
    current = classify(sample(at=1001, background_craft=advanced, checkpoint_status=status),
                       first, 1001, 'campaign')
    assert current['status'] == status
    assert current['attention'] == (status != 'completed')


def test_background_projection_binds_current_checkpoint_job_and_attempt():
    from jev_factorio.progress_watch import background_sample
    from jev_factorio.telemetry import fingerprint
    parameters = {'receipt': 'craft-1', 'batches': 20, 'recipe': 'logistic-science-pack'}
    step = {'action': 'factory_craft_job', 'parameters': parameters}
    checkpoint = {'session_id': 'campaign', 'last_tick': 200,
                  'background_step': step,
                  'background_job': {'session_id': 'campaign', 'parameters': parameters,
                                     'plan_id': 'craft', 'failed': '', 'finished': 2,
                                     'started_tick': 100, 'last_progress_tick': 190,
                                     'deadline_tick': 500},
                  'background_attempt': {'action': 'factory_craft_job', 'plan_id': 'craft',
                                         'receipt': 'craft-1', 'step_index': 0,
                                         'step_sha256': fingerprint(step)}}
    assert background_sample(checkpoint) == {
        'receipt': 'craft-1', 'requested': 20, 'finished': 2, 'started_tick': 100,
        'last_progress_tick': 190, 'deadline_tick': 500, 'observed_tick': 200}
    for key, value in [('receipt', 'other'), ('step_index', 1), ('step_sha256', '0' * 64)]:
        changed = {**checkpoint, 'background_attempt': {**checkpoint['background_attempt'], key: value}}
        with pytest.raises(ValueError, match='unbound'):
            background_sample(changed)
    with pytest.raises(ValueError, match='unbound'):
        background_sample({**checkpoint, 'session_id': 'other'})
    assert background_sample({'background_job': None}) is None


@pytest.mark.parametrize('reason', ['low choice confidence', 'Candidate evidence insufficient'])
def test_native_craft_counters_distinguish_foreground_policy_wait(reason):
    # Observed V27 receipt/counters at 02:25:45 and 02:26:00 UTC. Other sample
    # envelope fields below are test inputs, not a second native observation.
    recorded = dict(receipt='1779cc5ea58c4085a53769a0e0635fdd', requested=20,
        finished=16, started_tick=13092548, last_progress_tick=13097366,
        deadline_tick=13104548, observed_tick=13097376)
    waiting = sample(at=1791253545, last_progress_at=1791253545,
        checkpoint_status='blocked', owner_phase='blocked', reason=reason,
        pending=True, background_craft=recorded)
    first = classify(waiting, {}, 1791253545, 'campaign')
    assert first['status'] == 'blocked'  # First sample alone is not movement.
    advanced = {**recorded, 'finished': 18, 'last_progress_tick': 13097968,
                'observed_tick': 13097995}
    second = classify({**waiting, 'at': 1791253560, 'background_craft': advanced},
                      first, 1791253560, 'campaign')
    assert second['status'] == 'crafting' and not second['attention']
    assert second['foreground_status'] == 'blocked' and second['foreground_reason'] == reason
    assert second['blocked_age_seconds'] == second['progress_age_seconds'] == 15
    assert second['last_progress_at'] == first['last_progress_at']
    assert second['automatic_recovery_allowed'] is False
    assert banner(second) == f'JEV crafting: 18/20 batches | Foreground waiting: {reason}'
    # Observer restart, unchanged counters and a fresh heartbeat do not extend
    # the craft-progress window or erase the original foreground wait time.
    stale = classify({**waiting, 'at': 1791253680, 'background_craft': advanced},
                     json.loads(json.dumps(second)), 1791253680, 'campaign')
    assert stale['status'] == 'blocked' and stale['attention']
    assert stale['blocked_age_seconds'] == 135
    finished = classify({**waiting, 'at': 1791253561, 'background_craft': None},
                        second, 1791253561, 'campaign')
    assert finished['status'] == 'blocked' and finished['attention']
    assert finished['blocked_age_seconds'] == 16


@pytest.mark.parametrize('change,expected', [
    ({'checkpoint_status': 'uncertain'}, 'uncertain'),
    ({'reason': 'native reconciliation required'}, 'blocked'),
    ({'owner_alive': False}, 'stopped'), ({'child_alive': False}, 'stopped'),
    ({'owner_phase': 'stopped_by_service_owner'}, 'stopped'),
    ({'checkpoint_status': 'completed'}, 'completed'),
])
def test_crafting_label_never_overrides_fault_stop_or_completion(change, expected):
    waiting = sample(checkpoint_status='blocked', reason='low choice confidence', background_craft=craft())
    first = classify(waiting, {}, 1000, 'campaign')
    advanced = {**craft(), 'finished': 9, 'last_progress_tick': 10928000, 'observed_tick': 10928001}
    second = classify({**waiting, 'at': 1001, 'background_craft': advanced, **change},
                      first, 1001, 'campaign')
    assert second['status'] == expected
    assert not second['automatic_recovery_allowed']


def native_research_pair(index=0):
    from datetime import datetime
    pair = json.loads((Path(__file__).parent / 'fixtures/native-v27-research-progress.json').read_text())['pairs'][index]
    observation, validation = pair['observation'], pair['observation_validated']
    payload = observation['payload']
    now = datetime.fromisoformat(validation['time']['utc'].replace('Z', '+00:00')).timestamp()
    args = dict(session_id=payload['session_id'], checkpoint_tick=payload['factorio_tick'],
                execution_id=payload['supervisor_provenance']['execution_id'], now=now)
    return observation, validation, args


@pytest.mark.parametrize('reason', ['low choice confidence', 'Candidate evidence insufficient'])
def test_native_research_progress_preserves_foreground_wait_and_expires(reason):
    from jev_factorio.progress_watch import research_sample
    states = []
    for index in (0, 1):
        observation, validation, args = native_research_pair(index)
        research = research_sample(observation, validation, **args)
        # Heartbeat and completed-action fields are test inputs. Research and
        # observation pairing/timestamps come from the retained native records.
        current = sample(session_id=args['session_id'], at=args['now'],
                         last_progress_at=args['now']-200, checkpoint_status='blocked',
                         reason=reason, native_research=research)
        states.append(classify(current, states[-1] if states else {}, args['now'], args['session_id']))
    first, second = states
    assert first['status'] == 'blocked'
    assert second['status'] == 'researching' and not second['attention']
    assert second['foreground_status'] == 'blocked' and second['foreground_reason'] == reason
    assert second['last_progress_at'] == first['last_progress_at']
    assert second['blocked_since'] == first['blocked_since']
    assert 'automation-2 38.3%' in banner(second)
    assert not second['automatic_recovery_allowed']
    later = args['now']+120
    stale = classify({**current, 'at': later}, json.loads(json.dumps(second)), later, args['session_id'])
    assert stale['status'] == 'blocked' and stale['attention']
    assert stale['blocked_since'] == first['blocked_since']
    stopped_research = classify({**current, 'native_research': None}, second, args['now'], args['session_id'])
    assert stopped_research['status'] == 'blocked'
    replaced = classify({**current, 'native_research': {**research, 'technology': 'other'}}, second, args['now'], args['session_id'])
    assert replaced['status'] == 'blocked'


@pytest.mark.parametrize('fault', ['validation', 'observation_id', 'session', 'execution',
                                  'world', 'tick', 'future_checkpoint', 'stale', 'future', 'paused'])
def test_research_projection_rejects_unaccepted_mismatched_or_stale_records(fault):
    from jev_factorio.progress_watch import research_sample
    observation, validation, args = native_research_pair()
    if fault == 'validation': validation['payload']['accepted'] = False
    elif fault == 'observation_id': validation['payload']['observation_id'] = 'other'
    elif fault == 'session': validation['payload']['session_id'] = 'other'
    elif fault == 'execution': validation['payload']['supervisor_provenance']['execution_id'] = 'other'
    elif fault == 'world': observation['payload']['snapshot']['world_kind'] = 'mock'
    elif fault == 'tick': validation['payload']['factorio_tick'] -= 1
    elif fault == 'future_checkpoint': args['checkpoint_tick'] -= 1
    elif fault == 'stale': args['now'] += 31
    elif fault == 'future': args['now'] -= 3
    elif fault == 'paused': observation['payload']['snapshot']['factory']['acceptance_runtime']['tick_paused'] = True
    with pytest.raises(ValueError): research_sample(observation, validation, **args)


@pytest.mark.parametrize('change,expected', [
    ({'checkpoint_status': 'uncertain'}, 'uncertain'),
    ({'reason': 'native reconciliation required'}, 'blocked'),
    ({'owner_alive': False}, 'stopped'), ({'child_alive': False}, 'stopped'),
    ({'owner_phase': 'stopped_by_service_owner'}, 'stopped'),
    ({'checkpoint_status': 'completed'}, 'completed'),
    ({'checkpoint_status': 'running'}, 'progressing'),
])
def test_research_progress_never_overrides_fault_stop_or_completion(change, expected):
    research = dict(technology='automation-2', force_index=1, progress=.2, observed_tick=100)
    current = sample(checkpoint_status='blocked', reason='low choice confidence', native_research=research)
    first = classify(current, {}, 1000, 'campaign')
    second = classify({**current, 'at': 1001, 'native_research': {**research, 'progress': .3, 'observed_tick': 101}, **change}, first, 1001, 'campaign')
    assert second['status'] == expected
    assert not second['automatic_recovery_allowed']


@pytest.mark.parametrize('change', [dict(progress=.1, observed_tick=101), dict(progress=.3),
                                  dict(progress=float('nan')), dict(observed_tick=99)])
def test_invalid_research_counters_never_claim_progress(change):
    research = dict(technology='automation-2', force_index=1, progress=.2, observed_tick=100)
    first = classify(sample(native_research=research), {}, 1000, 'campaign')
    second = classify(sample(at=1001, native_research={**research, **change}), first, 1001, 'campaign')
    assert second['status'] == 'unknown' and second['attention']


def test_research_world_tick_alone_does_not_refresh_progress():
    research = dict(technology='automation-2', force_index=1, progress=.2, observed_tick=100)
    first = classify(sample(native_research=research), {}, 1000, 'campaign')
    second = classify(sample(at=1100, native_research={**research, 'observed_tick': 999}), first, 1100, 'campaign')
    assert second['status'] == 'no_progress' and second['research_progress']['advanced_at'] is None


@pytest.mark.parametrize('kind', ['native_research', 'background_craft'])
def test_long_observer_gap_requires_new_baseline_before_progress_credit(kind):
    if kind == 'native_research':
        before = dict(technology='automation-2', force_index=1, progress=.2, observed_tick=100)
        after = {**before, 'progress': .3, 'observed_tick': 101}
    else:
        before = craft()
        after = {**before, 'finished': 9, 'last_progress_tick': 10928000, 'observed_tick': 10928001}
    current = sample(checkpoint_status='blocked', reason='low choice confidence', **{kind: before})
    first = classify(current, {}, 1000, 'campaign')
    second = classify({**current, 'at': 1121, kind: after}, json.loads(json.dumps(first)), 1121, 'campaign')
    assert second['status'] == 'blocked' and second['attention']
    assert second['blocked_since'] == 1000
    key = 'research_progress' if kind == 'native_research' else 'craft_progress'
    assert second[key]['advanced_at'] is None
