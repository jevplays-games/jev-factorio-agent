"""A retained completed craft needs the exact prior checkpoint verification."""
from copy import deepcopy
import pytest

from jev_factorio.backends.native_attachment import readback
from jev_factorio.backends.native_completed_craft import checkpoint_completed_craft
from test_native_completed_attachment import settled_case
from test_craft_jobs import state, completed


def case():
    from test_native_optional_profiles import SESSION, ACTOR
    lua, client, connectors = settled_case()
    snapshot = state(); completed(snapshot)
    job = snapshot.factory['craft_job']
    job.update(session_id=SESSION, unit_number=ACTOR, surface_index=1, force_index=1)
    lua.globals().jev_fle_runtime.campaign.craft_jobs.job = lua.table_from(job, recursive=True)
    data = {'history': [{'kind': 'background_job_completed', 'job': job['id'],
             'outputs': deepcopy(job['outputs']), 'plan': 'factory:factory_craft:science',
             'tick': 200}], 'attempt_outcomes': [{'receipt': job['id'],
             'action': 'factory_craft_job', 'outcome': 'verified',
             'plan_id': 'factory:factory_craft:science', 'finished_tick': 200,
             'started_tick': 90}]}
    return lua, client, connectors, job, data


def test_retained_completed_job_with_checkpoint_receipt_attaches_without_crediting_output():
    lua, client, connectors, job, data = case()
    binding = checkpoint_completed_craft(data)
    before = lua.eval('helpers.table_to_json(jev_fle_runtime.campaign.craft_jobs.job)')
    result = readback(client, checkpoint_binding=connectors, completed_craft=binding)
    assert result['connector_snapshot_qualified'] is True
    assert lua.eval('helpers.table_to_json(jev_fle_runtime.campaign.craft_jobs.job)') == before
    assert lua.eval('next(game.get_player(1).get_main_inventory().get_contents())==nil')


@pytest.mark.parametrize('field,value', [
    ('id', 'other'), ('recipe', 'other'), ('paid', False), ('status', 'running'),
    ('status', 'invalid'), ('accepted', 9), ('finished', 9), ('queue_valid', False),
    ('unit_number', 999), ('player_index', 2), ('surface_index', 2), ('force_index', 2),
    ('completed_tick', 250), ('started_tick', 80), ('error', 'cancelled'),
])
def test_completed_receipt_mismatch_cannot_authorize_attachment(field, value):
    from lupa.lua52 import LuaError
    lua, client, connectors, job, data = case()
    lua.globals().jev_fle_runtime.campaign.craft_jobs.job[field] = value
    with pytest.raises((ValueError, RuntimeError, LuaError)):
        readback(client, checkpoint_binding=connectors,
                 completed_craft=checkpoint_completed_craft(data))


def test_completed_native_job_without_checkpoint_receipt_still_requires_reconciliation():
    from lupa.lua52 import LuaError
    _, client, connectors, _, _ = case()
    with pytest.raises(LuaError):
        readback(client, checkpoint_binding=connectors)


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'clock', 'plan'])
def test_checkpoint_completion_must_match_one_verified_attempt(change):
    _, _, _, _, data = case()
    if change == 'missing': data['attempt_outcomes'].clear()
    elif change == 'duplicate': data['attempt_outcomes'] *= 2
    elif change == 'clock': data['attempt_outcomes'][0]['finished_tick'] -= 1
    elif change == 'plan': data['attempt_outcomes'][0]['plan_id'] = 'other'
    with pytest.raises(ValueError):
        checkpoint_completed_craft(data)


def test_pending_checkpoint_cannot_supply_completed_job_authority():
    _, _, _, _, data = case()
    data['background_job'] = {'retained': True}
    assert checkpoint_completed_craft(data) is None


