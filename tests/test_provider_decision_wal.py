from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from jev_factorio import provider_decision_wal as wal_module
from jev_factorio.provider_decision_wal import (
    MAY_HAVE_BEEN_SENT,
    NOT_SENT,
    RESPONSE_RECEIVED,
    ProviderDecisionIdentity,
    ProviderDecisionWAL,
    WALAmbiguousRequest,
    WALBusyError,
    WALCapacityError,
    WALIdentityConflict,
    WALIntegrityError,
    WALInvalidTransition,
    WALNotFound,
    WALResponseConsumed,
    canonical_sha256,
)


def identity(**overrides) -> ProviderDecisionIdentity:
    values = {
        "session_id": "session-a",
        "actor_id": "actor-a",
        "observation_id": "observation-17",
        "decision_id": "decision-17",
        "provider_id": "typesafe",
        "model_id": "model-v1",
        "request_id": "request-17",
    }
    values.update(overrides)
    return ProviderDecisionIdentity(**values)


def request(**overrides) -> dict:
    value = {
        "candidate_ids": ["plan-a", "plan-b"],
        "facts": {"tick": 17, "inventory": {"iron-plate": 4}},
        "prompt_version": "selection-v1",
    }
    value.update(overrides)
    return value


def result(**overrides) -> dict:
    value = {
        "answers": {"plan-a": {"rank": 1}, "plan-b": {"rank": 2}},
        "usage": {"input_tokens": 120, "output_tokens": 9},
        "requested_model": "model-v1",
        "resolved_model": "model-v1.1",
    }
    value.update(overrides)
    return value


def create(tmp_path):
    path = tmp_path / "provider-decisions.json"
    return path, ProviderDecisionWAL.initialize(path)


def test_reservation_is_canonical_idempotent_private_and_distinct_from_action_wal(tmp_path):
    path, wal = create(tmp_path)
    original = request()
    reserved = wal.reserve(identity(), original)
    assert reserved.phase == NOT_SENT
    assert reserved.state == "reserved"
    assert reserved.request_sha256 == canonical_sha256(original)
    assert reserved.event_count == 1

    reordered = {
        "prompt_version": "selection-v1",
        "facts": {"inventory": {"iron-plate": 4}, "tick": 17},
        "candidate_ids": ["plan-a", "plan-b"],
    }
    reopened = ProviderDecisionWAL(path)
    assert reopened.reserve(identity(), reordered) == reserved

    disk = path.read_text(encoding="utf-8")
    assert "plan-a" not in disk
    assert "selection-v1" not in disk
    assert "pending" not in disk
    assert "attempt" not in disk
    assert "request_sha256" in disk


def test_identity_and_content_cannot_be_rebound_by_new_request_ids(tmp_path):
    _, wal = create(tmp_path)
    wal.reserve(identity(), request())

    fingerprinted = identity(
        session_id="session-cf", actor_id="actor-cf",
        provider_id="cloudflare:sha256:" + "a" * 64,
        observation_id="observation-cf", decision_id="decision-cf",
        request_id="request-cf",
    )
    assert wal.reserve(fingerprinted, request(candidate_ids=["cf-plan"])).state == "reserved"
    with pytest.raises(ValueError):
        identity(provider_id="raw-api-key-credential")

    with pytest.raises(WALIdentityConflict):
        wal.reserve(identity(observation_id="observation-18"), request())
    with pytest.raises(WALIdentityConflict):
        wal.reserve(identity(request_id="request-reused"), request())
    with pytest.raises(WALIdentityConflict):
        wal.reserve(identity(decision_id="decision-new", request_id="request-new"), request())
    with pytest.raises(WALIdentityConflict):
        wal.reserve(identity(), request(prompt_version="changed"))


def test_write_before_send_and_may_have_been_sent_never_auto_replays(tmp_path):
    path, wal = create(tmp_path)
    binding = identity()
    original = request()
    wal.reserve(binding, original)
    with pytest.raises(WALInvalidTransition, match="no durable response"):
        wal.recover_response(binding, original)
    assert wal.inspect(binding, original).state == "reserved"

    admitted = wal.mark_may_have_been_sent(binding, original)
    assert admitted.phase == MAY_HAVE_BEEN_SENT
    assert admitted.state == "may_have_been_sent"

    reopened = ProviderDecisionWAL(path)
    assert reopened.reserve(binding, original).state == "may_have_been_sent"
    with pytest.raises(WALAmbiguousRequest):
        reopened.recover_response(binding, original)
    with pytest.raises(WALAmbiguousRequest):
        reopened.mark_may_have_been_sent(binding, original)


