"""Read-only reattachment to completed, checkpoint-owned ordinary connectors.

This is not an owner migration. All optional owner registries must still be
empty. Partial/faulted/uncheckpointed routes require their existing recovery.
"""
from copy import deepcopy
from types import SimpleNamespace

from ..connector_checkpoint import capture, validate_binding
from ..iteration_timing import decode_native
from .native_current_attachment import current_connector_snapshot_command


def qualify_completed_connectors(client, result, checkpoint_binding):
    saved = deepcopy(validate_binding(checkpoint_binding, result['session_id']))
    routes = saved['routes']
    if (not routes or any(row['state'] != 'complete' or not row['owned']
                          or row.get('pending') is not None
                          or row['actor_unit'] != result['actor_unit']
                          for row in routes.values())):
        raise RuntimeError('Completed connector checkpoint requires reconciliation')
    command = current_connector_snapshot_command(result, completed_routes=True)

    def read():
        row = decode_native(client.send_command(command))
        if (not isinstance(row, dict)
                or set(row) != {'schema', 'session_id', 'actor_unit', 'tick', 'connector_ownership'}
                or type(row['schema']) is not int or row['schema'] != 1
                or row['session_id'] != result['session_id']
                or type(row['actor_unit']) is not int or row['actor_unit'] != result['actor_unit']
                or type(row['tick']) is not int or row['tick'] < 1):
            raise RuntimeError('Completed connector snapshot identity changed')
        owned = row['connector_ownership']
        if (not isinstance(owned, dict)
                or set(owned) != {'protocol', 'session_id', 'tick', 'routes'}
                or type(owned['protocol']) is not int or owned['protocol'] != 1
                or owned['session_id'] != result['session_id']
                or type(owned['tick']) is not int or owned['tick'] != row['tick']
                or not isinstance(owned['routes'], dict)
                or set(owned['routes']) != set(routes)):
            raise RuntimeError('Completed connector snapshot differs from checkpoint')
        return row

    before = read()
    native = SimpleNamespace(command=lambda script: client.send_command('/sc ' + script))
    for receipt, expected in routes.items():
        current = capture(native, receipt, before['connector_ownership']['routes'][receipt])
        if current != expected:
            raise RuntimeError('Completed connector paid cells differ from checkpoint')
    after = read()
    if (after['tick'] < before['tick'] or after['connector_ownership']['routes']
            != before['connector_ownership']['routes']):
        raise RuntimeError('Completed connector ledger changed during attachment')
    return {**result, 'connector_snapshot_qualified': True,
            'connector_snapshot_tick': after['tick'],
            'connector_snapshot_ownership': after['connector_ownership']}
