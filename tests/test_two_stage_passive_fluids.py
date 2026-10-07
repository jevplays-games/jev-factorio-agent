"""Captured boiler churn and synthetic freshness/dispatch boundaries."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from jev_factorio import two_stage_decision as protocol
from jev_factorio.state import GameSnapshot
from test_two_stage_controller import campaign, LiveClient, LiveMockBackend


def captured():
    document = json.loads((Path(__file__).parent/'fixtures/native-v32-boiler-churn.json').read_bytes())
    return [GameSnapshot(**row) for row in document['snapshots']]


def test_captured_only_steam_quantity_drift_keeps_projected_evidence_current():
    before, after = captured()
    original = deepcopy(before)
    assert protocol.native_digest(before) != protocol.native_digest(after)
    assert protocol.native_digest(before, protocol.NATIVE_PROJECTION) == protocol.native_digest(
        after, protocol.NATIVE_PROJECTION)
    projected = protocol.selection_facts(before.for_jev(), protocol.NATIVE_PROJECTION)
    boiler = projected['factory']['entities']['utility:boiler']
    assert 'fluids' not in boiler and boiler['fluid_presence']['steam'] is True
    assert before == original


@pytest.mark.parametrize('mutation', ['empty', 'missing_fluid', 'other_fluid', 'unit', 'fuel',
                                     'status', 'ports', 'inventory', 'other_entity_fluid'])
def test_material_changes_still_invalidate_native_freshness(mutation):
    before, _ = captured()
    after = deepcopy(before)
    boiler = after.factory['entities']['utility:boiler']
    if mutation == 'empty': boiler['fluids']['steam'] = 0
    elif mutation == 'missing_fluid': boiler['fluids'].pop('steam')
    elif mutation == 'other_fluid': boiler['fluids']['crude-oil'] = 1
    elif mutation == 'unit': boiler['unit_number'] += 1
    elif mutation == 'fuel': boiler['fuel']['coal'] = boiler['fuel'].get('coal', 0)+1
    elif mutation == 'status': boiler['status'] = 'different'
    elif mutation == 'ports': boiler['fluid_ports'] = []
    elif mutation == 'inventory': after.inventory['coal'] = after.inventory.get('coal', 0)+1
    else: after.factory['entities']['utility:engine']['fluids']['steam'] += .1
    assert protocol.native_digest(before, protocol.NATIVE_PROJECTION) != protocol.native_digest(
        after, protocol.NATIVE_PROJECTION)


@pytest.mark.parametrize('amount', [True, -1, float('nan'), float('inf'), '175'])
def test_malformed_telemetry_is_never_normalized_into_current_evidence(amount):
    before, _ = captured()
    before.factory['entities']['utility:boiler']['fluids']['steam'] = amount
    with pytest.raises(ValueError, match='Invalid boiler'):
        protocol.native_digest(before, protocol.NATIVE_PROJECTION)


class PassiveBoilerBackend(LiveMockBackend):
    def __init__(self, material_change=False):
        super().__init__()
        self.reads = 0
        self.material_change = material_change

    def observe(self):
        snapshot = super().observe()
        self.reads += 1
        factory = deepcopy(snapshot.factory or {})
        factory.setdefault('entities', {})['utility:boiler'] = {
            'name': 'boiler', 'unit_number': 1, 'fluids': {'steam': 175+self.reads/100},
            'fuel': {'coal': self.reads if self.material_change else 5}}
        return replace(snapshot, factory=factory)


@pytest.mark.parametrize('material_change', [False, True])
def test_real_controller_uses_same_projection_for_requests_and_phase_checks(
        tmp_path, monkeypatch, material_change):
    client, backend = LiveClient(), PassiveBoilerBackend(material_change)
    loop, _, _ = campaign(tmp_path, monkeypatch, client=client, backend=backend)
    loop.step()
    if material_change:
        assert backend.actions == [] and client.calls == []
        assert loop.memory.two_stage_decision['outcome'] == 'stale_evidence'
    else:
        assert backend.actions[0] == 'walk_to_coal' and len(client.calls) == 2
        for state, _ in client.calls:
            assert state['native_freshness_projection'] == protocol.NON_LAUNCH_PROJECTION
            boiler = state['facts']['factory']['entities']['utility:boiler']
            assert 'fluids' not in boiler and boiler['fluid_presence']['steam'] is True


def test_legacy_projection_and_unknown_contract_are_not_silently_changed():
    before, _ = captured()
    facts = before.for_jev()
    assert protocol.selection_facts(facts) == facts
    with pytest.raises(ValueError, match='Unknown native'):
        protocol.selection_facts(facts, 'future-version')


def test_passive_churn_does_not_grant_another_choice_after_strict_rejection(tmp_path, monkeypatch):
    client, backend = LiveClient(confidence=.37), PassiveBoilerBackend()
    loop, _, checkpoint = campaign(tmp_path, monkeypatch, client=client, backend=backend)
    loop.step()
    original = deepcopy(loop.memory.two_stage_decision)
    assert original['outcome'] == 'low_choice_confidence' and len(client.calls) == 2
    resumed_client = LiveClient()
    resumed, _, _ = campaign(tmp_path, monkeypatch, client=resumed_client,
                             backend=backend, resume=True)
    resumed.step()
    assert resumed_client.calls == [] and backend.actions == []
    assert protocol.encoded(resumed.memory.two_stage_decision) == protocol.encoded(original)
