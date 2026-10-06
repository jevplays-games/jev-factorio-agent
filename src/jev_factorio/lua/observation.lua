-- Read-only campaign/discovery envelope. No receipts, inventory or controls are cached.
local campaign = assert(storage.campaign)
local cache, epoch = {}, nil
local function profiler()
    if helpers.create_profiler then return helpers.create_profiler() end
    if game.create_profiler then return game.create_profiler() end
end
local function finish(timer, name)
    if timer then timer.stop(); rcon.print({"", "JEV_NATIVE_PROFILE|" .. name .. "|", timer}) end
end
campaign.observation_snapshot = function(generation)
    assert(type(generation) == "number" and generation >= 0 and generation % 1 == 0)
    local actor_ok, player = pcall(storage.fair.actor)
    if not actor_ok then
        local message = type(player) == "string" and player or ""
        local code
        if string.find(message, "Fair play requires the original connected character", 1, true) then
            code = "actor_unavailable"
        elseif string.find(message, "Fair player binding changed", 1, true) then
            code = "actor_changed"
        elseif string.find(message, "Fair play requires normal game speed", 1, true) then
            code = "actor_policy_changed"
        end
        local job = storage.campaign.craft_jobs and storage.campaign.craft_jobs.job
        if (code and type(job) == "table" and job.paid == true
                and type(job.id) == "string" and #job.id > 0 and #job.id <= 128) then
            rcon.print("JEV_CRAFT_OBSERVATION_FAILURE|" .. helpers.table_to_json({
                schema = 1, receipt = job.id, code = code
            }))
            return
        end
        error(player)
    end
    -- This is still the original character observation; the paid-craft marker
    -- above contains no replacement snapshot and does not relax fair.actor.
    local timer = profiler()
    local factory = campaign.observe() -- includes all currently enabled capability wrappers
    finish(timer, "campaign_snapshot")
    local radius = factory.exploration_radius
    assert(type(radius) == "number" and radius >= 1 and radius <= 32 and radius % 1 == 0)
    local identity = storage.jev_session_id .. ":" .. player.character.unit_number .. ":"
        .. player.surface.index .. ":" .. radius .. ":" .. generation
    if epoch ~= identity then cache, epoch = {}, identity end
    local targets, hits, misses = {}, 0, 0
    timer = profiler()
    for _, item in ipairs({"wood", "coal", "iron-ore", "copper-ore", "stone"}) do
        local entry = cache[item]
        local entity = entry and player.surface.find_entity(entry.value.name, entry.value.position)
        local selectable = false
        if entity and entity.valid then
            player.update_selected_entity(entity.position)
            selectable = player.selected == entity
        end
        if entry and game.tick >= entry.tick and game.tick - entry.tick <= 1800
            and entity and entity.valid and entity.minable
            and selectable
            and (entity.type ~= "resource" or entity.amount > 0)
            and (not entry.value.unit_number or entity.unit_number == entry.value.unit_number) then
            targets[item] = entry.value
            hits = hits + 1
        else
            local value = storage.fair.discover_mine_target(item, {x=0,y=0}, radius * 32)
            cache[item] = nil -- never cache absence, depleted sites, or malformed results
            if value.position then
                targets[item] = value
                cache[item] = {value=value, tick=game.tick}
            end
            misses = misses + 1
        end
    end
    finish(timer, "discovery")
    timer = profiler()
    local encoded = helpers.table_to_json({schema=1, factory=factory, targets=targets,
        session_id=storage.jev_session_id, actor_unit=player.character.unit_number,
        surface_index=player.surface.index, cache={hits=hits,misses=misses}})
    assert(#encoded <= 8 * 1024 * 1024, "Native observation payload budget exceeded")
    finish(timer, "serialize")
    rcon.print("JEV_SNAPSHOT|" .. encoded)
end
