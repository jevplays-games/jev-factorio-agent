local function configured_player_index()
    local index = storage.jev_player_index
    if index == nil then index = 1 end
    assert(type(index) == "number" and index > 0 and index % 1 == 0,
        "Native player index must be a positive integer")
    return index
end

-- Keep the same selection guard when this module is loaded independently.
local player_index = configured_player_index()
assert(storage.jev_bound_player_index == nil or storage.jev_bound_player_index == player_index,
    "Native player index cannot change within a runtime")
storage.jev_bound_player_index = player_index
local function selected_player()
    assert(configured_player_index() == player_index
        and storage.jev_bound_player_index == player_index,
        "Native player index cannot change within a runtime")
    return game.get_player(player_index)
end

storage.campaign = storage.campaign or {entities = {}, connections = {}}
local campaign = storage.campaign
campaign.receipts = campaign.receipts or {}
campaign.receipt_order = campaign.receipt_order or {}
campaign.rocket_baseline = campaign.rocket_baseline or storage.agent_characters[1].force.rockets_launched

local function contents(inventory)
    local result = {}
    if inventory then
        for _, stack in pairs(inventory.get_contents()) do
            result[stack.name] = (result[stack.name] or 0) + stack.count
        end
    end
    return result
end

local function inventory(entity, kind)
    return contents(entity.get_inventory(kind))
end

local function entity_for(role)
    local entity = campaign.entities[role]
    assert(entity and entity.valid, "Campaign entity is missing: " .. role)
    return entity
end

local function fluid_segments(entity, index)
    local result = {}
    local segment = entity.fluidbox.get_fluid_segment_id(index)
    if segment then table.insert(result, segment) end
    for _, connection in pairs(entity.fluidbox.get_pipe_connections(index)) do
        if connection.target then
            local adjacent = connection.target.get_fluid_segment_id(
                connection.target_fluidbox_index)
            if adjacent then table.insert(result, adjacent) end
        end
    end
    return result
end

campaign.register = function(role, name, position)
    assert(not campaign.entities[role] or not campaign.entities[role].valid,
        "Campaign role is already occupied")
    local agent = storage.agent_characters[1]
    local entity = agent.surface.find_entity(name, position)
    assert(entity and entity.force == agent.force and entity.unit_number,
        "Placed campaign entity not found")
    campaign.entities[role] = entity
    rcon.print(entity.unit_number)
end

