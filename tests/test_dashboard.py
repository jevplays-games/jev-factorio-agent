"""Offline reader, observer transport and local HTTP security tests."""
import http.client
import json
import math
import os
import threading
import time
from pathlib import Path

import pytest

from jev_factorio.dashboard import (
    ASSETS, MAX_LINE, SCHEMA, DashboardServer, EventWriter, Monitor, Tail, attach, sanitize,
)


def event(seq=1, kind="run_started", stage=2, run="test-run", **data):
    return {"schema": SCHEMA, "run_id": run, "seq": seq, "kind": kind,
            "stage": stage, "time": time.time(), "at": "2026-09-21T12:00:00+00:00", "data": data}


def write(path, *rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_writer_redacts_detaches_and_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "private-test-credential")
    path = tmp_path / "events.jsonl"
    source = {"api_key": "should not leak", "message": "private-test-credential https://example.test/key",
              "input_tokens": 42, "nested": [1, 2], "invalid": math.nan}
    with EventWriter(path) as writer:
        writer.emit("run_started", 2, **source)
        first_id = writer.run_id
    assert source["nested"] == [1, 2] and math.isnan(source["invalid"])
    with EventWriter(path) as writer:
        writer.emit("run_started", 2)
        assert writer.run_id != first_id
    raw = path.read_text()
    assert "private-test-credential" not in raw and "should not leak" not in raw
    assert "https://example" not in raw
    rows = [json.loads(line) for line in raw.splitlines()]
    assert [row["seq"] for row in rows] == [1, 2, 1, 2]
    assert rows[0]["data"]["input_tokens"] == 42
    assert rows[0]["data"]["invalid"] is None


def test_writer_rejects_input_alias_unrelated_file_and_incomplete_tail(tmp_path):
    source = tmp_path / "checkpoint.json"
    source.write_text('{"session_id": "original"}')
    original = source.read_bytes()
    with pytest.raises(ValueError):
        EventWriter(source, forbidden=(source,))
    with pytest.raises(ValueError):
        EventWriter(source)
    alias = tmp_path / "alias.jsonl"
    os.link(source, alias)
    with pytest.raises(ValueError):
        EventWriter(alias, forbidden=(source,))
    assert source.read_bytes() == original
    write(alias := tmp_path / "torn.jsonl", event())
    with alias.open("ab") as stream:
        stream.write(b'{"torn":')
    with pytest.raises(ValueError):
        EventWriter(alias)


def test_writer_failure_is_best_effort_and_warns_once(tmp_path, monkeypatch, capsys):
    with EventWriter(tmp_path / "events") as writer:
        def fail(*args):
            raise OSError("SECRET ERROR TEXT")
        monkeypatch.setattr(os, "write", fail)
        writer.emit("one", 2)
        writer.emit("two", 2)
        assert writer.disabled
    output = capsys.readouterr().err
    assert output.count("disabled") == 1
    assert "SECRET" not in output


def test_sanitize_bounded_depth_bytes_and_secret_keys():
    nested = {}
    current = nested
    for _ in range(100):
        current["next"] = {}
        current = current["next"]
    out = sanitize({"deep": nested, "wide": list(range(10000)), "text": "x" * 10000,
                    "Authorization": "Bearer ABCDEF", "value": float("inf")})
    assert len(out["wide"]) == 129
    assert len(out["text"]) < 2100
    assert out["Authorization"] == "[redacted]"
    assert out["value"] is None
    assert "display limit" in json.dumps(out)


