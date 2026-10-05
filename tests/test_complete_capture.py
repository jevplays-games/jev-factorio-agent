import json
import hashlib
from collections import Counter
from copy import deepcopy

import pytest

from jev_factorio.complete_capture import (capture, project_record, verify,
                                           checked_checkpoint_progress, checked_economic_binding)
from jev_factorio.acceptance_io import canonical
from jev_factorio.coal_supply import intents
from jev_factorio import solid_routes as solid
from jev_factorio.integration_evidence import TRIAL_SCHEMA_V2, TRIAL_SCHEMA_V3, analyze_rows
from jev_factorio.treatment import SCHEMA, SCHEMA_V2, digest
from jev_factorio.research_log import Redactor
from integration_evidence_fixtures import evidence


def retain_capture_route(rows, initial, final):
    """Keep one intent-matched paid route in the capture fixture boundaries."""
    available = rows[0]['state']['factory']['solid_routes']['routes']
    route, observation = next((key, value) for key, value in available.items()
                              if value['item'] != 'coal')
    saved = solid.commitment(observation)
    for checkpoint in (initial, final):
        checkpoint['solid_commitments'] = {route: deepcopy(saved)}
    for record in rows:
        for label in ('state', 'after_state'):
            native = record[label]['factory']['solid_routes']
            native['routes'] = {route: deepcopy(native['routes'][route])}
            native['diagnostics'] = []
    return route, saved


def test_projection_retains_paid_coal_solid_and_model_evidence():
    rows, _, _, _ = evidence()
    row = rows[1]
    row['coal_supply'] = True
    row['coal_supply_evidence'] = {'sources': {'burner-a': {'parts': {'drill': {'receipt': 'paid-1'}}}}}
    row['coal_kit_evidence'] = {'funding': {'receipt': 'paid-2'}}
    row['state']['factory']['coal_supply'] = {'sources': {'burner-a': {'flow': {'mined': 4}}}}
    row['after_state']['factory']['coal_supply'] = {'sources': {'burner-a': {'flow': {'mined': 5}}}}
    projected = project_record(row, Redactor({}), Counter())
    assert projected['decision']['model_called'] is True
    assert projected['coal_supply_evidence']['sources']['burner-a']['parts']['drill']['receipt'] == 'paid-1'
    assert projected['after_state']['factory']['coal_supply']['sources']['burner-a']['flow']['mined'] == 5
    assert projected['after_state']['factory']['solid_routes'] == row['after_state']['factory']['solid_routes']
    row['unreviewed_ownership_evidence'] = {'receipt': 'hidden'}
    with pytest.raises(ValueError, match='Unknown treatment'):
        project_record(row, Redactor({}), Counter())
    del row['unreviewed_ownership_evidence']
    row['after_state']['owned_receipts'] = {'r': 'hidden'}
    with pytest.raises(ValueError, match='Unknown state ownership'):
        project_record(row, Redactor({}), Counter())
    del row['after_state']['owned_receipts']
    row['decision']['paid_receipt'] = 'hidden'
    with pytest.raises(ValueError, match='Unknown decision ownership'):
        project_record(row, Redactor({}), Counter())


def _complete_capture_projection_row():
    rows, _, _, _ = evidence()
    row = rows[1]
    for label in ("state", "after_state"):
        row[label]["factory"].update(solid_routes={"routes": {}, "diagnostics": []},
                                      coal_supply={"sources": {}})
    return row


def test_input_route_validation_diagnostic_roundtrips_only_supported_schema():
    row = _complete_capture_projection_row()
    row.update(furnace_input_belts=True, input_route_evidence={})
    for diagnostic in (
        {},
        {"stage": "route_schema", "exception_class": "ValueError"},
        {"stage": "commitment", "exception_class": "KeyError", "source": "recipe:iron-plate"},
    ):
        row["input_validation_failure"] = deepcopy(diagnostic)
        projected = project_record(row, Redactor({}), Counter())
        assert projected["input_validation_failure"] == diagnostic


@pytest.mark.parametrize("diagnostic", [
    None,
    [],
    {"stage": "unknown", "exception_class": "ValueError"},
    {"stage": "route_schema", "exception_class": "RuntimeError"},
    {"stage": "route_schema", "exception_class": "ValueError", "extra": "unreviewed"},
    {"stage": "production_sites", "exception_class": "ValueError", "source": "recipe:iron-plate"},
    {"stage": "commitment", "exception_class": "ValueError", "source": "unreviewed-source"},
])
def test_input_route_validation_diagnostic_rejects_unknown_or_malformed_fields(diagnostic):
    row = _complete_capture_projection_row()
    row.update(furnace_input_belts=True, input_route_evidence={},
               input_validation_failure=deepcopy(diagnostic))
    with pytest.raises(ValueError, match="Invalid input-route validation failure evidence"):
        project_record(row, Redactor({}), Counter())


