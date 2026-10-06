"""After-state presence must control dashboard freshness and fallback."""
from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from jev_factorio import research_catalog
from jev_factorio.dashboard import DashboardServer, EventWriter, Monitor, SCHEMA, project_record, project_state
from test_research_catalog import DATA_20, raw_catalog

MISSING_RESEARCH = object()


def state(tick: int, research: str) -> dict:
    return {
        "tick": tick,
        "session_id": "session-481",
        "world_kind": "fle",
        "researched": ["automation"],
        "factory": {"research": research, "research_progress": 0.25},
    }


def _http_snapshot(server: DashboardServer) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request("GET", "/api/snapshot")
        response = connection.getresponse()
        assert response.status == 200
        return json.loads(response.read())
    finally:
        connection.close()


def test_project_record_uses_after_state_when_present_and_legacy_state_only_when_absent():
    old = state(100, "old-research")
    legacy = project_record({
        "state": {"tick": 101, "session_id": "session-481"},
        "decision": {"state": {"facts": {"factory": {"research": "legacy-research"}}}},
    })
    assert legacy["state"]["mission"]["research"]["name"] == "legacy-research"

    current = project_record({"state": old, "after_state": state(102, "current-research")})
    assert current["state"]["tick"] == 102
    assert current["state"]["mission"]["research"]["name"] == "current-research"


@pytest.mark.parametrize("after_state", [{}, None, [], "malformed"])
def test_explicit_empty_or_malformed_after_state_clears_instead_of_falling_back(after_state):
    projected = project_record({
        "state": state(100, "stale-research"),
        "after_state": after_state,
        "decision": {"state": {"facts": {"factory": {"research": "stale-decision-research"}}}},
    })
    assert projected["state"] == {}


def test_partial_after_state_is_projected_without_legacy_decision_facts():
    projected = project_record({
        "state": state(100, "stale-research"),
        "after_state": {"tick": 103, "session_id": "session-481", "world_kind": "fle"},
        "decision": {"state": {"facts": {"factory": {"research": "stale-decision-research"}}}},
    })
    assert projected["state"]["tick"] == 103
    assert projected["state"]["mission"]["research"]["name"] is None
    assert "researched" not in projected["state"]