def rotated_case():
    from dataclasses import asdict
    from jev_factorio.skills import Step
    from jev_factorio.telemetry import fingerprint
    lua, client, connectors, job, data = case()
    recipe = {'energy': .5, 'ingredients': [{'type': 'item', 'name': 'iron-plate', 'amount': 1}],
              'products': [{'type': 'item', 'name': 'science', 'amount': 1}]}
    force = lua.eval('jev_fle_runtime.agent_characters[1].force')
    force.recipes = lua.table_from({'science': recipe}, recursive=True)
    step = Step('factory_craft_job', 'craft_job_complete', 'science', 13,
                costs={'iron-plate': 10}, timeout_ticks=1800,
                parameters={'recipe': 'science', 'batches': 10, 'receipt': job['id']})
    data['attempt_outcomes'][0]['step_sha256'] = fingerprint(asdict(step))
    data['history'] = []
    return lua, client, connectors, job, data


def test_rotated_event_uses_exact_committed_step_and_current_native_recipe():
    lua, client, connectors, job, data = rotated_case()
    binding = checkpoint_completed_craft(data)
    assert 'outputs' not in binding and 'step_sha256' in binding
    before = lua.eval('helpers.table_to_json(jev_fle_runtime.campaign.craft_jobs.job)')
    assert readback(client, checkpoint_binding=connectors,
                    completed_craft=binding)['connector_snapshot_qualified']
    assert lua.eval('helpers.table_to_json(jev_fle_runtime.campaign.craft_jobs.job)') == before


@pytest.mark.parametrize('change', ['fingerprint', 'baseline', 'outputs', 'inputs',
                                   'recipe_energy', 'recipe_output', 'later_failed', 'duplicate'])
def test_rotated_event_does_not_accept_changed_committed_craft(change):
    from lupa.lua52 import LuaError
    lua, client, connectors, job, data = rotated_case()
    native = lua.globals().jev_fle_runtime.campaign.craft_jobs.job
    if change == 'fingerprint':data['attempt_outcomes'][0]['step_sha256'] = '0'*64
    elif change == 'baseline':native.baseline.science = 4
    elif change == 'outputs':native.outputs.science = 20
    elif change == 'inputs':native.inputs['iron-plate'] = 20
    elif change == 'recipe_energy':lua.eval('jev_fle_runtime.agent_characters[1].force').recipes.science.energy = 100
    elif change == 'recipe_output':lua.eval('jev_fle_runtime.agent_characters[1].force').recipes.science.products[1].amount = 2
    elif change == 'later_failed':data['attempt_outcomes'].append({**data['attempt_outcomes'][0],
        'receipt': 'later-job', 'started_tick': 201, 'outcome': 'failed'})
    elif change == 'duplicate':data['attempt_outcomes'] *= 2
    with pytest.raises((ValueError, RuntimeError, LuaError)):
        readback(client, checkpoint_binding=connectors,
                 completed_craft=checkpoint_completed_craft(data))


def test_latest_craft_witness_survives_many_routine_events_with_original_bounds():
    from jev_factorio.memory import CampaignMemory, retain_latest_craft
    _, _, _, job, data = rotated_case()
    memory = CampaignMemory('session', 'rocket_launch')
    memory.event('background_job_completed', job=job['id'], plan=data['attempt_outcomes'][0]['plan_id'],
                 outputs=job['outputs'], tick=200)
    memory.attempt_outcomes = deepcopy(data['attempt_outcomes'])
    for i in range(200):
        memory.event('routine_observation', tick=201+i)
        memory.attempt_outcomes = retain_latest_craft([
            *memory.attempt_outcomes, {'action': 'factory_insert', 'id': str(i)}])
    assert len(memory.history) == len(memory.attempt_outcomes) == 64
    binding = checkpoint_completed_craft({'history': memory.history,
                                         'attempt_outcomes': memory.attempt_outcomes})
    assert binding['id'] == job['id'] and binding['outputs'] == job['outputs']
    memory.event('background_job_completed', job='newer', tick=500)
    for i in range(65):memory.event('routine_observation', tick=501+i)
    assert [r['job'] for r in memory.history if r['kind']=='background_job_completed'] == ['newer']


def test_step_bound_completed_partial_plan_preserves_its_original_identity():
    _, client, connectors, _, data = rotated_case()
    data['attempt_outcomes'][0]['plan_id'] = 'factory:partial:science:target:20:batches:10'
    proof = checkpoint_completed_craft(data)
    assert readback(client, checkpoint_binding=connectors,
                    completed_craft=proof)['connector_snapshot_qualified']
