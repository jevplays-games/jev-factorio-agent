"""Synthetic contracts and actual Lua builder tests; no native throughput claims."""
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from jev_factorio import mining_outposts as outposts
from jev_factorio.backends.mining_outposts import MiningOutpostFactory
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.input_controller import input_loop_type
from jev_factorio.memory import load_checkpoint
from jev_factorio.outpost_controller import outpost_loop_type
from jev_factorio.judgments import question_batch
from jev_factorio.planning.decision_support import candidate_evidence, scheduling_context
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from jev_factorio.skills import Plan, Step
from test_factory import catalog, snapshot, machine, recipe

ROOT = Path(__file__).resolve().parents[1]
LUA = ROOT / 'src/jev_factorio/lua'
RESOURCE = 'iron-ore'


def state_fixture():
    state = snapshot(tick=300, inventory={'burner-mining-drill': 1, 'wooden-chest': 1, 'coal': 50})
    state.factory['entities']['recipe:iron-plate'] = machine(
        recipe='iron-plate', fuel={'coal': 50}, products_finished=100, crafting=False)
    for key in ('input_routes', 'output_buffers', 'mining_outposts'):
        state.factory[key] = dict(protocol=1, session_id=state.session_id, tick=state.tick, sources={})
    state.factory['mining_outposts']['sources'][RESOURCE] = dict(resource=RESOURCE, layout='outpost:iron-ore:1',
        surface_index=1, force_index=1, state='proposed', topology=False, remaining=2000, parts={}, flow={}, steps=[
            dict(part='chest', name='wooden-chest', position={'x': 149.5, 'y': -1.5}, direction=0),
            dict(part='drill', name='burner-mining-drill', position={'x': 150, 'y': 0}, direction=0)])
    data = catalog()
    data.recipes['burner-mining-drill'] = recipe('burner-mining-drill', {'iron-plate': 5})
    data.recipes['wooden-chest'] = recipe('wooden-chest', {'wood': 2})
    return state, data


def row(state):
    return state.factory['mining_outposts']['sources'][RESOURCE]


def command(state, part='chest'):
    return dict(resource=RESOURCE, layout=row(state)['layout'], part=part, receipt='build:'+part)


def build(state, part, receipt=None):
    spec = next(s for s in row(state)['steps'] if s['part'] == part)
    unit = 70 if part == 'chest' else 71
    role = outposts.role(RESOURCE, part)
    state.inventory[spec['name']] -= 1
    row(state)['parts'][part] = dict(role=role, unit_number=unit, receipt=receipt or 'build:'+part, paid=1)
    row(state)['state'] = 'building' if part == 'chest' else 'ready'
    row(state)['topology'] = part == 'drill'
    state.factory['entities'][role] = machine(spec['name'], unit_number=unit,
        position=deepcopy(spec['position']), fuel={'coal': 5} if part == 'drill' else {})


def full(state):
    build(state, 'chest'); build(state, 'drill')
    return state


def decode_native_fixture(source, native_catalog=None, receiver_capacity=False,
                         insertable_count=50):
    """Feed the fixture through the real atomic observation decoder."""
    from copy import deepcopy
    from test_atomic_observation import setup as atomic_setup

    patcher = pytest.MonkeyPatch()
    try:
        backend, native, payload, _ = atomic_setup(patcher, craft=True)
        if native_catalog is not None:
            native.catalog.machines = deepcopy(native_catalog.machines)
        native_inventory = {item: count for item, count in source.inventory.items()
                            if type(count) is int and count > 0}
        runtime = deepcopy(source.factory['acceptance_runtime'])
        payload.update({
            'tick': source.tick,
            'session_id': source.session_id,
            'actor_unit': runtime['actor_unit'],
            'surface_index': runtime['surface_index'],
            'force_index': runtime['force_index'],
            'position': {'x': source.player_position[0], 'y': source.player_position[1]},
            'inventory': deepcopy(native_inventory),
            'targets': deepcopy(source.factory['fair_resource_targets']),
            'anchors': {},
            'inventory_capacity': {
                'schema': 1, 'tick': source.tick, 'inventory': 'character_main',
                'quality': 'normal', 'method': 'get_insertable_count',
                'items': {'coal': 3900},
            },
        })
        payload['controls'].update({
            'tick': source.tick,
            'position': deepcopy(payload['position']),
        })
        runtime.update(session_id=source.session_id, speed=1, tick_paused=False)
        payload['factory'] = deepcopy(source.factory)
        # The general planner fixture predates the atomic wire contract and
        # uses None for an unknown research list.  The native envelope
        # requires a concrete list, so normalize this field before decoding.
        if payload['factory'].get('researched') is None:
            payload['factory']['researched'] = []
        for counter in ('rockets_launched', 'rocket_baseline'):
            if payload['factory'].get(counter) is None:
                payload['factory'][counter] = 0
        payload['factory'].update(tick=source.tick, acceptance_runtime=runtime)
        payload['factory'].pop('fair_resource_targets', None)
        payload['factory']['craft_job_inventory'] = {
            'tick': source.tick, 'items': deepcopy(native_inventory),
        }
        if receiver_capacity:
            runtime = payload['factory']['acceptance_runtime']
            inventory = deepcopy(native_inventory)
            furnace = payload['factory']['entities']['recipe:iron-plate']
            payload['_receiver_input_capacity_payload'] = {
                'schema': 1, 'tick': source.tick,
                'session_id': source.session_id,
                'actor_unit': runtime['actor_unit'],
                'surface_index': runtime['surface_index'],
                'force_index': runtime['force_index'],
                'actor_inventory': inventory, 'complete': True,
                'eligible_count': 1, 'item_count': len(inventory),
                'receivers': {'recipe:iron-plate': {
                    'unit_number': furnace['unit_number'],
                    'name': furnace['name'], 'type': 'furnace', 'burner': True,
                    'surface_index': runtime['surface_index'],
                    'force_index': runtime['force_index'],
                    'items': {
                        item: {
                            'inventory': 'fuel' if item == 'coal' else 'furnace_source',
                            'actor_count': count,
                            'insertable_count': insertable_count,
                            'method': 'get_insertable_count',
                        } for item, count in inventory.items()
                    },
                }},
            }
        return backend.observe()
    finally:
        patcher.undo()


def commission(state):
    row(state)['flow'] = dict(layout=row(state)['layout'], drill_unit=71, chest_unit=70,
        first_tick=0, last_tick=180, positive_samples=3, received=3, mined=3, conservation=True)


def make_loop(backend, **kwargs):
    cls = outpost_loop_type(input_loop_type(buffered_loop_type(HierarchicalLoop)))
    return cls(backend, policy='deterministic', target='rocket_launch', factory_scheduling='ready-work', tick_seconds=0, **kwargs)


@pytest.mark.parametrize('change', [
    lambda p:p.update(resource='coal'), lambda p:p.update(part='belt'), lambda p:p.update(receipt=''),
    lambda p:p.update(layout=True), lambda p:p.update(extra='field'), lambda p:p.pop('part')])
def test_invalid_commands_rejected(change):
    state, _ = state_fixture(); params = command(state); change(params)
    with pytest.raises(ValueError):
        Step(outposts.COMMAND, 'outpost_component', parameters=params)


def test_paid_legacy_outpost_is_proposed_without_relocating_the_furnace():
    state, data = state_fixture(); before = deepcopy(state)
    plan = MiningOutpostPlanner(data, state, 'rocket_launch')._need(RESOURCE, 20)
    step = plan.steps[0]
    assert step.action == outposts.COMMAND and step.parameters['part'] == 'chest'
    assert step.allowed(state) and not step.satisfied(state)
    assert step.costs == {'wooden-chest': 1, 'burner-mining-drill': 1, 'coal': 5}
    assert state == before
    build(state, 'chest', step.parameters['receipt']); assert step.satisfied(state)
    next_step = MiningOutpostPlanner(data, state, 'rocket_launch')._need(RESOURCE, 1).steps[0]
    assert next_step.action == outposts.COMMAND and next_step.parameters['part'] == 'drill'
    assert state.factory['entities']['recipe:iron-plate'] == before.factory['entities']['recipe:iron-plate']


@pytest.mark.parametrize('change', [
    lambda s:s.inventory.update(coal=4), lambda s:s.inventory.update(**{'wooden-chest':0}),
    lambda s:s.inventory.update(**{'burner-mining-drill':0}), lambda s:s.factory.update(crafting_queue=1),
    lambda s:s.factory.update(player_bound=False), lambda s:s.factory.update(player_connected=False),
    lambda s:row(s).update(remaining=99), lambda s:s.factory['mining_outposts'].update(tick=s.tick-1)])
def test_build_requires_whole_remaining_paid_kit_and_fresh_idle_actor(change):
    state, _ = state_fixture(); change(state)
    assert not Step(outposts.COMMAND, 'outpost_component', parameters=command(state)).allowed(state)


@pytest.mark.parametrize('change', [
    lambda s:s.factory['mining_outposts'].update(protocol=True),
    lambda s:s.factory['mining_outposts'].update(session_id='other'),
    lambda s:row(s).update(remaining=-1), lambda s:row(s).update(force_index=True),
    lambda s:row(s)['steps'][0]['position'].update(x=150),
    lambda s:row(s)['steps'][1].update(direction=1),
    lambda s:row(s)['parts'].update(drill=dict(role='alien',unit_number=5,paid=1,receipt='x')),
    lambda s:row(s).update(state='ready'), lambda s:row(s)['steps'][1]['position'].update(x=float('nan'))])
def test_malformed_telemetry_fails_closed(change):
    state, _ = state_fixture(); change(state)
    with pytest.raises(ValueError): outposts.sources(state)


def test_kit_acquisition_does_not_recursively_build_another_outpost():
    state, data = state_fixture(); state.inventory = {'iron-plate': 5, 'coal': 5, 'wooden-chest': 1}
    step = MiningOutpostPlanner(data, state, 'rocket_launch')._need(RESOURCE, 20).steps[0]
    assert step.action == 'factory_craft' and step.parameters['recipe'] == 'burner-mining-drill'
    state.inventory.pop('iron-plate'); state.factory['entities']['recipe:iron-plate']['input'] = {'iron-ore': 5}
    step = MiningOutpostPlanner(data, state, 'rocket_launch')._need(RESOURCE, 20).steps[0]
    assert step.action != outposts.COMMAND


def test_native_stocked_furnace_can_gather_ore_for_its_outpost_drill_kit():
    """A drill made from this furnace's plates must not strand ore behind itself."""
    state, data = state_fixture()
    state.world_kind = 'fle'
    state.inventory = {'wooden-chest': 1, 'iron-gear-wheel': 10}
    furnace = state.factory['entities']['recipe:iron-plate']
    furnace.update(unit_number=2547, fuel={'coal': 3}, products_finished=20,
                   input={}, output={})
    state.factory['production_sites'] = {
        'protocol': 1, 'session_id': state.session_id, 'tick': state.tick,
        'sources': {'recipe:iron-plate': {
            'state': 'owned', 'source_unit': 2547,
        }},
    }
    data.recipes['burner-mining-drill'] = recipe('burner-mining-drill', {'iron-plate': 5})
    before = deepcopy(state)

    plan = MiningOutpostPlanner(data, state, 'rocket_launch')._need('iron-plate', 10)

    assert plan.steps[0].action == 'factory_gather'
    # The independent drill kit currently needs five plates, so it gathers
    # five paid ore first; it does not pre-gather the outer ten-plate demand.
    assert plan.steps[0].parameters == {'resource': 'iron-ore', 'quantity': 5}
    assert plan.steps[0].allowed(state)
    assert state == before


