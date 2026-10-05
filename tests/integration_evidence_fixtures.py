"""Fabricated API-shaped evidence, never a native or provider execution claim."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from jev_factorio import solid_routes as routes
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.integration_evidence import FLAGS, TRIAL_SCHEMA
from jev_factorio.iteration_timing import CLOCKS, IO_KEYS, Ledger
from jev_factorio.solid_controller import solid_loop_type
from solid_routes_fixtures import fixture, full, row


def timing(index):
    values = [0, 0, 0]
    clocks = [lambda i=i: values[i] for i in range(3)]
    ledger = Ledger(clock=clocks[0], cpu_clock=clocks[1], thread_clock=clocks[2])
    with ledger.span('iteration'):
        for name in ('observe', 'planning', 'record_construct'):
            with ledger.span(name):
                values[:] = [a + b for a, b in zip(values, (1000000, 100000, 100000))]
        values[:] = [a + b for a, b in zip(values, (1000000, 100000, 100000))]
    value = ledger.snapshot(index, 'returned')
    value['native_io'] = {k: 0 for k in IO_KEYS}
    value['native_io'].update(command_calls=3, request_bytes=120, response_bytes=300)
    value['gap'] = {'complete': True, 'total_ns': dict.fromkeys(CLOCKS, 2000000),
                    'intentional_sleep_ns': dict.fromkeys(CLOCKS, 1000000),
                    'other_gap_ns': dict.fromkeys(CLOCKS, 1000000),
                    'sleep_calls': 1, 'sleep_failed': 0, 'requested_sleep_ns': 1000000,
                    'scope': 'previous_decorated_step_end_to_current_decorated_step_start'}
    return value


def evidence():
    """Three nonintersecting paid corridors plus a single lab over 31 minutes.

    Deliberately does not manufacture coal-source mining evidence. Everything,
    including model-call and receipt fields, is fabricated solely for parser tests.
    """
    base = fixture(); full(base)
    snapshot = deepcopy(base)
    snapshot.world_kind = 'fle'  # API shape, NOT an actual native run
    snapshot.session_id = 'private-fixture-session'
    snapshot.factory['entities'] = {}
    snapshot.factory['solid_routes']['session_id'] = snapshot.session_id
    snapshot.factory['solid_routes']['routes'] = {}
    snapshot.factory['solid_routes']['diagnostics'] = []
    intents = []
    for index in range(3):
        value = deepcopy(row(base))
        offset, y = index * 10000, index * 20
        coal = index < 2
        source = f'fixture:coal-source:{index}' if coal else value['source']['role']
        target = f'fixture:fuel:{index}' if coal else value['target']['role']
        item = 'coal' if coal else value['item']
        value['item'] = item
        value['route'] = f"solid:{5001 + offset}:{5002 + offset}:{item}:{'fuel' if coal else 'input'}"
        value['layout'] = f'solid-layout:fixture:{index}'
        for endpoint, role in ((value['source'], source), (value['target'], target)):
            endpoint['role'] = role
            endpoint['unit_number'] += offset
            endpoint['position']['y'] += y
            for corner in endpoint['bounds'].values(): corner['y'] += y
        if coal:
            value['source'].update(name='steel-chest', inventory='chest', recipe='')
            value['target'].update(name='stone-furnace', inventory='fuel', recipe='')
        for endpoint in (value['source'], value['target']):
            snapshot.factory['entities'][endpoint['role']] = {
                'name': endpoint['name'], 'unit_number': endpoint['unit_number'],
                'position': deepcopy(endpoint['position']), 'recipe': endpoint['recipe'],
                'input': {}, 'output': {}, 'fuel': {'coal': 10}, 'energy': 100,
                'products_finished': 0, 'crafting': True,
            }
        for step in value['steps']:
            step['position']['y'] += y
            paid = value['parts'][step['part']]
            paid.update(role=f"{value['route']}:{step['part']}", receipt=f"paid:{index}:{step['part']}",
                        unit_number=paid['unit_number'] + offset)
            snapshot.factory['entities'][paid['role']] = {
                'name': step['name'], 'unit_number': paid['unit_number'],
                'position': deepcopy(step['position']), 'energy': 100,
            }
        snapshot.factory['solid_routes']['routes'][value['route']] = value
        snapshot.factory['solid_routes']['diagnostics'].append({
            'intent_index': index + 1, 'state': 'committed', 'reason': 'paid_or_pending_route'})
        intents.append({'source': source, 'target': target, 'item': item, 'destination': value['target']['inventory']})
    snapshot.factory['entities']['utility:lab'] = {'name': 'lab', 'unit_number': 999999,
                                                  'position': {'x': 1, 'y': 100}, 'energy': 100}
    snapshot.factory['force_entity_counts'] = {'lab': 1}
    snapshot.factory['acceptance_runtime'] = {
        'schema': 1, 'session_id': snapshot.session_id, 'actor_unit': 999998, 'player_index': 1,
        'surface_index': 1, 'force_index': 1, 'mods': {'base': '2.0-fixture'},
        'speed': 1, 'tick_paused': False,
    }
    config = {k: False for k in FLAGS}
    config.update(solid_routes=True, factory_scheduling='ready-work')
    trial = dict(schema=TRIAL_SCHEMA, evidence_kind='fixture', arm='treatment', comparison_axis='algorithm',
                 experiment_sha256='a' * 64, workload_sha256='b' * 64, initial_save_sha256='c' * 64,
                 capacity_profile_sha256='d' * 64, expected_commit='e' * 40, expected_source_sha256='f' * 64,
                 configuration=config, solid_intents=intents, campaign_treatment=None,
                 requested_model='fixture-model', resolved_model='fixture-model', declared_at_utc='2026-09-26T19:00:00Z',
                 original_cutoff_utc='2026-09-26T23:00:00Z', runtime_cutoff_utc='2026-09-26T23:00:00Z',
                 minimum_window_seconds=1800, max_observation_gap_seconds=120,
                 max_no_science_progress_seconds=120, science_packs=['automation-science-pack'],
                 research_goal='fluid-handling', downstream_recipes=['automation-science-pack'],
                 minimum_timing_samples=30, regression_limits={'max_iteration_p95_ratio': 1.1, 'min_science_rate_ratio': .9})
    rows = []
    first_tick = 1000
    start = datetime(2026, 9, 26, 20, tzinfo=timezone.utc)
    for i in range(32):
        snapshot.tick = first_tick + 3600 * i
        snapshot.factory['tick'] = snapshot.tick
        snapshot.factory['solid_routes']['tick'] = snapshot.tick
        snapshot.researched = ['automation'] + (['fluid-handling'] if i == 31 else [])
        snapshot.factory.update(research='fluid-handling', research_progress=i / 31,
                                consumed={'automation-science-pack': i * 2})
        snapshot.factory['receipts'] = {f'science:{j}': {'role': 'utility:lab', 'unit_number': 999999,
            'item': 'automation-science-pack', 'extracting': False, 'quantity': 2,
            'tick': first_tick + 3600 * j} for j in range(i + 1)}
        for value in snapshot.factory['solid_routes']['routes'].values():
            value['flow'] = dict(layout=value['layout'], source_unit=value['source']['unit_number'],
                target_unit=value['target']['unit_number'], method='exclusive_fuel_lower_bound'
                if value['target']['inventory'] == 'fuel' else 'stoichiometric_balance',
                first_tick=first_tick - 180, last_tick=snapshot.tick, last_positive_tick=snapshot.tick,
                positive_samples=3 + i, sent=10 + i * 2, received=10 + i * 2, unattributed_loss=0)
            snapshot.factory['entities'][value['target']['role']]['products_finished'] = i * 2
        state = snapshot.for_jev()
        rows.append(dict(schema_version=2, controller='hierarchical', session_id=snapshot.session_id,
            world_kind='fle', process_id='private-fixture-process', execution_id='private-fixture-execution',
            run_id='private-fixture-run', segment_id='private-fixture-segment', policy='hybrid', target='rocket_launch', requested_model='fixture-model',
            resolved_model='fixture-model' if i == 1 else None, model_call=i == 1,
            code_revision={'commit': trial['expected_commit'], 'source_sha256': trial['expected_source_sha256']},
            acceptance_configuration=deepcopy(config), factory_scheduling='ready-work',
            recorded_at_utc=(start + timedelta(minutes=i)).isoformat(),
            tick=state['tick'], state=deepcopy(rows[-1]['after_state']) if rows else deepcopy(state),
            after_state=deepcopy(state), status='running', completed_goals={},
            decision={'model_called': i == 1} if i == 1 else None,
            pending=None, attempt=None, phases=[],
            action='observe', verified=True, solid_route_fault=False, failure_budgets={},
            mining_outposts=False,
            solid_routes=True,
            solid_route_evidence=deepcopy(state['factory']['solid_routes']),
            solid_science_policy=False,
            solid_funding_schema=1,
            solid_funding=None,
            solid_investment_evidence={},
            **({'previous_iteration_timing': timing(i)} if i else {})))
        # The preceding after-state can share the tick with this before-state.
        # Its flow evidence stays bound to its own snapshot, not this later tick.
    Memory = solid_loop_type(HierarchicalLoop).memory_type
    def checkpoint(state):
        memory = Memory(snapshot.session_id, 'rocket_launch', last_tick=state['tick'],
                        solid_intents=deepcopy(intents), solid_epoch={'actor_index': 1, 'surface_index': 1, 'force_index': 1},
                        solid_commitments={k: routes.commitment(v) for k, v in state['factory']['solid_routes']['routes'].items()})
        return asdict(memory)
    initial, final = checkpoint(rows[0]['state']), checkpoint(rows[-1]['after_state'])
    return rows, trial, initial, final
