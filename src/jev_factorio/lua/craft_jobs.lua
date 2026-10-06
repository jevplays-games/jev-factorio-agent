-- Opt-in receipt tracking. Never awards items, changes speed, or replays work.
local campaign = assert(storage.campaign)
campaign.craft_jobs = campaign.craft_jobs or {}
local jobs = campaign.craft_jobs
local buffers, routes = storage.output_buffers, storage.input_routes
local observer = campaign.observe
if routes and observer == routes.observer then observer = routes.previous_observe end
if buffers and observer == buffers.observer then observer = buffers.previous_observe end
if jobs.observe_wrapper and observer == jobs.observe_wrapper
    and campaign.observe ~= jobs.observe_wrapper then
    for _, entry in ipairs({
        {defines.events.on_pre_player_crafted_item, jobs.pre_handler},
        {defines.events.on_player_cancelled_crafting, jobs.cancel_handler},
        {defines.events.on_player_crafted_item, jobs.crafted_handler}
    }) do
        assert(entry[2] and script.get_event_handler(entry[1]) == entry[2],
            "Craft event handler changed")
    end
    return
end

local function actor()
    local player = storage.fair.actor()
    assert(type(storage.jev_session_id) == "string", "Missing native session")
    return player, {
        session_id = storage.jev_session_id, player_index = player.index,
        unit_number = player.character.unit_number,
        surface_index = player.surface.index, force_index = player.force.index
    }
end

local function invalidate(reason)
    if jobs.job then jobs.job.status, jobs.job.error = "invalid", reason end
end

local function same_actor(job, identity)
    for _, key in ipairs({"session_id", "player_index", "unit_number", "surface_index", "force_index"}) do
        if job[key] ~= identity[key] then return false end
    end
    return true
end

local function observation_actor_failure_code(message)
    if type(message) ~= "string" then return nil end
    if string.find(message, "Fair play requires the original connected character", 1, true) then
        return "actor_unavailable"
    end
    if string.find(message, "Fair player binding changed", 1, true) then
        return "actor_changed"
    end
    if string.find(message, "Fair play requires normal game speed", 1, true) then
        return "actor_policy_changed"
    end
end

local function queue_valid(player, job)
    local remaining = 0
    for _, entry in pairs(player.crafting_queue or {}) do
        if entry.recipe ~= job.recipe or entry.prerequisite then return false end
        remaining = remaining + entry.count
    end
    return remaining == job.requested - job.finished
end

