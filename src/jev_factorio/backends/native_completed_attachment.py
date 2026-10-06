"""Read-only reattachment to completed, checkpoint-owned ordinary connectors.

This is not an owner migration. Optional component ownership must still be
empty; native manual furnaces and unspent proposals have a separate read-only
qualification. Partial/faulted/uncheckpointed routes require existing recovery.
"""
from copy import deepcopy
from types import SimpleNamespace

from ..connector_checkpoint import capture, validate_binding, route_paid_coverage
from ..iteration_timing import decode_native
from .native_current_attachment import current_connector_snapshot_command


def qualify_completed_connectors(client, result, checkpoint_binding, *, completed_craft=None,
                                 background_craft=None, output_commitments=None):
    saved = deepcopy(validate_binding(checkpoint_binding, result['session_id']))
    routes = saved['routes']
    if (not routes or any(not route_paid_coverage(routes, receipt)
                          or row.get('pending') is not None
                          or row['actor_unit'] != result['actor_unit']
                          for receipt, row in routes.items())):
        raise RuntimeError('Completed connector checkpoint requires reconciliation')
    command = current_connector_snapshot_command(result, completed_routes=True,
                                                 completed_craft=completed_craft, background_craft=background_craft,
                                                 output_commitments=output_commitments)

    fields = {'schema', 'session_id', 'actor_unit', 'tick', 'connector_ownership',
              'settled_factory', 'completed_craft'}
    if completed_craft and 'step_sha256' in completed_craft:
        fields.add('completed_craft_recipe')

    if background_craft is not None:
        fields.add('background_inventory')

    def read():
        row = decode_native(client.send_command(command))
        if (not isinstance(row, dict)
                or set(row) != fields
                or type(row['schema']) is not int or row['schema'] != 1
                or row['session_id'] != result['session_id']
                or type(row['actor_unit']) is not int or row['actor_unit'] != result['actor_unit']
                or type(row['tick']) is not int or row['tick'] < 1):
            raise RuntimeError('Completed connector snapshot identity changed')
        from .native_completed_craft import verify_completed_craft, verify_background_craft
        if background_craft is not None:
            verify_background_craft(row['completed_craft'], row['background_inventory'],
                                    background_craft, result, row['tick'])
        else:
            verify_completed_craft(row['completed_craft'], completed_craft, result, row['tick'],
                                   recipe=row.get('completed_craft_recipe'))
        settled = row['settled_factory']
        expected_fields = {'sites', 'output_offers', 'outpost_offers'}
        if output_commitments:
            expected_fields.add('output_cells')
        if (not isinstance(settled, dict)
                or set(settled) != expected_fields):
            raise RuntimeError('Settled factory qualification is missing')
        for name, values in settled.items():
            if values == []:
                settled[name] = {}
            # Output buffers support iron, copper and steel; the ore registries
            # remain limited to their two original roles.
            limit = 3 if name in {'output_offers', 'output_cells'} else 2
            if not isinstance(settled[name], dict) or len(settled[name]) > limit:
                raise RuntimeError('Settled factory qualification exceeds its bound')
        if output_commitments:
            from .native_paid_output_attachment import validate_snapshot
            validate_snapshot(settled['output_cells'], output_commitments)
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
            != before['connector_ownership']['routes']
            or after['settled_factory'] != before['settled_factory']
            or after['completed_craft'] != before['completed_craft']
            or after.get('completed_craft_recipe') != before.get('completed_craft_recipe')
            or after.get('background_inventory') != before.get('background_inventory')):
        raise RuntimeError('Completed connector ledger changed during attachment')
    return {**result, 'connector_snapshot_qualified': True,
            'connector_snapshot_tick': after['tick'],
            'connector_snapshot_ownership': after['connector_ownership']}