def test_native_36_of_50_steam_trigger_prefers_direct_ore_input_over_proposed_outpost():
    state, data, plan = native_steam_trigger_plan(
        {'iron-ore': 3, 'coal': 5})

    # The recorded 0128 state had three carried ore and an empty furnace input.
    # Keep that exact state here; the buffered 11-ore transfer is tested below
    # as a separate hypothetical, not as a reconstruction of the live receipt.
    assert state.inventory['iron-ore'] == 3
    assert state.factory['entities']['recipe:iron-plate']['input'] == {}
    assert plan.steps[0].action == 'factory_gather'
    assert plan.steps[0].parameters == {'resource': 'iron-ore', 'quantity': 11}
    assert 'outpost_kit_prerequisite' not in plan.materials
    trigger = plan.materials['native_research_trigger']
    assert trigger['technology'] == 'steam-power'
    assert trigger['outer_recipe'] == 'offshore-pump'
    assert trigger['trigger_item'] == 'iron-plate'
    assert trigger['trigger_produced_now'] == 36
    assert trigger['trigger_remaining_now'] == 14
    assert trigger['outer_recipe_locked_now'] is True

    evidence = candidate_evidence(state, data, [plan])[plan.id][
        'native_research_trigger_start_evidence']
    assert evidence is not None
    assert evidence['typed_dependency_path'] == [
        'offshore-pump', 'steam-power', 'iron-plate', 'iron-ore']
    assert evidence['action_start_facts']['trigger_recipe_input_units_required'] == 14
    assert evidence['action_start_facts']['quantity'] == 11
    assert evidence['useful_partial_benefit_level'] == 1
    assert evidence['does_not_establish_trigger_item_output_or_unlock'] is True

    # Exercise the actual question-building path. These assertions inspect
    # supplied evidence and wording only; they do not treat a mock answer as
    # native proof that an action was selected or completed.
    from jev_factorio.jev_client import MockJevClient
    from jev_factorio.judgments import select_plan
    observed_request = {}

    class RequestCapture:
        def evaluate(self, context, questions):
            observed_request['context'] = context
            observed_request['questions'] = questions
            return MockJevClient().evaluate(context, questions)

    support = scheduling_context(state, data, [plan], 'rocket_launch')
    decision = select_plan(RequestCapture(),
                           {'facts': state.for_jev(), **support}, [plan])
    assert decision.model_called is True
    request_evidence = observed_request['context']['candidate_evidence'][plan.id]
    request_trigger = request_evidence['native_research_trigger_start_evidence']
    assert request_trigger['trigger_produced_now'] == 36
    assert request_trigger['trigger_count'] == 50
    assert request_trigger['trigger_remaining_now'] == 14
    assert request_trigger['action_start_facts']['quantity'] == 11
    assert 'locked' in observed_request['questions']['candidate']['instructions']
    assert 'level-1 preparation' in observed_request['questions'][
        plan.id + '/benefit']['instructions']
    assert 'does not itself' in observed_request['questions'][
        plan.id + '/benefit']['instructions']
    assert 'later recipe output or technology unlock is unverified' in (
        observed_request['questions'][plan.id + '/needs_observation']['instructions'])


def test_native_steam_trigger_transfer_uses_decoded_same_rpc_receiver_capacity():
    state, data, plan = native_steam_trigger_plan(
        {'iron-ore': 14, 'coal': 5}, with_capacity=True)

    assert plan.steps[0].action == 'factory_insert'
    assert plan.steps[0].parameters['role'] == 'recipe:iron-plate'
    assert plan.steps[0].parameters['item'] == 'iron-ore'
    assert plan.steps[0].parameters['quantity'] == 14
    assert state._receiver_input_capacity['receivers']['recipe:iron-plate'][
        'items']['iron-ore'] == 50
    assert type(state._receiver_input_capacity['receivers']['recipe:iron-plate'][
        'items']['iron-ore']) is int

    evidence = candidate_evidence(state, data, [plan])[plan.id][
        'native_research_trigger_start_evidence']
    assert evidence is not None
    transfer = evidence['action_start_facts']['transfer']
    capacity = transfer['receiver_capacity']
    assert capacity['insertable_count_now'] == 50
    assert capacity['actor_count_now'] == 14
    assert capacity['source_role'] == 'recipe:iron-plate'
    assert capacity['source_unit'] == 2547
    assert evidence['action_start_facts']['trigger_recipe_input_units_required'] == 14
    assert evidence['action_start_facts'][
        'native_dispatch_rechecks_insertable_count_before_removal']


def test_native_trigger_capacity_evidence_rejects_unpinned_query_source():
    state, data, plan = native_steam_trigger_plan(
        {'iron-ore': 14, 'coal': 5}, with_capacity=True)
    state._receiver_input_capacity['query_source_sha256'] = '0' * 64
    assert candidate_evidence(state, data, [plan])[plan.id][
        'native_research_trigger_start_evidence'] is None


def test_hypothetical_buffered_trigger_input_qualifies_only_exact_three_ore_transfer():
    """Separate hypothetical buffer case; actual 0128 input was empty."""
    state, data, plan = native_steam_trigger_plan(
        {'iron-ore': 3, 'coal': 5}, with_capacity=True,
        machine_input={'iron-ore': 11})

    assert state.factory['entities']['recipe:iron-plate']['input'] == {'iron-ore': 11}
    assert plan.steps[0].action == 'factory_insert'
    assert plan.steps[0].parameters['quantity'] == 3
    evidence = candidate_evidence(state, data, [plan])[plan.id][
        'native_research_trigger_start_evidence']
    assert evidence is not None
    action = evidence['action_start_facts']
    assert action['trigger_recipe_input_units_required'] == 14
    assert action['current_machine_input_now'] == 11
    assert action['current_machine_input_units_required'] == 3
    assert action['transfer']['paid_quantity_to_transfer'] == 3
    assert action['transfer']['receiver_capacity']['insertable_count_now'] == 50


@pytest.mark.parametrize(('with_capacity', 'insertable_count'), [
    (False, 50), (True, 0), (True, 13),
])
def test_native_steam_trigger_transfer_fails_closed_without_sufficient_same_rpc_capacity(
        with_capacity, insertable_count):
    state, data, plan = native_steam_trigger_plan(
        {'iron-ore': 14, 'coal': 5}, with_capacity=with_capacity,
        insertable_count=insertable_count)
    assert plan.steps[0].action == 'factory_insert'
    assert candidate_evidence(state, data, [plan])[plan.id][
        'native_research_trigger_start_evidence'] is None


def test_native_trigger_bridge_does_not_compress_locked_recipe_into_direct_item_edge():
    state, data, plan = native_steam_trigger_plan({'iron-ore': 3, 'coal': 5})
    from jev_factorio.planning.research_trigger import (
        current_trigger, trigger_input_requirement,
    )

    trigger = current_trigger(state, data, 'steam-power', 'offshore-pump')
    assert trigger == plan.materials['native_research_trigger']
    assert trigger_input_requirement(
        state, data, trigger, 'iron-ore', ['offshore-pump', 'iron-ore']) is None
    assert trigger_input_requirement(
        state, data, trigger, 'iron-ore',
        ['offshore-pump', 'iron-plate', 'iron-ore'])['planned_recipe_input_units'] == 14

    data.recipes['offshore-pump']['enabled'] = True
    assert current_trigger(state, data, 'steam-power', 'offshore-pump') is None


def test_native_trigger_uses_live_researched_snapshot_over_stale_catalog_cache():
    from jev_factorio.planning.research_trigger import current_trigger

    state, data, _ = native_steam_trigger_plan({'iron-ore': 3, 'coal': 5})
    data.technologies['steam-power']['researched'] = True
    assert current_trigger(state, data, 'steam-power', 'offshore-pump') is not None

    state.researched = ['steam-power']
    assert current_trigger(state, data, 'steam-power', 'offshore-pump') is None


def test_native_trigger_machine_input_qualifies_raw_idle_furnace_recipe_and_capacity():
    from jev_factorio.planning.research_trigger import (
        current_machine_input_requirement, current_trigger,
    )

    state, data, _ = native_steam_trigger_plan({'iron-ore': 3, 'coal': 5})
    trigger = current_trigger(state, data, 'steam-power', 'offshore-pump')
    assert trigger is not None
    requirement = current_machine_input_requirement(
        state, data, trigger, 'iron-ore',
        ['offshore-pump', 'iron-plate', 'iron-ore'])
    assert requirement['machine_recipe_observed'] == ''
    assert requirement['machine_recipe_identity_basis'] == (
        'exact_owned_recipe_role_and_enabled_smelting_recipe')
    assert requirement['receiver_capacity']['source_role'] == 'recipe:iron-plate'
    assert requirement['receiver_capacity']['source_unit'] == 2547
    assert requirement['receiver_capacity']['insertable_count_now'] == 50


@pytest.mark.parametrize('mutation', [
    lambda state, data: state.factory['entities']['recipe:iron-plate'].update(crafting=True),
    lambda state, data: state.factory['entities']['recipe:iron-plate'].update(crafting=None),
    lambda state, data: state.factory['entities']['recipe:iron-plate'].update(recipe='stone-brick'),
    lambda state, data: state.factory['entities']['recipe:iron-plate'].update(
        name='assembling-machine-1'),
    lambda state, data: state._receiver_input_capacity.update(query_source_sha256='0' * 64),
    lambda state, data: state._receiver_input_capacity['receivers'][
        'recipe:iron-plate']['items'].update({'iron-ore': 13}),
    lambda state, data: state._receiver_input_capacity['receivers'][
        'recipe:iron-plate'].update(unit_number=9999),
    lambda state, data: state._receiver_input_capacity['receivers'][
        'recipe:iron-plate'].update(surface_index=2),
])
def test_native_trigger_machine_input_rejects_unqualified_empty_recipe_or_receiver(mutation):
    from jev_factorio.planning.research_trigger import (
        current_machine_input_requirement, current_trigger,
    )

    state, data, _ = native_steam_trigger_plan({'iron-ore': 3, 'coal': 5})
    trigger = current_trigger(state, data, 'steam-power', 'offshore-pump')
    mutation(state, data)
    assert current_machine_input_requirement(
        state, data, trigger, 'iron-ore',
        ['offshore-pump', 'iron-plate', 'iron-ore']) is None