def test_input_route_validation_diagnostic_rejects_sensitive_nested_fields():
    row = _complete_capture_projection_row()
    row.update(furnace_input_belts=True, input_route_evidence={},
               input_validation_failure={
                   "stage": "commitment", "exception_class": "ValueError",
                   "authorization": "synthetic-secret"})
    with pytest.raises(ValueError, match="Sensitive key in selected evidence"):
        project_record(row, Redactor({}), Counter())


def test_economic_capture_requires_bound_checkpoint_record_and_native_protocol():
    rows, trial, initial, final = evidence()
    trial['configuration'].update(coal_supply=True, coal_kit_policy=True,
                                  coal_economic_admission=True)
    for checkpoint in (initial, final):
        checkpoint.update(coal_supply_schema=2, coal_economic_admission=True)
    for row in rows:
        row['acceptance_configuration'] = dict(trial['configuration'])
        row['coal_economic_admission'] = True
        row['coal_admission_evidence'] = {'eligible': False, 'reason': 'unqualified'}
        for label in ('state', 'after_state'):
            row[label]['factory']['coal_supply'] = {'protocol': 2}
    checked_economic_binding(trial, initial, final, rows)
    projected = project_record(rows[0], Redactor({}), Counter())
    assert projected['coal_admission_evidence'] == rows[0]['coal_admission_evidence']
    rows[0]['state']['factory']['coal_supply']['protocol'] = 1
    with pytest.raises(ValueError, match='protocol 2'):
        checked_economic_binding(trial, initial, final, rows)
    rows[0]['state']['factory']['coal_supply']['protocol'] = 2
    final['coal_economic_admission'] = False
    with pytest.raises(ValueError, match='checkpoint'):
        checked_economic_binding(trial, initial, final, rows)


def test_cross_checkpoint_history_and_paid_coal_ownership_cannot_regress():
    paid = {'layout': 'paid-layout', 'target': {'unit_number': 42},
            'parts': {'drill': {'unit_number': 43, 'receipt': 'paid-drill'}}}
    initial = {'last_tick': 100, 'failures': {'coal-kit': 2},
               'coal_commitments': {'consumer': paid}}
    final = {'last_tick': 101, 'failures': {'coal-kit': 2},
             'coal_commitments': {'consumer': paid}}
    checked_checkpoint_progress(initial, final)
    with pytest.raises(ValueError, match='failure history'):
        checked_checkpoint_progress(initial, {**final, 'failures': {'coal-kit': 1}})
    with pytest.raises(ValueError, match='paid coal ownership'):
        checked_checkpoint_progress(initial, {**final, 'coal_commitments': {}})
    with pytest.raises(ValueError, match='paid coal ownership'):
        checked_checkpoint_progress(initial, {**final, 'coal_commitments': {
            'consumer': {**paid, 'parts': {'drill': {'unit_number': 43, 'receipt': 'different'}}}}})
    with pytest.raises(ValueError, match='tick regressed'):
        checked_checkpoint_progress(initial, {**final, 'last_tick': 99})