def test_saved_result_replays_exactly_until_consumed_then_stops(tmp_path):
    path, wal = create(tmp_path)
    binding = identity()
    original = request()
    wal.reserve(binding, original)
    wal.mark_may_have_been_sent(binding, original)

    saved = wal.save_response(binding, original, result())
    assert saved.phase == RESPONSE_RECEIVED
    assert saved.state == "response_received"
    assert saved.result_sha256 == canonical_sha256(result(), max_bytes=wal_module.MAX_RESULT_BYTES)
    assert saved.result["answers"]["plan-a"]["rank"] == 1
    with pytest.raises(TypeError):
        saved.result["answers"]["plan-c"] = {"rank": 3}

    reopened = ProviderDecisionWAL(path)
    replay = reopened.recover_response(binding, original)
    assert replay == saved
    assert reopened.save_response(binding, original, result()) == saved
    with pytest.raises(WALIdentityConflict):
        reopened.save_response(
            binding, original,
            result(answers={"plan-a": {"rank": 2}, "plan-b": {"rank": 1}}),
        )
    with pytest.raises(WALInvalidTransition):
        reopened.consume_response(binding, original, "0" * 64)

    consumed = reopened.consume_response(binding, original, saved.result_sha256)
    assert consumed.state == "consumed"
    assert consumed.result_sha256 == saved.result_sha256
    assert ProviderDecisionWAL(path).consume_response(
        binding, original, saved.result_sha256,
    ) == consumed
    with pytest.raises(WALResponseConsumed):
        ProviderDecisionWAL(path).recover_response(binding, original)


def test_pre_send_and_ambiguous_errors_are_terminal_and_store_only_taxonomy(tmp_path):
    path, wal = create(tmp_path)
    pre_send = identity()
    original = request()
    wal.reserve(pre_send, original)
    local = wal.record_error(pre_send, original, "local_admission", NOT_SENT)
    assert local.state == "failed"
    with pytest.raises(WALInvalidTransition):
        wal.mark_may_have_been_sent(pre_send, original)

    admitted_but_not_entered = identity(
        observation_id="observation-18", decision_id="decision-18", request_id="request-18",
    )
    wal.reserve(admitted_but_not_entered, original)
    wal.mark_may_have_been_sent(admitted_but_not_entered, original)
    queue_full = wal.record_error(
        admitted_but_not_entered, original, "local_admission", NOT_SENT,
    )
    assert queue_full.state == "failed"
    assert queue_full.phase == NOT_SENT
    with pytest.raises(WALInvalidTransition):
        wal.mark_may_have_been_sent(admitted_but_not_entered, original)

    ambiguous = identity(
        observation_id="observation-19", decision_id="decision-19", request_id="request-19",
    )
    wal.reserve(ambiguous, request())
    wal.mark_may_have_been_sent(ambiguous, request())
    failure = wal.record_error(ambiguous, request(), "unknown_outcome", MAY_HAVE_BEEN_SENT)
    assert failure.state == "ambiguous"
    assert failure.phase == MAY_HAVE_BEEN_SENT
    assert failure.error_category == "unknown_outcome"
    assert "provider timed out with secret diagnostic" not in path.read_text(encoding="utf-8")
    with pytest.raises(WALInvalidTransition):
        wal.mark_may_have_been_sent(ambiguous, request())
    with pytest.raises(WALCapacityError):
        wal.reserve(
            identity(
                observation_id="observation-20", decision_id="decision-20",
                request_id="request-20",
            ),
            request(candidate_ids=["new-decision"]),
        )
    assert wal.record_error(
        ambiguous, request(), "unknown_outcome", MAY_HAVE_BEEN_SENT,
    ) == failure


def test_response_error_records_received_phase_without_saving_raw_error_body(tmp_path):
    path, wal = create(tmp_path)
    binding = identity()
    original = request()
    wal.reserve(binding, original)
    wal.mark_may_have_been_sent(binding, original)

    failed = wal.record_error(binding, original, "rate_limit", RESPONSE_RECEIVED)
    assert failed.state == "failed"
    assert failed.phase == RESPONSE_RECEIVED
    with pytest.raises(WALInvalidTransition):
        wal.recover_response(binding, original)
    text = path.read_text(encoding="utf-8")
    assert "rate_limit" in text
    assert "Retry-After" not in text
    assert "raw response body" not in text


def test_uncertain_atomic_write_failure_does_not_enter_provider_transport(tmp_path, monkeypatch):
    path, wal = create(tmp_path)
    binding = identity()
    original = request()
    wal.reserve(binding, original)
    installed = wal_module.atomic_json
    transport_entries = []

    def install_then_fail(target, value):
        installed(target, value)
        raise OSError("injected post-install directory-sync uncertainty")

    monkeypatch.setattr(wal_module, "atomic_json", install_then_fail)
    with pytest.raises(OSError):
        wal.mark_may_have_been_sent(binding, original)
    # The caller can enter transport only after mark_may_have_been_sent returns.
    # The injected late failure therefore leaves this count at zero.
    assert transport_entries == []
    monkeypatch.setattr(wal_module, "atomic_json", installed)

    reopened = ProviderDecisionWAL(path)
    assert reopened.inspect(binding, original).state == "may_have_been_sent"
    with pytest.raises(WALAmbiguousRequest):
        reopened.mark_may_have_been_sent(binding, original)
    assert transport_entries == []


