"""The composed solid controller's active and null funding records survive capture."""
import hashlib
import json
from copy import deepcopy

from jev_factorio.acceptance_io import canonical
from jev_factorio.complete_capture import capture, verify
from test_complete_capture import test_complete_capture_v2_admission_roundtrip as _fixture
from test_solid_investment import make_loop
from test_solid_kit_acquisition import kit_loop


def _bind_state(emitted, template, session):
    state = deepcopy(emitted)
    state['session_id'] = session
    state['factory']['tick'] = state['tick']
    state['factory']['acceptance_runtime'] = deepcopy(
        template['factory']['acceptance_runtime'])
    state['factory']['acceptance_runtime']['session_id'] = session
    state['factory']['coal_supply'] = deepcopy(template['factory']['coal_supply'])
    coal = state['factory']['coal_supply']
    coal['session_id'] = session
    coal['tick'] = state['tick']
    if isinstance(coal.get('admission'), dict):
        coal['admission']['session_id'] = session
        coal['admission']['tick'] = state['tick']
    state['factory']['solid_routes']['session_id'] = session
    state['factory']['solid_routes']['tick'] = state['tick']
    return state


def test_actual_composed_controller_funding_records_roundtrip_end_to_end(tmp_path):
    _fixture(tmp_path)
    trial_path = tmp_path / 'trial.json'
    initial_path = tmp_path / 'initial.json'
    final_path = tmp_path / 'final.json'
    preflight_path = tmp_path / 'preflight.json'
    gameplay_path = tmp_path / 'gameplay.jsonl'
    trial = json.loads(trial_path.read_bytes())
    initial = json.loads(initial_path.read_bytes())
    final = json.loads(final_path.read_bytes())
    rows = [json.loads(line) for line in gameplay_path.read_bytes().splitlines()]

    # This roundtrip isolates the controller's proposed funding boundary. The
    # fixtures' separately paid routes are removed from both checkpoint and
    # every observation, so this test does not imply route payment or acceptance.
    initial['solid_commitments'] = {}
    final['solid_commitments'] = {}
    for row in rows:
        row.update(solid_funding_schema=1, solid_funding=None)
        for label in ('state', 'after_state'):
            solid = row[label]['factory']['solid_routes']
            solid['routes'] = {}
            solid['diagnostics'] = []

    active_dir = tmp_path / 'active-controller'
    active_dir.mkdir()
    active_loop, _ = kit_loop(active_dir)
    active_record = active_loop.step()
    assert active_record['solid_funding_schema'] == 1
    assert active_record['solid_funding'] is not None

    null_dir = tmp_path / 'null-controller'
    null_dir.mkdir()
    null_loop, _ = make_loop(null_dir, enabled=False)
    null_record = null_loop.step()
    assert null_record['solid_funding_schema'] == 1
    assert null_record['solid_funding'] is None
    rows[0]['solid_funding_schema'] = null_record['solid_funding_schema']
    rows[0]['solid_funding'] = null_record['solid_funding']

    session = rows[0]['session_id']
    active_record = deepcopy(active_record)
    active_record['session_id'] = session
    active_record['acceptance_configuration'] = deepcopy(rows[9]['acceptance_configuration'])
    # The mock controller emits its actual composed solid row; add the other
    # trial's native envelopes to exercise the full capture contract.
    for key in ('coal_economic_admission', 'coal_admission_evidence', 'coal_supply',
                'coal_supply_evidence', 'coal_supply_fault', 'coal_kit_policy',
                'coal_kit_evidence'):
        if key in rows[9]:
            active_record[key] = deepcopy(rows[9][key])
    for label in ('state', 'after_state'):
        active_record[label] = _bind_state(active_record[label], rows[9][label], session)

    # Place the real controller event between fixture ticks 29,800 and 33,400.
    # Its after-observation becomes the next record's before-observation.
    rows[9]['state'] = deepcopy(active_record['after_state'])
    rows.insert(9, active_record)

    trial['initial_checkpoint_sha256'] = hashlib.sha256(canonical(initial)).hexdigest()
    preflight = json.loads(preflight_path.read_bytes())
    preflight['checkpoint_sha256'] = trial['initial_checkpoint_sha256']
    trial_path.write_bytes(canonical(trial))
    initial_path.write_bytes(canonical(initial))
    final_path.write_bytes(canonical(final))
    preflight_path.write_bytes(canonical(preflight))
    gameplay_path.write_bytes(b''.join(canonical(row) for row in rows))

    output = tmp_path / 'capture-active-funding'
    manifest = capture(gameplay=gameplay_path, trial_path=trial_path,
        initial_checkpoint=initial_path, final_checkpoint=final_path,
        save=tmp_path / 'save.zip', preflight_path=preflight_path, output=output)
    reviewed = verify(output)
    assert manifest['native_acceptance'] == 'not_accepted'
    assert reviewed['manifest']['native_acceptance'] == 'not_accepted'
    assert reviewed['rows'][0]['solid_funding'] is None
    actual = reviewed['rows'][9]
    assert actual['solid_funding_schema'] == active_record['solid_funding_schema'] == 1
    assert actual['solid_funding'] == active_record['solid_funding']
    assert reviewed['rows'][10]['solid_funding'] is None
