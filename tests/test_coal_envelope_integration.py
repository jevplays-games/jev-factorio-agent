"""Coal-envelope analyzer regressions using real Lua-extension outputs.

The analyzed science/campaign rows are the existing documented synthetic
analyzer scaffold. Only the native coal envelope and its protocol checks are
projected from the Lua producer; this is not a whole-record or native-run claim.
"""
from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from jev_factorio import coal_supply, integration_evidence, solid_routes
from jev_factorio.acceptance_io import canonical
from jev_factorio.integration_evidence import (
    TRIAL_SCHEMA_V2, TRIAL_SCHEMA_V3, analyze_rows,
)
from jev_factorio.treatment import SCHEMA, SCHEMA_V2, digest
from coal_supply_fixtures import TARGETS, plain
from integration_evidence_fixtures import evidence
from test_coal_supply_lua import runtime
from test_complete_capture import retain_capture_route


_CASES = {}
_PAID_CASES = {}


def _analyzer_case(version, economic):
    key = (version, economic)
    if key in _CASES:
        return deepcopy(_CASES[key])

    rows, trial, initial, final = evidence()
    trial['schema'] = TRIAL_SCHEMA_V3 if version == 3 else TRIAL_SCHEMA_V2
    trial['coal_targets'] = list(TARGETS)
    trial['solid_intents'][:2] = coal_supply.intents(TARGETS)
    trial['configuration'].update(coal_supply=True, coal_kit_policy=True)
    if economic:
        trial['configuration']['coal_economic_admission'] = True
    else:
        trial['configuration'].pop('coal_economic_admission', None)
    treatment_fields = {
        'schema': SCHEMA_V2 if economic else SCHEMA,
        'solid_intents': trial['solid_intents'],
        'coal_targets': trial['coal_targets'],
        'solid_science_policy': False,
        'coal_kit_policy': True,
    }
    if economic:
        treatment_fields['coal_economic_admission'] = True
    trial['treatment_sha256'] = digest(treatment_fields)
    trial['vm_uuid'] = 'fixture-vm'
    trial['production_vm_uuid'] = 'fixture-production-vm'
    if version == 3:
        if 'iron-plate' not in trial['downstream_recipes']:
            trial['downstream_recipes'].append('iron-plate')
        trial['downstream_chain'] = [{
            'route': 'solid:386:input:iron-plate',
            'producer_role': 'recipe:iron-plate',
            'producer_recipe': 'iron-plate',
            'product_item': 'iron-plate',
            'consumer_role': 'recipe:automation-science-pack',
            'consumer_unit': 123,
            'science_pack': 'automation-science-pack',
        }]

    schema = 2 if economic else 1
    for checkpoint in (initial, final):
        checkpoint.update(
            solid_intents=deepcopy(trial['solid_intents']),
            coal_targets=list(TARGETS),
            coal_kit_policy=True,
            coal_supply_schema=schema,
            coal_epoch=dict(checkpoint['solid_epoch']),
            coal_commitments={},
            coal_funding=None,
        )
        if economic:
            checkpoint['coal_economic_admission'] = True
        else:
            checkpoint.pop('coal_economic_admission', None)

    # Keep the scaffold's one downstream input route. It has the same actor
    # epoch as the actual coal extension and no coal corridor conflicts.
    retain_capture_route(rows, initial, final)

    lua = runtime()
    if economic:
        lua.execute('campaign.set_coal_admission_evidence(true)')
    for record in rows:
        record['acceptance_configuration'] = deepcopy(trial['configuration'])
        record.update(
            solid_funding_schema=1,
            solid_funding=None,
            coal_supply=True,
            coal_supply_fault=False,
            coal_supply_evidence={},
            coal_kit_policy=True,
            coal_kit_evidence={},
            coal_economic_admission=economic,
            coal_admission_evidence={},
        )
        for label in ('state', 'after_state'):
            state = record[label]
            session_id, tick = state['session_id'], state['tick']
            # The Lua extension emits the exact state session/tick. The producer
            # output is separately checked with the production Python parser.
            lua.execute(
                'storage.jev_session_id = ' + repr(session_id) +
                '; game.tick = ' + str(tick)
            )
            native = plain(lua.eval('campaign.observe()'))
            snapshot = SimpleNamespace(session_id=session_id, tick=tick, factory=native)
            parsed = coal_supply.sources(snapshot)
            envelope = native['coal_supply']
            assert set(parsed) == set(TARGETS)
            assert envelope['protocol'] == schema
            assert envelope['targets'] == list(TARGETS)
            assert all(envelope[k] == state['factory']['solid_routes'][k]
                       for k in ('actor_index', 'surface_index', 'force_index'))
            state['factory']['coal_supply'] = deepcopy(envelope)
            record['coal_supply_evidence'] = deepcopy(envelope)
            if economic:
                record['coal_admission_evidence'] = deepcopy(envelope['admission'])
        record['solid_route_evidence'] = deepcopy(
            record['after_state']['factory']['solid_routes'])

    trial['initial_checkpoint_sha256'] = hashlib.sha256(canonical(initial)).hexdigest()
    _CASES[key] = (rows, trial, initial, final)
    return deepcopy(_CASES[key])


