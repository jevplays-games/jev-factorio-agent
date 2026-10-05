"""Retained history preimages supplement signed authority, never replace it."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zlib

import pytest
from jev_factorio import compatible_epoch as epoch
from jev_factorio import compatible_recovery as recovery
from test_compatible_epoch import setup


def encode(raw): return base64.b64encode(raw).decode()
def digest(raw): return hashlib.sha256(raw).hexdigest()


def evidence(record, prepared, result):
    raw, p, r = map(epoch._canonical, (record, prepared, result))
    return {'record_sha256':digest(raw), 'record_zlib_base64':encode(zlib.compress(raw)),
            'prepared_base64':encode(p), 'result_base64':encode(r)}, digest(p), digest(r)


def retained_case(tmp_path, monkeypatch):
    memory, old, record = setup(tmp_path, monkeypatch)
    body = record['epoch_witness']['body'];body['schema']='jev.compatible-epoch-boundary.v2'
    for n, edge in enumerate(body['edges']):
        p={'provenance':{'run_id':record['owner_invocation']['run_id'],
                        'execution_id':str(n),'code_revision':edge['current_source']},
           'argv':['python','--reevaluate-blocked-once','--blocked-source-revision',
                   edge['previous_source']['commit'],'--exact-checkpoint-sha256',edge['row']['checkpoint_sha256']],
           'script_sha256':digest(str(n).encode())}
        r={'script_sha256':p['script_sha256'],'prepared_sha256':digest(epoch._canonical(p)),
           'checkpoint_sha256':digest(('final'+str(n)).encode()),'exit_code':0}
        journal={**p['provenance'],'schema_version':2,'controller':'hierarchical','policy':'jev',
                 'session_id':body['session_id'],'target':body['target'],'tick':edge['row']['tick'],
                 'history':[edge['history']]}
        proof, ph, rh=evidence(journal,p,r)
        edge.update(history_evidence=proof,prepared_sha256=ph,result_sha256=rh)
    memory.history.clear()
    return memory, old, record


def validate(memory, old, record):
    return epoch.validate_epoch_witness(record['epoch_witness'],memory,record,
        old['current_source'],old['decision_contract_sha256'],require_live_history=True)


def test_v2_retained_journal_and_launch_result_chain_cover_rolled_history(tmp_path, monkeypatch):
    memory, old, record=retained_case(tmp_path,monkeypatch)
    before=deepcopy(memory)
    validate(memory,old,record)
    assert memory == before and memory.history == []
    memory.compatible_source_recoveries.append(record)
    assert recovery.validate_lineage(memory)[-1] == record
    assert recovery.approved_sources(memory,record['current_source']) == [
        record['previous_source'],record['current_source']]


@pytest.mark.parametrize('change',['record_hash','prepared_bytes','result_bytes','missing_evidence',
                                  'missing_row','conflicting_live_event','unsigned','compression_tail','oversize'])
def test_missing_spliced_or_unsigned_retained_history_rejects(tmp_path,monkeypatch,change):
    memory,old,record=retained_case(tmp_path,monkeypatch)
    edge=record['epoch_witness']['body']['edges'][0];proof=edge['history_evidence']
    if change=='record_hash':proof['record_sha256']='f'*64
    elif change=='prepared_bytes':proof['prepared_base64']=encode(b'{}')
    elif change=='result_bytes':proof['result_base64']=encode(b'{}')
    elif change=='missing_evidence':del edge['history_evidence']
    elif change=='missing_row':memory.blocked_reevaluations.pop(0)
    elif change=='conflicting_live_event':memory.history=[{**edge['history'],'tick':-1}]
    elif change=='unsigned':monkeypatch.setattr(epoch,'_verify_signature',lambda *a:(_ for _ in ()).throw(ValueError('unsigned')))
    elif change=='compression_tail':proof['record_zlib_base64']=encode(base64.b64decode(proof['record_zlib_base64'])+b'extra')
    else:proof['record_zlib_base64']=encode(zlib.compress(b' '*(512*1024+1)))
    with pytest.raises(ValueError):validate(memory,old,record)


@pytest.mark.parametrize('key,value',[('session_id','other'),('target','other'),('run_id','other'),
                                    ('execution_id','other'),('code_revision',{}),('tick',-1),
                                    ('history',[]),('policy','deterministic'),('schema_version',True)])
def test_even_rehashed_journal_must_match_signed_edge_and_launcher(tmp_path,monkeypatch,key,value):
    memory,old,record=retained_case(tmp_path,monkeypatch)
    proof=record['epoch_witness']['body']['edges'][0]['history_evidence']
    journal=json.loads(zlib.decompress(base64.b64decode(proof['record_zlib_base64'])))
    journal[key]=value;raw=epoch._canonical(journal)
    proof['record_sha256']=digest(raw);proof['record_zlib_base64']=encode(zlib.compress(raw))
    with pytest.raises(ValueError):validate(memory,old,record)


def test_native_v20_full_journal_record_matches_original_launch_and_result():
    fixture=json.loads((Path(__file__).parent/'fixtures'/'native-v20-epoch-history-evidence.json').read_text())
    before=deepcopy(fixture)
    assert epoch._retained_history_evidence(fixture['evidence'],fixture['edge'],
                                          fixture['body'],fixture['owner']) == 142143
    assert fixture == before


def test_v1_still_requires_current_history_for_new_authorization(tmp_path,monkeypatch):
    memory,old,record=setup(tmp_path,monkeypatch);memory.history.clear()
    with pytest.raises(ValueError):validate(memory,old,record)


def test_live_authorization_accepts_retained_history_without_rewriting_checkpoint(tmp_path, monkeypatch):
    from dataclasses import asdict
    from jev_factorio import blocked_reevaluation
    memory, old, record = retained_case(tmp_path, monkeypatch)
    memory.status, memory.reason, memory.stalled_decisions = 'blocked', 'model abstention', 4
    memory.blocked_recovery = {'schema': 1, 'session_id': memory.session_id,
        'source_revision': deepcopy(record['previous_source']), 'attempts': [],
        'last_input_sha256': None, 'wait_level': 0}
    raw = epoch._canonical(asdict(memory))
    authority = {key: deepcopy(record[key]) for key in recovery._KEYS}
    authority.update(checkpoint_sha256=digest(raw), scope=recovery.scope(memory))
    witness = deepcopy(record['epoch_witness'])
    witness['body'].update(terminal_checkpoint_sha256=authority['checkpoint_sha256'],
                           authorization_sha256=recovery.digest_json(authority))
    monkeypatch.setattr(blocked_reevaluation, 'validate_source_revision', lambda *a, **k: {
        'source_head': authority['current_source']['commit'],
        'decision_contract_sha256': authority['decision_contract_sha256']})
    monkeypatch.setattr(recovery, 'validate_budget_contract', lambda *a: None)
    before = deepcopy(asdict(memory))
    result = recovery.validate_authorization(authority, raw, memory,
        authority['current_source'], authority['owner_invocation'], epoch_witness=witness)
    assert result['epoch_witness'] == witness and asdict(memory) == before
    assert memory.history == []
    with pytest.raises(ValueError):
        recovery.validate_authorization(authority, raw + b' ', memory,
            authority['current_source'], authority['owner_invocation'], epoch_witness=witness)


@pytest.mark.parametrize('change', ['run', 'source', 'old_source', 'checkpoint',
                                  'duplicate_flag', 'result_prepared', 'result_script', 'exit_type'])
def test_rehashed_launcher_receipts_still_require_exact_crosslinks(tmp_path, monkeypatch, change):
    memory, old, record = retained_case(tmp_path, monkeypatch)
    edge = record['epoch_witness']['body']['edges'][0]
    proof = edge['history_evidence']
    journal = json.loads(zlib.decompress(base64.b64decode(proof['record_zlib_base64'])))
    prepared = json.loads(base64.b64decode(proof['prepared_base64']))
    result = json.loads(base64.b64decode(proof['result_base64']))
    if change == 'run': prepared['provenance']['run_id'] = 'other'
    elif change == 'source': prepared['provenance']['code_revision'] = {}
    elif change == 'old_source': prepared['argv'][prepared['argv'].index('--blocked-source-revision')+1] = 'f'*40
    elif change == 'checkpoint': prepared['argv'][-1] = 'f'*64
    elif change == 'duplicate_flag': prepared['argv'].append('--reevaluate-blocked-once')
    elif change == 'result_script': result['script_sha256'] = 'f'*64
    elif change == 'exit_type': result['exit_code'] = True
    result['prepared_sha256'] = digest(epoch._canonical(prepared))
    if change == 'result_prepared': result['prepared_sha256'] = 'f'*64
    proof, ph, rh = evidence(journal, prepared, result)
    edge.update(history_evidence=proof, prepared_sha256=ph, result_sha256=rh)
    with pytest.raises(ValueError): validate(memory, old, record)
