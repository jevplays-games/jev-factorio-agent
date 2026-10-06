"""Current paid buffer identity for recipe-demand evidence; never dispatch authority."""
from copy import deepcopy

from ..output_buffers import sources, flow_complete, validate_commitments


def identity(snapshot, role, item):
    try:
        rows = [row for row in sources(snapshot).values()
                if row.get('chest_role') == role and row.get('item') == item
                and row.get('source', '').startswith('recipe:')]
        if len(rows) != 1:
            return None
        row = rows[0]
        source = row['source']
        parts = row['parts']
        if (set(parts) != {'chest', 'inserter'} or parts['chest']['role'] != role
                or not flow_complete(source, row['layout'], snapshot)):
            return None
        validate_commitments({source: {'source_unit': row['source_unit'],
                                      'layout': row['layout'], 'parts': parts}})
        return {'source_role': source, 'source_unit': row['source_unit'],
                'layout': row['layout'], 'parts': deepcopy(parts),
                'flow': deepcopy(row['flow'])}
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
