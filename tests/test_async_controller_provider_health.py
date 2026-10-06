"""Provider-health reconciliation for controller-owned async decisions."""
from __future__ import annotations

import asyncio

import pytest

from jev_factorio.async_provider import RequestIdentity
from jev_factorio.provider_decision_wal import ProviderDecisionWAL
from jev_factorio.provider_health import ProviderCircuit
from jev_factorio.operational_safety import SafetyStateError
from jev_factorio.jev_client import AsyncMockJevClient


class SaveThenInterrupt(AsyncMockJevClient):
    async def evaluate(self, state, questions, *, identity, deadline=None,
                       decision_lease=None):
        await super().evaluate(
            state, questions, identity=identity, deadline=deadline,
            decision_lease=decision_lease)
        raise asyncio.CancelledError


def test_consumed_response_repairs_interrupted_health_commit_and_ack_is_idempotent(tmp_path):
    safety = tmp_path / "safety"
    safety.mkdir(mode=0o700)
    wal = ProviderDecisionWAL.initialize(safety / "provider-decisions.json")
    client = SaveThenInterrupt()
    circuit = ProviderCircuit(client, safety / "provider.json")
    state = {"facts": {"tick": 8}, "candidate_plans": {"plan-a": {"id": "plan-a"}}}
    questions = {"next_action": {
        "type": "choice", "criteria": {"plan-a": "choose this action"},
    }}
    identity = RequestIdentity(
        session_id="session-a", actor_id="actor-a", observation_id="observation-a",
        decision_id="decision-a", request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    lease = circuit.prepare_decision_lease(wal, state, questions, identity=identity)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(circuit.evaluate_async(
            state, questions, identity=identity, decision_lease=lease,
        ))

    response = lease.inspect()
    assert response.state == "response_received"
    assert circuit.state["in_flight"] is not None
    assert "decision_outcome" not in circuit.state
    with pytest.raises(SafetyStateError, match="WAL-consumed"):
        circuit.acknowledge_decision_consumed(lease)

    wal.consume_response(lease.identity, lease.wal_request, response.result_sha256)
    circuit.acknowledge_decision_consumed(lease)
    recovered = dict(circuit.state)
    assert recovered["in_flight"] is None
    assert recovered["decision_outcome"]["state"] == "consumed"

    circuit.acknowledge_decision_consumed(lease)
    assert circuit.state == recovered