campaign.observe = function()
    local agent = storage.agent_characters[1]
    local force = agent.force
    local force_entity_counts = {}
    local connectors = {pipe = {}, ["small-electric-pole"] = {}}
    for _, entity in pairs(agent.surface.find_entities_filtered{force = force}) do
        if entity.valid then
            force_entity_counts[entity.name] = (force_entity_counts[entity.name] or 0) + 1
            local observed = connectors[entity.name]
            if observed then
                local connector = {
                    unit_number = entity.unit_number,
                    position = {x = entity.position.x, y = entity.position.y}
                }
                if entity.name == "pipe" then
                    local fluid = entity.fluidbox[1]
                    connector.fluid = fluid and fluid.name or ""
                end
                table.insert(observed, connector)
            end
        end
    end
    local researched = {}
    for name, technology in pairs(force.technologies) do
        if technology.researched then table.insert(researched, name) end
    end
    table.sort(researched)
    local entities = {}
    for role, entity in pairs(campaign.entities) do
        if entity.valid then
            local state = {
                name = entity.name,
                unit_number = entity.unit_number,
                position = {x = entity.position.x, y = entity.position.y},
                status = entity.status,
                energy = entity.energy,
                fuel = entity.burner and inventory(entity, defines.inventory.fuel) or {},
                input = inventory(entity, defines.inventory.assembling_machine_input),
                output = inventory(entity, defines.inventory.assembling_machine_output),
                fluids = {},
                fluid_ports = {},
                electric_network_id = entity.electric_network_id,
                health = entity.health,
                max_health = entity.max_health
            }
            if entity.type == "furnace" then
                state.input = inventory(entity, defines.inventory.furnace_source)
                state.output = inventory(entity, defines.inventory.furnace_result)
            elseif entity.type == "lab" then
                state.input = inventory(entity, defines.inventory.lab_input)
            elseif entity.type == "container" then
                state.output = inventory(entity, defines.inventory.chest)
            end
            if entity.type == "assembling-machine" or entity.type == "furnace"
                or entity.type == "rocket-silo" then
                local recipe = entity.get_recipe()
                state.recipe = recipe and recipe.name or ""
                state.products_finished = entity.products_finished
                state.crafting = entity.is_crafting()
                state.crafting_progress = entity.crafting_progress
            end
            if entity.type == "rocket-silo" then
                state.rocket_parts = entity.rocket_parts
                state.rocket_ready = entity.rocket_silo_status == defines.rocket_silo_status.rocket_ready
                state.parts_required = entity.prototype.rocket_parts_required
            end
            for index = 1, #entity.fluidbox do
                local fluid = entity.fluidbox[index]
                local filter = entity.fluidbox.get_filter(index)
                local fluid_name = type(filter) == "string" and filter
                    or (filter and filter.name) or (fluid and fluid.name) or ""
                local segments = fluid_segments(entity, index)
                if #segments == 0 then
                    table.insert(state.fluid_ports, {fluid = fluid_name})
                end
                for _, segment in pairs(segments) do
                    table.insert(state.fluid_ports, {id = segment, fluid = fluid_name})
                end
                if fluid then
                    state.fluids[fluid.name] = (state.fluids[fluid.name] or 0) + fluid.amount
                end
            end
            entities[role] = state
        end
    end
    local player = selected_player()
    local statistics = force.get_item_production_statistics(agent.surface)
    local produced = {}
    local consumed = statistics.get_output_count and {} or nil
    for name in pairs(prototypes.item) do
        local amount = statistics.get_input_count(name)
        if amount > 0 then produced[name] = amount end
        if consumed then
            local used = statistics.get_output_count(name)
            if used > 0 then consumed[name] = used end
        end
    end
    return {
        mining_outposts = campaign.observe_mining_outposts and campaign.observe_mining_outposts() or nil,
        tick = game.tick,
        entities = entities,
        force_entity_counts = force_entity_counts,
        connectors = connectors,
        connector_ownership = campaign.observe_connector_ownership and
            campaign.observe_connector_ownership() or nil,
        receipts = campaign.receipts,
        connections = campaign.connections,
        researched = researched,
        research = force.current_research and force.current_research.name or "",
        research_progress = force.research_progress,
        produced = produced,
        consumed = consumed,
        -- Read existing properties in this observation; no extra RPC or mutation.
        acceptance_runtime = {schema=1, speed=game.speed, tick_paused=game.tick_paused,
            session_id=storage.jev_session_id, actor_unit=agent.unit_number,
            player_index=player and player.index, surface_index=agent.surface.index,
            force_index=force.index, mods=script and script.active_mods or nil},
        rockets_launched = force.rockets_launched,
        rocket_baseline = campaign.rocket_baseline,
        exploration_radius = campaign.exploration_radius or 8,
        player_connected = player ~= nil and player.connected,
        player_bound = player ~= nil and player.character == agent,
        crafting_queue = player and player.character == agent and player.crafting_queue_size or 0
    }
end

campaign.explore = function(radius)
    assert(radius >= 1 and radius <= 32 and radius % 1 == 0)
    local surface = storage.agent_characters[1].surface
    surface.request_to_generate_chunks({0, 0}, radius)
    surface.force_generate_chunk_requests()
    campaign.exploration_radius = radius
end

campaign.discover = function()
    local agent = storage.agent_characters[1]
    local known = {}
    for _, entity in pairs(campaign.entities) do
        if entity.valid then known[entity.unit_number] = true end
    end
    for _, entity in pairs(agent.surface.find_entities_filtered{
        force = agent.force, type = "container"
    }) do
        if entity.unit_number and not known[entity.unit_number] then
            campaign.entities["stock:" .. entity.unit_number] = entity
        end
    end
end

campaign.pipe_source = function(source_role, target_role, fluid_name)
    local source = entity_for(source_role)
    local target = entity_for(target_role)
    local segments = {}
    for index = 1, #source.fluidbox do
        local fluid = source.fluidbox[index]
        local filter = source.fluidbox.get_filter(index)
        local name = type(filter) == "string" and filter
            or (filter and filter.name) or (fluid and fluid.name) or ""
        if name == fluid_name or name == "" then
            for _, segment in pairs(fluid_segments(source, index)) do
                segments[segment] = true
            end
        end
    end
    local closest, distance
    for _, pipe in pairs(source.surface.find_entities_filtered{
        name = "pipe", force = source.force
    }) do
        local segment = pipe.fluidbox.get_fluid_segment_id(1)
        local fluid = pipe.fluidbox[1]
        if segment and segments[segment] and (not fluid or fluid.name == fluid_name) then
            local candidate = math.abs(pipe.position.x - target.position.x)
                + math.abs(pipe.position.y - target.position.y)
            if not distance or candidate < distance then
                closest, distance = pipe, candidate
            end
        end
    end
    rcon.print(helpers.table_to_json(closest and closest.position or {}))
end

