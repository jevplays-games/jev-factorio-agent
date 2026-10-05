"""Issue #99: deterministic quantity/ownership regressions, not a native run."""
from copy import deepcopy

import pytest

from jev_factorio.planning.demand import SupplyLedger
from jev_factorio.planning.input_routes import InputRoutePlanner
from jev_factorio.planning.fuel_service import service_plan
from test_maintenance_progress import progress_scenario


def due_scenario(coal=0):
    backend, data = progress_scenario(coal=coal)
    state = backend.state
    state.factory['entities']['out:chest']['output'].clear()
    state.factory['entities']['input:inserter']['fuel']['coal'] = 1
    return state, data


def test_combined_eight_coal_deficit_instead_of_repeated_four_coal_trips():
    state, data = due_scenario()
    plan = InputRoutePlanner(data, state, 'rocket_launch')._need('iron-plate', 10)
    assert plan.steps[0].action == 'factory_gather'
    assert plan.steps[0].parameters['quantity'] == 8
    assert plan.materials['fuel_service']['combined_deficit'] == 8
    assert plan.materials['fuel_service']['reserve_basis'].startswith('unknown_burn_rate')


def test_one_gather_then_two_distinct_sequential_refills_retain_remaining_coal():
    state, data = due_scenario()
    actions = []
    for _ in range(3):
        plan = InputRoutePlanner(data, state, 'rocket_launch')._need('iron-plate', 10)
        step = plan.steps[0]
        actions.append((step.action, dict(step.parameters)))
        if step.action == 'factory_gather':
            state.inventory['coal'] += step.parameters['quantity']
        else:
            n = step.parameters['quantity']
            state.inventory['coal'] -= n
            state.factory['entities'][step.parameters['role']]['fuel']['coal'] += n
    assert [action for action,_ in actions] == ['factory_gather','factory_insert','factory_insert']
    assert len({args['role'] for action,args in actions if action=='factory_insert'}) == 2
    assert state.inventory['coal'] == 0