def test_complete_capture_roundtrip_retains_coal_and_rejects_tamper(tmp_path):
    rows, trial, initial, final = evidence()
    trial['schema'] = TRIAL_SCHEMA_V2
    trial['coal_targets'] = [trial['solid_intents'][0]['target'], trial['solid_intents'][1]['target']]
    trial['solid_intents'][:2] = intents(trial['coal_targets'])
    trial['configuration'].update(coal_supply=True, coal_kit_policy=True)
    trial['treatment_sha256'] = digest({'schema': SCHEMA, 'solid_intents': trial['solid_intents'],
        'coal_targets': trial['coal_targets'], 'solid_science_policy': False, 'coal_kit_policy': True})
    save = tmp_path / 'save.zip'
    save.write_bytes(b'fixture-save')
    trial['initial_save_sha256'] = hashlib.sha256(save.read_bytes()).hexdigest()
    for checkpoint in (initial, final):
        checkpoint['solid_intents'] = trial['solid_intents']
        checkpoint['coal_targets'] = trial['coal_targets']
        checkpoint['coal_kit_policy'] = True
        checkpoint['coal_supply_schema'] = 1
        checkpoint['coal_epoch'] = dict(checkpoint['solid_epoch'])
        checkpoint['coal_commitments'] = {}
        checkpoint['coal_funding'] = None
    trial['vm_uuid'] = 'isolated-vm'
    trial['production_vm_uuid'] = 'production-vm'
    for row in rows:
        row['acceptance_configuration'].update(coal_supply=True, coal_kit_policy=True)
        row.update(solid_funding_schema=1, solid_funding=None)
        row['coal_supply'] = True
        row['coal_supply_evidence'] = {'sources': {'burner-a': {'flow': {'mined': 1}}}}
        row['coal_supply_fault'] = False
        row['coal_kit_policy'] = True
        row['coal_kit_evidence'] = {'funding': {'receipt': 'paid-kit'}}
        for label in ('state', 'after_state'):
            state = row[label]
            state['factory']['coal_supply'] = {
                'protocol': 1, 'session_id': state['session_id'], 'tick': state['tick'],
                'actor_index': 1, 'surface_index': 1, 'force_index': 1,
                'targets': trial['coal_targets'], 'committed': False,
                'sources': {}, 'reason': 'no_supported_bundle'}
    retain_capture_route(rows, initial, final)
    trial['initial_checkpoint_sha256'] = hashlib.sha256(canonical(initial)).hexdigest()
    paths = {}
    for name, value in (('trial', trial), ('initial', initial), ('final', final)):
        paths[name] = tmp_path / (name + '.json')
        paths[name].write_bytes(canonical(value))
    gameplay = tmp_path / 'gameplay.jsonl'
    gameplay.write_bytes(b''.join(canonical(row) for row in rows))
    preflight = tmp_path / 'preflight.json'
    preflight.write_bytes(canonical({'schema': 'jev-factorio.dev-preflight.v1',
        'checkpoint_sha256': trial['initial_checkpoint_sha256'],
        'vm_uuid': trial['vm_uuid'], 'production_vm_uuid': trial['production_vm_uuid'],
        'ready_for_coordinated_validation': False,
        'issues': ['solid_preflight_not_supported', 'coal_preflight_not_supported']}))
    output = tmp_path / 'capture'
    broken = json.loads(gameplay.read_bytes().splitlines()[0])
    broken['state']['factory']['coal_supply'] = {}
    gameplay.write_bytes(canonical(broken) + b''.join(canonical(row) for row in rows[1:]))
    with pytest.raises(ValueError, match='coal supply observation'):
        capture(gameplay=gameplay, trial_path=paths['trial'],
                initial_checkpoint=paths['initial'], final_checkpoint=paths['final'],
                save=save, preflight_path=preflight, output=output)
    gameplay.write_bytes(b''.join(canonical(row) for row in rows))
    broken_final = dict(final, pending={'dispatch': 'prepared'})
    paths['final'].write_bytes(canonical(broken_final))
    with pytest.raises(ValueError):
        capture(gameplay=gameplay, trial_path=paths['trial'],
                initial_checkpoint=paths['initial'], final_checkpoint=paths['final'],
                save=save, preflight_path=preflight, output=output)
    paths['final'].write_bytes(canonical(final))
    manifest = capture(gameplay=gameplay, trial_path=paths['trial'],
                       initial_checkpoint=paths['initial'], final_checkpoint=paths['final'],
                       save=save, preflight_path=preflight, output=output)
    assert manifest['native_acceptance'] == 'not_accepted'
    checked = verify(output)
    assert checked['rows'][0]['coal_kit_evidence']['funding']['receipt'] == 'paid-kit'
    embedded = output / 'final-checkpoint.json'
    embedded.write_bytes(canonical(broken_final))
    files = sorted(p for p in output.iterdir() if p.name != 'SHA256SUMS')
    (output / 'SHA256SUMS').write_text(''.join(
        hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in files))
    with pytest.raises(ValueError):
        verify(output)
    embedded.write_bytes(canonical(final))
    (output / 'SHA256SUMS').write_text(''.join(
        hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in files))
    (output / 'gameplay.jsonl.gz').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='checksum'):
        verify(output)
    # V3 is the same complete capture with an additional, explicitly
    # diagnostic-only predeclared intermediate chain. No route is promoted.
    trial['schema'] = TRIAL_SCHEMA_V3
    trial['downstream_recipes'].append('iron-plate')
    trial['downstream_chain'] = [{'route': 'solid:1:2:iron-ore:input',
        'producer_role': 'recipe:iron-plate', 'producer_recipe': 'iron-plate',
        'product_item': 'iron-plate', 'consumer_role': 'recipe:automation-science-pack',
        'consumer_unit': 123, 'science_pack': 'automation-science-pack'}]
    paths['trial'].write_bytes(canonical(trial))
    later = tmp_path / 'capture-v3'
    capture(gameplay=gameplay, trial_path=paths['trial'],
            initial_checkpoint=paths['initial'], final_checkpoint=paths['final'],
            save=save, preflight_path=preflight, output=later)
    checked_v3 = verify(later)
    assert checked_v3['manifest']['native_acceptance'] == 'not_accepted'


