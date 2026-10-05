"""Offline throughput evidence cannot silently become production acceptance."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from jev_factorio.campaign_report import analyze, compare, distribution, main


def records():
    result = []
    for i in range(3):
        result.append({'session_id':'test', 'world_kind':'fle','process_id':'proc',
            'code_revision': {'commit':'a'*40,'source_sha256':'b'*64}, 'policy':'deterministic',
            'target':'rocket_launch','requested_model':'pinned','acceptance_configuration':{'factory_scheduling':'ready-work'},
            'campaign_treatment':{'schema':1,'lead_time_supply':True,
                'coverage_margin_lookahead':False,'profile_observations':False,
                'consolidated_observations':False},
            'decision':{'plan_id':None,'source':'deterministic','reason':'','state':{},
                'questions':{},'answers':{},'utilities':{},'model_called':False,'diagnostics':{}},
            'model_call':False,'resolved_model':None,'status':'running',
            'recorded_at_utc': (datetime(2026,9,25,tzinfo=timezone.utc)+timedelta(seconds=900*i)).isoformat(),
            'after_state': {'tick':54000*i,'researched':[], 'factory':{
                'produced':{'chemical-science-pack':i*20}, 'consumed':{'chemical-science-pack':i*15},
                'research':'study','research_progress':i*.1,
                'acceptance_runtime':{'speed':1,'tick_paused':False,'session_id':'test'}}},
            'phases':[{'stage':'observe','status':'returned','seconds':32},
                      {'stage':'dispatch','status':'returned','seconds':4},
                      {'stage':'approach','status':'returned','seconds':3},
                      {'stage':'transfer_rpc','status':'returned','seconds':1}]})
    return result


def write(path, rows):
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    return path


def test_report_separates_phase_timing_and_science_rates_without_authorizing_deployment(tmp_path):
    report = analyze(write(tmp_path/'log.jsonl', records()))
    assert report['measurement_eligible']
    assert report['science_consumed_per_actor_minute'] == 1
    assert report['science_consumed_per_decision'] == 15
    assert report['timing_seconds']['phase:observe']['p95'] == 32
    assert set(report['timing_seconds']) == {'phase:observe','phase:dispatch'}
    assert report['native_acceptance_proven'] is report['deployment_authorized'] is False


@pytest.mark.parametrize('key,value', [('session_id','other'), ('process_id','other'),
    ('code_revision',None), ('campaign_treatment',{}), ('policy','jev')])
def test_report_rejects_mixed_treatments_revisions_sessions_and_processes(tmp_path, key, value):
    rows = records(); rows[-1][key] = value
    with pytest.raises(ValueError, match='Mixed'):
        analyze(write(tmp_path/'log.jsonl', rows))


@pytest.mark.parametrize('case', ['tick','time','naive_time','incomplete','empty'])
def test_report_rejects_invalid_epoch_or_capture(tmp_path, case):
    rows = records()
    if case == 'tick': rows[-1]['after_state']['tick'] = 1
    if case == 'time': rows[-1]['recorded_at_utc'] = '2026-09-24T00:00:00+00:00'
    if case == 'naive_time': rows[-1]['recorded_at_utc'] = '2026-09-25T00:30:00'
    path = write(tmp_path/'log.jsonl', rows if case != 'empty' else [])
    if case == 'incomplete': path.write_text(path.read_text().rstrip())
    with pytest.raises(ValueError): analyze(path)


@pytest.mark.parametrize('case', ['counter_reset','missing','short','mock','speed','revision'])
def test_unknown_or_insufficient_evidence_cannot_qualify_measurement(tmp_path, case):
    rows = records()
    if case == 'counter_reset': rows[-1]['after_state']['factory']['consumed'] = {}
    if case == 'missing': rows[1]['after_state']['factory'].pop('consumed')
    if case == 'short': rows = rows[:2]
    if case == 'mock':
        for row in rows: row['world_kind']='mock'
    if case == 'speed': rows[0]['after_state']['factory']['acceptance_runtime']['speed']=2
    if case == 'revision':
        for row in rows: row['code_revision']=None
    result = analyze(write(tmp_path/'log.jsonl', rows))
    assert not result['measurement_eligible'] and result['issues']
    if case in {'counter_reset','missing'}: assert result['science_consumed_per_actor_minute'] is None


def test_matched_save_comparison_exposes_effects_but_is_not_world_attestation(tmp_path):
    baseline_rows = records()
    treatment_rows = records()
    for row in treatment_rows:
        row['campaign_treatment']['lead_time_supply'] = False
        row['after_state']['factory']['consumed']['chemical-science-pack'] *= 2
    left = analyze(write(tmp_path/'left.jsonl', baseline_rows))
    right = analyze(write(tmp_path/'right.jsonl', treatment_rows))
    first = tmp_path/'first.zip'; first.write_bytes(b'save')
    second = tmp_path/'second.zip'; second.write_bytes(b'save')
    result = compare(left, right, first, second)
    assert result['comparison_eligible']
    assert result['metrics']['science_consumed_per_actor_minute']['difference'] == 1
    assert not result['deployment_authorized'] and not result['native_acceptance_proven']
    second.write_bytes(b'different')
    assert 'initial_save_mismatch' in compare(left, right, first, second)['issues']


def test_cli_paired_arguments_are_all_required(tmp_path):
    path = write(tmp_path/'log.jsonl', records())
    with pytest.raises(SystemExit) as error:
        main([str(path), '--baseline',str(path)])
    assert error.value.code == 2


def test_cli_marks_identical_capture_pair_ineligible(tmp_path, capsys):
    path = write(tmp_path/'capture.jsonl', records())
    save = tmp_path/'initial.zip'
    save.write_bytes(b'same initial save')

    main([str(path), '--baseline',str(path), '--baseline-save',str(save),
          '--treatment-save',str(save)])

    report = json.loads(capsys.readouterr().out)
    assert not report['comparison_eligible']
    assert 'identical_campaign_treatment' in report['issues']
    assert 'same_gameplay_capture' in report['issues']
    assert report['native_acceptance_proven'] is False
    assert report['deployment_authorized'] is False


def test_module_cli_exports_identical_capture_as_ineligible(tmp_path):
    path = write(tmp_path/'capture.jsonl', records())
    save = tmp_path/'initial.zip'
    save.write_bytes(b'same initial save')
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env['PYTHONPATH'] = str(root/'src')

    result = subprocess.run([
        sys.executable, '-m', 'jev_factorio.campaign_report', str(path),
        '--baseline', str(path), '--baseline-save', str(save),
        '--treatment-save', str(save),
    ], cwd=root, env=env, text=True, capture_output=True, check=True)

    report = json.loads(result.stdout)
    assert not report['comparison_eligible']
    assert 'same_gameplay_capture' in report['issues']


def test_cli_reports_a_real_single_flag_treatment_comparison(tmp_path, capsys):
    baseline_rows = records()
    treatment_rows = records()
    for row in treatment_rows:
        row['campaign_treatment']['lead_time_supply'] = False
        row['after_state']['factory']['consumed']['chemical-science-pack'] *= 2
    baseline = write(tmp_path/'baseline.jsonl', baseline_rows)
    treatment = write(tmp_path/'treatment.jsonl', treatment_rows)
    save_a = tmp_path/'baseline.zip'; save_a.write_bytes(b'same initial save')
    save_b = tmp_path/'treatment.zip'; save_b.write_bytes(b'same initial save')

    main([str(treatment), '--baseline',str(baseline), '--baseline-save',str(save_a),
          '--treatment-save',str(save_b)])

    report = json.loads(capsys.readouterr().out)
    assert report['comparison_eligible']
    assert report['metrics']['science_consumed_per_actor_minute']['difference'] == 1
    assert report['native_acceptance_proven'] is False
    assert report['deployment_authorized'] is False


@pytest.mark.parametrize('process_id', [None, '', '   '])
def test_missing_process_identity_cannot_qualify_measurement(tmp_path, process_id):
    rows = records()
    for row in rows:
        row['process_id'] = process_id
    result = analyze(write(tmp_path/'log.jsonl', rows))
    assert not result['measurement_eligible']
    assert 'process_identity_required' in result['issues']


@pytest.mark.parametrize('field,value', [('commit', 'c'*40), ('source_sha256', 'd'*64)])
def test_paired_comparison_rejects_changed_source_even_with_matching_save(tmp_path, field, value):
    left = analyze(write(tmp_path/'left.jsonl', records()))
    rows = records()
    for row in rows:
        row['code_revision'][field] = value
    right = analyze(write(tmp_path/'right.jsonl', rows))
    initial = tmp_path/'initial.zip'
    initial.write_bytes(b'same initial save')
    result = compare(left, right, initial, initial)
    assert not result['comparison_eligible']
    assert 'uncontrolled_change:code_revision' in result['issues']


def test_percentile_definition_is_pinned():
    assert distribution([1,2,3,4]) == {'count':4,'p50':2,'p95':3,'sum':10}


def test_acceptance_projection_retains_new_treatment_marker():
    from jev_factorio.acceptance_capture import project_record
    from jev_factorio.research_log import Redactor
    from collections import Counter
    row = records()[0]; row['state'] = deepcopy(row['after_state'])
    projected = project_record(row, Redactor({}), Counter())
    assert projected['campaign_treatment'] == row['campaign_treatment']


@pytest.mark.parametrize('case', ['missing_flag','extra_flag','bool_schema','integer_flag'])
def test_report_rejects_malformed_campaign_treatment(tmp_path, case):
    rows = records()
    treatment = rows[0]['campaign_treatment']
    if case == 'missing_flag':
        treatment.pop('profile_observations')
    elif case == 'extra_flag':
        treatment['unreviewed_option'] = False
    elif case == 'bool_schema':
        treatment['schema'] = True
    elif case == 'integer_flag':
        treatment['lead_time_supply'] = 1
    for row in rows:
        row['campaign_treatment'] = deepcopy(treatment)
    report = analyze(write(tmp_path/'malformed-treatment.jsonl', rows))
    assert not report['measurement_eligible']
    assert 'campaign_treatment_invalid' in report['issues']


def test_comparison_rejects_identical_treatment_even_when_capture_files_differ(tmp_path):
    baseline_rows = records()
    treatment_rows = deepcopy(baseline_rows)
    treatment_rows[1]['reason'] = 'different ignored annotation'
    baseline = analyze(write(tmp_path/'baseline.jsonl', baseline_rows))
    treatment = analyze(write(tmp_path/'treatment.jsonl', treatment_rows))
    save = tmp_path/'initial.zip'
    save.write_bytes(b'same initial save')

    result = compare(baseline, treatment, save, save)

    assert not result['comparison_eligible']
    assert 'identical_campaign_treatment' in result['issues']
    assert 'same_gameplay_capture' not in result['issues']


def test_comparison_rejects_same_capture_even_if_report_treatment_is_relabelled(tmp_path):
    path = write(tmp_path/'same-capture.jsonl', records())
    baseline = analyze(path)
    treatment = deepcopy(baseline)
    treatment['identity']['campaign_treatment']['lead_time_supply'] = False
    save = tmp_path/'initial.zip'
    save.write_bytes(b'same initial save')

    result = compare(baseline, treatment, save, save)

    assert not result['comparison_eligible']
    assert 'same_gameplay_capture' in result['issues']


def test_comparison_requires_capture_hashes_for_both_arms(tmp_path):
    baseline = analyze(write(tmp_path/'baseline.jsonl', records()))
    treatment_rows = records()
    for row in treatment_rows:
        row['campaign_treatment']['lead_time_supply'] = False
    treatment = analyze(write(tmp_path/'treatment.jsonl', treatment_rows))
    treatment.pop('capture_sha256')
    save = tmp_path/'initial.zip'
    save.write_bytes(b'same initial save')

    result = compare(baseline, treatment, save, save)

    assert not result['comparison_eligible']
    assert 'capture_identity_invalid' in result['issues']


def test_comparison_rejects_a_multi_flag_treatment_change(tmp_path):
    baseline_rows = records()
    treatment_rows = records()
    for row in treatment_rows:
        row['campaign_treatment']['lead_time_supply'] = False
        row['campaign_treatment']['profile_observations'] = True
    baseline = analyze(write(tmp_path/'baseline.jsonl', baseline_rows))
    treatment = analyze(write(tmp_path/'treatment.jsonl', treatment_rows))
    save = tmp_path/'initial.zip'
    save.write_bytes(b'same initial save')

    result = compare(baseline, treatment, save, save)

    assert not result['comparison_eligible']
    assert 'campaign_treatment_requires_one_flag_change' in result['issues']


def test_comparison_does_not_trust_a_forged_eligible_flag_for_invalid_treatment(tmp_path):
    path = write(tmp_path/'capture.jsonl', records())
    valid = analyze(path)
    invalid = deepcopy(valid)
    invalid['identity']['campaign_treatment'] = {'schema':1,'lead_time_supply':True}
    invalid['measurement_eligible'] = True
    save = tmp_path/'initial.zip'
    save.write_bytes(b'same initial save')

    result = compare(valid, invalid, save, save)

    assert not result['comparison_eligible']
    assert 'invalid_campaign_treatment' in result['issues']


def test_producer_shaped_model_call_has_stable_resolved_identity(tmp_path):
    rows = records()
    for row in rows:
        row['decision']['model_called'] = True
        row['model_call'] = True
        row['resolved_model'] = 'provider/model-v1'

    report = analyze(write(tmp_path/'stable-model.jsonl', rows))

    assert report['measurement_eligible']
    assert report['resolved_models'] == ['provider/model-v1']


@pytest.mark.parametrize('case', ['switch','missing','null','blank','wrong_type','decision_mismatch'])
def test_producer_shaped_model_calls_require_one_valid_consistent_identity(tmp_path, case):
    rows = records()
    for row in rows:
        row['decision']['model_called'] = True
        row['model_call'] = True
        row['resolved_model'] = 'provider/model-v1'
    if case == 'switch':
        rows[1]['resolved_model'] = 'provider/model-v2'
    elif case == 'missing':
        rows[1].pop('resolved_model')
    elif case == 'null':
        rows[1]['resolved_model'] = None
    elif case == 'blank':
        rows[1]['resolved_model'] = '  '
    elif case == 'wrong_type':
        rows[1]['resolved_model'] = {'name':'provider/model-v1'}
    elif case == 'decision_mismatch':
        rows[1]['model_call'] = False
    report = analyze(write(tmp_path/f'{case}-model.jsonl', rows))
    assert not report['measurement_eligible']
    if case == 'switch':
        assert 'resolved_model_switch' in report['issues']
    else:
        assert 'model_identity_invalid' in report['issues']


def test_requested_model_alone_does_not_require_resolved_identity(tmp_path):
    rows = records()
    for row in rows:
        row['model_call'] = False
        row['decision']['model_called'] = False
        row['resolved_model'] = None

    report = analyze(write(tmp_path/'deterministic-no-call.jsonl', rows))

    assert report['measurement_eligible']
    assert report['resolved_models'] == []


def test_legacy_deterministic_capture_without_model_identity_fields_remains_supported(tmp_path):
    rows = records()
    for row in rows:
        row.pop('model_call')
        row.pop('decision')
        row.pop('resolved_model')

    report = analyze(write(tmp_path/'legacy-deterministic.jsonl', rows))

    assert report['measurement_eligible']
    assert report['resolved_models'] == []


@pytest.mark.parametrize('revision', [
    None,
    'not-a-revision',
    [],
    {'commit':None,'source_sha256':None},
    {'commit':'A'*40,'source_sha256':'b'*64},
    {'commit':'a'*40,'source_sha256':'b'*64,'extra':'unexpected'},
    {'commit':'a'*40,'source_sha256':'b'*63},
])
def test_report_requires_exact_valid_source_revision(tmp_path, revision):
    rows = records()
    for row in rows:
        row['code_revision'] = deepcopy(revision)

    report = analyze(write(tmp_path/'invalid-revision.jsonl', rows))

    assert not report['measurement_eligible']
    expected = 'source_revision_required' if revision is None else 'source_revision_invalid'
    assert expected in report['issues']


def test_report_rejects_missing_source_revision_and_accepts_exact_shape(tmp_path):
    missing = records()
    for row in missing:
        row.pop('code_revision')
    missing_report = analyze(write(tmp_path/'missing-revision.jsonl', missing))
    assert not missing_report['measurement_eligible']
    assert 'source_revision_required' in missing_report['issues']

    complete_report = analyze(write(tmp_path/'complete-revision.jsonl', records()))
    assert complete_report['measurement_eligible']


def test_capture_fingerprint_is_bound_to_the_bytes_parsed_if_path_is_replaced(tmp_path, monkeypatch):
    capture = tmp_path/'capture.jsonl'
    baseline_rows = records()
    baseline_bytes = write(capture, baseline_rows).read_bytes()
    treatment_rows = records()
    for row in treatment_rows:
        row['campaign_treatment']['lead_time_supply'] = False
        row['after_state']['factory']['consumed']['chemical-science-pack'] *= 2
    replacement = write(tmp_path/'replacement.jsonl', treatment_rows)
    treatment_bytes = replacement.read_bytes()

    original_open = Path.open
    replaced = False

    class ReplacePathOnClose:
        def __init__(self, path, mode, args, kwargs):
            self.path = path
            self.mode = mode
            self.args = args
            self.kwargs = kwargs
            self.stream = None

        def __enter__(self):
            self.stream = original_open(self.path, self.mode, *self.args, **self.kwargs)
            return self

        def __exit__(self, exc_type, exc, traceback):
            nonlocal replaced
            result = self.stream.__exit__(exc_type, exc, traceback)
            if self.path == capture and self.mode == 'rb' and not replaced:
                os.replace(replacement, capture)
                replaced = True
            return result

        def readline(self, *args):
            return self.stream.readline(*args)

        def read(self, *args):
            return self.stream.read(*args)

    def open_with_replacement(path, mode='r', *args, **kwargs):
        if Path(path) == capture and mode == 'rb':
            return ReplacePathOnClose(path, mode, args, kwargs)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', open_with_replacement)
    baseline = analyze(capture)
    assert replaced and capture.read_bytes() == treatment_bytes
    treatment = analyze(capture)

    assert baseline['measurement_eligible'] and treatment['measurement_eligible']
    assert baseline['science_consumed_per_actor_minute'] == 1
    assert treatment['science_consumed_per_actor_minute'] == 2
    assert baseline['capture_sha256'] == hashlib.sha256(baseline_bytes).hexdigest()
    assert treatment['capture_sha256'] == hashlib.sha256(treatment_bytes).hexdigest()
    assert baseline['capture_sha256'] != treatment['capture_sha256']

    save = tmp_path/'initial.zip'
    save.write_bytes(b'same operator-supplied initial save')
    comparison = compare(baseline, treatment, save, save)
    assert comparison['comparison_eligible']


def test_capture_fingerprint_covers_physical_wait_codec_bytes(tmp_path):
    from jev_factorio.wait_record_codec import Encoder, MARKER, encode_line

    encoder = Encoder('gameplay')
    physical_lines = []
    codec_rows = records()
    for row in codec_rows:
        row['unchanged_large_observation'] = {'facts': ['ore', 'belt', 'furnace'] * 400}
    for index, row in enumerate(codec_rows):
        prepared = encoder.prepare(row, wait=index > 0)
        assert prepared.is_delta is (index > 0)
        physical = encode_line(prepared)
        physical_lines.append(physical)
        encoder.commit(prepared, len(physical))
    capture_bytes = b''.join(physical_lines)
    assert any(MARKER in json.loads(line) for line in physical_lines[1:])

    path = tmp_path/'wait-codec.jsonl'
    path.write_bytes(capture_bytes)
    report = analyze(path)

    assert report['measurement_eligible']
    assert report['records'] == 3
    assert report['capture_sha256'] == hashlib.sha256(capture_bytes).hexdigest()