def test_fsync_failure_keeps_request_not_sent_and_blocks_transport_entry(tmp_path, monkeypatch):
    path, wal = create(tmp_path)
    binding = identity()
    original = request()
    wal.reserve(binding, original)
    from jev_factorio import operational_safety

    real_fsync = operational_safety.os.fsync
    transport_entries = []

    def offline_provider_call():
        transport_entries.append("fake-provider-dispatch")

    def dispatch_only_after_durable_admission():
        wal.mark_may_have_been_sent(binding, original)
        offline_provider_call()

    def fail_sync(_descriptor):
        raise OSError("injected file fsync failure")

    monkeypatch.setattr(operational_safety.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="fsync"):
        dispatch_only_after_durable_admission()
    assert transport_entries == []
    assert wal.inspect(binding, original).state == "reserved"

    monkeypatch.setattr(operational_safety.os, "fsync", real_fsync)
    dispatch_only_after_durable_admission()
    assert transport_entries == ["fake-provider-dispatch"]
    assert ProviderDecisionWAL(path).inspect(binding, original).state == "may_have_been_sent"


def test_directory_fsync_failure_after_replace_keeps_send_blocked(tmp_path, monkeypatch):
    path, wal = create(tmp_path)
    binding = identity()
    original = request()
    wal.reserve(binding, original)
    from jev_factorio import operational_safety

    real_fsync = operational_safety.os.fsync
    calls = 0
    transport_entries = []

    def fail_directory_sync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected containing-directory fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(operational_safety.os, "fsync", fail_directory_sync)
    with pytest.raises(OSError, match="directory fsync"):
        wal.mark_may_have_been_sent(binding, original)
    assert calls == 2
    assert transport_entries == []
    monkeypatch.setattr(operational_safety.os, "fsync", real_fsync)
    assert ProviderDecisionWAL(path).inspect(binding, original).state == "may_have_been_sent"
    with pytest.raises(WALAmbiguousRequest):
        ProviderDecisionWAL(path).mark_may_have_been_sent(binding, original)