def test_dashboard_observer_preserves_only_known_mock_async_identity(tmp_path):
    from jev_factorio.backends.mock import MockBackend
    from jev_factorio.controller import AsyncControllerBusy, HierarchicalLoop
    from jev_factorio.jev_client import AsyncMockJevClient

    backend = MockBackend()
    loop = HierarchicalLoop(
        backend, jev=AsyncMockJevClient(), target="bootstrap_mining", policy="jev",
        checkpoint=str(tmp_path / "dashboard-mock-identity.json"), tick_seconds=0,
        async_decisions=True)
    with EventWriter(tmp_path / "dashboard-mock-identity.jsonl") as writer:
        attach(loop, writer)
        assert loop.backend.session_id == backend.session_id
        assert loop.backend.actor_unit == 0
        assert loop.backend._shared is backend
        keys = loop._async_resource_lock_keys()
    assert keys == (("session", backend.session_id), ("transport", f"mock:{id(backend):x}"))

    class UnknownFacade:
        def __init__(self):
            self.inner = MockBackend()
            self.session_id = self.inner.session_id

        def __getattr__(self, key):
            return getattr(self.inner, key)

    unknown = UnknownFacade()
    unknown_loop = HierarchicalLoop(
        unknown, jev=AsyncMockJevClient(), target="bootstrap_mining", policy="jev",
        checkpoint=str(tmp_path / "dashboard-unknown-identity.json"), tick_seconds=0,
        async_decisions=True)
    with EventWriter(tmp_path / "dashboard-unknown-identity.jsonl") as writer:
        attach(unknown_loop, writer)
        assert not hasattr(unknown_loop.backend, "actor_unit")
        assert not hasattr(unknown_loop.backend, "_shared")
        with pytest.raises(AsyncControllerBusy, match="stable session and actor identity"):
            unknown_loop._async_resource_lock_keys()


def test_dashboard_observer_forwards_qualified_native_attachment_and_rcon_identity(tmp_path):
    from types import SimpleNamespace

    from jev_factorio.backends.mock import MockBackend
    from jev_factorio.controller import HierarchicalLoop
    from jev_factorio.jev_client import AsyncMockJevClient

    class IdentifiedFleFixture:
        def __init__(self):
            self.inner = MockBackend()
            self.session_id = self.inner.session_id
            self.actor_unit = 41
            self.surface_index = 2
            self.force_index = 3
            self.rcon = object()
            self._instance = SimpleNamespace(
                rcon_client=SimpleNamespace(client=self.rcon))
            self._native_attachment = {
                "qualified": True, "session_id": self.session_id,
                "actor_unit": self.actor_unit,
            }

        def __getattr__(self, key):
            return getattr(self.inner, key)

        def observe(self):
            snapshot = self.inner.observe()
            snapshot.world_kind = "fle"
            snapshot.factory["acceptance_runtime"] = {
                "session_id": self.session_id, "actor_unit": self.actor_unit,
                "surface_index": self.surface_index, "force_index": self.force_index,
            }
            return snapshot

    backend = IdentifiedFleFixture()
    loop = HierarchicalLoop(
        backend, jev=AsyncMockJevClient(), target="bootstrap_mining", policy="jev",
        checkpoint=str(tmp_path / "dashboard-fle-identity.json"), tick_seconds=0,
        async_decisions=True)
    with EventWriter(tmp_path / "dashboard-fle-identity.jsonl") as writer:
        attach(loop, writer)
        identity = loop._async_observation_identity(loop.backend.observe())
        keys = loop._async_resource_lock_keys()
    assert identity == {
        "session_id": backend.session_id, "actor_unit": 41,
        "surface_index": 2, "force_index": 3,
    }
    assert keys == (("session", backend.session_id), ("transport", f"rcon:{id(backend.rcon):x}"))


def test_tail_partial_invalid_truncation_and_rotation(tmp_path):
    path = tmp_path / "events"
    tail = Tail(path)
    assert tail.poll() == [] and tail.status == "waiting"
    path.write_bytes(b'{"a":')
    assert tail.poll() == [] and tail.pending
    with path.open("ab") as stream:
        stream.write(b'1}\nnot-json\n[1,2]\n{"b":2}\n')
    assert tail.poll() == [{"a": 1}, {"b": 2}]
    assert tail.invalid == 2
    path.write_bytes(b'{"c":3}\n')
    assert tail.poll() == [{"c": 3}] and tail.reset
    path.rename(tmp_path / "old")
    write(path, {"d": 4})
    assert tail.poll() == [{"d": 4}] and tail.reset
    path.unlink()
    assert tail.poll() == [] and tail.status == "unavailable"