def test_composed_controller_replans_two_burners_after_each_verified_action(tmp_path):
    """One public controller/actor records each dispatch and fresh verification."""
    from test_input_route_integration import RouteBackend, RouteLoop, controller

    class DemandPlanner(InputRoutePlanner):
        def plan(self):
            return self._need('iron-plate', 10)

    class DemandLoop(RouteLoop):
        planner_type = DemandPlanner

    class FuelBackend(RouteBackend):
        def execute(self, action, args):
            self.calls.append((action, deepcopy(args)))
            lose_acknowledgement = False
            if action == 'factory_gather':
                self.state.inventory['coal'] += args['quantity']
            elif action == 'factory_insert' and args['item'] == 'coal':
                self.state.inventory['coal'] -= args['quantity']
                entity = self.state.factory['entities'][args['role']]
                entity['fuel']['coal'] += args['quantity']
                self.state.factory['receipts'][args['receipt']] = {
                    'role': args['role'], 'unit_number': entity['unit_number'],
                    'item': 'coal', 'quantity': args['quantity'], 'extracting': False}
            else:
                raise AssertionError('Unrelated action reached grouped-service fixture')
            self.state.tick += 1
            for key in ('input_routes', 'output_buffers'):
                self.state.factory[key]['tick'] = self.state.tick
            return 'Synthetic action returned; verify fresh state'

    state, data = due_scenario()
    backend = FuelBackend()
    backend.state = state
    backend.enable_factory = lambda: data
    loop = controller(backend, tmp_path, kind=DemandLoop)
    records, boundaries = [], []
    for _ in range(8):
        # No subsequent controller decision is entered until the previous
        # public record has returned a verified action and cleared its attempt.
        if boundaries:
            assert boundaries[-1]['verified'] is True
            assert boundaries[-1]['pending_after'] is None
            assert boundaries[-1]['attempt_after'] is None
        calls_before = len(backend.calls)
        record = loop.step()
        records.append(deepcopy(record))
        calls_after = len(backend.calls)
        assert calls_after - calls_before in (0, 1)
        if calls_after > calls_before:
            action, args = backend.calls[-1]
            assert record['action'] == action
            assert record['verified'] is True
            assert record['pending'] is None and record['attempt'] is None
            outcome = record['attempt_outcomes'][-1]
            assert outcome['action'] == action and outcome['outcome'] == 'verified'
            assert outcome['dispatch_phases']['dispatch']['status'] == 'returned'
            if action == 'factory_insert':
                assert args['quantity'] == 4
                assert record['after_state']['factory']['entities'][args['role']]['fuel']['coal'] == 5
            boundaries.append({
                'action': action, 'parameters': deepcopy(args),
                'verified': record['verified'],
                'pending_after': deepcopy(record['pending']),
                'attempt_after': deepcopy(record['attempt']),
                'attempt_outcome': deepcopy(outcome),
            })
            assert len(boundaries) == len(backend.calls)
        if len(backend.calls) == 3 and loop.memory.pending is None:
            break
    else:
        pytest.fail('Grouped service did not settle within eight controller decisions')
    assert records[-1]['verified'] is True
    assert loop.memory.pending is None and loop.memory.attempt is None
    assert [action for action, _ in backend.calls] == [
        'factory_gather', 'factory_insert', 'factory_insert']
    assert backend.calls[0][1]['quantity'] == 8
    assert [args['quantity'] for action, args in backend.calls if action == 'factory_insert'] == [4, 4]
    assert {args['role'] for action, args in backend.calls[1:]} == {
        'input:inserter', 'input:drill'}
    assert len({backend.state.factory['entities'][args['role']]['unit_number']
                for action, args in backend.calls if action == 'factory_insert'}) == 2
    assert state.inventory['coal'] == 0
    final_observation = backend.observe()
    assert {role: final_observation.factory['entities'][role]['fuel']['coal']
            for role in ('input:inserter', 'input:drill')} == {
                'input:inserter': 5, 'input:drill': 5}
    assert [row['action'] for row in boundaries] == [
        'factory_gather', 'factory_insert', 'factory_insert']
    assert all(row['verified'] and row['pending_after'] is None
               and row['attempt_after'] is None
               and row['attempt_outcome']['outcome'] == 'verified'
               for row in boundaries)


