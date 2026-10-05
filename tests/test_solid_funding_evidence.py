"""Synthetic funding/acceptance composition; never Factorio or provider evidence."""
from copy import deepcopy
from dataclasses import asdict
from functools import lru_cache

import pytest

from integration_evidence_fixtures import evidence
from solid_routes_fixtures import fixture, row as route_row, SOURCE, TARGET
from jev_factorio.integration_evidence import analyze_rows
from jev_factorio.judgments import Decision
from jev_factorio.skills import Step
from jev_factorio.state import GameSnapshot
from jev_factorio.planning import solid_funding
from test_solid_kit_acquisition import kit_loop


def funding_catalog():
    from test_solid_investment import catalog
    value = catalog()
    value.technologies['fluid-handling'] = deepcopy(value.technologies['study'])
    return value


def funded_evidence():
    rows, trial, initial, final = evidence()
    def shift(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'tick', 'first_tick', 'last_tick', 'last_positive_tick'} and type(item) is int:
                    value[key] += 30000
                else: shift(item)
        elif isinstance(value, list):
            for item in value: shift(item)
    for value in (rows, initial, final): shift(value)
    trial['configuration']['solid_science_policy'] = True
    for cp in (initial, final):
        cp['solid_science_policy'] = True
    base = fixture()
    route = deepcopy(route_row(base))
    route.update(route='solid:105001:105002:iron-gear-wheel:input', layout='kit-layout:1')
    entities = {}
    for name, role, unit in (('source', 'fixture:kit-source', 105001),
                             ('target', 'fixture:kit-target', 105002)):
        endpoint = route[name]
        entity = deepcopy(base.factory['entities'][endpoint['role']])
        endpoint.update(role=role, unit_number=unit)
        endpoint['position']['y'] += 60
        for corner in endpoint['bounds'].values():
            corner['y'] += 60
        entity.update(unit_number=unit, position=deepcopy(endpoint['position']), products_finished=0)
        entities[role] = entity
    entities['fixture:kit-source']['output']['iron-gear-wheel'] = 120
    entities['fixture:kit-target']['input']['copper-plate'] = 120
    for step in route['steps']:
        step['position']['y'] += 60
    intent = solid_funding.intent(route)
    trial['solid_intents'].append(deepcopy(intent))
    data = funding_catalog()
    funding = solid_funding.start(route, {'catalog_sha256': solid_funding.catalog_digest(route, base, data)}, initial['last_tick'])
    from test_solid_investment import service_history
    from jev_factorio.telemetry import fingerprint
    outcomes = service_history(base)
    for value in outcomes:
        endpoint = route['source'] if value['action'] == 'factory_extract' else route['target']
        parameters = {'role': endpoint['role'], 'item': route['item'], 'quantity': 20,
                      'receipt': f"{value['started_tick']}:{value['action']}:{endpoint['role']}:{route['item']}"}
        value.update(plan_id=f"factory:{value['action']}:{endpoint['role']}", receipt=parameters['receipt'],
                     expected_unit_number=endpoint['unit_number'],
                     step_sha256=fingerprint(asdict(Step(value['action'], 'transfer', parameters=parameters))))
    for cp in (initial, final):
        cp['solid_intents'].append(deepcopy(intent))
        cp['solid_funding'] = deepcopy(funding)
        cp['attempt_outcomes'] = deepcopy(outcomes)
        cp['solid_funding_catalogs'] = {funding['key']: {'schema': 1, 'observed_tick': initial['last_tick'],
            'version': data.version, 'catalog_sha256': funding['catalog_sha256']}}
    for record in rows:
        record['acceptance_configuration']['solid_science_policy'] = True
        # Match SolidRouteMixin._record_extras for this enabled treatment. A
        # checkpoint and acceptance label alone are not the emitted producer
        # record that the analyzer now validates.
        record['solid_science_policy'] = True
        record.update(solid_funding_schema=1, solid_funding=deepcopy(funding), history=[])
        record['attempt_outcomes'] = deepcopy(outcomes)
        for label in ('state', 'after_state'):
            record[label]['inventory'].update({'iron-plate': 200, 'copper-plate': 100, 'transport-belt': 0})
            factory = record[label]['factory']
            factory['acceptance_runtime']['mods']['base'] = data.version
            factory['entities'].update(deepcopy(entities))
            factory['solid_routes']['routes'][route['route']] = deepcopy(route)
            factory['solid_routes']['diagnostics'].append(
                {'intent_index': 4, 'state': 'proposed', 'reason': 'ready_layout'})
    declaration = solid_funding.acquisition_evidence(route, GameSnapshot(**rows[0]['state']), data, {})
    for cp in (initial, final):
        cp['solid_funding_catalogs'][funding['key']]['catalog_sha256'] = solid_funding.digest(declaration['catalog'])
        cp['solid_funding_catalogs'][funding['key']]['acquisition_sha256'] = solid_funding.digest(
            {k: v for k, v in declaration.items() if k != 'reserved'})
    return rows, trial, initial, final