def test_tail_bounds_oversized_line_and_resumes(tmp_path):
    path = tmp_path / "events"
    path.write_bytes(b'x' * (MAX_LINE * 4) + b'\n{"ok":true}\n')
    tail = Tail(path)
    received = []
    for _ in range(8):
        received.extend(tail.poll())
        assert len(tail.pending) <= MAX_LINE
    assert received == [{"ok": True}]
    assert tail.invalid > 0


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO test")
def test_fifo_does_not_block_reader(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    assert Tail(path).poll() == []


def test_model_in_flight_return_not_verification_and_restart(tmp_path):
    monitor = Monitor(tmp_path / "events")
    monitor.accept(event())
    monitor.accept(event(2, "model_started", 5))
    assert monitor.view["model_busy"] is True
    monitor.accept(event(3, "model_returned", 5, duration_ms=70))
    monitor.accept(event(4, "dispatch_returned", 6))
    assert not monitor.view["model_busy"]
    assert monitor.view.get("verified") is not True
    monitor.accept(event(5, "decision_recorded", 7, record={"verified": False, "pending": {"dispatch": "ambiguous"}}))
    assert monitor.view["pending"]["dispatch"] == "ambiguous"
    monitor.accept(event(6, "decision_recorded", 7, record={"verified": True, "pending": None}))
    assert monitor.view["verified"] is True
    monitor.accept(event(run="new-run"))
    assert monitor.view.get("verified") is None
    assert len(monitor.events) == 1


def test_reducer_rejects_bad_envelopes_gaps_duplicates_and_top_level_override(tmp_path):
    monitor = Monitor(tmp_path / "events")
    for invalid in [{}, event(stage=9), event(seq=True), {**event(), "time": math.nan}, {**event(), "data": []}]:
        monitor.accept(invalid)
    assert monitor.rejected == 5
    monitor.accept(event(2))
    assert monitor.view["gap"]
    monitor.accept(event(2))
    assert monitor.rejected == 6
    monitor.accept(event(3, "controller_state", 2, seen="bad", run_id="injected", last_event_time=-1))
    assert monitor.view["run_id"] == "test-run"
    assert isinstance(monitor.view["seen"], list)


@pytest.mark.parametrize('phase', ['waiting_for_changed_game_evidence',
    'evaluating_changed_game_evidence', 'evaluation_outcome_unknown_waiting'])
@pytest.mark.parametrize('kind,stage', [('model_started', 5), ('action', 6)])
def test_fresh_work_supersedes_recovery_caption_without_rewriting_history(tmp_path, phase, kind, stage):
    monitor = Monitor(tmp_path / 'events')
    monitor.accept(event())
    old = event(2, 'decision_recorded', 7, record={
        'status': 'blocked', 'persistent_recovery': {'phase': phase, 'reason': 'retained refusal'}})
    monitor.accept(old)
    monitor.accept(event(3, 'cycle_started', 2))
    monitor.accept(event(4, 'observation', 2, state={'tick': 42}))
    assert monitor.view['persistent_recovery']['phase'] == phase  # Observation alone is not recovery.
    monitor.accept(event(5, kind, stage, action='factory_gather', parameters={'resource': 'iron-ore'}))
    assert 'persistent_recovery' not in monitor.view
    assert old['data']['record']['persistent_recovery']['phase'] == phase
    assert monitor.view.get('verified') is not True
    # A new actual rejection remains visible; this is not permanent suppression.
    monitor.accept(event(6, 'decision_recorded', 7, record={
        'status': 'blocked', 'persistent_recovery': {'phase': phase, 'reason': 'new refusal'}}))
    assert monitor.view['persistent_recovery']['reason'] == 'new refusal'


def test_legacy_read_only_projection_and_supervisor_identity(tmp_path):
    path = tmp_path / "legacy.jsonl"
    sup = tmp_path / "supervisor.json"
    row = {"state": {"session_id": "one", "world_kind": "mock", "inventory": {"coal": 5}, "tick": 123},
           "action": "mine_coal", "verified": False, "outcome": "pending", "password": "not exported"}
    write(path, row)
    sup.write_text(json.dumps({"session_id": "two", "phase": "repair", "cwd": "/secret/path", "cutoff": 42}))
    before = path.read_bytes(), sup.read_bytes()
    monitor = Monitor(path, legacy=True, supervisor=sup)
    monitor.poll()
    snapshot = monitor.snapshot()
    assert snapshot["source"]["mode"] == "legacy"
    assert snapshot["view"].get("model_busy") is None
    assert snapshot["events"][-1]["action"] == "mine_coal"
    assert snapshot["events"][-1]["tick"] == 123
    assert snapshot["events"][-1]["outcome"] == "pending"
    assert snapshot["events"][-1]["verified"] is False
    assert not snapshot["supervisor"]["session_match"]
    assert "cwd" not in snapshot["supervisor"]["state"]
    assert "not exported" not in json.dumps(snapshot)
    assert before == (path.read_bytes(), sup.read_bytes())


@pytest.fixture
def server(tmp_path):
    path = tmp_path / "events.jsonl"
    write(path, event(policy="hybrid"))
    monitor = Monitor(path)
    monitor.poll()
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    monitor.stop.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def request(server, path, headers=None, method="GET"):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.request(method, path, headers=headers or {})
    response = connection.getresponse()
    status, result, response_headers = response.status, response.read(), dict(response.getheaders())
    connection.close()
    return status, result, response_headers


@pytest.mark.parametrize("path", ["/", "/app.js", "/explain.js", "/styles.css", "/factory-steel.png", "/api/snapshot"])
def test_http_assets_and_security_headers(server, path):
    status, raw, headers = request(server, path)
    assert status == 200 and raw
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "Access-Control-Allow-Origin" not in headers
    if path == "/factory-steel.png":
        assert headers["Content-Type"] == "image/png"
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize("path", ["/.env", "/../main.py", "/%2e%2e/main.py", "/api/execute", "/api/repair"])
def test_no_file_browsing_or_control_routes(server, path):
    assert request(server, path)[0] == 404


def test_icons_absent_without_icon_dir(server):
    assert request(server, "/icons/iron-plate.png")[0] == 404


def test_icons_served_only_by_validated_name(tmp_path):
    PNG = bytes.fromhex("89504e470d0a1a0a")
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "iron-plate.png").write_bytes(PNG + b"plate")
    (tmp_path / "secret.png").write_bytes(PNG + b"secret")
    (icons / "linked.png").symlink_to(tmp_path / "secret.png")
    path = tmp_path / "events.jsonl"
    write(path, event(policy="hybrid"))
    monitor = Monitor(path)
    server = DashboardServer(0, monitor, icons)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, raw, headers = request(server, "/icons/iron-plate.png")
        assert status == 200 and raw.endswith(b"plate") and headers["Content-Type"] == "image/png"
        for path in ("/icons/../secret.png", "/icons/%2e%2e/secret.png", "/icons/linked.png",
                     "/icons/Iron-Plate.png", "/icons/iron-plate.png.bak", "/icons/"):
            assert request(server, path)[0] == 404, path
    finally:
        monitor.stop.set(); server.shutdown(); server.server_close(); thread.join(timeout=3)


