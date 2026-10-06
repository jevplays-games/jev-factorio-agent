-- Additive, paid ore outposts. No tick handler, observer wrapper, terrain edits or item grants.
local campaign, fair = storage.campaign, storage.fair
assert(campaign and fair and storage.input_routes, "Mining outposts require input-route support")
local o = storage.mining_outposts or {protocol=1, cells={}, offers={}, checked={}, reasons={}, serial=0, receipts={}}
assert(o.protocol==1, "Unsupported mining-outpost runtime")
storage.mining_outposts=o
local resources={"iron-ore","copper-ore"}
local sources={["iron-ore"]="recipe:iron-plate",["copper-ore"]="recipe:copper-plate"}
local names={chest="wooden-chest",drill="burner-mining-drill"}
local order={"chest","drill"}
local function point(p) return {x=p.x or p[1],y=p.y or p[2]} end
local function same(a,b) return math.abs(a.x-b.x)<0.01 and math.abs(a.y-b.y)<0.01 end
local function rotate(p,n) p=point(p);for _=1,n do p={x=-p.y,y=p.x} end;return p end
local function role(resource,part) return "outpost:"..resource..":"..part end
local function integer(v) return type(v)=="number" and v>=0 and v%1==0 and v<2^53 end
local function extent(spec)
    local prototype=prototypes.entity[spec.name]
    local w,h=prototype and prototype.tile_width or 1,prototype and prototype.tile_height or 1
    if spec.direction==4 or spec.direction==12 then w,h=h,w end
    return w/2-0.01,h/2-0.01
end
local function overlap(a,b)
    local ax,ay=extent(a);local bx,by=extent(b)
    return math.abs(a.position.x-b.position.x)<ax+bx
        and math.abs(a.position.y-b.position.y)<ay+by
end
local function reserved(spec,except)
    for _,collection in ipairs({o.cells,o.offers}) do
        for resource,cell in pairs(collection) do if resource~=except then
            for _,other in ipairs(cell.steps) do if overlap(spec,other) then return true end end
        end end
    end
    return false
end
campaign.mining_outpost_reserved=function(name,position,direction)
    return reserved({name=name,position=position,direction=direction})
end
local function clear(spec,player,resource)
    return not reserved(spec,resource)
        and (not campaign.production_reserved or not campaign.production_reserved(spec.name,spec.position,spec.direction))
        and player.surface.can_place_entity{name=spec.name,position=spec.position,direction=spec.direction,
            force=player.force,build_check_type=defines.build_check_type.manual}
