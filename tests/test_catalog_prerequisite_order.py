"""Actual bundled Lua catalog prerequisite-order contract regressions."""
from importlib.resources import files
from types import SimpleNamespace

import pytest

LuaRuntime = pytest.importorskip("lupa").LuaRuntime


LUA_FIXTURE = r"""
local native_pairs = pairs
pairs = function(value)
    if value ~= requested then
        return native_pairs(value)
    end
    local index = 0
    return function()
        index = index + 1
        local key = iteration_order[index]
        if key then
            return key, requested[key]
        end
    end
end

local force = {
    recipes = {
        ["iron-gear-wheel"] = {
            category = "crafting",
            enabled = false,
            hidden = false,
            energy = 0.5,
            ingredients = {{name = "iron-plate", amount = 2}},
            products = {{name = "iron-gear-wheel", amount = 1}},
        },
    },
    technologies = {
        science = {
            prerequisites = requested,
            researched = false,
            enabled = true,
            research_unit_count = 10,
            research_unit_energy = 60,
            research_unit_ingredients = {},
            prototype = {effects = {}},
        },
    },
}
storage = {agent_characters = {[1] = {force = force}}}
script = {active_mods = {base = "2.0.77", core = "2.0.77"}}
prototypes = {
    item = {['iron-plate'] = {stack_size = 100}},
    entity = {
        character = {crafting_categories = {crafting = true}},
        ['assembling-machine-1'] = {
            crafting_categories = {crafting = true},
            get_crafting_speed = function() return 0.5 end,
            burner_prototype = {},
        },
        boiler = {burner_prototype = {}, electric_energy_source_prototype = nil},
    },
}
helpers = {table_to_json = function(value) return value end}
rcon = {print = function(value) exported = value end}
"""


def export_catalog(prerequisite_names, iteration_order):
    lua = LuaRuntime(unpack_returned_tuples=True)
    requested = lua.table()
    for name in prerequisite_names:
        requested[name] = lua.table()
    lua.globals().requested = requested
    lua.globals().iteration_order = lua.table_from(iteration_order)
    lua.execute(LUA_FIXTURE)
    lua.execute(files("jev_factorio").joinpath("lua/catalog.lua").read_text())
    return lua, lua.globals().exported


def exported_prerequisites(catalog, technology="science"):
    values = catalog.technologies[technology].prerequisites
    return [values[index] for index in range(1, len(values) + 1)]


def research_first_prerequisite(prerequisites):
    from jev_factorio.planning.catalog import Catalog
    from jev_factorio.planning.factory import FactoryPlanner

    technologies = {
        name: {"enabled": True, "prerequisites": [], "trigger": None}
        for name in ("alpha", "omega")
    }
    technologies["science"] = {
        "enabled": True,
        "prerequisites": prerequisites,
        "trigger": None,
    }
    catalog = Catalog(
        version="2.0.77",
        recipes={},
        technologies=technologies,
        machines={},
        hand_categories={},
    )
    planner = FactoryPlanner(catalog, SimpleNamespace(factory={}, researched=[]), "rocket_launch")
    lab_prerequisite = object()
    planner._machine = lambda role, machine, path: lab_prerequisite
    visited = []
    original_research = planner._research

    def record_research(name, path=(), required_recipe=None):
        visited.append(name)
        return original_research(name, path, required_recipe)

    planner._research = record_research
    result = planner._research("science")
    return visited, result, lab_prerequisite


@pytest.mark.parametrize(
    ("names", "order"),
    [([], []), (["automation"], ["automation"])],
)
def test_catalog_preserves_zero_and_one_prerequisites(names, order):
    _, catalog = export_catalog(names, order)
    assert exported_prerequisites(catalog) == names


def test_catalog_sorts_multiple_prerequisites_for_both_legal_iteration_orders():
    expected = ["alpha", "omega"]
    outputs = []
    for order in (expected, list(reversed(expected))):
        _, catalog = export_catalog(expected, order)
        outputs.append(exported_prerequisites(catalog))
    assert outputs == [expected, expected]


def test_downstream_research_traverses_the_exported_prerequisite_order():
    _, catalog = export_catalog(["alpha", "omega"], ["omega", "alpha"])
    prerequisites = exported_prerequisites(catalog)
    visited, result, lab_prerequisite = research_first_prerequisite(prerequisites)
    assert visited[:2] == ["science", "alpha"]
    assert result is lab_prerequisite


def test_catalog_export_preserves_version_mods_recipe_and_capability_fields():
    _, catalog = export_catalog(["alpha", "omega"], ["omega", "alpha"])
    assert catalog.version == "2.0.77"
    assert catalog.mods.base == "2.0.77"
    assert catalog.mods.core == "2.0.77"
    recipe = catalog.recipes["iron-gear-wheel"]
    assert recipe.name == "iron-gear-wheel"
    assert recipe.category == "crafting"
    assert recipe.enabled is False
    assert recipe.hidden is False
    assert recipe.energy == 0.5
    assert recipe.ingredients[1].name == "iron-plate"
    assert recipe.ingredients[1].amount == 2
    assert recipe.products[1].name == "iron-gear-wheel"
    technology = catalog.technologies.science
    assert technology.researched is False
    assert technology.enabled is True
    assert technology.count == 10
    assert technology.energy_ticks == 60
    assert len(technology.ingredients) == 0
    assert technology.trigger is None
    assert len(technology.effects) == 0
    assert catalog.stack_sizes["iron-plate"] == 100
    assert catalog.hand_categories.crafting is True
    machine = catalog.machines["assembling-machine-1"]
    assert machine.speed == 0.5
    assert machine.burner is True
    assert catalog.machines.boiler.speed == 0
    assert catalog.machines.boiler.burner is True
    assert catalog.machines.boiler.electric is False