@pytest.mark.parametrize("headers", [{"Host": "attacker.test"}, {"Origin": "https://attacker.test"},
                                     {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}])
def test_rebinding_and_cross_origin_denied(server, headers):
    assert request(server, "/api/snapshot", headers)[0] == 403


def test_sse_initial_snapshot_and_reconnect(server):
    for _ in range(2):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("GET", "/api/events", headers={"Last-Event-ID": "999999"})
        response = connection.getresponse()
        assert response.status == 200
        assert response.readline() == b"event: snapshot\n"
        assert response.readline().startswith(b"id: ")
        data = response.readline().decode()
        assert json.loads(data.removeprefix("data: "))["view"]["policy"] == "hybrid"
        connection.close()


def test_no_runtime_dependencies_or_remote_frontend_assets():
    for name in ("index.html", "styles.css", "app.js", "explain.js", "factory-steel.png"):
        assert (ASSETS / name).is_file()
    js = (ASSETS / "app.js").read_text()
    assert ".innerHTML" not in js and "eval(" not in js
    assert "getDisplayMedia" in js and "getUserMedia" in js


def test_malformed_legacy_state_does_not_stop_following(tmp_path):
    path = tmp_path / "legacy"
    write(path, {"state": {}, "after_state": ["invalid"], "action": "observe"},
          {"state": {"session_id": "valid"}, "action": "observe"})
    monitor = Monitor(path, legacy=True)
    monitor.poll()
    assert monitor.rejected == 0
    assert monitor.view["run_id"] == "valid"


@pytest.mark.parametrize("timestamp", [10 ** 400, -(10 ** 400), float("inf"), float("nan")])
def test_unrenderable_timestamps_are_rejected_without_overflow(tmp_path, timestamp):
    monitor = Monitor(tmp_path / "events")
    monitor.accept({**event(), "time": timestamp})
    monitor.accept(event())
    assert monitor.rejected == 1
    assert monitor.view["run_id"] == "test-run"