def test_ambiguous_fuel_insert_blocks_next_mutation_until_receipt_is_reconciled(tmp_path):
    """A persisted ambiguous transfer waits for its exact receipt before replanning."""
    from test_input_route_integration import RouteBackend, RouteLoop, controller

    class DemandPlanner(InputRoutePlanner):
        def plan(self):
            return self._need('iron-plate', 10)

    class DemandLoop(RouteLoop):
        planner_type = DemandPlanner

    class DelayedReceiptBackend(RouteBackend):
        def __init__(self):
            super().__init__()
            self.delayed_receipt = None
            self.publish_delayed_receipt = False

        def execute(self, action, args):
            self.calls.append((action, deepcopy(args)))
            if action == 'factory_gather':
                self.state.inventory['coal'] += args['quantity']
            elif action == 'factory_insert' and args['item'] == 'coal':
                self.state.inventory['coal'] -= args['quantity']
                entity = self.state.factory['entities'][args['role']]
                entity['fuel']['coal'] += args['quantity']
                receipt_value = {
                    'role': args['role'], 'unit_number': entity['unit_number'],
                    'item': 'coal', 'quantity': args['quantity'], 'extracting': False}
                if self.delayed_receipt is None:
                    self.delayed_receipt = (args['receipt'], receipt_value)
                    lose_acknowledgement = True
                else:
                    self.state.factory['receipts'][args['receipt']] = receipt_value
                    lose_acknowledgement = False
            else:
                raise AssertionError('Unrelated action reached grouped-service fixture')
            self.state.tick += 1
            for key in ('input_routes', 'output_buffers'):
                self.state.factory[key]['tick'] = self.state.tick
            if action == 'factory_insert' and lose_acknowledgement:
                raise TimeoutError('Synthetic lost acknowledgement before receipt visibility')
            return 'Synthetic action returned; verify fresh state'

        def observe(self):
            snapshot = super().observe()
            if self.publish_delayed_receipt and self.delayed_receipt is not None:
                receipt, value = self.delayed_receipt
                snapshot.factory['receipts'][receipt] = deepcopy(value)
            return snapshot

    state, data = due_scenario()
    backend = DelayedReceiptBackend()
    backend.state = state
    backend.enable_factory = lambda: data
    loop = controller(backend, tmp_path, kind=DemandLoop)
    gather = loop.step()
    assert gather['action'] == 'factory_gather' and gather['verified'] is True
    assert backend.calls[0][1]['quantity'] == 8

    ambiguous = loop.step()
    assert ambiguous['action'] == 'factory_insert' and ambiguous['verified'] is False
    assert ambiguous['pending']['dispatch'] == 'ambiguous'
    assert ambiguous['attempt']['action'] == 'factory_insert'
    assert len(backend.calls) == 2

    resumed = controller(backend, tmp_path, kind=DemandLoop, resume=True)
    waiting = resumed.step()
    assert waiting['verified'] is False
    assert waiting['pending']['dispatch'] == 'ambiguous'
    assert waiting['attempt']['action'] == 'factory_insert'
    assert len(backend.calls) == 2

    backend.publish_delayed_receipt = True
    reconciled = resumed.step()
    assert reconciled['action'] == 'verify' and reconciled['verified'] is True
    assert reconciled['attempt_outcomes'][-1]['outcome'] == 'verified'
    assert reconciled['pending'] is None and reconciled['attempt'] is None
    assert len(backend.calls) == 2

    second_insert = resumed.step()
    assert second_insert['action'] == 'factory_insert' and second_insert['verified'] is True
    assert len(backend.calls) == 3
    assert backend.calls[1][1]['role'] == 'input:inserter'
    assert backend.calls[1][1]['quantity'] == 4
    assert backend.calls[2][1]['role'] == 'input:drill'
    assert backend.calls[2][1]['quantity'] == 4
    assert second_insert['after_state']['factory']['entities']['input:drill']['fuel']['coal'] == 5
    assert state.inventory['coal'] == 0
    assert resumed.memory.pending is None and resumed.memory.attempt is None


def test_legacy_aggregate_assertions_miss_modeled_split_and_barrier_mutations():
    """Mutation controls test assertion sensitivity; neither models a production failure."""
    def legacy_final_only_assertions(rows, carried, pending):
        assert len(rows) == 3
        assert rows[0]['action'] == 'factory_gather'
        assert rows[0]['quantity'] == 8
        assert {row['role'] for row in rows[1:]} == {'input:inserter', 'input:drill'}
        assert carried == 0
        assert rows[-1]['verified'] is True
        assert pending is None

    wrong_split = [
        {'action': 'factory_gather', 'quantity': 8, 'role': None, 'verified': True, 'pending': None},
        {'action': 'factory_insert', 'quantity': 3, 'role': 'input:inserter', 'fuel_after': 4,
         'verified': True, 'pending': None},
        {'action': 'factory_insert', 'quantity': 5, 'role': 'input:drill', 'fuel_after': 6,
         'verified': True, 'pending': None},
    ]
    legacy_final_only_assertions(wrong_split, carried=0, pending=None)
    with pytest.raises(AssertionError):
        assert [row['quantity'] for row in wrong_split if row['action'] == 'factory_insert'] == [4, 4]
    with pytest.raises(AssertionError):
        assert [row['fuel_after'] for row in wrong_split if row['action'] == 'factory_insert'] == [5, 5]

    premature_dispatch = [
        {'action': 'factory_gather', 'quantity': 8, 'role': None, 'verified': True, 'pending': None},
        {'action': 'factory_insert', 'quantity': 4, 'role': 'input:inserter',
         'verified': False, 'pending': {'dispatch': 'ambiguous'}},
        {'action': 'factory_insert', 'quantity': 4, 'role': 'input:drill', 'verified': True, 'pending': None},
    ]
    legacy_final_only_assertions(premature_dispatch, carried=0, pending=None)
    with pytest.raises(AssertionError):
        assert all(row['verified'] is True and row['pending'] is None
                   for row in premature_dispatch[:-1])