campaign.begin_craft_job = function(id, recipe_name, batches)
    local player, identity = actor()
    assert(type(id) == "string" and #id > 0 and #id <= 128, "Invalid job identity")
    assert(type(batches) == "number" and batches % 1 == 0 and batches >= 1 and batches <= 200,
        "Invalid craft batch")
    assert(not jobs.job or (jobs.job.status == "completed" and jobs.job.id ~= id),
        "Previous craft is unresolved or request was already used")
    assert(player.crafting_queue_size == 0, "Handcraft queue must be empty")
    local recipe = player.force.recipes[recipe_name]
    assert(recipe and recipe.enabled and #recipe.products == 1, "Unsupported native recipe")
    local product = recipe.products[1]
    assert(product.type == "item" and (not product.probability or product.probability == 1)
        and product.amount and product.amount > 0 and product.amount % 1 == 0,
        "Background craft needs one deterministic item product")
    local inputs, before = {}, {}
    assert(#recipe.ingredients > 0, "Background craft needs paid ingredients")
    for _, ingredient in pairs(recipe.ingredients) do
        assert(ingredient.type == "item" and ingredient.name ~= product.name
            and ingredient.amount > 0 and ingredient.amount % 1 == 0, "Unsupported craft ingredient")
        inputs[ingredient.name] = (inputs[ingredient.name] or 0) + ingredient.amount * batches
    end
    for name, amount in pairs(inputs) do
        before[name] = player.get_item_count(name)
        assert(before[name] >= amount, "All immediate ingredients must be carried")
    end
    if campaign.launch_assert_spend then campaign.launch_assert_spend(inputs) end
    local job = {
        id = id, recipe = recipe_name, requested = batches, accepted = 0,
        finished = 0, started_tick = game.tick, last_progress_tick = game.tick,
        baseline = {[product.name] = player.get_item_count(product.name)},
        outputs = {[product.name] = product.amount * batches}, inputs = inputs,
        status = "preparing", queue_valid = false, paid = false
    }
    for key, value in pairs(identity) do job[key] = value end
    jobs.job = job -- intent survives a command failure; never silently retry it
    jobs.submitting = true
    local ok, accepted = pcall(function()
        return player.begin_crafting{count = batches, recipe = recipe_name, silent = true}
    end)
    jobs.submitting = false
    if not ok then invalidate("enqueue_error"); error("Native crafting enqueue failed") end
    job.accepted = accepted
    if accepted ~= batches then invalidate("partial_acceptance"); error("Partial native craft acceptance") end
    for name, amount in pairs(inputs) do
        if before[name] - player.get_item_count(name) ~= amount then
            invalidate("input_debit_mismatch"); error("Native craft input debit mismatch")
        end
    end
    if job.status ~= "preparing" then error("Craft changed while submitting") end
    job.paid, job.status = true, "running"
    job.queue_valid = queue_valid(player, job)
    if not job.queue_valid then invalidate("queue_mismatch"); error("Native craft queue mismatch") end
    if campaign.successor_craft_paid then campaign.successor_craft_paid(job,before) end
    rcon.print("Native craft accepted; output remains unverified")
end

local function install(event_id, key, callback)
    local current = script.get_event_handler(event_id)
    assert(not jobs[key] or current == jobs[key],
        "Craft event handler changed; reconcile before attachment")
    if current ~= jobs[key] then jobs[key .. "_previous"] = current end
    local previous = jobs[key .. "_previous"]
    jobs[key] = function(event)
        if previous then previous(event) end
        callback(event)
    end
    script.on_event(event_id, jobs[key])
end

install(defines.events.on_pre_player_crafted_item, "pre_handler", function(event)
    local job = jobs.job
    if job and job.status == "running" and event.player_index == job.player_index and not jobs.submitting then
        invalidate("external_queue_change")
    end
end)
install(defines.events.on_player_cancelled_crafting, "cancel_handler", function(event)
    local job = jobs.job
    if job and job.status == "running" and event.player_index == job.player_index then
        invalidate("craft_cancelled")
    end
end)
install(defines.events.on_player_crafted_item, "crafted_handler", function(event)
    local job = jobs.job
    if not job or job.status ~= "running" or event.player_index ~= job.player_index then return end
    local ok, player, identity = pcall(actor)
    if not ok or not same_actor(job, identity) then invalidate("actor_changed"); return end
    local stack = event.item_stack
    local expected = stack and stack.valid_for_read and job.outputs[stack.name]
    if not event.recipe or event.recipe.name ~= job.recipe or not expected
        or stack.count ~= expected / job.requested
        or (stack.quality and stack.quality.name ~= "normal") then
        invalidate("unexpected_craft_event"); return
    end
    job.finished, job.last_progress_tick = job.finished + 1, game.tick
    if job.finished == job.requested then
        job.status, job.completed_tick = "completed", game.tick
    elseif job.finished > job.requested then invalidate("excess_craft_events") end
end)

if campaign.observe ~= jobs.observe_wrapper then jobs.previous_observe = campaign.observe end
local previous_observe = jobs.previous_observe
jobs.observe_wrapper = function()
    local result = previous_observe()
    local actor_ok, player, identity = pcall(actor)
    if not actor_ok then
        local job = jobs.job
        local code = observation_actor_failure_code(player)
        if (code and type(result) == "table" and type(job) == "table"
                and job.paid == true and type(job.id) == "string"
                and #job.id > 0 and #job.id <= 128) then
            result.craft_job_observation_failure = {
                schema = 1, receipt = job.id, code = code
            }
            return result
        end
        error(player)
    end
    result.craft_jobs_protocol, result.craft_job_actor = 1, identity
    -- FLE's earlier inventory read can precede a crafting event. Capture the
    -- main inventory in this same Lua observation as the receipt and game tick.
    local inventory = {}
    for _, stack in pairs(player.get_main_inventory().get_contents()) do
        inventory[stack.name] = (inventory[stack.name] or 0) + stack.count
    end
    result.craft_job_inventory = {tick = game.tick, items = inventory}
    local job = jobs.job
    if job then
        if not same_actor(job, identity) then invalidate("actor_changed") end
        job.queue_valid = queue_valid(player, job)
        if not job.queue_valid and job.status ~= "preparing" then invalidate("queue_mismatch") end
        result.craft_job = job
    end
    return result
end
campaign.observe = jobs.observe_wrapper
