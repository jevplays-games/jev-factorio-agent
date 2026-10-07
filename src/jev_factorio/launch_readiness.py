"""Base-2.0.77 launch evidence and bounded commands; native receipts own success."""
from __future__ import annotations

import math

SILO = 'recipe:rocket-part'
PAD = 'utility:landing-pad'
PAYLOADS = ('raw-fish', 'satellite')
COMMANDS = {
    'factory_launch_pad': {'site', 'receipt'},
    'factory_launch_fish': {'target', 'receipt'},
    'factory_launch_payload': {'role', 'silo_unit', 'rocket_unit', 'item', 'receipt'},
}
EFFECTS = {'launch_pad', 'launch_fish', 'launch_payload'}


def integer(value, low=0):
    return type(value) is int and value >= low


def text(value):
    return isinstance(value, str) and 0 < len(value) <= 128


def point(value):
    return (isinstance(value, dict) and set(value) == {'x', 'y'}
            and all(type(v) in {int, float} and math.isfinite(v) for v in value.values()))


def validate(action: str, parameters: dict) -> None:
    if action not in COMMANDS or not isinstance(parameters, dict) or set(parameters) != COMMANDS[action]:
        raise ValueError('Invalid launch command fields')
    for key, value in parameters.items():
        if key in {'silo_unit', 'rocket_unit'}:
            if not integer(value, 1): raise ValueError('Invalid launch entity identity')
        elif not text(value): raise ValueError('Invalid launch identifier')
    if action == 'factory_launch_payload' and (parameters['role'] != SILO or parameters['item'] not in PAYLOADS):
        raise ValueError('Unsupported launch cargo')


def evidence(snapshot) -> dict:
    value = snapshot.factory.get('launch_readiness')
    if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1
            or value.get('supported') is not True or value.get('version') != '2.0.77'
            or value.get('session_id') != snapshot.session_id
            or type(value.get('tick')) is not int or value['tick'] != snapshot.tick
            or any(not integer(value.get(k), 1) for k in ('actor_unit', 'surface_index', 'force_index'))
            or type(value.get('fault')) is not bool
            or not isinstance(value.get('attempts'), dict) or not isinstance(value.get('receipts'), dict)):
        raise ValueError('Missing or incompatible launch-readiness evidence')
    for name in ('pad', 'pad_site', 'fish', 'silo'):
        if not isinstance(value.get(name), dict): raise ValueError('Invalid launch observation')
    pad = value['pad']
    if pad and (pad.get('name') != 'cargo-landing-pad' or not integer(pad.get('unit_number'), 1)
                or not point(pad.get('position')) or not isinstance(pad.get('accepts'), dict)
                or set(pad['accepts']) != set(PAYLOADS)
                or any(type(v) is not bool for v in pad['accepts'].values())):
        raise ValueError('Invalid landing pad identity')
    site, fish, silo = value['pad_site'], value['fish'], value['silo']
    if site and (not text(site.get('id')) or not point(site.get('position'))):
        raise ValueError('Invalid landing pad site')
    if fish and (not text(fish.get('id')) or not point(fish.get('position'))
                 or fish.get('reachable') is not True or type(fish.get('yield')) is not int or fish['yield'] != 5):
        raise ValueError('Invalid reachable fish observation')
    if silo:
        machine = snapshot.factory.get('entities', {}).get(SILO, {})
        if (not integer(silo.get('unit_number'), 1) or machine.get('name') != 'rocket-silo'
                or type(machine.get('unit_number')) is not int or machine['unit_number'] != silo['unit_number']
                or not integer(silo.get('rocket_unit'))
                or any(type(silo.get(k)) is not bool for k in ('ready', 'cargo_available', 'automatic'))
                or not isinstance(silo.get('cargo'), dict)
                or any(not text(k) or not integer(v) for k, v in silo['cargo'].items())):
            raise ValueError('Invalid rocket identity or cargo observation')
    return value


def payload(silo: dict) -> str | None:
    cargo = silo.get('cargo', {})
    # Do not remove unexpected cargo to manufacture a successful launch.
    if len(cargo) != 1 or set(cargo) - set(PAYLOADS): return None
    return next((item for item in PAYLOADS if type(cargo.get(item)) is int and cargo[item] == 1), None)


