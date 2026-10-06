local function configured_player_index()
    local index = storage.jev_player_index
    if index == nil then index = 1 end
    assert(type(index) == "number" and index > 0 and index % 1 == 0,
        "Native player index must be a positive integer")
    return index
end

-- agent_characters[1] remains FLE's logical agent slot. The native player
-- owning that character may have a different index, fixed for this runtime.
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

storage.fair = storage.fair or {}
local fair = storage.fair
fair.quarantined = true
script.on_nth_tick(5, nil)
script.on_nth_tick(15, nil)
script.on_nth_tick(60, nil)

if storage.actions and storage.actions.inspect_inventory
    and storage.actions.inspect_inventory ~= fair.inspect_inventory then
    local inspect_inventory = storage.actions.inspect_inventory
    fair.inspect_inventory = function(...)
        local result = table.pack(pcall(inspect_inventory, ...))
        script.on_nth_tick(60, nil)
        if not result[1] then error(result[2]) end
        return table.unpack(result, 2, result.n)
    end
    storage.actions.inspect_inventory = fair.inspect_inventory
end

fair.actor = function()
    local player = selected_player()
    local character = storage.agent_characters and storage.agent_characters[1]
    assert(player and player.connected and character and character.valid,
        "Fair play requires the original connected character")
    assert(player.character == character, "Fair player binding changed")
    local controller, controllers = player.controller_type, defines.controllers
    assert(controller == nil or controller == controllers.character
        or controller == controllers.god or controller == controllers.editor,
        "Fair player controller does not support input")
    assert(game.speed == 1 and not player.cheat_mode, "Fair play requires normal game speed")
    return player
end