def test_native_trigger_machine_input_requires_current_owned_recipe():
    from jev_factorio.planning.research_trigger import (
        current_machine_input_requirement, current_trigger,
    )

    state, data, _ = native_steam_trigger_plan({'iron-ore': 3, 'coal': 5})
    trigger = current_trigger(state, data, 'steam-power', 'offshore-pump')
    state.factory['entities']['recipe:iron-plate']['recipe'] = 'stone-brick'
    assert current_machine_input_requirement(
        state, data, trigger, 'iron-ore',
        ['offshore-pump', 'iron-plate', 'iron-ore']) is None


@pytest.mark.parametrize('mutation', [
    lambda state, data: setattr(state, 'tick', state.tick + 1),
    lambda state, data: setattr(state, 'researched', ['steam-power']),
    lambda state, data: data.technologies['steam-power'].update(
        prerequisites=['logistics']),
    lambda state, data: data.technologies['steam-power'].update(enabled=False),
    lambda state, data: state.factory['produced'].update({'iron-plate': 50}),
])
def test_native_trigger_bridge_rejects_stale_completed_or_unavailable_evidence(mutation):
    from jev_factorio.planning.research_trigger import current_trigger

    state, data, _ = native_steam_trigger_plan({'iron-ore': 3, 'coal': 5})
    data.technologies['steam-power']['researched'] = True  # stale catalog field is not authority
    mutation(state, data)
    assert current_trigger(state, data, 'steam-power', 'offshore-pump') is None


def nested_kit_plan(kind='transfer'):
    state, data = state_fixture()
    furnace = state.factory['entities']['recipe:iron-plate']
    furnace.update(unit_number=2547, fuel={'coal': 3}, products_finished=100,
                   input={}, output={}, crafting=False)
    furnace.pop('recipe', None)
    state.player_position = (0, 0)
    state.factory.update(tick=state.tick,
        acceptance_runtime={
            'schema': 1, 'session_id': state.session_id, 'actor_unit': 17,
            'player_index': 1, 'surface_index': 1, 'force_index': 1,
            'speed': 1, 'tick_paused': False,
        },
        fair_resource_targets={
            'iron-ore': {'name': 'iron-ore', 'surface_index': 1,
                         'position': {'x': 8.0, 'y': 3.0}},
            'coal': {'name': 'coal', 'surface_index': 1,
                     'position': {'x': 6.0, 'y': 4.0}},
        })
    state.nearby_resources.update({'iron-ore': 8.5, 'coal': 7.2})
    state.inventory = {'wooden-chest': 1, 'coal': 50}
    if kind == 'transfer':
        state.inventory['iron-ore'] = 5
    elif kind == 'pickup':
        furnace['output'] = {'iron-plate': 5}
    elif kind == 'craft':
        state.inventory['iron-plate'] = 5
    elif kind != 'gather':
        raise AssertionError(kind)
    data.recipes['burner-mining-drill'] = recipe('burner-mining-drill', {'iron-plate': 5})
    data.recipes['outer-pump'] = recipe('outer-pump', {'iron-ore': 1})
    state.factory['production_sites'] = {
        'protocol': 1, 'session_id': state.session_id, 'tick': state.tick,
        'sources': {'recipe:iron-plate': {
            'state': 'owned', 'reason': 'owned legacy furnace',
            'anchor': 'cell-site:legacy-iron-furnace',
            'position': {'x': 0, 'y': 0}, 'belt_count': 1,
            'bill': {'stone-furnace': 1, 'burner-mining-drill': 1,
                     'burner-inserter': 2, 'wooden-chest': 1,
                     'transport-belt': 1},
            'source_unit': 2547,
        }},
    }
    state.factory['acceptance_runtime'] = {
        'schema': 1, 'session_id': state.session_id, 'actor_unit': 17,
        'player_index': 1, 'surface_index': 1, 'force_index': 1,
        'speed': 1, 'tick_paused': False,
    }
    state = decode_native_fixture(
        state, native_catalog=data, receiver_capacity=(kind == 'transfer'))
    planner = MiningOutpostPlanner(data, state, 'rocket_launch')
    planner.focus = ('outer-pump', 20)
    plan = planner._need('iron-ore', 20, ('item:outer-pump',))
    return state, data, plan


def native_steam_trigger_plan(inventory, *, with_capacity=True, insertable_count=50,
                              machine_input=None, crafting=False):
    """Build a 36/50 native trigger through the atomic observation decoder."""
    state, data = state_fixture()
    state.world_kind = 'fle'
    state.inventory = dict(inventory)
    furnace = state.factory['entities']['recipe:iron-plate']
    furnace.update(unit_number=2547, fuel={'coal': 5}, products_finished=36,
                   input=dict(machine_input or {}), output={}, crafting=crafting,
                   recipe='')
    state.factory['produced'] = {'iron-plate': 36}
    state.factory['production_sites'] = {
        'protocol': 1, 'session_id': state.session_id, 'tick': state.tick,
        'sources': {'recipe:iron-plate': {
            'state': 'owned', 'reason': 'owned legacy furnace',
            'anchor': 'cell-site:legacy-iron-furnace',
            'position': {'x': 0, 'y': 0}, 'belt_count': 1,
            'bill': {'stone-furnace': 1, 'burner-mining-drill': 1,
                     'burner-inserter': 2, 'wooden-chest': 1,
                     'transport-belt': 1},
            'source_unit': 2547,
        }},
    }
    state.factory['acceptance_runtime'] = {
        'schema': 1, 'session_id': state.session_id, 'actor_unit': 17,
        'player_index': 1, 'surface_index': 1, 'force_index': 1,
        'speed': 1, 'tick_paused': False,
    }
    data.recipes['offshore-pump'] = recipe(
        'offshore-pump', {'iron-gear-wheel': 2, 'pipe': 3}, enabled=False)
    data.technologies['steam-power'] = {
        'enabled': True, 'prerequisites': [],
        'trigger': {'type': 'craft-item', 'item': {'name': 'iron-plate'}, 'count': 50},
        'effects': [{'type': 'unlock-recipe', 'recipe': 'offshore-pump'}],
    }
    state = decode_native_fixture(
        state, native_catalog=data, receiver_capacity=with_capacity,
        insertable_count=insertable_count)
    planner = MiningOutpostPlanner(data, state, 'rocket_launch')
    _, plan = planner._recipe('offshore-pump', ())
    plan = replace(plan, materials={
        **(plan.materials or {}),
        'local_objective': {'item': 'offshore-pump', 'inventory_target': 1},
        'work_intent': {'scope': 'immediate', 'observed_tick': state.tick},
    })
    return state, data, plan


def advance_native_fixture(state, native_catalog=None, receiver_capacity=False):
    """Advance and decode a new coherent fixture instead of setting trust flags."""
    from copy import deepcopy

    fresh = deepcopy(state)
    fresh.tick += 1
    for name in ('mining_outposts', 'production_sites', 'input_routes', 'output_buffers'):
        if isinstance(fresh.factory.get(name), dict):
            fresh.factory[name]['tick'] = fresh.tick
    return decode_native_fixture(fresh, native_catalog=native_catalog,
                                 receiver_capacity=receiver_capacity)


@pytest.mark.parametrize(('kind', 'action'), [
    ('gather', 'factory_gather'),
    ('transfer', 'factory_insert'),
    ('pickup', 'factory_extract'),
    ('craft', 'factory_craft'),
])
def test_nested_outpost_kit_provenance_qualifies_each_current_native_child_step(kind, action):
    state, data, plan = nested_kit_plan(kind)
    assert plan.steps[0].action == action
    assert plan.materials['local_objective']['item'] == 'outer-pump'
    nested = plan.materials['outpost_kit_prerequisite']
    assert nested['parent_request']['planner_item_path'] == ['outer-pump', 'iron-ore']
    assert nested['child_request'] == {
        'item': 'burner-mining-drill', 'quantity': 1, 'kind': 'outpost_component'}
    support = scheduling_context(state, data, [plan], 'rocket_launch')
    row_evidence = support['candidate_evidence'][plan.id]
    start = row_evidence['outpost_kit_prerequisite_start_evidence']
    assert start is not None
    assert start['parent_target_item'] == 'outer-pump'
    assert start['parent_request_item'] == 'iron-ore'
    assert start['child_kit_item'] == 'burner-mining-drill'
    assert start['parent_and_child_paths_are_separate']
    assert start['admission_is_not_native_payback_evidence']
    assert start['outpost_placement_arrival_flow_output_and_parent_completion_unverified']
    assert start['useful_partial_benefit_level'] == 1
    assert row_evidence['local_target'] == plan.materials['local_objective']
    if action == 'factory_insert':
        assert row_evidence['recipe_input_transfer_start_evidence'] is None
        assert start['child_planner_item_path'] == [
            'burner-mining-drill', 'iron-plate', 'iron-ore']
        assert start['action_start_facts']['receiver_capacity_observed'] is True
        assert start['action_start_facts']['fresh_native_dispatch_capacity_check_required']
        assert start['action_start_facts']['native_dispatch_checks_receiver_insertable_count']
        assert start['action_start_facts']['transfer']['receiver_capacity'][
            'insertable_count_now'] >= start['action_start_facts']['transfer'][
                'paid_quantity_to_transfer']
    context, questions, selected = question_batch(
        {'facts': state.for_jev(), **support}, [plan])
    assert selected == [plan]
    benefit = questions[plan.id + '/benefit']['instructions']
    assert 'current child request on separate' in benefit
    assert 'not native payback' in benefit
    assert 'level 1' in benefit
    assert 'outpost arrival/flow/output' in benefit
    needs = questions[plan.id + '/needs_observation']['instructions']
    assert 'separate same-tick parent and child paths' in needs


def test_nested_outpost_kit_evidence_fails_closed_on_stale_paths_owners_quantity_actor_and_research():
    state, data, plan = nested_kit_plan('transfer')

    def evidence(changed_state=state, changed_plan=plan, changed_catalog=data):
        return candidate_evidence(changed_state, changed_catalog, [changed_plan])[
            changed_plan.id]['outpost_kit_prerequisite_start_evidence']

    assert evidence() is not None
    bad_parent = deepcopy(plan.materials['outpost_kit_prerequisite'])
    bad_parent['parent_request']['planner_item_path'] = ['unrelated', 'iron-ore']
    assert evidence(changed_plan=replace(plan, materials={
        **plan.materials, 'outpost_kit_prerequisite': bad_parent})) is None
    bad_child = deepcopy(plan.materials['recipe_input_transfer'])
    bad_child['planner_item_path'] = ['outer-pump', 'iron-plate', 'iron-ore']
    assert evidence(changed_plan=replace(plan, materials={
        **plan.materials, 'recipe_input_transfer': bad_child})) is None
    bad_step = replace(plan.steps[0], parameters={**plan.steps[0].parameters, 'quantity': 4})
    assert evidence(changed_plan=replace(plan, steps=(bad_step,))) is None

    stale = deepcopy(state)
    stale.tick += 1
    assert evidence(changed_state=stale) is None
    foreign = deepcopy(state)
    foreign.factory['entities']['recipe:iron-plate']['unit_number'] = 9999
    foreign.factory['production_sites']['sources']['recipe:iron-plate']['source_unit'] = 9999
    assert evidence(changed_state=foreign) is None
    actor = deepcopy(state)
    actor.factory['player_bound'] = False
    assert evidence(changed_state=actor) is None
    locked = deepcopy(data)
    locked.recipes['iron-plate']['enabled'] = False
    assert evidence(changed_catalog=locked) is None