def load_reconciled(snapshot, row: dict | None = None) -> bool:
    """Bind retained paid-load history to its exact current cargo receipt.

    A legacy observation may contain an already-loaded payload without a load
    attempt. Once the producer records an attempt, however, only its matching
    receipt from the same session/actor/tick and same silo/rocket/item proves
    that this payload transfer completed. The Lua producer does not emit a
    separate source identifier or cryptographic attestation, so this validates
    only the producer's existing typed envelope.
    """
    try:
        row = evidence(snapshot) if row is None else row
        attempts, receipts = row['attempts'], row['receipts']
        if not isinstance(attempts, dict) or not isinstance(receipts, dict):
            return False
        load_receipts = [(key, value) for key, value in receipts.items()
                         if isinstance(value, dict) and value.get('kind') == 'load']
        if 'load' not in attempts:
            return not load_receipts

        attempt = attempts['load']
        attempt_fields = {'receipt', 'silo_unit', 'rocket_unit', 'item', 'tick'}
        if (not isinstance(attempt, dict) or set(attempt) != attempt_fields
                or not text(attempt.get('receipt'))
                or not integer(attempt.get('silo_unit'), 1)
                or not integer(attempt.get('rocket_unit'), 1)
                or attempt.get('item') not in PAYLOADS
                or not integer(attempt.get('tick'))):
            return False
        if len(load_receipts) != 1 or load_receipts[0][0] != attempt['receipt']:
            return False

        receipt = receipts.get(attempt['receipt'])
        receipt_fields = {'kind', 'session_id', 'actor_unit', 'tick', 'item', 'quantity',
                          'silo_unit', 'rocket_unit'}
        silo = row.get('silo')
        item = payload(silo) if isinstance(silo, dict) else None
        if (not isinstance(receipt, dict) or set(receipt) != receipt_fields
                or receipt.get('kind') != 'load'
                or receipt.get('session_id') != row.get('session_id')
                or type(receipt.get('actor_unit')) is not int
                or receipt['actor_unit'] != row.get('actor_unit')
                or not integer(receipt.get('tick'))
                or receipt['tick'] != attempt['tick'] or receipt['tick'] > row.get('tick')
                or receipt.get('item') != attempt['item'] or receipt.get('item') != item
                or type(receipt.get('quantity')) is not int or receipt['quantity'] != 1
                or not integer(receipt.get('silo_unit'), 1)
                or receipt['silo_unit'] != attempt['silo_unit']
                or not integer(receipt.get('rocket_unit'), 1)
                or receipt['rocket_unit'] != attempt['rocket_unit']
                or not isinstance(silo, dict)
                or silo.get('unit_number') != attempt['silo_unit']
                or silo.get('rocket_unit') != attempt['rocket_unit']):
            return False
        return True
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _reservation_evidence(snapshot) -> dict:
    """Validate current, composed identities before inferring payload ownership.

    The launch action gates intentionally continue to use ``evidence`` alone;
    this stricter cross-observer binding is specific to creating a new carried
    payload reservation. Missing optional fields remain compatible with older
    snapshots that did not install the craft-jobs observer.
    """
    row = evidence(snapshot)
    version = getattr(snapshot, 'game_version', None)
    if version is not None and (not text(version) or version != row['version']):
        raise ValueError('Launch reservation version conflicts with current snapshot')

    factory = snapshot.factory
    protocol_present = 'craft_jobs_protocol' in factory
    actor_present = 'craft_job_actor' in factory
    if not protocol_present and not actor_present:
        return row
    if (type(factory.get('craft_jobs_protocol')) is not int
            or factory['craft_jobs_protocol'] != 1
            or not actor_present):
        raise ValueError('Invalid composed craft-jobs identity evidence')

    actor = factory.get('craft_job_actor')
    fields = {'session_id', 'player_index', 'unit_number', 'surface_index', 'force_index'}
    if not isinstance(actor, dict) or set(actor) != fields:
        raise ValueError('Invalid composed craft actor identity')
    if (not text(actor.get('session_id'))
            or any(not integer(actor.get(key), 1)
                   for key in ('player_index', 'unit_number', 'surface_index', 'force_index'))
            or actor['session_id'] != row['session_id']
            or actor['unit_number'] != row['actor_unit']
            or actor['surface_index'] != row['surface_index']
            or actor['force_index'] != row['force_index']):
        raise ValueError('Composed craft actor conflicts with launch actor')
    return row