def _paid_analyzer_case(version, economic):
    """Synthetic campaign scaffold carrying complete Lua-produced coal rows."""
    key = (version, economic)
    if key in _PAID_CASES:
        return deepcopy(_PAID_CASES[key])
    rows, trial, initial, final = _analyzer_case(version, economic)
    session_id = 'private-fixture-session'
    lua = runtime()
    if economic:
        lua.execute('campaign.set_coal_admission_evidence(true)')
    lua.execute(
        "storage.jev_session_id='" + session_id +
        "'; game.tick=1000; coal_all(); coal_pulse(); coal_pulse(); coal_pulse()"
    )
    first_tick = 1180
    snapshots = {}

    def snapshot_at(tick):
        if tick not in snapshots:
            lua.execute(f'game.tick={tick}')
            snapshots[tick] = plain(lua.eval('campaign.observe()'))
        return deepcopy(snapshots[tick])

    for row_index, record in enumerate(rows):
        before_tick = first_tick + max(0, row_index - 1) * 3600
        after_tick = first_tick + row_index * 3600
        for label, tick in (('state', before_tick), ('after_state', after_tick)):
            state = record[label]
            prior_solid = state['factory']['solid_routes']
            input_routes = {key: deepcopy(value)
                            for key, value in prior_solid['routes'].items()
                            if value['item'] != 'coal'}
            native = snapshot_at(tick)
            native_routes = deepcopy(native['solid_routes']['routes'])
            native_routes.update(input_routes)
            solid_native = deepcopy(native['solid_routes'])
            solid_native['tick'] = tick
            solid_native['routes'] = native_routes
            state['tick'] = tick
            state['factory']['tick'] = tick
            state['factory']['solid_routes'] = solid_native
            state['factory']['coal_supply'] = deepcopy(native['coal_supply'])
            for role, entity in native['entities'].items():
                state['factory']['entities'][role] = deepcopy(entity)
            for route in input_routes.values():
                if route.get('flow'):
                    route['flow']['last_tick'] = tick
                    route['flow']['last_positive_tick'] = tick
                solid_native['routes'][route['route']] = route
            record['solid_route_evidence'] = deepcopy(solid_native)
            record['coal_supply_evidence'] = deepcopy(native['coal_supply'])
            if economic:
                record['coal_admission_evidence'] = deepcopy(native['coal_supply']['admission'])
        record['tick'] = after_tick

    first_state = rows[0]['state']
    final_state = rows[-1]['after_state']
    first_native = first_state['factory']
    final_native = final_state['factory']
    first_coal = coal_supply.sources(SimpleNamespace(
        session_id=session_id, tick=first_state['tick'], factory=first_native))
    final_coal = coal_supply.sources(SimpleNamespace(
        session_id=session_id, tick=final_state['tick'], factory=final_native))
    first_solid = solid_routes.routes(SimpleNamespace(
        session_id=session_id, tick=first_state['tick'], factory=first_native))
    final_solid = solid_routes.routes(SimpleNamespace(
        session_id=session_id, tick=final_state['tick'], factory=final_native))
    epoch = {key: first_native['coal_supply'][key]
             for key in ('actor_index', 'surface_index', 'force_index')}
    initial.update(
        coal_epoch=deepcopy(epoch),
        solid_epoch=deepcopy(epoch),
        coal_commitments={key: coal_supply.commitment(value)
                          for key, value in first_coal.items()},
        solid_commitments={key: solid_routes.commitment(value)
                           for key, value in first_solid.items()},
        last_tick=first_state['tick'],
    )
    final.update(
        coal_epoch=deepcopy(epoch),
        solid_epoch=deepcopy(epoch),
        coal_commitments={key: coal_supply.commitment(value)
                          for key, value in final_coal.items()},
        solid_commitments={key: solid_routes.commitment(value)
                           for key, value in final_solid.items()},
        last_tick=final_state['tick'],
    )
    trial['initial_checkpoint_sha256'] = hashlib.sha256(canonical(initial)).hexdigest()
    _PAID_CASES[key] = (rows, trial, initial, final)
    return deepcopy(_PAID_CASES[key])


