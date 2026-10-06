"""Durable archive-pointer checkpoint and exact-request archive controls."""
from __future__ import annotations

import hashlib
import json
import base64

import pytest

from jev_factorio.async_decision_archive import (
    AsyncDecisionArchive,
    AsyncDecisionArchiveBusy,
    AsyncDecisionArchiveError,
    validate_pointer,
)
from jev_factorio.checkpoint_io import checkpoint_data
from jev_factorio.memory import CampaignMemory


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _record(request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"):
    payload = {"state": {"tick": 8}, "model": "fixture", "questions": {"choice": {}}}
    payload_sha256 = _hash(payload)
    wal_request = {
        "provider_payload_sha256": payload_sha256,
        "trace_binding": None,
        "health_state_sha256": "c" * 64,
    }
    wire_body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                           separators=(",", ":")).encode("utf-8")
    return {
        "schema": 1,
        "archive_id": "a" * 64,
        "request_id": request_id,
        "identity": {
            "session_id": "session-a", "actor_id": "actor:" + "d" * 64,
            "observation_id": "observation:1", "decision_id": "decision:1",
            "request_id": request_id,
            "provider_id": "mock:sha256:" + "e" * 64, "model_id": "fixture-model",
        },
        "selector": {
            "source_state": {"tick": 8},
            "state": {"tick": 8}, "questions": {"choice": {}},
            "candidate_plans": [{"id": "first"}, {"id": "second"}],
            "offered_plans": [{"id": "first"}],
            "source_revision": {"commit": "f" * 40, "source_sha256": "0" * 64},
            "session_id": "session-a", "actor_id": "actor:" + "d" * 64,
            "runtime_identity": {
                "session_id": "session-a", "actor_unit": 3,
                "surface_index": 1, "force_index": 1,
            },
            "observation_id": "observation:1", "decision_id": "decision:1",
            "request_id": request_id, "observation_tick": 8,
            "target": "rocket_launch", "policy": "jev", "confidence_floor": 0.45,
            "max_request_bytes": 48000, "request_order": {},
            "persistent_input_sha256": None, "selection_batch": None,
            "source_authorized": False, "source_auth_reason_sha256": None,
            "frontier_sha256": "f" * 64,
        },
        "provider": {
            "provider_id": "mock:sha256:" + "e" * 64,
            "model_id": "fixture-model", "payload": payload,
            "payload_json": json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                       sort_keys=True, separators=(",", ":")),
            "payload_sha256": payload_sha256, "wal_request": wal_request,
            "wire_body_base64": base64.b64encode(wire_body).decode("ascii"),
            "wire_body_sha256": hashlib.sha256(wire_body).hexdigest(),
            "wal_id": "1" * 64,
        },
    }


def test_campaign_memory_roundtrips_strict_async_archive_pointer(tmp_path):
    safety = tmp_path / "campaign.safety"
    safety.mkdir(mode=0o700)
    archive = AsyncDecisionArchive(safety / "decision-archives")
    pointer = archive.store(_record())
    memory = CampaignMemory("session-a", "rocket_launch")
    memory.async_decision = pointer
    assert checkpoint_data(memory)["async_decision"] == pointer

    path = tmp_path / "campaign.json"
    memory.save(path)
    restored = CampaignMemory.from_bytes(path.read_bytes(), "session-a", "rocket_launch")

    assert restored.async_decision == pointer
    assert checkpoint_data(CampaignMemory("session-a", "rocket_launch")).get("async_decision") is None


def test_archive_reopens_exact_selector_provider_and_wal_binding(tmp_path):
    safety = tmp_path / "campaign.safety"
    safety.mkdir(mode=0o700)
    directory = safety / "decision-archives"
    archive = AsyncDecisionArchive(directory)
    record = _record()
    pointer = archive.store(record)
    reopened = AsyncDecisionArchive(directory).load(pointer)

    assert reopened["selector"] == record["selector"]
    assert reopened["provider"]["payload"] == record["provider"]["payload"]
    assert reopened["provider"]["wal_request"] == record["provider"]["wal_request"]
    assert pointer["disposition"] == "pending"


def test_async_settlement_marker_is_immutable_and_bound_to_archive(tmp_path):
    archive = AsyncDecisionArchive(tmp_path / "decision-archives")
    pointer = archive.store(_record())
    record = archive.load(pointer)
    entry = archive.settlement_entry(
        record, disposition="selected", wal_state="consumed",
        wal_phase="response_received", wal_record_sha256="d" * 64,
        selected_plan_id="plan-a", selected_plan_sha256="e" * 64,
    )

    archive.store_settlement(record, entry)

    assert archive.load_settlement(record) == entry
    archive.store_settlement(record, entry)
    assert archive.load_settlement(record) == entry
    with pytest.raises(AsyncDecisionArchiveBusy, match="cannot be replaced"):
        archive.store_settlement(record, {**entry, "selected_plan_id": "plan-b"})

    with pytest.raises(AsyncDecisionArchiveError, match="settlement"):
        archive.settlement_entry(
            record, disposition="no_action", wal_state="ambiguous",
            wal_phase="may_have_been_sent", wal_record_sha256="f" * 64,
        )