fair.stop = function(reason)
    -- Cleanup is scoped to the originally selected LuaPlayer. A replacement
    -- character must never be adopted, and some controller types reject these
    -- input writes entirely (notably spectator and ghost controllers).
    local player_ok, player = pcall(game.get_player, player_index)
    local cleanup_errors, skipped = {}, nil
    if not player_ok then
        cleanup_errors[#cleanup_errors + 1] = tostring(player)
    elseif not player then
        skipped = "player_unavailable"
    else
        local inspected, valid, controller = pcall(function()
            return player.valid, player.controller_type
        end)
        if not inspected then
            cleanup_errors[#cleanup_errors + 1] = tostring(valid)
        elseif valid == false then
            skipped = "invalid_player"
        else
            local controllers = defines and defines.controllers or {}
            local supports_input = controller ~= nil and (
                (controllers.character ~= nil and controller == controllers.character)
                or (controllers.god ~= nil and controller == controllers.god)
                or (controllers.editor ~= nil and controller == controllers.editor))
            if supports_input and controller == controllers.character then
                local character_ok, has_valid_character = pcall(function()
                    local character = player.character
                    return character ~= nil and character.valid
                end)
                if not character_ok then
                    cleanup_errors[#cleanup_errors + 1] = tostring(has_valid_character)
                    supports_input = false
                elseif not has_valid_character then
                    skipped = "invalid_character"
                    supports_input = false
                end
            end
            if controller == nil then
                -- Older offline Lua fixtures omit controller_type. Permit their
                -- ordinary character case only while the original character is
                -- still valid and bound; never infer support for a replacement.
                local original_bound, same_original = pcall(function()
                    local character = player.character
                    return character and character.valid
                        and storage.agent_characters
                        and character == storage.agent_characters[1]
                end)
                if not original_bound then
                    cleanup_errors[#cleanup_errors + 1] = tostring(same_original)
                else
                    supports_input = same_original and true or false
                end
            end
            if not supports_input then
                if not cleanup_errors[1] and not skipped then
                    skipped = "unsupported_controller"
                end
            else
                for _, assignment in ipairs({
                    {"walking_state", {walking = false}},
                    {"mining_state", {mining = false}}
                }) do
                    local assigned, failure = pcall(function()
                        player[assignment[1]] = assignment[2]
                    end)
                    if not assigned then
                        cleanup_errors[#cleanup_errors + 1] = tostring(failure)
                    end
                end
            end
        end
    end

    local cleanup_error = #cleanup_errors > 0 and table.concat(cleanup_errors, "; ") or nil
    local job = fair.job
    local was_failed = job and job.status == "failed"
    if job then
        if cleanup_error then
            job.cleanup_result = "failed"
            job.cleanup_error = cleanup_error
        elseif skipped then
            job.cleanup_result = "skipped:" .. skipped
        else
            job.cleanup_result = "cleared"
        end

        local final_reason = reason
        if not final_reason and (cleanup_error or skipped) then
            final_reason = "Player controls could not be cleared: "
                .. (cleanup_error or skipped)
        end
        if job.status ~= "failed" or reason or cleanup_error or skipped then
            job.status = final_reason and "failed" or "completed"
            if final_reason then
                if reason or not was_failed then job.error = final_reason end
            else
                job.error = nil
            end
        end
    end

    -- Persist terminal job state and its original reason before surfacing an
    -- actual API setter/getter fault to Factorio's event error log.
    if cleanup_error then error(cleanup_error, 0) end
end

fair.bind = function()
    fair.quarantined = true
    script.on_nth_tick(5, nil)
    script.on_nth_tick(15, nil)
    script.on_nth_tick(60, nil)
    for _, name in pairs({"crafting_queue", "harvest_queues", "walking_queues"}) do
        assert(not storage[name] or next(storage[name]) == nil,
            "Legacy scripted work must be reconciled: " .. name)
    end
    local player = selected_player()
    local character = (storage.agent_characters or {})[1]
    assert(player and player.connected and character and character.valid,
        "Fair play requires the original connected character")
    assert(not player.character or player.character == character,
        "Refusing to replace a different character")
    if not player.character then
        player.set_controller{type = defines.controllers.character, character = character}
    end
    local player = fair.actor()
    storage.fast = false
    fair.stop("Controls stopped on adapter attachment")
    fair.quarantined = false
    return {position = player.position}
end

-- Rejected exact start/goal routes are cooled down, not retried every tick.
-- No assumed API flag can prohibit every neutral destructible obstacle: inspect
-- every PathfinderWaypoint before assigning any walking controls.
fair.blocked_routes = fair.blocked_routes or {}
local function route_key(player, position)
    return table.concat({player.surface.index or 0, player.character.unit_number,
        player.position.x, player.position.y, position.x, position.y}, ":")
end
local function reject_route(job, code, reason)
    job.failure_code = code
    job.movement_started = job.movement_started or false
    fair.blocked_routes[job.route_key] = game.tick + 3600
    local count = 0
    for key, expiry in pairs(fair.blocked_routes) do
        if expiry <= game.tick then fair.blocked_routes[key] = nil else count = count + 1 end
    end
    -- Keep memory bounded even across many distinct destinations.
    while count > 64 do
        local oldest_key, oldest_expiry
        for key, expiry in pairs(fair.blocked_routes) do
            if not oldest_expiry or expiry < oldest_expiry then
                oldest_key, oldest_expiry = key, expiry
            end
        end
        fair.blocked_routes[oldest_key] = nil
        count = count - 1
    end
    fair.stop(reason)
end

-- Plan both legs of at most four detours BEFORE moving. Nine native requests
-- (one original plus two per detour) and a 600-tick planning deadline bound work.
local function request_move_path(job, player, start, goal)
    assert((job.path_requests or 0) < 9, "Native path request budget exhausted")
    job.path_requests = (job.path_requests or 0) + 1
    job.request = player.surface.request_path{
        bounding_box = player.character.prototype.collision_box,
        collision_mask = player.character.prototype.collision_mask,
        start = start, goal = goal, force = player.force,
        radius = 0.2, entity_to_ignore = player.character, can_open_gates = true,
        pathfind_flags = {cache = false, allow_paths_through_own_entities = false,
                          allow_destroy_friendly_entities = false}
    }
end
local function next_detour(job, code)
    local player = fair.actor()
    local offsets = {6, -6, 12, -12}
    job.detour_index = (job.detour_index or 0) + 1
    if job.detour_index > #offsets or job.path_requests >= 9
        or game.tick > job.path_deadline then
        reject_route(job, code,
            "No safe route within bounded detours; choose another target or clear obstacles by normal mining")
        return
    end
    local dx, dy = job.goal.x - job.origin.x, job.goal.y - job.origin.y
    local length = math.sqrt(dx * dx + dy * dy)
    if length == 0 then
        reject_route(job, code, "No safe zero-length route; inspect the target obstruction")
        return
    end
    local center = job.blocked_waypoint or {
        x = (job.origin.x + job.goal.x) / 2,
        y = (job.origin.y + job.goal.y) / 2
    }
    job.detour_goal = {x = center.x - dy / length * offsets[job.detour_index],
                       y = center.y + dx / length * offsets[job.detour_index]}
    job.detour_prefix, job.path_leg = nil, 1
    request_move_path(job, player, job.origin, job.detour_goal)
end

fair.begin_move = function(position)
    local player = fair.actor()
    fair.stop()
    local character = player.character
    fair.job = {
        kind = "walk", status = "path_pending", lease = game.tick + 180,
        unit = character.unit_number, last_progress = game.tick,
        last_position = player.position, goal = position,
        route_key = route_key(player, position), movement_started = false,
        origin = {x = player.position.x, y = player.position.y},
        path_requests = 0, path_deadline = game.tick + 600
    }
    if (fair.blocked_routes[fair.job.route_key] or 0) > game.tick then
        fair.job.failure_code = "blocked_route_cooldown"
        fair.stop("Known blocked route is cooling down; choose another target or clear it normally")
        return {rejected = true, failure_code = fair.job.failure_code, movement_started = false}
    end
    request_move_path(fair.job, player, fair.job.origin, position)
    return {request = fair.job.request}
end

local function mining_entity(player, position, item)
    local filter = {position = position, radius = 0.75}
    if item == "wood" then filter.type = "tree" else filter.name = item end
    local best, best_distance
    for _, entity in pairs(player.surface.find_entities_filtered(filter)) do
        if entity.valid and entity.minable and (not storage.coal_supply or not storage.coal_supply.resource_reserved(entity)) then
            local distance = (entity.position.x - position.x)^2
                + (entity.position.y - position.y)^2
            if not best or distance < best_distance then
                best, best_distance = entity, distance
            end
        end
    end
    assert(best, "No mineable resource at target")
    return best
end

fair.mine_approach = function(position, item)
    local player = fair.actor()
    local entity = mining_entity(player, position, item)
    -- Resource entities may not have unit numbers. Retain the actual LuaEntity
    -- across the entire (possibly multi-leg) walk instead of resolving a
    -- replacement at the same coordinate as permission to mine it.
    fair.mining_token = (fair.mining_token or 0) + 1
    fair.mining_target = {token = fair.mining_token, entity = entity, item = item,
        actor = player.character.unit_number, surface = player.surface.index,
        position = {x = entity.position.x, y = entity.position.y}}
    if player.can_reach_entity(entity) then
        return {reachable = true, identity = fair.mining_token}
    end
    local horizontal = player.position.x - entity.position.x
    local vertical = player.position.y - entity.position.y
    local distance = math.sqrt(horizontal * horizontal + vertical * vertical)
    assert(distance > 0, "Unreachable mining target overlaps player")
    local target = {
        x = entity.position.x + horizontal / distance * 1.5,
        y = entity.position.y + vertical / distance * 1.5
    }
    local approach = player.surface.find_non_colliding_position("character", target, 2, 0.25)
    assert(approach, "No collision-free mining approach")
    return {reachable = false, position = approach, identity = fair.mining_token}
end

fair.begin_mine = function(position, item, quantity, expected_identity)
    local player = fair.actor()
    fair.stop()
    local entity = mining_entity(player, position, item)
    if expected_identity ~= nil then
        local observed = fair.mining_target
        assert(observed and observed.token == expected_identity and observed.entity.valid
            and observed.entity == entity and observed.item == item
            and observed.actor == player.character.unit_number
            and observed.surface == player.surface.index
            and entity.surface.index == observed.surface
            and entity.position.x == observed.position.x and entity.position.y == observed.position.y,
            "Mining target identity changed during approach")
        fair.mining_target = nil
    end
    assert(player.can_reach_entity(entity), "Mining target is outside normal reach")
    player.update_selected_entity(entity.position)
    assert(player.selected == entity, "Mining target is obscured by another entity")
    fair.job = {
        kind = "mine", status = "mining", lease = game.tick + 180,
        unit = player.character.unit_number, entity = entity, item = item,
        baseline = player.get_item_count(item), quantity = quantity,
        last_progress = game.tick, last_count = player.get_item_count(item)
    }
    player.mining_state = {mining = true, position = entity.position}
    return {baseline = fair.job.baseline}
end

fair.next_mine_target = function(item, radius)
    local player = fair.actor()
    assert(type(item) == "string", "Mining item must be a string")
    assert(type(radius) == "number" and radius > 0 and radius <= 128,
        "Mining search radius is invalid")
    local filter = {position = player.position, radius = radius}
    if item == "wood" then filter.type = "tree" else filter.name = item end
    local best, best_distance
    for _, entity in pairs(player.surface.find_entities_filtered(filter)) do
        if entity.valid and entity.minable and (not storage.coal_supply or not storage.coal_supply.resource_reserved(entity)) then
            local horizontal = entity.position.x - player.position.x
            local vertical = entity.position.y - player.position.y
            local distance = horizontal * horizontal + vertical * vertical
            if not best or distance < best_distance then
                -- Reject targets obscured by another entity before committing
                -- an observation.  This only updates the normal cursor; it
                -- does not walk, mine, transfer, or alter game speed.  The
                -- later begin_mine call independently enforces normal reach.
                player.update_selected_entity(entity.position)
                if player.selected == entity then
                    best, best_distance = entity, distance
                end
            end
        end
    end
    if not best then return {} end
    return {
        position = {x = best.position.x, y = best.position.y},
        unit_number = best.unit_number,
        name = best.name,
        surface_index = best.surface.index,
    }
end

fair.discover_mine_target = function(item, center, radius)
    -- Discovery inspects only terrain the campaign already generated. It may
    -- probe normal cursor selection, but never starts walking or mining. The
    -- later fair harvesting path walks to the returned entity
    -- and independently verifies normal reach and cursor selection.
    local player = fair.actor()
    assert(type(item) == "string", "Mining item must be a string")
    assert(type(center) == "table" and type(center.x) == "number"
        and type(center.y) == "number", "Mining search center is invalid")
    assert(type(radius) == "number" and radius > 0 and radius <= 1024,
        "Mining discovery radius is invalid")
    local filter = {position = center, radius = radius}
    if item == "wood" then filter.type = "tree" else filter.name = item end
    local best, best_distance
    for _, entity in pairs(player.surface.find_entities_filtered(filter)) do
        if entity.valid and entity.minable
            and (entity.type ~= "resource" or entity.amount > 0) then
            local horizontal = entity.position.x - center.x
            local vertical = entity.position.y - center.y
            local distance = horizontal * horizontal + vertical * vertical
            if not best or distance < best_distance then
                player.update_selected_entity(entity.position)
                if player.selected == entity then
                    best, best_distance = entity, distance
                end
            end
        end
    end
    if not best then return {} end
    return {
        position = {x = best.position.x, y = best.position.y},
        unit_number = best.unit_number,
        name = best.name,
        surface_index = best.surface.index,
    }
end

fair.observe = function()
    local player = fair.actor()
    local job = fair.job or {}
    if job.status ~= "failed" and job.status ~= "completed" then
        job.lease = game.tick + 180
    end
    return {
        position = player.position, tick = game.tick, status = job.status or "idle",
        error = job.error, failure_code = job.failure_code,
        movement_started = job.movement_started or false, path_requests = job.path_requests or 0,
        walking = player.walking_state.walking,
        mining = player.mining_state.mining,
        gained = job.item and player.get_item_count(job.item) - job.baseline or 0
    }
end

fair.find_build_site = function(name, center, radius)
    local player = fair.actor()
    assert(type(name) == "string" and prototypes.entity[name], "Unknown building prototype")
    assert(type(center) == "table" and type(center.x) == "number"
        and type(center.y) == "number", "Invalid build-site center")
    assert(type(radius) == "number" and radius >= 0 and radius <= 32
        and radius % 0.5 == 0, "Invalid build-site radius")
    local directions = {
        defines.direction.north, defines.direction.east,
        defines.direction.south, defines.direction.west
    }
    local best, best_distance
    local half_steps = radius * 2
    for horizontal = -half_steps, half_steps do
        for vertical = -half_steps, half_steps do
            local position = {
                x = center.x + horizontal / 2,
                y = center.y + vertical / 2
            }
            local distance = horizontal * horizontal + vertical * vertical
            for _, direction in ipairs(directions) do
                if player.surface.can_place_entity{
                    name = name, position = position, direction = direction,
                    force = player.force,
                    build_check_type = defines.build_check_type.manual
                } and (not storage.campaign or not storage.campaign.production_reserved
                    or not storage.campaign.production_reserved(name, position, direction))
                    and (not storage.campaign or not storage.campaign.mining_outpost_reserved
                    or not storage.campaign.mining_outpost_reserved(name, position, direction))
                    and (not storage.coal_supply or not storage.coal_supply.placement_reserved(name, position, direction))
                    and (not best or distance < best_distance) then
                    best = {position = position, direction = direction}
                    best_distance = distance
                end
            end
        end
    end
    assert(best, "No ordinary build site")
    return best
end

fair.place = function(name, position, direction)
    local player = fair.actor()
    assert(not storage.coal_supply or not storage.coal_supply.placement_reserved(name, position, direction),
        "Coal network owns this construction footprint")
    assert(not player.surface.find_entity(name, position), "Building already exists")
    assert((player.position.x - position.x)^2 + (player.position.y - position.y)^2
        <= player.build_distance^2, "Building is outside normal reach")
    assert(player.clear_cursor(), "Cannot clear the cursor without losing items")
    local before = player.get_item_count(name)
    local stack = player.get_main_inventory().find_item_stack(name)
    assert(stack and stack.valid_for_read, "Missing building item")
    assert(player.cursor_stack.transfer_stack(stack), "Cannot move existing item into cursor")
    local ok, failure = pcall(function()
        assert(player.can_build_from_cursor{position = position, direction = direction},
            "Building is obstructed or outside normal reach")
        player.build_from_cursor{position = position, direction = direction}
    end)
    local cleared = player.clear_cursor()
    assert(ok, failure)
    assert(cleared, "Could not return cursor items")
    assert(player.get_item_count(name) == before - 1, "Native build did not consume one item")
    local entity = player.surface.find_entity(name, position)
    assert(entity and entity.valid and entity.force == player.force, "Native build did not create entity")
    return {name = entity.name, position = entity.position, unit_number = entity.unit_number,
        drop_position = entity.type == "mining-drill" and entity.drop_position or nil}
end

fair.insert = function(name, position, item, quantity, expected_unit)
    local player = fair.actor()
    local entity = player.surface.find_entity(name, position)
    assert(entity and entity.valid and player.can_reach_entity(entity),
        "Interaction target is outside normal reach")
    if expected_unit ~= nil then
        assert(type(expected_unit) == "number" and expected_unit > 0 and expected_unit % 1 == 0
            and entity.unit_number == expected_unit, "Transfer target identity changed")
    end
    assert(not storage.coal_supply or storage.coal_supply.external_insert_allowed(entity, item),
        "Coal insert must use the journaled campaign transfer")
    local inventory = player.get_main_inventory()
    assert(inventory.get_item_count(item) >= quantity, "Missing transfer items")
    assert(entity.can_insert{name = item, count = quantity}, "Transfer destination is full")
    local removed = inventory.remove{name = item, count = quantity}
    local inserted = entity.insert{name = item, count = removed}
    if inserted < removed then
        assert(inventory.insert{name = item, count = removed - inserted} == removed - inserted)
    end
    assert(inserted == quantity, "Partial transfer requires reconciliation")
    return {quantity = inserted}
end

local previous_path = script.get_event_handler(defines.events.on_script_path_request_finished)
if previous_path ~= fair.path_handler then fair.previous_path = previous_path end
fair.path_handler = function(event)
    local job = fair.job
    if job and job.status == "path_pending" and event.id == job.request then
        if not event.path or #event.path == 0 then
            next_detour(job, event.try_again_later and "pathfinder_busy" or "no_safe_path")
            return
        end
        for _, waypoint in ipairs(event.path) do
            if waypoint.needs_destroy_to_reach then
                job.blocked_waypoint = job.blocked_waypoint or waypoint.position
                next_detour(job, "destruction_required")
                return
            end
        end
        if job.path_leg == 1 then
            job.detour_prefix, job.path_leg = event.path, 2
            request_move_path(job, fair.actor(), event.path[#event.path].position, job.goal)
            return
        end
        local path = event.path
        if job.path_leg == 2 then
            path = job.detour_prefix
            for _, point in ipairs(event.path) do path[#path + 1] = point end
        end
        local player = fair.actor()
        if (player.position.x - job.origin.x)^2 + (player.position.y - job.origin.y)^2 > 0.0625 then
            reject_route(job, "route_origin_changed", "Actor moved during route planning; reconcile before retry")
            return
        end
        job.last_progress, job.last_position = game.tick, player.position
        job.path, job.index, job.status = path, 1, "walking"
    elseif fair.previous_path then fair.previous_path(event) end
end
script.on_event(defines.events.on_script_path_request_finished, fair.path_handler)

local previous_tick = script.get_event_handler(defines.events.on_tick)
if previous_tick ~= fair.tick_handler then fair.previous_tick = previous_tick end
fair.tick_handler = function(event)
    if fair.quarantined then fair.stop("Adapter attachment is not validated"); return end
    local job = fair.job
    if not job or job.status == "failed" or job.status == "completed" then return end
    local ok, player = pcall(fair.actor)
    if not ok then fair.stop("Fair player/session invariant failed"); return end
    if player.character.unit_number ~= job.unit or game.tick > job.lease then
        fair.stop("Control lease expired or character changed"); return
    end
    if job.status == "path_pending" and game.tick > job.path_deadline then
        reject_route(job, "path_deadline", "Native path planning exceeded its bounded deadline")
        return
    end
    if job.status == "mining" then
        local count = player.get_item_count(job.item)
        if count - job.baseline >= job.quantity then fair.stop(); return end
        if not job.entity.valid then fair.stop("Resource depleted before requested amount"); return end
        if not player.can_reach_entity(job.entity) then fair.stop("Mining target left reach"); return end
        player.update_selected_entity(job.entity.position)
        if player.selected ~= job.entity then fair.stop("Mining target became obscured"); return end
        if count ~= job.last_count then job.last_count, job.last_progress = count, game.tick end
        if game.tick - job.last_progress > 600 then fair.stop("Native mining made no progress"); return end
        player.mining_state = {mining = true, position = job.entity.position}
    elseif job.status == "walking" then
        local point = job.path[job.index]
        while point and (player.position.x - point.position.x)^2
            + (player.position.y - point.position.y)^2 < 0.0625 do
            job.index = job.index + 1
            point = job.path[job.index]
        end
        if not point then fair.stop(); return end
        local horizontal = point.position.x - player.position.x
        local vertical = point.position.y - player.position.y
        local direction
        if math.abs(horizontal) > 2 * math.abs(vertical) then
            direction = horizontal > 0 and defines.direction.east or defines.direction.west
        elseif math.abs(vertical) > 2 * math.abs(horizontal) then
            direction = vertical > 0 and defines.direction.south or defines.direction.north
        elseif horizontal > 0 then
            direction = vertical > 0 and defines.direction.southeast or defines.direction.northeast
        else
            direction = vertical > 0 and defines.direction.southwest or defines.direction.northwest
        end
        if (player.position.x - job.last_position.x)^2
            + (player.position.y - job.last_position.y)^2 > 0.25 then
            job.last_progress, job.last_position = game.tick, player.position
        end
        if game.tick - job.last_progress > 300 then
            reject_route(job, "movement_obstructed", "Native walking is obstructed; partial movement must be reconciled")
            return
        end
        job.movement_started = true
        player.walking_state = {walking = true, direction = direction}
    end
end
script.on_event(defines.events.on_tick, fair.tick_handler)