end
local function ore_patch(player,resource,area)
    local patch=player.surface.find_entities_filtered{type="resource",area=area,limit=65}
    assert(#patch>0 and #patch<=64,"Unsupported outpost mining area")
    local total=0
    for _,e in ipairs(patch) do
        local mining=e.prototype and e.prototype.mineable_properties
        local product=mining and mining.products and mining.products[1]
        assert(e.valid and e.name==resource and e.minable and integer(e.amount)
            and mining and not mining.required_fluid and #mining.products==1
            and product.type=="item" and product.name==resource and product.amount==1
            and (not product.probability or product.probability==1),"Unsupported or mixed outpost ore")
        total=total+e.amount
    end
    return patch,total
end
local function remaining(cell)
    local total=0
    for _,e in ipairs(cell.patch) do
        if e.valid then
            assert(e.name==cell.resource and integer(e.amount),"Outpost resource identity changed")
            total=total+e.amount
        end
    end
    return total
end
local function geometry(cell)
    local player=fair.actor()
    assert(player.surface==cell.surface and player.force==cell.force
        and player.force.mining_drill_productivity_bonus==0,"Outpost actor/surface/productivity changed")
    for _,spec in ipairs(cell.steps) do
        local paid=cell.parts[spec.part]
        if paid then
            local e=paid.entity
            assert(e and e.valid and e.name==spec.name and e.unit_number==paid.unit_number
                and e.surface==cell.surface and e.force==cell.force and e.direction==spec.direction
                and same(e.position,spec.position) and campaign.entities[paid.role]==e,
                "Outpost component identity or orientation changed")
        end
    end
    return player
end
local function topology(cell)
    return cell.parts.drill and cell.parts.chest
        and cell.parts.drill.entity.drop_target==cell.parts.chest.entity or false
end
local function survey(resource)
    local player=fair.actor()
    local furnace=campaign.entities[sources[resource]]
    if not furnace or not furnace.valid then return nil,"producer_missing" end
    if storage.input_routes.cells[sources[resource]] or storage.input_routes.offers[sources[resource]] then
        return nil,"direct_route_available"
    end
    if player.force.mining_drill_productivity_bonus~=0 then return nil,"unsupported_productivity" end
    local dp,cp=prototypes.entity["burner-mining-drill"],prototypes.entity["wooden-chest"]
    if not (dp and dp.tile_width==2 and dp.tile_height==2 and dp.vector_to_place_result
        and cp and cp.tile_width==1 and cp.tile_height==1) then return nil,"unsupported_prototypes" end
    local radius=campaign.exploration_radius or 8
    assert(integer(radius) and radius>=1 and radius<=32,"Invalid generated-area bound")
    local candidates=player.surface.find_entities_filtered{name=resource,position={x=0,y=0},radius=radius*32,limit=128}
    table.sort(candidates,function(a,b)
        local da=math.abs(a.position.x-furnace.position.x)+math.abs(a.position.y-furnace.position.y)
        local db=math.abs(b.position.x-furnace.position.x)+math.abs(b.position.y-furnace.position.y)
        if da~=db then return da<db end
        if a.position.x~=b.position.x then return a.position.x<b.position.x end
        return a.position.y<b.position.y
    end)
    for index=1,math.min(8,#candidates) do
        local e=candidates[index]
        if e.valid and e.minable and e.amount>=100 then
            local p={x=math.floor(e.position.x),y=math.floor(e.position.y)}
            -- Conservative area; exact native mining_area is checked again after construction.
            local ok,patch,total=pcall(ore_patch,player,resource,{left_top={x=p.x-3,y=p.y-3},right_bottom={x=p.x+3,y=p.y+3}})
            local other=player.surface.find_entities_filtered{type="mining-drill",position=p,radius=8,limit=1}
            if ok and total>=100 and #other==0 then for turn=0,3 do
                local v=rotate(dp.vector_to_place_result,turn)
                local chest={part="chest",name=names.chest,direction=0,
                    position={x=math.floor(p.x+v.x)+0.5,y=math.floor(p.y+v.y)+0.5}}
                local drill={part="drill",name=names.drill,direction=turn*4,position=p}
                if not overlap(chest,drill) and clear(chest,player,resource) and clear(drill,player,resource) then
                    -- A local standing location is not a promise of a complete native path.
                    local access=false
                    for _,d in ipairs({{x=3,y=0},{x=-3,y=0},{x=0,y=3},{x=0,y=-3}}) do
                        local stand={name="character",direction=0,position={x=p.x+d.x,y=p.y+d.y}}
                        if not overlap(stand,chest) and clear(stand,player,resource) then access=true;break end
                    end
                    if access then
                        o.serial=o.serial+1
                        return {resource=resource,layout="outpost:"..resource..":"..o.serial,
                            surface=player.surface,force=player.force,steps={chest,drill},parts={},patch=patch},"available"
                    end
                end
            end end
        end
    end
    return nil,#candidates==0 and "no_observed_ore_in_generated_area" or "no_supported_outpost_site"
end
local function parameters(p)
    assert(type(p)=="table" and sources[p.resource] and names[p.part],"Invalid outpost command")
    local count=0
    for key,value in pairs(p) do
        assert((key=="resource" or key=="layout" or key=="part" or key=="receipt")
            and type(value)=="string" and #value>0 and #value<=128,"Invalid outpost field")
        count=count+1
    end
    assert(count==4,"Invalid outpost fields")
end
local function preflight(cell,p)
    assert(cell and cell.layout==p.layout and not cell.fault,"Stale outpost offer")
    local player=geometry(cell)
    assert(not storage.input_routes.cells[sources[cell.resource]],"A direct route is already committed")
    local offers=storage.input_routes.offers
    assert(type(offers)=="table","Missing direct-route offer table")
    assert(cell.parts.chest or rawget(offers,sources[cell.resource])==nil,
        "A direct route is available before outpost payment")
    assert(player.crafting_queue_size==0 and remaining(cell)>=100,"Outpost build is busy or ore depleted")
    assert(not cell.parts[p.part] and (p.part=="chest" or cell.parts.chest),"Outpost part exists or predecessor missing")
    assert(not o.receipts[p.receipt],"Outpost receipt already used")
    assert(player.get_item_count("coal")>=5,"Retain commissioning fuel")
    for _,spec in ipairs(cell.steps) do if not cell.parts[spec.part] then
        assert(not campaign.entities[role(cell.resource,spec.part)],"Outpost role already occupied")
        assert(player.get_item_count(spec.name)>=1 and clear(spec,player,cell.resource),"Outpost kit missing or site obstructed")
    end end
    return player
end
campaign.prepare_mining_outpost=function(p)
    parameters(p)
    local cell=o.cells[p.resource] or o.offers[p.resource]
    preflight(cell,p)
    o.cells[p.resource]=cell -- Freeze the observed geometry, not a game mutation.
    local spec=cell.steps[p.part=="chest" and 1 or 2]
    rcon.print(helpers.table_to_json({position=spec.position,name=spec.name}))
end
campaign.build_mining_outpost=function(p)
    parameters(p)
    local cell=o.cells[p.resource]
    local player=preflight(cell,p)
    local spec=cell.steps[p.part=="chest" and 1 or 2]
    local before=player.get_item_count(spec.name)
    fair.place(spec.name,spec.position,spec.direction)
    local entity=player.surface.find_entity(spec.name,spec.position)
    assert(entity and entity.valid and entity.unit_number and player.get_item_count(spec.name)==before-1,
        "Outpost construction was not paid")
    local id=role(cell.resource,p.part)
    campaign.entities[id]=entity
    cell.parts[p.part]={entity=entity,unit_number=entity.unit_number,role=id,receipt=p.receipt,paid=1}
    o.receipts[p.receipt]=entity.unit_number
    cell.built_tick=game.tick
    geometry(cell)
    if p.part=="drill" then
        local patch,total=ore_patch(player,cell.resource,entity.mining_area)
        assert(total>=100,"Exact outpost mining area is depleted")
        cell.patch=patch;cell.initial_ore=total;cell.first_tick=game.tick
        cell.positive=0;cell.previous_stored=0
        assert(cell.parts.chest.entity.get_inventory(defines.inventory.chest).get_item_count()==0,
            "Commissioning requires an empty paid chest")
    end
    rcon.print(helpers.table_to_json({unit_number=entity.unit_number}))
end
local function sample(cell)
    geometry(cell)
    local linked=topology(cell)
    if cell.parts.drill and not linked and game.tick-cell.built_tick>120 then error("Outpost topology changed") end
    if not linked then return false end
    local drill,chest=cell.parts.drill.entity,cell.parts.chest.entity
    if drill.mining_target then
        local found=false;for _,e in ipairs(cell.patch) do if e==drill.mining_target then found=true end end
        assert(found,"Unobserved outpost mining target")
    end
    local inv=chest.get_inventory(defines.inventory.chest)
    local stored=inv.get_item_count(cell.resource)
    assert(inv.get_item_count()==stored,"Foreign outpost chest contents")
    if not cell.flow then
        local mined=cell.initial_ore-remaining(cell)
        assert(integer(mined) and stored<=mined and mined-stored<=1 and stored>=cell.previous_stored,
            "Outpost ore conservation failed")
        if stored>cell.previous_stored then cell.positive=cell.positive+1 end
        cell.previous_stored=stored
        if stored==mined and stored>=3 and cell.positive>=3 and game.tick-cell.first_tick>=120 then
            cell.flow={layout=cell.layout,drill_unit=drill.unit_number,chest_unit=chest.unit_number,
                first_tick=cell.first_tick,last_tick=game.tick,positive_samples=cell.positive,
                mined=mined,received=stored,conservation=true}
        end
    end
    return linked
end
campaign.observe_mining_outposts=function()
    local rows={}
    for _,resource in ipairs(resources) do
        local cell=o.cells[resource] or o.offers[resource]
        if cell and not o.cells[resource] then
            local ok=pcall(function()
                geometry(cell);assert(remaining(cell)>=100)
                for _,s in ipairs(cell.steps) do assert(clear(s,fair.actor(),resource)) end
            end)
            if not ok then cell=nil;o.offers[resource]=nil end
        end
        if not cell and (not o.checked[resource] or game.tick-o.checked[resource]>=300) then
            o.checked[resource]=game.tick
            local ok,offer,reason=pcall(survey,resource)
            cell=ok and offer or nil;o.offers[resource]=cell
            o.reasons[resource]=ok and reason or "unsupported_survey_evidence"
        end
        if cell then
            local ok,linked=pcall(sample,cell)
            if not ok then cell.fault="outpost_identity_topology_or_flow_mismatch";linked=false end
            local parts={}
            for part,paid in pairs(cell.parts) do parts[part]={role=paid.role,unit_number=paid.unit_number,receipt=paid.receipt,paid=paid.paid} end
            local valid,left=pcall(remaining,cell)
            if not valid then cell.fault="outpost_resource_changed";left=0 end
            rows[resource]={resource=resource,layout=cell.layout,surface_index=cell.surface.index,
                force_index=cell.force.index,steps=cell.steps,parts=parts,remaining=left,flow=cell.flow or {},
                topology=linked==true,state=cell.fault and "fault" or
                    (not o.cells[resource] and "proposed" or (linked and (left==0 and cell.flow and "depleted" or "ready") or "building"))}
        end
    end
    return {protocol=1,session_id=storage.jev_session_id,tick=game.tick,sources=rows,diagnostics=o.reasons}
end
campaign.guard_mining_outpost_transfer=function(target,item,quantity,receipt,extracting)
    for _,cell in pairs(o.cells) do
        assert(not cell.fault,"Outpost reconciliation required")
        local ok=pcall(sample,cell)
        if not ok then cell.fault="outpost_identity_topology_or_flow_mismatch";error(cell.fault) end
        if target==role(cell.resource,"chest") then
            assert(extracting and item==cell.resource and cell.flow and topology(cell),"Do not seed or drain uncommissioned outposts")
        elseif target==role(cell.resource,"drill") then
            assert(not extracting and item=="coal" and topology(cell),"Invalid outpost drill transfer")
        end
    end
end