def test_corrupt_missing_or_torn_evidence_fails_closed(tmp_path):
    path, wal = create(tmp_path)
    binding = identity()
    original = request()
    wal.reserve(binding, original)
    valid = path.read_bytes()

    path.write_bytes(valid[: len(valid) // 2])
    with pytest.raises(WALIntegrityError):
        wal.inspect(binding, original)

    path.write_bytes(valid.replace(b'"last_sequence": 1', b'"last_sequence": 2', 1))
    with pytest.raises(WALIntegrityError):
        wal.inspect(binding, original)

    path.unlink()
    with pytest.raises(WALNotFound):
        wal.inspect(binding, original)


def test_writer_lock_is_single_owner_and_fails_fast(tmp_path):
    path, wal = create(tmp_path)
    binding = identity()
    original = request()
    wal.reserve(binding, original)

    with wal_module._writer_lock(path):
        with pytest.raises(WALBusyError):
            wal.inspect(binding, original)


def test_writer_lock_rejects_a_second_process(tmp_path):
    path, wal = create(tmp_path)
    binding = identity()
    original = request()
    wal.reserve(binding, original)
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from jev_factorio.provider_decision_wal import _writer_lock\n"
        "with _writer_lock(Path(sys.argv[1])):\n"
        " print('locked', flush=True)\n"
        " sys.stdin.read(1)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(WALBusyError):
            wal.inspect(binding, original)
        child.stdin.write("x")
        child.stdin.flush()
        assert child.wait(timeout=10) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()


def test_request_result_and_unresolved_bounds_fail_closed(tmp_path, monkeypatch):
    path, wal = create(tmp_path)
    too_large = {"facts": "x" * (wal_module.MAX_REQUEST_BYTES + 1)}
    with pytest.raises(ValueError):
        wal.reserve(identity(), too_large)
    with pytest.raises(ValueError):
        wal.reserve(identity(), request(headers={"authorization": "never-store-this"}))
    with pytest.raises(WALNotFound):
        wal.inspect(identity(), request())

    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="Cyclic JSON value"):
        wal.reserve(identity(), cyclic)

    deeply_nested = {"value": None}
    for _ in range(wal_module.MAX_JSON_DEPTH + 1):
        deeply_nested = {"value": deeply_nested}
    with pytest.raises(ValueError, match="shape bound"):
        wal.reserve(identity(), deeply_nested)

    oversized_sequence = {"items": [None] * wal_module.MAX_JSON_NODES}
    with pytest.raises(ValueError, match="shape bound"):
        wal.reserve(identity(), oversized_sequence)

    assert json.loads(path.read_text(encoding="utf-8"))["last_sequence"] == 0
    assert wal.reserve(identity(), request()).state == "reserved"

    binding = identity()
    original = request()
    wal.reserve(binding, original)
    wal.mark_may_have_been_sent(binding, original)
    oversized_result = result(usage={"detail": "x" * wal_module.MAX_RESULT_BYTES})
    with pytest.raises(ValueError):
        wal.save_response(binding, original, oversized_result)
    assert wal.inspect(binding, original).state == "may_have_been_sent"

    secret_field_result = result(usage={"api_key": "never-persist-this"})
    with pytest.raises(ValueError):
        wal.save_response(binding, original, secret_field_result)
    assert "never-persist-this" not in path.read_text(encoding="utf-8")

    other = identity(
        observation_id="observation-18", decision_id="decision-18", request_id="request-18",
    )
    monkeypatch.setattr(wal_module, "MAX_UNRESOLVED_RECORDS", 1)
    with pytest.raises(WALCapacityError):
        wal.reserve(other, request(candidate_ids=["other-plan"]))


def test_active_actor_is_serialized_and_new_ids_do_not_reset_observation_budget(tmp_path):
    _, wal = create(tmp_path)
    first = identity()
    wal.reserve(first, request())

    with pytest.raises(WALCapacityError):
        wal.reserve(
            identity(observation_id="observation-18", decision_id="decision-18",
                     request_id="request-18"),
            request(candidate_ids=["next-plan"]),
        )
    independent_actor = identity(
        actor_id="actor-b", decision_id="decision-b", request_id="request-b",
    )
    assert wal.reserve(independent_actor, request()).state == "reserved"
    wal.record_error(first, request(), "local_admission", NOT_SENT)

    other = ProviderDecisionWAL(wal.path)
    for index in range(wal_module.MAX_REQUESTS_PER_OBSERVATION - 1):
        binding = identity(
            decision_id=f"alternative-{index}",
            request_id=f"alternative-request-{index}",
        )
        payload = request(prompt_version=f"selection-alternative-{index}")
        other.reserve(binding, payload)
        other.record_error(binding, payload, "local_admission", NOT_SENT)
    over_budget = identity(
        decision_id="alternative-over", request_id="alternative-request-over",
    )
    with pytest.raises(WALCapacityError):
        other.reserve(over_budget, request(prompt_version="selection-alternative-over"))


def test_terminal_history_is_bounded_and_never_pruned_to_make_room(tmp_path, monkeypatch):
    path, wal = create(tmp_path)
    monkeypatch.setattr(wal_module, "MAX_RECORDS", 1)
    first = identity()
    wal.reserve(first, request())
    wal.record_error(first, request(), "local_admission", NOT_SENT)

    with pytest.raises(WALCapacityError):
        wal.reserve(
            identity(
                session_id="session-b", actor_id="actor-b",
                observation_id="observation-b", decision_id="decision-b",
                request_id="request-b",
            ),
            request(candidate_ids=["different-plan"]),
        )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert len(document["records"]) == 1
    assert document["records"][0]["identity"]["request_id"] == first.request_id


@pytest.mark.skipif(os.name != "posix", reason="POSIX private-file mode check")
def test_world_readable_ledger_fails_closed(tmp_path):
    path, wal = create(tmp_path)
    binding = identity()
    original = request()
    wal.reserve(binding, original)
    path.chmod(0o644)
    with pytest.raises(WALIntegrityError):
        wal.inspect(binding, original)


def test_missing_or_existing_state_is_never_silently_initialized_or_replaced(tmp_path):
    path = tmp_path / "missing.json"
    unopened = ProviderDecisionWAL(path)
    with pytest.raises(WALNotFound):
        unopened.inspect(identity(), request())

    created = ProviderDecisionWAL.initialize(path)
    original_bytes = path.read_bytes()
    with pytest.raises(WALIntegrityError):
        ProviderDecisionWAL.initialize(path)
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "phase,category",
    [
        (NOT_SENT, "rate_limit"),
        (MAY_HAVE_BEEN_SENT, "local_admission"),
        ("response_received", "local_admission"),
        ("made_up_phase", "unknown_outcome"),
    ],
)
def test_error_phase_taxonomy_is_validated_before_mutation(tmp_path, phase, category):
    path, wal = create(tmp_path)
    binding = identity()
    original = request()
    wal.reserve(binding, original)
    with pytest.raises((ValueError, WALInvalidTransition)):
        wal.record_error(binding, original, category, phase)
    assert wal.inspect(binding, original).state == "reserved"
    assert json.loads(path.read_text(encoding="utf-8"))["last_sequence"] == 1