@lru_cache(maxsize=1)
def acquisition_fixture():
    records, _, initial, _ = funded_evidence()
    snapshot = GameSnapshot(**records[0]['state'])
    row = snapshot.factory['solid_routes']['routes'][initial['solid_funding']['route']]
    data = funding_catalog()
    plan, _ = solid_funding.acquire(row, snapshot, data)
    return asdict(plan.steps[0]), solid_funding.acquisition_evidence(row, snapshot, data, {})


def event(kind, funding, tick, **extras):
    if kind == 'solid_kit_committed':
        step, acquisition = acquisition_fixture()
        extras.setdefault('step', deepcopy(step))
        extras.setdefault('acquisition', deepcopy(acquisition))
    return {'kind': kind, 'key': funding['key'] + ('' if kind == 'solid_kit_paid_handoff' else ':kit'),
            'tick': tick, 'funding': deepcopy(funding), **extras}


def plan_event(funding, tick):
    return {'kind': 'plan_committed', 'plan': funding['key'] + ':kit',
            'source': 'jev', 'tick': tick}


def decision_for(record, funding):
    record['decision'] = asdict(Decision(funding['key'] + ':kit', 'jev', model_called=True))
    record.update(model_call=True, resolved_model=record['requested_model'])
    if record['action'] == 'observe': record['verified'] = False


def close_funding(data, index=3, kind='solid_kit_abandoned'):
    rows, _, initial, final = data
    funding = initial['solid_funding']
    # An initial lock may have a shorter retained horizon. Make the synthetic
    # deadline release consistent with its recorded observation.
    deadline = min(funding['deadline_tick'], rows[index]['after_state']['tick'])
    funding['deadline_tick'] = deadline
    for record in rows:
        if record['solid_funding'] is not None:
            record['solid_funding']['deadline_tick'] = deadline
    rows[index]['history'] = [event(kind, funding, rows[index]['after_state']['tick'], reason='kit_deadline')]
    for record in rows[index:]:
        record['solid_funding'] = None
        record['history'] = deepcopy(rows[index]['history'])
        if kind == 'solid_kit_abandoned':
            record['failure_budgets'][funding['key'] + ':kit'] = 2
    final['solid_funding'] = None
    final['failures'] = deepcopy(rows[-1]['failure_budgets'])
    final['history'] = deepcopy(rows[-1]['history'])


def assert_rejected(data):
    result = analyze_rows(*data)
    assert not result['integrity_checks_passed'], result['issues']
    assert not result['measurement_checks_passed']
    assert any('solid_funding' in issue for issue in result['issues']), result['issues']
    assert result['native_acceptance'] == 'not_accepted'
    assert result['deployment_authorized'] is False


def test_held_funding_is_measurable_without_claiming_acceptance():
    result = analyze_rows(*funded_evidence())
    assert result['measurement_checks_passed'], result['issues']
    assert result['native_acceptance'] == 'not_accepted'
    assert result['deployment_authorized'] is False


def test_valid_funding_abandonment_retains_budget_and_measurement():
    data = funded_evidence(); close_funding(data)
    result = analyze_rows(*data)
    assert result['measurement_checks_passed'], result['issues']


def test_initial_funding_paid_handoff_has_exact_observed_ownership():
    rows, trial, initial, final = evidence()
    trial['configuration']['solid_science_policy'] = True
    for cp in (initial, final): cp['solid_science_policy'] = True
    route = next(r for r in rows[0]['after_state']['factory']['solid_routes']['routes'].values()
                 if r['item'] != 'coal')
    funding = solid_funding.start(route, {'catalog_sha256': '1' * 64}, initial['last_tick'])
    initial['solid_funding'] = deepcopy(funding)
    for record in rows:
        record['acceptance_configuration']['solid_science_policy'] = True
        record['solid_science_policy'] = True
        record.update(solid_funding_schema=1, solid_funding=None, history=[])
    rows[0]['history'] = [event('solid_kit_paid_handoff', funding, initial['last_tick'])]
    for record in rows[1:]: record['history'] = deepcopy(rows[0]['history'])
    final['history'] = deepcopy(rows[-1]['history'])
    result = analyze_rows(rows, trial, initial, final)
    assert result['measurement_checks_passed'], result['issues']


