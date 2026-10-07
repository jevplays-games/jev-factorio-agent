"""Captured V41 failure plus synthetic durability and native-action boundaries."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from jev_factorio import two_stage_decision as protocol
from jev_factorio.skills import Plan, Step
from jev_factorio.state import GameSnapshot
from test_two_stage_controller import campaign, LiveClient, LiveMockBackend


def captured():
    document = json.loads((Path(__file__).parent / 'fixtures/native-v41-fish-churn.json').read_bytes())
    assert all(row['payload']['accepted'] is True for row in document['accepted_validations'])
    return document, [GameSnapshot(**row) for row in document['snapshots']]


def test_captured_fish_appearance_and_motion_stop_invalidating_unrelated_work():
    _, snapshots = captured()
    original = deepcopy(snapshots)
    assert [snapshot.tick for snapshot in snapshots] == [18185552, 18185798, 18185923]
    assert snapshots[0].factory['launch_readiness']['fish'] == {}
    assert snapshots[1].factory['launch_readiness']['fish']['id'] == 'fish:5'
    old = [protocol.native_digest(snapshot, protocol.NATIVE_PROJECTION) for snapshot in snapshots]
    assert old[1] == 'bb96c9dbbb15d2945debadc5148925cfa190fa4b4406158485ad7ce88d6378dc'
    assert len(set(old)) == 3
    assert len({protocol.native_digest(snapshot, protocol.NON_LAUNCH_PROJECTION)
                for snapshot in snapshots}) == 1
    facts = protocol.selection_facts(snapshots[1].for_jev(), protocol.NON_LAUNCH_PROJECTION)
    assert 'fish' not in facts['factory']['launch_readiness']
    assert facts['factory']['launch_readiness']['attempts'] == snapshots[1].factory['launch_readiness']['attempts']
    assert snapshots == original


@pytest.mark.parametrize('mutation', ['inventory', 'fault', 'attempt', 'receipt',
                                     'actor', 'force', 'surface', 'boiler_fuel'])
def test_relevant_native_changes_still_invalidate_unrelated_work(mutation):
    _, snapshots = captured()
    before, after = snapshots[1], deepcopy(snapshots[1])
    row = after.factory['launch_readiness']
    if mutation == 'inventory': after.inventory['coal'] += 1
    elif mutation == 'fault': row['fault'] = not row['fault']
    elif mutation == 'attempt': row['attempts']['fish'] = 'new attempt'
    elif mutation == 'receipt': row['receipts']['new'] = {'kind': 'fish'}
    elif mutation == 'actor': row['actor_unit'] += 1
    elif mutation == 'force': row['force_index'] += 1
    elif mutation == 'surface': row['surface_index'] += 1
    else: after.factory['entities']['utility:boiler']['fuel']['coal'] = 2
    assert protocol.native_digest(before, protocol.NON_LAUNCH_PROJECTION) != protocol.native_digest(
        after, protocol.NON_LAUNCH_PROJECTION)


@pytest.mark.parametrize('mutation', ['position', 'id', 'yield', 'reach', 'missing_fish',
                                     'foreign_session', 'stale_tick', 'unsupported'])
def test_invalid_launch_observation_is_not_hidden_by_projection(mutation):
    _, snapshots = captured()
    snapshot = snapshots[1]
    row = snapshot.factory['launch_readiness']
    if mutation == 'position': row['fish']['position']['x'] = float('nan')
    elif mutation == 'id': row['fish']['id'] = ''
    elif mutation == 'yield': row['fish']['yield'] = True
    elif mutation == 'reach': row['fish']['reachable'] = False
    elif mutation == 'missing_fish': row.pop('fish')
    elif mutation == 'foreign_session': row['session_id'] = 'foreign'
    elif mutation == 'stale_tick': row['tick'] -= 1
    else: row['supported'] = False
    with pytest.raises(ValueError):
        protocol.native_digest(snapshot, protocol.NON_LAUNCH_PROJECTION)


@pytest.mark.parametrize('action,effect', [
    ('factory_launch_fish', 'launch_fish'), ('factory_launch_pad', 'launch_pad'),
    ('factory_launch_payload', 'launch_payload'), ('factory_launch', 'rocket_launched'),
    ('factory_wait', 'rocket_launched'),
])
def test_every_launch_step_retains_the_complete_fish_observation(action, effect):
    parameters = {
        'factory_launch_fish': {'target': 'fish:5', 'receipt': 'launch:fish:1'},
        'factory_launch_pad': {'site': 'pad:1', 'receipt': 'launch:pad:1'},
        'factory_launch_payload': {'role': 'recipe:rocket-part', 'silo_unit': 1,
                                   'rocket_unit': 2, 'item': 'raw-fish', 'receipt': 'launch:load:1'},
        'factory_launch': {'role': 'recipe:rocket-part'}, 'factory_wait': {},
    }[action]
    plans = [Plan('ordinary', 'rocket_launch', 'Gather coal',
                  (Step('factory_gather', 'inventory', 'coal', 5,
                        parameters={'resource': 'coal', 'quantity': 5}),)),
             Plan('launch', 'rocket_launch', 'Launch work',
                  (Step('factory_wait', 'crafting_idle'), Step(action, effect, parameters=parameters)))]
    assert protocol.projection_for_plans(plans) == protocol.NATIVE_PROJECTION
    _, snapshots = captured()
    assert protocol.native_digest(snapshots[1], protocol.projection_for_plans(plans)) != protocol.native_digest(
        snapshots[2], protocol.projection_for_plans(plans))


def test_saved_v1_record_and_unmarked_digest_keep_their_original_contract():
    document, snapshots = captured()
    record = document['retained_record']
    original = protocol.encoded(record)
    assert record['prepared']['context']['native_freshness_projection'] == protocol.NATIVE_PROJECTION
    protocol.validate(record, record['binding']['session_id'], record['binding']['target'])
    assert protocol.encoded(record) == original
    assert protocol.native_digest(snapshots[1]) != protocol.native_digest(snapshots[2])


def test_omission_contract_cannot_be_attached_to_a_launch_action():
    from test_two_stage_decision import prepared
    record = prepared()
    record['prepared']['context']['native_freshness_projection'] = protocol.NON_LAUNCH_PROJECTION
    record['prepared']['plans'][0]['steps'][0]['action'] = 'factory_launch_fish'
    record['prepared']['plans'][0]['steps'][0]['parameters'] = {'target': 'fish:5', 'receipt': 'launch:fish:1'}
    record['prepared_sha256'] = protocol.digest({'binding': record['binding'], 'prepared': record['prepared']})
    with pytest.raises(ValueError, match='Launch actions require complete'):
        protocol.validate(record, record['binding']['session_id'], record['binding']['target'])


class MovingFishBackend(LiveMockBackend):
    def __init__(self, material_change=False):
        super().__init__()
        self.reads = 0
        self.material_change = material_change

    def observe(self):
        snapshot = super().observe()
        self.reads += 1
        factory = deepcopy(snapshot.factory)
        fish = ({'id': 'fish:5', 'position': {'x': self.reads / 10, 'y': 4.5},
                 'reachable': True, 'yield': 5} if self.reads % 3 else {})
        factory['launch_readiness'] = {
            'schema': 1, 'supported': True, 'version': '2.0.77',
            'session_id': snapshot.session_id, 'tick': snapshot.tick,
            'actor_unit': 1, 'surface_index': 1, 'force_index': 1,
            'fault': False, 'attempts': {}, 'receipts': {},
            'pad': {}, 'pad_site': {}, 'fish': fish, 'silo': {},
        }
        inventory = dict(snapshot.inventory)
        if self.material_change:
            inventory['coal'] = self.reads
        return replace(snapshot, factory=factory, inventory=inventory)


def test_controller_keeps_exact_fish_freshness_for_launch_frontier(tmp_path, monkeypatch):
    client, backend = LiveClient(), MovingFishBackend()
    loop, _, _ = campaign(tmp_path, monkeypatch, client=client, backend=backend)
    plan = Plan('fish', 'rocket_launch', 'Collect the observed fish',
                (Step('factory_launch_fish', 'launch_fish',
                      parameters={'target': 'fish:5', 'receipt': 'launch:fish:1'}),))
    loop._work_candidates = lambda _snapshot: ([plan], '')
    loop.step()
    record = loop.memory.two_stage_decision
    assert record['prepared']['context']['native_freshness_projection'] == protocol.NATIVE_PROJECTION
    assert 'fish' in record['prepared']['context']['facts']['factory']['launch_readiness']
    assert record['outcome'] == 'stale_evidence'
    assert client.calls == [] and backend.actions == []


@pytest.mark.parametrize('material_change', [False, True])
def test_controller_binds_same_projection_to_both_requests_and_fresh_native_checks(
        tmp_path, monkeypatch, material_change):
    client, backend = LiveClient(), MovingFishBackend(material_change)
    loop, _, _ = campaign(tmp_path, monkeypatch, client=client, backend=backend)
    loop.step()
    if material_change:
        assert backend.actions == [] and client.calls == []
        assert loop.memory.two_stage_decision['outcome'] == 'stale_evidence'
    else:
        assert backend.actions[0] == 'walk_to_coal' and len(client.calls) == 2
        for state, _ in client.calls:
            assert state['native_freshness_projection'] == protocol.NON_LAUNCH_PROJECTION
            assert 'fish' not in state['facts']['factory']['launch_readiness']


def test_fish_motion_and_disappearance_cannot_reroll_a_strict_rejection(tmp_path, monkeypatch):
    backend, client = MovingFishBackend(), LiveClient(confidence=.37)
    loop, _, _ = campaign(tmp_path, monkeypatch, client=client, backend=backend)
    loop.step()
    original = deepcopy(loop.memory.two_stage_decision)
    assert original['outcome'] == 'low_choice_confidence'
    assert len(client.calls) == 2 and backend.actions == []
    resumed_client = LiveClient()
    resumed, _, _ = campaign(tmp_path, monkeypatch, client=resumed_client, backend=backend, resume=True)
    for _ in range(3):
        resumed.step()
    assert resumed_client.calls == [] and backend.actions == []
    assert protocol.encoded(resumed.memory.two_stage_decision) == protocol.encoded(original)


@pytest.mark.parametrize('phase', ['assessment', 'choice'])
def test_fish_motion_does_not_replay_an_ambiguous_provider_request(tmp_path, monkeypatch, phase):
    backend = MovingFishBackend()
    loop, _, _ = campaign(tmp_path, monkeypatch, client=LiveClient(timeout=phase), backend=backend)
    loop.step()
    assert loop.memory.two_stage_decision['phase'] == phase + '_pending'
    client = LiveClient()
    resumed, _, _ = campaign(tmp_path, monkeypatch, client=client, backend=backend, resume=True)
    resumed.step()
    assert client.calls == [] and backend.actions == []
    assert resumed.memory.two_stage_decision['phase'] == phase + '_pending'
