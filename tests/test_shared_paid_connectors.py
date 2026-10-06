"""Replay retained V24 facts; mutations below are explicitly negative controls."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jev_factorio.connector_checkpoint import (
    pending_owned, reconcile, route_paid_coverage, shared_connector_handoff, validate_binding,
)
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.memory import CampaignMemory
from jev_factorio.planning.connection_identity import connection_key
from jev_factorio.skills import Plan
from jev_factorio.state import GameSnapshot

FIXTURE = Path(__file__).parent / 'fixtures/native-v24-shared-poles.json'


def retained():
    data = json.loads(FIXTURE.read_text())
    memory = CampaignMemory(target='rocket_launch', **data['checkpoint'])
    memory.active_goal = memory.active_plan['goal']
    snapshot = GameSnapshot(**data['snapshot'])
    return memory, snapshot, Plan.from_dict(memory.active_plan).steps[0]


class Pages:
    def __init__(self, binding):
        self.routes = deepcopy(binding['routes'])
        self.calls = []

    def command(self, command):
        self.calls.append(command)
        args = command.split('connector_page(', 1)[1].split(')', 1)[0]
        receipt, offset, limit = json.loads('[' + args + ']')
        row = self.routes[receipt]
        cells = [{**cell, 'paid': int(cell['paid'])}
                 for cell in row['cells'][offset - 1:offset - 1 + limit]]
        return json.dumps(dict(id=receipt, valid=True, cell_count=len(row['cells']),
                               offset=offset, cells=cells))


def test_native_shared_poles_have_direct_paid_anchors_without_relabelling():
    memory, snapshot, step = retained()
    before = deepcopy(memory.connector_ownership)
    native = Pages(before)
    reconcile(memory, snapshot, native, resume=True)
    assert memory.connector_ownership == before
    assert shared_connector_handoff(memory)
    assert pending_owned(memory, step) and step.satisfied(snapshot)
    routes = before['routes']
    row = routes[connection_key(step.parameters)]
    assert (row['paid'], row['external'], row['owned']) == (7, 2, False)
    assert sum(r['paid'] for r in routes.values()) == 35
    assert len(native.calls) == 4


@pytest.mark.parametrize('change', ['missing_donor', 'unit', 'position', 'source_unit',
                                  'actor_unit', 'force_index', 'surface_index',
                                  'unpaid_anchor', 'incomplete_donor', 'pipe', 'cycle'])
def test_external_references_require_direct_exact_payment(change):
    memory, _, step = retained()
    routes = memory.connector_ownership['routes']
    row = routes[connection_key(step.parameters)]
    donor_id, donor = next((k, v) for k, v in routes.items() if v['target'] == 'utility:lab')
    if change == 'missing_donor': del routes[donor_id]
    elif change == 'unit': row['cells'][0]['unit_number'] += 999
    elif change == 'position': row['cells'][0]['position']['x'] += 1
    elif change in {'source_unit', 'actor_unit', 'force_index', 'surface_index'}: donor[change] += 1
    elif change in {'unpaid_anchor', 'cycle'}:
        donor['cells'][0].update(paid=False, external=True)
        donor.update(paid=donor['paid'] - 1, external=1, owned=False)
    elif change == 'incomplete_donor': donor.update(state='building', owned=False)
    elif change == 'pipe': row.update(kind='pipe', fluid='water')
    assert not route_paid_coverage(routes, connection_key(step.parameters))


def test_later_route_can_reuse_a_paid_cell_from_an_already_shared_route():
    memory, _, step = retained()
    routes = memory.connector_ownership['routes']
    donor = routes[connection_key(step.parameters)]
    # Synthetic third consumer, explicitly anchored in V24's paid unit 2581.
    row = deepcopy(donor)
    row.update(target='recipe:electronic-circuit', target_unit=9000, paid=0, external=1)
    row['cells'] = [{**deepcopy(donor['cells'][2]), 'index': 1, 'paid': False, 'external': True}]
    row['id'] = connection_key(row)
    routes[row['id']] = row
    validate_binding(memory.connector_ownership, memory.session_id)
    assert route_paid_coverage(routes, row['id'])
    assert not row['owned'] and not donor['owned']


@pytest.mark.parametrize('field,value', [('pending', None), ('attempt', None),
    ('status', 'blocked'), ('reason', 'unrelated'), ('step_index', True),
    ('background_job', {}), ('background_attempt', {}), ('background_step', {}),
    ('transfer_recovery', {}), ('native_pending', {}), ('native_attempt', {})])
def test_handoff_does_not_admit_other_uncertain_or_overlapping_work(field, value):
    memory, _, _ = retained()
    setattr(memory, field, value)
    assert not shared_connector_handoff(memory)


def verifier():
    memory, snapshot, _ = retained()
    loop = HierarchicalLoop(SimpleNamespace(), policy='deterministic')
    loop.memory = memory
    loop._diagnostic_trace = None
    return loop, snapshot


def test_ordinary_verifier_finishes_original_attempt_without_dispatch_or_model():
    loop, snapshot = verifier()
    binding = deepcopy(loop.memory.connector_ownership)
    attempt = deepcopy(loop.memory.attempt)
    result = loop._verify_pending(snapshot)
    assert result['verified'] is True
    assert loop.memory.pending is None and loop.memory.attempt is None
    assert loop.memory.connector_ownership == binding
    outcome = loop.memory.attempt_outcomes[-1]
    assert outcome['id'] == attempt['id'] and outcome['outcome'] == 'verified'
    assert not loop.memory.reservations


@pytest.mark.parametrize('change', ['network', 'capital_fault', 'active', 'dispatch', 'attempt'])
def test_verifier_retains_uncertainty_without_native_completion(change):
    loop, snapshot = verifier()
    if change == 'network': snapshot.factory['entities']['recipe:copper-cable']['electric_network_id'] = 999
    elif change == 'capital_fault': loop._capital_fault = True
    elif change == 'active': snapshot.factory['connector_ownership']['active'] = 'f' * 64
    elif change == 'dispatch': loop.memory.pending['dispatch'] = 'ambiguous'
    elif change == 'attempt': loop.memory.attempt['step_sha256'] = 'f' * 64
    before = deepcopy(loop.memory.pending)
    loop._verify_pending(snapshot)
    assert loop.memory.status == 'uncertain' and loop.memory.pending == before


def test_native_page_change_rejected_even_with_matching_paid_summary():
    memory, snapshot, _ = retained()
    native = Pages(memory.connector_ownership)
    donor = next(v for v in native.routes.values() if v['target'] == 'utility:lab')
    donor['cells'][0]['unit_number'] += 999
    with pytest.raises(ValueError, match='Paid connector unit changed'):
        reconcile(memory, snapshot, native, resume=True)


@pytest.mark.parametrize('tamper', [None, 'donor_page', 'second_summary'])
def test_attachment_compares_shared_and_donor_cells_without_mutation(tamper):
    from jev_factorio.backends.native_completed_attachment import qualify_completed_connectors
    from jev_factorio.backends.native_current_attachment import current_connector_snapshot_command
    from test_native_current_attachment import current
    memory, snapshot, _ = retained()
    binding = deepcopy(memory.connector_ownership)
    result = current()
    result.update(session_id=memory.session_id, actor_unit=2543)
    result['native_installation'].update(session_id=memory.session_id, actor_unit=2543)
    native = Pages(binding)
    donor = next(v for v in native.routes.values() if v['target'] == 'utility:lab')
    if tamper == 'donor_page': donor['cells'][0]['unit_number'] += 999
    ownership = deepcopy(snapshot.factory['connector_ownership'])
    ownership.pop('active', None)
    reads = []

    class Client:
        def send_command(self, command):
            if 'connector_page(' in command:
                return native.command(command)
            assert command == current_connector_snapshot_command(result, completed_routes=True)
            reads.append(command)
            row = deepcopy(ownership)
            if tamper == 'second_summary' and len(reads) == 2:
                row['routes'][donor['id']]['source_unit'] += 1
            return json.dumps(dict(schema=1, session_id=memory.session_id, actor_unit=2543,
                                   tick=snapshot.tick, connector_ownership=row, completed_craft=False,
                                   settled_factory=dict(sites={}, output_offers={}, outpost_offers={})))

    if tamper:
        with pytest.raises((ValueError, RuntimeError)):
            qualify_completed_connectors(Client(), result, binding)
    else:
        assert qualify_completed_connectors(Client(), result, binding)['connector_snapshot_qualified']
        assert len(reads) == 2 and len(native.calls) == 4
    assert binding == memory.connector_ownership


def test_compatible_source_handoff_preserves_pending_and_all_budget_state(tmp_path, monkeypatch):
    from jev_factorio import compatible_recovery as recovery
    from test_compatible_source_recovery import setup, NEW, OWNER
    _, original, path, authority = setup(tmp_path, monkeypatch)
    memory, _, _ = retained()
    memory.blocked_recovery = deepcopy(original.memory.blocked_recovery)
    memory.blocked_recovery['session_id'] = memory.session_id
    memory.failures = {'retained-failure': 2}
    memory.save(path)
    import hashlib
    authority.update(session_id=memory.session_id, target=memory.target,
                     checkpoint_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                     scope=recovery.scope(memory))
    before = asdict(memory)
    migrated = recovery.migrate_checkpoint(path, authority, CampaignMemory, NEW, OWNER, lock_fd=9)
    after = asdict(migrated)
    for key in set(before) - {'blocked_recovery', 'compatible_source_recoveries'}:
        assert after[key] == before[key], key
    assert migrated.blocked_recovery['attempts'] == memory.blocked_recovery['attempts']
    assert migrated.blocked_recovery['source_revision'] == NEW
    assert len(migrated.compatible_source_recoveries) == 1


def test_installed_lua_shared_routes_attach_without_rewriting_ownership():
    from test_native_completed_attachment import installed_case
    from test_native_optional_profiles import _owner_state_json
    from jev_factorio.backends.native_attachment import readback
    lua, client, old_binding = installed_case()
    memory, _, _ = retained()
    binding = deepcopy(memory.connector_ownership)
    owner = next(iter(old_binding['routes'].values()))
    binding['session_id'] = owner['session_id']
    rows = deepcopy(binding['routes'])
    for key, row in binding['routes'].items():
        row.update(session_id=owner['session_id'], actor_unit=owner['actor_unit'])
        rows[key] = deepcopy(row)
        rows[key]['cell_count'] = len(row['cells'])
        for cell in rows[key]['cells']: cell['paid'] = int(cell['paid'])
    lua.globals().shared_rows = lua.table_from(rows, recursive=True)
    lua.execute('''
      local c=jev_fle_runtime.campaign;local p=game.get_player(1)
      c.connector_ledger.routes=shared_rows
      local units={}
      for _,row in pairs(shared_rows) do
        c.entities[row.source]={valid=true,unit_number=row.source_unit}
        c.entities[row.target]={valid=true,unit_number=row.target_unit}
        for _,cell in ipairs(row.cells) do
          units[row.kind..':'..cell.position.x..':'..cell.position.y]=cell.unit_number
        end
      end
      p.surface.find_entity=function(name,pos)
        local unit=units[name..':'..pos.x..':'..pos.y]
        if unit then return {valid=true,unit_number=unit,force=p.force} end
      end
    ''')
    before = _owner_state_json(lua)
    assert readback(client, checkpoint_binding=binding)['connector_snapshot_qualified']
    assert _owner_state_json(lua) == before