def test_newer_empty_after_state_clears_served_snapshot_and_does_not_leak_secrets(tmp_path):
    path = tmp_path / "dashboard.jsonl"
    catalog = raw_catalog(DATA_20, researched=["electronics", "automation-science-pack"])
    catalog["state"].update({"tick": 5, "current": "automation", "progress": 0.5})
    research_path = path.with_name(research_catalog.FILENAME)
    research_catalog.write(research_path, catalog)
    monitor = Monitor(path, research=research_path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            fresh = state(101, "logistic-science-pack")
            writer.emit("decision_recorded", 7, record=project_record({
                "state": state(100, "automation"),
                "after_state": fresh,
                "action": "observe",
            }))
            monitor.poll()
            first = monitor.snapshot()["view"]
            assert first["state"]["mission"]["research"]["name"] == "logistic-science-pack"
            assert isinstance(first["state_observed_time"], (int, float))
            assert first["research"]["source"] == "telemetry"
            assert first["research"]["current"]["name"] == "logistic-science-pack"
            first_milestone_tick = monitor.research_seen["automation"]

            no_research = state(102, "automation")
            no_research["researched"] = []
            no_research["factory"].update(research=None, research_progress=None)
            writer.emit("decision_recorded", 7, record=project_record({
                "after_state": no_research, "action": "observe",
            }))
            monitor.poll()
            empty_research_view = _http_snapshot(server)["view"]
            assert empty_research_view["state_observed_time"] is not None
            assert empty_research_view["research"]["status"] == "ok"
            assert empty_research_view["research"]["source"] == "telemetry"
            assert empty_research_view["research"]["current"] is None

            incomplete_states = [
                {}, None, [], "malformed",
                {"tick": 102, "session_id": "session-481", "world_kind": "fle"},
                {"tick": 102, "researched": "automation"},
                {"tick": 102, "researched": ["automation", 7]},
                {"tick": 102, "researched": ["rocket-silo", 7]},
                {"researched": []},
            ]
            previous_time = empty_research_view["last_event_time"]
            for incomplete in incomplete_states:
                writer.emit("decision_recorded", 7, record=project_record({
                    "state": fresh,
                    "after_state": incomplete,
                    "action": "observe",
                    "decision": {"Authorization": "Bearer dashboard-481-private-token"},
                }))
                monitor.poll()
                snapshot = _http_snapshot(server)
                view = snapshot["view"]
                assert view["last_event_time"] > previous_time
                previous_time = view["last_event_time"]
                assert snapshot["source"]["invalid"] == 0
                assert view["state_observed_time"] is None
                assert view["research"]["status"] == "observation incomplete"
                assert view["research"]["source"] == "telemetry-history"
                assert view["research"]["current"] is None
                assert view["research"]["version"] == "2.0.77"
                assert view["research"]["tiers"]
                rocket = next(row for row in view["milestones"] if row["key"] == "rocket-silo")
                assert rocket["state"] != "done"
                assert "dashboard-481-private-token" not in json.dumps(snapshot)
                if isinstance(incomplete, dict) and incomplete:
                    assert view["state"].get("tick") == incomplete.get("tick")
                    if "researched" in incomplete:
                        assert view["state"].get("researched") == incomplete["researched"]
                else:
                    assert view["state"] == {}
            # The sidecar remains available for static tree metadata, but its
            # older current/progress cannot fill the explicit observation gap.
            assert view["research"]["source"] != "catalog"
            assert monitor.research_seen["automation"] == first_milestone_tick

        # A new controller run clears the old state projection, but does not
        # turn the old catalog's current topic into a fresh observation.
        with EventWriter(path) as run_reset_writer:
            run_reset_writer.emit("run_started", 2, controller="hierarchical")
            monitor.poll()
            after_run_reset = _http_snapshot(server)["view"]
            assert after_run_reset.get("state_observed_time") is None
            assert after_run_reset["research"]["status"] == "observation incomplete"
            assert after_run_reset["research"]["source"] == "unknown"
            assert after_run_reset["research"]["current"] is None

        # A replacement/truncated event file also cannot revive that sidecar.
        path.unlink()
        with EventWriter(path) as tail_reset_writer:
            tail_reset_writer.emit("run_started", 2, controller="hierarchical")
            monitor.poll()
            after_tail_reset = _http_snapshot(server)["view"]
            assert after_tail_reset.get("state_observed_time") is None
            assert after_tail_reset["research"]["status"] == "observation incomplete"
            assert after_tail_reset["research"]["source"] == "unknown"
            assert after_tail_reset["research"]["current"] is None

            recovered = state(103, "automation")
            tail_reset_writer.emit("decision_recorded", 7, record=project_record({
                "after_state": recovered, "action": "observe",
            }))
            monitor.poll()
            recovered_view = _http_snapshot(server)["view"]
            assert recovered_view["state_observed_time"] is not None
            assert recovered_view["research"]["status"] == "ok"
            assert recovered_view["research"]["source"] == "telemetry"
            assert recovered_view["research"]["current"]["name"] == "automation"
            # A fresh run may reseed historical telemetry milestones.
            assert monitor.research_seen["automation"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize(
    "observed_state",
    [
        None,
        [],
        "malformed",
        {},
        {"tick": 102, "session_id": "session-481", "world_kind": "fle"},
        {"tick": 102, "session_id": "session-481", "world_kind": "fle",
         "researched": ["automation", 7]},
    ],
    ids=["null", "array", "string", "empty-object", "missing-researched", "malformed-researched"],
)
def test_incomplete_raw_observation_clears_freshness_mission_and_catalog_current(tmp_path, observed_state):
    path = tmp_path / "dashboard.jsonl"
    catalog = raw_catalog(DATA_20, researched=["electronics", "automation-science-pack"])
    catalog["state"].update({"tick": 5, "current": "automation", "progress": 0.5})
    research_path = path.with_name(research_catalog.FILENAME)
    research_catalog.write(research_path, catalog)
    monitor = Monitor(path, research=research_path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            complete = state(101, "logistic-science-pack")
            writer.emit("decision_recorded", 7, record=project_record({
                "after_state": complete, "action": "observe",
            }))
            monitor.poll()
            before = _http_snapshot(server)["view"]
            assert before["research"]["current"]["name"] == "logistic-science-pack"
            assert "mission_record" in before
            assert before["state_observed_time"] is not None

            writer.emit("observation", 2, state=observed_state)
            monitor.poll()
            snapshot = _http_snapshot(server)
            view = snapshot["view"]
            assert view["last_event_time"] > before["last_event_time"]
            assert view["state_observed_time"] is None
            assert monitor._research_observation_incomplete is True
            assert view["research"]["status"] == "observation incomplete"
            assert view["research"]["source"] != "catalog"
            assert view["research"]["current"] is None
            assert "mission_record" not in view
            assert "recorded_action" not in view
            assert snapshot["source"]["invalid"] == 0

            recovered = state(103, "automation")
            recovered["researched"] = ["automation"]
            writer.emit("observation", 2, state=project_state(recovered))
            monitor.poll()
            recovered_view = _http_snapshot(server)["view"]
            assert monitor._research_observation_incomplete is False
            assert recovered_view["state_observed_time"] is not None
            assert recovered_view["research"]["status"] == "ok"
            assert recovered_view["research"]["source"] == "telemetry"
            assert recovered_view["research"]["current"]["name"] == "automation"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_raw_producer_mission_projection_keeps_launch_fresh_but_not_catalog_research(tmp_path):
    from test_dashboard_mission import mission_state

    path = tmp_path / "dashboard.jsonl"
    catalog = raw_catalog(DATA_20, researched=["electronics", "automation-science-pack"])
    catalog["state"].update({"tick": 5, "current": "automation", "progress": 0.5})
    research_path = path.with_name(research_catalog.FILENAME)
    research_catalog.write(research_path, catalog)
    monitor = Monitor(path, research=research_path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            complete = state(101, "logistic-science-pack")
            writer.emit("decision_recorded", 7, record=project_record({
                "after_state": complete, "action": "observe",
            }))
            monitor.poll()
            assert "mission_record" in monitor.view

            # attach() emits this bounded mission projection around for_jev();
            # this producer shape has valid launch telemetry but no research list.
            producer_state = project_state(mission_state())
            assert "mission" in producer_state and "researched" not in producer_state
            writer.emit("observation", 2, state=producer_state)
            monitor.poll()

            view = _http_snapshot(server)["view"]
            assert monitor._research_observation_incomplete is True
            assert view["state_observed_time"] is not None
            assert view["state"]["mission"]["launch"]["headline"] == "Launch prerequisites observed"
            assert view["research"]["status"] == "observation incomplete"
            assert view["research"]["source"] != "catalog"
            assert view["research"]["current"] is None
            assert "mission_record" not in view
            assert "recorded_action" not in view
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_legacy_reader_accepts_explicit_incomplete_state_to_clear_prior_record(tmp_path):
    path = tmp_path / "legacy.jsonl"
    first = {
        "session_id": "legacy-481",
        "state": state(100, "old-legacy-research"),
        "action": "observe",
        "recorded_at_utc": "2026-10-06T10:00:00+00:00",
    }
    path.write_text(json.dumps(first) + "\n", encoding="utf-8")
    monitor = Monitor(path, legacy=True)
    monitor.poll()
    assert monitor.view["state"]["mission"]["research"]["name"] == "old-legacy-research"
    assert monitor.view["state_observed_time"] is not None

    second = {
        "session_id": "legacy-481",
        "state": state(100, "old-legacy-research"),
        "after_state": state(101, "current-legacy-research"),
        "action": "observe",
        "recorded_at_utc": "2026-10-06T10:00:30+00:00",
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(second) + "\n")
    monitor.poll()
    assert monitor.view["state"]["tick"] == 101
    assert monitor.view["state"]["mission"]["research"]["name"] == "current-legacy-research"

    third = {
        "session_id": "legacy-481",
        "after_state": None,
        "action": "observe",
        "recorded_at_utc": "2026-10-06T10:01:00+00:00",
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(third) + "\n")
    monitor.poll()

    assert monitor.rejected == 0
    assert monitor.view["state"] == {}
    assert monitor.view["state_observed_time"] is None
    assert monitor.view["last_event_time"] > 0


def test_future_incomplete_event_cannot_make_old_observation_fresh():
    monitor = Monitor("unused-events")
    now = time.time()
    monitor.accept({
        "schema": SCHEMA, "run_id": "future-481", "seq": 1, "stage": 7,
        "time": now - 3600, "kind": "decision_recorded",
        "data": {"record": project_record({"after_state": state(100, "stale-research")})},
    })
    assert monitor.view["state_observed_time"] == now - 3600

    future = now + 3600
    monitor.accept({
        "schema": SCHEMA, "run_id": "future-481", "seq": 2, "stage": 7,
        "time": future, "kind": "decision_recorded",
        "data": {"record": project_record({"state": state(100, "stale-research"), "after_state": []})},
    })
    assert monitor.view["last_event_time"] == future
    assert monitor.view["state"] == {}
    assert monitor.view["state_observed_time"] is None


@pytest.mark.parametrize("kind", ["decision_recorded", "observation"])
@pytest.mark.parametrize(
    "bad_researched",
    [
        ["rocket-silo", 7],
        ["rocket-silo", "bad name"],
        ["rocket-silo"] * (research_catalog.MAX_TECHNOLOGIES + 1),
    ],
    ids=["mixed-type", "invalid-name", "over-limit"],
)
def test_incoherent_researched_list_never_creates_new_milestone_history(kind, bad_researched):
    monitor = Monitor("unused-events")
    run_id = f"malformed-history-{kind}"
    valid_before = {"tick": 101, "session_id": "session-481", "world_kind": "fle",
                    "researched": ["automation"]}

    def send(seq, event_kind, value):
        if event_kind == "decision_recorded":
            data = {"record": value}
        else:
            data = {"state": value}
        monitor.accept({
            "schema": SCHEMA,
            "run_id": run_id,
            "seq": seq,
            "stage": 7,
            "time": 2000 + seq,
            "kind": event_kind,
            "data": data,
        })

    before_record = project_record({"after_state": valid_before, "action": "observe"})
    send(1, "decision_recorded", before_record)
    assert monitor.research_seen == {"automation": None}

    invalid_state = {"tick": 102, "session_id": "session-481", "world_kind": "fle",
                     "researched": bad_researched}
    invalid_record = project_record({"state": valid_before, "after_state": invalid_state, "action": "observe"})
    send(2, kind, invalid_record if kind == "decision_recorded" else invalid_state)

    assert monitor._research_observation_incomplete is True
    assert monitor.view["state_observed_time"] is None
    assert monitor.research_seen == {"automation": None}
    rocket = next(row for row in monitor.snapshot()["view"]["milestones"] if row["key"] == "rocket-silo")
    assert rocket["state"] != "done"

    recovered = {"tick": 103, "session_id": "session-481", "world_kind": "fle",
                 "researched": ["automation", "rocket-silo"]}
    recovered_record = project_record({"after_state": recovered, "action": "observe"})
    send(3, kind, recovered_record if kind == "decision_recorded" else recovered)

    assert monitor._research_observation_incomplete is False
    assert monitor.view["state_observed_time"] == 2003
    assert monitor.research_seen == {"automation": None, "rocket-silo": 103}
    rocket = next(row for row in monitor.snapshot()["view"]["milestones"] if row["key"] == "rocket-silo")
    assert rocket["state"] == "done"
    assert rocket["tick"] == 103


def test_raw_launch_projection_is_bound_to_current_observation_identity(tmp_path):
    """A prior launch display cannot gain freshness from a new outer record."""
    from test_dashboard_mission import mission_state

    path = tmp_path / "dashboard.jsonl"
    catalog = raw_catalog(DATA_20, researched=["electronics", "automation-science-pack"])
    catalog["state"].update({"tick": 5, "current": "automation", "progress": 0.5})
    research_path = path.with_name(research_catalog.FILENAME)
    research_catalog.write(research_path, catalog)
    monitor = Monitor(path, research=research_path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            producer_state = project_state(mission_state())
            assert "researched" not in producer_state
            writer.emit("observation", 2, state=producer_state)
            monitor.poll()
            valid = _http_snapshot(server)["view"]
            assert valid["state_observed_time"] is not None
            assert valid["state"]["mission"]["launch"]["evidence_valid"] is True
            assert valid["research"]["status"] == "observation incomplete"
            assert valid["research"]["source"] != "catalog"
            assert valid["research"]["current"] is None

            cases = []
            for name in ("missing_outer_tick", "changed_outer_tick", "missing_outer_session",
                         "changed_outer_session", "missing_launch", "empty_launch",
                         "changed_launch_tick", "changed_launch_session", "bad_launch_schema",
                         "bad_launch_gates", "bad_gate_state", "bad_evidence_flag",
                         "bad_mission_schema", "bad_research_shape", "huge_research_progress",
                         "bad_automation_family", "oversized_outer_session"):
                bad = project_state(mission_state())
                if name == "missing_outer_tick":
                    bad.pop("tick")
                elif name == "changed_outer_tick":
                    bad["tick"] += 1
                elif name == "missing_outer_session":
                    bad.pop("session_id")
                elif name == "changed_outer_session":
                    bad["session_id"] = "other-session"
                elif name == "missing_launch":
                    bad["mission"].pop("launch")
                elif name == "empty_launch":
                    bad["mission"]["launch"] = {}
                elif name == "changed_launch_tick":
                    bad["mission"]["launch"]["tick"] += 1
                elif name == "changed_launch_session":
                    bad["mission"]["launch"]["session_id"] = "other-session"
                elif name == "bad_launch_schema":
                    bad["mission"]["launch"]["schema"] = True
                elif name == "bad_launch_gates":
                    bad["mission"]["launch"]["gates"] = []
                elif name == "bad_gate_state":
                    bad["mission"]["launch"]["gates"][0]["state"] = []
                elif name == "bad_evidence_flag":
                    bad["mission"]["launch"]["evidence_valid"] = "true"
                elif name == "bad_mission_schema":
                    bad["mission"]["schema"] = True
                elif name == "bad_research_shape":
                    bad["mission"]["research"] = []
                elif name == "huge_research_progress":
                    bad["mission"]["research"]["progress"] = 10 ** 1000
                elif name == "bad_automation_family":
                    bad["mission"]["automation"] = [{"family": []}]
                elif name == "oversized_outer_session":
                    bad["session_id"] = "s" * 161

                writer.emit("observation", 2, state=bad)
                monitor.poll()
                view = _http_snapshot(server)["view"]
                cases.append({
                    "case": name,
                    "fresh": view["state_observed_time"] is not None,
                    "mission_visible": "mission" in view["state"],
                    "research_status": view["research"]["status"],
                    "research_not_catalog": view["research"]["source"] != "catalog",
                    "research_current": view["research"]["current"],
                })

            assert cases == [
                {"case": row["case"], "fresh": False, "mission_visible": False,
                 "research_status": "observation incomplete", "research_not_catalog": True,
                 "research_current": None}
                for row in cases
            ], cases

            for changed_field in ("tick", "session_id"):
                with_research = project_state(mission_state())
                with_research["researched"] = ["automation"]
                if changed_field == "tick":
                    with_research["tick"] += 1
                else:
                    with_research["session_id"] = "other-session"
                writer.emit("observation", 2, state=with_research)
                monitor.poll()
                view = _http_snapshot(server)["view"]
                # The research list independently supplies observation freshness,
                # but it cannot validate the copied launch projection.
                assert view["state_observed_time"] is not None
                assert "mission" not in view["state"]
                assert view["research"]["source"] != "catalog"
                assert view["research"]["current"] is None

            writer.emit("observation", 2, state=project_state(mission_state()))
            monitor.poll()
            recovered = _http_snapshot(server)["view"]
            assert recovered["state_observed_time"] is not None
            assert recovered["state"]["mission"]["launch"]["evidence_valid"] is True
            assert recovered["research"]["status"] == "observation incomplete"
            assert recovered["research"]["current"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _mission_state_with_automation(counts):
    from test_dashboard_mission import mission_state

    result = mission_state()
    for family, count in zip(("input_routes", "production_sites", "successors"), counts):
        result["factory"][family] = {
            "sources": {f"role-{index}": {"phase": "ready"} for index in range(count)}
        }
    return result


@pytest.mark.parametrize(
    ("counts", "projected_total"),
    [
        ((0, 0, 0), 0),
        ((6, 0, 0), 6),
        ((6, 1, 0), 7),
        ((6, 6, 0), 12),
        ((6, 6, 6), 18),
    ],
    ids=["empty", "one-family-six", "seven-across-families", "twelve-across-families", "eighteen-across-families"],
)
def test_actual_producer_automation_family_bounds_remain_fresh(tmp_path, counts, projected_total):
    """The producer caps each of three families at six, not the aggregate at six."""
    source = _mission_state_with_automation(counts)
    producer_state = project_state(source)
    families = ("input_routes", "production_sites", "successors")
    family_counts = {
        family: sum(row["family"] == family for row in producer_state["mission"]["automation"])
        for family in families
    }
    assert len(producer_state["mission"]["automation"]) == projected_total
    assert family_counts == dict(zip(families, (min(count, 6) for count in counts)))

    path = tmp_path / "dashboard.jsonl"
    monitor = Monitor(path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            writer.emit("observation", 2, state=producer_state)
            monitor.poll()
            view = _http_snapshot(server)["view"]
            assert isinstance(view["state"].get("mission"), dict)
            assert view["state_observed_time"] is not None
            assert len(view["state"]["mission"]["automation"]) == projected_total
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_actual_producer_clips_each_automation_family_and_deduplicates_shared_keys(tmp_path):
    """Repeated source/diagnostic keys are merged before the display projection."""
    source = _mission_state_with_automation((9, 9, 9))
    source["factory"]["input_routes"] = {
        "sources": {f"role-{index}": {"phase": "ready"} for index in range(9)},
        "diagnostics": {"role-0": {"reason": "shared key", "survey_tick": 1000}},
    }
    producer_state = project_state(source)
    family_counts = {
        family: sum(row["family"] == family for row in producer_state["mission"]["automation"])
        for family in ("input_routes", "production_sites", "successors")
    }
    assert family_counts == {"input_routes": 6, "production_sites": 6, "successors": 6}
    input_rows = [row for row in producer_state["mission"]["automation"] if row["family"] == "input_routes"]
    assert len(input_rows) == 6
    assert sum(row["role"] == "role-0" for row in input_rows) == 1
    assert next(row for row in input_rows if row["role"] == "role-0")["reason"] == "shared key"

    path = tmp_path / "dashboard.jsonl"
    monitor = Monitor(path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            writer.emit("observation", 2, state=producer_state)
            monitor.poll()
            view = _http_snapshot(server)["view"]
            assert isinstance(view["state"].get("mission"), dict)
            assert view["state_observed_time"] is not None
            assert len(view["state"]["mission"]["automation"]) == 18
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_distinct_role_keys_with_same_truncated_display_label_remain_valid(tmp_path):
    """The public label is lossy; distinct producer keys can display identically."""
    prefix = "r" * 160
    source = _mission_state_with_automation((0, 0, 0))
    source["factory"]["input_routes"] = {"sources": {
        prefix + "-source-A": {"phase": "ready"},
        prefix + "-source-B": {"phase": "blocked"},
    }}
    producer_state = project_state(source)
    rows = producer_state["mission"]["automation"]
    assert len(rows) == 2
    assert rows[0]["role"] == rows[1]["role"] == prefix
    assert rows[0]["state"] != rows[1]["state"]

    path = tmp_path / "dashboard.jsonl"
    monitor = Monitor(path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            writer.emit("observation", 2, state=producer_state)
            monitor.poll()
            view = _http_snapshot(server)["view"]
            accepted = view["state"].get("mission")
            assert isinstance(accepted, dict)
            assert view["state_observed_time"] is not None
            assert accepted["automation"] == rows
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_single_family_automation_overflow_is_rejected_and_later_valid_state_recovers(tmp_path):
    """A family over six is invalid even when the aggregate limit is eighteen."""
    from test_dashboard_mission import mission_state

    bad = project_state(_mission_state_with_automation((6, 0, 0)))
    bad["mission"]["automation"].append({
        "family": "input_routes", "role": "role-overflow", "state": "ready",
        "reason": None, "survey_tick": None, "cached": None,
    })
    assert len(bad["mission"]["automation"]) == 7

    path = tmp_path / "dashboard.jsonl"
    monitor = Monitor(path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            writer.emit("observation", 2, state=bad)
            monitor.poll()
            rejected = _http_snapshot(server)["view"]
            assert "mission" not in rejected["state"]
            assert rejected["state_observed_time"] is None

            recovered_state = project_state(mission_state())
            writer.emit("observation", 2, state=recovered_state)
            monitor.poll()
            recovered = _http_snapshot(server)["view"]
            assert isinstance(recovered["state"].get("mission"), dict)
            assert recovered["state_observed_time"] is not None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize(
    "researched",
    [MISSING_RESEARCH, None, "automation", ["automation", 7]],
    ids=["missing", "explicit-null", "wrong-type", "mixed-list"],
)
def test_unmarked_base659_partial_projected_record_cannot_revive_catalog_research(tmp_path, researched):
    """Older persisted decision records have no V6 incomplete-observation hint."""
    path = tmp_path / "dashboard.jsonl"
    catalog = raw_catalog(DATA_20, researched=["electronics", "automation-science-pack"])
    catalog["state"].update({"tick": 5, "current": "automation", "progress": 0.5})
    research_path = path.with_name(research_catalog.FILENAME)
    research_catalog.write(research_path, catalog)
    monitor = Monitor(path, research=research_path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            complete = state(100, "logistic-science-pack")
            writer.emit("decision_recorded", 7, record=project_record({
                "tick": 100, "session_id": "session-481", "state": complete,
                "after_state": complete, "action": "observe",
            }))
            monitor.poll()
            before = _http_snapshot(server)["view"]
            assert before["research"]["current"]["name"] == "logistic-science-pack"
            assert before["state_observed_time"] is not None

            # The old base-659 serializer produced this projected shape before
            # V6 added its private incomplete-observation hint. Keep it as an
            # on-disk compatibility record rather than rebuilding raw data.
            partial = {
                "tick": 101, "session_id": "session-481", "world_kind": "mock",
            }
            if researched is not MISSING_RESEARCH:
                partial["researched"] = researched
            old_record = project_record({
                "tick": 100, "session_id": "session-481", "world_kind": "mock",
                "state": complete, "after_state": partial, "action": "observe",
                "outcome": "explicit incomplete producer observation",
            })
            old_record.pop("_dashboard_research_observation_incomplete", None)
            if researched is MISSING_RESEARCH:
                assert "researched" not in old_record["state"]
            else:
                assert old_record["state"]["researched"] == researched
            assert old_record["state"]["tick"] == 101
            assert "_dashboard_research_observation_incomplete" not in old_record

            writer.emit("decision_recorded", 7, record=old_record)
            monitor.poll()
            after = _http_snapshot(server)["view"]
            assert after["last_event_time"] > before["last_event_time"]
            assert after["state_observed_time"] is None
            assert after["research"]["status"] == "observation incomplete"
            assert after["research"]["source"] == "telemetry-history"
            assert after["research"]["current"] is None
            if researched is MISSING_RESEARCH:
                assert "researched" not in after["state"]
            else:
                assert after["state"]["researched"] == researched
            assert monitor.research_seen == {"automation": None}

            recovered = state(102, "automation")
            recovered["researched"] = ["automation"]
            writer.emit("decision_recorded", 7, record=project_record({
                "tick": 102, "session_id": "session-481", "after_state": recovered,
                "action": "observe",
            }))
            monitor.poll()
            final = _http_snapshot(server)["view"]
            assert final["state_observed_time"] is not None
            assert final["research"]["status"] == "ok"
            assert final["research"]["source"] == "telemetry"
            assert final["research"]["current"]["name"] == "automation"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_legacy_absent_after_state_decision_facts_keep_the_documented_fallback(tmp_path):
    path = tmp_path / "legacy.jsonl"
    path.write_text(json.dumps({
        "session_id": "legacy-481",
        "state": {"tick": 99, "session_id": "legacy-481", "world_kind": "mock"},
        "decision": {"state": {"facts": {"factory": {
            "research": "legacy-research", "research_progress": 0.25,
        }}}},
        "action": "observe",
        "recorded_at_utc": "2026-10-06T10:00:00+00:00",
    }) + "\n", encoding="utf-8")

    monitor = Monitor(path, legacy=True)
    monitor.poll()

    assert monitor.rejected == 0
    assert monitor.view["state_observed_time"] is not None
    assert monitor.view["state"]["mission"]["research"]["name"] == "legacy-research"
    assert monitor._research_observation_incomplete is False


def test_legacy_absent_after_state_does_not_hide_explicit_malformed_research(tmp_path):
    path = tmp_path / "legacy-malformed.jsonl"
    catalog = raw_catalog(DATA_20, researched=["electronics", "automation-science-pack"])
    catalog["state"].update({"tick": 5, "current": "automation", "progress": 0.5})
    research_path = path.with_name(research_catalog.FILENAME)
    research_catalog.write(research_path, catalog)
    path.write_text(json.dumps({
        "session_id": "legacy-481",
        "state": {
            "tick": 99, "session_id": "legacy-481", "world_kind": "mock",
            "researched": None,
        },
        "decision": {"state": {"facts": {"factory": {
            "research": "legacy-research", "research_progress": 0.25,
        }}}},
        "action": "observe",
        "recorded_at_utc": "2026-10-06T10:00:00+00:00",
    }) + "\n", encoding="utf-8")

    monitor = Monitor(path, research=research_path, legacy=True)
    monitor.poll()

    assert monitor.rejected == 0
    assert monitor._research_observation_incomplete is True
    assert monitor.view["state_observed_time"] is None
    research = monitor.snapshot()["view"]["research"]
    assert research["status"] == "observation incomplete"
    assert research["source"] != "catalog"
    assert research["current"] is None