def test_transient_funding_can_be_committed_then_abandoned_in_one_record():
    data = funded_evidence(); rows, _, initial, final = data
    funding = deepcopy(initial['solid_funding'])
    funding['started_tick'] = rows[3]['state']['tick']
    funding['deadline_tick'] = funding['started_tick'] + solid_funding.MAX_TICKS
    initial['solid_funding'] = final['solid_funding'] = None
    for record in rows: record['solid_funding'] = None
    rows[3]['history'] = [event('solid_kit_committed', funding, funding['started_tick']),
                          plan_event(funding, funding['started_tick']),
                          {'kind': 'plan_failed', 'plan': funding['key'] + ':kit',
                           'reason': 'Plan precondition changed', 'tick': rows[3]['after_state']['tick']},
                          event('solid_kit_abandoned', funding, rows[3]['after_state']['tick'], reason='kit_failure_budget')]
    rows[3]['verified'] = False
    decision_for(rows[3], funding)
    rows[3]['after_state']['factory']['solid_routes']['routes'].pop(funding['route'])
    for record in rows[3:]:
        record['failure_budgets'][funding['key'] + ':kit'] = 3
        record['history'] = deepcopy(rows[3]['history'])
    final['failures'] = deepcopy(rows[-1]['failure_budgets'])
    final['history'] = deepcopy(rows[-1]['history'])
    result = analyze_rows(*data)
    assert result['measurement_checks_passed'], result['issues']


def test_missing_funding_history_with_legacy_disabled_policy_stays_compatible():
    data = evidence()
    for cp in data[2:]: cp.pop('solid_funding', None)
    result = analyze_rows(*data)
    assert result['measurement_checks_passed'], result['issues']


@pytest.mark.parametrize('mutation', [
    'erase_initial', 'erase_final', 'invent_final', 'missing_record', 'missing_schema',
    'schema_bool', 'schema_string', 'schema_future', 'record_bool', 'record_list',
    'record_empty', 'action_bool', 'action_regression', 'layout_changed', 'catalog_changed',
    'unit_changed', 'started_changed', 'deadline_changed', 'native_route_missing',
    'unlogged_commit', 'disabled_policy',
])
def test_funding_history_corruption_is_not_a_passing_measurement(mutation):
    data = funded_evidence(); rows, trial, initial, final = data
    if mutation == 'erase_initial':
        for record in rows: record['solid_funding'] = None
        final['solid_funding'] = None
    elif mutation == 'erase_final': final['solid_funding'] = None
    elif mutation == 'invent_final':
        initial['solid_funding'] = None
        for record in rows: record['solid_funding'] = None
    elif mutation == 'missing_record': rows[3].pop('solid_funding')
    elif mutation == 'missing_schema': rows[3].pop('solid_funding_schema')
    elif mutation.startswith('schema_'):
        rows[3]['solid_funding_schema'] = {'schema_bool': True, 'schema_string': '1', 'schema_future': 2}[mutation]
    elif mutation.startswith('record_'):
        rows[3]['solid_funding'] = {'record_bool': True, 'record_list': [], 'record_empty': {}}[mutation]
    elif mutation == 'action_bool': rows[3]['solid_funding']['actions'] = True
    elif mutation == 'action_regression': rows[2]['solid_funding']['actions'] = 2
    elif mutation == 'layout_changed': rows[3]['solid_funding']['layout'] = 'changed-layout'
    elif mutation == 'catalog_changed': rows[3]['solid_funding']['catalog_sha256'] = '2' * 64
    elif mutation == 'unit_changed': rows[3]['solid_funding']['source_unit'] += 1
    elif mutation == 'started_changed': rows[3]['solid_funding']['started_tick'] -= 1
    elif mutation == 'deadline_changed': rows[3]['solid_funding']['deadline_tick'] -= 1
    elif mutation == 'native_route_missing':
        for record in rows:
            for label in ('state', 'after_state'):
                record[label]['factory']['solid_routes']['routes'].pop(initial['solid_funding']['route'])
    elif mutation == 'unlogged_commit':
        initial['solid_funding'] = None
        for record in rows[:3]: record['solid_funding'] = None
    elif mutation == 'disabled_policy':
        initial['solid_funding'] = final['solid_funding'] = None
        initial['solid_science_policy'] = final['solid_science_policy'] = False
        trial['configuration']['solid_science_policy'] = False
        for record in rows:
            record['acceptance_configuration']['solid_science_policy'] = False
            record['solid_science_policy'] = False
    assert_rejected(data)


