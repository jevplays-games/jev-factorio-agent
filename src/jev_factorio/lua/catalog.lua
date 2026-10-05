local force = storage.agent_characters[1].force
local catalog = {
    version = script.active_mods.base,
    mods = script.active_mods,
    recipes = {},
    technologies = {},
    machines = {},
    stack_sizes = {},
    hand_categories = prototypes.entity.character.crafting_categories
}
for name, item in pairs(prototypes.item) do
    catalog.stack_sizes[name] = item.stack_size
end
for name, recipe in pairs(force.recipes) do
    catalog.recipes[name] = {
        name = name,
        category = recipe.category,
        enabled = recipe.enabled,
        hidden = recipe.hidden,
        energy = recipe.energy,
        ingredients = recipe.ingredients,
        products = recipe.products
    }
end
for name, technology in pairs(force.technologies) do
    local prerequisites = {}
    for key in pairs(technology.prerequisites) do
        table.insert(prerequisites, key)
    end
    table.sort(prerequisites)
    catalog.technologies[name] = {
        prerequisites = prerequisites,
        researched = technology.researched,
        enabled = technology.enabled,
        count = technology.research_unit_count,
        energy_ticks = technology.research_unit_energy,
        ingredients = technology.research_unit_ingredients,
        trigger = technology.prototype.research_trigger,
        effects = technology.prototype.effects
    }
end
for _, name in ipairs({
    "stone-furnace", "steel-furnace", "electric-furnace",
    "assembling-machine-1", "assembling-machine-2", "assembling-machine-3",
    "chemical-plant", "oil-refinery", "rocket-silo"
}) do
    local prototype = prototypes.entity[name]
    if prototype then
        catalog.machines[name] = {
            categories = prototype.crafting_categories,
            speed = prototype.get_crafting_speed(),
            burner = prototype.burner_prototype ~= nil,
            electric = prototype.electric_energy_source_prototype ~= nil
        }
    end
end
-- Utility fuel admission needs the actual native burner prototype too. A
-- boiler is not a crafting machine; do not call get_crafting_speed on it.
local boiler = prototypes.entity.boiler
if boiler then
    catalog.machines.boiler = {
        categories = {},
        speed = 0,
        burner = boiler.burner_prototype ~= nil,
        electric = boiler.electric_energy_source_prototype ~= nil
    }
end
rcon.print(helpers.table_to_json(catalog))
