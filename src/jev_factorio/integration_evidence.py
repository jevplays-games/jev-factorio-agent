"""Read-only #92/#103 integration evidence checks, separate from ore-side trials.

Consumes retained, complete gameplay observations and checkpoint bytes.  Does not
start, stop, resume, deploy, mutate a game, extend a cutoff, or certify provenance.
Only fixed labels, hashes, counts and timings are emitted.  Raw evidence stays
private.  In particular, a coal corridor is NOT evidence of a coal mining source.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import math
import hashlib
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace

from . import (input_routes, mining_outposts, solid_routes, coal_supply, treatment,
               downstream_recipe_witness)
from .acceptance_io import MAX_JSON, MAX_LOG, canonical, load_json, records, sha256, stable_read, write_new
from .acceptance_boundaries import (final_successor_issues, project_history_issues,
                                    successor_history_issues)
from .backends.solid_routes import validate_intents
from .campaign_progress import SCIENCE
from .iteration_timing import NAMES, validate_timing
from .latency_report import distribution
from .memory import load_checkpoint
from .solid_funding_evidence import funding_history_issues
from .planning import capital
from .telemetry import validate_phase

SCHEMA = 'jev-factorio.integration-evidence.v1'
TRIAL_SCHEMA = 'jev-factorio.integration-trial.v1'
TRIAL_SCHEMA_V2 = 'jev-factorio.integration-trial.v2'
TRIAL_SCHEMA_V3 = 'jev-factorio.integration-trial.v3'
TRIAL_KEYS = {
    'schema', 'evidence_kind', 'arm', 'comparison_axis', 'experiment_sha256',
    'workload_sha256', 'initial_save_sha256', 'capacity_profile_sha256',
    'expected_commit', 'expected_source_sha256', 'configuration', 'solid_intents',
    'campaign_treatment', 'requested_model', 'resolved_model',
    'declared_at_utc', 'original_cutoff_utc', 'runtime_cutoff_utc',
    'minimum_window_seconds', 'max_observation_gap_seconds',
    'max_no_science_progress_seconds', 'science_packs', 'research_goal',
    'downstream_recipes', 'minimum_timing_samples', 'regression_limits',
}
FLAGS = {'background_work', 'furnace_output_buffers', 'furnace_input_belts',
         'mining_outposts', 'ore_side_successors', 'solid_routes', 'solid_science_policy'}
COAL_FLAGS = {'coal_supply', 'coal_kit_policy'}
REQUIRED_GATES = [
    'native_coal_mining_bootstrap_and_network_fuel_provenance',
    'downstream_chain_causal_use_review',
    'native_crash_restart_partial_construction_matrix',
    'effective_host_capacity_and_contention_readback',
    'independent_exact_head_source_and_integration_review',
    'ssh_signed_publication_and_exact_head_hosted_checks',
    'authorized_immutable_runtime_and_original_cutoff_readback',
    'external_native_evidence_authenticity',
    'actual_provider_call_provenance',
    'source_clean_tree_and_fingerprint_readback',
    'native_persistence_bytes_writes_and_actor_haul_counts',
    'clock_domain_and_complete_window_attribution_review',
]
METRIC_COUNTERS = ('command_calls', 'batch_calls', 'failed_calls', 'request_bytes',
                   'response_bytes', 'unknown_request_size_calls', 'unknown_response_size_calls')


def _number(value, minimum=0, maximum=2**53 - 1):
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)


def _integer(value, minimum=0, maximum=2**53 - 1):
    return type(value) is int and minimum <= value <= maximum


def _digest(value, size=64):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{' + str(size) + '}', value) is not None


def _utc(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('Invalid UTC timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() != timezone.utc.utcoffset(result):
        raise ValueError('UTC timestamp required')
    return result


def _names(values, maximum=32):
    return (isinstance(values, list) and 1 <= len(values) <= maximum
            and all(isinstance(v, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', v) for v in values)
            and len(set(values)) == len(values))


def _chain_text(value, maximum=128):
    return (isinstance(value, str) and 1 <= len(value) <= maximum
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:_.-]*', value) is not None)


def _valid_mods(value):
    return (isinstance(value, dict) and 1 <= len(value) <= 1024
            and all(type(name) is str and type(version) is str
                    and 1 <= len(name) <= 128 and 1 <= len(version) <= 128
                    and all(32 <= ord(char) <= 126 for char in name + version)
                    for name, version in value.items()))


def _retains_prefix(before, after):
    """Retain paid checkpoint identities while allowing append-only construction."""
    if isinstance(before, dict):
        return isinstance(after, dict) and all(
            key in after and _retains_prefix(value, after[key]) for key, value in before.items())
    if isinstance(before, list):
        return isinstance(after, list) and len(after) >= len(before) and all(
            _retains_prefix(value, after[index]) for index, value in enumerate(before))
    return (before is None or (type(before) is int and before == 0)
            or before == '' or before == after)


def validate_trial(trial: dict) -> None:
    if not isinstance(trial, dict) or trial.get('schema') not in {TRIAL_SCHEMA, TRIAL_SCHEMA_V2, TRIAL_SCHEMA_V3}:
        raise ValueError('Invalid integration trial schema')
    complete = trial['schema'] in {TRIAL_SCHEMA_V2, TRIAL_SCHEMA_V3}
    if set(trial) != TRIAL_KEYS | ({'coal_targets', 'treatment_sha256', 'initial_checkpoint_sha256',
                                   'vm_uuid', 'production_vm_uuid'} if complete else set()) | (
                                       {'downstream_chain'} if trial['schema'] == TRIAL_SCHEMA_V3 else set()):
        raise ValueError('Invalid integration trial fields')
    if (trial['evidence_kind'] not in {'fixture', 'native_isolated', 'native_campaign'}
            or trial['arm'] not in {'baseline', 'treatment'}
            or trial['comparison_axis'] not in {'algorithm', 'capacity', 'unmatched'}):
        raise ValueError('Invalid evidence or comparison class')
    for key in ('experiment_sha256', 'workload_sha256', 'initial_save_sha256',
                'capacity_profile_sha256', 'expected_source_sha256'):
        if not _digest(trial[key]):
            raise ValueError('Invalid trial digest')
    if not _digest(trial['expected_commit'], 40):
        raise ValueError('Invalid source revision')
    configuration = trial['configuration']
    economic = complete and isinstance(configuration, dict) and 'coal_economic_admission' in configuration
    flags = FLAGS | (COAL_FLAGS if complete else set()) | ({'coal_economic_admission'} if economic else set())
    if (not isinstance(configuration, dict) or set(configuration) != flags | {'factory_scheduling'}
            or configuration['factory_scheduling'] != 'ready-work'
            or any(type(configuration[k]) is not bool for k in flags)
            or configuration['solid_routes'] is not True
            or complete and configuration['coal_supply'] is not True
            or economic and configuration['coal_economic_admission'] and not configuration['coal_kit_policy']
            or configuration['furnace_input_belts'] and not configuration['furnace_output_buffers']
            or configuration['ore_side_successors'] and (
                not configuration['background_work'] or not configuration['furnace_input_belts']
                or configuration['mining_outposts'])):
        raise ValueError('Explicit ready-work solid-route configuration required')
    campaign = trial['campaign_treatment']
    campaign_flags = {'lead_time_supply', 'coverage_margin_lookahead',
                      'profile_observations', 'consolidated_observations'}
    if campaign is not None and (
            not isinstance(campaign, dict) or set(campaign) != campaign_flags | {'schema'}
            or not _integer(campaign['schema'], 1, 1)
            or any(type(campaign[k]) is not bool for k in campaign_flags)):
        raise ValueError('Invalid explicit campaign treatment')
    for key in ('requested_model', 'resolved_model'):
        value = trial[key]
        if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError('Explicit predeclared model identity required')
    validate_intents(trial['solid_intents'])
    if complete:
        if not _digest(trial['initial_checkpoint_sha256']):
            raise ValueError('Invalid predeclared checkpoint digest')
        if any(not isinstance(trial[key], str) or not 0 < len(trial[key]) <= 128
               for key in ('vm_uuid', 'production_vm_uuid')):
            raise ValueError('Invalid predeclared VM identity')
        coal_supply.validate_transport_intents(coal_supply.validate_targets(trial['coal_targets']),
                                               trial['solid_intents'])
        binding = {'schema': treatment.SCHEMA_V2 if economic else treatment.SCHEMA,
                   'solid_intents': trial['solid_intents'],
                   'coal_targets': trial['coal_targets'],
                   'solid_science_policy': configuration['solid_science_policy'],
                   'coal_kit_policy': configuration['coal_kit_policy']}
        if economic:
            binding['coal_economic_admission'] = configuration['coal_economic_admission']
        if trial['treatment_sha256'] != treatment.digest(binding):
            raise ValueError('Integration trial treatment digest mismatch')
    for key in ('declared_at_utc', 'original_cutoff_utc', 'runtime_cutoff_utc'):
        _utc(trial[key])
    bounds = {'minimum_window_seconds': (1800, 86400),
              'max_observation_gap_seconds': (1, 120),
              'max_no_science_progress_seconds': (1, 600),
              'minimum_timing_samples': (2, 50000)}
    for key, (lo, hi) in bounds.items():
        if not _integer(trial[key], lo, hi):
            raise ValueError('Invalid predeclared observation or timing bound')
    if (not _names(trial['science_packs'], len(SCIENCE)) or not set(trial['science_packs']) <= SCIENCE
            or not _names([trial['research_goal']], 1) or not _names(trial['downstream_recipes'])):
        raise ValueError('Invalid predeclared science dependency')
    if trial['schema'] == TRIAL_SCHEMA_V3:
        chains = trial['downstream_chain']
        required = {'route', 'producer_role', 'producer_recipe', 'product_item', 'consumer_role',
                    'consumer_unit', 'science_pack'}
        if (not isinstance(chains, list) or not 1 <= len(chains) <= 16
                or any(not isinstance(chain, dict) or set(chain) != required
                       or any(not _chain_text(chain[key]) for key in
                              ('producer_role', 'producer_recipe', 'product_item', 'consumer_role', 'science_pack'))
                       or not _chain_text(chain['route'], 256)
                       or not _integer(chain['consumer_unit'], 1)
                       or chain['producer_recipe'] not in trial['downstream_recipes']
                       or chain['science_pack'] not in trial['science_packs']
                       for chain in chains)
                or len({chain['route'] for chain in chains}) != len(chains)):
            raise ValueError('Invalid predeclared downstream chain diagnostic')
    limits = trial['regression_limits']
    if (not isinstance(limits, dict) or set(limits) != {'max_iteration_p95_ratio', 'min_science_rate_ratio'}
            or not _number(limits['max_iteration_p95_ratio'], 0.01, 10)
            or not _number(limits['min_science_rate_ratio'], 0, 10)):
        raise ValueError('Invalid predeclared regression limits')


def _checkpoint(raw):
    """Validate captured bytes, not a second read of a possibly changed input path."""
    value = load_json(raw)
    if not isinstance(value, dict):
        raise ValueError('Invalid checkpoint')
    with tempfile.TemporaryDirectory(prefix='jev-evidence-check-') as directory:
        path = Path(directory) / 'checkpoint.json'
        write_new(path, canonical(value))
        load_checkpoint(path, value.get('session_id'), 'rocket_launch')
    return value


def _counter(value):
    if (not isinstance(value, dict) or len(value) > 4096
            or any(not isinstance(k, str) or len(k) > 128 or not _number(v) for k, v in value.items())):
        raise ValueError('Invalid bounded counter map')
    return value


def _failure_counter(value: object) -> dict:
    data = _counter(value)
    if any(not _integer(v) for v in data.values()):
        raise ValueError('Failure budgets must be nonnegative integers')
    return data


def _route_binding(row):
    return {key: deepcopy(row[key]) for key in ('route', 'layout', 'item', 'source', 'target', 'steps')}


def _identity(row):
    return {key: row.get(key) for key in ('session_id', 'world_kind', 'process_id', 'execution_id',
             'run_id', 'segment_id', 'policy', 'target', 'requested_model', 'code_revision',
             'acceptance_configuration', 'campaign_treatment')}


def _receipt_stock_consumption(chain, rows, receipts):
    """Correlate one new paid insert with fresh stock and later recipe work.

    Items are fungible, so even this strict sequence cannot certify that the
    inserted units were the units consumed. Never promote it to acceptance.
    """
    item = chain['product_item']
    role, unit = chain['consumer_role'], chain['consumer_unit']
    producer_role = chain['producer_role']
    observations = []
    for row in rows:
        for label in ('state', 'after_state'):
            state = row.get(label)
            factory = state.get('factory') if isinstance(state, dict) else None
            entities = factory.get('entities') if isinstance(factory, dict) else None
            entity = entities.get(role) if isinstance(entities, dict) else None
            all_receipts = factory.get('receipts') if isinstance(factory, dict) else None
            stock = entity.get('input') if isinstance(entity, dict) else None
            if (not isinstance(state, dict) or not _integer(state.get('tick'))
                    or not isinstance(entity, dict) or entity.get('unit_number') != unit
                    or entity.get('recipe') != chain['science_pack']
                    or not isinstance(stock, dict) or len(stock) > 128
                    or any(not _chain_text(name) or not _integer(amount, 1, 1000000)
                           for name, amount in stock.items())
                    or not _integer(entity.get('products_finished'))
                    or not isinstance(all_receipts, dict)):
                return 'stock_observation_unqualified'
            observations.append((state['tick'], stock.get(item, 0),
                                 entity['products_finished'], all_receipts))
    inserts = [(key, value) for key, value in receipts.items()
               if isinstance(value, dict) and value.get('role') == role
               and value.get('unit_number') == unit and value.get('item') == item
               and value.get('extracting') is False]
    extracts = [(key, value) for key, value in receipts.items()
                if isinstance(value, dict) and value.get('role') == producer_role
                and value.get('item') == item and value.get('extracting') is True]
    if len(inserts) != 1 or len(extracts) != 1:
        return 'unique_paid_transfer_missing'
    insert_id, insert = inserts[0]
    _, extract = extracts[0]
    quantity = insert.get('quantity')
    if (not _integer(quantity, 1, 200) or extract.get('quantity') != quantity
            or not _integer(insert.get('tick')) or not _integer(extract.get('tick'))
            or extract['tick'] >= insert['tick']):
        return 'paid_transfer_mismatch'
    first = next((index for index, (_, _, _, seen) in enumerate(observations)
                  if insert_id in seen), None)
    if first is None or first == 0:
        return 'fresh_receipt_boundary_missing'
    before = observations[first - 1]
    after = observations[first]
    if (any(insert_id in seen for _, _, _, seen in observations[:first])
            or after[3].get(insert_id) != insert
            or before[0] > insert['tick'] or after[0] < insert['tick']
            or before[1] != 0 or after[1] != quantity
            or after[2] != before[2]):
        return 'receipt_stock_boundary_mismatch'
    for tick, stock, completed, seen in observations[first + 1:]:
        if (completed < after[2] or stock > quantity
                or seen.get(insert_id) != insert):
            return 'later_stock_or_receipt_ambiguous'
        if tick <= after[0]:
            continue
        if stock < quantity and completed == after[2]:
            return 'stock_drawdown_without_recipe_work'
        if completed > after[2] and stock == quantity:
            return 'recipe_work_without_stock_drawdown'
        if completed > after[2] and stock < quantity:
            return 'paid_insert_stock_and_recipe_work_correlated'
    return 'later_recipe_consumption_not_observed'


def _recipe_witness_route(route):
    """Project a captured owned route into the strict witness route contract."""
    if not isinstance(route, dict):
        return None
    source, target, steps = route.get('source'), route.get('target'), route.get('steps')
    if (not isinstance(source, dict) or not isinstance(target, dict)
            or not isinstance(steps, list) or not 1 <= len(steps) <= 128
            or not _chain_text(route.get('route'), 256) or not _chain_text(route.get('item'))
            or not _chain_text(source.get('role')) or not _integer(source.get('unit_number'), 1)
            or not _chain_text(target.get('role')) or not _integer(target.get('unit_number'), 1)):
        return None
    return {'id': route['route'], 'item': route['item'],
            'source_role': source['role'], 'source_unit': source['unit_number'],
            'target_role': target['role'], 'target_unit': target['unit_number'],
            'paid_parts': len(steps)}


def _recipe_witness_status(chain, record, bundle):
    """Bind one read-only recipe witness to the declared chain and captured state."""
    if not isinstance(bundle, dict):
        return 'recipe_dependency_witness_unqualified'
    result = bundle.get('result')
    epoch = result.get('epoch') if isinstance(result, dict) else None
    if (not isinstance(epoch, dict) or not _integer(epoch.get('tick'), 0)
            or result.get('status') != 'observed'
            or result.get('recipe_dependency_verified') is not True):
        return 'recipe_dependency_query_unqualified'

    candidates = []
    for label in ('state', 'after_state'):
        state = record.get(label) if isinstance(record, dict) else None
        if not isinstance(state, dict) or not _integer(state.get('tick')):
            continue
        if abs(state['tick'] - epoch['tick']) > 120:
            continue
        factory = state.get('factory')
        if not isinstance(factory, dict):
            continue
        runtime = factory.get('acceptance_runtime')
        routes = factory.get('solid_routes', {}).get('routes') if isinstance(
            factory.get('solid_routes'), dict) else None
        route = routes.get(chain['route']) if isinstance(routes, dict) else None
        route_binding = _recipe_witness_route(route)
        entities = factory.get('entities')
        producer = entities.get(chain['producer_role']) if isinstance(entities, dict) else None
        consumer = entities.get(chain['consumer_role']) if isinstance(entities, dict) else None
        if (not isinstance(runtime, dict) or not isinstance(consumer, dict)
                or not _integer(runtime.get('schema'), 1, 1)
                or runtime.get('session_id') != state['session_id']
                or route_binding is None
                or not isinstance(producer, dict)
                or producer.get('unit_number') != route_binding['target_unit']
                or producer.get('recipe') != chain['producer_recipe']
                or not isinstance(state.get('session_id'), str)
                or consumer.get('unit_number') != chain['consumer_unit']
                or consumer.get('recipe') != chain['science_pack']
                or route_binding['id'] != chain['route']
                or route_binding['target_role'] != chain['producer_role']
                or route is None or route.get('target', {}).get('recipe') != chain['producer_recipe']
                or route.get('target', {}).get('inventory') != 'input'):
            continue
        identity = {'session_id': state['session_id'],
                    'actor_index': runtime.get('player_index'),
                    'actor_unit': runtime.get('actor_unit'),
                    'surface_index': runtime.get('surface_index'),
                    'force_index': runtime.get('force_index')}
        if any(not _integer(identity[key], 1) for key in
               ('actor_index', 'actor_unit', 'surface_index', 'force_index')):
            continue
        candidates.append((state['tick'], identity, route_binding))
    if not candidates:
        return 'recipe_dependency_observation_not_bound'
    # Both sides of one gameplay record may have the same tick. Distinct route
    # or actor bindings at that tick are ambiguous and must not be combined.
    if len({(tuple(sorted(identity.items())), tuple(sorted(route.items())))
            for _, identity, route in candidates}) != 1:
        return 'recipe_dependency_observation_ambiguous'
    observation_tick, identity, expected_route = min(
        candidates, key=lambda value: abs(value[0] - epoch['tick']))
    request = {'route': chain['route'], 'producer_role': chain['producer_role'],
               'producer_unit': expected_route['target_unit'],
               'product_item': chain['product_item'],
               'consumer_role': chain['consumer_role'],
               'consumer_unit': chain['consumer_unit'],
               'science_pack': chain['science_pack']}
    expected_epoch = bundle.get('expected_epoch')
    if (not isinstance(expected_epoch, dict)
            or any(expected_epoch.get(key) != value for key, value in identity.items())
            or not _integer(expected_epoch.get('min_tick'), 0)
            or not _integer(expected_epoch.get('max_tick'), 0)
            or expected_epoch['min_tick'] > epoch['tick']
            or expected_epoch['max_tick'] < epoch['tick']
            or expected_epoch['max_tick'] - expected_epoch['min_tick'] > 120
            or expected_epoch['min_tick'] < observation_tick - 120
            or expected_epoch['max_tick'] > observation_tick + 120):
        return 'recipe_dependency_capture_binding_mismatch'
    try:
        decoded = downstream_recipe_witness.decode_capture_bundle(
            bundle, request=request, expected_epoch=expected_epoch,
            expected_route=expected_route)
    except (ValueError, RuntimeError, KeyError, TypeError, AttributeError):
        return 'recipe_dependency_capture_binding_mismatch'
    if (decoded.get('status') != 'observed'
            or decoded.get('recipe_dependency_verified') is not True):
        return 'recipe_dependency_query_unqualified'
    return 'recipe_dependency_observed'


def _recipe_witness_statuses(chains, rows):
    """Consume at most one private witness bundle per predeclared route."""
    by_route = {chain['route']: [] for chain in chains}
    malformed = set()
    total = 0
    for row_index, record in enumerate(rows):
        if not isinstance(record, dict):
            malformed.add(row_index)
            continue
        if 'recipe_dependency_witnesses' not in record:
            continue
        captures = record['recipe_dependency_witnesses']
        if not isinstance(captures, list) or len(captures) > 16:
            malformed.add(row_index)
            continue
        total += len(captures)
        for bundle in captures:
            request = bundle.get('request') if isinstance(bundle, dict) else None
            route = request.get('route') if isinstance(request, dict) else None
            if not isinstance(route, str) or route not in by_route:
                # An unassociated member could hide or conflict with a declared
                # route's witness, so no positive conclusion may survive it.
                malformed.add(row_index)
                continue
            by_route[route].append((record, bundle))
    if total > 2 * len(chains):
        return ['recipe_dependency_witness_budget_exceeded'] * len(chains)

    result = []
    for chain in chains:
        candidates = by_route[chain['route']]
        if len(candidates) > 1:
            result.append('recipe_dependency_witness_ambiguous')
        elif not candidates:
            result.append('recipe_dependency_witness_missing')
        else:
            record, bundle = candidates[0]
            result.append(_recipe_witness_status(chain, record, bundle))
    if malformed and result:
        # A malformed optional list/member cannot be associated with a declared
        # route; it may hide a duplicate even when another record is valid.
        result = ['recipe_dependency_witness_unqualified'
                  if status in {'recipe_dependency_witness_missing',
                                'recipe_dependency_observed'} else status
                  for status in result]
    return result


def _downstream_chain_diagnostic(chains, rows, routes, receipts, lab_unit, integrity_valid=True):
    """Report bounded paid-transfer correlation, never provenance or acceptance.

    A transfer receipt identifies one entity and the actor, not the source of
    the transferred stock. In particular, a matching extract/insert pair cannot
    establish that the routed ingredient became the later science pack.
    """
    if chains is None:
        return None
    if (not integrity_valid or not isinstance(rows, list) or not rows
            or not isinstance(routes, dict) or not isinstance(receipts, dict)):
        return {'schema': 'jev-factorio.downstream-chain-diagnostic.v1',
                'predeclared_chains': len(chains),
                'status_by_declared_order': ['input_integrity_failed'] * len(chains),
                'correlated_sequences': 0, 'intermediate_provenance_qualified': False,
                'receipt_stock_recipe_status_by_declared_order': [
                    'input_integrity_failed'] * len(chains),
                'receipt_stock_recipe_correlations': 0,
                'recipe_dependency_status_by_declared_order': [
                    'input_integrity_failed'] * len(chains),
                'current_recipe_dependency_matches': 0,
                'receipt_stock_recipe_dependency_correlations': 0,
                'composed_status_by_declared_order': ['input_integrity_failed'] * len(chains),
                'scope': 'Analyzer integrity checks failed; no chain diagnostic qualified.'}
    observed = []
    def after_factory(row):
        state = row.get('after_state') if isinstance(row, dict) else None
        factory = state.get('factory') if isinstance(state, dict) else None
        return factory if isinstance(factory, dict) else {}

    events = sorted((receipt for receipt in receipts.values() if isinstance(receipt, dict)
                     and _integer(receipt.get('tick')) and _integer(receipt.get('quantity'), 1, 200)),
                    key=lambda receipt: receipt['tick'])
    for chain in chains:
        route = routes.get(chain['route'])
        if (not isinstance(route, dict) or route.get('kind') != 'downstream'
                or not _integer(route.get('target_unit'), 1) or not _chain_text(route.get('recipe'))
                or not _integer(route.get('attributed_positive_boundaries'), 3)
                or not _integer(route.get('attributed_received'), 1)
                or not _integer(route.get('first_positive_tick'))
                or not _integer(route.get('target_first_products'))
                or not _integer(route.get('target_last_products'))
                or route['target_last_products'] <= route['target_first_products']):
            observed.append('routed_producer_not_qualified')
            continue
        observed_routes = []
        for row in rows:
            solid = after_factory(row).get('solid_routes')
            mapping = solid.get('routes') if isinstance(solid, dict) else None
            if isinstance(mapping, dict) and chain['route'] in mapping:
                observed_routes.append(mapping[chain['route']])
        if (len(observed_routes) != len(rows)
                or any(not isinstance(value, dict) or not isinstance(value.get('target'), dict)
                       or value['target'].get('role') != chain['producer_role']
                       or value['target'].get('unit_number') != route['target_unit']
                       for value in observed_routes)
                or route['recipe'] != chain['producer_recipe']
                or route['recipe'] == chain['science_pack']):
            observed.append('producer_binding_mismatch')
            continue
        consumers = []
        for row in rows:
            entities = after_factory(row).get('entities')
            consumers.append(entities.get(chain['consumer_role']) if isinstance(entities, dict) else None)
        if (any(not isinstance(entity, dict) or entity.get('unit_number') != chain['consumer_unit']
                or entity.get('recipe') != chain['science_pack']
                or not _integer(entity.get('products_finished')) for entity in consumers)
                or consumers[-1]['products_finished'] <= consumers[0]['products_finished']):
            observed.append('owned_science_consumer_not_qualified')
            continue
        product_extracts = {}
        product_inserted_tick = None
        science_extracts = {}
        complete = False
        for receipt in events:
            tick, quantity = receipt['tick'], receipt['quantity']
            role, unit, item, extracting = (receipt.get('role'), receipt.get('unit_number'),
                                             receipt.get('item'), receipt.get('extracting'))
            if tick < route['first_positive_tick'] or type(extracting) is not bool:
                continue
            if (role == chain['producer_role'] and unit == route['target_unit']
                    and item == chain['product_item'] and extracting):
                product_extracts.setdefault(quantity, tick)
            elif (role == chain['consumer_role'] and unit == chain['consumer_unit']
                    and item == chain['product_item'] and not extracting
                    and quantity in product_extracts and tick > product_extracts[quantity]):
                product_inserted_tick = tick
            elif (product_inserted_tick is not None and tick > product_inserted_tick
                  and role == chain['consumer_role'] and unit == chain['consumer_unit']
                  and item == chain['science_pack'] and extracting):
                science_extracts.setdefault(quantity, tick)
            elif (role == 'utility:lab' and unit == lab_unit and item == chain['science_pack']
                  and not extracting and quantity in science_extracts
                  and tick > science_extracts[quantity]):
                complete = True
                break
        observed.append('correlated_paid_transfer_sequence' if complete else 'paid_transfer_sequence_missing')
    stock_sequences = [
        _receipt_stock_consumption(chain, rows, receipts)
        if status == 'correlated_paid_transfer_sequence' else 'chain_not_qualified'
        for chain, status in zip(chains, observed)
    ]
    recipe_statuses = _recipe_witness_statuses(chains, rows)
    composed = []
    for sequence, stock, recipe in zip(observed, stock_sequences, recipe_statuses):
        if (sequence == 'correlated_paid_transfer_sequence'
                and stock == 'paid_insert_stock_and_recipe_work_correlated'
                and recipe == 'recipe_dependency_observed'):
            composed.append('paid_stock_recipe_dependency_correlated')
        elif recipe == 'recipe_dependency_observed':
            composed.append('recipe_dependency_only')
        elif stock == 'paid_insert_stock_and_recipe_work_correlated':
            composed.append('receipt_stock_recipe_only')
        else:
            composed.append('not_composed')
    return {'schema': 'jev-factorio.downstream-chain-diagnostic.v1',
            'predeclared_chains': len(chains), 'status_by_declared_order': observed,
            'correlated_sequences': observed.count('correlated_paid_transfer_sequence'),
            'receipt_stock_recipe_status_by_declared_order': stock_sequences,
            'receipt_stock_recipe_correlations': stock_sequences.count(
                'paid_insert_stock_and_recipe_work_correlated'),
            'recipe_dependency_status_by_declared_order': recipe_statuses,
            'current_recipe_dependency_matches': recipe_statuses.count(
                'recipe_dependency_observed'),
            'receipt_stock_recipe_dependency_correlations': composed.count(
                'paid_stock_recipe_dependency_correlated'),
            'composed_status_by_declared_order': composed,
            'intermediate_provenance_qualified': False,
            'scope': ('A bound current recipe edge may align with separate paid-transfer, stock, and recipe-counter '
                      'observations. This remains correlation only: exact-unit provenance, causal science use, '
                      'evidence authenticity, and native acceptance are not proven.')}


def analyze_rows(rows: list[dict], trial: dict, initial: dict, final: dict) -> dict:
    """Check internal consistency. A passing result is never acceptance or authority.

    Window accounting starts at the first *after_state*, timestamped by the first
    complete record. Earlier work in its state is excluded, rather than borrowing
    unobserved wall time or pre-window route/receipt counters.
    """
    validate_trial(trial)
    if not isinstance(rows, list) or not 2 <= len(rows) <= 50000:
        raise ValueError('A bounded multi-observation stream is required')
    if not all(isinstance(v, dict) for v in (initial, final)):
        raise ValueError('Invalid checkpoint evidence')
    # Every public analysis path uses the complete composed checkpoint parser.
    # Validation runs against an owned captured copy, never the live input path.
    initial = _checkpoint(canonical(initial))
    final = _checkpoint(canonical(final))
    issues = set()
    def reject(condition, code):
        if condition:
            issues.add(code)
    # Evidence kind is preserved, never promoted by a consistency result.
    reject(_utc(trial['original_cutoff_utc']) != _utc(trial['runtime_cutoff_utc']), 'original_cutoff_changed')
    session = initial.get('session_id')
    reject(not isinstance(session, str) or not session or final.get('session_id') != session, 'checkpoint_session_mismatch')
    reject(any(final.get(k) for k in ('pending', 'attempt', 'active_plan', 'reservations',
                                     'background_job', 'background_attempt')), 'unresolved_final_work')
    reject(final.get('active_plan') is None and final.get('step_index') != 0,
           'final_step_index_not_cleared')
    reject(initial.get('status') != 'running', 'initial_checkpoint_not_running')
    reject(final.get('status') not in {'running', 'completed'}, 'terminal_failure')
    extensions = {'background_work': 'background_schema',
                  'furnace_output_buffers': 'output_buffers_schema',
                  'furnace_input_belts': 'input_routes_schema',
                  'mining_outposts': 'outposts_schema', 'ore_side_successors': 'successor_schema'}
    for cp in (initial, final):
        reject(cp.get('solid_intents') != trial['solid_intents']
               or 'solid_science_policy' not in cp or type(cp.get('solid_science_policy')) is not bool
               or cp['solid_science_policy'] is not trial['configuration']['solid_science_policy'],
               'checkpoint_treatment_mismatch')
        if trial['schema'] in {TRIAL_SCHEMA_V2, TRIAL_SCHEMA_V3}:
            reject(cp.get('coal_targets') != trial['coal_targets']
                   or cp.get('coal_kit_policy') is not trial['configuration']['coal_kit_policy']
                   or cp.get('coal_supply_schema') != (2 if trial['configuration'].get('coal_economic_admission', False) else 1)
                   or cp.get('coal_economic_admission', False) is not trial['configuration'].get('coal_economic_admission', False)
                   or not isinstance(cp.get('coal_commitments'), dict)
                   or not isinstance(cp.get('coal_epoch'), dict) or not cp['coal_epoch']
                   or cp.get('coal_epoch') != cp.get('solid_epoch'),
                   'checkpoint_coal_treatment_mismatch')
        reject(any((field in cp) is not trial['configuration'][flag] for flag, field in extensions.items()),
               'checkpoint_composition_mismatch')
    for field in ('output_commitments', 'input_commitments', 'outpost_commitments', 'successor_receipts'):
        reject(not _retains_prefix(initial.get(field, {}), final.get(field, {})),
               'composed_ownership_regressed')
    if trial['schema'] in {TRIAL_SCHEMA_V2, TRIAL_SCHEMA_V3}:
        reject(not _retains_prefix(initial.get('coal_commitments', {}),
                                   final.get('coal_commitments', {})),
               'coal_ownership_regressed')
    for key, before in initial.get('successor_projects', {}).items():
        after = final.get('successor_projects', {}).get(key)
        reject(not isinstance(after, dict) or any(
            before.get(name) != after.get(name) for name in
            ('anchor', 'predecessor_unit', 'started_tick', 'deadline_tick'))
            or before.get('source_unit') not in (0, after.get('source_unit'))
            or before.get('status') == 'qualified' and after.get('status') != 'qualified',
            'composed_ownership_regressed')
    seen, previous_identity, runtime_identity = set(), None, None
    evidence_hash = hashlib.sha256()
    previous_tick = previous_time = None
    first_time = first_tick = last_time = last_tick = None
    received_receipts, receipt_values, window_receipts = set(), {}, {}
    deliveries, consumptions = Counter(), Counter()
    first_consumed = previous_consumed = observed_consumed = None
    observed_research = set()
    observed_progress = {}
    observed_flows, pending_routes = {}, {}
    previous_failures = _failure_counter(initial.get('failures', {}))
    previous_goals = deepcopy(initial.get('completed_goals', {}))
    previous_capital = deepcopy(initial.get('capital_investment'))
    final_failures = _failure_counter(final.get('failures', {}))
    route_history, route_statistics, observed_products = {}, {}, {}
    committed = deepcopy(initial.get('solid_commitments', {}))
    researched_before = set()
    progress_highwater = {}
    last_progress_time = None
    longest_stall = longest_gap = 0.0
    baseline_goal_complete = False
    goal_completed = False
    resolved_models = set()
    model_calls = 0
    terminal_seen = False
    last_timing_index = None
    timing_samples = defaultdict(list)
    timing_counts = Counter()
    incomplete_timings = boundary_timings = 0
    # Opaque backend profiles are not presumed CPU or transport measurements.
    initial_lab_unit = None
    allowed_intents = {(v['source'], v['target'], v['item'], v['destination']) for v in trial['solid_intents']}
    for index, record in enumerate(rows):
        if not isinstance(record, dict):
            raise ValueError('Invalid gameplay record')
        digest = sha256(canonical(record))
        if digest in seen:
            raise ValueError('Duplicate gameplay record')
        seen.add(digest)
        evidence_hash.update(digest.encode('ascii') + b'\n')
        identity = _identity(record)
        if previous_identity is not None:
            reject(identity != previous_identity, 'mixed_invocation_or_treatment')
        previous_identity = identity
        reject(any(not isinstance(record.get(k), str) or not 0 < len(record[k]) <= 128
                   for k in ('session_id', 'process_id', 'execution_id', 'run_id', 'segment_id')), 'invocation_identity_missing')
        reject(record.get('session_id') != session or record.get('world_kind') != 'fle', 'non_native_or_mixed_session')
        reject(record.get('controller') != 'hierarchical' or record.get('target') != 'rocket_launch', 'unexpected_controller')
        revision = record.get('code_revision', {})
        reject(not isinstance(revision, dict) or revision.get('commit') != trial['expected_commit']
               or revision.get('source_sha256') != trial['expected_source_sha256']
               or ('dirty' in revision and revision['dirty'] is not False), 'source_readback_mismatch')
        reject(record.get('acceptance_configuration') != trial['configuration']
               or record.get('campaign_treatment') != trial['campaign_treatment'], 'configuration_mismatch')
        if trial['configuration'].get('coal_economic_admission', False):
            reject(record.get('coal_economic_admission') is not True
                   or not isinstance(record.get('coal_admission_evidence'), dict),
                   'coal_economic_evidence_mismatch')
            for label in ('state', 'after_state'):
                state = record.get(label)
                try:
                    if (not isinstance(state, dict)
                            or state['factory']['coal_supply']['protocol'] != 2):
                        raise ValueError('Missing coal economic protocol')
                    coal_supply.sources(SimpleNamespace(tick=state['tick'],
                        session_id=state['session_id'], factory=state['factory']))
                except (ValueError, KeyError, TypeError, AttributeError):
                    issues.add('coal_economic_native_evidence_invalid')
        reject(record.get('requested_model') != trial['requested_model'], 'requested_model_mismatch')
        reject(type(record.get('model_call')) is not bool, 'invalid_model_call_flag')
        decision = record.get('decision')
        reject(decision is not None and not isinstance(decision, dict)
               or isinstance(decision, dict) and type(decision.get('model_called')) is not bool
               or record.get('model_call') is not (decision.get('model_called') if isinstance(decision, dict) else False),
               'decision_model_call_mismatch')
        reject(type(record.get('status')) is not str or record.get('status') not in {'running', 'completed'}
               or record.get('solid_route_fault') is not False
               or trial['schema'] in {TRIAL_SCHEMA_V2, TRIAL_SCHEMA_V3}
               and record.get('coal_supply_fault') is not False,
               'controller_or_route_failure')
        reject(terminal_seen, 'records_after_terminal_completion')
        terminal_seen = record.get('status') == 'completed'
        reject(record.get('pending') is not None or record.get('attempt') is not None
               or initial.get('pending') is not None or initial.get('attempt') is not None,
               'pending_attempt_requires_native_reconciliation')
        reject(record.get('policy') not in {'hybrid', 'jev'}, 'actual_jev_policy_not_demonstrated')
        if index and record.get('model_call') is True:
            model_calls += 1
            model = record.get('resolved_model')
            if model != trial['resolved_model'] or not isinstance(model, str) or not 0 < len(model) <= 256:
                issues.add('resolved_model_missing_or_mismatched')
            else:
                resolved_models.add(model)
        now = _utc(record.get('recorded_at_utc'))
        reject(now > _utc(trial['runtime_cutoff_utc']), 'evidence_after_original_cutoff')
        if first_time is None:
            first_time = now
            last_progress_time = now
            reject(_utc(trial['declared_at_utc']) >= now, 'experiment_not_predeclared')
        if previous_time is not None:
            gap = (now - previous_time).total_seconds()
            reject(gap <= 0, 'record_wall_time_not_increasing')
            longest_gap = max(longest_gap, gap)
        previous_time, last_time = now, now
        budgets = _failure_counter(record.get('failure_budgets'))
        reject(any(budgets.get(k, -1) < v for k, v in previous_failures.items()), 'failure_history_regressed')
        previous_failures = budgets
        goals = record.get('completed_goals')
        if not isinstance(goals, dict) or len(goals) > 64 or any(
                not isinstance(k, str) or not _integer(v) for k, v in goals.items()):
            raise ValueError('Invalid completed goal history')
        reject(any(goals.get(k) != v or k not in goals for k, v in previous_goals.items()),
               'completed_goal_history_regressed')
        previous_goals = deepcopy(goals)
        reject(record.get('status') == 'completed' and 'rocket_launch' not in goals,
               'completed_target_history_missing')
        current_capital = record.get('capital_investment')
        if current_capital is not None:
            capital.validate_state(current_capital, record['after_state']['tick'])
        history = record.get('history', [])
        if not isinstance(history, list) or any(not isinstance(event, dict) for event in history):
            raise ValueError('Invalid capital event history')
        if previous_capital is not None:
            spec = previous_capital['spec']
            entity = record['after_state']['factory'].get('entities', {}).get(spec['role'], {})
            unit = previous_capital['unit_number']
            reject(unit is not None and (not isinstance(entity, dict)
                   or entity.get('unit_number') != unit), 'capital_paid_ownership_regressed')
            if current_capital is None:
                completed_event = any(event.get('kind') == 'capital_completed'
                    and event.get('key') == spec['key'] and event.get('unit_number') == unit
                    for event in history)
                abandoned_event = any(event.get('kind') == 'capital_abandoned'
                    and event.get('key') == spec['key'] for event in history)
                produced = (isinstance(entity, dict) and _integer(entity.get('products_finished'))
                    and previous_capital['products_baseline'] is not None
                    and entity['products_finished'] > previous_capital['products_baseline']
                    and _number(entity.get('output', {}).get(spec['item']), 0.000001))
                reject(not (completed_event and produced or abandoned_event and budgets.get(spec['key'], 0) >= 2),
                       'capital_reconciliation_missing')
            else:
                reject(any(previous_capital[k] != current_capital[k] for k in
                       ('spec', 'started_tick', 'deadline_tick'))
                       or previous_capital['unit_number'] not in (None, current_capital['unit_number'])
                       or previous_capital['products_baseline'] not in (None, current_capital['products_baseline']),
                       'capital_paid_ownership_regressed')
        elif current_capital is not None:
            reject(not any(event.get('kind') == 'capital_committed'
                           and event.get('spec') == current_capital['spec'] for event in history),
                   'capital_commitment_unlogged')
        previous_capital = deepcopy(current_capital)
        # Check every before/after state, not just endpoints or unique ticks.
        for label in ('state', 'after_state'):
            state = record.get(label)
            if not isinstance(state, dict) or not isinstance(state.get('factory'), dict) or not _integer(state.get('tick')):
                raise ValueError('Missing complete native observation')
            tick = state['tick']
            factory = state['factory']
            if not isinstance(factory.get('entities'), dict) or len(factory['entities']) > 4096:
                raise ValueError('Missing bounded owned entity map')
            reject(state.get('session_id') != session or state.get('world_kind') != 'fle', 'observation_session_mismatch')
            reject(factory.get('tick') != tick or type(factory.get('tick')) is not int, 'incoherent_native_tick')
            reject(previous_tick is not None and tick < previous_tick, 'native_tick_regressed')
            previous_tick = tick
            runtime = factory.get('acceptance_runtime')
            if not isinstance(runtime, dict):
                issues.add('native_runtime_missing')
            else:
                reject(not _integer(runtime.get('schema'), 1, 1), 'native_runtime_schema_mismatch')
                bound = {k: runtime.get(k) for k in ('session_id', 'actor_unit', 'player_index', 'surface_index', 'force_index', 'mods')}
                reject(runtime.get('speed') != 1 or type(runtime.get('speed')) not in (int, float)
                       or runtime.get('tick_paused') is not False, 'simulation_speed_or_pause_changed')
                reject(bound['session_id'] != session or any(not _integer(bound[k], 1) for k in (
                    'actor_unit', 'player_index', 'surface_index', 'force_index'))
                    or not isinstance(bound['mods'], dict) or not bound['mods'], 'invalid_native_epoch')
                reject(not _valid_mods(bound['mods']), 'invalid_native_mods')
                reject(runtime_identity is not None and bound != runtime_identity, 'native_epoch_drift')
                runtime_identity = bound
                epoch = {'actor_index': bound['player_index'], 'surface_index': bound['surface_index'], 'force_index': bound['force_index']}
                reject(any(cp.get('solid_epoch') != epoch for cp in (initial, final)), 'checkpoint_epoch_mismatch')
            view = SimpleNamespace(session_id=session, tick=tick, factory=factory)
            try:
                native_routes = solid_routes.routes(view)
            except (ValueError, KeyError, TypeError, AttributeError):
                issues.add('invalid_solid_observation')
                native_routes = {}
            for key, old in committed.items():
                current = native_routes.get(key)
                reject(current is None or any(current.get(k) != old[k] for k in ('layout', 'source', 'target', 'steps'))
                       or any(current.get('parts', {}).get(k) != v for k, v in old['parts'].items()), 'paid_route_ownership_regressed')
            for key, route in native_routes.items():
                reject(index == 0 and label == 'state' and route['state'] != 'proposed'
                       and key not in initial.get('solid_commitments', {}),
                       'uncheckpointed_initial_solid_commitment')
                reject((route['source']['role'], route['target']['role'], route['item'], route['target']['inventory'])
                       not in allowed_intents, 'unrequested_solid_route')
                reject(not solid_routes.current(route, view), 'stale_or_faulted_route')
                if route['target']['inventory'] != 'fuel':
                    target = factory['entities'].get(route['target']['role'])
                    products = target.get('products_finished') if isinstance(target, dict) else None
                    reject(not _integer(products), 'invalid_downstream_production_counter')
                    previous = observed_products.get(key)
                    reject(previous is not None and _integer(products) and products < previous,
                           'downstream_production_counter_regressed')
                    if _integer(products):
                        observed_products[key] = products
                if route['parts'] or route['pending']:
                    committed[key] = solid_routes.commitment(route)
                pending = pending_routes.get(key)
                if pending:
                    paid = route['parts'].get(pending['part'], {})
                    if route['pending']:
                        order = {'prepared': 0, 'dispatching': 1, 'placed': 2}
                        reject(order[route['pending']['phase']] < order[pending['phase']], 'native_pending_phase_regressed')
                    reject(not (route['pending'] and all(route['pending'][k] == pending[k] for k in ('part', 'receipt')))
                           and paid.get('receipt') != pending['receipt'], 'native_pending_identity_lost')
                pending_routes[key] = deepcopy(route['pending'])
                prior_flow = observed_flows.get(key)
                if prior_flow:
                    flow = route['flow']
                    reject(not flow or flow['first_tick'] != prior_flow['first_tick'], 'route_flow_epoch_changed')
                    reject(bool(flow) and any(flow[k] < prior_flow[k] for k in
                           ('sent', 'received', 'positive_samples', 'last_tick', 'last_positive_tick')),
                           'route_flow_counter_regressed')
                if route['flow']:
                    observed_flows[key] = deepcopy(route['flow'])
            # Check all captured observation boundaries, including the pre-action
            # side of an iteration. A later after-state cannot erase a reset.
            consumed_now = _counter(factory.get('consumed'))
            reject(observed_consumed is not None and any(consumed_now.get(k, -1) < v
                   for k, v in observed_consumed.items()), 'consumption_counter_regressed')
            observed_consumed = dict(consumed_now)
            completed_now = state.get('researched')
            if (not isinstance(completed_now, list) or len(completed_now) > 4096
                    or any(not isinstance(v, str) or len(v) > 128 for v in completed_now)):
                raise ValueError('Invalid research completion evidence')
            reject(not observed_research <= set(completed_now), 'research_completion_regressed')
            observed_research = set(completed_now)
            research_now, progress_now = factory.get('research'), factory.get('research_progress')
            if isinstance(research_now, str) and research_now and _number(progress_now, 0, 1):
                reject(progress_now < observed_progress.get(research_now, 0), 'research_progress_regressed')
                observed_progress[research_now] = progress_now
            receipts_now = factory.get('receipts')
            if not isinstance(receipts_now, dict) or len(receipts_now) > 10000:
                raise ValueError('Missing bounded receipt evidence')
            for receipt_id, receipt in receipts_now.items():
                if not isinstance(receipt_id, str) or len(receipt_id) > 128 or not isinstance(receipt, dict):
                    raise ValueError('Invalid receipt evidence')
                reject(receipt_id in receipt_values and receipt != receipt_values[receipt_id], 'receipt_identity_rewritten')
                receipt_values[receipt_id] = deepcopy(receipt)
            lab = factory.get('entities', {}).get('utility:lab', {})
            unit = lab.get('unit_number') if isinstance(lab, dict) else None
            reject(not _integer(unit, 1) or lab.get('name') != 'lab', 'owned_lab_missing')
            if initial_lab_unit is None:
                initial_lab_unit = unit
            reject(unit != initial_lab_unit, 'owned_lab_replaced')
            counts = factory.get('force_entity_counts', {})
            reject(not isinstance(counts, dict) or not _integer(counts.get('lab'), 1, 1), 'exclusive_owned_lab_not_observed')
            if label != 'after_state':
                continue
            if first_tick is None:
                first_tick = tick
                reject(not _integer(initial.get('last_tick')) or initial['last_tick'] > tick
                       or tick - initial['last_tick'] > trial['max_observation_gap_seconds'] * 60, 'initial_checkpoint_window_mismatch')
            prior_after_tick = last_tick
            last_tick = tick
            receipts = factory.get('receipts')
            if not isinstance(receipts, dict) or len(receipts) > 10000:
                raise ValueError('Missing bounded receipt evidence')
            for receipt_id, receipt in receipts.items():
                if not isinstance(receipt_id, str) or len(receipt_id) > 128 or not isinstance(receipt, dict):
                    raise ValueError('Invalid receipt evidence')
                if receipt_id in receipt_values:
                    reject(receipt != receipt_values[receipt_id], 'receipt_identity_rewritten')
                receipt_values[receipt_id] = deepcopy(receipt)
                if receipt_id not in received_receipts and index > 0 and receipt.get('role') == 'utility:lab':
                    if receipt.get('item') in trial['science_packs'] and receipt.get('extracting') is False:
                        if (not _integer(receipt.get('quantity'), 1, 200)
                                or not _integer(receipt.get('unit_number'), 1) or receipt['unit_number'] != unit
                                or not _integer(receipt.get('tick'))
                                or not first_tick < receipt['tick'] <= tick
                                or prior_after_tick is not None and receipt['tick'] <= prior_after_tick):
                            issues.add('unbound_science_delivery')
                        else:
                            deliveries[receipt['item']] += receipt['quantity']
                if (receipt_id not in received_receipts and index > 0
                        and _integer(receipt.get('tick')) and first_tick < receipt['tick'] <= tick):
                    window_receipts[receipt_id] = deepcopy(receipt)
                received_receipts.add(receipt_id)
            consumed = _counter(factory.get('consumed'))
            if first_consumed is None:
                first_consumed = dict(consumed)
            if previous_consumed is not None:
                reject(any(consumed.get(k, -1) < v for k, v in previous_consumed.items()), 'consumption_counter_regressed')
            increment = sum(max(0, consumed.get(p, 0) - (previous_consumed or consumed).get(p, 0)) for p in trial['science_packs'])
            consumptions = Counter({p: consumed.get(p, 0) - first_consumed.get(p, 0) for p in trial['science_packs']})
            previous_consumed = dict(consumed)
            completed = state.get('researched')
            if not isinstance(completed, list) or any(not isinstance(v, str) or len(v) > 128 for v in completed):
                raise ValueError('Invalid research completion evidence')
            completed = set(completed)
            reject(not researched_before <= completed, 'research_completion_regressed')
            research = factory.get('research')
            progress = factory.get('research_progress')
            advanced = bool(completed - researched_before)
            if isinstance(research, str) and research and _number(progress, 0, 1):
                reject(progress < progress_highwater.get(research, 0), 'research_progress_regressed')
                advanced |= progress > progress_highwater.get(research, progress)
                progress_highwater[research] = progress
            elif research not in ('', None) or not completed:
                issues.add('research_progress_missing')
            if index == 0:
                baseline_goal_complete = trial['research_goal'] in completed
            goal_completed = trial['research_goal'] in completed
            researched_before = completed
            # Consumption plus research progress is sustained useful work; delivery
            # is checked separately because a legitimate batch can cover minutes.
            if increment > 0 and advanced:
                longest_stall = max(longest_stall, (now - last_progress_time).total_seconds())
                last_progress_time = now
            for key, route in native_routes.items():
                flow = route['flow']
                if not flow:
                    continue
                binding = _route_binding(route)
                old = route_history.get(key)
                if old is not None:
                    reject(old['binding'] != binding, 'route_flow_identity_changed')
                    reject(any(flow[k] < old['flow'][k] for k in ('sent', 'received', 'positive_samples', 'last_tick')),
                           'route_flow_counter_regressed')
                    reject(flow['first_tick'] != old['flow']['first_tick'], 'route_flow_epoch_changed')
                    stats = route_statistics[key]
                    stats['sent'] += max(0, flow['sent'] - old['flow']['sent'])
                    stats['received'] += max(0, flow['received'] - old['flow']['received'])
                    stats['positive_samples'] += max(0, flow['positive_samples'] - old['flow']['positive_samples'])
                    attributable = max(0, flow['received'] - stats['baseline_sent'])
                    if attributable > stats['attributed_received']:
                        if (flow['positive_samples'] > old['flow']['positive_samples']
                                and flow['last_positive_tick'] > old['flow']['last_positive_tick']):
                            stats['attributed_positive_boundaries'] += 1
                            if stats['first_positive_tick'] is None:
                                stats['first_positive_tick'] = flow['last_positive_tick']
                        else:
                            issues.add('route_positive_sample_history_mismatch')
                        stats['attributed_received'] = attributable
                else:
                    route_statistics[key] = {'route': key, 'sent': 0, 'received': 0, 'positive_samples': 0,
                        'baseline_sent': flow['sent'], 'attributed_received': 0,
                        'attributed_positive_boundaries': 0, 'first_positive_tick': None,
                        'kind': 'coal' if route['target']['inventory'] == 'fuel' else 'downstream',
                        'target_unit': route['target']['unit_number'], 'source_unit': route['source']['unit_number'],
                        'recipe': route['target']['recipe'], 'item': route['item'],
                        'target_first_products': factory['entities'][route['target']['role']].get('products_finished'),
                        'target_last_products': None}
                route_statistics[key]['target_last_products'] = factory['entities'][route['target']['role']].get('products_finished')
                route_history[key] = {'binding': binding, 'flow': deepcopy(flow)}
        prior = record.get('previous_iteration_timing')
        if prior is not None:
            prior = validate_timing(prior)
            reject(prior['status'] != 'returned', 'failed_iteration_timing')
            number = prior['iteration_index']
            reject(last_timing_index is not None and number <= last_timing_index, 'duplicate_or_regressed_iteration_timing')
            reject(last_timing_index is not None and number != last_timing_index + 1, 'complete_iteration_timing_coverage_missing')
            last_timing_index = number
            if index <= 1:
                boundary_timings += 1  # preceding cycle crosses the first after-state boundary
            elif not prior['partition_complete']:
                incomplete_timings += 1
            else:
                for clock in ('wall', 'process_cpu'):
                    timing_samples['iteration:' + clock].append(prior['totals_ns'][clock])
                for phase, values in prior['phases'].items():
                    for clock in ('wall', 'process_cpu'):
                        timing_samples['exclusive:' + phase + ':' + clock].append(values[clock + '_exclusive_ns'])
                    timing_counts['phase:' + phase + ':calls'] += values['calls']
                    timing_counts['phase:' + phase + ':failed'] += values['failed']
                for name in METRIC_COUNTERS:
                    timing_counts['native_io:' + name] += prior['native_io'][name]
                gap = prior['gap']
                reject(not gap['complete'], 'iteration_gap_timing_unknown')
                if gap['complete']:
                    for name in ('total_ns', 'intentional_sleep_ns', 'other_gap_ns'):
                        for clock in ('wall', 'process_cpu'):
                            timing_samples['gap:' + name + ':' + clock].append(gap[name][clock])
                    timing_counts['gap:sleep_calls'] += gap['sleep_calls']
                    timing_counts['gap:sleep_failed'] += gap['sleep_failed']
        if index > 1 and prior is None:
            issues.add('complete_iteration_timing_coverage_missing')
        phases = record.get('phases', [])
        if not isinstance(phases, list) or len(phases) > 1024:
            raise ValueError('Invalid bounded phase list')
        for phase in phases:
            validate_phase(phase)
    longest_stall = max(longest_stall, (last_time - last_progress_time).total_seconds())
    wall = (last_time - first_time).total_seconds()
    native_ticks = last_tick - first_tick
    reject(wall < trial['minimum_window_seconds'] or native_ticks < trial['minimum_window_seconds'] * 60,
           'complete_30_minute_window_missing')
    reject(native_ticks > (wall + trial['max_observation_gap_seconds']) * 60, 'native_time_outpaces_wall_time')
    reject(longest_gap > trial['max_observation_gap_seconds'], 'observation_gap_exceeds_predeclared_limit')
    outcome_gaps = set()
    def outcome(condition, code):
        if condition:
            outcome_gaps.add(code)
    outcome(longest_stall > trial['max_no_science_progress_seconds'], 'sustained_science_progress_missing')
    outcome(baseline_goal_complete or not goal_completed, 'new_research_milestone_not_completed')
    outcome(any(deliveries[p] <= 0 or consumptions[p] <= 0 for p in trial['science_packs']),
            'science_delivery_or_consumption_missing')
    reject(not model_calls or len(resolved_models) != 1, 'single_actual_jev_model_not_demonstrated')
    reject(final.get('last_tick') != last_tick, 'final_checkpoint_window_mismatch')
    reject(final_failures != previous_failures, 'final_failure_history_mismatch')
    reject(final.get('status') != rows[-1].get('status'), 'final_checkpoint_status_mismatch')
    reject(final.get('completed_goals') != previous_goals,
           'completed_goal_history_regressed')
    reject(final.get('capital_investment') != previous_capital,
           'final_capital_history_mismatch')
    last_factory = rows[-1]['after_state']['factory']
    for field, envelope, parser in (('input_commitments', 'input_routes', input_routes.sources),
                                    ('outpost_commitments', 'mining_outposts', mining_outposts.sources)):
        observed = last_factory.get(envelope, {}).get('sources', {})
        if envelope in last_factory:
            try:
                parser(SimpleNamespace(session_id=session, tick=last_tick, factory=last_factory))
            except (ValueError, KeyError, TypeError, AttributeError):
                issues.add('invalid_final_composed_observation')
        for key, row in observed.items() if isinstance(observed, dict) else ():
            reject(not isinstance(row, dict) or row.get('state') not in {'proposed', 'building', 'ready', 'depleted'}
                   or row.get('state') != 'proposed' and key not in final.get(field, {}),
                   'observed_composed_commitment_missing')
        for key, saved in final.get(field, {}).items():
            reject(not isinstance(observed, dict) or key not in observed
                   or not _retains_prefix(saved, observed[key]),
                   'final_composed_ownership_not_observed')
    observed_successor_sources = {
        source for record in rows for label in ('state', 'after_state')
        for source in record[label].get('factory', {}).get('successors', {}).get('sources', {})
    }
    issues.update(final_successor_issues(initial, final, rows[-1], observed_successor_sources))
    issues.update(successor_history_issues(rows))
    issues.update(project_history_issues(initial, rows, final))
    issues.update(funding_history_issues(initial, rows, final))
    reject(set(final.get('solid_commitments', {})) != set(committed), 'final_route_checkpoint_mismatch')
    for key, current in committed.items():
        reject(final.get('solid_commitments', {}).get(key) != current, 'final_route_checkpoint_mismatch')
    qualified = [v for v in route_statistics.values()
                 if v['sent'] > 0 and v['attributed_received'] > 0
                 and v['attributed_positive_boundaries'] >= 3]
    coal = [v for v in qualified if v['kind'] == 'coal' and v['item'] == 'coal']
    # An intermediate route does not itself explain separate lab receipts.
    # Until a complete dependency chain is bound, require the routed target
    # recipe to name a pack also newly delivered to and consumed by the lab.
    downstream = [v for v in qualified if v['kind'] == 'downstream' and v['recipe'] in trial['downstream_recipes']
                  and v['recipe'] in trial['science_packs']
                  and deliveries[v['recipe']] > 0 and consumptions[v['recipe']] > 0
                  and _integer(v['target_first_products']) and _integer(v['target_last_products'])
                  and v['target_last_products'] > v['target_first_products']]
    outcome(len({v['target_unit'] for v in coal}) < 2, 'two_distinct_fuel_consumers_not_measured')
    outcome(not downstream, 'downstream_flow_and_production_not_measured')
    reject(incomplete_timings > 0 or len(timing_samples['iteration:wall']) < trial['minimum_timing_samples'],
           'complete_iteration_timing_coverage_missing')
    return {
        'schema': SCHEMA, 'trial_sha256': sha256(canonical(trial)),
        'evidence_sha256': evidence_hash.hexdigest(),
        'invocation_sha256': sha256(canonical(_identity(rows[0]))),
        'runtime_mods_sha256': sha256(canonical(runtime_identity['mods'])) if runtime_identity is not None
                               and _valid_mods(runtime_identity['mods']) else None,
        'checkpoint_sha256': {'initial': sha256(canonical(initial)), 'final': sha256(canonical(final))},
        'input_binding_sha256': sha256(canonical({
            'gameplay': evidence_hash.hexdigest(), 'trial': sha256(canonical(trial)),
            'initial_checkpoint': sha256(canonical(initial)),
            'final_checkpoint': sha256(canonical(final)),
        })),
        'source_commit': trial['expected_commit'], 'source_sha256': trial['expected_source_sha256'],
        'working_tree_cleanliness': 'explicitly_reported_clean' if all(
            row.get('code_revision', {}).get('dirty') is False for row in rows) else 'not_reported',
        'integrity_checks_passed': not issues,
        'measurement_checks_passed': not issues and (trial['arm'] == 'baseline' or not outcome_gaps),
        'issues': sorted(issues | (outcome_gaps if trial['arm'] == 'treatment' else set())),
        'outcome_gaps': sorted(outcome_gaps),
        'evidence_kind': trial['evidence_kind'], 'records': len(rows),
        'window': {'wall_seconds': wall, 'native_ticks': native_ticks,
                   'max_record_gap_seconds': longest_gap, 'max_no_science_progress_seconds': longest_stall},
        'science': {'delivered_by_new_owned_lab_receipts': sum(deliveries.values()),
                    'force_consumed_with_single_owned_lab': sum(consumptions.values()),
                    'consumed_per_wall_minute': sum(consumptions.values()) * 60 / wall if wall > 0 else None,
                    'consumed_by_declared_pack': dict(consumptions),
                    'new_declared_research_completed': goal_completed and not baseline_goal_complete},
        'transport': {'coal_consumers_with_new_flow': len({v['target_unit'] for v in coal}),
                      'coal_inventory_delivery_lower_bound': sum(v['attributed_received'] for v in coal),
                      'downstream_routes_with_flow_and_production': len(downstream),
                      'downstream_delivery_units': sum(v['attributed_received'] for v in downstream),
                      'intermediate_chain_diagnostic': _downstream_chain_diagnostic(
                          trial['downstream_chain'] if trial['schema'] == TRIAL_SCHEMA_V3 else None,
                          rows, route_statistics, window_receipts, initial_lab_unit, not issues),
                      'mined_coal_provenance_verified': False,
                      'source_note': 'Stocked chests and corridor flow do not prove paid coal mining or bootstrap.'},
        'model_calls': model_calls,
        'timing': {'distributions': {k: distribution(v) for k, v in sorted(timing_samples.items())},
                   'counts': dict(sorted(timing_counts.items())), 'incomplete_iterations': incomplete_timings,
                   'boundary_iterations_excluded': boundary_timings,
                   'unobserved_phase_labels': sorted(NAMES - {k.split(':')[1] for k in timing_samples if k.startswith('exclusive:')}),
                   'final_unpublished_tail_inferred': False, 'inclusive_phases_summed': False,
                   'opaque_helper_cpu_or_network_inferred': False},
        'remaining_gates': list(REQUIRED_GATES),
        'native_acceptance': 'not_accepted', 'deployment_authorized': False,
        'causal_improvement_proven': False, 'external_authenticity_proven': False,
    }


def analyze(gameplay: Path, trial_path: Path, initial_checkpoint: Path, final_checkpoint: Path) -> dict:
    captured = {'gameplay': stable_read(gameplay, MAX_LOG), 'trial': stable_read(trial_path, MAX_JSON),
                'initial_checkpoint': stable_read(initial_checkpoint), 'final_checkpoint': stable_read(final_checkpoint)}
    trial = load_json(captured['trial'])
    if (trial.get('schema') in {TRIAL_SCHEMA_V2, TRIAL_SCHEMA_V3}
            and sha256(captured['initial_checkpoint']) != trial.get('initial_checkpoint_sha256')):
        raise ValueError('Predeclared checkpoint differs from analyzed input')
    initial = load_json(captured['initial_checkpoint'])
    final = load_json(captured['final_checkpoint'])
    result = analyze_rows(records(captured['gameplay']), trial, initial, final)
    result['inputs_sha256'] = {k: sha256(v) for k, v in captured.items()}
    result['raw_inputs_binding_sha256'] = sha256(canonical(result['inputs_sha256']))
    return result


def compare(baseline: dict, treatment: dict, baseline_trial: dict, treatment_trial: dict) -> dict:
    """Compare internally validated reports, never authenticate caller assertions.

    Use compare_files for an audit: it recomputes both reports from the retained
    input bytes instead of trusting editable, previously generated report JSON.
    A stalled baseline is valid measured data, not a reason to discard its arm.
    """
    for trial, value in ((baseline_trial, baseline), (treatment_trial, treatment)):
        validate_trial(trial)
        if (not isinstance(value, dict) or value.get('schema') != SCHEMA
                or value.get('trial_sha256') != sha256(canonical(trial))
                or not _digest(value.get('evidence_sha256')) or not _digest(value.get('invocation_sha256'))
                or not _digest(value.get('input_binding_sha256'))):
            raise ValueError('Report is not bound to the supplied trial and capture')
    issues = set()
    axis = baseline_trial['comparison_axis']
    if (baseline_trial['arm'] != 'baseline' or treatment_trial['arm'] != 'treatment'
            or axis not in {'algorithm', 'capacity'} or treatment_trial['comparison_axis'] != axis):
        issues.add('invalid_or_unmatched_pair')
    controlled = {'experiment_sha256', 'workload_sha256', 'initial_save_sha256', 'configuration',
                  'science_packs', 'research_goal', 'downstream_recipes', 'solid_intents', 'evidence_kind',
                  'campaign_treatment', 'requested_model', 'resolved_model',
                  'minimum_window_seconds', 'max_observation_gap_seconds',
                  'max_no_science_progress_seconds', 'minimum_timing_samples', 'regression_limits'}
    if baseline_trial['schema'] != treatment_trial['schema']:
        issues.add('uncontrolled_trial_schema')
    if (baseline_trial['schema'] == treatment_trial['schema']
            and baseline_trial['schema'] in {TRIAL_SCHEMA_V2, TRIAL_SCHEMA_V3}):
        controlled.update(('coal_targets', 'treatment_sha256', 'initial_checkpoint_sha256',
                           'vm_uuid', 'production_vm_uuid'))
        if baseline_trial['schema'] == TRIAL_SCHEMA_V3:
            controlled.add('downstream_chain')
    controlled |= {'capacity_profile_sha256'} if axis == 'algorithm' else {'expected_commit', 'expected_source_sha256'}
    if any(baseline_trial[k] != treatment_trial[k] for k in controlled):
        issues.add('uncontrolled_pair_difference')
    if baseline['evidence_sha256'] == treatment['evidence_sha256'] or baseline['invocation_sha256'] == treatment['invocation_sha256']:
        issues.add('reused_capture_or_invocation')
    if (not _digest(baseline.get('runtime_mods_sha256'))
            or baseline.get('runtime_mods_sha256') != treatment.get('runtime_mods_sha256')):
        issues.add('uncontrolled_runtime_mods')
    windows = [value.get('window', {}) for value in (baseline, treatment)]
    if (not all(isinstance(v, dict) and _number(v.get('wall_seconds'))
                and _integer(v.get('native_ticks')) for v in windows)
            or abs(windows[0]['wall_seconds'] - windows[1]['wall_seconds'])
                > baseline_trial['max_observation_gap_seconds']
            or abs(windows[0]['native_ticks'] - windows[1]['native_ticks'])
                > baseline_trial['max_observation_gap_seconds'] * 60):
        issues.add('unmatched_measurement_windows')
    if baseline.get('integrity_checks_passed') is not True or treatment.get('measurement_checks_passed') is not True:
        issues.add('ineligible_pair_arm')
    rates = [r.get('science', {}).get('consumed_per_wall_minute') for r in (baseline, treatment)]
    p95 = [r.get('timing', {}).get('distributions', {}).get('iteration:wall', {}).get('p95_ns') for r in (baseline, treatment)]
    rate_ratio = latency_ratio = None
    if not all(_number(v) for v in rates) or not all(_number(v, 1) for v in p95):
        issues.add('missing_comparison_metric')
    else:
        # A zero baseline has no finite rate ratio. Keep its absolute rate and
        # report the difference; never invent an infinite percentage improvement.
        if rates[0] > 0:
            rate_ratio = rates[1] / rates[0]
            if rate_ratio < baseline_trial['regression_limits']['min_science_rate_ratio']:
                issues.add('science_rate_regressed')
        latency_ratio = p95[1] / p95[0]
        if latency_ratio > baseline_trial['regression_limits']['max_iteration_p95_ratio']:
            issues.add('iteration_p95_regressed')
    return {'schema': 'jev-factorio.integration-comparison.v1',
            'paired_measurement_checks_passed': not issues, 'issues': sorted(issues),
            'evidence_kind': baseline_trial['evidence_kind'],
            'baseline_evidence_sha256': baseline['evidence_sha256'],
            'treatment_evidence_sha256': treatment['evidence_sha256'],
            'baseline_input_binding_sha256': baseline['input_binding_sha256'],
            'treatment_input_binding_sha256': treatment['input_binding_sha256'],
            'baseline_raw_inputs_binding_sha256': baseline.get('raw_inputs_binding_sha256'),
            'treatment_raw_inputs_binding_sha256': treatment.get('raw_inputs_binding_sha256'),
            'baseline_science_per_wall_minute': rates[0], 'treatment_science_per_wall_minute': rates[1],
            'science_rate_ratio': rate_ratio, 'iteration_p95_ratio': latency_ratio,
            'causal_improvement_proven': False, 'native_acceptance': 'not_accepted',
            'deployment_authorized': False, 'remaining_gates': list(REQUIRED_GATES)}


def compare_files(baseline: dict[str, Path], treatment: dict[str, Path]) -> dict:
    """Reanalyze private inputs; capture hashes bind any second trial-file read."""
    def arm(paths):
        if set(paths) != {'gameplay', 'trial', 'initial_checkpoint', 'final_checkpoint'}:
            raise ValueError('Four explicit input files are required per comparison arm')
        value = analyze(paths['gameplay'], paths['trial'], paths['initial_checkpoint'], paths['final_checkpoint'])
        trial_bytes = stable_read(paths['trial'], MAX_JSON)
        trial = load_json(trial_bytes)
        if (sha256(trial_bytes) != value['inputs_sha256']['trial']
                or sha256(canonical(trial)) != value['trial_sha256']):
            raise ValueError('Trial changed during comparison')
        return value, trial
    first, first_trial = arm(baseline)
    second, second_trial = arm(treatment)
    return compare(first, second, first_trial, second_trial)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gameplay', type=Path, required=True)
    parser.add_argument('--trial', type=Path, required=True)
    parser.add_argument('--initial-checkpoint', type=Path, required=True)
    parser.add_argument('--final-checkpoint', type=Path, required=True)
    for name in ('gameplay', 'trial', 'initial-checkpoint', 'final-checkpoint'):
        parser.add_argument('--baseline-' + name, type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        names = ('gameplay', 'trial', 'initial_checkpoint', 'final_checkpoint')
        baseline = {name: getattr(args, 'baseline_' + name) for name in names}
        if any(baseline.values()):
            if not all(baseline.values()):
                raise ValueError('Incomplete baseline arm')
            result = compare_files(baseline, {name: getattr(args, name) for name in names})
        else:
            result = analyze(args.gameplay, args.trial, args.initial_checkpoint, args.final_checkpoint)
        write_new(args.output, canonical(result))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
        parser.exit(2, 'Integration evidence invalid; no native acceptance or deployment authorization.\n')
    passed = result.get('paired_measurement_checks_passed', result.get('measurement_checks_passed', False))
    parser.exit(0 if passed else 2,
                'Integration consistency report written; native acceptance remains open.\n')


if __name__ == '__main__':
    main()