@pytest.mark.parametrize('version', [2, 3])
@pytest.mark.parametrize('economic', [False, True])
def test_actual_protocol_positive_and_missing_envelopes_are_checked_in_both_boundaries(
        version, economic):
    rows, trial, initial, final = _analyzer_case(version, economic)
    valid = analyze_rows(rows, trial, initial, final)
    assert valid['integrity_checks_passed'], valid['issues']
    assert valid['native_acceptance'] == 'not_accepted'

    for boundary in ('state', 'after_state'):
        broken = deepcopy(rows)
        broken[11][boundary]['factory'].pop('coal_supply')
        result = analyze_rows(broken, trial, initial, final)
        assert not result['integrity_checks_passed'], result['issues']
        assert 'coal_native_evidence_invalid' in result['issues']
        assert result['native_acceptance'] == 'not_accepted'


@pytest.mark.parametrize('version', [2, 3])
@pytest.mark.parametrize('economic', [False, True])
@pytest.mark.parametrize('mutation', ['empty', 'protocol', 'targets', 'session', 'tick', 'actor'])
def test_actual_coal_envelope_treatment_identity_and_targets_are_bound(
        version, economic, mutation):
    rows, trial, initial, final = _analyzer_case(version, economic)
    broken = deepcopy(rows)
    native = broken[14]['after_state']['factory']['coal_supply']
    if mutation == 'empty':
        native.clear()
    elif mutation == 'protocol':
        native['protocol'] = 2 if not economic else 1
    elif mutation == 'targets':
        native['targets'] = ['gamma', 'delta']
    elif mutation == 'session':
        native['session_id'] = 'different-session'
    elif mutation == 'tick':
        native['tick'] += 1
    elif mutation == 'actor':
        native['actor_index'] += 1

    result = analyze_rows(broken, trial, initial, final)
    assert not result['integrity_checks_passed'], result['issues']
    assert 'coal_native_evidence_invalid' in result['issues']
    assert result['native_acceptance'] == 'not_accepted'


