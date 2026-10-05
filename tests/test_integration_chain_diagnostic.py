"""Opt-in intermediate chain diagnostics never certify material provenance."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from jev_factorio import integration_evidence as evidence
from jev_factorio.coal_supply import intents
from jev_factorio.solid_routes import commitment, current
from jev_factorio.treatment import SCHEMA, digest
from jev_factorio.backends.native_attachment import (
    PINNED_ASSETS, WATER_ORIGIN_OBSERVATION_PROFILE,
)
from jev_factorio.downstream_recipe_witness import capture_bundle
from integration_evidence_fixtures import evidence as fixture


CHAIN = {'route': 'solid:1:2:iron-ore:input', 'producer_role': 'recipe:iron-plate',
         'producer_recipe': 'iron-plate', 'product_item': 'iron-plate',
         'consumer_role': 'recipe:automation-science-pack', 'consumer_unit': 22,
         'science_pack': 'automation-science-pack'}


def trial_v3():
    _, trial, _, _ = fixture()
    trial['schema'] = evidence.TRIAL_SCHEMA_V3
    trial['downstream_recipes'] = ['iron-plate']
    trial['downstream_chain'] = [deepcopy(CHAIN)]
    trial['coal_targets'] = [trial['solid_intents'][0]['target'], trial['solid_intents'][1]['target']]
    trial['solid_intents'][:2] = intents(trial['coal_targets'])
    trial['configuration'].update(coal_supply=True, coal_kit_policy=True)
    trial['treatment_sha256'] = digest({'schema': SCHEMA, 'solid_intents': trial['solid_intents'],
        'coal_targets': trial['coal_targets'], 'solid_science_policy': False, 'coal_kit_policy': True})
    trial['initial_checkpoint_sha256'] = '0' * 64
    trial['vm_uuid'] = 'isolated-vm'
    trial['production_vm_uuid'] = 'production-vm'
    return trial


def chain_inputs():
    route = {'route': CHAIN['route'], 'kind': 'downstream', 'target_unit': 11,
             'recipe': 'iron-plate', 'attributed_positive_boundaries': 3,
             'attributed_received': 4, 'first_positive_tick': 10,
             'target_first_products': 0, 'target_last_products': 2}
    observed_route = {'target': {'role': CHAIN['producer_role'], 'unit_number': 11}}
    rows = [{'after_state': {'factory': {'solid_routes': {'routes': {CHAIN['route']: observed_route}},
            'entities': {CHAIN['consumer_role']: {'unit_number': 22,
                'recipe': CHAIN['science_pack'], 'products_finished': products}}}}}
            for products in (0, 2)]
    receipts = {
        'extract-ingredient': {'role': CHAIN['producer_role'], 'unit_number': 11,
            'item': CHAIN['product_item'], 'extracting': True, 'quantity': 2, 'tick': 11},
        'insert-ingredient': {'role': CHAIN['consumer_role'], 'unit_number': 22,
            'item': CHAIN['product_item'], 'extracting': False, 'quantity': 2, 'tick': 12},
        'extract-pack': {'role': CHAIN['consumer_role'], 'unit_number': 22,
            'item': CHAIN['science_pack'], 'extracting': True, 'quantity': 1, 'tick': 13},
        'insert-lab': {'role': 'utility:lab', 'unit_number': 33,
            'item': CHAIN['science_pack'], 'extracting': False, 'quantity': 1, 'tick': 14},
    }
    return rows, {CHAIN['route']: route}, receipts


def diagnose(rows, routes, receipts, chain=CHAIN):
    return evidence._downstream_chain_diagnostic([chain], rows, routes, receipts, 33)


def test_v3_declares_bounded_intermediate_chain_without_changing_old_trial():
    evidence.validate_trial(trial_v3())
    _, old, _, _ = fixture()
    evidence.validate_trial(old)


@pytest.mark.parametrize('change', [
    lambda t: t['downstream_chain'][0].update(producer_recipe='copper-plate'),
    lambda t: t['downstream_chain'][0].update(science_pack='logistic-science-pack'),
    lambda t: t['downstream_chain'].append(deepcopy(t['downstream_chain'][0])),
    lambda t: t['downstream_chain'][0].update(consumer_unit=True),
])
def test_v3_rejects_unbound_or_duplicated_declaration(change):
    trial = trial_v3()
    change(trial)
    with pytest.raises(ValueError, match='downstream chain'):
        evidence.validate_trial(trial)


def test_complete_paid_sequence_is_diagnostic_correlation_only():
    value = diagnose(*chain_inputs())
    assert value['correlated_sequences'] == 1
    assert value['status_by_declared_order'] == ['correlated_paid_transfer_sequence']
    assert value['intermediate_provenance_qualified'] is False


def stock_chain_inputs():
    """Paid insert first appears with fresh input stock, followed by recipe work."""
    _, routes, receipts = chain_inputs()
    receipts['extract-pack']['tick'] = 15
    receipts['insert-lab']['tick'] = 16
    item, role = CHAIN['product_item'], CHAIN['consumer_role']
    route = {'route': CHAIN['route'], 'item': 'iron-ore',
             'source': {'role': 'buffer:iron-ore', 'unit_number': 10},
             'target': {'role': CHAIN['producer_role'], 'unit_number': 11,
                        'inventory': 'input', 'recipe': CHAIN['producer_recipe']},
             'steps': [{'part': 'receive'}, {'part': 'send'}]}
    def state(tick, stock, products, seen):
        return {'tick': tick, 'session_id': 'witness-session', 'factory': {
            'tick': tick,
            'acceptance_runtime': {'schema': 1, 'session_id': 'witness-session',
                'actor_unit': 9, 'player_index': 1, 'surface_index': 1,
                'force_index': 1, 'mods': {'base': '2.0.77'}},
            'solid_routes': {'routes': {CHAIN['route']: deepcopy(route)}},
            'entities': {
                CHAIN['producer_role']: {'unit_number': 11,
                    'recipe': CHAIN['producer_recipe'], 'products_finished': products},
                role: {'unit_number': 22, 'recipe': CHAIN['science_pack'],
                       'products_finished': products,
                       'input': {item: stock} if stock else {}},
            },
            'receipts': {key: deepcopy(receipts[key]) for key in seen}}}
    rows = [
        {'state': state(10, 0, 0, ()), 'after_state': state(10, 0, 0, ())},
        {'state': state(11, 0, 0, ('extract-ingredient',)),
         'after_state': state(12, 2, 0, ('extract-ingredient', 'insert-ingredient'))},
        {'state': state(13, 2, 0, ('extract-ingredient', 'insert-ingredient')),
         'after_state': state(14, 0, 1, ('extract-ingredient', 'insert-ingredient'))},
        {'state': state(15, 0, 1, ('extract-ingredient', 'insert-ingredient', 'extract-pack')),
         'after_state': state(16, 0, 1, receipts)},
    ]
    return rows, routes, receipts


def recipe_witness_capture(chain, record, tick):
    state = record['after_state']
    factory = state['factory']
    route = factory['solid_routes']['routes'][chain['route']]
    runtime = factory['acceptance_runtime']
    route_binding = evidence._recipe_witness_route(route)
    request = {'route': chain['route'], 'producer_role': chain['producer_role'],
        'producer_unit': route['target']['unit_number'], 'product_item': chain['product_item'],
        'consumer_role': chain['consumer_role'], 'consumer_unit': chain['consumer_unit'],
        'science_pack': chain['science_pack']}
    epoch = {'session_id': state['session_id'], 'tick': tick,
        'actor_index': runtime['player_index'], 'actor_unit': runtime['actor_unit'],
        'surface_index': runtime['surface_index'], 'force_index': runtime['force_index']}
    expected_epoch = {key: value for key, value in epoch.items() if key != 'tick'}
    expected_epoch.update(min_tick=tick, max_tick=tick)
    result = {'schema': 'jev.downstream-recipe-witness.v1', 'status': 'observed',
        'reason': 'none', 'base_version': '2.0.77', 'epoch': epoch, 'request': request,
        'route': route_binding,
        'producer': {'role': chain['producer_role'], 'unit': request['producer_unit'],
            'recipe': {'name': chain['producer_recipe'], 'product': chain['product_item'],
                'product_amount': 1, 'ingredients': {route['item']: 1}}},
        'consumer': {'role': chain['consumer_role'], 'unit': chain['consumer_unit'],
            'recipe': {'name': chain['science_pack'], 'product': chain['science_pack'],
                'product_amount': 1, 'ingredients': {chain['product_item']: 1}}},
        'recipe_dependency_verified': True,
        'stock_provenance_qualified': False, 'mutation_authorized': False}
    attachment = {'session_id': state['session_id'], 'actor_unit': runtime['actor_unit'],
        'modules': {'solid_routes': True},
        'native_installation': {'profile': WATER_ORIGIN_OBSERVATION_PROFILE,
            'assets': {'solid_routes': PINNED_ASSETS['solid_routes']}}}
    return capture_bundle(result, request=request, expected_epoch=expected_epoch,
        expected_route=route_binding, attachment=attachment)


def test_paid_receipt_fresh_stock_and_later_recipe_work_are_correlated_only():
    value = diagnose(*stock_chain_inputs())
    assert value['receipt_stock_recipe_status_by_declared_order'] == [
        'paid_insert_stock_and_recipe_work_correlated']
    assert value['receipt_stock_recipe_correlations'] == 1
    assert value['intermediate_provenance_qualified'] is False


def test_recipe_witness_composes_with_receipt_stock_correlation_without_qualifying_provenance():
    rows, routes, receipts = stock_chain_inputs()
    rows[1]['recipe_dependency_witnesses'] = [recipe_witness_capture(CHAIN, rows[1], 12)]
    value = diagnose(rows, routes, receipts)
    assert value['status_by_declared_order'] == ['correlated_paid_transfer_sequence']
    assert value['receipt_stock_recipe_status_by_declared_order'] == [
        'paid_insert_stock_and_recipe_work_correlated']
    assert value['recipe_dependency_status_by_declared_order'] == [
        'recipe_dependency_observed']
    assert value['current_recipe_dependency_matches'] == 1
    assert value['composed_status_by_declared_order'] == [
        'paid_stock_recipe_dependency_correlated']
    assert value['receipt_stock_recipe_dependency_correlations'] == 1
    assert value['intermediate_provenance_qualified'] is False
    assert 'exact-unit provenance' in value['scope']


@pytest.mark.parametrize('damage', [
    lambda bundle: bundle['expected_route'].update(target_unit=99),
    lambda bundle: bundle['attachment'].update(session_id='foreign-session'),
    lambda bundle: bundle['expected_epoch'].update(actor_index=2),
    lambda bundle: bundle['result']['consumer']['recipe']['ingredients'].clear(),
    lambda bundle: bundle['result']['epoch'].update(tick=999),
])
def test_mismatched_recipe_witness_cannot_upgrade_receipt_stock_correlation(damage):
    rows, routes, receipts = stock_chain_inputs()
    bundle = recipe_witness_capture(CHAIN, rows[1], 12)
    damage(bundle)
    rows[1]['recipe_dependency_witnesses'] = [bundle]
    value = diagnose(rows, routes, receipts)
    assert value['receipt_stock_recipe_correlations'] == 1
    assert value['current_recipe_dependency_matches'] == 0
    assert value['receipt_stock_recipe_dependency_correlations'] == 0
    assert value['intermediate_provenance_qualified'] is False


@pytest.mark.parametrize('mutate_observation', [
    lambda record: record['after_state']['factory']['acceptance_runtime'].update(
        session_id='foreign-session'),
    lambda record: record['after_state']['factory']['entities'][CHAIN['producer_role']].update(
        unit_number=99),
    lambda record: record['after_state']['factory']['entities'][CHAIN['producer_role']].update(
        recipe='steel-plate'),
])
def test_recipe_witness_must_match_observed_runtime_and_producer(mutate_observation):
    rows, routes, receipts = stock_chain_inputs()
    rows[1]['recipe_dependency_witnesses'] = [recipe_witness_capture(CHAIN, rows[1], 12)]
    for label in ('state', 'after_state'):
        mutate_observation({'after_state': rows[1][label]})
    value = diagnose(rows, routes, receipts)
    assert value['recipe_dependency_status_by_declared_order'] == [
        'recipe_dependency_observation_not_bound']
    assert value['current_recipe_dependency_matches'] == 0
    assert value['receipt_stock_recipe_dependency_correlations'] == 0
    assert value['intermediate_provenance_qualified'] is False


def test_duplicate_recipe_witnesses_are_ambiguous_not_additional_support():
    rows, routes, receipts = stock_chain_inputs()
    bundle = recipe_witness_capture(CHAIN, rows[1], 12)
    rows[1]['recipe_dependency_witnesses'] = [bundle, deepcopy(bundle)]
    value = diagnose(rows, routes, receipts)
    assert value['recipe_dependency_status_by_declared_order'] == [
        'recipe_dependency_witness_ambiguous']
    assert value['current_recipe_dependency_matches'] == 0
    assert value['receipt_stock_recipe_dependency_correlations'] == 0
    assert value['intermediate_provenance_qualified'] is False


def test_malformed_capture_list_cannot_be_hidden_by_a_separate_valid_witness():
    rows, routes, receipts = stock_chain_inputs()
    rows[1]['recipe_dependency_witnesses'] = [recipe_witness_capture(CHAIN, rows[1], 12)]
    rows[-1]['recipe_dependency_witnesses'] = {'malformed': 'capture list'}
    value = diagnose(rows, routes, receipts)
    assert value['recipe_dependency_status_by_declared_order'] == [
        'recipe_dependency_witness_unqualified']
    assert value['current_recipe_dependency_matches'] == 0
    assert value['receipt_stock_recipe_dependency_correlations'] == 0
    assert value['intermediate_provenance_qualified'] is False


def test_unassociated_malformed_capture_member_downgrades_valid_bundle_in_same_list():
    rows, routes, receipts = stock_chain_inputs()
    rows[1]['recipe_dependency_witnesses'] = [
        recipe_witness_capture(CHAIN, rows[1], 12),
        {'malformed': 'unassociated capture'},
    ]
    value = diagnose(rows, routes, receipts)
    assert value['recipe_dependency_status_by_declared_order'] == [
        'recipe_dependency_witness_unqualified']
    assert value['current_recipe_dependency_matches'] == 0
    assert value['receipt_stock_recipe_dependency_correlations'] == 0
    assert value['intermediate_provenance_qualified'] is False


def test_explicit_null_capture_list_cannot_hide_behind_a_valid_bundle():
    rows, routes, receipts = stock_chain_inputs()
    rows[1]['recipe_dependency_witnesses'] = [recipe_witness_capture(CHAIN, rows[1], 12)]
    rows[-1]['recipe_dependency_witnesses'] = None
    value = diagnose(rows, routes, receipts)
    assert value['recipe_dependency_status_by_declared_order'] == [
        'recipe_dependency_witness_unqualified']
    assert value['current_recipe_dependency_matches'] == 0
    assert value['receipt_stock_recipe_dependency_correlations'] == 0
    assert value['intermediate_provenance_qualified'] is False


def change_later(rows, *, stock=None, products=None):
    for row in rows[2:]:
        for label in ('state', 'after_state'):
            entity = row[label]['factory']['entities'][CHAIN['consumer_role']]
            if stock is not None:
                entity['input'] = {'iron-plate': stock} if stock else {}
            if products is not None:
                entity['products_finished'] = products


@pytest.mark.parametrize('damage', [
    lambda rows, receipts: rows[0]['state']['factory']['receipts'].update(
        {'insert-ingredient': deepcopy(receipts['insert-ingredient'])}),
    lambda rows, receipts: rows[1]['state']['factory']['entities'][
        CHAIN['consumer_role']]['input'].update({'iron-plate': 1}),
    lambda rows, receipts: rows[1]['after_state']['factory']['entities'][
        CHAIN['consumer_role']]['input'].update({'iron-plate': 3}),
    lambda rows, receipts: receipts.update({'duplicate-insert': deepcopy(receipts['insert-ingredient'])}),
    lambda rows, receipts: change_later(rows, stock=2),
    lambda rows, receipts: change_later(rows, products=0),
    lambda rows, receipts: rows[2]['state']['factory']['entities'][
        CHAIN['consumer_role']].update(input={}),
    lambda rows, receipts: rows[2]['after_state']['factory']['receipts'][
        'insert-ingredient'].update(quantity=3),
])
def test_stale_duplicate_unrelated_or_unconsumed_stock_does_not_qualify(damage):
    rows, routes, receipts = stock_chain_inputs()
    damage(rows, receipts)
    value = diagnose(rows, routes, receipts)
    assert value['receipt_stock_recipe_correlations'] == 0
    assert value['intermediate_provenance_qualified'] is False


def test_foreign_receipt_and_invalid_route_cannot_qualify_stock_sequence():
    rows, routes, receipts = stock_chain_inputs()
    receipts['insert-ingredient']['unit_number'] = 99
    assert diagnose(rows, routes, receipts)['receipt_stock_recipe_correlations'] == 0
    rows, routes, receipts = stock_chain_inputs()
    routes[CHAIN['route']]['attributed_received'] = 0
    assert diagnose(rows, routes, receipts)['receipt_stock_recipe_status_by_declared_order'] == [
        'chain_not_qualified']


@pytest.mark.parametrize('damage', [
    lambda rows, routes, receipts: rows[0].pop('after_state'),
    lambda rows, routes, receipts: rows[0]['after_state']['factory'].pop('solid_routes'),
    lambda rows, routes, receipts: rows[-1]['after_state']['factory']['entities'].pop(
        CHAIN['consumer_role']),
    lambda rows, routes, receipts: routes[CHAIN['route']].pop('recipe'),
])
def test_missing_or_malformed_observation_paths_fail_closed(damage):
    rows, routes, receipts = chain_inputs()
    damage(rows, routes, receipts)
    assert diagnose(rows, routes, receipts)['correlated_sequences'] == 0


def test_analyzer_keeps_intermediate_route_unqualified_with_sequence():
    rows, trial, initial, final = fixture()
    route = next(value for value in rows[0]['after_state']['factory']['solid_routes']['routes'].values()
                 if value['target']['inventory'] == 'input')
    chain = {**CHAIN, 'route': route['route'], 'producer_role': route['target']['role'],
             'consumer_role': 'fixture:science-assembler', 'consumer_unit': 2233}
    trial['schema'] = evidence.TRIAL_SCHEMA_V3
    trial['downstream_recipes'] = ['iron-plate']
    trial['downstream_chain'] = [chain]
    trial['coal_targets'] = [trial['solid_intents'][0]['target'], trial['solid_intents'][1]['target']]
    trial['solid_intents'][:2] = intents(trial['coal_targets'])
    trial['configuration'].update(coal_supply=True, coal_kit_policy=True)
    trial['treatment_sha256'] = digest({'schema': SCHEMA, 'solid_intents': trial['solid_intents'],
        'coal_targets': trial['coal_targets'], 'solid_science_policy': False, 'coal_kit_policy': True})
    trial['initial_checkpoint_sha256'] = '0' * 64
    trial['vm_uuid'], trial['production_vm_uuid'] = 'isolated-vm', 'production-vm'
    for checkpoint in (initial, final):
        checkpoint['solid_intents'] = trial['solid_intents']
        checkpoint['coal_targets'] = trial['coal_targets']
        checkpoint['coal_kit_policy'] = True
        checkpoint['coal_supply_schema'] = 1
        checkpoint['coal_epoch'] = dict(checkpoint['solid_epoch'])
        checkpoint['coal_commitments'] = {}
    for index, record in enumerate(rows):
        record['acceptance_configuration'].update(coal_supply=True, coal_kit_policy=True)
        # Mirror CoalSupplyMixin._record_extras for the disabled-economic-
        # admission composition. The snapshots below model a supported empty
        # bundle; the producer still emits each declared treatment/evidence
        # field on every record.
        record.update(coal_supply=True, coal_supply_fault=False, coal_kit_policy=True,
                      coal_kit_evidence={}, coal_economic_admission=False,
                      coal_admission_evidence={})
        for label in ('state', 'after_state'):
            state = record[label]
            state['factory']['coal_supply'] = {'protocol': 1, 'session_id': state['session_id'],
                'tick': state['tick'], 'actor_index': 1, 'surface_index': 1, 'force_index': 1,
                'targets': trial['coal_targets'], 'committed': False, 'sources': {},
                'reason': 'no_supported_bundle'}
            for coal_route in state['factory']['solid_routes']['routes'].values():
                if coal_route['target']['inventory'] != 'fuel':
                    continue
                old_role = coal_route['source']['role']
                new_role = 'coal:' + coal_route['target']['role'] + ':chest'
                coal_route['source']['role'] = new_role
                state['factory']['entities'][new_role] = state['factory']['entities'].pop(old_role)
            routed = state['factory']['solid_routes']['routes'][chain['route']]
            routed['target']['recipe'] = 'iron-plate'
            state['factory']['entities'][chain['producer_role']]['recipe'] = 'iron-plate'
            state['factory']['entities'][chain['consumer_role']] = {
                'name': 'assembling-machine-1', 'unit_number': chain['consumer_unit'],
                'recipe': chain['science_pack'], 'products_finished': index * 2}
            additions = {
                2: ('ingredient-out', chain['producer_role'], routed['target']['unit_number'],
                    'iron-plate', True, 2, 8200),
                3: ('ingredient-in', chain['consumer_role'], chain['consumer_unit'],
                    'iron-plate', False, 2, 11800),
                4: ('pack-out', chain['consumer_role'], chain['consumer_unit'],
                    chain['science_pack'], True, 2, 15400),
            }
            for step, (receipt_id, role, unit, item, extracting, qty, tick) in additions.items():
                if index >= step:
                    state['factory']['receipts'][receipt_id] = {
                        'role': role, 'unit_number': unit, 'item': item,
                        'extracting': extracting, 'quantity': qty, 'tick': tick}
        record['coal_supply_evidence'] = deepcopy(record['state']['factory']['coal_supply'])
    for checkpoint, state in ((initial, rows[0]['state']), (final, rows[-1]['after_state'])):
        checkpoint['solid_commitments'] = {key: commitment(value) for key, value in
            state['factory']['solid_routes']['routes'].items()}
    for record in rows:
        for label in ('state', 'after_state'):
            state = record[label]
            view = SimpleNamespace(factory=state['factory'])
            for key, value in state['factory']['solid_routes']['routes'].items():
                assert current(value, view), (label, state['tick'], key)
    rows[0]['recipe_dependency_witnesses'] = [recipe_witness_capture(
        chain, rows[0], rows[0]['after_state']['tick'])]
    result = evidence.analyze_rows(rows, trial, initial, final)
    assert result['integrity_checks_passed'], result['issues']
    diagnostic = result['transport']['intermediate_chain_diagnostic']
    assert diagnostic['correlated_sequences'] == 1, diagnostic
    assert diagnostic['recipe_dependency_status_by_declared_order'] == [
        'recipe_dependency_observed']
    assert diagnostic['composed_status_by_declared_order'] == ['recipe_dependency_only']
    assert diagnostic['intermediate_provenance_qualified'] is False
    assert result['transport']['downstream_routes_with_flow_and_production'] == 0
    assert 'downstream_flow_and_production_not_measured' in result['outcome_gaps']
    assert result['native_acceptance'] == 'not_accepted'
    assert result['deployment_authorized'] is False


@pytest.mark.parametrize('break_witness', [
    lambda rows, routes, receipts: receipts['extract-ingredient'].update(unit_number=999),
    lambda rows, routes, receipts: receipts['insert-ingredient'].update(quantity=1),
    lambda rows, routes, receipts: receipts['extract-pack'].update(tick=9),
    lambda rows, routes, receipts: receipts['insert-lab'].update(unit_number=999),
    lambda rows, routes, receipts: rows[-1]['after_state']['factory']['entities'][
        CHAIN['consumer_role']].update(recipe='transport-belt'),
    lambda rows, routes, receipts: routes[CHAIN['route']].update(target_last_products=0),
    lambda rows, routes, receipts: routes[CHAIN['route']].update(first_positive_tick=15),
])
def test_missing_stale_or_wrong_owner_boundary_cannot_report_sequence(break_witness):
    rows, routes, receipts = chain_inputs()
    break_witness(rows, routes, receipts)
    assert diagnose(rows, routes, receipts)['correlated_sequences'] == 0
