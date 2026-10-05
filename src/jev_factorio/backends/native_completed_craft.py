"""Bind a retained completed native handcraft receipt to prior verification."""
from copy import deepcopy
from dataclasses import asdict
import math
import json
import re
from types import SimpleNamespace

from ..craft_jobs import counts, identifier, natural, receipt_for


def checkpoint_background_craft(data):
    """Bind unresolved background work without claiming prior verification."""
    if data.get('background_job') is None:
        return None
    from ..memory import checkpoint_memory_type
    memory = checkpoint_memory_type(data).from_bytes(
        json.dumps(data, sort_keys=True, allow_nan=False).encode(),
        data['session_id'], data['target'])
    if any(getattr(memory, key, None) is not None for key in
           ('pending', 'attempt', 'active_plan', 'transfer_recovery')):
        raise ValueError('Background attachment requires no foreground work')
    binding = {'job': deepcopy(memory.background_job),
               'attempt': deepcopy(memory.background_attempt),
               'step': deepcopy(memory.background_step), 'checkpoint_tick': memory.last_tick}
    validate_background_craft_binding(binding)
    return binding


def validate_background_craft_binding(binding):
    from ..background import _validate_background_step
    from ..craft_jobs import CraftJob
    from ..telemetry import validate_attempt
    if not isinstance(binding, dict) or set(binding) != {'job', 'attempt', 'step', 'checkpoint_tick'}:
        raise ValueError('Invalid background craft attachment binding')
    job = CraftJob.from_dict(binding['job'])
    attempt = binding['attempt']
    validate_attempt(attempt)
    _validate_background_step(job, attempt, binding['step'])
    if (job.failed or attempt['plan_id'] != job.plan_id or attempt['step_index'] != 0
            or attempt['receipt'] != job.parameters['receipt']
            or attempt['started_tick'] > job.started_tick
            or natural(binding['checkpoint_tick']) < job.last_progress_tick):
        raise ValueError('Background craft attachment differs from checkpoint')
    return job


def verify_background_craft(receipt, inventory, binding, result, tick):
    """Qualify completed native work; only the controller may commit its outcome."""
    job = validate_background_craft_binding(binding)
    if (job.session_id != result['session_id'] or job.actor['unit_number'] != result['actor_unit']
            or tick < binding['checkpoint_tick'] or not isinstance(receipt, dict)
            or receipt.get('status') != 'completed' or receipt.get('error') is not None
            or counts(inventory) != inventory or set(inventory) != set(job.outputs)):
        raise ValueError('Retained background craft requires reconciliation')
    snapshot = SimpleNamespace(session_id=result['session_id'], tick=tick, inventory=inventory,
        factory={'craft_jobs_protocol': 1, 'craft_job': receipt,
                 'craft_job_actor': {**job.actor, 'session_id': job.session_id},
                 'player_connected': True, 'player_bound': True})
    if not job.observe(snapshot):
        raise ValueError('Retained background craft has not completed')


def checkpoint_completed_craft(data):
    if any(data.get(key) is not None for key in
           ('pending', 'attempt', 'background_job', 'background_attempt', 'background_step')):
        return None
    events = [row for row in data.get('history', [])
              if row.get('kind') == 'background_job_completed']
    crafts = [row for row in data.get('attempt_outcomes', [])
              if row.get('action') == 'factory_craft_job']
    if not crafts:
        if events:
            raise ValueError('Completed craft has no unique checkpoint verification')
        return None
    latest = max(crafts, key=lambda row: row['started_tick'])
    if (latest.get('outcome') != 'verified'
            or sum(row.get('receipt') == latest.get('receipt') for row in crafts) != 1):
        raise ValueError('Latest craft has no unique verified attempt')
    matching = [row for row in events if row.get('job') == latest.get('receipt')]
    if not matching:
        # Old checkpoints may have rotated the event away. Native attachment
        # must reconstruct the exact committed Step from the receipt and recipe
        # and match its retained fingerprint before accepting this binding.
        return validate_completed_craft_binding({
            'id': latest['receipt'], 'plan_id': latest['plan_id'],
            'started_tick': latest['started_tick'], 'verified_tick': latest['finished_tick'],
            'step_sha256': latest['step_sha256']})
    if len(matching) != 1:
        raise ValueError('Duplicate completed craft event')
    event = matching[0]
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
        'started_tick': attempt['started_tick'], 'verified_tick': event['tick'],
        **({'step_sha256': attempt['step_sha256']} if 'step_sha256' in attempt else {})})