def test_campaign_memory_retains_settlement_lineage_inside_history_bound():
    memory = CampaignMemory("session-a", "rocket_launch")
    memory.event(
        "async_decision_settled", schema=1, entries=[], entries_sha256="a" * 64)
    for index in range(80):
        memory.event("ordinary_test_event", sequence=index)

    assert len(memory.history) == 64
    assert sum(row.get("kind") == "async_decision_settled"
               for row in memory.history) == 1
    assert memory.history[0]["kind"] == "async_decision_settled"
    assert memory.history[-1]["sequence"] == 79


def test_campaign_memory_replaces_settlement_ledger_as_one_cumulative_row():
    memory = CampaignMemory("session-a", "rocket_launch")
    memory.event("async_decision_settled", schema=1, entries=[{"archive_id": "a"}],
                 entries_sha256="b" * 64)
    memory.event("ordinary_event", sequence=1)
    memory.event("async_decision_settled", schema=1,
                 entries=[{"archive_id": "a"}, {"archive_id": "c"}],
                 entries_sha256="d" * 64)

    ledgers = [row for row in memory.history
               if row.get("kind") == "async_decision_settled"]
    assert len(ledgers) == 1
    assert ledgers[0]["entries"] == [{"archive_id": "a"}, {"archive_id": "c"}]


def test_campaign_memory_retains_one_cumulative_async_plan_lineage_row():
    memory = CampaignMemory("session-a", "rocket_launch")
    memory.event("async_plan_lineage", schema=1, entries=[], entries_sha256=_hash([]))
    for index in range(80):
        memory.event("ordinary_test_event", sequence=index)

    rows = [row for row in memory.history if row.get("kind") == "async_plan_lineage"]
    assert len(memory.history) == 64
    assert len(rows) == 1
    assert rows[0]["entries"] == []


def test_async_plan_lineage_rejects_nested_step_record_and_byte_overflow(tmp_path):
    from jev_factorio.controller import HierarchicalLoop
    from jev_factorio.backends.mock import MockBackend
    from jev_factorio.jev_client import AsyncMockJevClient
    from jev_factorio.memory import CampaignMemory

    loop = HierarchicalLoop(
        MockBackend(), jev=AsyncMockJevClient(),
        target="rocket_launch", checkpoint=str(tmp_path / "campaign.json"),
        tick_seconds=0, async_decisions=True,
    )
    loop.memory = CampaignMemory("fixture-session", "rocket_launch")
    entry = {
        "archive_id": "a" * 64, "archive_sha256": "b" * 64,
        "request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "selected_plan_id": "plan-a", "selected_plan_sha256": "c" * 64,
        "source_revision_sha256": "d" * 64, "selector_sha256": "e" * 64,
        "provider_payload_sha256": "f" * 64, "wire_body_sha256": "0" * 64,
        "state": "active", "verified_steps": [{} for _ in range(33)],
        "terminal": None,
    }
    entries = [entry]
    loop.memory.event("async_plan_lineage", schema=1, entries=entries,
                      entries_sha256=_hash(entries))
    with pytest.raises(ValueError, match="lineage entry is malformed"):
        loop._async_plan_lineage_entries()

    before = list(loop.memory.history)
    with pytest.raises(ValueError, match="record capacity"):
        loop._async_store_plan_lineage([{} for _ in range(129)])
    with pytest.raises(ValueError, match="byte capacity"):
        loop._async_store_plan_lineage([{"padding": "x" * 40_000} for _ in range(128)])
    assert loop.memory.history == before


def test_archive_refuses_conflicting_replacement_and_pointer_tampering(tmp_path):
    archive = AsyncDecisionArchive(tmp_path / "decision-archives")
    pointer = archive.store(_record())
    conflicting = _record("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    with pytest.raises(AsyncDecisionArchiveBusy):
        archive.store(conflicting)

    bad = {**pointer, "archive_sha256": "9" * 64}
    with pytest.raises(AsyncDecisionArchiveError):
        archive.load(bad)


@pytest.mark.parametrize("bad_pointer", [
    {"schema": 1, "archive_id": "a" * 64, "archive_sha256": "b" * 64,
     "request_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "disposition": "pending", "selected_plan_id": "plan",
     "selected_plan_sha256": "d" * 64},
    {"schema": 1, "archive_id": "a" * 64, "archive_sha256": "b" * 64,
     "request_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "disposition": "selected", "selected_plan_id": None,
     "selected_plan_sha256": None},
    {"schema": 1, "archive_id": "a" * 64, "archive_sha256": "b" * 64,
     "request_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "disposition": "selected", "selected_plan_id": "plan",
     "selected_plan_sha256": "d" * 64, "unknown": True},
])
def test_archive_pointer_rejects_inconsistent_or_unknown_fields(bad_pointer):
    with pytest.raises(AsyncDecisionArchiveError):
        validate_pointer(bad_pointer)


def test_archive_rejects_payload_digest_mismatch(tmp_path):
    archive = AsyncDecisionArchive(tmp_path / "decision-archives")
    record = _record()
    record["provider"]["payload"]["state"]["tick"] = 9
    with pytest.raises(AsyncDecisionArchiveError, match="payload digest"):
        archive.store(record)