@pytest.mark.parametrize('mutation', [
    'missing_event', 'wrong_key', 'old_event', 'future_event', 'missing_proof',
    'different_proof', 'missing_budget', 'insufficient_budget', 'handoff_without_payment',
    'invented_kind', 'missing_reason',
])
def test_funding_release_requires_current_matching_reconciliation(mutation):
    data = funded_evidence(); close_funding(data)
    rows, _, initial, final = data
    proof = rows[3]['history'][0]
    if mutation == 'missing_event': rows[3]['history'] = []
    elif mutation == 'wrong_key': proof['key'] = 'different-key'
    elif mutation == 'old_event': proof['tick'] = initial['last_tick'] - 1
    elif mutation == 'future_event': proof['tick'] = rows[3]['after_state']['tick'] + 1
    elif mutation == 'missing_proof': proof.pop('funding')
    elif mutation == 'different_proof': proof['funding']['catalog_sha256'] = '2' * 64
    elif mutation in {'missing_budget', 'insufficient_budget'}:
        for record in rows[3:]: record['failure_budgets'][initial['solid_funding']['key'] + ':kit'] = 0 if mutation == 'missing_budget' else 1
        final['failures'] = deepcopy(rows[-1]['failure_budgets'])
    elif mutation == 'handoff_without_payment':
        proof['kind'] = 'solid_kit_paid_handoff'; proof['key'] = initial['solid_funding']['key']
    elif mutation == 'invented_kind': proof['kind'] = 'solid_kit_cancelled'
    elif mutation == 'missing_reason': proof.pop('reason')
    assert_rejected(data)


def test_real_kit_record_retains_detached_funding_and_transition_proof(tmp_path):
    loop, backend = kit_loop(tmp_path)
    record = loop.step()
    assert record['solid_funding_schema'] == 1
    assert record['solid_funding'] == loop.memory.solid_funding
    committed = next(e for e in record['history'] if e['kind'] == 'solid_kit_committed')
    assert committed['funding'] == loop.memory.solid_funding
    record['solid_funding']['actions'] += 10
    committed['funding']['actions'] += 10
    assert loop.memory.solid_funding['actions'] == 1
    assert next(e for e in loop.memory.history if e['kind'] == 'solid_kit_committed')['funding']['actions'] == 1


def test_real_acquisition_and_paid_handoff_reconcile_every_record(tmp_path):
    from jev_factorio.solid_funding_evidence import funding_history_issues
    from solid_routes_fixtures import row
    loop, backend = kit_loop(tmp_path)
    loop._observe()
    initial = asdict(loop.memory)
    records = []
    for _ in range(20):
        records.append(loop.step())
        assert not funding_history_issues(initial, records, asdict(loop.memory))
        if row(backend.state)['state'] == 'ready':
            break
    assert row(backend.state)['state'] == 'ready'
    assert loop.memory.solid_funding is None
    proofs = [e['funding']['actions'] for e in loop.memory.history if e['kind'] == 'solid_kit_committed']
    assert proofs == list(range(1, len(proofs) + 1)) and len(proofs) > 1


@pytest.mark.parametrize('change', ['deadline', 'catalog', 'demand'])
def test_real_fresh_guard_abandonment_in_one_record_reconciles(change, tmp_path):
    from jev_factorio.solid_funding_evidence import funding_history_issues
    loop, backend = kit_loop(tmp_path)
    loop._observe()
    initial = asdict(loop.memory)
    def update():
        if backend.observations == 3:
            if change == 'deadline':
                backend.state.tick += 216001
                backend.state.factory['solid_routes']['tick'] = backend.state.tick
            elif change == 'catalog':
                loop.catalog.recipes['inserter']['ingredients'][0]['amount'] += 1
            else:
                backend.state.factory['research_progress'] = 1
    backend.before_observe = update
    result = loop.step()
    assert not result['verified'] and not backend.calls
    issues = funding_history_issues(initial, [result], asdict(loop.memory))
    if change == 'deadline':
        assert not issues
    else:
        assert issues == ['solid_funding_abandonment_trigger_unproven']
    assert loop.memory.solid_funding is None