def validate_completed_craft_binding(value):
    if (not isinstance(value, dict)
            or set(value) not in ({'id', 'plan_id', 'outputs', 'started_tick', 'verified_tick'},
                                  {'id', 'plan_id', 'step_sha256', 'started_tick', 'verified_tick'},
                                  {'id', 'plan_id', 'outputs', 'step_sha256', 'started_tick', 'verified_tick'})):
        raise ValueError('Invalid completed craft checkpoint binding')
    identifier(value['id']); identifier(value['plan_id'])
    if 'outputs' in value:
        counts(value['outputs'], positive=True)
    if 'step_sha256' in value and (not isinstance(value['step_sha256'], str)
                                  or not re.fullmatch('[0-9a-f]{64}', value['step_sha256'])):
        raise ValueError('Invalid completed craft step fingerprint')
    if natural(value['started_tick']) > natural(value['verified_tick']):
        raise ValueError('Completed craft checkpoint clock regressed')
    return deepcopy(value)


def verify_completed_craft(receipt, binding, result, tick, *, recipe=None):
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
            or 'step_sha256' not in binding and binding['plan_id'] != 'factory:factory_craft:' + receipt['recipe']
            or 'outputs' in binding and receipt['outputs'] != binding['outputs']
            or not binding['started_tick'] <= receipt['started_tick']
                   <= receipt['completed_tick'] <= binding['verified_tick'] <= tick):
        raise ValueError('Retained craft differs from checkpoint verification')

    if 'step_sha256' in binding:
        verify_completed_step(receipt, binding, recipe)


def verify_completed_step(receipt, binding, recipe):
    """Reconstruct only the canonical paid handcraft; unknown shapes stay held."""
    from ..skills import Step
    from ..telemetry import fingerprint
    if (not isinstance(recipe, dict) or set(recipe) != {'energy', 'ingredients', 'products'}
            or type(recipe['energy']) not in {int, float}
            or not math.isfinite(recipe['energy']) or recipe['energy'] <= 0):
        raise ValueError('Completed craft native recipe is unavailable')
    batches = receipt['requested']
    expected = {}
    for key in ('ingredients', 'products'):
        rows = recipe[key]
        if (not isinstance(rows, list) or not 1 <= len(rows) <= 32
                or key == 'products' and len(rows) != 1):
            raise ValueError('Unsupported completed craft recipe')
        values = {}
        for row in rows:
            if (not isinstance(row, dict) or row.get('type') != 'item'
                    or type(row.get('amount')) is not int or row['amount'] <= 0
                    or row.get('probability', 1) != 1
                    or any(k in row for k in ('amount_min', 'amount_max'))):
                raise ValueError('Unsupported completed craft recipe quantity')
            item = identifier(row.get('name'))
            if item in values:
                raise ValueError('Duplicate completed craft ingredient')
            values[item] = row['amount'] * batches
        expected[key] = values
    if receipt['inputs'] != expected['ingredients'] or receipt['outputs'] != expected['products']:
        raise ValueError('Completed craft receipt differs from native recipe')
    item, amount = next(iter(receipt['outputs'].items()))
    step = Step('factory_craft_job', 'craft_job_complete', item,
                receipt['baseline'][item] + amount,
                costs=deepcopy(receipt['inputs']),
                timeout_ticks=max(1800, math.ceil(recipe['energy'] * batches * 120)),
                parameters={'recipe': receipt['recipe'], 'batches': batches,
                            'receipt': receipt['id']})
    if fingerprint(asdict(step)) != binding['step_sha256']:
        raise ValueError('Completed craft differs from committed step fingerprint')
