"""Native-shaped boiler maintenance remains bounded by current observations."""
import pytest

from jev_factorio.planning.fuel_history import FuelHistory, estimated_rate
from jev_factorio.planning.ready_work import ReadyWorkPlanner

from test_factory import catalog, machine, recipe, snapshot
from test_grouped_fuel_service import due_scenario


def boiler_state(fuel=4):
    state, data = due_scenario()
    state.world_kind = 'fle'
    state.factory['entities']['utility:boiler'] = machine(
        'boiler', unit_number=901, position={'x': 3, 'y': 0}, fuel={'coal': fuel})
    state.factory['acceptance_runtime'] = {
        'schema': 1, 'session_id': state.session_id,
        'actor_unit': 1, 'player_index': 1, 'surface_index': 1, 'force_index': 1,
    }
    state.factory['inventory_insertable'] = {'coal': 50}
    return state, data


def observe_boiler_depletion(state):
    history = FuelHistory()
    boiler_unit = state.factory['entities']['utility:boiler']['unit_number']
    for tick, coal in ((0, 3), (300, 2), (600, 1)):
        state.tick = tick
        state.factory['entities']['utility:boiler']['fuel']['coal'] = coal
        history.observe(state)
    assert estimated_rate(state, boiler_unit) == pytest.approx(2 / 600)


def test_native_boiler_primary_uses_bounded_measured_lead_reserve():
    state, data = boiler_state(fuel=1)
    state.factory['research'] = ''
    observe_boiler_depletion(state)

    plan = ReadyWorkPlanner(data, state, 'rocket_launch')._fuel('utility:boiler', ())

    service = plan.materials['fuel_service']
    assert plan.steps[0].action == 'factory_gather'
    assert service['consumers'][0]['role'] == 'utility:boiler'
    assert service['combined_deficit'] == 4
    assert 0 < service['reserve'] <= service['max_reserve']
    assert plan.steps[0].parameters['quantity'] == service['acquisition_target']
    assert service['reserve_basis'].startswith(
        'observed_inventory_depletion_proxy_not_attributed_native_burn')
    assert service['lead_basis'] == 'catalog_policy_and_Manhattan_geometry_not_native_timing'


def test_empty_lab_or_no_active_research_does_not_create_a_boiler_reserve():
    state, data = boiler_state(fuel=4)
    state.factory['research'] = ''
    state.factory['entities']['utility:lab']['energy'] = 0

    plan = ReadyWorkPlanner(data, state, 'rocket_launch')._fuel('utility:boiler', ())

    service = plan.materials['fuel_service']
    assert service['combined_deficit'] == 1
    assert service['reserve'] == 0
    assert service['consumer_count'] == 1
    assert plan.steps[0].parameters['quantity'] == 1


def test_unknown_active_research_deadline_defers_optional_boiler_reserve():
    state, data = boiler_state(fuel=1)
    state.factory['research'] = 'unavailable-study'
    observe_boiler_depletion(state)

    plan = ReadyWorkPlanner(data, state, 'rocket_launch')._fuel('utility:boiler', ())

    service = plan.materials['fuel_service']
    assert service['combined_deficit'] == 4
    assert service['reserve'] == 0
    assert service['reserve_basis'] == 'science_or_power_deadline_defers_optional_reserve'


def test_known_near_science_deadline_defers_optional_boiler_reserve(monkeypatch):
    state, data = boiler_state(fuel=1)
    state.factory['research'] = 'automation'
    observe_boiler_depletion(state)
    monkeypatch.setattr('jev_factorio.planning.scheduling.research_schedule',
                        lambda snapshot, catalog: [{'amount': 1,
                            'deadline_tick': snapshot.tick + 100}])

    plan = ReadyWorkPlanner(data, state, 'rocket_launch')._fuel('utility:boiler', ())

    service = plan.materials['fuel_service']
    assert service['combined_deficit'] == 4
    assert service['reserve'] == 0
    assert service['reserve_basis'] == 'science_or_power_deadline_defers_optional_reserve'


def test_boiler_batches_a_partial_carried_load_before_primary_transfer():
    state, data = boiler_state(fuel=1)
    state.inventory['coal'] = 1

    plan = ReadyWorkPlanner(data, state, 'rocket_launch')._fuel('utility:boiler', ())

    assert plan.steps[0].action == 'factory_gather'
    assert plan.steps[0].parameters['quantity'] == 3
    service = plan.materials['fuel_service']
    assert service['combined_deficit'] == 4
    assert service['carried_spendable'] == 1
    assert service['acquisition_target'] == 4


def test_primary_boiler_groups_only_other_demanded_consumers():
    state, data = boiler_state(fuel=4)
    state.factory['research'] = ''
    state.factory['entities']['utility:lab']['energy'] = 0
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')

    plan = planner._fuel('utility:boiler', ())

    assert plan.materials['fuel_service']['consumer_count'] == 1
    assert plan.materials['fuel_service']['consumers'][0]['role'] == 'utility:boiler'


@pytest.mark.parametrize('mutation', [
    lambda state: state.factory['entities']['utility:boiler'].update(unit_number=0),
    lambda state: state.factory['entities']['utility:boiler'].update(name='stone-furnace'),
    lambda state: state.factory['entities']['utility:boiler'].update(fuel={'coal': True}),
    lambda state: state.factory['entities']['utility:boiler'].pop('fuel'),
])
def test_malformed_current_boiler_identity_or_stock_fails_closed(mutation):
    state, data = boiler_state()
    mutation(state)
    with pytest.raises(ValueError, match='native boiler'):
        ReadyWorkPlanner(data, state, 'rocket_launch')._fuel('utility:boiler', ())


