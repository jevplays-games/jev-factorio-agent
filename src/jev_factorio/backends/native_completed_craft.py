"""Bind a retained completed native handcraft receipt to prior verification."""
from copy import deepcopy
from types import SimpleNamespace

from ..craft_jobs import counts, identifier, natural, receipt_for


def checkpoint_completed_craft(data):
    if any(data.get(key) is not None for key in
           ('pending', 'attempt', 'background_job', 'background_attempt', 'background_step')):
        return None
    events = [row for row in data.get('history', [])
              if row.get('kind') == 'background_job_completed']
    if not events:
        return None
    event = events[-1]
    attempts = [row for row in data.get('attempt_outcomes', [])
                if row.get('receipt') == event.get('job')
                and row.get('action') == 'factory_craft_job'
                and row.get('outcome') == 'verified']
    if len(attempts) != 1:
        raise ValueError('Completed craft has no unique checkpoint verification')
    attempt = attempts[0]
    if attempt['plan_id'] != event.get('plan') or attempt['finished_tick'] != event.get('tick'):
        raise ValueError('Completed craft checkpoint verification differs')
    return validate_completed_craft_binding({
        'id': event['job'], 'plan_id': event['plan'], 'outputs': event['outputs'],
        'started_tick': attempt['started_tick'], 'verified_tick': event['tick']})


def validate_completed_craft_binding(value):
    if (not isinstance(value, dict)
            or set(value) != {'id', 'plan_id', 'outputs', 'started_tick', 'verified_tick'}):
        raise ValueError('Invalid completed craft checkpoint binding')
    identifier(value['id']); identifier(value['plan_id'])
    counts(value['outputs'], positive=True)
    if natural(value['started_tick']) > natural(value['verified_tick']):
        raise ValueError('Completed craft checkpoint clock regressed')
    return deepcopy(value)


def verify_completed_craft(receipt, binding, result, tick):
    if binding is None:
        if receipt is not False:
            raise ValueError('Uncheckpointed retained craft receipt')
        return
    binding = validate_completed_craft_binding(binding)
    if not isinstance(receipt, dict):
        raise ValueError('Completed craft receipt disappeared')
    actor = {key: receipt.get(key) for key in
             ('player_index', 'surface_index', 'force_index')}
    actor.update(session_id=result['session_id'], unit_number=result['actor_unit'])
    snapshot = SimpleNamespace(session_id=result['session_id'], tick=tick, factory={
        'craft_jobs_protocol': 1, 'craft_job_actor': actor, 'craft_job': receipt,
        'player_connected': True, 'player_bound': True})
    receipt_for({'receipt': binding['id'], 'recipe': receipt.get('recipe'),
                 'batches': receipt.get('requested')}, snapshot)
    if (receipt['status'] != 'completed' or receipt.get('error') is not None
            or binding['plan_id'] != 'factory:factory_craft:' + receipt['recipe']
            or receipt['outputs'] != binding['outputs']
            or not binding['started_tick'] <= receipt['started_tick']
                   <= receipt['completed_tick'] <= binding['verified_tick'] <= tick):
        raise ValueError('Retained craft differs from checkpoint verification')
