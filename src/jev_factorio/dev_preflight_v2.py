"""Point-in-time transport ownership checks, never attachment or acceptance."""
from __future__ import annotations

import math
from copy import deepcopy
from importlib.resources import files

from .acceptance_io import canonical, sha256

REPORT_SCHEMA = 'jev-factorio.dev-preflight.v2'
SCOPE = 'point_in_time_transport_ownership_not_flow_or_payment_authenticity'
QUERY = 'lua/acceptance_probe_v2.lua'
NATIVE_KEYS = set('schema tick speed tick_paused marked runtime_present campaign_present fair_present '
                  'session_id player_index bound_player_index connected bound actor_unit surface_index force_index '
                  'mods entities input_routes output_buffers outposts solid_routes coal_supply idle '
                  'truncated ownership_complete'.split())
ENTITY_KEYS = set('name unit_number position direction surface_index force_index quality bounds recipe'.split())
IDLE_KEYS = set('walking mining crafting_queue_size cheat_mode quarantined fair_job_status '
                'craft_job_status legacy_queue_nonempty construction_pending manual_pending'.split())


class OwnershipMismatch(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise OwnershipMismatch(reason)


def same(left, right):
    return canonical(left) == canonical(right)


def table(value, maximum):
    require(isinstance(value, dict) and len(value) <= maximum, 'invalid_ownership_table')
    require(all(isinstance(k, str) and 0 < len(k) <= 128 for k in value), 'invalid_ownership_key')
    return value


def integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**53 - 1


def point(value):
    return (isinstance(value, dict) and set(value) == {'x', 'y'}
            and all(type(v) in {int, float} and math.isfinite(v) and abs(v) <= 1_000_000
                    for v in value.values()))


def idle_checkpoint(checkpoint):
    """No pending/prepared owner is silently converted into an idle boundary."""
    require(checkpoint.get('status') == 'running' and checkpoint.get('target') == 'rocket_launch',
            'checkpoint_not_idle_running')
    require(not any(checkpoint.get(k) for k in (
        'active_plan', 'pending', 'attempt', 'reservations', 'background_job',
        'background_attempt', 'capital_investment', 'solid_funding', 'coal_funding',
        'transfer_recovery')), 'checkpoint_has_active_or_pending_work')
    require(all(project.get('status') == 'qualified'
                for project in checkpoint.get('successor_projects', {}).values()),
            'checkpoint_successor_not_idle')


def query_sha256():
    return sha256(files('jev_factorio').joinpath(QUERY).read_text(encoding='utf-8').encode())


def normalize_native(native):
    """Defensive adapter compatibility for empty []; only declared maps convert.

    The current 2.0.77 engine encoder was observed returning {} for these maps.
    Never coerce a nonempty list or an ordered intents/targets/steps array. This
    runs again when verifying a saved report, not just at the RCON boundary.
    """
    if not isinstance(native, dict):
        return native
    native = deepcopy(native)
    def empty_map(parent, key):
        if isinstance(parent, dict) and type(parent.get(key)) is list and not parent[key]:
            parent[key] = {}
    empty_map(native, 'entities')
    for family in ('solid_routes', 'coal_supply', 'outposts', 'input_routes', 'output_buffers'):
        group = native.get(family)
        if not isinstance(group, dict):
            continue
        empty_map(group, 'commitments')
        if family == 'outposts':
            empty_map(group, 'receipts')
        rows = group.get('commitments')
        if isinstance(rows, dict):
            for row in rows.values():
                empty_map(row, 'parts')
                if family == 'outposts':
                    empty_map(row, 'flow')
    return native


def inspect_native(native, checkpoint, expected_session):
    """Revalidate a fixed v2 projection; unsigned input is not engine attestation."""
    try:
        idle_checkpoint(checkpoint)
        native = normalize_native(native)
        require(isinstance(native, dict) and set(native) == NATIVE_KEYS
                and type(native['schema']) is int and native['schema'] == 2,
                'invalid_probe_schema')
        require(native['ownership_complete'] is True and native['truncated'] is False,
                'ownership_probe_incomplete')
        require(all(native[k] is True for k in ('marked', 'runtime_present', 'campaign_present',
                    'fair_present', 'connected', 'bound')), 'native_actor_binding_missing')
        require(native['session_id'] == checkpoint['session_id'] == expected_session,
                'session_mismatch')
        require(type(native['speed']) in {int, float} and native['speed'] == 1
                and native['tick_paused'] is False, 'simulation_not_normal_running')
        require(integer(native['tick']) and native['tick'] >= checkpoint['last_tick'],
                'native_tick_precedes_checkpoint')
        require(all(integer(native[k], 1) for k in ('player_index', 'bound_player_index',
                    'actor_unit', 'surface_index', 'force_index'))
                and native['player_index'] == native['bound_player_index'], 'invalid_actor_epoch')
        require(native['mods'] == {'base': '2.0.77'}, 'unsupported_native_mod_version')
        idle = native['idle']
        require(isinstance(idle, dict) and set(idle) == IDLE_KEYS, 'invalid_idle_projection')
        require(all(idle[k] is False for k in ('walking', 'mining', 'cheat_mode', 'quarantined',
                    'legacy_queue_nonempty', 'construction_pending', 'manual_pending'))
                and type(idle['crafting_queue_size']) is int and idle['crafting_queue_size'] == 0
                and idle['fair_job_status'] in {'absent', 'completed'}
                and idle['craft_job_status'] in {'absent', 'completed'}, 'native_work_not_reconciled')
        epoch = {'actor_index': native['player_index'], 'surface_index': native['surface_index'],
                 'force_index': native['force_index']}
        require(same(checkpoint.get('solid_epoch'), epoch)
                and same(checkpoint.get('coal_epoch'), epoch), 'checkpoint_actor_epoch_mismatch')

        entities = table(native['entities'], 2048)
        units = set()
        for entity in entities.values():
            require(isinstance(entity, dict) and set(entity) == ENTITY_KEYS
                    and integer(entity['unit_number'], 1) and entity['unit_number'] not in units
                    and isinstance(entity['name'], str) and 0 < len(entity['name']) <= 128
                    and entity['quality'] == 'normal' and integer(entity['direction'])
                    and entity['direction'] <= 15 and point(entity['position'])
                    and isinstance(entity['recipe'], str) and len(entity['recipe']) <= 128
                    and isinstance(entity['bounds'], dict)
                    and set(entity['bounds']) == {'left_top', 'right_bottom'}
                    and all(point(v) for v in entity['bounds'].values())
                    and same(entity['surface_index'], native['surface_index'])
                    and same(entity['force_index'], native['force_index']), 'invalid_or_aliased_owned_entity')
            units.add(entity['unit_number'])

        paid_units, paid_roles, paid_receipts = set(), set(), set()
        def parts(parts, steps=None):
            for name, part in table(parts, 66).items():
                require(isinstance(part, dict) and set(part) == {'role', 'unit_number', 'receipt', 'paid'}
                        and type(part['paid']) is int and part['paid'] == 1
                        and integer(part['unit_number'], 1)
                        and isinstance(part['role'], str) and part['role'] in entities
                        and isinstance(part['receipt'], str) and 0 < len(part['receipt']) <= 128,
                        'invalid_paid_part')
                entity = entities[part['role']]
                require(entity['unit_number'] == part['unit_number'], 'owned_component_missing')
                require(part['unit_number'] not in paid_units and part['role'] not in paid_roles
                        and part['receipt'] not in paid_receipts, 'shared_paid_component_or_receipt')
                paid_units.add(part['unit_number']); paid_roles.add(part['role']); paid_receipts.add(part['receipt'])
                if steps is not None:
                    spec = next((s for s in steps if s['part'] == name), None)
                    require(spec is not None and all(same(entity[k], spec[k])
                            for k in ('name', 'position', 'direction')), 'paid_component_geometry_mismatch')

        def endpoint(saved):
            entity = entities.get(saved['role'])
            require(entity is not None and all(same(entity[k], saved[k])
                    for k in ('unit_number', 'name', 'position', 'bounds'))
                    and (not saved['recipe'] or entity['recipe'] == saved['recipe']),
                    'owned_endpoint_mismatch')

        solid = native['solid_routes']
        require(isinstance(solid, dict) and set(solid) == {'present', 'protocol', 'implementation_revision',
                'contract_family', 'reservation_contract', 'intents', 'binding', 'commitments'}
                and solid['present'] is True and type(solid['protocol']) is int and solid['protocol'] == 1
                and type(solid['implementation_revision']) is int and solid['implementation_revision'] == 4
                and solid['contract_family'] == 'straight-solid-corridor-v1'
                and solid['reservation_contract'] == 'full-corridor-manhattan-v1', 'unsupported_solid_runtime')
        intents = checkpoint['solid_intents']
        binding = '\n'.join(''.join(str(len(i[k])) + ':' + i[k] for k in
                            ('source', 'target', 'item', 'destination')) for i in intents)
        require(same(solid['intents'], intents) and solid['binding'] == binding, 'solid_treatment_mismatch')
        require(same(table(solid['commitments'], 4), checkpoint['solid_commitments']), 'owned_solid_mismatch')
        from .solid_routes import validate_commitment as validate_solid
        for key, row in solid['commitments'].items():
            validate_solid(row, key)
            endpoint(row['source']); endpoint(row['target']); parts(row['parts'], row['steps'])

        coal = native['coal_supply']
        require(isinstance(coal, dict) and set(coal) == {'present', 'revision', 'targets', 'binding',
                'admission_evidence', 'committed', 'commitments'} and coal['present'] is True
                and type(coal['revision']) is int and coal['revision'] == 4, 'unsupported_coal_runtime')
        targets = checkpoint['coal_targets']
        require(same(coal['targets'], targets) and coal['binding'] == '\n'.join(str(len(t)) + ':' + t for t in targets)
                and coal['admission_evidence'] is checkpoint.get('coal_economic_admission', False),
                'coal_treatment_mismatch')
        require(coal['committed'] is bool(checkpoint['coal_commitments'])
                and same(table(coal['commitments'], 4), checkpoint['coal_commitments']), 'owned_coal_mismatch')
        from .coal_supply import validate_commitment as validate_coal
        for key, row in coal['commitments'].items():
            validate_coal(row, key)
            endpoint(row['target']); parts(row['parts'], row['steps'])

        outposts = native['outposts']
        require(isinstance(outposts, dict) and set(outposts) == {'present', 'protocol', 'commitments', 'receipts'},
                'invalid_outpost_projection')
        require(type(outposts['present']) is bool and type(outposts['protocol']) is int
                and outposts['protocol'] == (1 if outposts['present'] else 0)
                and (outposts['present'] or 'outposts_schema' not in checkpoint), 'unsupported_outpost_runtime')
        require(same(table(outposts['commitments'], 2), checkpoint.get('outpost_commitments', {})),
                'owned_outpost_mismatch')
        expected_receipts = {}
        for row in outposts['commitments'].values():
            require(same(row['surface_index'], epoch['surface_index'])
                    and same(row['force_index'], epoch['force_index']), 'outpost_epoch_mismatch')
            parts(row['parts'], row['steps'])
            expected_receipts.update({p['receipt']: p['unit_number'] for p in row['parts'].values()})
        require(same(table(outposts['receipts'], 4), expected_receipts), 'outpost_receipt_mismatch')

        expected_input = dict(checkpoint.get('input_commitments', {}))
        expected_output = dict(checkpoint.get('output_commitments', {}))
        for source, project in checkpoint.get('successor_projects', {}).items():
            require(entities.get('recipe:' + source[7:], {}).get('unit_number') == project['predecessor_unit']
                    and entities.get(source, {}).get('unit_number') == project['source_unit'], 'successor_unit_mismatch')
            retained = checkpoint['successor_receipts'][source]
            for label, destination in (('input', expected_input), ('output', expected_output)):
                if retained[label]:
                    expected = {'source_unit': project['source_unit'], 'layout': retained[label + '_layout'],
                                'parts': retained[label]}
                    require(source not in destination or same(destination[source], expected), 'successor_receipt_mismatch')
                    destination[source] = expected
        for family, expected in (('input_routes', expected_input), ('output_buffers', expected_output)):
            group = native[family]
            schema_enabled = ('successor_schema' in checkpoint or
                              ('input_routes_schema' in checkpoint if family == 'input_routes'
                               else 'output_buffers_schema' in checkpoint))
            require(isinstance(group, dict) and set(group) == {'present', 'protocol', 'commitments'}
                    and type(group['present']) is bool and type(group['protocol']) is int
                    and group['protocol'] == (1 if group['present'] else 0)
                    and (group['present'] or (not expected and not schema_enabled)),
                    'invalid_legacy_transport_projection')
            actual = table(group['commitments'], 5)
            reason = 'ordinary_output_ownership_not_retained' if family == 'output_buffers' and set(actual) - set(expected) else 'owned_' + family + '_mismatch'
            require(same(actual, expected), reason)
            for source, row in actual.items():
                require(entities.get(source, {}).get('unit_number') == row['source_unit'], 'legacy_source_missing')
                parts(row['parts'])
        require(not any(role.startswith(('solid:', 'coal:', 'outpost:', 'input:', 'output-chest:', 'output-arm:'))
                        and role not in paid_roles for role in entities), 'untracked_transport_entity')
        return []
    except OwnershipMismatch as error:
        return [str(error)]
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError):
        return ['invalid_ownership_probe']
