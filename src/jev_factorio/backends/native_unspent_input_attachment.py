"""Read-only qualification of unspent input proposals after paid output flow."""

UNSPENT_INPUT_GUARDS = r'''
assert(i and i.protocol==1 and empty(i.cells) and type(i.offers)=="table")
settled.input_offers={}
for role,row in pairs(i.offers) do
 keys(row,"source source_unit source_position entity item ore output_layout layout steps parts belt_count reserve_belts")
 local e=source(role,row);local pos=point(row.source_position)
 local output=assert(b.cells[role],"Input proposal lacks checkpoint-owned output")
 local flow=assert(output.flow,"Input proposal lacks completed output flow")
 assert(row.source==role and row.item==string.sub(role,8) and same(e.position,pos)
  and row.ore==(role=="recipe:iron-plate" and "iron-ore" or "copper-ore")
  and empty(row.parts) and row.reserve_belts==0
  and type(row.layout)=="string" and #row.layout<=128
  and string.sub(row.layout,1,#("input:"..row.source_unit..":"))=="input:"..row.source_unit..":"
  and row.output_layout==output.layout and output.parts.chest and output.parts.inserter
  and flow.layout==output.layout and flow.source_unit==row.source_unit
  and flow.conservation==true and type(flow.positive_samples)=="number"
  and flow.positive_samples%1==0 and flow.positive_samples>=3
  and type(flow.received)=="number" and flow.received%1==0 and flow.received>=3
  and type(flow.first_tick)=="number" and type(flow.last_tick)=="number"
  and flow.first_tick%1==0 and flow.last_tick%1==0
  and flow.first_tick>=0 and flow.last_tick<=game.tick and flow.last_tick-flow.first_tick>=120
  and type(row.belt_count)=="number" and row.belt_count%1==0 and row.belt_count>=1 and row.belt_count<=64
  and type(row.steps)=="table" and #row.steps==row.belt_count+2)
 local steps={};local count=0;local positions={}
 for index in pairs(row.steps) do
  assert(type(index)=="number" and index%1==0 and index>=1 and index<=#row.steps)
  count=count+1
 end
 assert(count==#row.steps)
 for index,spec in ipairs(row.steps) do
  keys(spec,"part name direction position")
  local part=index==1 and "inserter" or index==#row.steps and "drill" or "belt:"..(#row.steps-index)
  local name=index==1 and "burner-inserter" or index==#row.steps and "burner-mining-drill" or "transport-belt"
  local point=point(spec.position);local key=point.x..":"..point.y
  assert(spec.part==part and spec.name==name and not positions[key]
   and (spec.direction==0 or spec.direction==4 or spec.direction==8 or spec.direction==12)
   and not c.entities["input:"..row.source_unit..":"..part])
  positions[key]=true
  steps[index]={part=part,name=name,direction=spec.direction,position=point}
 end
 settled.input_offers[role]={source_unit=row.source_unit,source_position=pos,item=row.item,ore=row.ore,
  output_layout=row.output_layout,layout=row.layout,belt_count=row.belt_count,reserve_belts=0,steps=steps}
end
'''