def test_commit_increment_needs_an_exact_new_proof_and_retains_identity():
    data = funded_evidence(); rows, _, initial, final = data
    next_state = deepcopy(initial['solid_funding']); next_state['actions'] = 2
    rows[3]['history'] = [event('solid_kit_committed', next_state, rows[3]['state']['tick']),
                          plan_event(next_state, rows[3]['state']['tick'])]
    rows[3].update(action='verify', verified=True)
    selected = rows[3]['history'][0]['step']
    rows[3]['after_state']['inventory'][selected['item']] = selected['threshold']
    decision_for(rows[3], next_state)
    for record in rows[3:]:
        record['solid_funding'] = deepcopy(next_state)
        record['history'] = deepcopy(rows[3]['history'])
    final['solid_funding'] = deepcopy(next_state)
    final['history'] = deepcopy(rows[-1]['history'])
    result = analyze_rows(*data)
    assert result['measurement_checks_passed'], result['issues']
    rows[3]['history'] = []
    assert_rejected(data)


@pytest.mark.parametrize('mutation', ['reset_actions', 'skip_actions', 'repeat_old_proof',
                                     'rewrite_old_proof', 'exhausted_budget'])
def test_new_commit_cannot_reset_or_replay_funding(mutation):
    data = funded_evidence(); rows, _, initial, final = data
    funding = deepcopy(initial['solid_funding'])
    funding['actions'] = 2
    if mutation == 'reset_actions': funding['actions'] = 1
    if mutation == 'skip_actions': funding['actions'] = 3
    if mutation == 'rewrite_old_proof': funding['deadline_tick'] -= 1
    proof = event('solid_kit_committed', funding, rows[3]['state']['tick'])
    rows[3]['history'] = [proof, plan_event(funding, rows[3]['state']['tick'])]
    if mutation == 'repeat_old_proof': initial['history'].append(deepcopy(proof))
    if mutation == 'exhausted_budget':
        initial['failures'][funding['key'] + ':kit'] = 2
        for record in rows: record['failure_budgets'][funding['key'] + ':kit'] = 2
        final['failures'] = deepcopy(rows[-1]['failure_budgets'])
    for record in rows[3:]: record['solid_funding'] = deepcopy(funding)
    final['solid_funding'] = deepcopy(funding)
    assert_rejected(data)


def test_history_ring_repetitions_do_not_repeat_abandonment():
    data = funded_evidence(); close_funding(data)
    rows = data[0]
    for record in rows[4:10]: record['history'] = deepcopy(rows[3]['history'])
    result = analyze_rows(*data)
    assert result['measurement_checks_passed'], result['issues']


def test_checked_evidence_is_not_mutated():
    data = funded_evidence(); close_funding(data)
    original = deepcopy(data)
    analyze_rows(*data)
    assert data == original


@pytest.mark.parametrize('history', [None, {}, 1, 'private-marker', [None] * 65])
def test_funding_history_shape_is_bounded_and_fail_closed(history):
    from jev_factorio.solid_funding_evidence import funding_history_issues
    data = funded_evidence(); data[0][3]['history'] = history
    assert funding_history_issues(data[2], data[0], data[3])
    # The parent analyzer may reject malformed common history before the
    # funding checker is reached. Raising is also fail-closed.
    with pytest.raises(ValueError, match='event history'):
        analyze_rows(*data)


def test_funding_issue_labels_never_echo_private_input():
    data = funded_evidence(); data[0][3]['solid_funding']['layout'] = 'PRIVATE-PATH-AND-SESSION'
    result = analyze_rows(*data)
    assert not result['integrity_checks_passed']
    assert all('PRIVATE-PATH-AND-SESSION' not in value for value in result['issues'])


def test_transition_proof_has_no_unrecognized_fields():
    data = funded_evidence(); close_funding(data)
    data[0][3]['history'][0]['allow_without_budget'] = True
    assert_rejected(data)

def test_direct_checker_rejects_malformed_preceding_budget():
    from jev_factorio.solid_funding_evidence import funding_history_issues
    data = funded_evidence(); rows, _, initial, final = data
    funding = deepcopy(initial['solid_funding']); funding['actions'] = 2
    rows[3]['history'] = [event('solid_kit_committed', funding, rows[3]['state']['tick']),
                          plan_event(funding, rows[3]['state']['tick'])]
    for record in rows[3:]: record['solid_funding'] = deepcopy(funding)
    rows[2]['failure_budgets'][funding['key'] + ':kit'] = True
    final['solid_funding'] = deepcopy(funding)
    assert funding_history_issues(initial, rows, final)


