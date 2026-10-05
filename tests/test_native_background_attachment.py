"""A completed retained craft may attach, but only reconciliation credits it."""
from copy import deepcopy
from dataclasses import asdict
import json
import pytest

from jev_factorio.backends.native_attachment import readback
from jev_factorio.backends.native_completed_craft import checkpoint_background_craft
from jev_factorio.background import BackgroundMemory
from jev_factorio.checkpoint_io import checkpoint_data
from jev_factorio.skills import Step
from jev_factorio.telemetry import make_attempt
from test_native_completed_craft import case
from test_craft_jobs import admitted


def background_case():
    lua, client, connectors, receipt, _ = case()
    job = admitted()
    job.session_id = receipt['session_id']
    job.actor['unit_number'] = receipt['unit_number']
    step = Step('factory_craft_job', 'craft_job_complete', 'science', 13,
                costs=job.inputs, timeout_ticks=job.deadline_tick-job.started_tick,
                parameters=job.parameters)
    plan = {'id': job.plan_id, 'steps': [asdict(step)]}
    attempt = make_attempt(job.session_id, job.goal, plan, 0, {'started_tick': 90},
                           process_id='a' * 32)
    memory = BackgroundMemory(job.session_id, job.goal, active_goal=job.goal,
                              status='running', last_tick=130, background_schema=3,
                              background_job=job.to_dict(), background_attempt=attempt,
                              background_step=asdict(step))
    data = checkpoint_data(memory)
    lua.execute("game.get_player(1).get_main_inventory=function() return {get_contents=function() return {{name='science',count=13}} end} end")
    return lua, client, connectors, receipt, data


def test_completed_background_receipt_attaches_read_only_with_output_and_owned_connectors():
    lua, client, connectors, _, data = background_case()
    before = deepcopy(data)
    native_before = lua.eval('helpers.table_to_json(jev_fle_runtime.campaign.craft_jobs.job)')
    proof = checkpoint_background_craft(data)
    assert readback(client, checkpoint_binding=connectors,
                    background_craft=proof)['connector_snapshot_qualified']
    assert data == before and data['background_job'] is not None
    assert data['attempt_outcomes'] == []
    assert lua.eval('helpers.table_to_json(jev_fle_runtime.campaign.craft_jobs.job)') == native_before


@pytest.mark.parametrize('change', ['missing_output', 'late', 'wrong_receipt', 'running',
                                   'wrong_actor', 'wrong_paid_input', 'queue', 'unbound_step',
                                   'regressed_tick', 'failed', 'missing_attempt'])
def test_background_attachment_rejects_unproven_work_without_credits(change):
    lua, client, connectors, _, data = background_case()
    native = lua.globals().jev_fle_runtime.campaign.craft_jobs.job
    if change == 'missing_output':
        lua.execute("game.get_player(1).get_main_inventory=function() return {get_contents=function() return {{name='science',count=12}} end} end")
    elif change == 'late':data['background_job']['deadline_tick'] = 199
    elif change == 'wrong_receipt':native.id = 'other'
    elif change == 'running':native.status = 'running'
    elif change == 'wrong_actor':native.unit_number = 999
    elif change == 'wrong_paid_input':native.inputs['iron-plate'] = 9
    elif change == 'queue':lua.execute('game.get_player(1).crafting_queue_size=1')
    elif change == 'unbound_step':data['background_step']['threshold'] += 1
    elif change == 'regressed_tick':data['last_tick'] = int(lua.eval('game.tick')) + 1
    elif change == 'failed':data['background_job']['failed'] = 'failed'
    else:data['background_attempt'] = None
    before = deepcopy(data)
    with pytest.raises(Exception):
        readback(client, checkpoint_binding=connectors,
                 background_craft=checkpoint_background_craft(data))
    assert data == before


def test_backend_forwards_background_binding_without_completing_it(monkeypatch):
    from jev_factorio.main import make_backend
    from jev_factorio.backends.fle import FleBackend
    _, _, connectors, _, data = background_case()
    proof = checkpoint_background_craft(data)
    calls=[]
    monkeypatch.setattr(FleBackend,'start',lambda self,**kwargs:calls.append(kwargs))
    make_backend('fle',resume=True,connector_binding=connectors,background_craft=proof)
    assert calls[0]['background_craft'] == proof
    assert 'completed_craft' not in calls[0]


def test_unbound_profile_cannot_ignore_background_authority():
    _, client, connectors, _, data = background_case()
    proof = checkpoint_background_craft(data)
    with pytest.raises(RuntimeError, match='checkpoint-bound'):
        readback(client, background_craft=proof)



def test_reconcile_cli_binds_background_receipt_before_backend(tmp_path, monkeypatch):
    from test_cli_resume_preflight import _invoke_cli
    _, _, connectors, _, data = background_case()
    data['connector_ownership'] = connectors
    path = tmp_path/'controller.json'
    path.write_bytes(json.dumps(data, sort_keys=True).encode())
    before = path.read_bytes()
    result, calls = _invoke_cli(monkeypatch, path, ['--background-work'],
                               policy='jev', limit_args=['--reconcile-only'])
    assert result == 'backend-boundary' and len(calls) == 1
    assert calls[0][1]['background_craft'] == checkpoint_background_craft(data)
    assert calls[0][1]['connector_binding'] == connectors
    assert 'completed_craft' not in calls[0][1]
    assert path.read_bytes() == before



def test_retained_native_v20_receipt_qualifies_without_mutating_the_checkpoint():
    from pathlib import Path
    from jev_factorio.backends.native_completed_craft import verify_background_craft
    raw = json.loads((Path(__file__).parent/'fixtures'/'native-v20-completed-background-readback.json').read_text())
    cp, native = raw['checkpoint'], raw['native']
    proof = {'job':cp['background_job'], 'attempt':cp['background_attempt'],
             'step':cp['background_step'], 'checkpoint_tick':cp['last_tick']}
    before = deepcopy(raw)
    inventory = {k:native['inventory'].get(k,0) for k in cp['background_job']['outputs']}
    verify_background_craft(native['receipt'],inventory,proof,native,native['tick'])
    assert raw == before and cp['background_job']['finished'] == 0
    assert native['receipt']['finished'] == 20