def test_held_coal_is_not_spent_or_counted_twice():
    state, data = due_scenario(10)
    planner = InputRoutePlanner(data, state, 'rocket_launch')
    planner.ledger = SupplyLedger.capture(state, data, reserved={'coal':10})
    plan = planner._need('iron-plate',10)
    assert plan.steps[0].action == 'factory_gather'
    assert plan.steps[0].parameters['quantity'] == 8
    assert plan.steps[0].threshold == 18
    assert plan.materials['fuel_service']['carried_held'] == 10


def test_partial_carried_fuel_is_useful_without_another_acquisition():
    state,data=due_scenario(2)
    plan=InputRoutePlanner(data,state,'rocket_launch')._need('iron-plate',10)
    assert plan.steps[0].action=='factory_insert'
    assert plan.steps[0].parameters['quantity']==2
    assert plan.steps[0].costs=={'coal':2}


@pytest.mark.parametrize('capacity',[0,1,3])
def test_full_or_limited_inventory_is_bounded(capacity):
    state,data=due_scenario()
    state.factory['inventory_insertable']={'coal':capacity}
    planner=InputRoutePlanner(data,state,'rocket_launch')
    if capacity==0:
        with pytest.raises(ValueError,match='capacity'): planner._need('iron-plate',10)
    else:
        plan=planner._need('iron-plate',10)
        assert plan.steps[0].parameters['quantity']==capacity


def test_alias_does_not_duplicate_native_consumer():
    state,data=due_scenario()
    route=state.factory['input_routes']['sources']['recipe:iron-plate']
    state.factory['entities']['alias']=deepcopy(state.factory['entities']['input:inserter'])
    route['parts']['drill']['role']='alias'
    # Direct policy test: contradictory route topology is separately rejected
    # by the actual route validator. The accounting itself must deduplicate.
    planner=InputRoutePlanner(data,state,'rocket_launch')
    plan=service_plan(planner,'input:inserter','recipe:iron-plate',(),planner._acquire)
    assert plan.materials['fuel_service']['combined_deficit']==4


def test_geometry_unavailable_is_unknown_not_a_zero_cost_reserve():
    state,data=due_scenario()
    state.factory['entities']['input:inserter'].pop('position')
    plan=InputRoutePlanner(data,state,'rocket_launch')._need('iron-plate',10)
    assert plan.materials['fuel_service']['lead_ticks_estimate'] is None
    assert plan.materials['fuel_service']['reserve']==0


def test_ready_science_and_stocked_output_still_avoid_fueling():
    state,data=due_scenario()
    state.factory['entities']['out:chest']['output']['iron-plate']=227
    plan=InputRoutePlanner(data,state,'rocket_launch')._need('iron-plate',10)
    assert plan.steps[0].action=='factory_extract'
    assert 'fuel_service' not in plan.materials


def test_changed_observation_recomputes_due_group_and_plan_roundtrips():
    from jev_factorio.skills import Plan
    state,data=due_scenario()
    first=InputRoutePlanner(data,state,'rocket_launch')._need('iron-plate',10)
    assert first.steps[0].parameters['quantity']==8
    state.factory['entities']['input:inserter']['fuel']['coal']=5
    changed=InputRoutePlanner(data,state,'rocket_launch')._need('iron-plate',10)
    assert changed.steps[0].parameters['quantity']==4
    assert Plan.from_dict(changed.to_dict()).to_dict()==changed.to_dict()


@pytest.mark.parametrize('alias_fuel', [0, 2, 5])
def test_conflicting_native_alias_telemetry_does_not_authorize_service(alias_fuel):
    state,data=due_scenario()
    route=state.factory['input_routes']['sources']['recipe:iron-plate']
    state.factory['entities']['alias']=deepcopy(state.factory['entities']['input:inserter'])
    state.factory['entities']['alias']['fuel']['coal']=alias_fuel
    route['parts']['drill']['role']='alias'
    planner=InputRoutePlanner(data,state,'rocket_launch')
    with pytest.raises(ValueError,match='Aliased'):
        service_plan(planner,'input:inserter','recipe:iron-plate',(),planner._acquire)