campaign.transfer = function(role, item, quantity, receipt, extracting, preflight_context)
    if campaign.guard_mining_outpost_transfer then
        campaign.guard_mining_outpost_transfer(role, item, quantity, receipt, extracting)
    end
    assert(not campaign.receipts[receipt], "Transfer receipt already exists")
    local agent = storage.agent_characters[1]
    local machine = entity_for(role)
    local player = storage.fair.actor()
    local reachable = player.can_reach_entity(machine)
    assert(reachable, "Transfer is out of reach")
    local function destination_inventory()
        if extracting then
            return agent.get_inventory(defines.inventory.character_main), "actor_main"
        elseif item == "coal" and machine.burner then
            -- Furnace source inventories accept smeltable ingredients, not
            -- burner fuel.  Fuel service plans intentionally use the same
            -- fair transfer path as every other item, so select the native
            -- fuel inventory before the furnace-source branch.
            return machine.get_inventory(defines.inventory.fuel), "machine_fuel"
        elseif machine.type == "furnace" then
            return machine.get_inventory(defines.inventory.furnace_source), "furnace_source"
        elseif machine.type == "lab" then
            return machine.get_inventory(defines.inventory.lab_input), "lab_input"
        elseif machine.type == "assembling-machine" or machine.type == "rocket-silo" then
            return machine.get_inventory(defines.inventory.assembling_machine_input),
                "assembling_machine_input"
        elseif machine.burner then
            return machine.get_inventory(defines.inventory.fuel), "machine_fuel"
        end
    end
    local source = extracting and
        (machine.get_output_inventory() or machine.get_inventory(defines.inventory.chest))
        or agent.get_inventory(defines.inventory.character_main)
    local target, target_kind = destination_inventory()
    assert(source and source.get_item_count(item) >= quantity, "Transfer source is short")
    assert(target, "Transfer destination inventory is unavailable")
    local source_count = source.get_item_count(item)
    assert(type(source_count) == "number" and source_count % 1 == 0 and source_count >= quantity,
        "Transfer source is short")
    local insertable_count = target.get_insertable_count(item)
    assert(type(insertable_count) == "number" and insertable_count % 1 == 0
        and insertable_count >= 0, "Transfer destination capacity is unavailable")
    if insertable_count < quantity and preflight_context == nil then
        -- Keep the legacy public transfer path fail-before-mutation. Typed
        -- nonmutation evidence is reserved for the explicitly bound controller
        -- call below; ordinary NativeFactory/ObservedFactory calls still reject.
        assert(insertable_count >= quantity, "Transfer destination capacity is short")
    end
    if insertable_count < quantity then
        -- The request is accepted only for the current controller attempt and
        -- exact observed actor/entity. All assertions precede any inventory or
        -- receipt mutation, so the returned envelope is a narrow nonmutation
        -- result, not inference from a failed RCON command or absent receipt.
        local expected_keys = {
            schema=true, attempt_id=true, session_id=true, plan_id=true, step_index=true,
            step_sha256=true, action=true, started_tick=true, observed_tick=true,
            receipt=true, item=true, quantity=true, direction=true, role=true,
            machine_unit_number=true, machine_name=true, actor_unit_number=true,
            actor_player_index=true, surface_index=true, force_index=true,
        }
        assert(type(preflight_context) == "table", "Invalid transfer preflight request")
        local field_count = 0
        for key in pairs(preflight_context) do
            assert(expected_keys[key], "Invalid transfer preflight request field")
            field_count = field_count + 1
        end
        assert(field_count == 20 and preflight_context.schema == 1
            and type(preflight_context.attempt_id) == "string"
            and string.match(preflight_context.attempt_id, "^[0-9a-f]+$")
            and #preflight_context.attempt_id == 32
            and type(preflight_context.session_id) == "string"
            and preflight_context.session_id == storage.jev_session_id
            and type(preflight_context.plan_id) == "string" and #preflight_context.plan_id > 0
            and type(preflight_context.step_index) == "number"
            and preflight_context.step_index % 1 == 0
            and type(preflight_context.step_sha256) == "string"
            and string.match(preflight_context.step_sha256, "^[0-9a-f]+$")
            and #preflight_context.step_sha256 == 64
            and preflight_context.action == (extracting and "factory_extract" or "factory_insert")
            and preflight_context.started_tick <= preflight_context.observed_tick
            and preflight_context.observed_tick <= game.tick
            and preflight_context.receipt == receipt
            and preflight_context.item == item
            and preflight_context.quantity == quantity
            and preflight_context.direction == (extracting and "extract" or "insert")
            and preflight_context.role == role
            and preflight_context.machine_unit_number == machine.unit_number
            and preflight_context.machine_name == machine.name
            and preflight_context.actor_unit_number == agent.unit_number
            and preflight_context.actor_player_index == player.index
            and preflight_context.surface_index == agent.surface.index
            and preflight_context.force_index == agent.force.index
            and machine.surface == agent.surface and machine.force == agent.force
            and player.character == agent and player.surface == agent.surface
            and machine.unit_number and machine.unit_number > 0
            and agent.unit_number and agent.unit_number > 0
            and player.index and player.index > 0
            and not campaign.receipts[receipt]
            and source_count >= quantity and reachable == true,
            "Transfer preflight request identity or checks changed")
        local source_endpoint, target_endpoint
        if extracting then
            source_endpoint = {kind="machine_output_or_chest", role=role,
                name=machine.name, unit_number=machine.unit_number, quantity=source_count}
            target_endpoint = {kind="actor_main", role="@agent", name=agent.name,
                unit_number=agent.unit_number, insertable_count=insertable_count}
        else
            source_endpoint = {kind="actor_main", role="@agent", name=agent.name,
                unit_number=agent.unit_number, quantity=source_count}
            target_endpoint = {kind=target_kind, role=role, name=machine.name,
                unit_number=machine.unit_number, insertable_count=insertable_count}
        end
        return {
            schema="jev.transfer-capacity-preflight.v1",
            result="destination_capacity_short",
            request=preflight_context,
            tick=game.tick,
            actor={name=agent.name, unit_number=agent.unit_number, player_index=player.index,
                surface_index=agent.surface.index, force_index=agent.force.index},
            machine={role=role, name=machine.name, unit_number=machine.unit_number,
                surface_index=machine.surface.index, force_index=machine.force.index},
            source=source_endpoint, target=target_endpoint,
            checks={reachable=true, receipt_absent=true, source_sufficient=true, capacity_short=true},
        }
    end
    local removed = source.remove{name = item, count = quantity}
    local inserted = target.insert{name = item, count = removed}
    if inserted < removed then
        assert(source.insert{name = item, count = removed - inserted} == removed - inserted,
            "Transfer refund failed")
    end
    campaign.receipts[receipt] = {
        role = role, item = item, quantity = inserted,
        unit_number = machine.unit_number, extracting = extracting, tick = game.tick
    }
    table.insert(campaign.receipt_order, receipt)
    if #campaign.receipt_order > 128 then
        campaign.receipts[table.remove(campaign.receipt_order, 1)] = nil
    end
    assert(inserted == quantity, "Partial transfer; inspect receipt before continuing")
