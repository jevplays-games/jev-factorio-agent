"""Focused regressions for the PR25 dashboard evidence follow-ups."""
from __future__ import annotations

import http.client
import json
import threading

import pytest

from jev_factorio.dashboard import DashboardServer, EventWriter, Monitor


def _http_response(server: DashboardServer, headers: dict[str, str] | list[tuple[str, str]],
                   path: str = "/api/snapshot"):
    # The bound socket stays ephemeral; tests set only the request policy's logical port.
    connection = http.client.HTTPConnection("127.0.0.1", server.socket.getsockname()[1], timeout=5)
    try:
        connection.putrequest("GET", path, skip_host=True)
        entries = headers.items() if isinstance(headers, dict) else headers
        for name, value in entries:
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _serve(monitor: Monitor, logical_port: int):
    server = DashboardServer(0, monitor)
    server.server_port = logical_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_monitor_keeps_dispatch_event_separate_from_record_action(tmp_path):
    path = tmp_path / "events.jsonl"
    parameters = {"role": "utility:lab", "item": "automation-science-pack", "count": 2}
    with EventWriter(path) as writer:
        writer.emit("run_started", 2, controller="hierarchical")
        writer.emit("action", 6, action="factory_insert", parameters=parameters)
        writer.emit("decision_recorded", 7, record={"action": "verify", "verified": True})
        writer.emit("decision_recorded", 7, record={"action": "observe", "verified": False})

    monitor = Monitor(path)
    monitor.poll()
    view = monitor.snapshot()["view"]
    action_event = json.loads(path.read_text(encoding="utf-8").splitlines()[1])

    assert view["action"] == "observe"  # controller record label for stage progression
    assert view["recorded_action"] == "observe"
    assert view["dispatch"] == {
        "observed": True,
        "action": "factory_insert",
        "parameters": parameters,
        "event_seq": 2,
        "event_time": action_event["time"],
    }


def test_monitor_clears_old_parameters_on_an_unparameterized_dispatch(tmp_path):
    path = tmp_path / "events.jsonl"
    with EventWriter(path) as writer:
        writer.emit("run_started", 2, controller="hierarchical")
        writer.emit("action", 6, action="factory_insert",
                    parameters={"role": "utility:lab", "count": 2})
        writer.emit("action", 6, action="walk_to_coal")
        writer.emit("decision_recorded", 7, record={"action": "verify", "verified": False})

    monitor = Monitor(path)
    monitor.poll()
    view = monitor.snapshot()["view"]
    assert view["action"] == "verify"
    assert view["dispatch"]["observed"] is True
    assert view["dispatch"]["action"] == "walk_to_coal"
    assert view["dispatch"]["parameters"] is None
    assert view["dispatch"]["event_seq"] == 3


def test_monitor_does_not_infer_dispatch_from_legacy_record(tmp_path):
    path = tmp_path / "legacy.jsonl"
    path.write_text(json.dumps({
        "state": {"session_id": "legacy-session", "world_kind": "mock", "tick": 17},
        "action": "mine_coal", "parameters": {"amount": 4},
        "verified": True, "outcome": "completed",
    }) + "\n", encoding="utf-8")

    monitor = Monitor(path, legacy=True)
    monitor.poll()
    view = monitor.snapshot()["view"]
    assert view["action"] == "mine_coal"
    assert "dispatch" not in view


def test_monitor_clears_dispatch_at_a_new_run_boundary(tmp_path):
    path = tmp_path / "events.jsonl"
    with EventWriter(path) as writer:
        writer.emit("run_started", 2, controller="hierarchical")
        writer.emit("action", 6, action="factory_insert", parameters={"count": 2})
    with EventWriter(path) as writer:
        writer.emit("run_started", 2, controller="hierarchical")
        writer.emit("decision_recorded", 7, record={"action": "observe"})

    monitor = Monitor(path)
    monitor.poll()
    view = monitor.snapshot()["view"]
    assert view["action"] == "observe"
    assert "dispatch" not in view


@pytest.mark.parametrize("host,origin", [
    ("127.0.0.1", None),
    ("localhost", None),
    ("127.0.0.1", "http://127.0.0.1"),
    ("127.0.0.1", "http://127.0.0.1:80"),
    ("127.0.0.1:80", "http://127.0.0.1"),
    ("127.0.0.1:80", "http://127.0.0.1:80"),
    ("localhost", "http://localhost:80"),
    ("localhost:80", "http://localhost"),
])
def test_http_port80_accepts_only_equivalent_loopback_hosts(tmp_path, host, origin):
    server, thread = _serve(Monitor(tmp_path / "events.jsonl"), logical_port=80)
    headers = {"Host": host}
    if origin is not None:
        headers["Origin"] = origin
    try:
        status, body = _http_response(server, headers)
        assert status == 200
        assert json.loads(body)["source"]["status"] == "waiting"
        status, _ = _http_response(server, headers, path="/not-a-route")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("headers", [
    {},
    {"Host": ""},
    {"Host": "evil.example"},
    {"Host": "127.0.0.1:80", "Origin": ""},
    {"Host": "127.0.0.1:80", "Origin": "null"},
    {"Host": "127.0.0.1:80", "Origin": "http://evil.example"},
    {"Host": "127.0.0.1:80", "Origin": "http://127.0.0.1:81"},
    {"Host": "127.0.0.1:80", "Origin": "http://user@127.0.0.1"},
    {"Host": "127.0.0.1:80", "Origin": "http://127.0.0.1/path"},
    {"Host": "127.0.0.1:80", "Sec-Fetch-Site": "cross-site"},
])
def test_http_port80_rejects_empty_malformed_and_foreign_headers(tmp_path, headers):
    server, thread = _serve(Monitor(tmp_path / "events.jsonl"), logical_port=80)
    try:
        status, _ = _http_response(server, headers)
        assert status == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("headers", [
    [("Host", "localhost"), ("Host", "localhost")],
    [("Host", "localhost"), ("Host", "evil.example")],
    [("Host", "evil.example"), ("Host", "localhost")],
])
def test_http_port80_rejects_duplicate_host_headers(tmp_path, headers):
    server, thread = _serve(Monitor(tmp_path / "events.jsonl"), logical_port=80)
    try:
        assert _http_response(server, headers)[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("site_values", [
    ("same-origin", "same-origin"),
    ("same-origin", "cross-site"),
    ("cross-site", "same-origin"),
])
def test_http_rejects_ambiguous_duplicate_fetch_site_headers(tmp_path, site_values):
    server, thread = _serve(Monitor(tmp_path / "events.jsonl"), logical_port=80)
    try:
        headers = [("Host", "127.0.0.1:80"),
                   *(("Sec-Fetch-Site", value) for value in site_values)]
        assert _http_response(server, headers)[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_http_nondefault_port_still_requires_exact_host_and_origin(tmp_path):
    server, thread = _serve(Monitor(tmp_path / "events.jsonl"), logical_port=8765)
    try:
        assert _http_response(server, {"Host": "127.0.0.1:8765"})[0] == 200
        assert _http_response(server, {"Host": "127.0.0.1"})[0] == 403
        assert _http_response(server, {
            "Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1",
        })[0] == 403
        assert _http_response(server, {
            "Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765",
        })[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
