"""Owned drill-to-chest outposts; forecasts never prove native ore production."""
from __future__ import annotations

import math

COMMAND = "factory_outpost_build"
FIELDS = {"resource", "layout", "part", "receipt"}
EFFECTS = {"outpost_component", "outpost_flow"}
RESOURCES = {"iron-ore": "recipe:iron-plate", "copper-ore": "recipe:copper-plate"}
PARTS = {"chest": "wooden-chest", "drill": "burner-mining-drill"}


def integer(value: object, low: int = 0, high: int = 2**53 - 1) -> bool:
    return type(value) is int and low <= value <= high


def text(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128


def role(resource: str, part: str) -> str:
    return f"outpost:{resource}:{part}"


def validate(parameters: dict) -> None:
    if (not isinstance(parameters, dict) or set(parameters) != FIELDS
            or not all(text(value) for value in parameters.values())
            or parameters['resource'] not in RESOURCES or parameters['part'] not in PARTS):
        raise ValueError("Invalid mining-outpost command")


def sources(snapshot) -> dict:
    data = snapshot.factory.get('mining_outposts')
    if (not isinstance(data, dict) or not integer(data.get('protocol'), 1, 1)
            or data.get('session_id') != snapshot.session_id
            or not integer(data.get('tick')) or data['tick'] != snapshot.tick
            or not isinstance(data.get('sources'), dict)
            or not set(data['sources']).issubset(RESOURCES)):
        raise ValueError("Missing or stale mining-outpost telemetry")
    units, receipts = set(), set()
    for resource, row in data['sources'].items():
        if (not isinstance(row, dict) or row.get('resource') != resource
                or not text(row.get('layout')) or not integer(row.get('surface_index'), 1)
                or not integer(row.get('force_index'), 1)
                or row.get('state') not in {'proposed', 'building', 'ready', 'depleted', 'fault'}
                or type(row.get('topology')) is not bool
                or not integer(row.get('remaining'))
                or not isinstance(row.get('parts'), dict)
                or not set(row['parts']).issubset(PARTS)
                or not isinstance(row.get('steps'), list) or len(row['steps']) != 2
                or not isinstance(row.get('flow'), dict)):
            raise ValueError("Invalid mining-outpost row")
        if set(row['parts']) not in (set(), {'chest'}, {'chest', 'drill'}):
            raise ValueError("Outpost construction is not a paid prefix")
        if (row['state'] == 'proposed' and row['parts']
                or row['state'] in {'ready', 'depleted'} and (len(row['parts']) != 2 or not row['topology'])
                or row['state'] == 'depleted' and row['remaining'] != 0):
            raise ValueError("Inconsistent mining-outpost phase")
        points = []
        for spec, part in zip(row['steps'], PARTS):
            if not isinstance(spec, dict):
                raise ValueError("Invalid outpost geometry")
            point = spec.get('position')
            if (spec.get('part') != part or spec.get('name') != PARTS[part]
                    or not isinstance(point, dict) or set(point) != {'x', 'y'}
                    or any(type(v) not in {int, float} or not math.isfinite(v)
                           or abs(v) > 1_000_000 or v % 1 != (0.5 if part == 'chest' else 0)
                           for v in point.values())
                    or type(spec.get('direction')) is not int
                    or spec['direction'] not in ({0} if part == 'chest' else {0, 4, 8, 12})):
                raise ValueError("Invalid outpost geometry")
            points.append(point)
            paid = row['parts'].get(part)
            if paid is not None:
                if (not isinstance(paid, dict) or set(paid) != {'role', 'unit_number', 'receipt', 'paid'}
                        or paid['role'] != role(resource, part) or not integer(paid['unit_number'], 1)
                        or not integer(paid['paid'], 1, 1) or not text(paid['receipt'])
                        or paid['unit_number'] in units or paid['receipt'] in receipts):
                    raise ValueError("Invalid outpost payment or ownership")
                units.add(paid['unit_number'])
                receipts.add(paid['receipt'])
        if all(abs(points[0][axis] - points[1][axis]) < 1.49 for axis in ('x', 'y')):
            raise ValueError("Overlapping outpost footprints")
    return data['sources']


def current(row: dict, snapshot) -> bool:
    if row['state'] == 'fault':
        return False
    for spec in row['steps']:
        part = row['parts'].get(spec['part'])
        if part:
            entity = snapshot.factory.get('entities', {}).get(part['role'], {})
            if (entity.get('unit_number') != part['unit_number'] or entity.get('name') != spec['name']
                    or entity.get('position') != spec['position']):
                return False
    return True


def flow_complete(resource: str, layout: str, snapshot) -> bool:
    try:
        row = sources(snapshot).get(resource)
        if (not row or row['layout'] != layout or not current(row, snapshot)
                or row['state'] not in {'ready', 'depleted'} or not row['topology']):
            return False
        proof = row['flow']
        return (proof.get('layout') == layout and proof.get('conservation') is True
                and all(integer(proof.get(key)) for key in
                        ('drill_unit', 'chest_unit', 'first_tick', 'last_tick', 'positive_samples', 'mined', 'received'))
                and proof['drill_unit'] == row['parts']['drill']['unit_number']
                and proof['chest_unit'] == row['parts']['chest']['unit_number']
                and 120 <= proof['last_tick'] - proof['first_tick']
                and proof['last_tick'] <= snapshot.tick
                and proof['positive_samples'] >= 3 and proof['mined'] == proof['received'] >= 3)
    except (ValueError, KeyError, TypeError):
        return False


def remaining_kit(row: dict) -> dict[str, int]:
    return {name: 1 for part, name in PARTS.items() if part not in row['parts']}


def allowed(parameters: dict, snapshot) -> bool:
    validate(parameters)
    try:
        row = sources(snapshot).get(parameters['resource'])
        todo = [part for part in PARTS if row and part not in row['parts']]
        return bool(row and current(row, snapshot) and row['layout'] == parameters['layout']
                    and todo and todo[0] == parameters['part']
                    and row['remaining'] >= 100
                    and all(snapshot.inventory.get(name, 0) >= count for name, count in remaining_kit(row).items())
                    and snapshot.inventory.get('coal', 0) >= 5
                    and snapshot.factory.get('player_bound') is True
                    and snapshot.factory.get('player_connected') is True
                    and snapshot.factory.get('crafting_queue') == 0)
    except (ValueError, KeyError, TypeError):
        return False


def component_complete(parameters: dict, snapshot) -> bool:
    validate(parameters)
    try:
        row = sources(snapshot).get(parameters['resource'])
        return bool(row and current(row, snapshot) and row['layout'] == parameters['layout']
                    and row['parts'].get(parameters['part'], {}).get('receipt') == parameters['receipt'])
    except (ValueError, KeyError, TypeError):
        return False


def permits(action: str, parameters: dict, snapshot) -> bool:
    """Protect ownership, commissioning, and direct-route precedence."""
    rows = sources(snapshot)
    if any(not current(row, snapshot) for row in rows.values()):
        return False
    for resource, row in rows.items():
        target = parameters.get('role', '')
        if target.startswith('outpost:'):
            if target == role(resource, 'drill'):
                return (action == 'factory_insert' and parameters.get('item') == 'coal'
                        and row['topology'] and len(row['parts']) == 2)
            if target == role(resource, 'chest'):
                return (action == 'factory_extract' and parameters.get('item') == resource
                        and flow_complete(resource, row['layout'], snapshot))
        if (action == 'factory_gather' and parameters.get('resource') == resource
                and 'drill' in row['parts'] and row['state'] != 'depleted'):
            return False
    if str(parameters.get('role', '')).startswith('outpost:'):
        return False
    if action == COMMAND:
        # A live proposal is enough to supersede an unpaid outpost selection.
        # Once the chest is paid, keep the receipt-bound prefix continuable.
        direct = snapshot.factory.get('input_routes', {}).get('sources', {}).get(
            RESOURCES.get(parameters.get('resource')))
        if direct:
            if direct.get('state') != 'proposed':
                return False
            outpost = rows.get(parameters.get('resource'))
            if not outpost or not outpost['parts']:
                return False
    if action == 'factory_input_build':
        resource = next((ore for ore, source in RESOURCES.items() if source == parameters.get('source')), None)
        if resource in rows and rows[resource]['state'] != 'proposed':
            return False
    return True
