"""Read-only qualification of exact checkpoint-paid ordinary output components."""
from copy import deepcopy
import json
import math

from ..output_buffers import validate_commitments


def binding_prefix(commitments):
    bound = {} if commitments is None else deepcopy(commitments)
    validate_commitments(bound)
    if any(not row['parts'] for row in bound.values()):
        raise ValueError('Output attachment requires retained paid components')
    encoded = json.dumps(json.dumps(bound, sort_keys=True))
    return ('local bound_outputs=helpers.json_to_table(' + encoded + ');'
            'local allowed_output_roles={};for _,row in pairs(bound_outputs) do '
            'for _,paid in pairs(row.parts) do allowed_output_roles[paid.role]=true end end;')


def validate_snapshot(rows, commitments):
    validate_commitments(commitments)
    if not isinstance(rows, dict) or set(rows) != set(commitments):
        raise RuntimeError('Native output owners differ from checkpoint')
    for role, row in rows.items():
        if (not isinstance(row, dict) or set(row) != {
                'source_unit', 'layout', 'parts', 'source_position',
                'chest_position', 'inserter_position', 'direction'}
                or {key:row[key] for key in ('source_unit','layout','parts')} != commitments[role]
                or type(row['direction']) is not int or row['direction'] not in (0,4,8,12)):
            raise RuntimeError('Native paid output evidence differs from checkpoint')
        for key in ('source_position','chest_position','inserter_position'):
            point = row[key]
            if (not isinstance(point, dict) or set(point) != {'x','y'}
                    or any(type(value) not in (int,float) or not math.isfinite(value)
                           or abs(value)>1_000_000 for value in point.values())):
                raise RuntimeError('Invalid paid output geometry')


PAID_OUTPUT_GUARDS = r'''
assert(b and b.protocol==1 and type(b.cells)=="table" and type(b.offers)=="table")
settled.output_cells={}
for role,bound in pairs(bound_outputs) do assert(b.cells[role],"Saved output owner missing") end
for role,row in pairs(b.cells) do
 local bound=assert(bound_outputs[role],"Uncheckpointed output owner")
 keys(row,"source item source_unit source_position entity layout chest_position inserter_position direction chest_role inserter_role parts built_tick previous positive received first_tick flow fault")
 local e=source(role,row,true);local pos=point(row.source_position)
 assert(row.source==role and row.item==string.sub(role,8) and same(e.position,pos)
  and row.source_unit==bound.source_unit and row.layout==bound.layout
  and type(row.layout)=="string" and string.sub(row.layout,1,7)=="output:"
  and row.chest_role=="output-chest:"..row.source_unit
  and row.inserter_role=="output-arm:"..row.source_unit
  and (row.direction==0 or row.direction==4 or row.direction==8 or row.direction==12)
  and row.fault==nil and b.offers[role]==nil
  and type(row.built_tick)=="number" and row.built_tick%1==0
  and row.built_tick>0 and row.built_tick<=game.tick)
 local recipe=e.get_recipe()
 assert(not recipe or recipe.name==row.item,"Output source recipe changed")
 local chest_pos=point(row.chest_position);local arm_pos=point(row.inserter_position)
 assert(not same(chest_pos,arm_pos))
 keys(row.parts,"chest inserter")
 local parts={}
 for part,paid in pairs(bound.parts) do
  local entry=assert(row.parts[part],"Saved output part missing")
  keys(entry,"entity role unit_number receipt paid")
  local entity=assert(entry.entity)
  local expected_role=part=="chest" and row.chest_role or row.inserter_role
  local name=part=="chest" and "wooden-chest" or "burner-inserter"
  assert(entry.role==expected_role and entry.role==paid.role
   and entry.unit_number==paid.unit_number and entry.receipt==paid.receipt
   and entry.paid==1 and paid.paid==1 and c.entities[entry.role]==entity
   and entity.valid and entity.name==name and entity.unit_number==paid.unit_number
   and entity.force==a.force and entity.surface==a.surface
   and same(entity.position,part=="chest" and chest_pos or arm_pos),
   "Paid output component differs from checkpoint")
  if part=="inserter" then
   assert(entity.direction==row.direction and row.parts.chest
    and entity.pickup_target==e and entity.drop_target==row.parts.chest.entity,
    "Paid output topology changed")
  end
  parts[part]={role=entry.role,unit_number=entry.unit_number,receipt=entry.receipt,paid=entry.paid}
 end
 for part in pairs(row.parts) do assert(bound.parts[part],"Uncheckpointed output part") end
 if not bound.parts.inserter then assert(not c.entities[row.inserter_role]) end
 settled.output_cells[role]={source_unit=row.source_unit,layout=row.layout,parts=parts,
  source_position=pos,chest_position=chest_pos,inserter_position=arm_pos,direction=row.direction}
end
'''
