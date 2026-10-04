"""The composed solid controller's active and null funding records survive capture."""
import hashlib
import json
from copy import deepcopy

import pytest

from jev_factorio.acceptance_io import canonical
from jev_factorio import solid_routes as solid_contract
from jev_factorio.coal_controller import coal_loop_type
from jev_factorio.complete_capture import capture, verify
from jev_factorio.integration_evidence import TRIAL_SCHEMA_V2
from jev_factorio.treatment import SCHEMA, digest
from solid_routes_fixtures import ROUTE
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


def test_actual_composed_partial_funding_resume_paid_route_capture_and_verify(tmp_path):
    """Capture real controller recovery and route receipts in one composed state."""
    _fixture(tmp_path)
    trial_path = tmp_path / 'trial.json'
    initial_path = tmp_path / 'initial.json'
    final_path = tmp_path / 'final.json'
    preflight_path = tmp_path / 'preflight.json'
    gameplay_path = tmp_path / 'gameplay.jsonl'
    trial = json.loads(trial_path.read_bytes())
    trial['schema'] = TRIAL_SCHEMA_V2
    trial['configuration'].pop('coal_economic_admission', None)
    trial['configuration']['solid_science_policy'] = True
    trial['configuration']['coal_kit_policy'] = False
    trial['treatment_sha256'] = digest({
        'schema': SCHEMA,
        'solid_intents': trial['solid_intents'],
        'coal_targets': trial['coal_targets'],
        'solid_science_policy': True,
        'coal_kit_policy': False,
    })

    base, backend = kit_loop(tmp_path)
    checkpoint_dir = tmp_path / 'composed-controller'
    checkpoint_dir.mkdir()
    backend.checkpoint = checkpoint_dir / 'checkpoint.json'
    # The actual composed loop below installs its validated intent set. The
    # fixture backend reads one shared state object, including both protocol
    # observations; the capture rows are never borrowed from the parser fixture.
    backend._factory = None
    backend.coal_supply_supported = True
    runtime = {
        'schema': 1, 'session_id': backend.state.session_id, 'actor_unit': 999998,
        'player_index': 1, 'surface_index': 1, 'force_index': 1,
        'mods': {'base': '2.0-fixture'}, 'speed': 1, 'tick_paused': False,
    }

    def bind_same_composed_observation():
        factory = backend.state.factory
        solid_native = factory['solid_routes']
        factory['acceptance_runtime'] = deepcopy(runtime)
        factory['coal_supply'] = {
            'protocol': 1, 'session_id': backend.state.session_id,
            'tick': backend.state.tick,
            'actor_index': solid_native['actor_index'],
            'surface_index': solid_native['surface_index'],
            'force_index': solid_native['force_index'],
            'targets': deepcopy(trial['coal_targets']), 'committed': False,
            'sources': {}, 'reason': 'no_supported_bundle',
        }

    backend.before_observe = bind_same_composed_observation
    bind_same_composed_observation()
    composed_type = coal_loop_type(type(base))

    def make_composed_loop(*, resume):
        return composed_type(
            backend, target='rocket_launch', policy='deterministic',
            factory_scheduling='ready-work', tick_seconds=0,
            checkpoint=str(backend.checkpoint), resume_controller=resume,
            solid_intents=deepcopy(trial['solid_intents']),
            solid_science_policy=True, coal_targets=deepcopy(trial['coal_targets']),
            coal_kit_policy=False, coal_economic_admission=False,
        )

    loop = make_composed_loop(resume=False)
    loop._observe()
    # `kit_loop` seeds a real observed service history on its controller
    # memory. The coal composition must start with that same decision context;
    # a fresh empty CampaignMemory makes the fixture's base planner report no
    # executable frontier before the solid investment proposal can be added.
    for field in ('active_goal', 'completed_goals', 'attempt_outcomes', 'failures',
                  'last_tick', 'status'):
        setattr(loop.memory, field, deepcopy(getattr(base.memory, field)))
    loop._save()
    initial_raw = backend.checkpoint.read_bytes()
    initial = json.loads(initial_raw)
    assert initial['solid_commitments'] == {}
    assert initial['solid_intents'] == trial['solid_intents']
    assert initial['coal_targets'] == trial['coal_targets']
    assert initial['coal_epoch'] == initial['solid_epoch']

    # The first real paid kit action changes inventory but loses its response.
    # The next process resumes from the durable partial funding checkpoint and
    # reconciles that exact receipt without issuing the action twice.
    backend.lose_kit_ack = True
    interrupted = loop.step()
    assert interrupted['action'] in {'factory_extract', 'factory_craft'}
    assert interrupted['solid_funding'] is not None
    assert backend.calls
    records = [deepcopy(interrupted)]
    interrupted_raw = backend.checkpoint.read_bytes()
    checkpointed = loop.memory_type.from_bytes(
        interrupted_raw, backend.state.session_id, 'rocket_launch')
    assert checkpointed.solid_funding == interrupted['solid_funding']
    assert checkpointed.pending is not None
    recovery_attempt_id = checkpointed.attempt['id']
    calls_before_resume = len(backend.calls)

    backend.lose_kit_ack = False
    resumed = make_composed_loop(resume=True)
    recovered = resumed.step()
    records.append(deepcopy(recovered))
    assert len(backend.calls) == calls_before_resume
    assert resumed.memory.pending is None
    assert resumed.memory.solid_funding is not None

    for _ in range(24):
        route = backend.state.factory['solid_routes']['routes'][ROUTE]
        if (route['state'] == 'ready' and resumed.memory.solid_funding is None
                and resumed.memory.pending is None):
            break
        records.append(deepcopy(resumed.step()))
    route = backend.state.factory['solid_routes']['routes'][ROUTE]
    assert route['state'] == 'ready'
    assert len(route['parts']) == len(route['steps']) == 4
    assert resumed.memory.solid_funding is None and resumed.memory.pending is None
    assert len([call for call in backend.calls if call[0] == solid_contract.COMMAND]) == 4
    assert all(part.get('receipt') for part in route['parts'].values())
    final_raw = backend.checkpoint.read_bytes()
    final = json.loads(final_raw)
    assert final['solid_commitments'][ROUTE]['parts'] == route['parts']
    assert final['coal_commitments'] == {}
    assert final['solid_intents'] == trial['solid_intents']
    final_outcome_ids = {outcome['id'] for outcome in final['attempt_outcomes']}
    assert {outcome['id'] for outcome in initial['attempt_outcomes']} <= final_outcome_ids
    assert recovery_attempt_id in final_outcome_ids

    observations = [record[label] for record in records
                    for label in ('state', 'after_state')]
    observed_routes = [state['factory']['solid_routes']['routes'][ROUTE]
                       for state in observations]
    partial = next((index for index, value in enumerate(observed_routes)
                    if 0 < len(value['parts']) < len(value['steps'])), None)
    assert partial is not None
    assert any(
        set(observed_routes[partial]['parts']) <= set(later['parts'])
        and all(later['parts'][key] == value
                for key, value in observed_routes[partial]['parts'].items())
        for later in observed_routes[partial + 1:]
    )
    assert any(record['solid_funding'] is not None for record in records)
    assert any(record['solid_funding'] is None for record in records)
    assert all(record['solid_funding_schema'] == 1 for record in records)
    assert all(state['factory']['acceptance_runtime'] == runtime
               and state['factory']['coal_supply']['tick'] == state['tick']
               for state in observations)

    initial_path.write_bytes(initial_raw)
    final_path.write_bytes(final_raw)
    trial['initial_checkpoint_sha256'] = hashlib.sha256(initial_raw).hexdigest()
    trial_path.write_bytes(canonical(trial))
    preflight = json.loads(preflight_path.read_bytes())
    preflight['checkpoint_sha256'] = trial['initial_checkpoint_sha256']
    preflight_path.write_bytes(canonical(preflight))
    gameplay_path.write_bytes(b''.join(canonical(record) for record in records))

    output = tmp_path / 'capture-actual-funded-recovery'
    manifest = capture(gameplay=gameplay_path, trial_path=trial_path,
        initial_checkpoint=initial_path, final_checkpoint=final_path,
        save=tmp_path / 'save.zip', preflight_path=preflight_path, output=output)
    reviewed = verify(output)
    assert manifest['native_acceptance'] == 'not_accepted'
    assert reviewed['manifest']['native_acceptance'] == 'not_accepted'
    assert len(reviewed['rows']) == len(records)
    assert any(row['solid_funding'] is not None for row in reviewed['rows'])
    assert any(row['solid_funding'] is None for row in reviewed['rows'])
    recovered_rows = [row for row in reviewed['rows']
                      if row.get('action') == recovered['action']]
    assert recovered_rows
    assert reviewed['rows'][-1]['after_state']['factory']['solid_routes']['routes'][ROUTE]['parts'] == route['parts']

    funded_index = next(index for index, record in enumerate(records)
        if record.get('solid_funding') is not None
        and record['after_state']['factory']['solid_routes']['routes'][ROUTE]['state'] == 'building')

    def reject_capture(changed, label, reason):
        gameplay_path.write_bytes(b''.join(canonical(record) for record in changed))
        with pytest.raises(ValueError, match=reason):
            capture(gameplay=gameplay_path, trial_path=trial_path,
                initial_checkpoint=initial_path, final_checkpoint=final_path,
                save=tmp_path / 'save.zip', preflight_path=preflight_path,
                output=tmp_path / f'capture-reject-{label}')

    mismatched = deepcopy(records)
    mismatched[funded_index]['solid_funding']['target_unit'] += 1
    reject_capture(mismatched, 'funding-binding',
        'Active solid funding differs from native proposed route')

    unrelated_action = deepcopy(records)
    unrelated_action[funded_index]['action'] = 'factory_craft'
    reject_capture(unrelated_action, 'unrelated-action',
        'Active solid funding differs from native proposed route')

    multi_part = deepcopy(records)
    after_route = multi_part[funded_index]['after_state']['factory']['solid_routes']['routes'][ROUTE]
    after_route['parts']['belt:2'] = deepcopy(route['parts']['belt:2'])
    reject_capture(multi_part, 'multi-part-handoff',
        'Active solid funding differs from native proposed route')

    wrong_route = deepcopy(records)
    wrong_route[funded_index]['solid_funding']['route'] += ':tampered'
    reject_capture(wrong_route, 'route-identity',
        'Active solid funding differs from native proposed route')

    historical_building = deepcopy(records)
    historical_building[funded_index]['state']['factory']['solid_routes']['routes'][ROUTE] = deepcopy(
        historical_building[funded_index]['after_state']['factory']['solid_routes']['routes'][ROUTE])
    reject_capture(historical_building, 'unbound-building-funding',
        'Active solid funding differs from native proposed route')

    tampered = deepcopy(records)
    tampered[0]['unreviewed_solid_funding_receipt'] = {'receipt': 'unknown-critical'}
    reject_capture(tampered, 'unknown-critical-field',
        'Unknown treatment or ownership evidence field')