@pytest.mark.parametrize('change', [
    lambda s: setattr(s, '_coherent_observation_verified', (s.session_id, s.tick - 1)),
    lambda s: s.factory['acceptance_runtime'].update(session_id='other-session'),
    lambda s: s.factory['fair_resource_targets']['iron-ore'].update(surface_index=2),
    lambda s: s.factory['fair_resource_targets']['iron-ore'].update(name='copper-ore'),
    lambda s: s.factory.update(observation_snapshot_schema=1),
])
def test_nested_raw_gather_requires_fresh_decoded_target_identity(change):
    state, data, plan = nested_kit_plan('gather')
    change(state)
    assert candidate_evidence(state, data, [plan])[plan.id][
        'outpost_kit_prerequisite_start_evidence'] is None


def test_nested_component_quantity_uses_exact_current_remaining_kit():
    state, data, plan = nested_kit_plan('transfer')
    changed = deepcopy(plan.materials['outpost_kit_prerequisite'])
    changed['child_request']['quantity'] = 2
    wrong = replace(plan, materials={**plan.materials, 'outpost_kit_prerequisite': changed})
    assert candidate_evidence(state, data, [wrong])[wrong.id][
        'outpost_kit_prerequisite_start_evidence'] is None


def test_nested_parent_fractional_request_uses_same_rounded_shortage_for_admission_and_evidence():
    state, data, _ = nested_kit_plan('gather')
    state.inventory['iron-ore'] = 0
    state = decode_native_fixture(state)
    planner = MiningOutpostPlanner(data, state, 'rocket_launch')
    planner.focus = ('outer-pump', 20)
    plan = planner._need('iron-ore', 10.5, ('item:outer-pump',))

    nested = plan.materials['outpost_kit_prerequisite']
    assert nested['parent_request']['amount'] == 11
    assert nested['admission']['shortage_now'] == 11
    start = candidate_evidence(state, data, [plan])[plan.id][
        'outpost_kit_prerequisite_start_evidence']
    assert start['parent_request_amount'] == 11
    assert start['parent_shortage_now'] == 11


def test_nested_construction_fuel_is_a_separate_bounded_native_request():
    state, data = nested_kit_plan('gather')[:2]
    state.inventory.update({'burner-mining-drill': 1, 'coal': 0})
    state = decode_native_fixture(state)
    planner = MiningOutpostPlanner(data, state, 'rocket_launch')
    planner.focus = ('outer-pump', 20)
    plan = planner._need('iron-ore', 20, ('item:outer-pump',))

    assert plan.steps[0].action == 'factory_gather'
    nested = plan.materials['outpost_kit_prerequisite']
    assert nested['child_request'] == {
        'item': 'coal', 'quantity': 5, 'kind': 'outpost_construction_fuel'}
    start = candidate_evidence(state, data, [plan])[plan.id][
        'outpost_kit_prerequisite_start_evidence']
    assert start['child_request_kind'] == 'outpost_construction_fuel'
    assert start['child_kit_quantity'] == 5
    assert start['action_start_facts']['resource'] == 'coal'
    assert start['action_start_facts']['quantity'] == 5
    _, questions, _ = question_batch(
        {'facts': state.for_jev(), **scheduling_context(state, data, [plan], 'rocket_launch')},
        [plan])
    assert 'bounded five-coal construction-fuel request' in (
        questions[plan.id + '/benefit']['instructions'])

    state.inventory['coal'] = 4
    state = decode_native_fixture(state)
    edge_planner = MiningOutpostPlanner(data, state, 'rocket_launch')
    edge_planner.focus = ('outer-pump', 20)
    edge_plan = edge_planner._need('iron-ore', 20, ('item:outer-pump',))
    assert edge_plan.steps[0].parameters['quantity'] == 1
    edge = candidate_evidence(state, data, [edge_plan])[edge_plan.id][
        'outpost_kit_prerequisite_start_evidence']
    assert edge['child_request_kind'] == 'outpost_construction_fuel'
    assert edge['child_kit_quantity'] == 5
    assert edge['action_start_facts']['quantity'] == 1


def test_nested_component_chain_requalifies_gather_transfer_pickup_and_handcraft():
    state, data, plan = nested_kit_plan('gather')
    actions = []

    def current_plan():
        planner = MiningOutpostPlanner(data, state, 'rocket_launch')
        planner.focus = ('outer-pump', 20)
        plan = planner._need(
            'iron-ore', 20, ('item:outer-pump',))
        evidence = candidate_evidence(state, data, [plan])[plan.id][
            'outpost_kit_prerequisite_start_evidence']
        assert evidence is not None
        actions.append(plan.steps[0].action)
        return plan

    gather = current_plan()
    state.inventory['iron-ore'] = gather.steps[0].threshold
    state = advance_native_fixture(state, native_catalog=data, receiver_capacity=True)

    transfer = current_plan()
    assert transfer.steps[0].action == 'factory_insert'
    state.inventory['iron-ore'] -= transfer.steps[0].parameters['quantity']
    furnace = state.factory['entities']['recipe:iron-plate']
    # Represent the later fresh native snapshot after the paid ore was processed.
    furnace['input'] = {}
    furnace['output'] = {'iron-plate': 5}
    state = advance_native_fixture(state, native_catalog=data, receiver_capacity=True)

    pickup = current_plan()
    assert pickup.steps[0].action == 'factory_extract'
    assert pickup.steps[0].parameters['quantity'] == 5
    furnace['output'] = {}
    state.inventory['iron-plate'] = 5
    state = advance_native_fixture(state, native_catalog=data, receiver_capacity=True)

    craft = current_plan()
    assert craft.steps[0].action == 'factory_craft'
    assert actions == ['factory_gather', 'factory_insert', 'factory_extract', 'factory_craft']


def test_outpost_kit_still_collects_paid_plate_output_before_manual_ore():
    state, data = state_fixture()
    state.inventory = {'wooden-chest': 1, 'coal': 5}
    state.factory['entities']['recipe:iron-plate']['output'] = {'iron-plate': 5}
    data.recipes['burner-mining-drill'] = recipe('burner-mining-drill', {'iron-plate': 5})

    step = MiningOutpostPlanner(data, state, 'rocket_launch')._need(RESOURCE, 20).steps[0]

    assert step.action == 'factory_extract'
    assert step.parameters['role'] == 'recipe:iron-plate'
    assert step.parameters['item'] == 'iron-plate'
    assert step.parameters['quantity'] == 5
    assert step.allowed(state)


def test_outpost_kit_independent_path_still_rejects_its_own_recipe_cycle():
    state, data = state_fixture()
    state.inventory = {'wooden-chest': 1, 'coal': 5}
    data.recipes['burner-mining-drill'] = recipe(
        'burner-mining-drill', {'burner-mining-drill': 1})

    with pytest.raises(ValueError, match='Cyclic production dependency'):
        MiningOutpostPlanner(data, state, 'rocket_launch')._need(RESOURCE, 20)


@pytest.mark.parametrize('mode', ['small', 'bootstrap', 'other_goal', 'existing_ore', 'no_offer'])
def test_manual_fallback_and_existing_paid_supply_remain_available(mode):
    state, data = state_fixture(); amount, goal = 20, 'rocket_launch'
    if mode == 'small': amount = 2
    if mode == 'bootstrap': state.factory['entities']['recipe:iron-plate']['products_finished'] = 0
    if mode == 'other_goal': goal = 'iron_smelting'
    if mode == 'existing_ore': state.factory['entities']['legacy:chest'] = machine('wooden-chest', output={RESOURCE: 50})
    if mode == 'no_offer': state.factory['mining_outposts']['sources'] = {}
    step = MiningOutpostPlanner(data, state, goal)._need(RESOURCE, amount).steps[0]
    assert step.action in {'factory_gather', 'factory_extract'}


def test_flow_is_not_placement_or_time_and_precommissioning_cannot_be_seeded_or_drained():
    state, data = state_fixture(); full(state)
    state.factory['entities'][outposts.role(RESOURCE,'chest')]['output'][RESOURCE] = 10
    assert not outposts.flow_complete(RESOURCE, row(state)['layout'], state)
    for action in ('factory_insert','factory_extract'):
        assert not Step(action, 'transfer', parameters=dict(role=outposts.role(RESOURCE,'chest'),item=RESOURCE,
            quantity=1,receipt='test')).allowed(state)
    assert not Step('factory_gather','inventory', RESOURCE,20,
                    parameters={'resource':RESOURCE,'quantity':20}).allowed(state)
    wait=MiningOutpostPlanner(data,state,'rocket_launch')._need(RESOURCE,20).steps[0]
    assert wait.effect=='outpost_flow' and not wait.satisfied(state)
    state.tick+=100000;state.factory['mining_outposts']['tick']=state.tick
    assert not wait.satisfied(state)
    commission(state);assert wait.satisfied(state)


@pytest.mark.parametrize('key,value', [('received',4),('mined',2),('positive_samples',2),('drill_unit',99),
    ('chest_unit',99),('conservation',1),('last_tick',999),('first_tick',179),('layout','other')])
def test_certificate_requires_identity_conservation_samples_and_time(key,value):
    state,_=state_fixture();full(state);commission(state);row(state)['flow'][key]=value
    assert not outposts.flow_complete(RESOURCE,row(state)['layout'],state)


def test_collection_uses_real_buffer_and_bounded_hauling_not_more_mining():
    state,data=state_fixture();full(state);commission(state)
    chest=state.factory['entities'][outposts.role(RESOURCE,'chest')]
    chest['output'][RESOURCE]=75
    step=MiningOutpostPlanner(data,state,'rocket_launch')._need(RESOURCE,300).steps[0]
    assert step.action=='factory_extract' and step.parameters['quantity']==50 and step.allowed(state)
    chest['output'][RESOURCE]=2
    step=MiningOutpostPlanner(data,state,'rocket_launch')._need(RESOURCE,2).steps[0]
    assert step.action=='factory_extract' and step.parameters['quantity']==2
    chest['output'][RESOURCE]=0
    step=MiningOutpostPlanner(data,state,'rocket_launch')._need(RESOURCE,20).steps[0]
    assert step.action=='factory_wait' and step.effect=='machine_output' and step.threshold==20
    assert not step.satisfied(state)


def test_depleted_outpost_tail_then_observed_manual_fallback_without_rebuild():
    state,data=state_fixture();full(state);commission(state);row(state).update(state='depleted',remaining=0)
    chest=state.factory['entities'][outposts.role(RESOURCE,'chest')]
    chest['output'][RESOURCE]=2
    assert MiningOutpostPlanner(data,state,'rocket_launch')._need(RESOURCE,20).steps[0].action=='factory_extract'
    chest['output'][RESOURCE]=0
    step=MiningOutpostPlanner(data,state,'rocket_launch')._need(RESOURCE,20).steps[0]
    assert step.action=='factory_gather' and step.allowed(state)