end

campaign.bind_player = function()
    local agent = storage.agent_characters[1]
    local player = selected_player()
    assert(player and player.connected, "Native crafting requires a connected game client")
    assert(player and (not player.character or player.character == agent),
        "The crafting player already controls another character")
    if player.character ~= agent then
        player.set_controller{type = defines.controllers.character, character = agent}
    end
    assert(player.character == agent)
end

campaign.craft = function(recipe_name, batches)
    storage.fair.actor()
    local agent = storage.agent_characters[1]
    local player = selected_player()
    assert(player and player.connected and player.character == agent,
        "Native crafting requires the connected bound player")
    assert(player.crafting_queue_size == 0, "A native craft is already in flight")
    local recipe = agent.force.recipes[recipe_name]
    assert(recipe and recipe.enabled, "Recipe is locked")
    assert(prototypes.entity.character.crafting_categories[recipe.category],
        "Recipe requires a machine")
    for _, ingredient in pairs(recipe.ingredients) do
        assert(ingredient.type == "item"
            and player.get_item_count(ingredient.name) >= ingredient.amount * batches,
            "Missing directly supplied crafting ingredients")
    end
    assert(player.begin_crafting{recipe = recipe_name, count = batches} == batches,
        "Native crafting did not accept the requested batch")
end

campaign.configure = function(role, recipe_name)
    local entity = entity_for(role)
    assert(storage.fair.actor().can_reach_entity(entity), "Configuration is out of reach")
    local recipe = entity.force.recipes[recipe_name]
    assert(recipe and recipe.enabled, "Recipe is locked")
    assert(next(inventory(entity, defines.inventory.assembling_machine_input)) == nil,
        "Refusing to change a machine with buffered ingredients")
    entity.set_recipe(recipe_name)
    assert(entity.get_recipe() and entity.get_recipe().name == recipe_name)
end

campaign.research = function(name)
    storage.fair.actor()
    local force = storage.agent_characters[1].force
    assert(not force.current_research or force.current_research.name == name,
        "Refusing to cancel unrelated research")
    if not force.current_research then assert(force.add_research(name)) end
end

campaign.launch = function(role)
    local silo = entity_for(role)
    assert(storage.fair.actor().can_reach_entity(silo), "Rocket silo is out of reach")
    assert(silo.rocket_silo_status == defines.rocket_silo_status.rocket_ready,
        "Rocket is not ready")
    assert(silo.launch_rocket(), "Native rocket launch was refused")
end