def ready(snapshot) -> bool:
    try:
        row = evidence(snapshot)
    except (ValueError, TypeError, KeyError):
        return False
    silo = row['silo']
    return bool(snapshot.factory.get('player_bound') is True
                and snapshot.factory.get('player_connected') is True
                and not row['fault'] and row['pad'] and silo.get('ready') is True
                and silo.get('cargo_available') is True and integer(silo.get('rocket_unit'), 1)
                and silo.get('automatic') is False and payload(silo)
                and row['pad']['accepts'].get(payload(silo)) is True
                and not row['attempts'].get('launch')
                and load_reconciled(snapshot, row))


def allowed(action: str, parameters: dict, snapshot) -> bool:
    validate(action, parameters)
    try:
        row = evidence(snapshot)
    except (ValueError, TypeError, KeyError):
        return False
    if row['fault'] or row['receipts'].get(parameters['receipt']): return False
    if snapshot.factory.get('player_bound') is not True or snapshot.factory.get('player_connected') is not True:
        return False
    if action == 'factory_launch_pad':
        return bool(not row['pad'] and row['pad_site'].get('id') == parameters['site']
                    and not row['attempts'].get('pad') and snapshot.inventory.get('cargo-landing-pad', 0) >= 1)
    if action == 'factory_launch_fish':
        return bool(not row['attempts'].get('fish') and row['fish'].get('id') == parameters['target']
                    and snapshot.factory.get('crafting_queue', 0) == 0
                    and not any(snapshot.inventory.get(item, 0) for item in PAYLOADS)
                    and not payload(row['silo']))
    silo = row['silo']
    return bool(row['pad'] and row['pad']['accepts'].get(parameters['item']) is True
                and 'load' not in row['attempts'] and silo.get('ready') is True
                and silo.get('automatic') is False and silo.get('cargo_available') is True
                and silo.get('unit_number') == parameters['silo_unit']
                and silo.get('rocket_unit') == parameters['rocket_unit'] and not silo['cargo']
                and snapshot.inventory.get(parameters['item'], 0) >= 1)


def satisfied(effect: str, action: str, parameters: dict, snapshot) -> bool:
    kinds = {'launch_pad': ('factory_launch_pad', 'pad'), 'launch_fish': ('factory_launch_fish', 'fish'),
             'launch_payload': ('factory_launch_payload', 'load')}
    if effect not in kinds or action != kinds[effect][0]: return False
    try:
        validate(action, parameters)
        row = evidence(snapshot)
        if effect == 'launch_payload' and not load_reconciled(snapshot, row):
            return False
        receipt = row['receipts'].get(parameters['receipt'], {})
        if (receipt.get('kind') != kinds[effect][1] or receipt.get('session_id') != snapshot.session_id
                or type(receipt.get('actor_unit')) is not int or receipt['actor_unit'] != row['actor_unit']
                or not integer(receipt.get('tick')) or receipt['tick'] > snapshot.tick):
            return False
        if effect == 'launch_pad':
            return (receipt.get('site') == parameters['site'] and type(receipt.get('paid')) is int
                    and receipt['paid'] == 1 and type(receipt.get('unit_number')) is int
                    and receipt['unit_number'] == row['pad'].get('unit_number'))
        if effect == 'launch_fish':
            return (receipt.get('target') == parameters['target'] and type(receipt.get('quantity')) is int
                    and receipt['quantity'] == 5 and snapshot.inventory.get('raw-fish', 0) >= 1)
        return (receipt.get('item') == parameters['item'] and type(receipt.get('quantity')) is int
                and receipt['quantity'] == 1 and type(receipt.get('silo_unit')) is int
                and receipt['silo_unit'] == parameters['silo_unit'] == row['silo'].get('unit_number')
                and type(receipt.get('rocket_unit')) is int
                and receipt['rocket_unit'] == parameters['rocket_unit'] == row['silo'].get('rocket_unit')
                and row['silo'].get('cargo', {}).get(parameters['item'], 0) >= 1)
    except (ValueError, KeyError, TypeError):
        return False


def reserved(snapshot) -> dict:
    """Protect one payload only when its launch evidence is currently valid."""
    if snapshot.victory is True: return {}
    try:
        row = _reservation_evidence(snapshot)
    except (ValueError, TypeError, KeyError, AttributeError):
        return {}
    if payload(row['silo']): return {}
    item = next((item for item in PAYLOADS if snapshot.inventory.get(item, 0) >= 1), None)
    return {item: 1} if item else {}


def affordable(action: str, costs: dict, snapshot) -> bool:
    if action == 'factory_launch_payload': return True
    return all(snapshot.inventory.get(item, 0) - count >= reserved(snapshot).get(item, 0)
               for item, count in costs.items())
