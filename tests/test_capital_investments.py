"""Real planner/controller paths over synthetic worlds, not native throughput claims."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from jev_factorio.controller import HierarchicalLoop
from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.input_controller import input_loop_type
from jev_factorio.outpost_controller import MiningOutpostMixin, outpost_loop_type
from jev_factorio.memory import CampaignMemory, load_checkpoint
from jev_factorio.planning import capital
from jev_factorio import capital_controller
from jev_factorio.planning.ready_work import ReadyWorkPlanner
from jev_factorio.planning.input_routes import InputRoutePlanner
from jev_factorio.planning.mining_outposts import MiningOutpostPlanner
from jev_factorio.planning.factory import FactoryPlanner
from jev_factorio.planning.decision_support import scheduling_context
from jev_factorio.skills import Plan, Step
from jev_factorio.telemetry import make_attempt
from test_economic_production import economic_catalog, economic_state
from test_factory import machine, recipe

ITEM = 'iron-gear-wheel'
ROLE = 'recipe:' + ITEM
MACHINE = 'assembling-machine-1'


def scenario():
    data, state = economic_catalog(), economic_state()
    state.tick = 1000
    state.inventory = {'iron-plate': 200, 'coal': 50}
    data.recipes[MACHINE] = recipe(MACHINE, {ITEM: 5})
    data.recipes['automation-science-pack'] = recipe('automation-science-pack', {ITEM: 5})
    data.recipes['automation-science-pack']['energy'] = 5
    # Already commissioned science machine; the investment frontier can focus on gears.
    state.factory['entities']['recipe:automation-science-pack'] = machine(MACHINE, unit_number=40,
        recipe='automation-science-pack', energy=100, electric_network_id=1, products_finished=100)
    state.factory['entities']['utility:lab']['input'] = {'automation-science-pack': 20}
    for capability in ('output_buffers', 'input_routes', 'mining_outposts'):
        state.factory[capability] = {'protocol': 1, 'session_id': state.session_id,
                                    'tick': state.tick, 'sources': {}}
    return data, state


def offer(data, state, kind=ReadyWorkPlanner):
    planner = kind(data, state, 'rocket_launch')
    return planner._need(ITEM, 20)


class Backend:
    """Explicit synthetic native payment/counter model; no FLE initialization."""
    output_buffers_supported = input_routes_supported = mining_outposts_supported = True
    craft_jobs_supported = True
    def __init__(self, data, state):
        self.data, self.state, self.calls = data, state, []
        self.lose_place_ack = False
        self.place_effect = True
    def enable_factory(self):
        return self.data
    def observe(self):
        return deepcopy(self.state)
    def act(self, action):
        assert action == 'idle'
        return 'synthetic clock observation'
    def execute(self, action, p):
        self.calls.append((action, deepcopy(p)))
        state = self.state
        if action == 'factory_craft':
            r, n = self.data.recipes[p['recipe']], p['batches']
            for i in r['ingredients']:
                assert state.inventory.get(i['name'], 0) >= i['amount'] * n
                state.inventory[i['name']] -= i['amount'] * n
            for product in r['products']:
                state.inventory[product['name']] = state.inventory.get(product['name'], 0) + product['amount'] * n
        elif action == 'factory_place':
            assert p['role'] not in state.factory['entities']
            if self.place_effect:
                assert state.inventory[p['name']] >= 1
                state.inventory[p['name']] -= 1
                state.factory['entities'][p['role']] = machine(p['name'], unit_number=80,
                    products_finished=0, energy=100, electric_network_id=1)
            if self.lose_place_ack:
                raise TimeoutError('synthetic lost acknowledgement')
        elif action == 'factory_configure':
            state.factory['entities'][p['role']]['recipe'] = p['recipe']
        elif action in {'factory_insert', 'factory_extract'}:
            m = state.factory['entities'][p['role']]
            item, n = p['item'], p['quantity']
            extracting = action == 'factory_extract'
            if extracting:
                assert m['output'].get(item, 0) >= n
                m['output'][item] -= n
                state.inventory[item] = state.inventory.get(item, 0) + n
            else:
                assert state.inventory.get(item, 0) >= n
                state.inventory[item] -= n
                section = 'fuel' if item == 'coal' else 'input'
                m[section][item] = m[section].get(item, 0) + n
            state.factory['receipts'][p['receipt']] = dict(p, unit_number=m['unit_number'], extracting=extracting)
        elif action == 'factory_wait':
            pass
        else:
            raise AssertionError(action)
        return 'synthetic contract acknowledgement'
    def advance(self, ticks=60, produce=False):
        self.state.tick += ticks
        for key in ('input_routes', 'output_buffers', 'mining_outposts'):
            if key in self.state.factory:
                self.state.factory[key]['tick'] = self.state.tick
        if produce:
            m = self.state.factory['entities'][ROLE]
            r = self.data.recipes[ITEM]
            n = min(int(m['input'].get(i['name'], 0) / i['amount']) for i in r['ingredients'])
            for i in r['ingredients']:
                m['input'][i['name']] -= i['amount'] * n
            m['output'][ITEM] = m['output'].get(ITEM, 0) + n
            m['products_finished'] += n


def make_loop(backend, path=None, resume=False, kind=HierarchicalLoop, primary=None):
    # No outpost runtime attached for controller types without that capability.
    if not issubclass(kind, MiningOutpostMixin):
        backend.state.factory.pop('mining_outposts', None)
    loop = kind(backend, policy='deterministic', target='rocket_launch', factory_scheduling='ready-work',
                checkpoint=str(path) if path else None, resume_controller=resume, tick_seconds=0)
    if not resume:
        loop.memory = loop.memory_type(backend.state.session_id, 'rocket_launch', active_goal='rocket_launch',
            last_tick=backend.state.tick, completed_goals={g: 1 for g in loop.order[:-1]})
    # Retain the actual final frontier and dispatcher; choose only the ordinary baseline task.
    if primary:
        loop._compile_candidates = lambda s: ([primary(s)], '')
    return loop


def seed_commit(loop, state, plan):
    capital_controller.commit(loop, plan, state)
    return loop.memory.capital_investment


@pytest.mark.parametrize('owner', ['coal_funding', 'solid_funding'])
def test_outer_capital_frontier_and_commit_respect_retained_funding(owner):
    data, state = scenario()
    loop = make_loop(Backend(data, state), primary=lambda s: wait(data, s))
    plans, _ = capital_controller.frontier(loop, state)
    selected = next(p for p in plans if capital.MARKER in (p.materials or {}))
    retained = {'key': 'retained-funding'}
    setattr(loop.memory, owner, retained)
    plans, _ = capital_controller.frontier(loop, state)
    assert not any(capital.MARKER in (p.materials or {}) for p in plans)
    with pytest.raises(ValueError, match='Cannot start capital commitment'):
        capital_controller.commit(loop, selected, state)
    assert loop.memory.capital_investment is None
    assert getattr(loop.memory, owner) == retained


def wait(data, state):
    return ReadyWorkPlanner(data, state, 'rocket_launch')._wait('research_progress', 'study', 0.1)


@pytest.mark.parametrize('kind', [ReadyWorkPlanner, InputRoutePlanner, MiningOutpostPlanner])
def test_self_ingredient_uses_guarded_kit_then_machine(kind):
    data, state = scenario()
    before = deepcopy(state)
    first = offer(data, state, kind)
    assert first.steps[0].parameters == {'recipe': ITEM, 'batches': 5}
    assert first.steps[0].costs == {'iron-plate': 10}
    assert first.materials[capital.MARKER]['stage'] == 'kit'
    assert state == before
    state.inventory[ITEM] = 5
    second = offer(data, state, kind)
    assert second.steps[0].parameters == {'recipe': MACHINE, 'batches': 1}
    assert second.steps[0].costs == {ITEM: 5}
    assert second.materials[capital.MARKER]['spec']['key'] == first.materials[capital.MARKER]['spec']['key']


def test_constructor_guard_restored_even_when_bootstrap_raises(monkeypatch):
    data, state = scenario()
    plan = offer(data, state)
    planner = ReadyWorkPlanner(data, state, 'rocket_launch')
    monkeypatch.setattr(planner, '_machine', lambda *args: (_ for _ in ()).throw(ValueError('bad fixture')))
    with pytest.raises(ValueError):
        capital.continuation(planner, plan.materials[capital.MARKER]['spec'])
    assert planner._economic_acquiring is False


@pytest.mark.parametrize('kind', [HierarchicalLoop, buffered_loop_type(HierarchicalLoop),
    input_loop_type(buffered_loop_type(HierarchicalLoop)),
    outpost_loop_type(input_loop_type(buffered_loop_type(HierarchicalLoop)))])
def test_full_lifecycle_survives_restart_and_proves_native_output(tmp_path, kind):
    data, state = scenario()
    backend = Backend(data, state)
    path = tmp_path / 'campaign.json'
    task = lambda s: offer(data, s)
    loop = make_loop(backend, path, kind=kind, primary=task)
    before_stock = state.inventory['iron-plate']
    stages = []
    for n in range(5):
        record = loop.step()
        assert record['verified'] and loop.memory.capital_investment is not None
        stages.append(loop.memory.capital_investment['stage'])
        backend.advance()
        loop = make_loop(backend, path, resume=True, kind=kind, primary=task)
    assert [a for a, _ in backend.calls] == ['factory_craft', 'factory_craft', 'factory_place',
                                            'factory_configure', 'factory_insert']
    assert stages[:4] == ['kit', 'kit', 'build', 'configure']
    assert stages[4] in {'configure', 'supply'}  # Ordinary urgent refill may preempt the tagged stage.
    # Placement/configuration/payment do not count as demonstrated production.
    loaded = load_checkpoint(path, state.session_id, 'rocket_launch')
    assert loaded.capital_investment['unit_number'] == 80
    assert loaded.capital_investment['products_baseline'] == 0
    assert not state.factory['entities'][ROLE]['output']
    assert before_stock - state.inventory['iron-plate'] == 50  # 10 for kit + 40 for twenty gears.
    backend.advance(produce=True)
    # Observe releases intent only after the bound machine counter and output grew.
    loop._observe()
    assert loop.memory.capital_investment is None
    assert any(e['kind'] == 'capital_completed' for e in loop.memory.history)
    assert sum(a == 'factory_place' for a, _ in backend.calls) == 1
    assert state.factory['entities'][ROLE]['output'][ITEM] == 20
    # Reuse the paid machine, not another producer or continued handcrafting.
    plan = offer(data, state)
    assert plan.steps[0].action == 'factory_extract'
    assert not (plan.materials or {}).get(capital.MARKER)


def test_kit_holds_are_real_current_stock_not_forecast():
    data, state = scenario()
    backend = Backend(data, state)
    loop = make_loop(backend)
    intent = seed_commit(loop, state, offer(data, state))
    assert capital.held_kit(intent, state, data) == {'iron-plate': 10}
    state.inventory = {ITEM: 3, 'iron-plate': 4, MACHINE: 0}
    assert capital.held_kit(intent, state, data) == {ITEM: 3, 'iron-plate': 4}
    state.factory['entities']['recipe:iron-plate'] = machine(output={'iron-plate': 1000})
    assert capital.held_kit(intent, state, data) == {ITEM: 3, 'iron-plate': 4}
    state.inventory[MACHINE] = 1
    assert capital.held_kit(intent, state, data) == {MACHINE: 1}


@pytest.mark.parametrize('spend,allowed', [(5, True), (190, True), (191, False), (200, False)])
def test_other_tasks_cannot_consume_earmarked_kit(spend, allowed):
    data, state = scenario()
    loop = make_loop(Backend(data, state))
    intent = seed_commit(loop, state, offer(data, state))
    p = ReadyWorkPlanner(data, state, 'rocket_launch')._transfer('utility:lab', 'iron-plate', spend)
    assert capital.costs_allowed(p, state, intent, data) is allowed
    assert loop._investment_step_allowed(p, p.steps[0], state) is allowed


def test_urgent_boiler_maintenance_preempts_without_stealing_kit():
    data, state = scenario()
    state.factory['entities']['utility:boiler']['fuel']['coal'] = 0
    backend = Backend(data, state)
    task = lambda s: ReadyWorkPlanner(data, s, 'rocket_launch')._transfer('utility:boiler', 'coal', 50)
    loop = make_loop(backend, primary=task)
    seed_commit(loop, state, offer(data, state))
    original = deepcopy(loop.memory.capital_investment)
    result = loop.step()
    assert result['verified'] and backend.calls[0][0] == 'factory_insert'
    assert state.factory['entities']['utility:boiler']['fuel']['coal'] == 50
    assert state.inventory['iron-plate'] == 200
    assert loop.memory.capital_investment == original


def test_research_wait_exposes_investment_without_changing_research():
    data, state = scenario()
    state.factory['entities']['utility:lab']['input']['automation-science-pack'] = 100
    backend = Backend(data, state)
    loop = make_loop(backend, primary=lambda s: wait(data, s))
    before = deepcopy(state)
    plans, blocker = loop._work_candidates(state)
    assert any(capital.MARKER in (p.materials or {}) for p in plans)
    assert loop.memory.capital_investment is None and state == before
    context = scheduling_context(state, data, plans, 'rocket_launch')
    best = next(p for p in plans if p.id == context['deterministic_ranking'][0])
    assert capital.MARKER in best.materials
    assert state.factory['research'] == 'study'


def test_current_native_precondition_blocks_stale_capital_priority():
    data, state = scenario()
    plan = offer(data, state)
    row = scheduling_context(state, data, [plan], 'rocket_launch')['candidate_evidence'][plan.id]
    assert row['urgency'] == 1
    state.tick += 1
    row = scheduling_context(state, data, [plan], 'rocket_launch')['candidate_evidence'][plan.id]
    assert row['urgency'] == 0


@pytest.mark.parametrize('change', ['empty_output', 'counter_only', 'output_only', 'no_counter'])
def test_placement_time_and_one_sided_evidence_do_not_complete(change):
    data, state = scenario()
    loop = make_loop(Backend(data, state))
    intent = seed_commit(loop, state, offer(data, state))
    intent.update(unit_number=80, products_baseline=0, stage='verify')
    m = machine(MACHINE, unit_number=80, recipe=ITEM, products_finished=0, output={})
    if change == 'counter_only': m['products_finished'] = 1
    if change == 'output_only': m['output'] = {ITEM: 1}
    if change == 'no_counter': m.pop('products_finished'); m['output'] = {ITEM: 1}
    state.factory['entities'][ROLE] = m
    state.tick += 100000
    capital_controller.observe(loop, state)
    assert loop.memory.capital_investment is not None
    assert not any(e['kind'] == 'capital_completed' for e in loop.memory.history)


@pytest.mark.parametrize('change', ['missing', 'replaced', 'wrong_name', 'wrong_recipe', 'counter_regressed', 'nan_output'])
def test_owned_machine_anomalies_fail_closed(change):
    data, state = scenario()
    loop = make_loop(Backend(data, state))
    intent = seed_commit(loop, state, offer(data, state))
    intent.update(unit_number=80, products_baseline=5, stage='verify')
    m = machine(MACHINE, unit_number=80, recipe=ITEM, products_finished=5)
    state.factory['entities'][ROLE] = m
    if change == 'missing': del state.factory['entities'][ROLE]
    if change == 'replaced': m['unit_number'] = 81
    if change == 'wrong_name': m['name'] = 'stone-furnace'
    if change == 'wrong_recipe': m['recipe'] = 'automation-science-pack'
    if change == 'counter_regressed': m['products_finished'] = 4
    if change == 'nan_output': m['output'] = {ITEM: float('nan')}
    capital_controller.observe(loop, state)
    assert loop.memory.status == 'uncertain' and loop._execution_barrier(state)
    assert loop.memory.capital_investment is not None


def test_untracked_same_named_machine_is_not_adopted():
    data, state = scenario()
    loop = make_loop(Backend(data, state))
    seed_commit(loop, state, offer(data, state))
    state.factory['entities'][ROLE] = machine(MACHINE, recipe=ITEM)
    capital_controller.observe(loop, state)
    assert loop.memory.status == 'uncertain'
    assert loop.memory.capital_investment['unit_number'] is None


@pytest.mark.parametrize('effect', [True, False])
def test_lost_placement_ack_never_replays_on_resume(tmp_path, effect):
    data, state = scenario()
    state.inventory[MACHINE] = 1
    backend = Backend(data, state)
    backend.lose_place_ack, backend.place_effect = True, effect
    path = tmp_path / 'state.json'
    loop = make_loop(backend, path, primary=lambda s: offer(data, s))
    loop.step()
    assert loop.memory.pending['dispatch'] == 'ambiguous'
    loop = make_loop(backend, path, resume=True, primary=lambda s: offer(data, s))
    result = loop.step()
    assert len(backend.calls) == 1
    if effect:
        assert result['verified']
        assert loop.memory.capital_investment['unit_number'] == 80
    else:
        assert loop.memory.pending is not None
        assert loop.memory.capital_investment['unit_number'] is None


def test_intent_fault_does_not_release_a_pending_action(tmp_path):
    data, state = scenario()
    state.inventory[MACHINE] = 1
    backend = Backend(data, state)
    backend.lose_place_ack = True
    path = tmp_path / 'state.json'
    loop = make_loop(backend, path, primary=lambda s: offer(data, s))
    loop.step()
    loop._observe()  # First bind the native unit from the retained placement.
    state.factory['entities'][ROLE]['unit_number'] = 999
    pending = deepcopy(loop.memory.pending)
    result = loop.step()
    assert result['status'] == 'uncertain' and not result['verified']
    assert loop.memory.pending == pending
    assert len(backend.calls) == 1


@pytest.mark.parametrize('kind', [BackgroundWorkLoop,
    outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))])
@pytest.mark.parametrize('identity_fault', ['missing', 'replaced'])
def test_background_capital_identity_fault_retains_ambiguous_placement(
        tmp_path, kind, identity_fault):
    data, state = scenario()
    state.inventory[MACHINE] = 1
    backend = Backend(data, state)
    backend.lose_place_ack = True
    path = tmp_path / 'background-capital.json'
    loop = make_loop(backend, path, kind=kind, primary=lambda s: offer(data, s))

    first = loop.step()
    assert first['action'] == 'factory_place' and not first['verified']
    assert loop.memory.pending['dispatch'] == 'ambiguous'
    assert len(backend.calls) == 1
    loop._observe()  # Bind the real placement through the capital observer.
    assert loop.memory.capital_investment['unit_number'] == 80

    pending = deepcopy(loop.memory.pending)
    attempt = deepcopy(loop.memory.attempt)
    intent = deepcopy(loop.memory.capital_investment)
    active_plan = deepcopy(loop.memory.active_plan)
    outcomes = deepcopy(loop.memory.attempt_outcomes)
    failures = deepcopy(loop.memory.failures)
    stalled = loop.memory.stalled_decisions
    if identity_fault == 'missing':
        del state.factory['entities'][ROLE]
    else:
        state.factory['entities'][ROLE]['unit_number'] = 81

    result = loop.step()

    assert loop._capital_fault and loop._execution_barrier(state)
    assert loop.memory.status == result['status'] == 'uncertain'
    assert not result['verified'] and result['action'] == 'observe'
    assert loop.memory.pending == pending
    assert loop.memory.attempt == attempt
    assert loop.memory.capital_investment == intent
    assert loop.memory.active_plan == active_plan
    assert loop.memory.attempt_outcomes == outcomes
    assert loop.memory.failures == failures
    assert loop.memory.stalled_decisions == stalled
    assert len(backend.calls) == 1
    assert state.inventory[MACHINE] == 0
    loaded = loop.memory_type.load(path, state.session_id, 'rocket_launch')
    assert loaded.pending == pending and loaded.attempt == attempt
    assert loaded.capital_investment == intent
    assert loaded.active_plan == json.loads(json.dumps(active_plan))

    result = loop.step()
    assert result['status'] == 'uncertain' and not result['verified']
    assert loop.memory.pending == pending and loop.memory.attempt == attempt
    assert loop.memory.capital_investment == intent
    assert len(backend.calls) == 1


def test_catalog_change_does_not_silently_reprice_committed_kit():
    data, state = scenario()
    loop = make_loop(Backend(data, state))
    seed_commit(loop, state, offer(data, state))
    data.recipes[MACHINE]['ingredients'][0]['amount'] = 100
    capital_controller.observe(loop, state)
    assert loop.memory.status == 'uncertain'


@pytest.mark.parametrize('field,value', [('stage', 'finished'), ('started_tick', -1),
    ('deadline_tick', 0), ('deadline_tick', 1000000000), ('unit_number', True),
    ('products_baseline', -1), ('products_baseline', 1)])
def test_invalid_commitment_checkpoint_rejected(tmp_path, field, value):
    data, state = scenario()
    loop = make_loop(Backend(data, state))
    seed_commit(loop, state, offer(data, state))
    record = asdict(loop.memory)
    record['capital_investment'][field] = value
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        load_checkpoint(path, state.session_id, 'rocket_launch')


@pytest.mark.parametrize('field,value', [('schema', True), ('workload', 1), ('workload', 2001),
    ('queue_ticks', 0), ('investment_ticks', -1), ('batches', 21), ('key', 'forged'), ('role', 'other')])
def test_invalid_frozen_investment_spec_rejected(field, value):
    data, state = scenario()
    spec = deepcopy(offer(data, state).materials[capital.MARKER]['spec'])
    spec[field] = value
    with pytest.raises(ValueError): capital.validate_spec(spec)


def test_legacy_memory_without_investment_round_trips_unchanged(tmp_path):
    memory = CampaignMemory('test', 'bootstrap_mining')
    path = tmp_path / 'legacy.json'
    memory.save(path)
    assert 'capital_investment' not in json.loads(path.read_text())
    assert CampaignMemory.load(path, 'test', 'bootstrap_mining').capital_investment is None


def test_expiry_preserves_paid_machine_and_does_not_restart_investment():
    data, state = scenario()
    loop = make_loop(Backend(data, state), primary=lambda s: wait(data, s))
    intent = seed_commit(loop, state, offer(data, state))
    state.tick = intent['deadline_tick']
    loop.memory.last_tick = state.tick
    state.factory['entities'][ROLE] = machine(MACHINE, unit_number=80, recipe=ITEM, products_finished=0)
    intent['unit_number'] = 80
    before = deepcopy(state.factory['entities'][ROLE])
    plans, _ = loop._work_candidates(state)
    assert loop.memory.capital_investment is None
    assert loop.memory.failures[intent['spec']['key']] == 2
    assert state.factory['entities'][ROLE] == before
    assert all((p.materials or {}).get(capital.MARKER, {}).get('spec', {}).get('key') != intent['spec']['key'] for p in plans)


def test_expiry_cannot_clear_pending_or_background_work():
    data, state = scenario()
    loop = make_loop(Backend(data, state), primary=lambda s: wait(data, s))
    intent = seed_commit(loop, state, offer(data, state))
    state.tick = intent['deadline_tick']
    loop.memory.pending = {'started_tick': 1000, 'polls': 0, 'action': 'factory_wait', 'dispatch': 'returned'}
    loop._work_candidates(state)
    assert loop.memory.capital_investment is intent
    assert loop.memory.pending is not None


def test_serial_planner_does_not_start_optional_capital():
    data, state = scenario()
    step = FactoryPlanner(data, state, 'rocket_launch')._need(ITEM, 20).steps[0]
    assert step.action == 'factory_craft' and step.parameters['batches'] == 20

class CraftBackend(Backend):
    def __init__(self, data, state):
        super().__init__(data, state)
        state.factory.update(craft_jobs_protocol=1, craft_job_actor={
            'session_id': state.session_id, 'player_index': 1, 'unit_number': 9,
            'surface_index': 1, 'force_index': 1})
    def execute(self, action, p):
        if action != 'factory_craft_job':
            return super().execute(action, p)
        self.calls.append((action, deepcopy(p)))
        r, n = self.data.recipes[p['recipe']], p['batches']
        inputs = {i['name']: int(i['amount'] * n) for i in r['ingredients']}
        outputs = {i['name']: int(i['amount'] * n) for i in r['products']}
        for item, count in inputs.items():
            assert self.state.inventory.get(item, 0) >= count
            self.state.inventory[item] -= count
        self.state.factory.update(crafting_queue=1, craft_job={
            **self.state.factory['craft_job_actor'], 'id': p['receipt'], 'recipe': p['recipe'],
            'requested': n, 'accepted': n, 'finished': 0, 'started_tick': self.state.tick,
            'last_progress_tick': self.state.tick, 'inputs': inputs, 'outputs': outputs,
            'baseline': {i: self.state.inventory.get(i, 0) for i in outputs}, 'status': 'running',
            'queue_valid': True, 'paid': True})
        return 'synthetic accepted craft receipt'
    def complete_craft(self):
        self.advance(60)
        job = self.state.factory['craft_job']
        for item, count in job['outputs'].items():
            self.state.inventory[item] = self.state.inventory.get(item, 0) + count
        job.update(status='completed', finished=job['accepted'], last_progress_tick=self.state.tick,
                   completed_tick=self.state.tick)
        self.state.factory['crafting_queue'] = 0


@pytest.mark.parametrize('kind', [BackgroundWorkLoop,
    outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))])
def test_background_bootstrap_retains_intent_and_output_locks_across_restart(tmp_path, kind):
    data, state = scenario()
    backend = CraftBackend(data, state)
    path = tmp_path / 'background.json'
    def setup(resume=False):
        loop = make_loop(backend, path, resume, kind=kind)
        loop._compile_candidates = lambda s: ([loop._tracked_plan(offer(data, s), s)], '')
        return loop
    loop = setup()
    first = loop.step()
    assert not first['verified'] and loop.memory.background_job and loop.memory.capital_investment
    assert loop.memory.pending is None
    assert state.inventory['iron-plate'] == 190
    loop = setup(True)
    result = loop.step()
    assert not any(a == 'factory_place' for a, _ in backend.calls)
    assert loop.memory.background_job and loop.memory.capital_investment
    backend.complete_craft()
    # Existing acknowledged craft must be observed before using its output in the kit.
    loop.step()
    if loop.memory.pending: loop.step()
    if len([a for a, _ in backend.calls if a == 'factory_craft_job']) == 1: loop.step()
    assert [p['recipe'] for a, p in backend.calls if a == 'factory_craft_job'] == [ITEM, MACHINE]
    assert loop.memory.background_job and loop.memory.capital_investment
    backend.complete_craft()
    for _ in range(4):
        loop.step()
        backend.advance()
        if any(a == 'factory_place' for a, _ in backend.calls): break
    assert sum(a == 'factory_place' for a, _ in backend.calls) == 1
    assert loop.memory.capital_investment['unit_number'] == 80


def test_two_reconciled_step_failures_abandon_only_intent():
    data, state = scenario()
    loop = make_loop(Backend(data, state))
    plan = offer(data, state)
    intent = seed_commit(loop, state, plan)
    for _ in range(2):
        loop.memory.active_plan = plan.to_dict()
        loop._fail_plan('synthetic precondition change')
    assert loop.memory.capital_investment is None
    assert loop.memory.failures[plan.id] == 2
    assert loop.memory.failures[intent['spec']['key']] == 2
    assert not loop.backend.calls


def test_exhausted_optional_investment_does_not_block_normal_work():
    data, state = scenario()
    loop = make_loop(Backend(data, state), primary=lambda s: offer(data, s))
    plan = offer(data, state)
    loop.memory.failures[plan.materials[capital.MARKER]['spec']['key']] = 2
    plans, _ = loop._work_candidates(state)
    assert plans
    assert all(capital.MARKER not in (p.materials or {}) for p in plans)


@pytest.mark.parametrize('dispatch', ['prepared', 'ambiguous', 'returned'])
def test_research_pending_yield_keeps_original_acknowledgement_rules(dispatch):
    data, state = scenario()
    backend = CraftBackend(data, state)
    loop = make_loop(backend, kind=BackgroundWorkLoop, primary=lambda s: wait(data, s))
    passive = wait(data, state)
    loop.memory.active_plan = passive.to_dict()
    loop.memory.pending = {'started_tick': state.tick, 'polls': 0, 'action': 'factory_wait', 'dispatch': dispatch}
    loop.memory.attempt = make_attempt(state.session_id, 'rocket_launch', passive.to_dict(), 0,
                                      loop.memory.pending, process_id=loop._process_id)
    result = loop._verify_pending(state)
    assert not result['verified'] and not backend.calls and loop.memory.capital_investment is None
    assert (loop.memory.pending is None) is (dispatch == 'returned')


def test_capital_spec_does_not_change_for_unrelated_catalog_entry():
    data, state = scenario()
    plan = offer(data, state)
    data.recipes['unrelated-widget'] = recipe('unrelated-widget', {'stone': 3})
    capital.validate_spec(plan.materials[capital.MARKER]['spec'], data, state.researched)


def test_budget_is_per_committed_objective_not_reset_by_each_replan():
    data, state = scenario()
    loop = make_loop(Backend(data, state))
    first = offer(data, state)
    intent = seed_commit(loop, state, first)
    deadline = intent['deadline_tick']
    state.tick += 1000
    state.inventory[ITEM] = 5
    following = capital.continuation(ReadyWorkPlanner(data, state, 'rocket_launch'), intent['spec'])
    capital_controller.commit(loop, following, state)
    assert intent['deadline_tick'] == deadline and intent['started_tick'] == 1000


def test_serial_controller_rejects_active_capital_intent():
    data, state = scenario()
    loop = make_loop(Backend(data, state))
    seed_commit(loop, state, offer(data, state))
    loop.factory_scheduling = 'serial'
    capital_controller.observe(loop, state)
    assert loop.memory.status == 'uncertain' and not loop.backend.calls


def test_late_verified_placement_can_reconcile_uncertain_without_identity_fault(tmp_path):
    data, state = scenario()
    state.inventory[MACHINE] = 1
    backend = Backend(data, state)
    backend.lose_place_ack = True
    loop = make_loop(backend, tmp_path / 'late.json', primary=lambda s: offer(data, s))
    loop.step()
    loop.memory.status = 'uncertain'  # Existing pending timeout, not capital identity corruption.
    result = loop.step()
    assert result['verified'] and loop.memory.pending is None and not loop._capital_fault
    assert len(backend.calls) == 1


def test_capital_events_have_valid_durable_research_integrity(tmp_path):
    from jev_factorio.research_log import ResearchLog, RunConfiguration, verify_run
    from jev_factorio.causal_trace import CausalTrace
    data, state = scenario()
    backend = Backend(data, state)
    directory = tmp_path / 'evidence'
    with ResearchLog(directory, RunConfiguration('mock', 'hierarchical', 'deterministic',
                     target='rocket_launch', factory_scheduling='ready-work'), environ={}) as sink:
        loop = make_loop(backend, primary=lambda s: offer(data, s))
        loop._trace = CausalTrace(sink, 'hierarchical', provenance=loop.provenance)
        for _ in range(5):
            loop.step()
            backend.advance()
        backend.advance(produce=True)
        loop._observe()
        assert loop.memory.capital_investment is None
    result = verify_run(directory)
    assert result['complete']
    rows = [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines()]
    assert any(row['event_type'] == 'capital_committed' for row in rows)
    assert any(row['event_type'] == 'capital_completed' for row in rows)


@pytest.mark.parametrize('shortcut', ['boiler', 'urgency'])
def test_exhausted_investment_resume_uses_ordinary_work_before_shortcuts(tmp_path, monkeypatch, shortcut):
    data, state = scenario()
    backend = Backend(data, state)
    path = tmp_path / 'exhausted.json'
    loop = make_loop(backend, path, primary=lambda s: offer(data, s))
    key = offer(data, state).materials[capital.MARKER]['spec']['key']
    loop.memory.failures[key] = 2
    loop._save()
    if shortcut == 'boiler':
        state.factory['entities']['utility:boiler']['fuel']['coal'] = 0
    else:
        original = capital_controller.candidate_evidence
        def urgent(snapshot, catalog, plans):
            evidence = original(snapshot, catalog, plans)
            for row in evidence.values():
                row['urgency'] = 2
            return evidence
        monkeypatch.setattr(capital_controller, 'candidate_evidence', urgent)
    loop = make_loop(backend, path, resume=True, primary=lambda s: offer(data, s))
    loop._observe()  # Resume restores the durable memory on first observation.
    plans, _ = loop._work_candidates(state)
    assert plans and all(capital.MARKER not in (p.materials or {}) for p in plans)
    result = loop.step()
    assert backend.calls and loop.memory.status == 'running'
    if shortcut == 'boiler':
        assert result['verified'] and loop.memory.pending is None
    else:
        assert loop.memory.pending['action'] == 'factory_wait'
    assert loop.memory.capital_investment is None
    assert loop.memory.failures[key] == 2
    assert not any(e['kind'] == 'capital_committed' for e in loop.memory.history)
    assert all(not costs for costs in loop.memory.reservations.values())


def test_stale_offer_for_existing_producer_keeps_ordinary_work():
    data, state = scenario()
    stale = offer(data, state)
    state.factory['entities'][ROLE] = machine(MACHINE, unit_number=80, recipe=ITEM,
        products_finished=0, energy=100, electric_network_id=1)
    loop = make_loop(Backend(data, state), primary=lambda s: stale)
    plans, _ = loop._work_candidates(state)
    assert plans and all(capital.MARKER not in (p.materials or {}) for p in plans)
    assert loop.memory.capital_investment is None
    with pytest.raises(ValueError, match='Cannot start capital commitment'):
        capital_controller.commit(loop, stale, state)


def research_catalog():
    data, state = scenario()
    state.researched = list(state.researched or []) + ['unlock-test']
    data.technologies['unlock-test'] = {'effects': [{'type': 'unlock-recipe', 'recipe': MACHINE}]}
    data.recipes[MACHINE]['enabled'] = False
    spec = deepcopy(offer(data, state).materials[capital.MARKER]['spec'])
    return data, state, spec


def legacy_spec(data, spec):
    value = deepcopy(spec)
    value['schema'] = 1
    value['catalog_sha256'] = capital.catalog_digest(data, value['recipe'], value['machine'], schema=1)
    value['key'] = capital.key_for(value)
    return value


def test_research_unlock_refresh_preserves_structural_spec_and_capabilities():
    data, state, spec = research_catalog()
    assert spec['schema'] == 2
    before = deepcopy(spec)
    data.recipes[MACHINE]['enabled'] = True
    capital.validate_spec(spec, data, state.researched)
    assert spec == before
    data.recipes[MACHINE]['enabled'] = False
    with pytest.raises(ValueError, match='capability changed'):
        capital.validate_spec(spec, data, [])


def test_legacy_research_refresh_keeps_exact_checkpoint_identity():
    data, state, spec = research_catalog()
    saved = legacy_spec(data, spec)
    before = deepcopy(saved)
    data.recipes[MACHINE]['enabled'] = True
    graph_before = deepcopy(data.recipes)
    capital.validate_spec(saved, data, state.researched)
    assert saved == before and data.recipes == graph_before
    with pytest.raises(ValueError, match='capability changed'):
        capital.validate_spec(saved, data, [])  # No observed research authorizes reconstruction.


@pytest.mark.parametrize('schema', [1, 2])
@pytest.mark.parametrize('change', ['ingredients', 'energy', 'hidden', 'machine_speed', 'hand_categories', 'version'])
def test_research_compatibility_never_ignores_structural_change(schema, change):
    data, state, spec = research_catalog()
    if schema == 1:
        spec = legacy_spec(data, spec)
    data.recipes[MACHINE]['enabled'] = True
    if change == 'ingredients':
        data.recipes[MACHINE]['ingredients'][0]['amount'] += 1
    elif change == 'energy':
        data.recipes[MACHINE]['energy'] += 1
    elif change == 'hidden':
        data.recipes[MACHINE]['hidden'] = True
    elif change == 'machine_speed':
        data.machines[MACHINE]['speed'] += 1
    elif change == 'hand_categories':
        data.hand_categories['crafting'] = False
    else:
        data = replace(data, version='2.0.99')
    with pytest.raises(ValueError, match='capability changed'):
        capital.validate_spec(spec, data, state.researched)


def test_legacy_reconstruction_budget_fails_closed_without_mutation():
    data, state, spec = research_catalog()
    for index in range(8):
        name = f'dependency-{index}'
        data.recipes[name] = recipe(name, {})
        data.recipes[name]['enabled'] = True
        data.recipes[MACHINE]['ingredients'].append({'name': name, 'amount': 1, 'type': 'item'})
        data.technologies['unlock-test']['effects'].append({'type': 'unlock-recipe', 'recipe': name})
    saved = legacy_spec(data, spec)
    capital.validate_spec(saved, data, state.researched)  # Exact legacy hashes remain valid.
    data.recipes[MACHINE]['enabled'] = True
    before = deepcopy(data.recipes)
    with pytest.raises(ValueError, match='capability changed'):
        capital.validate_spec(saved, data, state.researched)
    assert data.recipes == before


def test_legacy_expiry_blocks_semantic_reinvestment_without_rewriting_budget():
    data, state, spec = research_catalog()
    old = legacy_spec(data, spec)
    plan = offer(data, state)
    plan.materials[capital.MARKER]['spec'] = old
    plan = replace(plan, id=old['key'] + ':kit:test')
    loop = make_loop(Backend(data, state))
    capital_controller.commit(loop, plan, state)
    original = deepcopy(loop.memory.capital_investment)
    data.recipes[MACHINE]['enabled'] = True
    capital.validate_spec(old, data, state.researched)
    state.tick = original['deadline_tick']
    loop.memory.last_tick = state.tick
    capital_controller.frontier(loop, state)
    assert loop.memory.capital_investment is None
    failures = deepcopy(loop.memory.failures)
    new = deepcopy(spec)
    assert new['key'] != old['key'] and failures[old['key']] == 2
    assert not capital_controller._available(loop, new, state)
    assert loop.memory.failures == failures
    assert old == original['spec']


def test_semantic_failure_blocks_legacy_alias_too():
    data, state, spec = research_catalog()
    old = legacy_spec(data, spec)
    loop = make_loop(Backend(data, state))
    loop.memory.failures[spec['key']] = 2
    data.recipes[MACHINE]['enabled'] = True
    assert not capital_controller._available(loop, old, state)