def test_fuel_service_uses_carried_coal_before_waiting_for_commissioning():
    state,data=state_fixture();full(state);state.inventory['coal']=5
    state.factory['entities'][outposts.role(RESOURCE,'drill')]['fuel']={}
    step=MiningOutpostPlanner(data,state,'rocket_launch')._need(RESOURCE,20).steps[0]
    assert step.action=='factory_insert' and step.parameters['item']=='coal' and step.parameters['quantity']==5
    assert step.allowed(state)


class Backend:
    input_routes_supported = output_buffers_supported = mining_outposts_supported = True
    def __init__(self,state,data): self.state,self.data,self.calls=state,data,[]
    def enable_factory(self): return self.data
    def observe(self): return deepcopy(self.state)
    def act(self, action):
        assert action == 'idle'
        return 'synthetic idle'
    def execute(self, action, parameters):
        self.calls.append((action,deepcopy(parameters)))
        if action==outposts.COMMAND:
            build(self.state,parameters['part'],parameters['receipt'])
        return 'synthetic result'


def controlled_loop(tmp_path, state=None, data=None):
    if state is None: state,data=state_fixture()
    backend=Backend(state,data)
    loop=make_loop(backend,checkpoint=str(tmp_path/'checkpoint.json'))
    loop.order=['rocket_launch']
    loop._compile_candidates=lambda s: ([MiningOutpostPlanner(data,s,'rocket_launch')._need(RESOURCE,20)],'')
    return loop,backend


def test_actual_controller_builds_each_paid_component_once_and_retains_ownership(tmp_path):
    loop,backend=controlled_loop(tmp_path)
    assert loop.step()['verified']
    assert loop.step()['verified']
    assert [p['part'] for a,p in backend.calls]==['chest','drill']
    assert set(loop.memory.outpost_commitments[RESOURCE]['parts'])=={'chest','drill'}
    saved=load_checkpoint(loop.checkpoint,backend.state.session_id,'rocket_launch')
    assert saved.outpost_commitments==loop.memory.outpost_commitments
    assert backend.state.inventory['burner-mining-drill']==0
    assert backend.state.factory['entities']['recipe:iron-plate']['unit_number']==17


def test_lost_acknowledgement_verifies_paid_component_without_replaying(tmp_path):
    loop,backend=controlled_loop(tmp_path)
    execute=backend.execute
    def lost(action,p):
        execute(action,p);raise TimeoutError('lost result')
    backend.execute=lost
    loop.step()
    assert loop.memory.pending and loop.memory.pending['dispatch']=='ambiguous'
    loop.step()
    assert len(backend.calls)==1 and loop.memory.pending is None
    assert len(loop.memory.outpost_commitments[RESOURCE]['parts'])==1


def test_uncertain_unpaid_build_remains_behind_pending_barrier(tmp_path):
    loop,backend=controlled_loop(tmp_path)
    def lost(action,p): backend.calls.append((action,p));raise TimeoutError('unknown')
    backend.execute=lost;loop.step()
    for _ in range(3):loop.step()
    assert len(backend.calls)==1 and loop.memory.pending
    assert not loop.memory.outpost_commitments


@pytest.mark.parametrize('change', ['missing', 'replace', 'move', 'lost_prefix', 'lost_proof'])
def test_resume_does_not_drop_owned_outpost_evidence(tmp_path,change):
    loop,backend=controlled_loop(tmp_path);loop.step();loop.step()
    state=backend.state;commission(state);loop._observe()
    if change=='missing': state.factory['mining_outposts']['sources']={}
    if change=='replace': state.factory['entities'][outposts.role(RESOURCE,'drill')]['unit_number']=909
    if change=='move': state.factory['entities'][outposts.role(RESOURCE,'drill')]['position']['x']+=1
    if change=='lost_prefix': row(state)['parts'].pop('drill');row(state).update(state='building',topology=False,flow={})
    if change=='lost_proof': row(state)['flow']={}
    loop._observe()
    assert loop.memory.status=='uncertain'
    assert loop.memory.outpost_commitments[RESOURCE]['flow']
    assert len(backend.calls)==2


def test_legacy_checkpoint_upgrade_is_explicit_and_idle_only(tmp_path):
    state,data=state_fixture()
    old_cls=input_loop_type(buffered_loop_type(HierarchicalLoop)).memory_type
    memory=old_cls(state.session_id,'rocket_launch',last_tick=state.tick,active_goal='rocket_launch')
    path=tmp_path/'legacy.json';memory.save(path);before=path.read_bytes()
    cls=outpost_loop_type(input_loop_type(buffered_loop_type(HierarchicalLoop))).memory_type
    upgraded=cls.load(path,state.session_id,'rocket_launch')
    assert upgraded.outpost_commitments=={} and path.read_bytes()==before
    assert upgraded.history[-1]['kind']=='mining_outposts_enabled'
    memory.active_plan=MiningOutpostPlanner(data,state,'rocket_launch')._need(RESOURCE,20).to_dict();memory.save(path)
    with pytest.raises(ValueError,match='idle'):cls.load(path,state.session_id,'rocket_launch')


def test_legacy_readers_reject_new_ownership_and_half_extensions(tmp_path):
    loop,backend=controlled_loop(tmp_path);loop.step()
    old_cls=input_loop_type(buffered_loop_type(HierarchicalLoop)).memory_type
    with pytest.raises(ValueError):old_cls.load(loop.checkpoint,backend.state.session_id,'rocket_launch')
    data=json.loads(loop.checkpoint.read_text());data.pop('outpost_commitments');loop.checkpoint.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='Incomplete'):load_checkpoint(loop.checkpoint,backend.state.session_id,'rocket_launch')


def test_backend_adapter_prepares_walks_builds_and_never_retries():
    calls=[]
    def call(name,*args):
        calls.append(name)
        if name=='prepare_mining_outpost':return json.dumps({'name':'wooden-chest','position':{'x':149.5,'y':-1.5}})
        raise TimeoutError('lost placement result')
    native=SimpleNamespace(command=lambda code:None,call=call,backend=SimpleNamespace(
        _fair=SimpleNamespace(approach=lambda *args:calls.append('walk'))))
    adapter=MiningOutpostFactory(native);state,_=state_fixture()
    with pytest.raises(TimeoutError):adapter.execute(outposts.COMMAND,command(state))
    assert calls==['prepare_mining_outpost','walk','build_mining_outpost']


@pytest.fixture
def lua_runtime():
    lua=pytest.importorskip('lupa.lua54').LuaRuntime()
    lua.execute((ROOT/'tests/fixtures/input_routes_runtime.lua').read_text())
    lua.execute('''
        source.surface.index=1;force.index=1
        storage.input_routes={protocol=1,cells={},offers={}}
        storage.campaign.exploration_radius=8
        prototypes.entity['wooden-chest']={tile_width=1,tile_height=1}
        for _,ore in ipairs(resources) do
            ore.position.x=ore.position.x+150
            ore.prototype={mineable_properties={products={{name='iron-ore',type='item',amount=1}}}}
        end
        stock['wooden-chest']=1
        local oldcreate=create
        create=function(name,pos,dir,id)
            local e=oldcreate(name,{x=pos.x,y=pos.y},dir,id)
            local get=e.get_inventory
            e.get_inventory=function(kind)
                local inv=get(kind)
                inv.get_item_count=function(item)
                    if item then return inv.values[item] or 0 end
                    local sum=0;for _,n in pairs(inv.values) do sum=sum+n end;return sum
                end
                return inv
            end
            return e
        end
        function observed() return storage.campaign.observe_mining_outposts().sources['iron-ore'] end
        function build_outpost()
            local c=storage.campaign;local row=observed()
            assert(row,'No outpost offer: '..(storage.mining_outposts.reasons['iron-ore'] or 'unknown'))
            for _,s in ipairs(row.steps) do
                local p={resource='iron-ore',layout=row.layout,part=s.part,receipt='build:'..s.part}
                c.prepare_mining_outpost(p);player.position=s.position;c.build_mining_outpost(p)
            end
            local cell=storage.mining_outposts.cells['iron-ore']
            cell.parts.drill.entity.drop_target=cell.parts.chest.entity
            cell.parts.drill.entity.mining_target=cell.patch[1]
            return cell
        end
        function pulse_outpost(cell)
            game.tick=game.tick+60;cell.patch[1].amount=cell.patch[1].amount-1
            local inv=cell.parts.chest.entity.get_inventory(defines.inventory.chest).values
            inv['iron-ore']=(inv['iron-ore'] or 0)+1
            return observed()
        end
    ''')
    lua.execute((LUA/'mining_outposts.lua').read_text())
    return lua


def test_actual_lua_surveys_distant_ore_without_mutation(lua_runtime):
    lua_runtime.execute('''
        local row=observed();assert(row and row.state=='proposed')
        assert(row.steps[2].position.x>100 and row.remaining==2000)
        assert(observed().layout==row.layout and placements==0)
        assert(storage.campaign.entities['recipe:iron-plate']==source and stock['burner-mining-drill']==1)
        local p={resource='iron-ore',layout=row.layout,part='chest',receipt='chest'}
        storage.campaign.prepare_mining_outpost(p)
        assert(placements==0 and observed().state=='building')
    ''')


def test_actual_lua_paid_build_conservation_and_collection_gate(lua_runtime):
    lua_runtime.execute('''
        local original_source=source;local cell=build_outpost();local c=storage.campaign
        assert(placements==2 and stock['wooden-chest']==0 and stock['burner-mining-drill']==0)
        assert(c.entities['recipe:iron-plate']==original_source and source.unit_number==17)
        assert(observed().topology and not cell.flow)
        assert(not pcall(c.guard_mining_outpost_transfer,'outpost:iron-ore:chest','iron-ore',1,'x',true))
        assert(not pcall(c.guard_mining_outpost_transfer,'outpost:iron-ore:chest','iron-ore',1,'x',false))
        c.guard_mining_outpost_transfer('outpost:iron-ore:drill','coal',5,'fuel',false)
        c.transfer('outpost:iron-ore:drill','coal',5,'fuel',false)
        assert(stock.coal==95)
        pulse_outpost(cell);pulse_outpost(cell);assert(not cell.flow)
        local row=pulse_outpost(cell);assert(row.flow and row.flow.mined==3 and row.flow.received==3)
        c.guard_mining_outpost_transfer('outpost:iron-ore:chest','iron-ore',3,'collect',true)
        c.transfer('outpost:iron-ore:chest','iron-ore',3,'collect',true)
        assert(stock['iron-ore']==3 and observed().state=='ready')
    ''')


@pytest.mark.parametrize('change', [
    "player.connected=false", "game.speed=2", "player.cheat_mode=true", "player.crafting_queue_size=1",
    "stock['wooden-chest']=0", "stock['burner-mining-drill']=0", "stock.coal=4",
    "resources[1].amount=1;resources[2].amount=1", "obstacle=function(q)return true end",
    "force.mining_drill_productivity_bonus=1"])
def test_actual_lua_stale_preflight_never_places(lua_runtime,change):
    lua_runtime.execute("r=observed();p={resource='iron-ore',layout=r.layout,part='chest',receipt='a'}")
    lua_runtime.execute(change)
    lua_runtime.execute("assert(not pcall(storage.campaign.prepare_mining_outpost,p));assert(placements==0)")