def test_complete_capture_v2_admission_roundtrip(tmp_path):
    rows, trial, initial, final = evidence()
    # Build the same bounded complete treatment without borrowing an accepted
    # native result: all native admission witnesses remain explicitly negative.
    trial['schema'] = TRIAL_SCHEMA_V2
    trial['coal_targets'] = [trial['solid_intents'][0]['target'], trial['solid_intents'][1]['target']]
    trial['solid_intents'][:2] = intents(trial['coal_targets'])
    trial['configuration'].update(coal_supply=True, coal_kit_policy=True,
                                  coal_economic_admission=True)
    trial['treatment_sha256'] = digest({'schema': SCHEMA_V2,
        'solid_intents': trial['solid_intents'], 'coal_targets': trial['coal_targets'],
        'solid_science_policy': False, 'coal_kit_policy': True,
        'coal_economic_admission': True})
    save = tmp_path / 'save.zip'
    save.write_bytes(b'fixture-save')
    trial['initial_save_sha256'] = hashlib.sha256(save.read_bytes()).hexdigest()
    for checkpoint in (initial, final):
        checkpoint.update(solid_intents=trial['solid_intents'],
                          coal_targets=trial['coal_targets'], coal_kit_policy=True,
                          coal_supply_schema=2, coal_economic_admission=True,
                          coal_epoch=dict(checkpoint['solid_epoch']),
                          coal_commitments={}, coal_funding=None)
    trial['vm_uuid'], trial['production_vm_uuid'] = 'isolated-vm', 'production-vm'
    for row in rows:
        row['acceptance_configuration'].update(coal_supply=True, coal_kit_policy=True,
                                               coal_economic_admission=True)
        row.update(solid_funding_schema=1, solid_funding=None,
                   coal_supply=True, coal_supply_evidence={}, coal_supply_fault=False,
                   coal_kit_policy=True, coal_kit_evidence={},
                   coal_economic_admission=True,
                   coal_admission_evidence={'eligible': False,
                                            'reason': 'electric_conversion_and_construction_cost_unknown'})
        for label in ('state', 'after_state'):
            state = row[label]
            state['factory']['coal_supply'] = {
                'protocol': 2, 'session_id': state['session_id'], 'tick': state['tick'],
                'actor_index': 1, 'surface_index': 1, 'force_index': 1,
                'targets': trial['coal_targets'], 'committed': False,
                'sources': {}, 'reason': 'no_supported_bundle',
                'admission': {'protocol': 1, 'session_id': state['session_id'],
                              'tick': state['tick'], 'actor_index': 1,
                              'surface_index': 1, 'force_index': 1,
                              'qualified': False,
                              'reason': 'electric_conversion_and_construction_cost_unknown'}}
    retain_capture_route(rows, initial, final)
    trial['initial_checkpoint_sha256'] = hashlib.sha256(canonical(initial)).hexdigest()
    paths = {}
    for name, value in (('trial', trial), ('initial', initial), ('final', final)):
        paths[name] = tmp_path / (name + '.json')
        paths[name].write_bytes(canonical(value))
    gameplay = tmp_path / 'gameplay.jsonl'
    gameplay.write_bytes(b''.join(canonical(row) for row in rows))
    preflight = tmp_path / 'preflight.json'
    preflight.write_bytes(canonical({'schema': 'jev-factorio.dev-preflight.v1',
        'checkpoint_sha256': trial['initial_checkpoint_sha256'],
        'vm_uuid': trial['vm_uuid'], 'production_vm_uuid': trial['production_vm_uuid'],
        'ready_for_coordinated_validation': False,
        'issues': ['solid_preflight_not_supported', 'coal_preflight_not_supported']}))
    output = tmp_path / 'capture'
    capture(gameplay=gameplay, trial_path=paths['trial'],
            initial_checkpoint=paths['initial'], final_checkpoint=paths['final'],
            save=save, preflight_path=preflight, output=output)
    assert verify(output)['rows'][0]['coal_admission_evidence']['eligible'] is False
    assert 'coal_economic_native_evidence_invalid' not in analyze_rows(rows, trial, initial, final)['issues']
    rows[0]['state']['factory']['coal_supply']['protocol'] = 1
    assert 'coal_economic_native_evidence_invalid' in analyze_rows(rows, trial, initial, final)['issues']
