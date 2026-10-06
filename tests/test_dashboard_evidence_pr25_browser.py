"""Actual HTTP/SSE Chromium regressions for dashboard projection integrity."""
from __future__ import annotations

import json
import os
import threading

import pytest

playwright = pytest.importorskip("playwright.sync_api", reason="Install .[dashboard-test] for browser tests")
from jev_factorio.dashboard import DashboardServer, EventWriter, Monitor


@pytest.fixture
def live_events(tmp_path):
    path = tmp_path / "events.jsonl"
    writers = []
    writer = EventWriter(path)
    writers.append(writer)
    writer.emit("run_started", 2, controller="hierarchical")
    monitor = Monitor(path, supervisor=tmp_path / "supervisor.json")
    server = DashboardServer(0, monitor)
    follow = threading.Thread(target=monitor.follow, daemon=True)
    web = threading.Thread(target=server.serve_forever, daemon=True)
    follow.start()
    web.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get("CHROMIUM_PATH"),
                                        args=["--no-sandbox"])
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            yield page, writer, writers, path, f"http://127.0.0.1:{server.server_port}", errors
            browser.close()
    finally:
        for active in writers:
            if active.fd is not None:
                active.__exit__(None, None, None)
        monitor.stop.set()
        server.shutdown()
        server.server_close()
        follow.join(timeout=3)
        web.join(timeout=3)


@pytest.fixture
def live_legacy(tmp_path):
    path = tmp_path / "legacy.jsonl"
    path.write_text(json.dumps({
        "state": {"session_id": "pr25-legacy", "world_kind": "mock", "tick": 17},
        "goal": "bootstrap_mining", "action": "mine_coal",
        "parameters": {"amount": 4}, "verified": True, "outcome": "completed",
    }) + "\n", encoding="utf-8")
    monitor = Monitor(path, legacy=True, supervisor=tmp_path / "supervisor.json")
    server = DashboardServer(0, monitor)
    follow = threading.Thread(target=monitor.follow, daemon=True)
    web = threading.Thread(target=server.serve_forever, daemon=True)
    follow.start()
    web.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get("CHROMIUM_PATH"),
                                        args=["--no-sandbox"])
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            yield page, f"http://127.0.0.1:{server.server_port}", errors
            browser.close()
    finally:
        monitor.stop.set()
        server.shutdown()
        server.server_close()
        follow.join(timeout=3)
        web.join(timeout=3)


def _inspector(page):
    page.locator("#stage-6").click()
    evidence = json.loads(page.locator("#inspector-content").inner_text())
    page.locator("#close-inspector").click()
    return evidence


def _wait_event_count(page, count):
    # The dashboard deliberately serves a strict CSP without unsafe-eval, so
    # wait on rendered output rather than Playwright's page-context predicate.
    playwright.expect(page.locator("#event-count")).to_have_text(f"{count} recent events")


def test_chromium_inspector_separates_dispatch_from_verify_observe_and_failure(live_events):
    page, writer, writers, path, url, errors = live_events
    page.goto(url, wait_until="load")
    _wait_event_count(page, 1)

    first_parameters = {"role": "utility:lab", "item": "automation-science-pack", "count": 2}
    writer.emit("action", 6, action="factory_insert", parameters=first_parameters)
    _wait_event_count(page, 2)
    dispatched = _inspector(page)
    assert dispatched["recorded_action"] is None
    assert dispatched["dispatch"]["observed"] is True
    assert dispatched["dispatch"]["action"] == "factory_insert"
    assert dispatched["dispatch"]["parameters"] == first_parameters

    writer.emit("decision_recorded", 7, record={"action": "verify", "verified": True,
                                                 "outcome": "Observed lab input"})
    _wait_event_count(page, 3)
    first = _inspector(page)
    assert first["recorded_action"] == "verify"
    assert first["dispatch"]["observed"] is True
    assert first["dispatch"]["action"] == "factory_insert"
    assert first["dispatch"]["parameters"] == first_parameters

    writer.emit("decision_recorded", 7, record={"action": "observe", "verified": False,
                                                 "outcome": "Waiting for changed evidence"})
    _wait_event_count(page, 4)
    observed = _inspector(page)
    assert observed["recorded_action"] == "observe"
    assert observed["dispatch"]["action"] == "factory_insert"
    assert observed["dispatch"]["parameters"] == first_parameters

    writer.emit("dispatch_failed", 6, duration_ms=12)
    writer.emit("decision_recorded", 7, record={"action": "verify", "verified": False,
                                                 "outcome": "Postcondition failed"})
    _wait_event_count(page, 6)
    failed = _inspector(page)
    assert failed["recorded_action"] == "verify"
    assert failed["dispatch"]["action"] == "factory_insert"
    assert failed["dispatch"]["parameters"] == first_parameters

    # A later action event replaces the prior dispatch pair, even without parameters.
    writer.emit("action", 6, action="walk_to_coal")
    writer.emit("decision_recorded", 7, record={"action": "observe", "verified": False})
    _wait_event_count(page, 8)
    later = _inspector(page)
    assert later["recorded_action"] == "observe"
    assert later["dispatch"]["action"] == "walk_to_coal"
    assert later["dispatch"]["parameters"] is None
    assert not errors

    writer.__exit__(None, None, None)
    next_writer = EventWriter(path)
    writers.append(next_writer)
    next_writer.emit("run_started", 2, controller="hierarchical")
    _wait_event_count(page, 1)
    after_boundary = _inspector(page)
    assert after_boundary["dispatch"]["observed"] is False
    assert "action" not in after_boundary["dispatch"]
    assert not errors