@pytest.mark.parametrize('economic', [False, True])
def test_actual_paid_producer_prefix_binds_receipts_and_checkpoints(economic):
    session_id = 'coal386-paid-prefix'
    lua = runtime()
    if economic:
        lua.execute('campaign.set_coal_admission_evidence(true)')
    lua.execute(
        "storage.jev_session_id='" + session_id +
        "'; game.tick=70000; coal_all(); coal_pulse(); coal_pulse(); coal_pulse()"
    )
    native = plain(lua.eval('campaign.observe()'))
    state = {'session_id': session_id, 'tick': native['tick'], 'factory': native}
    observed = coal_supply.sources(SimpleNamespace(
        session_id=session_id, tick=native['tick'], factory=native))
    assert native['coal_supply']['protocol'] == (2 if economic else 1)
    assert native['coal_supply']['committed'] is True
    assert set(observed) == set(TARGETS)
    saved = {target: coal_supply.commitment(row) for target, row in observed.items()}
    checkpoint = {
        'session_id': session_id,
        'coal_targets': list(TARGETS),
        'coal_epoch': {key: native['coal_supply'][key]
                       for key in ('actor_index', 'surface_index', 'force_index')},
        'coal_commitments': deepcopy(saved),
    }
    configuration = {'coal_economic_admission': economic}
    retained = integration_evidence._checked_coal_observation(
        state, configuration, list(TARGETS), expected_session=session_id,
        checkpoint=checkpoint)
    assert retained == saved

    changed = deepcopy(state)
    changed['factory']['coal_supply']['sources']['alpha']['parts']['chest']['receipt'] += ':changed'
    # The source parser accepts a syntactically valid different receipt. The
    # analyzer's retained checkpoint comparison must reject the identity swap.
    with pytest.raises(integration_evidence._CoalOwnershipMismatch):
        integration_evidence._checked_coal_observation(
            changed, configuration, list(TARGETS), expected_session=session_id,
            checkpoint=checkpoint)

    missing_owner = deepcopy(checkpoint)
    missing_owner['coal_commitments'] = {}
    with pytest.raises(integration_evidence._CoalOwnershipMismatch):
        integration_evidence._checked_coal_observation(
            state, configuration, list(TARGETS), expected_session=session_id,
            checkpoint=missing_owner)

    # The same production helper maintains a receipt prefix between arbitrary
    # observations, even when neither endpoint checksum is being checked.
    with pytest.raises(integration_evidence._CoalOwnershipMismatch):
        integration_evidence._checked_coal_observation(
            changed, configuration, list(TARGETS), expected_session=session_id,
            retained=saved)


@pytest.mark.parametrize('version', [2, 3])
@pytest.mark.parametrize('economic', [False, True])
def test_paid_native_checkpoint_receipts_are_bound_through_public_analyzer(version, economic):
    rows, trial, initial, final = _paid_analyzer_case(version, economic)
    valid = analyze_rows(rows, trial, initial, final)
    assert valid['integrity_checks_passed'], valid['issues']
    assert valid['native_acceptance'] == 'not_accepted'
    assert 'two_distinct_fuel_consumers_not_measured' in valid['issues']

    changed_receipt = deepcopy(rows)
    changed_receipt[-1]['after_state']['factory']['coal_supply']['sources']['alpha']['parts']['chest']['receipt'] += ':changed'
    rejected_receipt = analyze_rows(changed_receipt, trial, initial, final)
    assert not rejected_receipt['integrity_checks_passed']
    assert 'coal_ownership_regressed' in rejected_receipt['issues']
    assert rejected_receipt['native_acceptance'] == 'not_accepted'

    missing_final_owner = deepcopy(final)
    missing_final_owner['coal_commitments'] = {}
    rejected_final = analyze_rows(rows, trial, initial, missing_final_owner)
    assert not rejected_final['integrity_checks_passed']
    assert 'coal_ownership_regressed' in rejected_final['issues']

    missing_initial_owner = deepcopy(initial)
    missing_initial_owner['coal_commitments'] = {}
    trial_with_initial_hash = deepcopy(trial)
    trial_with_initial_hash['initial_checkpoint_sha256'] = hashlib.sha256(
        canonical(missing_initial_owner)).hexdigest()
    rejected_initial = analyze_rows(rows, trial_with_initial_hash,
                                    missing_initial_owner, final)
    assert not rejected_initial['integrity_checks_passed']
    assert 'coal_ownership_regressed' in rejected_initial['issues']