@pytest.mark.parametrize('release_path', ['observe', 'clear_plan'])
def test_exhausted_existing_budget_emits_release_at_actual_clear(release_path, tmp_path):
    from jev_factorio.solid_funding_evidence import funding_history_issues
    loop, backend = kit_loop(tmp_path)
    loop.step()
    funding = deepcopy(loop.memory.solid_funding)
    loop.memory.failures[funding['key'] + ':kit'] = 2
    initial = asdict(loop.memory)
    if release_path == 'clear_plan':
        loop._clear_plan()
    snapshot = loop._observe()
    record = loop._record(snapshot, 'observe', 'Exhausted existing kit budget')
    assert loop.memory.solid_funding is None
    released = [e for e in record['history'] if e['kind'] == 'solid_kit_abandoned']
    assert len(released) == 1 and released[0]['funding'] == funding
    assert not funding_history_issues(initial, [record], asdict(loop.memory))
    assert len(backend.calls) == 1  # Only the first, already verified paid action.


@pytest.mark.parametrize('missing', [('initial',), ('final',), ('initial', 'final')])
def test_enabled_policy_requires_explicit_checkpoint_funding_fields(missing):
    data = funded_evidence()
    rows, _, initial, final = data
    initial['solid_funding'] = final['solid_funding'] = None
    for record in rows:
        record['solid_funding'] = None
    # Explicit null is a valid retained state; omission is unknown evidence.
    assert analyze_rows(*data)['measurement_checks_passed']
    for label in missing:
        {'initial': initial, 'final': final}[label].pop('solid_funding')
    assert_rejected(data)


@pytest.mark.parametrize('retain_recommit', [True, False])
def test_abandonment_budget_cannot_be_reused_by_same_record_recommit(retain_recommit):
    data = funded_evidence()
    rows, _, initial, final = data
    original = deepcopy(initial['solid_funding'])
    tick = rows[3]['state']['tick']
    restarted = deepcopy(original)
    restarted.update(started_tick=tick, deadline_tick=tick + solid_funding.MAX_TICKS, actions=1)
    history = [event('solid_kit_abandoned', original, tick, reason='kit_failure_budget'),
               event('solid_kit_committed', restarted, tick), plan_event(restarted, tick)]
    if not retain_recommit:
        history.append(event('solid_kit_abandoned', restarted, tick, reason='kit_failure_budget'))
    rows[3]['history'] = history
    for record in rows[3:]:
        record['solid_funding'] = deepcopy(restarted) if retain_recommit else None
        record['failure_budgets'][original['key'] + ':kit'] = 2
    final['solid_funding'] = deepcopy(rows[-1]['solid_funding'])
    final['failures'] = deepcopy(rows[-1]['failure_budgets'])
    assert_rejected(data)


@pytest.mark.parametrize('last_action', [3, 8])
def test_one_record_cannot_claim_multiple_new_funding_commits(last_action):
    data = funded_evidence()
    rows, _, initial, final = data
    history = []
    proof = deepcopy(initial['solid_funding'])
    for action in range(2, last_action + 1):
        proof['actions'] = action
        history.append(event('solid_kit_committed', proof, rows[3]['state']['tick']))
        history.append(plan_event(proof, rows[3]['state']['tick']))
    rows[3]['history'] = history
    for record in rows[3:]:
        record['solid_funding'] = deepcopy(proof)
    final['solid_funding'] = deepcopy(proof)
    assert_rejected(data)


def test_repeated_history_commit_does_not_consume_current_record_commit_limit():
    data = funded_evidence()
    rows, _, initial, final = data
    old = event('solid_kit_committed', initial['solid_funding'], initial['last_tick'])
    initial['history'].append(deepcopy(old))
    for record in rows[:3]: record['history'] = [deepcopy(old)]
    new = deepcopy(initial['solid_funding'])
    new['actions'] = 2
    history = [old, event('solid_kit_committed', new, rows[3]['state']['tick']),
               plan_event(new, rows[3]['state']['tick'])]
    rows[3].update(action='verify', verified=True)
    selected = history[1]['step']
    rows[3]['after_state']['inventory'][selected['item']] = selected['threshold']
    decision_for(rows[3], new)
    for record in rows[3:]:
        record['history'] = deepcopy(history)
        record['solid_funding'] = deepcopy(new)
    final['solid_funding'] = deepcopy(new)
    final['history'] = deepcopy(rows[-1]['history'])
    result = analyze_rows(*data)
    assert result['measurement_checks_passed'], result['issues']