def test_actual_lua_rechecks_after_walk_and_requires_normal_reach(lua_runtime):
    lua_runtime.execute('''
        local c=storage.campaign;local r=observed()
        local p={resource='iron-ore',layout=r.layout,part='chest',receipt='a'}
        c.prepare_mining_outpost(p);player.position={x=0,y=0}
        assert(not pcall(c.build_mining_outpost,p) and placements==0)
        player.position=r.steps[1].position;obstacle=function(q)return true end
        assert(not pcall(c.build_mining_outpost,p) and placements==0)
    ''')


def test_actual_lua_duplicate_receipt_and_duplicate_build_fail(lua_runtime):
    lua_runtime.execute('''
        local c=storage.campaign;local r=observed()
        local p={resource='iron-ore',layout=r.layout,part='chest',receipt='a'}
        c.prepare_mining_outpost(p);player.position=r.steps[1].position;c.build_mining_outpost(p)
        assert(not pcall(c.build_mining_outpost,p) and placements==1)
        p.part='drill';assert(not pcall(c.prepare_mining_outpost,p) and placements==1)
    ''')


@pytest.mark.parametrize('change', [
    "cell.parts.drill.entity.valid=false", "cell.parts.drill.entity.direction=4",
    "cell.parts.drill.entity.drop_target=source;game.tick=game.tick+121",
    "cell.parts.chest.entity.position.x=cell.parts.chest.entity.position.x+1",
    "storage.campaign.entities['outpost:iron-ore:drill']=source",
    "cell.parts.chest.entity.get_inventory(4).values['iron-ore']=50",
    "cell.parts.chest.entity.get_inventory(4).values['copper-ore']=1",
    "cell.patch[1].amount=cell.patch[1].amount+1", "cell.patch[1].amount=cell.patch[1].amount-10",
    "force.mining_drill_productivity_bonus=1"])
def test_actual_lua_identity_and_conservation_fail_closed(lua_runtime,change):
    lua_runtime.execute('cell=build_outpost();observed()');lua_runtime.execute(change)
    lua_runtime.execute("assert(observed().state=='fault');assert(placements==2)")


def test_actual_lua_reinstall_preserves_owner_and_never_wraps_observer_or_tick(lua_runtime):
    lua_runtime.execute('cell=build_outpost();observer=storage.campaign.observe;tick=handlers[1]')
    for _ in range(3):lua_runtime.execute((LUA/'mining_outposts.lua').read_text())
    lua_runtime.execute('''
        assert(storage.campaign.observe==observer and handlers[1]==tick)
        assert(storage.mining_outposts.cells['iron-ore']==cell and placements==2)
        pulse_outpost(cell);pulse_outpost(cell);pulse_outpost(cell);assert(cell.flow)
    ''')


@pytest.mark.parametrize('change', [
    "resources={}", "resources[2].name='copper-ore'", "force.mining_drill_productivity_bonus=1",
    "prototypes.entity['burner-mining-drill'].tile_width=3",
    "resources[1].prototype.mineable_properties.products[1].amount=2",
    "resources[1].prototype.mineable_properties.products[1].probability=0.5",
    "storage.input_routes.offers['recipe:iron-plate']={}",
    "storage.campaign.production_reserved=function()return true end"])
def test_actual_lua_unsupported_or_conflicting_sites_fall_back_without_build(lua_runtime,change):
    lua_runtime.execute(change)
    lua_runtime.execute("assert(not observed() and placements==0);assert(storage.mining_outposts.reasons['iron-ore'])")


@pytest.mark.parametrize('arguments', [
    [], ['--furnace-input-belts'],
    ['--furnace-input-belts', '--furnace-output-buffers'],
    ['--backend', 'fle', '--controller', 'hierarchical', '--factory-scheduling', 'ready-work',
     '--furnace-input-belts', '--furnace-output-buffers', '--target', 'iron_smelting'],
])
def test_invalid_outpost_cli_fails_before_world_initialization(monkeypatch, tmp_path, arguments):
    from jev_factorio import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['jev-factorio', '--mining-outposts', *arguments])
    monkeypatch.setattr(main, 'make_backend', lambda *a, **k: pytest.fail('World initialized'))
    with pytest.raises(SystemExit) as error:
        main.cli()
    assert error.value.code == 2


def test_cli_records_explicit_outpost_treatment_and_preserves_resume(monkeypatch, tmp_path):
    from jev_factorio import main
    from jev_factorio.research_log import verify_run
    from jev_factorio.outpost_controller import MiningOutpostMixin
    from jev_factorio.controller import HierarchicalLoop
    from jev_factorio.buffer_controller import buffered_loop_type
    from jev_factorio.input_controller import input_loop_type
    from jev_factorio.outpost_controller import outpost_loop_type
    captured = {}
    monkeypatch.chdir(tmp_path)
    checkpoint = tmp_path / 'state.json'
    selected = outpost_loop_type(input_loop_type(buffered_loop_type(HierarchicalLoop)))
    memory = selected.memory_type('cli-outpost-session', 'rocket_launch', last_tick=300)
    memory.save(checkpoint)
    original_checkpoint = checkpoint.read_bytes()
    selected.memory_type.load(checkpoint, 'cli-outpost-session', 'rocket_launch')
    monkeypatch.setattr('sys.argv', [
        'jev-factorio', '--backend', 'fle', '--controller', 'hierarchical',
        '--factory-scheduling', 'ready-work', '--furnace-output-buffers', '--furnace-input-belts',
        '--mining-outposts', '--policy', 'deterministic', '--tick-seconds', '2', '--steps', '0',
        '--resume', '--resume-controller', '--checkpoint', str(checkpoint), '--run-dir', str(tmp_path/'research')])
    def backend(name, **options):
        captured.update(backend=name, options=options)
        return object()
    def initialize(self, backend, jev=None, **options):
        captured['loop'] = type(self)
        captured['loop_options'] = options
    monkeypatch.setattr(main, 'make_backend', backend)
    monkeypatch.setattr(MiningOutpostMixin, '__init__', initialize)
    monkeypatch.setattr(MiningOutpostMixin, 'run', lambda self, steps: captured.update(steps=steps), raising=False)
    main.cli()
    manifest = json.loads((tmp_path/'research/manifest.json').read_text())
    assert manifest['configuration']['mining_outposts'] is True
    assert manifest['configuration']['resume'] and manifest['configuration']['resume_controller']
    assert captured['options'] == {
        'resume': True, 'adopt_session': False,
        'connector_witness_path': checkpoint.with_name(
            'native-connector-observer-v1.witness.jsonl'),
    }
    assert captured['loop_options']['resume_controller'] and captured['steps'] == 0
    assert verify_run(tmp_path/'research')['complete']
    assert checkpoint.read_bytes() == original_checkpoint


def test_outpost_capability_cannot_be_silently_dropped():
    state, data = state_fixture()
    backend = Backend(state, data)
    cls = input_loop_type(buffered_loop_type(HierarchicalLoop))
    loop = cls(backend, policy='deterministic', target='rocket_launch', factory_scheduling='ready-work')
    with pytest.raises(ValueError, match='[Oo]utpost'):
        loop._observe()
    assert not backend.calls


def test_readonly_manifest_validation_accepts_legacy_omission_and_rejects_nonboolean():
    from jev_factorio.research_log import RunConfiguration, _configuration
    configuration = asdict(RunConfiguration('fle', 'hierarchical', 'hybrid', mining_outposts=True))
    _configuration(configuration)
    configuration.pop('mining_outposts')
    _configuration(configuration)
    for invalid in (1, 'true', None):
        with pytest.raises(ValueError):
            _configuration(dict(configuration, mining_outposts=invalid))


def test_restart_command_preserves_outpost_flag_and_original_deadline(tmp_path, monkeypatch):
    from jev_factorio.supervisor import Supervisor, SupervisorConfig
    from test_supervisor import FakeClock, FakeProcess
    clock = FakeClock()
    state, data = state_fixture()
    backend = Backend(state, data)
    producer = make_loop(backend)
    config = SupervisorConfig(state_dir=tmp_path/'supervisor', checkpoint=tmp_path/'state.json',
        session_id='fresh', started_at=1000, repair_command=['unused'], cwd=tmp_path,
        factory_scheduling='ready-work', furnace_output_buffers=True, furnace_input_belts=True,
        mining_outposts=True)
    config.state_dir.mkdir()
    memory = producer.memory_type('fresh', 'rocket_launch', active_goal='rocket_launch',
                                  last_tick=state.tick)
    memory.save(config.checkpoint)
    original_checkpoint = config.checkpoint.read_bytes()
    composed = load_checkpoint(config.checkpoint, 'fresh', 'rocket_launch')
    assert (composed.output_buffers_schema, composed.input_routes_schema,
            composed.outposts_schema) == (1, 1, 1)
    assert composed.output_commitments == {} and composed.input_commitments == {}
    assert composed.outpost_commitments == {}
    instance = Supervisor(config, clock=clock, sleep=clock.sleep, popen=lambda *a, **k: FakeProcess())
    monkeypatch.setattr(instance, 'source_identity', lambda: ('head', 'source'))
    instance.initialize()
    assert config.checkpoint.read_bytes() == original_checkpoint
    assert backend.calls == []
    cutoff = instance.state['cutoff']
    for _ in range(2):
        command = instance.gameplay_command()
        assert '--mining-outposts' in command and '--resume' in command and '--resume-controller' in command
        instance.initialize()
        assert instance.state['cutoff'] == cutoff
        assert config.checkpoint.read_bytes() == original_checkpoint
        assert backend.calls == []
    config.mining_outposts = False
    with pytest.raises(ValueError, match='configuration cannot be changed|launch composition preflight'):
        instance.initialize()
    assert backend.calls == []


def test_supervisor_requires_outpost_capability_dependencies(tmp_path):
    from jev_factorio.supervisor import SupervisorConfig
    config = SupervisorConfig(state_dir=tmp_path, checkpoint=tmp_path/'state', session_id='fresh',
        started_at=1, repair_command=['unused'], cwd=tmp_path, mining_outposts=True)
    with pytest.raises(ValueError, match='input belts'):
        config.validate()
    config.furnace_input_belts = True
    with pytest.raises(ValueError, match='ready-work'):
        config.validate()


def test_background_locks_still_reject_construction_and_locked_ore():
    from jev_factorio.craft_jobs import CraftJob
    state, data = state_fixture()
    plan = MiningOutpostPlanner(data, state, 'rocket_launch')._need(RESOURCE, 20)
    lock = SimpleNamespace(failed='', outputs={RESOURCE: 20})
    assert not CraftJob.permits(lock, plan.steps[0])
    full(state); commission(state)
    state.factory['entities'][outposts.role(RESOURCE, 'chest')]['output'][RESOURCE] = 50
    pickup = MiningOutpostPlanner(data, state, 'rocket_launch')._need(RESOURCE, 20).steps[0]
    assert pickup.allowed(state) and not CraftJob.permits(lock, pickup)
    lock.outputs = {'copper-cable': 20}
    assert CraftJob.permits(lock, pickup)


