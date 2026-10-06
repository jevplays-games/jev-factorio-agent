"""Bind ordinary connector payments to a controller checkpoint without replay."""
from __future__ import annotations

import json
from copy import deepcopy

from .iteration_timing import decode_native
from .planning.connection_identity import connection_key


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise ValueError("Invalid connector receipt integer")
    return value


def _receipt(value):
    if (not isinstance(value, str) or len(value) != 64
            or any(c not in '0123456789abcdef' for c in value)):
        raise ValueError("Invalid connector receipt identity")
    return value


def _identity(row):
    if not isinstance(row, dict):
        raise ValueError("Invalid connector route")
    if (row.get('kind') not in {'pipe', 'small-electric-pole'}
            or not isinstance(row.get('fluid'), str) or len(row['fluid']) > 128
            or not all(isinstance(row.get(k), str) and row[k] and len(row[k]) <= 128
                       for k in ('source', 'target'))):
        raise ValueError("Invalid connector route identity")
    for key in ('source_unit', 'target_unit', 'actor_unit', 'surface_index', 'force_index'):
        _integer(row.get(key), 1)
    if not isinstance(row.get('session_id'), str) or not row['session_id']:
        raise ValueError("Invalid connector route session")
    return {key: row[key] for key in ('source', 'target', 'source_unit', 'target_unit',
                                      'kind', 'fluid', 'actor_unit', 'surface_index',
                                      'force_index', 'session_id')}


def _page(native, receipt, offset, limit):
    script = ('rcon.print(helpers.table_to_json(storage.campaign.connector_page('
              + json.dumps(receipt) + ',' + str(offset) + ',' + str(limit) + ')))')
    return decode_native(native.command(script))


def capture(native, receipt, row):
    """Read bounded detail pages; never infer ownership from same-force cells."""
    _receipt(receipt)
    if row.get('id') != receipt or row.get('state') not in {'building', 'complete', 'fault'}:
        raise ValueError("Connector route receipt or state changed")
    identity = _identity(row)
    count = _integer(row.get('cell_count'), 1)
    if count > 1200:
        raise ValueError("Connector route exceeds bound")
    paid, external = _integer(row.get('paid')), _integer(row.get('external'))
    if paid + external > count:
        raise ValueError("Connector route counts changed")
    cells = []
    for offset in range(1, count + 1, 64):
        page = _page(native, receipt, offset, min(64, count - offset + 1))
        if (not isinstance(page, dict) or page.get('id') != receipt
                or page.get('valid') is not True or page.get('cell_count') != count
                or page.get('offset') != offset or not isinstance(page.get('cells'), list)
                or len(page['cells']) != min(64, count - offset + 1)):
            raise ValueError("Connector detail page changed")
        for index, cell in enumerate(page['cells'], offset):
            if not isinstance(cell, dict) or cell.get('index') != index:
                raise ValueError("Connector cell order changed")
            pos = cell.get('position')
            if (not isinstance(pos, dict) or set(pos) != {'x', 'y'}
                    or any(type(pos[k]) not in {int, float} or abs(pos[k]) > 1_000_000
                           or pos[k] % 1 != .5 for k in ('x', 'y'))):
                raise ValueError("Connector cell position changed")
            unit = cell.get('unit_number')
            if unit is not None:
                _integer(unit, 1)
            is_paid = cell.get('paid') == 1 and type(cell.get('paid')) is int
            is_external = cell.get('external') is True
            if is_paid and is_external or is_paid and unit is None or is_external and unit is None:
                raise ValueError("Connector cell ownership changed")
            if cell.get('paid') not in {0, 1} or type(cell.get('paid')) is not int:
                raise ValueError("Invalid connector cell payment")
            cells.append({'index': index, 'position': dict(pos), 'unit_number': unit,
                          'paid': is_paid, 'external': is_external})
    if sum(c['paid'] for c in cells) != paid or sum(c['external'] for c in cells) != external:
        raise ValueError("Connector detail counts differ")
    complete = row['state'] == 'complete'
    if complete and (row.get('pending') is not None or not all(c['unit_number'] for c in cells)):
        raise ValueError("Incomplete completed connector route")
    owned = complete and paid == count and external == 0
    # Lua omits the nil `owned` field while a route is still building.
    if row.get('owned', False) is not owned:
        raise ValueError("Connector ownership claim differs")
    return {'id': receipt, **identity, 'state': row['state'], 'paid': paid,
            'external': external, 'owned': owned, 'pending': row.get('pending'),
            'cells': cells}