def test_legacy_inspector_labels_record_without_inventing_a_dispatch(live_legacy):
    page, url, errors = live_legacy
    page.goto(url, wait_until="load")
    _wait_event_count(page, 1)
    evidence = _inspector(page)
    assert evidence["recorded_action"] == "mine_coal"
    assert evidence["dispatch"]["observed"] is False
    assert "legacy" in evidence["dispatch"]["reason"].lower()
    assert "parameters" not in evidence["dispatch"]
    assert not errors


def test_unknown_and_legacy_targets_do_not_render_a_rocket_campaign(live_events):
    page, writer, _, _, url, errors = live_events
    page.goto(url, wait_until="load")
    _wait_event_count(page, 1)

    names = lambda: page.locator("#goals .goal-node strong").all_text_contents()
    assert page.locator("#goals-count").inner_text() == "TARGET NOT RECORDED"
    assert names() == ["Campaign target not recorded"]
    assert "rocket_launch" not in names()

    writer.emit("goals", 3, goal="bootstrap_mining", completed_goals={"stockpile_fuel": 42})
    _wait_event_count(page, 2)
    assert names() == ["Campaign target not recorded", "stockpile_fuel"]
    assert "bootstrap_mining" not in names() and "rocket_launch" not in names()

    writer.emit("goals", 3, target=None, completed_goals={"stockpile_fuel": 42})
    _wait_event_count(page, 3)
    assert names() == ["Campaign target not recorded", "stockpile_fuel"]
    writer.emit("goals", 3, target="", completed_goals={"stockpile_fuel": 42})
    _wait_event_count(page, 4)
    assert names() == ["Campaign target not recorded", "stockpile_fuel"]
    writer.emit("goals", 3, target="unrecognized_mod_target", completed_goals={"stockpile_fuel": 42})
    _wait_event_count(page, 5)
    assert names()[0] == "Unsupported campaign target"
    assert names()[1:] == ["stockpile_fuel"]
    assert "rocket_launch" not in names()
    assert not errors


def test_explicit_campaign_targets_and_run_transition_keep_only_observed_goals(live_events):
    page, writer, writers, path, url, errors = live_events
    page.goto(url, wait_until="load")
    _wait_event_count(page, 1)
    names = lambda: page.locator("#goals .goal-node strong").all_text_contents()

    writer.emit("goals", 3, target="automation_science", goal="bootstrap_mining",
                completed_goals={"stockpile_fuel": 42})
    _wait_event_count(page, 2)
    assert names() == ["stockpile_fuel", "bootstrap_mining", "automation_science"]

    writer.emit("goals", 3, target="bootstrap_mining", completed_goals={"stockpile_fuel": 42})
    _wait_event_count(page, 3)
    assert names() == ["stockpile_fuel", "bootstrap_mining"]

    writer.emit("goals", 3, target="iron_smelting", completed_goals={"stockpile_fuel": 42})
    _wait_event_count(page, 4)
    assert names() == ["stockpile_fuel", "bootstrap_mining", "iron_smelting"]

    writer.emit("goals", 3, target="steam_power", completed_goals={"stockpile_fuel": 42})
    _wait_event_count(page, 5)
    assert names() == ["stockpile_fuel", "bootstrap_mining", "steam_power"]

    writer.emit("goals", 3, target="rocket_launch", goal="rocket_launch",
                completed_goals={"stockpile_fuel": 42, "bootstrap_mining": 85,
                                 "rocket_launch": 120})
    _wait_event_count(page, 6)
    assert page.locator('#goals [data-milestone="rocket_launch"]').count() == 1
    assert page.locator('#goals [data-milestone="rocket_launch"]').inner_text().startswith("rocket_launch")

    writer.__exit__(None, None, None)
    next_writer = EventWriter(path)
    writers.append(next_writer)
    next_writer.emit("run_started", 2, controller="hierarchical")
    _wait_event_count(page, 1)
    assert page.locator("#goals-count").inner_text() == "TARGET NOT RECORDED"
    assert names() == ["Campaign target not recorded"]
    assert "rocket_launch" not in names()
    assert not errors


def test_legacy_flat_record_without_target_retains_completion_but_not_chain(live_legacy):
    page, url, errors = live_legacy
    page.goto(url, wait_until="load")
    _wait_event_count(page, 1)
    names = page.locator("#goals .goal-node strong").all_text_contents()
    assert page.locator("#goals-count").inner_text() == "TARGET NOT RECORDED"
    assert "rocket_launch" not in names
    assert "stockpile_fuel" not in names and "bootstrap_mining" not in names
    assert not errors
