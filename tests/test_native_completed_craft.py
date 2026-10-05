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