def test_persistence_failure_poisoning_stops_later_mutations(tmp_path, monkeypatch):
    loop, backend = controlled_loop(tmp_path)
    loop.step()
    before = len(backend.calls)
    def fail(self, path):
        raise OSError('Synthetic disk failure')
    monkeypatch.setattr(type(loop.memory), 'save', fail)
    with pytest.raises(OSError):
        loop._observe()
    with pytest.raises(RuntimeError, match='persistence failed'):
        loop.step()
    assert len(backend.calls) == before


def test_recreated_controller_reconciles_paid_lost_ack_without_build_replay(tmp_path):
    loop, backend = controlled_loop(tmp_path)
    execute = backend.execute
    def lost(action, parameters):
        execute(action, parameters)
        raise TimeoutError('lost acknowledgement')
    backend.execute = lost
    loop.step()
    resumed = make_loop(backend, checkpoint=str(loop.checkpoint), resume_controller=True)
    resumed.step()
    assert len(backend.calls) == 1 and resumed.memory.pending is None
    assert resumed.memory.outpost_commitments[RESOURCE]['parts']['chest']['paid'] == 1


def test_actual_lua_committed_outpost_rejects_direct_route_prepare(lua_runtime):
    lua_runtime.execute((LUA/'input_routes.lua').read_text())
    lua_runtime.execute('''
        local cell=build_outpost()
        local p={source='recipe:iron-plate',layout='stale',part='inserter',receipt='direct',reserve_belts=0}
        local ok,error=pcall(storage.campaign.prepare_input_route,p)
        assert(not ok and string.find(error,'ore outpost'))
        assert(not storage.input_routes.cells['recipe:iron-plate'] and placements==2)
    ''')


def test_actual_lua_exhaustion_keeps_paid_cell_and_verified_tail(lua_runtime):
    lua_runtime.execute('''
        local cell=build_outpost()
        pulse_outpost(cell);pulse_outpost(cell);pulse_outpost(cell)
        local proof=cell.flow
        for _,e in ipairs(cell.patch) do e.amount=0;e.valid=false end
        cell.parts.drill.entity.mining_target=nil
        local row=observed()
        assert(row.state=='depleted' and row.remaining==0 and row.flow==proof and placements==2)
        storage.campaign.guard_mining_outpost_transfer('outpost:iron-ore:chest','iron-ore',3,'tail',true)
        assert(observed().parts.drill.unit_number==cell.parts.drill.unit_number)
    ''')


def test_actual_base_transfer_calls_outpost_guard_before_any_inventory_access():
    lua = pytest.importorskip('lupa.lua54').LuaRuntime()
    lua.execute('storage={agent_characters={{force={rockets_launched=0}}}}')
    lua.execute((LUA/'factory.lua').read_text())
    lua.execute('''
        calls=0
        storage.campaign.guard_mining_outpost_transfer=function(role,item,quantity,receipt,extracting)
            calls=calls+1;assert(role=='outpost:iron-ore:chest' and extracting)
            error('Uncommissioned outpost guard')
        end
        local ok,error=pcall(storage.campaign.transfer,'outpost:iron-ore:chest','iron-ore',1,'x',true)
        assert(not ok and string.find(error,'Uncommissioned outpost guard') and calls==1)
        assert(not next(storage.campaign.receipts))
    ''')


def test_full_capability_stack_builds_and_reloads_with_background_memory(tmp_path):
    from jev_factorio.background import BackgroundWorkLoop
    state, data = state_fixture()
    backend = Backend(state, data)
    backend.craft_jobs_supported = True
    cls = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    loop = cls(backend, policy='deterministic', target='rocket_launch', factory_scheduling='ready-work',
               tick_seconds=0, checkpoint=str(tmp_path/'combined.json'))
    loop.order = ['rocket_launch']
    loop._compile_candidates = lambda s: ([MiningOutpostPlanner(data, s, 'rocket_launch')._need(RESOURCE, 20)], '')
    assert loop.step()['verified'] and loop.step()['verified']
    loaded = load_checkpoint(loop.checkpoint, state.session_id, 'rocket_launch')
    assert loaded.background_schema == 2 and loaded.background_job is None
    assert loaded.input_routes_schema == 1 and loaded.outposts_schema == 1
    assert loaded.outpost_commitments == loop.memory.outpost_commitments
    assert len(backend.calls) == 2


def lone_investment_planner(state, data, amount=20):
    """A planner whose whole frontier is the policy-chosen outpost build."""
    planner = MiningOutpostPlanner(data, state, 'rocket_launch')
    primary = planner._need(RESOURCE, amount)
    assert primary.steps[0].action == outposts.COMMAND
    planner.plan = lambda: primary
    planner.focus = ('pipe', 41)
    return planner, primary


def native_direct_parent_demand_plans(*, late_annotations=False):
    """Build the direct alternative from a current atomic-decoder fixture."""
    state, data = state_fixture()
    data.recipes['pipe'] = recipe('pipe', {'iron-plate': 1})
    if late_annotations:
        # Reproduce the planner ordering where the outpost is remembered
        # before outer mixins attach current utility/economics annotations.
        data.technologies['automation'] = {
            'enabled': True, 'effects': [], 'prerequisites': [],
            'trigger': None, 'count': 10, 'energy_ticks': 60,
            'ingredients': [{'name': 'automation-science-pack', 'amount': 1}],
        }
        state.researched = []
        state.factory['researched'] = []
        state.factory['research'] = ''
        state.factory['entities']['utility:lab'] = machine(
            name='lab', unit_number=2548, energy=0)
    state.world_kind = 'fle'
    state.inventory = {'wooden-chest': 1, 'burner-mining-drill': 1, 'coal': 50}
    furnace = state.factory['entities']['recipe:iron-plate']
    furnace.update(unit_number=2547, name='stone-furnace', recipe='iron-plate',
                   fuel={'coal': 50}, input={}, output={}, products_finished=100,
                   crafting=False)
    state.factory['production_sites'] = {
        'protocol': 1, 'session_id': state.session_id, 'tick': state.tick,
        'sources': {'recipe:iron-plate': {
            'state': 'owned', 'reason': 'owned legacy furnace',
            'anchor': 'cell-site:legacy-iron-furnace',
            'position': {'x': 0, 'y': 0}, 'belt_count': 1,
            'bill': {'stone-furnace': 1, 'burner-mining-drill': 1,
                     'burner-inserter': 2, 'wooden-chest': 1,
                     'transport-belt': 1},
            'source_unit': 2547,
        }},
    }
    state.factory['acceptance_runtime'] = {
        'schema': 1, 'session_id': state.session_id, 'actor_unit': 17,
        'player_index': 1, 'surface_index': 1, 'force_index': 1,
        'speed': 1, 'tick_paused': False,
        'mods': {'base': data.version, 'core': data.version},
    }
    state = decode_native_fixture(state, native_catalog=data)
    planner = MiningOutpostPlanner(data, state, 'rocket_launch')
    planner._set_focus('pipe', 41)
    primary = planner._need(
        'iron-ore', 20, ('item:pipe', 'item:iron-plate'))
    assert primary.steps[0].action == outposts.COMMAND
    if late_annotations:
        remembered = planner._proposed_outposts[primary.id]['parent_purpose']
        assert 'utility_power_prerequisite' not in remembered
        assert 'economics' not in remembered
        from jev_factorio.planning.factory import FactoryPlanner
        utility_plan = FactoryPlanner(data, state, 'rocket_launch')._powered(
            'utility:lab', ('technology:automation',))
        assert utility_plan is not None
        economic_plan = planner._economic_evidence(
            primary, objective='unlock_basic_assembly',
            technology='automation', observed_tick=state.tick)
        materials = dict(primary.materials or {})
        materials['utility_power_prerequisite'] = deepcopy(
            utility_plan.materials['utility_power_prerequisite'])
        materials['economics'] = deepcopy(economic_plan.materials['economics'])
        primary = replace(primary, materials=materials)
    planner.plan = lambda: primary
    return state, data, primary, planner.candidates()