def validate_binding(binding, session):
    if (not isinstance(binding, dict) or set(binding) != {'protocol', 'session_id', 'routes'}
            or binding['protocol'] != 1 or type(binding['protocol']) is not int
            or binding['session_id'] != session or not isinstance(binding['routes'], dict)
            or len(binding['routes']) > 128):
        raise ValueError("Invalid connector checkpoint binding")
    paid_units = set()
    for receipt, row in binding['routes'].items():
        _receipt(receipt)
        if (not isinstance(row, dict) or row.get('id') != receipt
                or row.get('session_id') != session or not isinstance(row.get('cells'), list)
                or not 1 <= len(row['cells']) <= 1200):
            raise ValueError("Invalid connector checkpoint route")
        _identity(row)
        if connection_key(row) != receipt:
            raise ValueError("Connector checkpoint receipt identity differs")
        if row.get('state') not in {'building', 'complete', 'fault'} or type(row.get('owned')) is not bool:
            raise ValueError("Invalid connector checkpoint state")
        if row.get('pending') is not None:
            _integer(row['pending'], 1)
            if row['pending'] > len(row['cells']):
                raise ValueError("Invalid connector checkpoint pending cell")
        if type(row.get('paid')) is not int or type(row.get('external')) is not int:
            raise ValueError("Invalid connector checkpoint counts")
        for index, cell in enumerate(row['cells'], 1):
            if (not isinstance(cell, dict) or set(cell) != {'index', 'position', 'unit_number',
                                                            'paid', 'external'}
                    or cell['index'] != index or type(cell['paid']) is not bool
                    or type(cell['external']) is not bool or cell['paid'] and cell['external']):
                raise ValueError("Invalid connector checkpoint cell")
            pos = cell['position']
            if (not isinstance(pos, dict) or set(pos) != {'x', 'y'}
                    or any(type(pos[k]) not in {int, float} or abs(pos[k]) > 1_000_000
                           or pos[k] % 1 != .5 for k in ('x', 'y'))):
                raise ValueError("Invalid connector checkpoint position")
            if cell['unit_number'] is not None:
                _integer(cell['unit_number'], 1)
            if (cell['paid'] or cell['external']) and cell['unit_number'] is None:
                raise ValueError("Unidentified connector payment")
            if cell['paid']:
                if cell['unit_number'] in paid_units:
                    raise ValueError("Paid connector unit reused")
                paid_units.add(cell['unit_number'])
        if len({(cell['position']['x'], cell['position']['y']) for cell in row['cells']}) != len(row['cells']):
            raise ValueError("Repeated connector checkpoint position")
        if row.get('paid') != sum(c['paid'] for c in row['cells']) or row.get('external') != sum(c['external'] for c in row['cells']):
            raise ValueError("Connector checkpoint counts differ")
        if row.get('owned') is not (row.get('state') == 'complete' and row['paid'] == len(row['cells']) and row['external'] == 0):
            raise ValueError("Connector checkpoint ownership differs")
        if row['state'] == 'complete' and (row['pending'] is not None
                                          or any(c['unit_number'] is None for c in row['cells'])):
            raise ValueError("Incomplete connector checkpoint completion")
    return binding


def reconcile(memory, snapshot, native, *, resume):
    """A new route is admissible only as the exact checkpointed pending action."""
    data = snapshot.factory.get('connector_ownership')
    if data is None:
        if memory.connector_ownership is not None:
            raise ValueError("Native connector ledger disappeared")
        return
    if (not isinstance(data, dict) or data.get('protocol') != 1
            or type(data.get('protocol')) is not int or data.get('session_id') != snapshot.session_id
            or not isinstance(data.get('routes'), (dict, list)) or len(data['routes']) > 128):
        raise ValueError("Invalid native connector ledger")
    routes = {} if data['routes'] == [] else data['routes']
    if not isinstance(routes, dict):
        raise ValueError("Invalid native connector routes")
    if memory.connector_ownership is None:
        if resume or routes or data.get('active') is not None:
            raise ValueError("Old checkpoint cannot adopt connector ledger")
        memory.connector_ownership = {'protocol': 1, 'session_id': snapshot.session_id, 'routes': {}}
    saved = validate_binding(memory.connector_ownership, snapshot.session_id)['routes']
    next_saved = deepcopy(saved)
    if not set(saved) <= set(routes):
        raise ValueError("Native connector route disappeared")
    new = set(routes) - set(saved)
    if new:
        pending = memory.pending or {}
        plan = memory.active_plan or {}
        steps = plan.get('steps', [])
        if (len(new) != 1 or pending.get('action') != 'factory_connect'
                or pending.get('dispatch') not in {'prepared', 'ambiguous', 'returned'}
                or memory.step_index >= len(steps)):
            raise ValueError("Uncheckpointed connector route appeared")
        step = steps[memory.step_index]
        params = step.get('parameters') or {}
        if new != {connection_key(params)}:
            raise ValueError("Pending connector identity differs")
    if native is None and routes:
        raise ValueError("Native connector detail reader unavailable")
    for receipt, summary in routes.items():
        _receipt(receipt)
        if not isinstance(summary, dict):
            raise ValueError("Invalid native connector summary")
        prior = saved.get(receipt)
        if summary.get('state') == 'fault':
            raise ValueError("Native connector ownership fault")
        # Summary counts cannot attest each cell's payment attribution. Always
        # compare the bounded native detail pages against the checkpoint.
        current = capture(native, receipt, summary)
        if prior is not None:
            if any(current[key] != prior[key] for key in
                   ('source', 'target', 'source_unit', 'target_unit', 'kind', 'fluid',
                    'actor_unit', 'surface_index', 'force_index', 'session_id')):
                raise ValueError("Connector endpoint identity changed")
            for old, cell in zip(prior['cells'], current['cells']):
                if old['position'] != cell['position'] or (old['unit_number'] is not None and old != cell):
                    raise ValueError("Paid connector unit changed")
            if len(prior['cells']) != len(current['cells']) or current['paid'] < prior['paid']:
                raise ValueError("Connector route regressed")
        next_saved[receipt] = current
    active = data.get('active')
    if active is not None:
        _receipt(active)
        if active not in routes or routes[active].get('state') == 'complete':
            raise ValueError("Invalid active connector route")
    validate_binding({'protocol': 1, 'session_id': snapshot.session_id,
                      'routes': next_saved}, snapshot.session_id)
    memory.connector_ownership['routes'] = next_saved
    if active is not None:
        # No new paid action may proceed while any route is unresolved.
        memory.status, memory.reason = 'uncertain', 'Connector route needs exact reconciliation'


