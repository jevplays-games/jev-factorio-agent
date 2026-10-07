"""Persistent observation of useful progress. Never restart or actuate a game.

The probe is an operator-pinned, read-only adapter returning small JSON. An
optional display adapter receives only the classified status on stdin. Neither
adapter is a gameplay controller, model client or recovery authority.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def background_sample(checkpoint):
    """Project the controller's receipt-bound craft observation for its probe.

    This reads retained native evidence; it cannot verify completion or dispatch
    anything. The trusted probe must read an atomic, identity-pinned checkpoint.
    """
    job = checkpoint.get('background_job')
    if job is None:
        return None
    attempt = checkpoint.get('background_attempt')
    step = checkpoint.get('background_step')
    if not all(isinstance(x, dict) for x in (job, attempt, step)):
        raise ValueError('unbound background craft')
    parameters = job.get('parameters')
    if (not isinstance(parameters, dict) or job.get('failed') != ''
            or job.get('session_id') != checkpoint.get('session_id')
            or attempt.get('action') != 'factory_craft_job'
            or attempt.get('plan_id') != job.get('plan_id')
            or attempt.get('receipt') != parameters.get('receipt')
            or attempt.get('step_index') != 0
            or attempt.get('step_sha256') != hashlib.sha256(json.dumps(
                step, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()).hexdigest()
            or step.get('action') != 'factory_craft_job'
            or step.get('parameters') != parameters):
        raise ValueError('unbound background craft')
    return dict(receipt=parameters.get('receipt'), requested=parameters.get('batches'),
                finished=job.get('finished'), started_tick=job.get('started_tick'),
                last_progress_tick=job.get('last_progress_tick'),
                deadline_tick=job.get('deadline_tick'), observed_tick=checkpoint.get('last_tick'))


def craft_progress(sample, previous, now):
    """Require observed counter movement, not a fresh timestamp or new job ID."""
    if sample is None:
        return None
    if not isinstance(sample, dict):
        raise ValueError('invalid tracked craft evidence')
    keys = ('requested', 'finished', 'started_tick', 'last_progress_tick',
            'deadline_tick', 'observed_tick')
    receipt = sample.get('receipt')
    if (not isinstance(receipt, str) or not 1 <= len(receipt) <= 128
            or any(type(sample.get(k)) is not int or sample[k] < 0 for k in keys)
            or not 0 <= sample['finished'] < sample['requested'] <= 200
            or not sample['started_tick'] <= sample['last_progress_tick']
            <= sample['observed_tick'] < sample['deadline_tick']):
        raise ValueError('invalid tracked craft evidence')
    result = {k: sample[k] for k in ('receipt', *keys)}
    result['advanced_at'] = None
    if isinstance(previous, dict) and previous.get('receipt') == receipt:
        fixed = ('requested', 'started_tick', 'deadline_tick')
        counters = ('finished', 'last_progress_tick', 'observed_tick')
        if (any(previous.get(k) != sample[k] for k in fixed)
                or any(type(previous.get(k)) is not int or sample[k] < previous[k]
                       for k in counters)):
            raise ValueError('tracked craft evidence regressed or changed')
        count_moved = sample['finished'] > previous['finished']
        tick_moved = sample['last_progress_tick'] > previous['last_progress_tick']
        if count_moved != tick_moved:
            raise ValueError('tracked craft counter and event tick disagree')
        stamp = previous.get('advanced_at')
        if stamp is not None and (not number(stamp) or not 0 <= stamp <= now + 2):
            raise ValueError('invalid tracked craft progress time')
        result['advanced_at'] = now if count_moved else stamp
    return result


def research_sample(observation, validation, *, session_id, execution_id, checkpoint_tick, now):
    """Project a recent accepted native observation from a trusted event reader.

    The caller pins the campaign, execution and read-only log source. Event
    pairing is required: a model request or an unvalidated snapshot is not an
    accepted native observation. This does not verify research completion.
    """
    try:
        observed, accepted = observation['payload'], validation['payload']
        snapshot = observed['snapshot']; factory = snapshot['factory']
        tick = snapshot['tick']; runtime = factory['acceptance_runtime']
        if (observation['event_type'] != 'observation'
                or validation['event_type'] != 'observation_validated'
                or observed['status'] != 'ok' or accepted['accepted'] is not True
                or not isinstance(observed['observation_id'], str)
                or not observed['observation_id']
                or observed['observation_id'] != accepted['observation_id']
                or snapshot['world_kind'] != 'fle'
                or type(tick) is not int or type(checkpoint_tick) is not int
                or not 0 <= tick <= checkpoint_tick
                or snapshot['session_id'] != session_id
                or factory['tick'] != tick or factory['observation_snapshot_schema'] != 2
                or runtime['schema'] != 1 or runtime['session_id'] != session_id
                or type(runtime['force_index']) is not int or runtime['force_index'] < 1
                or runtime['speed'] != 1 or runtime['tick_paused'] is not False):
            raise ValueError('unbound research observation')
        for event, payload in ((observation, observed), (validation, accepted)):
            stamp = datetime.fromisoformat(event['time']['utc'].replace('Z', '+00:00'))
            if (stamp.tzinfo is None or not now - 30 <= stamp.timestamp() <= now + 2
                    or payload['session_id'] != session_id or payload['world_kind'] != 'fle'
                    or payload['factorio_tick'] != tick or event['time']['factorio_tick'] != tick
                    or payload['supervisor_provenance']['execution_id'] != execution_id):
                raise ValueError('stale or unbound research observation')
        technology = factory.get('research')
        if not technology:
            return None
        return {'technology': technology, 'force_index': runtime['force_index'],
                'progress': factory['research_progress'], 'observed_tick': tick}
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError('invalid research observation') from error


def research_progress(sample, previous, now):
    """Count only advancing native research fraction and observation tick."""
    if sample is None:
        return None
    if (not isinstance(sample, dict)
            or not isinstance(sample.get('technology'), str)
            or not 1 <= len(sample['technology']) <= 128
            or type(sample.get('force_index')) is not int or sample['force_index'] < 1
            or not number(sample.get('progress')) or not 0 <= sample['progress'] < 1
            or type(sample.get('observed_tick')) is not int or sample['observed_tick'] < 0):
        raise ValueError('invalid tracked research evidence')
    result = {k: sample[k] for k in ('technology', 'force_index', 'progress', 'observed_tick')}
    result['advanced_at'] = None
    if (isinstance(previous, dict) and all(previous.get(k) == sample[k]
                                         for k in ('technology', 'force_index'))):
        old_progress, old_tick = previous.get('progress'), previous.get('observed_tick')
        if (not number(old_progress) or type(old_tick) is not int
                or sample['progress'] < old_progress or sample['observed_tick'] < old_tick
                or (sample['progress'] > old_progress and sample['observed_tick'] == old_tick)):
            raise ValueError('tracked research evidence regressed or changed')
        stamp = previous.get('advanced_at')
        if stamp is not None and (not number(stamp) or not 0 <= stamp <= now + 2):
            raise ValueError('invalid tracked research progress time')
        result['advanced_at'] = now if sample['progress'] > old_progress else stamp
    return result


def smelting_sample(observation, validation, **binding):
    """Project registered native furnaces from the same accepted event contract."""
    research_sample(observation, validation, **binding)  # Also validates idle research.
    factory = observation['payload']['snapshot']['factory']
    entities = factory.get('entities')
    if not isinstance(entities, dict):
        raise ValueError('missing native furnace observations')
    machines = []
    for recipe in ('iron-plate', 'copper-plate', 'steel-plate'):
        role = 'recipe:' + recipe
        row = entities.get(role)
        if row is None:
            continue
        if (not isinstance(row, dict) or row.get('name') not in {
                'stone-furnace', 'steel-furnace', 'electric-furnace'}):
            raise ValueError('invalid registered furnace')
        # Empty furnaces have no active recipe. Neither idle inventory nor an
        # unrelated recipe supplies activity credit for this registered role.
        if row.get('recipe') != recipe:
            continue
        machines.append({key: row.get(key) for key in (
            'unit_number', 'name', 'recipe', 'products_finished', 'crafting')} | {'role': role})
    return {'observed_tick': factory['tick'],
            'force_index': factory['acceptance_runtime']['force_index'], 'machines': machines}


def smelting_progress(sample, previous, now):
    """Require a stable furnace identity and increasing native completed batches."""
    if sample is None:
        return None
    if (not isinstance(sample, dict) or type(sample.get('observed_tick')) is not int
            or sample['observed_tick'] < 0 or type(sample.get('force_index')) is not int
            or sample['force_index'] < 1 or not isinstance(sample.get('machines'), list)
            or len(sample['machines']) > 3):
        raise ValueError('invalid native smelting evidence')
    old = previous if isinstance(previous, dict) else {}
    result = {'observed_tick': sample['observed_tick'],
              'force_index': sample['force_index'], 'machines': []}
    seen = set()
    for machine in sample['machines']:
        if (not isinstance(machine, dict) or type(machine.get('unit_number')) is not int
                or machine['unit_number'] <= 0 or machine.get('recipe') not in {
                    'iron-plate', 'copper-plate', 'steel-plate'}
                or machine.get('role') != 'recipe:' + machine['recipe']
                or machine.get('name') not in {'stone-furnace', 'steel-furnace', 'electric-furnace'}
                or type(machine.get('products_finished')) is not int
                or machine['products_finished'] < 0 or type(machine.get('crafting')) is not bool
                or machine['role'] in seen or machine['unit_number'] in seen):
            raise ValueError('invalid native furnace counter')
        seen.update((machine['role'], machine['unit_number']))
        identity = ('unit_number', 'role', 'name', 'recipe')
        prior = next((row for row in old.get('machines', []) if isinstance(row, dict)
                      and all(row.get(k) == machine[k] for k in identity)), None)
        row = dict(machine, advanced_at=None)
        if prior is not None and old.get('force_index') == sample['force_index']:
            count, tick = prior.get('products_finished'), old.get('observed_tick')
            if (type(count) is not int or type(tick) is not int
                    or machine['products_finished'] < count or sample['observed_tick'] < tick
                    or machine['products_finished'] > count and sample['observed_tick'] == tick):
                raise ValueError('native furnace counter regressed or changed')
            stamp = prior.get('advanced_at')
            if stamp is not None and (not number(stamp) or not 0 <= stamp <= now + 2):
                raise ValueError('invalid furnace progress time')
            row['advanced_at'] = now if machine['products_finished'] > count else stamp
        result['machines'].append(row)
    return result


def completed_progress_sample(checkpoint, owner, *, owner_qualified=False):
    """Project completed work without treating bounded history eviction as loss.

    The adapter must independently bind a qualified owner to the exact reviewed
    script, execution, live child and checkpoint session. Its retained watermark
    supplements history; it never supplies a new completion time.
    """
    session = checkpoint.get('session_id')
    tick = checkpoint.get('last_tick')
    history = checkpoint.get('history')
    if (not isinstance(session, str) or not session
            or type(tick) is not int or tick < 0
            or not isinstance(history, list) or len(history) > 65):
        raise ValueError('invalid completed progress checkpoint')
    useful = []
    for row in history:
        if not isinstance(row, dict):
            raise ValueError('invalid completed progress history')
        if (row.get('kind') == 'background_job_completed'
                or row.get('kind') == 'step_verified' and row.get('action') not in
                (None, 'factory_wait', 'idle', 'observe', 'verify')):
            value = row.get('tick')
            if type(value) is not int or not 0 <= value <= tick:
                raise ValueError('invalid completed progress tick')
            useful.append(value)
    if owner_qualified:
        watermark = owner.get('useful_tick')
        if (owner.get('session_id') != session or type(watermark) is not int
                or not 0 <= watermark <= tick):
            raise ValueError('invalid qualified owner progress watermark')
        useful.append(watermark)
    return {'progress_tick': max(useful, default=0),
            'last_progress_at': owner.get('last_useful_action_at')}


def classify(sample, previous, now, session_id, *, heartbeat_seconds=30,
             stall_seconds=120):
    """Do not turn process liveness, tick movement or a restart into progress."""
    previous = previous if isinstance(previous, dict) else {}
    prior = previous if previous.get('session_id') == session_id else {}
    result = {'schema': 1, 'at': now, 'session_id': session_id,
              'status': 'unknown', 'reason': 'probe unavailable', 'attention': True,
              'last_progress_at': prior.get('last_progress_at'),
              'progress_tick': prior.get('progress_tick'),
              'progress_age_seconds': None, 'blocked_since': None,
              'blocked_age_seconds': None, 'pending': False,
              'automatic_recovery_allowed': False, 'craft_progress': None,
              'research_progress': None, 'smelting_progress': None}
    if number(prior.get('at')) and now + 2 < prior['at']:
        result['reason'] = 'monitor clock regressed'
        return result
    if not isinstance(sample, dict) or sample.get('session_id') != session_id:
        return result
    at = sample.get('at')
    if not number(at) or at > now + 2 or now - at > heartbeat_seconds:
        result['reason'] = 'stale or invalid controller heartbeat'
        return result
    tick, stamp = sample.get('progress_tick'), sample.get('last_progress_at')
    if type(tick) is not int or tick < 0 or (stamp is not None and (
            not number(stamp) or stamp < 0 or stamp > now + 2)):
        result['reason'] = 'invalid progress evidence'
        return result
    prior_tick = prior.get('progress_tick')
    if type(prior_tick) is int and tick < prior_tick:
        result['reason'] = 'verified progress tick regressed'
        return result
    # A repeated historical tick cannot be made fresh by a rewritten timestamp.
    if prior_tick == tick and number(prior.get('last_progress_at')):
        stamp = prior['last_progress_at']
    # Newly learned receipts may be historical. Their supplied completion
    # time remains authoritative even when the progress watermark increases.
    result.update(progress_tick=tick, last_progress_at=stamp,
                  progress_age_seconds=max(0, now - stamp) if stamp is not None else None,
                  pending=sample.get('pending') is True)
    craft_error = None
    # After a long observer gap, a larger counter only proves work occurred
    # sometime during that gap. Establish a fresh baseline before crediting it.
    continuous_sample = number(prior.get('at')) and 0 <= now - prior['at'] < stall_seconds
    try:
        craft = craft_progress(sample.get('background_craft'),
                               prior.get('craft_progress') if continuous_sample else None, now)
        result['craft_progress'] = craft
    except ValueError as error:
        craft = None
        craft_error = str(error)
    craft_stamp = craft.get('advanced_at') if craft else None
    research_error = None
    try:
        research = research_progress(sample.get('native_research'),
                                     prior.get('research_progress') if continuous_sample else None, now)
        result['research_progress'] = research
    except ValueError as error:
        research = None
        research_error = str(error)
    research_stamp = research.get('advanced_at') if research else None
    smelting_error = None
    try:
        smelting = smelting_progress(sample.get('native_smelting'),
            prior.get('smelting_progress') if continuous_sample else None, now)
        result['smelting_progress'] = smelting
    except ValueError as error:
        smelting = None
        smelting_error = str(error)
    advancing_furnaces = [row for row in (smelting or {}).get('machines', [])
        if row['crafting'] and number(row['advanced_at'])
        and now - row['advanced_at'] < stall_seconds]
    status, phase = sample.get('checkpoint_status'), sample.get('owner_phase')
    if phase == 'stopped_by_service_owner':
        result.update(status='stopped', reason='stopped by service owner', attention=False)
    elif status == 'completed':
        result.update(status='completed', reason='campaign completed', attention=False)
    elif sample.get('owner_alive') is not True or sample.get('child_alive') is not True:
        result.update(status='stopped', reason='controller process unavailable')
    elif status in {'blocked', 'uncertain'}:
        since = prior.get('blocked_since')
        if (prior.get('status') not in {'blocked', 'uncertain', 'crafting', 'researching', 'smelting'}
                or not number(since) or since > now):
            since = now
        result.update(status=status, reason=str(sample.get('reason') or status)[:180],
                      blocked_since=since, blocked_age_seconds=now - since)
        # A policy wait affects the next foreground choice. It does not stop an
        # already admitted native craft whose receipt-bound counter is moving.
        # Native uncertainty and unrelated holds keep their attention priority.
        if (status == 'blocked' and sample.get('reason') in {
                'low choice confidence', 'Candidate evidence insufficient'}
                and not research_error and not smelting_error
                and number(craft_stamp) and now - craft_stamp < stall_seconds):
            result.update(status='crafting', reason='tracked craft is advancing; foreground decision waiting',
                          attention=False, foreground_status=status,
                          foreground_reason=sample['reason'])
        elif (status == 'blocked' and sample.get('reason') in {
                'low choice confidence', 'Candidate evidence insufficient'}
                and not craft_error and not smelting_error and number(research_stamp)
                and now - research_stamp < stall_seconds):
            result.update(status='researching', reason='native research is advancing; foreground decision waiting',
                          attention=False, foreground_status=status,
                          foreground_reason=sample['reason'])
        elif (status == 'blocked' and sample.get('reason') in {
                'low choice confidence', 'Candidate evidence insufficient'}
                and not craft_error and not research_error and advancing_furnaces):
            result.update(status='smelting',
                          reason='native furnace production is advancing; foreground decision waiting',
                          attention=False, foreground_status=status, foreground_reason=sample['reason'])
    elif status != 'running':
        result['reason'] = 'unrecognized controller state'
    elif craft_error:
        result['reason'] = craft_error
    elif research_error:
        result['reason'] = research_error
    elif smelting_error:
        result['reason'] = smelting_error
    elif number(craft_stamp) and now - craft_stamp < stall_seconds:
        result.update(status='progressing', reason='tracked craft is advancing', attention=False)
    elif number(research_stamp) and now - research_stamp < stall_seconds:
        result.update(status='progressing', reason='native research is advancing', attention=False)
    elif advancing_furnaces:
        result.update(status='progressing', reason='native furnace production is advancing', attention=False)
    elif stamp is None or now - stamp >= stall_seconds:
        result.update(status='no_progress', reason=('awaiting verified progress' if stamp is None
                      else 'no recent verified useful action'))
    else:
        result.update(status='progressing', reason='verified useful progress', attention=False)
    return result


def banner(state):
    status = state['status']
    if status == 'progressing':
        return ''
    if status == 'crafting':
        craft = state['craft_progress']
        return (f"JEV crafting: {craft['finished']}/{craft['requested']} batches"
                f" | Foreground waiting: {state['foreground_reason']}")[:240]
    if status == 'researching':
        research = state['research_progress']
        return (f"JEV researching: {research['technology']} {research['progress']:.1%}"
                f" | Foreground waiting: {state['foreground_reason']}")[:240]
    if status == 'smelting':
        return ("JEV smelting: native furnace production advancing"
                f" | Foreground waiting: {state['foreground_reason']}")[:240]
    age = state.get('progress_age_seconds')
    suffix = f' | {int(age // 60)}m since progress' if number(age) else ''
    return ('JEV ' + status.replace('_', ' ') + ': ' + state['reason'] + suffix)[:240]


def read_json(path, limit=65536):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('monitor input exceeds bound')
    return json.loads(raw)


def command(path):
    value = read_json(path)
    if (not isinstance(value, list) or not value or len(value) > 32
            or any(not isinstance(s, str) or not s or '\0' in s for s in value)
            or not Path(value[0]).is_absolute()):
        raise ValueError('adapter requires an absolute executable and argument list')
    return value


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, sort_keys=True, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)


def append_event(path, data, max_bytes=1024 * 1024, backups=3):
    path = Path(path)
    if path.exists() and path.stat().st_size >= max_bytes:
        for index in range(backups, 0, -1):
            source = path if index == 1 else path.with_name(path.name + f'.{index-1}')
            if source.exists():
                os.replace(source, path.with_name(path.name + f'.{index}'))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(data, sort_keys=True, allow_nan=False) + '\n')
        stream.flush(); os.fsync(stream.fileno())


def run_once(probe, display, state_dir, session_id, *, timeout=10,
             heartbeat_seconds=30, stall_seconds=120):
    root = Path(state_dir)
    try: previous = read_json(root / 'status.json')
    except (OSError, ValueError): previous = {}
    error = None
    try:
        result = subprocess.run(probe, capture_output=True, timeout=timeout, check=True)
        if len(result.stdout) > 65536:
            raise ValueError('probe output exceeds bound')
        sample = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        sample = None; error = type(exc).__name__
    state = classify(sample, previous, time.time(), session_id,
                     heartbeat_seconds=heartbeat_seconds, stall_seconds=stall_seconds)
    state['probe_error'] = error
    state['banner'] = banner(state)
    state['display_error'] = None
    if display:
        try:
            subprocess.run(display, input=json.dumps(state).encode(), capture_output=True,
                           timeout=timeout, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            state['display_error'] = type(exc).__name__
    keys = ('status', 'reason', 'attention', 'probe_error', 'display_error')
    if any(state.get(k) != previous.get(k) for k in keys):
        append_event(root / 'transitions.jsonl', state)
        print(json.dumps(state, sort_keys=True), flush=True)
    atomic_json(root / 'status.json', state)
    return state


def main():
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe-command-file', required=True)
    parser.add_argument('--display-command-file')
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--interval', type=float, default=15)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--heartbeat-seconds', type=float, default=30)
    parser.add_argument('--stall-seconds', type=float, default=120)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if any(not number(v) or v <= 0 for v in (
            args.interval, args.timeout, args.heartbeat_seconds, args.stall_seconds)):
        parser.error('monitor intervals must be finite and positive')
    root = Path(args.state_dir); root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (root / 'monitor.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    probe = command(args.probe_command_file)
    display = command(args.display_command_file) if args.display_command_file else None
    while True:
        started = time.monotonic()
        run_once(probe, display, root, args.session_id, timeout=args.timeout,
                 heartbeat_seconds=args.heartbeat_seconds, stall_seconds=args.stall_seconds)
        if args.once: return
        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
