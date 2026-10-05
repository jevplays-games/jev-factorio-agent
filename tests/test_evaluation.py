import json
from copy import deepcopy

import pytest

from jev_factorio.evaluation import summarize
from attempt_helpers import ReceiptBackend, controller, install_plan


def write(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


@pytest.mark.parametrize("mode", ["immediate", "delayed", "lost_ack", "lost_observation"])
def test_verified_action_count_is_independent_of_acknowledgment(monkeypatch, tmp_path, mode):
    backend = ReceiptBackend(mode)
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    if mode == "lost_observation":
        with pytest.raises(TimeoutError):
            loop.step()
    else:
        loop.step()
    if mode == "delayed":
        backend.publish()
    if mode != "immediate":
        controller(tmp_path, backend, resume=True).step()
    summary = summarize(tmp_path / "run.jsonl")
    assert summary["verified_actions"] == 1 and summary["verified_waits"] == 0
    assert summary["native_victory_event_observed"] is False
    assert summary["evidence_class"] == "synthetic"
    assert summary["unknown_verification_latencies"] == (mode != "immediate")
    assert len(backend.calls) == 1


def test_duplicate_completion_counted_once_and_conflict_rejected(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    record = controller(tmp_path, backend).step()
    path = tmp_path / "duplicate.jsonl"
    write(path, [record, record])
    assert summarize(path)["verified_actions"] == 1
    conflicting = deepcopy(record)
    conflicting["attempt_outcomes"][0]["finished_tick"] += 1
    write(path, [record, conflicting])
    with pytest.raises(ValueError, match="Conflicting"):
        summarize(path)


def test_legacy_logs_do_not_invent_unique_attempts_or_timing(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    record = controller(tmp_path, backend).step()
    record["schema_version"] = 1
    for key in ["attempt", "attempt_outcomes", "phases", "recorded_at_utc", "process_id"]:
        del record[key]
    path = tmp_path / "legacy.jsonl"
    write(path, [record, record])
    summary = summarize(path)
    assert summary["verified_actions"] is None
    assert summary["legacy_records_present"]
    assert summary["identified_attempts"] == 0
    assert summary["legacy_verified_action_records"] == 2
    assert summary["verification_latency_seconds"] == []


def test_mixed_versions_preserve_known_counts_and_signal_incompleteness(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    current = controller(tmp_path, backend).step()
    old = {**current, "schema_version": 1}
    path = tmp_path / "mixed-versions.jsonl"
    write(path, [old, current])
    result = summarize(path)
    assert result["verified_actions"] is None
    assert result["identified_verified_actions"] == 1


def test_existing_stock_is_not_reported_as_new_production(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    record = controller(tmp_path, backend).step()
    record["after_state"]["factory"]["produced"]["iron-plate"] = 110
    path = tmp_path / "production.jsonl"
    write(path, [record])
    assert summarize(path)["observed_production_delta"] == {"iron-plate": 10}
    record["after_state"]["factory"]["produced"]["iron-plate"] = 90
    write(path, [record])
    assert summarize(path)["observed_production_delta"] is None


def test_future_schema_and_missing_v2_fields_fail_closed(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    record = controller(tmp_path, backend).step()
    path = tmp_path / "invalid.jsonl"
    for change in ({"schema_version": 3}, {"schema_version": True}, {"attempt_outcomes": None}):
        write(path, [{**record, **change}])
        with pytest.raises(ValueError):
            summarize(path)


def test_sessions_cannot_be_combined(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    record = controller(tmp_path, backend).step()
    path = tmp_path / "mixed.jsonl"
    write(path, [record, {**record, "session_id": "another-world"}])
    with pytest.raises(ValueError, match="mix"):
        summarize(path)


def test_actual_inflight_background_attempts_are_counted_and_deduplicated(tmp_path):
    from test_background_work import ReceiptBackend as BackgroundReceiptBackend
    from test_background_work import controller as background_controller

    backend = BackgroundReceiptBackend()
    loop = background_controller(backend, tmp_path)
    in_flight = loop.step()
    attempt = deepcopy(in_flight["background_attempt"])
    assert in_flight["schema_version"] == 2
    assert in_flight["background_schema"] == 3
    assert in_flight["attempt"] is None
    assert in_flight["attempt_outcomes"] == []

    schema3_path = tmp_path / "background-schema-3.jsonl"
    write(schema3_path, [in_flight])
    summary = summarize(schema3_path)
    assert summary["identified_attempts"] == 1
    assert summary["verified_actions"] == 0
    assert summary["identified_verified_actions"] == 0

    checkpoint = tmp_path / "state.json"
    legacy_checkpoint = json.loads(checkpoint.read_text())
    legacy_checkpoint["background_schema"] = 2
    legacy_checkpoint.pop("background_step")
    checkpoint.write_text(json.dumps(legacy_checkpoint))
    legacy_loop = background_controller(backend, tmp_path, resume=True)
    schema2_record = legacy_loop.step()
    assert schema2_record["background_schema"] == 2
    assert schema2_record["background_attempt"]["id"] == attempt["id"]

    def attempts_in(rows):
        return [
            item
            for row in rows
            for item in [row.get("attempt"), row.get("background_attempt"),
                         *row.get("attempt_outcomes", [])]
            if item is not None
        ]

    schema2_path = tmp_path / "background-schema-2.jsonl"
    write(schema2_path, [schema2_record])
    schema2_attempts = attempts_in([schema2_record])
    assert summarize(schema2_path)["identified_attempts"] == len({item["id"] for item in schema2_attempts})

    duplicate_path = tmp_path / "background-duplicate.jsonl"
    active_rows = [in_flight, schema2_record]
    write(duplicate_path, active_rows)
    active_attempts = attempts_in(active_rows)
    assert summarize(duplicate_path)["identified_attempts"] == len({item["id"] for item in active_attempts})

    backend.complete()
    completed = legacy_loop.step()
    assert any(outcome["id"] == attempt["id"] for outcome in completed["attempt_outcomes"])
    completion_path = tmp_path / "background-completed.jsonl"
    complete_rows = [*active_rows, completed]
    write(completion_path, complete_rows)
    result = summarize(completion_path)
    complete_attempts = attempts_in(complete_rows)
    verified = {item["id"]: item for item in complete_attempts
                if item.get("outcome") == "verified"}
    from jev_factorio.telemetry import WAIT_ACTIONS
    assert result["identified_attempts"] == len({item["id"] for item in complete_attempts})
    assert result["verified_actions"] == sum(item["action"] not in WAIT_ACTIONS
                                              for item in verified.values())
    assert result["verified_waits"] == sum(item["action"] in WAIT_ACTIONS
                                           for item in verified.values())
    assert attempt["id"] in verified

    foreground_conflict = deepcopy(in_flight)
    foreground_conflict["attempt"] = deepcopy(attempt)
    foreground_conflict["attempt"]["plan_id"] += "-conflict"
    conflict_path = tmp_path / "foreground-background-conflict.jsonl"
    write(conflict_path, [foreground_conflict])
    with pytest.raises(ValueError, match="Conflicting attempt identity"):
        summarize(conflict_path)

    completion_conflict = deepcopy(completed)
    completed_attempt = next(row for row in completion_conflict["attempt_outcomes"]
                             if row["id"] == attempt["id"])
    completed_attempt["plan_id"] += "-conflict"
    conflict_path = tmp_path / "background-completion-conflict.jsonl"
    write(conflict_path, [in_flight, completion_conflict])
    with pytest.raises(ValueError, match="Conflicting attempt identity"):
        summarize(conflict_path)

    unsupported = deepcopy(in_flight)
    unsupported["background_schema"] = 4
    write(conflict_path, [unsupported])
    with pytest.raises(ValueError, match="background attempt schema"):
        summarize(conflict_path)


def _with_produced(record, before, after):
    result = deepcopy(record)
    result["state"]["factory"]["produced"] = deepcopy(before)
    result["after_state"]["factory"]["produced"] = deepcopy(after)
    return result


def _production_record(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    return controller(tmp_path, backend).step()


@pytest.mark.parametrize(("transitions", "expected"), [
    ([(100, 100), (100, 0), (0, 110)], None),
    ([(100, 110), (90, 120)], None),
    ([(100, 110), (112, 120)], {"iron-plate": 20}),
])
def test_production_delta_requires_monotonic_intra_and_inter_record_counters(
        monkeypatch, tmp_path, transitions, expected):
    template = _production_record(monkeypatch, tmp_path)
    records = [_with_produced(template, {"iron-plate": before}, {"iron-plate": after})
               for before, after in transitions]
    path = tmp_path / "production-continuity.jsonl"
    write(path, records)

    assert summarize(path)["observed_production_delta"] == expected


def test_production_delta_preserves_sparse_and_unchanged_counter_semantics(monkeypatch, tmp_path):
    template = _production_record(monkeypatch, tmp_path)
    sparse = [
        _with_produced(template, {"iron-plate": 100}, {"iron-plate": 100, "copper-plate": 2}),
        _with_produced(template, {"iron-plate": 100, "copper-plate": 2},
                       {"iron-plate": 103, "copper-plate": 2}),
    ]
    path = tmp_path / "sparse-production.jsonl"
    write(path, sparse)
    assert summarize(path)["observed_production_delta"] == {
        "iron-plate": 3, "copper-plate": 2,
    }

    unchanged = _with_produced(template, {"iron-plate": 100}, {"iron-plate": 100})
    write(path, [unchanged])
    assert summarize(path)["observed_production_delta"] == {"iron-plate": 0}


def test_missing_intermediate_production_snapshot_makes_delta_unknown(monkeypatch, tmp_path):
    template = _production_record(monkeypatch, tmp_path)
    records = [
        _with_produced(template, {"iron-plate": 100}, {"iron-plate": 100}),
        _with_produced(template, {"iron-plate": 100}, {"iron-plate": 105}),
        _with_produced(template, {"iron-plate": 105}, {"iron-plate": 110}),
    ]
    del records[1]["after_state"]["factory"]["produced"]
    path = tmp_path / "missing-intermediate-production.jsonl"
    write(path, records)

    assert summarize(path)["observed_production_delta"] is None


def test_malformed_intermediate_production_snapshot_is_rejected(monkeypatch, tmp_path):
    template = _production_record(monkeypatch, tmp_path)
    records = [
        _with_produced(template, {"iron-plate": 100}, {"iron-plate": 105}),
        _with_produced(template, {"iron-plate": 110}, {"iron-plate": 115}),
    ]
    records[1]["state"]["factory"]["produced"]["iron-plate"] = True
    path = tmp_path / "malformed-intermediate-production.jsonl"
    write(path, records)

    with pytest.raises(ValueError):
        summarize(path)


@pytest.mark.parametrize("invalid", [True, -1, "7", float("nan"), float("inf"), float("-inf")])
def test_malformed_production_counters_are_rejected(monkeypatch, tmp_path, invalid):
    record = _production_record(monkeypatch, tmp_path)
    record = _with_produced(record, {"iron-plate": 1}, {"iron-plate": invalid})
    path = tmp_path / "malformed-production.jsonl"
    write(path, [record])

    with pytest.raises(ValueError):
        summarize(path)


def test_legacy_log_production_delta_remains_available_without_attempt_ids(monkeypatch, tmp_path):
    record = _production_record(monkeypatch, tmp_path)
    record = _with_produced(record, {"iron-plate": 100}, {"iron-plate": 110})
    record["schema_version"] = 1
    for key in ["attempt", "attempt_outcomes", "phases", "recorded_at_utc", "process_id"]:
        record.pop(key, None)
    path = tmp_path / "legacy-production.jsonl"
    write(path, [record])

    summary = summarize(path)
    assert summary["identified_attempts"] == 0
    assert summary["observed_production_delta"] == {"iron-plate": 10}