def pending_owned(memory, step):
    if memory.connector_ownership is None:
        return False
    validate_binding(memory.connector_ownership, memory.connector_ownership['session_id'])
    params = step.parameters or {}
    return route_paid_coverage(memory.connector_ownership['routes'], connection_key(params))


def route_paid_coverage(routes, receipt):
    """Check a validated ledger without rewriting route-local payment flags.

    Electricity routes may reuse an exact paid pole from another completed
    route with the same source and owner. Each external reference needs its own
    direct paid-cell anchor; external references cannot attest one another.
    Callers must validate the binding and freshly compare native detail pages.
    """
    row = routes.get(receipt)
    if not row or row['state'] != 'complete' or row['pending'] is not None:
        return False
    if len({c['unit_number'] for c in row['cells']}) != len(row['cells']):
        return False
    if row['owned']:
        return True
    if row['kind'] != 'small-electric-pole' or row['fluid'] != 'electricity':
        return False
    identity = ('source', 'source_unit', 'kind', 'fluid', 'actor_unit',
                'surface_index', 'force_index', 'session_id')
    donors = [other for key, other in routes.items()
              if key != receipt and other['state'] == 'complete'
              and other['pending'] is None
              and all(other[k] == row[k] for k in identity)]
    for cell in row['cells']:
        if cell['paid'] and not cell['external'] and cell['unit_number']:
            continue
        if not cell['external'] or cell['paid'] or not cell['unit_number']:
            return False
        anchors = [paid for donor in donors for paid in donor['cells']
                   if paid['paid'] and not paid['external']
                   and paid['unit_number'] == cell['unit_number']
                   and paid['position'] == cell['position']]
        if len(anchors) != 1:
            return False
    return True


def shared_connector_handoff(memory):
    """Recognize one returned, paid shared-pole action; never authorize replay."""
    from .skills import Plan
    from .telemetry import fingerprint
    pending = memory.pending or {}
    if (memory.status != 'uncertain'
            or memory.reason != 'Connector route needs exact reconciliation'
            or pending.get('action') != 'factory_connect'
            or pending.get('dispatch') != 'returned'
            or not memory.active_plan or type(memory.step_index) is not int or memory.step_index != 0
            or any(getattr(memory, k, None) is not None for k in (
                'native_pending', 'native_attempt', 'background_job',
                'background_attempt', 'background_step', 'transfer_recovery'))):
        return False
    plan = Plan.from_dict(memory.active_plan)
    if len(plan.steps) != 1 or plan.steps[0].action != 'factory_connect':
        return False
    step = plan.steps[0]
    attempt = memory.attempt or {}
    if (not attempt.get('id') or attempt.get('action') != step.action
            or attempt.get('plan_id') != plan.id or attempt.get('step_index') != 0
            or attempt.get('started_tick') != pending.get('started_tick')
            or attempt.get('step_sha256') != fingerprint(memory.active_plan['steps'][0])
            or not memory.connector_ownership
            or memory.connector_ownership['session_id'] != memory.session_id):
        return False
    if not pending_owned(memory, step):
        return False
    routes = memory.connector_ownership['routes']
    row = routes[connection_key(step.parameters or {})]
    return bool(row['external'] > 0 and all(route_paid_coverage(routes, key) for key in routes))