def test_mock_planner_retains_legacy_boiler_fuel_behavior():
    state, data = due_scenario()
    state.factory['entities']['utility:boiler'] = machine(
        'boiler', unit_number=901, fuel={'coal': 4})

    plan = ReadyWorkPlanner(data, state, 'rocket_launch')._fuel('utility:boiler', ())

    assert plan.steps[0].parameters['quantity'] == 1
    assert 'fuel_service' not in plan.materials


@pytest.mark.parametrize('fuel', [{}, {'wood': 2}, {'coal': 0}])
def test_native_sparse_inventory_means_zero_coal_and_requires_paid_refill(fuel):
    state, data = boiler_state()
    state.factory['entities']['utility:boiler']['fuel'] = fuel
    state.inventory.pop('coal', None)
    plan = ReadyWorkPlanner(data, state, 'rocket_launch')._fuel('utility:boiler', ())
    assert plan.steps[0].action == 'factory_gather'
    service = plan.materials['fuel_service']
    assert service['consumers'][0]['fuel'] == 0
    assert service['consumers'][0]['role'] == 'utility:boiler'
    assert service['combined_deficit'] == 5


def test_science_pack_acquisition_precedes_low_boiler_service_but_start_stays_powered():
    from jev_factorio.planning.factory import FactoryPlanner

    data = catalog()
    data.technologies['automation'] = {
        'enabled': True, 'prerequisites': [], 'effects': [],
        'count': 10, 'energy_ticks': 600,
        'ingredients': [{'name': 'automation-science-pack', 'amount': 1}],
    }
    state = snapshot(inventory={'automation-science-pack': 10})
    state.factory.update(research='', research_progress=0)
    state.factory['entities'] = {
        'utility:water': machine('offshore-pump', fluid_ports=[{'id': 1, 'fluid': 'water'}]),
        'utility:boiler': machine('boiler', fuel={'coal': 1},
            fluid_ports=[{'id': 1, 'fluid': 'water'}, {'id': 2, 'fluid': 'steam'}]),
        'utility:engine': machine('steam-engine', electric_network_id=1,
            fluid_ports=[{'id': 2, 'fluid': 'steam'}]),
        'utility:lab': machine('lab', electric_network_id=1),
    }

    # A low boiler does not block spending already-carried science on the lab.
    first = FactoryPlanner(data, state, 'rocket_launch')._research('automation')
    assert first.steps[0].action == 'factory_insert'
    assert first.steps[0].parameters['role'] == 'utility:lab'

    # Once packs are supplied, the original power gate still prevents research
    # start/progress until the boiler is serviced and fresh fuel is observed.
    state.inventory['automation-science-pack'] = 0
    state.factory['entities']['utility:lab']['input'] = {'automation-science-pack': 10}
    second = FactoryPlanner(data, state, 'rocket_launch')._research('automation')
    assert second.steps[0].action == 'factory_gather'
    assert second.steps[0].item == 'coal'

    state.inventory['coal'] = second.steps[0].parameters['quantity']
    third = FactoryPlanner(data, state, 'rocket_launch')._research('automation')
    assert third.steps[0].action == 'factory_insert'
    assert third.steps[0].parameters['role'] == 'utility:boiler'

    state.inventory['coal'] = 0
    state.factory['entities']['utility:boiler']['fuel']['coal'] = 5
    fourth = FactoryPlanner(data, state, 'rocket_launch')._research('automation')
    assert fourth.steps[0].action == 'factory_research'
    assert fourth.steps[0].parameters['technology'] == 'automation'


def test_lab_power_does_not_preempt_science_ingredient_acquisition():
    from jev_factorio.planning.factory import FactoryPlanner

    data = catalog()
    data.recipes['automation-science-pack'] = recipe(
        'automation-science-pack', {'iron-ore': 1}, product='automation-science-pack')
    data.technologies['automation'] = {
        'enabled': True, 'prerequisites': [], 'effects': [],
        'count': 10, 'energy_ticks': 600,
        'ingredients': [{'name': 'automation-science-pack', 'amount': 1}],
    }
    state = snapshot()
    state.factory.update(research='', research_progress=0)
    state.factory['entities'] = {
        'utility:water': machine('offshore-pump', fluid_ports=[{'id': 1, 'fluid': 'water'}]),
        'utility:boiler': machine('boiler', fuel={'coal': 1},
            fluid_ports=[{'id': 1, 'fluid': 'water'}, {'id': 2, 'fluid': 'steam'}]),
        'utility:engine': machine('steam-engine', electric_network_id=1,
            fluid_ports=[{'id': 2, 'fluid': 'steam'}]),
        'utility:lab': machine('lab', electric_network_id=1),
    }

    # The captured native replay has a low boiler, no active study, and an
    # empty lab. The first science dependency is independent of lab power.
    first = FactoryPlanner(data, state, 'rocket_launch')._research('automation')
    assert first.steps[0].action == 'factory_gather'
    assert first.steps[0].item == 'iron-ore'
    assert 'fuel_service' not in first.materials
