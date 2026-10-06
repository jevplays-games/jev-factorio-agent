"""Nonmutating guards for manual furnaces and unspent optional proposals.

Production sites own only their original furnace at this boundary. Output,
input and outpost component ownership must remain empty. No observer, survey,
registration or reconciliation mutator is called to manufacture that boundary.
"""

SETTLED_FACTORY_GUARDS = r'''
local settled={sites={},output_offers={},outpost_offers={}}
local function empty(t) return type(t)=="table" and next(t)==nil end
local function keys(row,names)
 assert(type(row)=="table");local allowed={}
 for name in string.gmatch(names,"%S+") do allowed[name]=true end
 for name in pairs(row) do assert(allowed[name],"Unknown retained owner field") end
end
local function point(p)
 assert(type(p)=="table" and type(p.x)=="number" and type(p.y)=="number"
  and p.x==p.x and p.y==p.y and math.abs(p.x)<=1000000 and math.abs(p.y)<=1000000)
 return {x=p.x,y=p.y}
end
local function same(x,y) return x.x==y.x and x.y==y.y end
local function source(role,row,output_offer)
 -- Steel is supported by output_buffers, not the ore-site ownership survey.
 assert(role=="recipe:iron-plate" or role=="recipe:copper-plate"
  or (output_offer and role=="recipe:steel-plate"))
 local e=assert(c.entities[role])
 assert(e.valid and e==row.entity and e.name=="stone-furnace"
  and type(row.source_unit)=="number" and row.source_unit>0
  and row.source_unit%1==0 and e.unit_number==row.source_unit
  and e.surface==a.surface and e.force==a.force)
 return e
end
assert(b and b.protocol==1 and empty(b.cells) and type(b.offers)=="table")
assert(i and i.protocol==1 and empty(i.cells) and empty(i.offers))
assert(sites and sites.protocol==1 and empty(sites.offers) and type(sites.owned)=="table")
assert(o and o.protocol==1 and empty(o.cells) and empty(o.receipts) and type(o.offers)=="table")
for role,row in pairs(sites.owned) do
 keys(row,"role ore item anchor position surface force resource resource_position specs input_steps output_arm chest belt_count checks entity source_unit")
 local e=source(role,row);local pos=point(row.position)
 assert(row.role==role and row.item==string.sub(role,8)
  and row.ore==(role=="recipe:iron-plate" and "iron-ore" or "copper-ore")
  and row.surface==a.surface and row.force==a.force and same(e.position,pos)
  and pos.x%1==0 and pos.y%1==0
  and type(row.anchor)=="string" and #row.anchor<=128 and string.sub(row.anchor,1,10)=="cell-site:"
  and type(row.belt_count)=="number" and row.belt_count%1==0 and row.belt_count>=1 and row.belt_count<=64)
 settled.sites[role]={source_unit=row.source_unit,anchor=row.anchor,position=pos,belt_count=row.belt_count}
end
for role,row in pairs(b.offers) do
 keys(row,"source item source_unit source_position entity layout chest_position inserter_position direction chest_role inserter_role parts")
 local e=source(role,row,true);local pos=point(row.source_position)
 assert(row.source==role and row.item==string.sub(role,8) and same(e.position,pos)
  and empty(row.parts) and type(row.layout)=="string" and #row.layout<=128
  and string.sub(row.layout,1,7)=="output:"
  and row.chest_role=="output-chest:"..row.source_unit
  and row.inserter_role=="output-arm:"..row.source_unit
  and not c.entities[row.chest_role] and not c.entities[row.inserter_role]
  and (row.direction==0 or row.direction==4 or row.direction==8 or row.direction==12))
 settled.output_offers[role]={source_unit=row.source_unit,layout=row.layout,
  source_position=pos,chest_position=point(row.chest_position),
  inserter_position=point(row.inserter_position),direction=row.direction}
end
for resource,row in pairs(o.offers) do
 keys(row,"resource layout surface force steps parts patch")
 assert((resource=="iron-ore" or resource=="copper-ore") and row.resource==resource
  and row.surface==a.surface and row.force==a.force and empty(row.parts)
  and type(row.layout)=="string" and #row.layout<=128
  and string.sub(row.layout,1,#("outpost:"..resource..":"))=="outpost:"..resource..":"
  and type(row.steps)=="table" and #row.steps==2)
 local steps={}
 for index,spec in ipairs(row.steps) do
  keys(spec,"part name direction position")
  assert(spec.part==(index==1 and "chest" or "drill")
   and spec.name==(index==1 and "wooden-chest" or "burner-mining-drill")
   and (spec.direction==0 or spec.direction==4 or spec.direction==8 or spec.direction==12)
   and not c.entities["outpost:"..resource..":"..spec.part])
  steps[index]={part=spec.part,name=spec.name,direction=spec.direction,position=point(spec.position)}
 end
 settled.outpost_offers[resource]={layout=row.layout,steps=steps}
end
'''


def settled_factory_guards(output_commitments=None):
    if not output_commitments:
        return SETTLED_FACTORY_GUARDS
    from .native_paid_output_attachment import PAID_OUTPUT_GUARDS
    from .native_unspent_input_attachment import UNSPENT_INPUT_GUARDS
    empty_buffers = 'assert(b and b.protocol==1 and empty(b.cells) and type(b.offers)=="table")'
    empty_inputs = 'assert(i and i.protocol==1 and empty(i.cells) and empty(i.offers))'
    assert SETTLED_FACTORY_GUARDS.count(empty_buffers) == 1
    assert SETTLED_FACTORY_GUARDS.count(empty_inputs) == 1
    return SETTLED_FACTORY_GUARDS.replace(empty_buffers, PAID_OUTPUT_GUARDS).replace(
        empty_inputs, UNSPENT_INPUT_GUARDS)