def test_native_direct_alternative_retains_current_parent_demand_without_payoff_claim():
    state, data, primary, plans = native_direct_parent_demand_plans()
    assert len(plans) == 2 and plans[0].id == primary.id
    parent, direct = plans
    assert direct.steps[0].action == 'factory_gather'
    assert direct.steps[0].parameters == {'resource': 'iron-ore', 'quantity': 41}
    assert direct.materials['local_objective'] == parent.materials['local_objective']

    row = candidate_evidence(state, data, plans)[direct.id]
    evidence = row['direct_alternative_parent_demand_start_evidence']
    assert evidence is not None
    assert row['work_scope'] == 'immediate'
    assert row['local_target'] == {
        'item': 'pipe', 'inventory_target': 41, 'ultimate_goal': 'rocket_launch'}
    assert evidence['parent_local_target_item'] == row['local_target']['item']
    assert evidence['parent_local_target_inventory'] == row['local_target']['inventory_target']
    assert evidence['parent_proposed_request_amount'] == 20
    assert evidence['ready_work_raw_target'] == 41
    assert evidence['current_direct_recipe_path'] == [
        'pipe', 'iron-plate', 'iron-ore']
    assert evidence['native_actor_bound_and_inventory_fresh'] is True
    assert evidence['useful_partial_benefit_level'] == 1
    assert evidence['does_not_establish_gathered_output_or_local_target_completion'] is True
    assert evidence['does_not_establish_outpost_payback_or_completion'] is True
    assert row['local_target_completion_evidence'] is None
    assert row['utility_power_prerequisite_start_evidence'] is None

    # The unchanged public request budget must still carry both choices and
    # the full evidence rows; this is prompt construction only, not a model call.
    support = scheduling_context(state, data, plans, 'rocket_launch')
    context, questions, offered = question_batch(
        {'facts': state.for_jev(), **support}, plans)
    assert [plan.id for plan in offered] == [plan.id for plan in plans]
    encoded = json.dumps(
        {'context': context, 'questions': questions}, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode('utf-8')
    assert len(encoded) <= 32000


def test_late_parent_utility_and_economics_annotations_rebind_from_final_plan():
    state, data, primary, plans = native_direct_parent_demand_plans(
        late_annotations=True)
    parent, direct = plans
    marker = direct.materials['direct_alternative_to_proposed_outpost']

    assert marker['parent_purpose']['utility_power_prerequisite'] == (
        parent.materials['utility_power_prerequisite'])
    assert marker['parent_purpose']['economics'] == parent.materials['economics']
    row = candidate_evidence(state, data, plans)[direct.id]
    evidence = row['direct_alternative_parent_demand_start_evidence']
    assert evidence is not None
    assert row['work_scope'] == 'immediate'
    assert evidence['parent_local_target_item'] == 'pipe'
    assert evidence['does_not_establish_outpost_payback_or_completion'] is True


def test_late_parent_purpose_still_requires_exact_economics_annotation():
    state, data, _, plans = native_direct_parent_demand_plans(
        late_annotations=True)
    _, direct = plans
    direct_materials = deepcopy(direct.materials)
    direct_materials['direct_alternative_to_proposed_outpost'][
        'parent_purpose']['economics']['technology'] = 'study'
    direct = replace(direct, materials=direct_materials)

    row = candidate_evidence(state, data, [plans[0], direct])[direct.id]
    assert row['direct_alternative_parent_demand_start_evidence'] is None
    assert row['work_scope'] == 'lookahead'


@pytest.mark.parametrize('invalid_field, invalid_value', [
    ('consumer_unit', 999),
    ('observed_tick', 299),
])
def test_late_parent_purpose_rebinding_keeps_current_utility_demand_gate(
        invalid_field, invalid_value):
    state, data, _, plans = native_direct_parent_demand_plans(
        late_annotations=True)
    parent, direct = plans
    parent_materials = deepcopy(parent.materials)
    direct_materials = deepcopy(direct.materials)
    annotation = parent_materials['utility_power_prerequisite']
    annotation[invalid_field] = invalid_value
    direct_materials['direct_alternative_to_proposed_outpost'][
        'parent_purpose']['utility_power_prerequisite'] = deepcopy(annotation)

    parent = replace(parent, materials=parent_materials)
    direct = replace(direct, materials=direct_materials)
    row = candidate_evidence(state, data, [parent, direct])[direct.id]
    assert row['direct_alternative_parent_demand_start_evidence'] is None
    assert row['work_scope'] == 'lookahead'


@pytest.mark.parametrize('tamper', [
    'stale_parent_tick',
    'parent_not_offered',
    'request_amount_mismatch',
    'dependency_path_mismatch',
    'local_target_mismatch',
    'stale_raw_prerequisite',
    'compiled_raw_target_mismatch',
    'native_fair_target_missing',
])
def test_native_direct_parent_demand_witness_fails_closed_on_stale_or_mismatched_inputs(tamper):
    state, data, _, plans = native_direct_parent_demand_plans()
    parent, direct = plans
    parent_materials = deepcopy(parent.materials)
    direct_materials = deepcopy(direct.materials)
    marker = direct_materials['direct_alternative_to_proposed_outpost']

    if tamper == 'stale_parent_tick':
        marker['observed_tick'] -= 1
    elif tamper == 'parent_not_offered':
        pass  # The candidate pair below intentionally omits the parent.
    elif tamper == 'request_amount_mismatch':
        marker['requested_amount'] += 1
    elif tamper == 'dependency_path_mismatch':
        wrong_path = ['pipe', 'iron-ore']
        requests = [
            parent_materials['proposed_outpost_request'],
            marker['proposed_outpost_request'],
            marker['parent_purpose']['proposed_outpost_request'],
        ]
        for request in requests:
            request['planner_item_path'] = list(wrong_path)
    elif tamper == 'local_target_mismatch':
        direct_materials['local_objective']['inventory_target'] += 1
    elif tamper == 'stale_raw_prerequisite':
        direct_materials['raw_prerequisite']['observed_tick'] -= 1
    elif tamper == 'compiled_raw_target_mismatch':
        marker['compiled_gather_target']['inventory_target'] += 1
    elif tamper == 'native_fair_target_missing':
        state.factory.get('fair_resource_targets', {}).pop('iron-ore', None)

    parent = replace(parent, materials=parent_materials)
    direct = replace(direct, materials=direct_materials)
    pair = [direct] if tamper == 'parent_not_offered' else [parent, direct]
    row = candidate_evidence(state, data, pair)[direct.id]
    assert row['direct_alternative_parent_demand_start_evidence'] is None
    assert row['work_scope'] != 'immediate'
    assert row['work_scope_provenance'] == {
        'compiled_scope': 'lookahead', 'qualified_current_scope': None,
        'basis': 'current_parent_demand_not_verified',
    }


@pytest.mark.parametrize('mutation', [
    lambda state, catalog, annotation: annotation.update(observed_tick=state.tick - 1),
    lambda state, catalog, annotation: annotation.update(consumer_unit=999),
    lambda state, catalog, annotation: catalog.technologies['study'].update(enabled=False),
])
def test_parent_utility_annotation_requires_same_tick_current_consumer_and_demand(mutation):
    from test_utility_power_prerequisite_evidence import fixture as utility_fixture
    from jev_factorio.planning.decision_support import _current_parent_utility_demand
    from jev_factorio.planning.factory import FactoryPlanner

    data, state, _ = utility_fixture(goal='rocket_launch', fuel=1, coal=50)
    state.world_kind = 'fle'
    state.factory['acceptance_runtime'] = {
        'schema': 1, 'session_id': state.session_id, 'actor_unit': 17,
        'player_index': 1, 'surface_index': 1, 'force_index': 1,
        'speed': 1, 'tick_paused': False,
        'mods': {'base': data.version, 'core': data.version},
    }
    state = decode_native_fixture(state, native_catalog=data)
    planner = FactoryPlanner(data, state, 'rocket_launch')
    power_plan = planner._powered('utility:lab', ('technology:study',))
    assert power_plan is not None
    annotation = deepcopy(power_plan.materials['utility_power_prerequisite'])
    assert _current_parent_utility_demand(
        state, data, annotation, 'rocket_launch') is not None

    changed_state, changed_catalog, changed_annotation = (
        deepcopy(state), deepcopy(data), deepcopy(annotation))
    mutation(changed_state, changed_catalog, changed_annotation)
    assert _current_parent_utility_demand(
        changed_state, changed_catalog, changed_annotation, 'rocket_launch') is None


def test_lone_proposed_outpost_investment_is_offered_with_the_direct_path():
    state, data = state_fixture()
    planner, primary = lone_investment_planner(state, data)
    plans = planner.candidates()
    assert len(plans) == 2 and plans[0].id == primary.id
    alternative = plans[1]
    step = alternative.steps[0]
    assert step.action in {'factory_gather', 'factory_extract'}
    assert step.allowed(state) and not step.satisfied(state)
    marker = alternative.materials['direct_alternative_to_proposed_outpost']
    assert marker['investment_plan_id'] == primary.id
    assert marker['resource'] == RESOURCE and marker['requested_amount'] == 20
    assert marker['observed_tick'] == state.tick
    assert 'direct_alternative_to_proposed_outpost' not in (plans[0].materials or {})
    assert plans[0].steps[0].action == outposts.COMMAND  # deterministic priority is unchanged
    assert len({plan.id for plan in plans}) == 2


def test_started_outpost_prefix_stays_the_only_candidate():
    state, data = state_fixture()
    build(state, 'chest')
    planner = MiningOutpostPlanner(data, state, 'rocket_launch')
    primary = planner._need(RESOURCE, 20)
    assert primary.steps[0].action == outposts.COMMAND and primary.steps[0].parameters['part'] == 'drill'
    planner.plan = lambda: primary
    planner.focus = ('pipe', 41)
    plans = planner.candidates()
    assert [plan.id for plan in plans] == [primary.id]
    assert all('direct_alternative_to_proposed_outpost' not in (plan.materials or {}) for plan in plans)


@pytest.mark.parametrize('mode', ['small', 'bootstrap', 'existing_ore'])
def test_direct_primary_is_not_given_a_second_alternative(mode):
    state, data = state_fixture()
    amount = 20
    if mode == 'small':
        amount = 2
    if mode == 'bootstrap':
        state.factory['entities']['recipe:iron-plate']['products_finished'] = 0
    if mode == 'existing_ore':
        state.factory['entities']['legacy:chest'] = machine('wooden-chest', output={RESOURCE: 50})
    planner = MiningOutpostPlanner(data, state, 'rocket_launch')
    primary = planner._need(RESOURCE, amount)
    assert primary.steps[0].action in {'factory_gather', 'factory_extract'}
    assert planner._proposed_outposts == {}
    planner.plan = lambda: primary
    planner.focus = ('pipe', 41)
    assert all('direct_alternative_to_proposed_outpost' not in (plan.materials or {})
               for plan in planner.candidates())


@pytest.mark.parametrize('kind', ['wait', 'none', 'same_plan', 'error', 'disallowed', 'satisfied'])
def test_unusable_direct_alternative_is_not_offered(kind, monkeypatch):
    from jev_factorio.planning.input_routes import InputRoutePlanner
    state, data = state_fixture()
    planner, primary = lone_investment_planner(state, data)
    direct = InputRoutePlanner._need(planner, RESOURCE, 20)

    def fake(self, item, amount, path=()):
        if kind == 'wait':
            return planner._wait('crafting_idle')
        if kind == 'none':
            return None
        if kind == 'same_plan':
            return primary
        if kind == 'error':
            raise ValueError('direct path unavailable')
        return direct

    if kind == 'disallowed':
        state.factory['player_bound'] = False
    monkeypatch.setattr(InputRoutePlanner, '_need', fake)
    if kind == 'satisfied':
        monkeypatch.setattr(type(direct.steps[0]), 'satisfied', lambda self, snapshot: True)
        monkeypatch.setattr(type(direct.steps[0]), 'allowed', lambda self, snapshot: True)
    if kind == 'disallowed':
        monkeypatch.setattr(type(direct.steps[0]), 'allowed', lambda self, snapshot: False)
    assert [plan.id for plan in planner.candidates()] == [primary.id]


def test_direct_alternative_survives_the_request_budget_and_both_plans_are_questioned():
    state, data = state_fixture()
    planner, primary = lone_investment_planner(state, data)
    plans = planner.candidates()
    context = {'facts': state.for_jev(), **scheduling_context(state, data, plans, 'rocket_launch')}
    _, questions, offered = question_batch(context, plans)
    assert [plan.id for plan in offered] == [plan.id for plan in plans]
    assert set(questions['candidate']['criteria']) == {plan.id for plan in plans} | {'observe'}
    for plan in plans:
        assert plan.id + '/benefit' in questions


def test_incomplete_kit_craft_primary_also_gets_the_direct_alternative():
    state, data = state_fixture()
    state.inventory = {'iron-plate': 5, 'coal': 5, 'wooden-chest': 1}
    planner = MiningOutpostPlanner(data, state, 'rocket_launch')
    primary = planner._need(RESOURCE, 20)
    assert primary.steps[0].action == 'factory_craft'
    assert primary.steps[0].parameters['recipe'] == 'burner-mining-drill'
    assert planner._proposed_outposts[primary.id]['item'] == RESOURCE
    planner.plan = lambda: primary
    planner.focus = ('pipe', 41)
    plans = planner.candidates()
    assert len(plans) == 2 and plans[0].id == primary.id
    alternative = plans[1]
    assert alternative.id != primary.id
    assert alternative.steps[0].action in {'factory_gather', 'factory_extract', 'factory_insert'}
    assert alternative.materials['direct_alternative_to_proposed_outpost']['investment_plan_id'] == primary.id


def test_direct_alternative_carries_the_production_batch_prefix():
    state, data = state_fixture()
    planner, primary = lone_investment_planner(state, data)
    alternative = planner.candidates()[1]
    assert alternative.description.startswith('Next production batch: 41 pipe. ')
    planner.focus = None
    assert not planner.candidates()[1].description.startswith('Next production batch')
