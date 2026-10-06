"""Real Chromium UI/capture tests. Synthetic media is injected by tests only."""
import json
import os
import re
import threading
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api", reason="Install .[dashboard-test] for browser tests")
from jev_factorio.dashboard import DashboardServer, EventWriter, Monitor


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "events.jsonl"
    writer = EventWriter(path)
    writer.emit("run_started", 2, policy="hybrid", target="rocket_launch")
    monitor = Monitor(path, supervisor=tmp_path / "supervisor.json", research=tmp_path / "research-catalog.json")
    server = DashboardServer(0, monitor)
    follow = threading.Thread(target=monitor.follow, daemon=True)
    web = threading.Thread(target=server.serve_forever, daemon=True)
    follow.start(); web.start()
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get("CHROMIUM_PATH"),
                                    args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1672, "height": 1150}, reduced_motion="reduce")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page, writer, f"http://127.0.0.1:{server.server_port}", errors
        context.close(); browser.close()
    writer.__exit__(None, None, None)
    monitor.stop.set(); server.shutdown(); server.server_close()
    follow.join(timeout=3); web.join(timeout=3)


@pytest.fixture
def live_legacy(tmp_path):
    path = tmp_path / "legacy.jsonl"
    path.write_text(json.dumps({
        "state": {"session_id": "usage-legacy-test", "world_kind": "mock", "tick": 1},
        "action": "observe", "outcome": "completed",
        "usage": {"input_tokens": 111, "output_tokens": 22},
    }) + "\n", encoding="utf-8")
    monitor = Monitor(path, legacy=True, supervisor=tmp_path / "supervisor.json",
                      research=tmp_path / "research-catalog.json")
    server = DashboardServer(0, monitor)
    follow = threading.Thread(target=monitor.follow, daemon=True)
    web = threading.Thread(target=server.serve_forever, daemon=True)
    follow.start(); web.start()
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get("CHROMIUM_PATH"),
                                    args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1672, "height": 1150}, reduced_motion="reduce")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page, f"http://127.0.0.1:{server.server_port}", errors
        context.close(); browser.close()
    monitor.stop.set(); server.shutdown(); server.server_close()
    follow.join(timeout=3); web.join(timeout=3)


def seed(writer):
    writer.emit("observation", 2, state={"session_id": "mock:browser-test", "world_kind": "mock", "tick": 314,
                "inventory": {"iron-ore": 148, "copper-ore": 82, "coal": 45, "iron-plate": 64},
                "player_position": [12, 0], "drill_status": "working", "drill_fuel": 5,
                "drill_output_connected": True, "iron_ore_collected": 8})
    writer.emit("goals", 3, goal="bootstrap_mining", target="rocket_launch", completed_goals={"stockpile_fuel": 100}, status="running")
    plan = {"id": "coal-buffer", "description": "Replenish the coal buffer", "steps": [{"action": "mine_coal"}]}
    other = {"id": "walk-coal", "description": "Approach observed coal", "steps": [{"action": "walk_to_coal"}]}
    writer.emit("model_request", 5, candidates={plan["id"]: plan, other["id"]: other}, questions={"candidate": {"type": "choice"}})
    writer.emit("model_started", 5)
    return plan


CAPTURE_FIXTURE = """(() => {
  window.testCapture = null;
  const media = () => {
    const canvas = document.createElement('canvas'); canvas.width = 960; canvas.height = 540;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#102a32'; ctx.fillRect(0,0,960,540);
    ctx.fillStyle = '#255b63';
    for (let x=0;x<960;x+=60) for(let y=0;y<540;y+=60) if((x+y)%120===0) ctx.fillRect(x,y,58,58);
    ctx.fillStyle = '#06161c'; ctx.fillRect(140,205,680,120);
    ctx.fillStyle = '#acf5e4'; ctx.font = '22px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('SYNTHETIC BROWSER CAPTURE TEST',480,255);
    ctx.font = '15px sans-serif'; ctx.fillText('Actual video element • not Factorio gameplay',480,290);
    const stream = canvas.captureStream(10); window.testCapture = stream;
    return Promise.resolve(stream);
  };
  Object.defineProperty(navigator.mediaDevices, 'getDisplayMedia', {value: media, configurable:true});
  Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {value: media, configurable:true});
  Object.defineProperty(navigator.mediaDevices, 'enumerateDevices', {value: async () => [{kind:'videoinput',deviceId:'test',label:'OBS Virtual Camera (test fixture)'}], configurable:true});
})();"""


DELAYED_FULLSCREEN_FIXTURE = """(() => {
  const nativeRequestFullscreen = Element.prototype.requestFullscreen;
  window.testFullscreenDelay = {requestedAt: null, nativeCalledAt: null, settledAt: null};
  Element.prototype.requestFullscreen = function(...args) {
    const element = this;
    window.testFullscreenDelay.requestedAt = performance.now();
    return new Promise((resolve, reject) => {
      setTimeout(() => {
        window.testFullscreenDelay.nativeCalledAt = performance.now();
        nativeRequestFullscreen.apply(element, args).then(value => {
          window.testFullscreenDelay.settledAt = performance.now();
          resolve(value);
        }, reject);
      }, 175);
    });
  };
})();"""


def test_live_decision_capture_freeze_inspector_and_overlay(live, tmp_path):
    page, writer, url, errors = live
    page.add_init_script(CAPTURE_FIXTURE)
    requests = []
    page.on("request", lambda req: requests.append(req.url))
    page.goto(url)
    plan = seed(writer)
    playwright.expect(page.locator("#thinking-status")).to_have_text("JEV is evaluating")
    playwright.expect(page.locator("#candidate-count")).to_have_text("2 MODEL CANDIDATES")
    page.locator("#capture-main").click()
    page.wait_for_function("document.querySelector('#game-video').videoWidth === 960")
    playwright.expect(page.locator("#video-status")).to_have_text("WINDOW CAPTURE")
    page.locator("#inspect-model").click()
    playwright.expect(page.locator("#inspector-content")).to_contain_text("coal-buffer")
    page.locator("#close-inspector").click()
    page.locator("#freeze").click()
    writer.emit("model_returned", 5, duration_ms=81)
    writer.emit("controller_state", 2, plan=plan, pending={"action": "mine_coal", "dispatch": "returned", "polls": 1},
                decision={"plan_id": "coal-buffer", "source": "jev", "utilities": {"coal-buffer": 0.83}})
    page.wait_for_timeout(800)
    playwright.expect(page.locator("#source-mode")).to_have_text("DISPLAY FROZEN")
    page.locator("#freeze").click()
    playwright.expect(page.locator("#selected-plan")).to_have_text("coal-buffer")
    playwright.expect(page.locator("#verification-status")).to_contain_text("Pending")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot = os.environ.get("DASHBOARD_SCREENSHOT")
    if screenshot:
        page.screenshot(path=screenshot, full_page=True)
    page.locator("#broadcast").click()
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgba(0, 0, 0, 0)"
    page.keyboard.press("b")
    page.locator("#stop-capture").click()
    assert page.evaluate("window.testCapture.getVideoTracks()[0].readyState") == "ended"
    playwright.expect(page.locator("#capture-placeholder")).to_be_visible()
    assert all(req.startswith(url) for req in requests)
    assert not errors


def test_camera_permission_denial_and_mobile_layout(live):
    page, writer, url, errors = live
    page.add_init_script("Object.defineProperty(navigator.mediaDevices,'getDisplayMedia',{value:()=>Promise.reject(new DOMException('denied','NotAllowedError'))});")
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    seed(writer)
    page.locator("#capture-main").click()
    page.wait_for_timeout(800)
    playwright.expect(page.locator("#notice")).to_contain_text("cancelled or denied")
    assert page.evaluate("document.querySelector('#game-video').srcObject === null")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


def test_obs_camera_selection_and_stop(live):
    page, writer, url, errors = live
    page.add_init_script(CAPTURE_FIXTURE)
    page.goto(url)
    page.locator("#camera").click()
    playwright.expect(page.locator("#camera-devices")).to_be_visible()
    playwright.expect(page.locator("#camera-devices")).to_contain_text("OBS Virtual Camera")
    page.wait_for_function("document.querySelector('#game-video').videoWidth === 960")
    page.locator("#stop-capture").click()
    assert page.evaluate("window.testCapture.getVideoTracks()[0].readyState") == "ended"
    assert not errors


def test_studio_layout_fits_broadcast_canvas_without_capture(live):
    page, writer, url, errors = live
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(url + "/?studio=1")
    seed(writer)
    playwright.expect(page.locator("#candidate-count")).to_have_text("2 MODEL CANDIDATES")
    playwright.expect(page.locator("#video-status")).to_have_text("OBS COMPOSITION")
    playwright.expect(page.locator("#capture-placeholder")).not_to_be_visible()
    assert page.evaluate("document.querySelector('#game-video').srcObject === null")
    page.evaluate("notice('Legacy log: completed decisions only. In-flight timing is unavailable.')")
    stage = page.locator("#game-stage").bounding_box()
    assert stage["x"] == pytest.approx(277)
    assert stage["y"] == pytest.approx(111)
    assert stage["width"] == pytest.approx(1342)
    assert stage["width"] > 1300
    assert stage["width"] / stage["height"] == pytest.approx(16 / 9)
    for selector in ("body", ".workspace", ".center-column", ".game-panel", "#game-stage"):
        assert page.locator(selector).evaluate("(element) => getComputedStyle(element).backgroundColor") == "rgba(0, 0, 0, 0)"
        assert page.locator(selector).evaluate("(element) => getComputedStyle(element).backgroundImage") == "none"
    for selector in (".thinking", ".game-panel", ".candidates-panel", ".right-column", ".event-panel"):
        bounds = page.locator(selector).bounding_box()
        assert bounds["y"] >= 0
        assert bounds["y"] + bounds["height"] <= 1080
        assert bounds["height"] > 100
    assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
    page.keyboard.press("b")
    playwright.expect(page.locator(".thinking")).not_to_be_visible()
    page.keyboard.press("b")
    playwright.expect(page.locator(".thinking")).to_be_visible()
    assert not errors


def test_factory_theme_is_accessible_without_external_artwork(live):
    page, writer, url, errors = live
    page.goto(url)
    seed(writer)
    playwright.expect(page).to_have_title("JEV · Factorio Mission Control")
    playwright.expect(page.locator(".brand-gear")).to_be_visible()
    assert page.locator(".brand-gear").get_attribute("aria-hidden") == "true"
    assert page.locator(".panel").first.evaluate("(element) => getComputedStyle(element).borderRadius") == "4px"
    assert "factory-steel.png" in page.locator(".topbar").evaluate("(element) => getComputedStyle(element).backgroundImage")
    assert "factory-steel.png" not in page.locator(".thinking").evaluate("(element) => getComputedStyle(element).backgroundImage")
    contrast = page.evaluate("""() => {
        const style = getComputedStyle(document.documentElement);
        const luminance = (hex) => {
            const values = hex.trim().slice(1).match(/../g).map((pair) => parseInt(pair, 16) / 255)
                .map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
            return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722;
        };
        return (luminance(style.getPropertyValue("--muted")) + 0.05) /
               (luminance(style.getPropertyValue("--panel")) + 0.05);
    }""")
    assert contrast >= 4.5
    page.locator("#freeze").focus()
    assert page.locator("#freeze").evaluate("(element) => getComputedStyle(element).outlineStyle") == "solid"
    assert page.evaluate("""() => performance.getEntriesByType("resource")
        .every((resource) => new URL(resource.name).origin === location.origin)""")
    assert not errors


def test_untrusted_text_is_inert_and_export_is_display_only(live, tmp_path):
    page, writer, url, errors = live
    page.goto(url)
    seed(writer)
    writer.emit("controller_state", 2, plan={"id": "<img src=x onerror=alert(1)>", "steps": []}, status="running")
    playwright.expect(page.locator("#selected-plan")).to_contain_text("<img")
    assert page.locator("#selected-plan img").count() == 0
    with page.expect_download() as download:
        page.locator("#export").click()
    path = tmp_path / "export.json"
    download.value.save_as(path)
    assert json.loads(path.read_text())["complete_audit"] is False
    assert not errors


def test_frozen_evidence_clock_keeps_advancing(live):
    page, writer, url, errors = live
    writer.path.with_name("supervisor.json").write_text(json.dumps({
        "session_id": "mock:browser-test", "phase": "gameplay", "started_at": time.time() - 90,
        "cutoff": time.time() + 90, "repair_required": False, "attempt": 0,
    }))
    page.goto(url)
    seed(writer)
    playwright.expect(page.locator("#run-time")).not_to_have_text("Not connected")
    page.locator("#freeze").click()
    before = page.locator("#run-time").inner_text()
    page.wait_for_timeout(2300)
    after = page.locator("#run-time").inner_text()
    seconds = lambda value: sum(int(part) * scale for part, scale in zip(value.split(":"), (3600, 60, 1)))
    assert seconds(before) >= 90
    assert seconds(after) - seconds(before) >= 2
    assert not errors


def test_candidate_focus_survives_heartbeats_and_new_records(live):
    page, writer, url, errors = live
    page.goto(url)
    plan = seed(writer)
    button = page.locator("#candidates button").first
    playwright.expect(button).to_be_visible()
    playwright.expect(page.locator("#thinking-status")).to_have_text("JEV is evaluating")
    page.evaluate("""() => {
        window.candidateMutations = 0;
        new MutationObserver(() => window.candidateMutations++).observe(
            document.querySelector("#candidates"), {childList:true});
    }""")
    button.focus()
    page.wait_for_timeout(1200)
    playwright.expect(button).to_be_focused()
    assert page.evaluate("window.candidateMutations") == 0
    writer.emit("controller_state", 2, plan=plan, status="running")
    playwright.expect(page.locator("#selected-plan")).to_have_text("coal-buffer")
    playwright.expect(button).to_be_focused()
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#inspector-content")).to_contain_text("mine_coal")
    assert not errors


def test_transport_cadence_and_record_to_screen_latency(live):
    page, writer, url, errors = live
    page.goto(url)
    seed(writer)
    playwright.expect(page.locator("#thinking-status")).to_have_text("JEV is evaluating")
    page.evaluate("""() => {
        window.snapshotTimes = [];
        events.addEventListener("snapshot", () => window.snapshotTimes.push(performance.now()));
    }""")
    page.wait_for_function("() => window.snapshotTimes.length >= 6")
    timestamps = page.evaluate("window.snapshotTimes")
    intervals = [later - earlier for earlier, later in zip(timestamps, timestamps[1:])]
    assert 400 <= sorted(intervals)[len(intervals) // 2] < 1500
    started = time.monotonic()
    writer.emit("observation", 2, state={"session_id": "mock:browser-test", "tick": 999})
    playwright.expect(page.locator("#tick")).to_have_text("tick 999", timeout=2000)
    assert time.monotonic() - started < 2
    assert not errors


def test_supervisor_booleans_and_persistent_source_warnings(live):
    page, writer, url, errors = live
    writer.path.with_name("supervisor.json").write_text(json.dumps({
        "session_id": "mock:browser-test", "phase": "gameplay", "repair_required": False, "attempt": 0,
    }))
    page.goto(url)
    seed(writer)
    playwright.expect(page.locator("#repair-detail")).to_contain_text("Repair required: false")
    writer.seq += 1
    writer.emit("controller_state", 2, status="running")
    playwright.expect(page.locator("#notice")).to_contain_text("history has a gap")
    message = page.evaluate("""() => {
        notice("Capture was cancelled or denied.");
        return document.querySelector("#notice").textContent;
    }""")
    assert "history has a gap" in message and "cancelled or denied" in message
    assert not errors


def test_all_evidence_controls_filter_and_frozen_export(live, tmp_path):
    page, writer, url, errors = live
    page.goto(url)
    plan = seed(writer)
    writer.emit("action", 6, action="mine_coal", parameters={"amount": 5})
    writer.emit("controller_state", 2, plan=plan, pending={"action": "mine_coal", "dispatch": "returned", "polls": 2},
                status="running", decision={"plan_id": "coal-buffer", "source": "jev"})
    playwright.expect(page.locator("#selected-plan")).to_have_text("coal-buffer")
    for stage in range(1, 9):
        page.locator(f"#stage-{stage}").click()
        playwright.expect(page.locator("#inspector")).to_be_visible()
        assert page.locator("#inspector-content").inner_text()
        page.keyboard.press("Escape")
        playwright.expect(page.locator("#inspector")).not_to_be_visible()
    for control, evidence in (("inspect-model", "coal-buffer"), ("inspect-plan", "coal-buffer"), ("inspect-pending", "mine_coal")):
        page.locator("#" + control).click()
        playwright.expect(page.locator("#inspector-content")).to_contain_text(evidence)
        page.locator("#close-inspector").click()
    page.locator("#event-filter").select_option("6")
    playwright.expect(page.locator(".event-row")).to_have_count(1)
    playwright.expect(page.locator(".event-row")).to_contain_text("mine_coal")
    page.locator("#event-filter").select_option("all")
    assert page.locator(".event-row").count() > 1
    page.locator("#freeze").click()
    writer.emit("controller_state", 2, status="completed")
    page.wait_for_timeout(800)
    with page.expect_download() as download:
        page.locator("#export").click()
    destination = tmp_path / "frozen.json"
    download.value.save_as(destination)
    exported = json.loads(destination.read_text())
    assert exported["display_only"] is True and exported["snapshot"]["view"]["status"] == "running"
    page.locator("#freeze").click()
    playwright.expect(page.locator("#controller-status")).to_have_text("COMPLETED")
    assert not errors


def test_capture_replacement_cancel_fullscreen_and_track_end(live):
    page, writer, url, errors = live
    page.add_init_script(CAPTURE_FIXTURE)
    page.add_init_script(DELAYED_FULLSCREEN_FIXTURE)
    page.goto(url)
    seed(writer)
    page.locator("#capture").click()
    page.wait_for_function("document.querySelector('#game-video').videoWidth === 960")
    page.evaluate("window.oldTrack = window.testCapture.getVideoTracks()[0]")
    page.locator("#camera").click()
    playwright.expect(page.locator("#video-status")).to_have_text("CAMERA / OBS")
    assert page.evaluate("window.oldTrack.readyState") == "ended"
    page.evaluate("""Object.defineProperty(navigator.mediaDevices, 'getDisplayMedia', {
        value: async () => { throw new DOMException('cancelled', 'NotAllowedError'); }, configurable:true
    })""")
    page.locator("#capture").click()
    playwright.expect(page.locator("#notice")).to_contain_text("cancelled or denied")
    assert page.evaluate("window.testCapture.getVideoTracks()[0].readyState") == "live"
    page.locator("#camera-devices").select_option("test")
    page.locator("#fullscreen").click()
    # The fixture holds the real request for 175 ms, reproducing the old
    # 50 ms race without relaxing CSP or sleeping in the assertion path.
    stage_fullscreen = page.locator("#game-stage:fullscreen")
    playwright.expect(stage_fullscreen).to_have_count(1)
    timing = page.evaluate("window.testFullscreenDelay")
    assert timing["nativeCalledAt"] - timing["requestedAt"] > 50
    assert timing["settledAt"] >= timing["nativeCalledAt"]
    page.evaluate("document.exitFullscreen()")
    page.evaluate("window.testCapture.getVideoTracks()[0].dispatchEvent(new Event('ended'))")
    playwright.expect(page.locator("#capture-placeholder")).to_be_visible()
    assert page.evaluate("document.querySelector('#game-video').srcObject === null")
    assert not errors


def test_restored_page_reconnects_and_invalid_snapshot_recovers(live):
    page, writer, url, errors = live
    page.goto(url)
    seed(writer)
    playwright.expect(page.locator("#connection")).to_have_text("Feed connected")
    page.evaluate("""events.dispatchEvent(new MessageEvent('snapshot', {data:'not json'}))""")
    playwright.expect(page.locator("#notice")).to_contain_text("invalid dashboard snapshot")
    playwright.expect(page.locator("#notice")).not_to_contain_text("invalid dashboard snapshot", timeout=2500)
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    writer.emit("controller_state", 2, status="restored")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    playwright.expect(page.locator("#controller-status")).to_have_text("RESTORED")
    assert not errors


def test_stale_disconnected_and_delayed_feed_indicators(live):
    page, writer, url, errors = live
    page.goto(url)
    seed(writer)
    playwright.expect(page.locator("#thinking-status")).to_have_text("JEV is evaluating")
    status = page.evaluate("""() => {
        const snapshot = JSON.parse(JSON.stringify(latest));
        snapshot.view.last_event_time = snapshot.server_time - 30;
        events.dispatchEvent(new MessageEvent("snapshot", {data:JSON.stringify(snapshot)}));
        return {connection:document.querySelector("#connection").textContent,
                active:document.querySelector("#signal").classList.contains("active")};
    }""")
    assert status == {"connection": "No recent telemetry", "active": False}
    playwright.expect(page.locator("#connection")).to_have_text("Feed connected")
    playwright.expect(page.locator("#stage-5.active")).to_have_count(1)
    status = page.evaluate("""() => {
        receivedAt -= 4000; refreshStatus();
        return document.querySelector("#connection").textContent;
    }""")
    assert status == "Feed delayed"
    playwright.expect(page.locator("#connection")).to_have_text("Feed connected")
    playwright.expect(page.locator("#stage-5.active")).to_have_count(1)
    status = page.evaluate("""() => {
        events.onerror();
        return document.querySelector("#connection").textContent;
    }""")
    assert status == "Reconnecting"
    playwright.expect(page.locator("#connection")).to_have_text("Feed connected")
    playwright.expect(page.locator("#stage-5.active")).to_have_count(1)
    assert not errors


def test_legacy_missing_details_are_unavailable_not_zero_or_no_response(live):
    page, writer, url, errors = live
    page.goto(url)
    seed(writer)
    playwright.expect(page.locator("#thinking-status")).to_have_text("JEV is evaluating")
    labels = page.evaluate("""() => {
        const snapshot = JSON.parse(JSON.stringify(latest));
        snapshot.source.mode = "legacy";
        snapshot.view.request = null;
        events.dispatchEvent(new MessageEvent("snapshot", {data:JSON.stringify(snapshot)}));
        return {candidates:document.querySelector("#candidate-count").textContent,
                model:document.querySelector("#model-detail").textContent,
                active:document.querySelector("#signal").classList.contains("active")};
    }""")
    assert labels == {
        "candidates": "UNAVAILABLE IN LEGACY LOG",
        "model": "Completed decision only · no in-flight telemetry",
        "active": False,
    }
    assert not errors


def test_recorded_evidence_ticker_and_actions_do_not_claim_live_phase(live):
    page, writer, url, errors = live
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(url + "/?studio=1")
    seed(writer)
    playwright.expect(page.locator("#thinking-status")).to_have_text("JEV is evaluating")
    result = page.evaluate("""() => {
        events.close();
        const before = document.querySelector(".game-stage").getBoundingClientRect();
        const snapshot = JSON.parse(JSON.stringify(latest));
        snapshot.source.mode = "legacy";
        Object.assign(snapshot.view, {request:null, plan:null, stage:7, seen:[7],
          verified:true, pending:null, goal:"rocket_launch"});
        snapshot.events = [{stage:7,kind:"decision_recorded",action:"mine_coal",
          tick:123,verified:true,outcome:"Observed coal increase"}];
        latest = snapshot;
        render(snapshot);
        refreshStatus();
        const after = document.querySelector(".game-stage").getBoundingClientRect();
        return {before:[before.x,before.y,before.width,before.height],
                after:[after.x,after.y,after.width,after.height]};
    }""")
    assert result["before"] == result["after"]
    playwright.expect(page.locator("#stage-7")).not_to_have_class("workflow-node active")
    assert page.locator("#stage-7").evaluate("node => !node.classList.contains('active') && node.classList.contains('seen')")
    playwright.expect(page.locator("#goals")).to_contain_text("Active target")
    playwright.expect(page.locator("#candidate-table")).to_be_hidden()
    playwright.expect(page.locator("#recorded-actions")).to_contain_text("mine coal")
    playwright.expect(page.locator("#event-log")).to_contain_text("tick 123")
    playwright.expect(page.locator("#event-log")).to_contain_text("Observed coal increase")
    playwright.expect(page.locator("#event-log")).not_to_contain_text("captured row")
    playwright.expect(page.locator("#evidence-ticker")).to_be_visible()
    assert page.locator("#evidence-ticker span").evaluate("node => getComputedStyle(node).animationName") == "none"
    page.emulate_media(reduced_motion="no-preference")
    page.locator("#evidence-ticker").focus()
    assert page.locator("#evidence-ticker span").evaluate("node => getComputedStyle(node).animationPlayState") == "paused"
    page.evaluate("notice('Telemetry unavailable')")
    assert page.locator("#evidence-ticker").evaluate("node => getComputedStyle(node).visibility") == "hidden"
    assert not errors


def test_camera_device_switch_and_playback_failure_cleanup(live):
    page, writer, url, errors = live
    page.add_init_script(CAPTURE_FIXTURE)
    page.goto(url)
    page.evaluate("""() => {
        const original = navigator.mediaDevices.getUserMedia;
        Object.defineProperty(navigator.mediaDevices, "getUserMedia", {configurable:true, value:async (constraints) => {
            window.lastConstraints = constraints;
            const incoming = await original(constraints);
            incoming.getVideoTracks()[0].getSettings = () => ({deviceId:constraints.video?.deviceId?.exact || "test"});
            return incoming;
        }});
        Object.defineProperty(navigator.mediaDevices, "enumerateDevices", {configurable:true, value:async () => [
            {kind:"videoinput",deviceId:"test",label:"OBS Virtual Camera (test fixture)"},
            {kind:"videoinput",deviceId:"alternate",label:"Alternate Camera (test fixture)"}
        ]});
    }""")
    page.locator("#camera").click()
    playwright.expect(page.locator("#camera-devices")).to_be_visible()
    page.evaluate("window.priorCamera = window.testCapture.getVideoTracks()[0]")
    page.locator("#camera-devices").select_option("alternate")
    page.wait_for_function("window.lastConstraints.video.deviceId?.exact === 'alternate'")
    playwright.expect(page.locator("#camera-devices")).to_have_value("alternate")
    assert page.evaluate("window.priorCamera.readyState") == "ended"
    assert page.evaluate("window.lastConstraints.audio") is False
    page.locator("#stop-capture").click()
    assert page.locator("#camera-devices").input_value() == ""
    page.locator("#camera").click()
    playwright.expect(page.locator("#camera-devices")).to_be_visible()
    assert page.evaluate("window.lastConstraints.video") is True
    page.locator("#stop-capture").click()
    page.evaluate("() => { HTMLMediaElement.prototype.play = () => Promise.reject(new DOMException('denied','NotAllowedError')); }")
    page.locator("#capture").click()
    playwright.expect(page.locator("#notice")).to_contain_text("Video playback could not start")
    assert page.evaluate("window.testCapture.getVideoTracks()[0].readyState") == "ended"
    assert page.evaluate("document.querySelector('#game-video').srcObject === null")
    assert not errors


def test_late_capture_permission_is_released_after_page_exit(live):
    page, writer, url, errors = live
    page.add_init_script("""Object.defineProperty(navigator.mediaDevices, "getDisplayMedia", {
        value:() => new Promise((resolve) => {window.deliverCapture = resolve;})
    })""")
    page.goto(url)
    page.locator("#capture").click()
    page.wait_for_function("typeof window.deliverCapture === 'function'")
    page.evaluate("""() => {
        window.dispatchEvent(new PageTransitionEvent("pagehide"));
        const canvas = document.createElement("canvas");
        window.lateStream = canvas.captureStream(1);
        window.deliverCapture(window.lateStream);
    }""")
    page.wait_for_function("window.lateStream.getVideoTracks()[0].readyState === 'ended'")
    assert page.evaluate("document.querySelector('#game-video').srcObject === null")
    assert not errors


def test_decision_metrics_observations_and_inventory(live):
    page, writer, url, errors = live
    page.goto(url)
    plan = seed(writer)
    answers = {
        "coal-buffer/benefit": {"score": 1.5, "confidence": 0.8},
        "coal-buffer/disruption": {"score": 0.25, "confidence": 0.9},
        "coal-buffer/needs_observation": {"noul": 0.1},
        "candidate": {"probabilities": {"coal-buffer": 0.75}},
    }
    writer.emit("model_response", 5, answers=answers, usage={"input_tokens": 1000, "output_tokens": 250})
    writer.emit("model_returned", 5, duration_ms=81)
    writer.emit("controller_state", 2, plan=plan, status="running",
                decision={"plan_id": "coal-buffer", "source": "jev", "answers": answers, "utilities": {"coal-buffer": 0.83}})
    playwright.expect(page.locator("#latency")).to_have_text("81 ms")
    playwright.expect(page.locator("#tokens")).to_have_text("1.3K")
    playwright.expect(page.locator("#candidates tr.selected td")).to_have_text([
        "Replenish the coal buffercoal-buffer", "1.50", "0.25", "0.10", "0.75", "0.80 / 0.90", "0.830", "COMMITTED",
    ])
    playwright.expect(page.locator("#observations")).to_contain_text("Working")
    playwright.expect(page.locator("#observations")).to_contain_text("5 coal")
    playwright.expect(page.locator("#inventory")).to_contain_text("148")
    playwright.expect(page.locator(".goal-node.done")).to_contain_text("stockpile_fuel")
    playwright.expect(page.locator(".goal-node.current")).to_contain_text("bootstrap_mining")
    assert not errors


def test_current_response_usage_replaces_previous_completed_usage(live):
    page, writer, url, errors = live
    page.goto(url)
    writer.emit("decision_recorded", 7, record={
        "usage": {"input_tokens": 111, "output_tokens": 22},
        "state": {"session_id": "usage-browser-test", "world_kind": "mock", "tick": 1},
        "action": "observe", "outcome": "completed",
    })
    playwright.expect(page.locator("#tokens")).to_have_text("133")

    writer.emit("cycle_started", 2)
    writer.emit("model_request", 5, questions={})
    writer.emit("model_started", 5)
    # Until a response exists, the completed decision remains a useful fallback.
    playwright.expect(page.locator("#tokens")).to_have_text("133")
    writer.emit("model_response", 5, answers={}, usage={"input_tokens": 333, "output_tokens": 44})
    # Current response usage must win before the current decision is recorded.
    playwright.expect(page.locator("#tokens")).to_have_text("377")

    writer.emit("model_returned", 5, duration_ms=81)
    writer.emit("decision_recorded", 7, record={
        "usage": {"input_tokens": 333, "output_tokens": 44},
        "state": {"session_id": "usage-browser-test", "world_kind": "mock", "tick": 2},
        "action": "observe", "outcome": "completed",
    })
    playwright.expect(page.locator("#tokens")).to_have_text("377")

    writer.emit("cycle_started", 2)
    playwright.expect(page.locator("#tokens")).to_have_text("377")
    writer.emit("model_response", 5, answers={}, usage=None)
    playwright.expect(page.locator("#tokens")).to_have_text("—")

    writer.emit("model_returned", 5, duration_ms=82)
    writer.emit("decision_recorded", 7, record={
        "state": {"session_id": "usage-browser-test", "world_kind": "mock", "tick": 3},
        "action": "observe", "outcome": "completed",
    })
    playwright.expect(page.locator("#tokens")).to_have_text("—")

    writer.emit("cycle_started", 2)
    writer.emit("model_response", 5, answers={})
    playwright.expect(page.locator("#tokens")).to_have_text("—")

    writer.emit("cycle_started", 2)
    writer.emit("model_response", 5, answers={}, usage={"input_tokens": 0, "output_tokens": 0})
    playwright.expect(page.locator("#tokens")).to_have_text("0")
    writer.emit("model_returned", 5, duration_ms=83)
    writer.emit("decision_recorded", 7, record={
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "state": {"session_id": "usage-browser-test", "world_kind": "mock", "tick": 4},
        "action": "observe", "outcome": "completed",
    })
    playwright.expect(page.locator("#tokens")).to_have_text("0")
    assert not errors


def test_legacy_record_usage_remains_available(live_legacy):
    page, url, errors = live_legacy
    page.goto(url)
    playwright.expect(page.locator("#tokens")).to_have_text("133")
    playwright.expect(page.locator("#source-mode")).to_have_text("LEGACY / COMPLETED DECISIONS")
    assert not errors


@pytest.mark.parametrize("width,height", [(390, 844), (760, 1024), (1280, 720)])
def test_responsive_controls_stay_within_viewport(live, width, height):
    page, writer, url, errors = live
    page.set_viewport_size({"width": width, "height": height})
    page.goto(url)
    seed(writer)
    playwright.expect(page.locator("#candidate-count")).to_have_text("2 MODEL CANDIDATES")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#inspect-model").click()
    bounds = page.locator("#inspector").bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= height
    page.locator("#close-inspector").click()
    assert not errors


def mission_fixture():
    from test_dashboard_mission import mission_state
    from jev_factorio.dashboard import project_state
    return project_state(mission_state())


def test_mission_launch_gates_receipt_victory_and_frozen_display(live):
    from test_dashboard_mission import mission_state, receipt
    from jev_factorio.dashboard import project_state
    page, writer, url, errors = live
    page.goto(url)
    state = mission_state()
    writer.emit('observation',2,state=project_state(state))
    playwright.expect(page.locator('#launch-headline')).to_have_text('Launch prerequisites observed')
    playwright.expect(page.locator('[data-gate="request"] .mission-status')).to_have_text('PENDING')
    page.locator('#freeze').click()
    state['factory']['launch_readiness']['receipts']['launch']=receipt()
    writer.emit('observation',2,state=project_state(state))
    page.wait_for_timeout(600)
    playwright.expect(page.locator('#launch-headline')).to_have_text('Launch prerequisites observed')
    page.locator('#freeze').click()
    playwright.expect(page.locator('#launch-headline')).to_have_text('Launch submitted; victory unverified')
    playwright.expect(page.locator('[data-gate="victory"] .mission-status')).to_have_text('PENDING')
    state.update(victory=True,victory_source='native:base-game-rocket-launch')
    writer.emit('observation',2,state=project_state(state))
    playwright.expect(page.locator('#launch-headline')).to_have_text('Native victory observed')
    page.locator('#inspect-mission').click()
    playwright.expect(page.locator('#inspector-content')).to_contain_text('"deployment_authorized": false')
    page.locator('#close-inspector').click()
    writer.emit('observation',2,state=project_state({'tick':1001,'session_id':'new-session'}))
    playwright.expect(page.locator('#launch-headline')).to_have_text('Launch evidence unavailable')
    assert not errors


def test_mission_stale_snapshot_does_not_rejuvenate_on_model_event(live):
    page,writer,url,errors=live
    page.goto(url)
    writer.emit('observation',2,state=mission_fixture())
    playwright.expect(page.locator('#launch-headline')).to_have_text('Launch prerequisites observed')
    # Check one synchronous render: the live SSE heartbeat may replace latest
    # between separate browser calls after this deliberately injected stale state.
    rendered = page.evaluate('''() => {
        latest.view.state_observed_time = latest.server_time - 100;
        latest.view.last_event_time = latest.server_time;
        refreshStatus();
        return {
            freshness: document.querySelector('#mission-freshness').textContent,
            historical: document.querySelector('#mission-panel').classList.contains('mission-historical')
        };
    }''')
    assert rendered == {'freshness': 'STALE / DISCONNECTED', 'historical': True}
    assert not errors


def test_mission_layout_studio_mobile_xss_and_release_unknown(live):
    page,writer,url,errors=live
    page.set_viewport_size({'width':1920,'height':1080})
    page.goto(url+'/?studio=1')
    state=mission_fixture()
    state['mission']['research']['name']='<img src=x onerror="window.BAD=1">'
    writer.emit('observation',2,state=state)
    writer.emit('decision_recorded',7,record={'state':state,'mission_record':{
        'commit':'a'*40,'tick':1000,'features':{'ore_side_successors':False}}})
    playwright.expect(page.locator('#launch-headline')).to_have_text('Launch prerequisites observed')
    stage=page.locator('#game-stage').bounding_box()
    assert stage['x']==pytest.approx(277) and stage['y']==pytest.approx(111)
    assert stage['width']==pytest.approx(1342)
    # Studio keeps readiness to one line; detail stays rendered for Evidence and non-studio layouts.
    playwright.expect(page.locator('#launch-summary')).to_contain_text('launch gates observed')
    playwright.expect(page.locator('#mission-panel details').first).to_be_hidden()
    playwright.expect(page.locator('#mission-features')).to_contain_text('Disabled (recorded)')
    assert page.locator('#mission-production img').count()==0
    assert page.evaluate('window.BAD') is None
    playwright.expect(page.locator('#mission-release')).to_contain_text('Not supplied by gameplay')
    playwright.expect(page.locator('#mission-release')).to_contain_text('No acceptance report connected')
    page.screenshot(path='/tmp/mission-control-studio-test.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.locator('#mission-panel').bounding_box()['width']>200
    assert not errors


def test_legacy_stream_view_is_plain_and_progressive(live):
    page, writer, url, errors = live
    page.goto(url)
    seed(writer)
    playwright.expect(page.locator("#inventory")).to_contain_text("148")
    result = page.evaluate("""() => {
        events.close();
        const snapshot = JSON.parse(JSON.stringify(latest));
        snapshot.source.mode = "legacy";
        const state = {tick: 1988600, inventory: {"iron-plate": 55, "logistic-science-pack": 11},
          researched: ["automation"], drill_status: "waiting_for_space_in_destination", drill_fuel: 5,
          drill_output_connected: false, iron_ore_collected: 0, player_position: [34.4, 63.8]};
        Object.assign(snapshot.view, {request: null, plan: null, state, action: "observe", verified: false,
          outcome: "Waiting for the in-flight postcondition",
          pending: {started_tick: 1986778, polls: 3, action: "factory_insert", dispatch: "ambiguous"}});
        snapshot.events = [{action: "factory_insert", tick: 1756859, verified: true,
          outcome: "Transferred 5 coal (1756859:factory_insert:input:147:drill:coal)"}];
        latest = snapshot; render(snapshot); refreshStatus();
        const before = document.querySelectorAll("#inventory .slot").length;
        state.researched = ["automation", "logistic-science-pack"];
        render(JSON.parse(JSON.stringify(snapshot)));
        return {before, newSlots: [...document.querySelectorAll("#inventory .slot.new")].map((n) => n.title)};
    }""")
    playwright.expect(page.locator("#event-log")).to_contain_text("Loaded 5 coal into a mining drill")
    playwright.expect(page.locator("#event-log")).not_to_contain_text("1756859:factory_insert")
    playwright.expect(page.locator("#observations")).to_contain_text("Blocked: output full")
    playwright.expect(page.locator("#observations")).to_contain_text("None yet")
    playwright.expect(page.locator("#verification-status")).to_have_text("Pending: loading items")
    playwright.expect(page.locator("#pending-polls")).to_have_text("Checking · 30s · 3 looks")
    playwright.expect(page.locator("#pending-check")).to_have_class("pending-check active")
    playwright.expect(page.locator("#stage-7")).to_have_class("workflow-node last")
    playwright.expect(page.locator("#thinking-status")).to_have_text("Checking whether the last action worked")
    assert result["newSlots"] == ["logistic science pack: 11 (newly unlocked)"]
    playwright.expect(page.locator("#inventory .slot.no-icon").first).to_be_visible()
    assert not errors


def test_studio_readiness_is_compact_and_right_column_fits(live):
    page, writer, url, errors = live
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(url + "/?studio=1")
    seed(writer)
    playwright.expect(page.locator("#launch-summary")).to_be_visible()
    playwright.expect(page.locator("#launch-gates")).to_be_hidden()
    for selector in (".workflow-panel", ".verification"):
        box = page.locator(selector).bounding_box()
        assert box and box["y"] + box["height"] <= 1080, selector
    assert not errors


def test_studio_objective_milestones_are_compact_and_fit(live):
    page, writer, url, errors = live
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(url + "/?studio=1")
    seed(writer)
    writer.emit("observation", 2, state={"session_id": "mock:browser-test", "world_kind": "mock", "tick": 400,
                "researched": ["steam-power", "automation-science-pack"]})
    writer.emit("observation", 2, state={"session_id": "mock:browser-test", "world_kind": "mock", "tick": 500,
                "researched": ["steam-power", "automation-science-pack", "logistic-science-pack"]})
    writer.emit("goals", 3, goal="rocket_launch", target="rocket_launch",
                completed_goals={"stockpile_fuel": 100, "bootstrap_mining": 200}, status="running")
    playwright.expect(page.locator("#goals-count")).to_have_text("5 / 11")
    playwright.expect(page.locator("#goals .goal-node")).to_have_count(11)
    playwright.expect(page.locator('[data-milestone="steam-power"]')).to_contain_text("Researched")
    playwright.expect(page.locator('[data-milestone="logistic-science-pack"]')).to_contain_text("Seen tick 500")
    playwright.expect(page.locator('[data-milestone="bootstrap_mining"]')).to_contain_text("Verified tick 200")
    playwright.expect(page.locator(".goal-node.current")).to_contain_text("Oil processing")
    playwright.expect(page.locator('[data-milestone="rocket_launch"]')).to_contain_text("Active target")
    for node in page.locator("#goals .goal-node").all():
        assert node.bounding_box()["height"] < 24
        # Names are never clipped; only the detail text may shrink if a font renders wider.
        assert node.locator("strong").evaluate("el => el.scrollWidth <= el.clientWidth")
    thinking = page.locator(".thinking")
    assert thinking.evaluate("node => node.scrollHeight <= node.clientHeight")
    assert not errors


@pytest.mark.parametrize("state,expected", [
    ({"started_at": -7200, "cutoff": -3600}, "01:00:00"),  # clock stops at the cutoff
    ({"started_at": -(50 * 3600 + 61)}, re.compile(r"^50:01:0\d$")),  # no cutoff: still counting, hours may exceed 24
    ({"cutoff": 3600}, "Not connected"),                  # a cutoff alone is not a run start
    ({"started_at": 3600}, "Not connected"),              # a future start is not elapsed time
    ({"started_at": "yesterday"}, "Not connected"),
])
def test_run_time_counts_up_from_the_supervisor_start(live, state, expected):
    page, writer, url, errors = live
    now = time.time()
    values = {key: now + value if isinstance(value, (int, float)) else value for key, value in state.items()}
    writer.path.with_name("supervisor.json").write_text(json.dumps({
        "session_id": "mock:browser-test", "phase": "gameplay", "attempt": 0, **values}))
    page.goto(url)
    seed(writer)
    playwright.expect(page.locator(".top-context.run-time .eyebrow")).to_have_text("RUN TIME")
    playwright.expect(page.locator("#run-time")).to_have_text(expected)
    assert not errors


def test_run_time_ignores_a_supervisor_for_another_session(live):
    page, writer, url, errors = live
    writer.path.with_name("supervisor.json").write_text(json.dumps({
        "session_id": "another-session", "phase": "gameplay", "started_at": time.time() - 60}))
    page.goto(url)
    seed(writer)
    playwright.expect(page.locator("#thinking-status")).to_have_text("JEV is evaluating")
    playwright.expect(page.locator("#run-time")).to_have_text("Not connected")
    assert not errors


def research_seed(writer, researched, current=None, progress=None):
    from jev_factorio.dashboard import project_state
    writer.emit("observation", 2, state=project_state({
        "session_id": "mock:browser-test", "world_kind": "mock", "tick": 400, "researched": researched,
        "factory": {"research": current, "research_progress": progress} if current else {}}))


def test_research_strip_shows_the_game_tree_and_fits_the_studio_header(live):
    from jev_factorio import research_catalog
    from test_research_catalog import DATA_20, raw_catalog
    page, writer, url, errors = live
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(url + "/?studio=1")
    seed(writer)
    playwright.expect(page.locator("#research-strip")).to_be_hidden()
    research_catalog.write(writer.path.with_name(research_catalog.FILENAME), raw_catalog(DATA_20))
    research_seed(writer, ["electronics", "automation-science-pack", "automation"], "logistic-science-pack", 0.42)
    playwright.expect(page.locator("#research-strip")).to_be_visible()
    playwright.expect(page.locator("#research-current")).to_have_text("Logistic science pack")
    playwright.expect(page.locator("#research-progress")).to_have_text("42%")
    playwright.expect(page.locator("#research-goal")).to_have_text("TO ROCKET SILO")
    playwright.expect(page.locator("#research-tiers li")).to_have_count(3)
    playwright.expect(page.locator("#research-tiers li.active")).to_have_attribute("data-pack", "automation-science-pack")
    playwright.expect(page.locator('[data-milestone="rocket-silo"]')).to_contain_text("Not yet")
    playwright.expect(page.locator('[data-milestone="automation-science-pack"]')).to_contain_text("Researched")
    strip = page.locator("#research-strip").bounding_box()
    context = page.locator(".top-context").first.bounding_box()
    brand = page.locator(".brand").bounding_box()
    assert brand["x"] + brand["width"] <= strip["x"] and strip["x"] + strip["width"] <= context["x"]
    assert strip["y"] >= 0 and strip["y"] + strip["height"] <= 48
    assert page.locator(".thinking").evaluate("node => node.scrollHeight <= node.clientHeight")
    assert not errors


def test_research_strip_describes_trigger_technologies_without_a_progress_bar(live):
    from jev_factorio import research_catalog
    from test_research_catalog import DATA_20, raw_catalog
    page, writer, url, errors = live
    page.goto(url + "/?studio=1")
    seed(writer)
    research_catalog.write(writer.path.with_name(research_catalog.FILENAME), raw_catalog(DATA_20))
    research_seed(writer, ["electronics"], "automation-science-pack", 0)
    playwright.expect(page.locator("#research-progress")).to_have_text("Craft 10 × iron gear wheel")
    playwright.expect(page.locator("#research-bar-track")).to_be_hidden()
    assert not errors


def test_research_strip_stays_hidden_for_a_tree_from_another_version(live):
    from jev_factorio import research_catalog
    from test_research_catalog import DATA_20, raw_catalog
    page, writer, url, errors = live
    page.goto(url + "/?studio=1")
    seed(writer)
    research_catalog.write(writer.path.with_name(research_catalog.FILENAME), raw_catalog(DATA_20))
    research_seed(writer, ["automation", "a-technology-this-tree-does-not-have"])
    playwright.expect(page.locator("#thinking-status")).to_have_text("JEV is evaluating")
    page.wait_for_timeout(800)
    playwright.expect(page.locator("#research-strip")).to_be_hidden()
    playwright.expect(page.locator('[data-milestone="steam-power"]')).to_be_visible()
    assert not errors


def test_long_space_age_ladders_fold_tiers_and_milestones(live):
    from jev_factorio import research_catalog
    page, writer, url, errors = live
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(url + "/?studio=1")
    seed(writer)
    packs = [f"pack-{index:02d}" for index in range(12)]
    technologies, previous = {}, []
    for index, pack in enumerate(packs):
        technologies[f"unlock-{pack}"] = {"prerequisites": previous, "essential": True, "unlocks": [pack],
                                          "ingredients": [{"name": p, "amount": 1} for p in packs[:index]]}
        technologies[f"use-{pack}"] = {"prerequisites": [f"unlock-{pack}"],
                                       "ingredients": [{"name": p, "amount": 1} for p in packs[:index + 1]]}
        previous = [f"unlock-{pack}"]
    raw = {"schema": research_catalog.SCHEMA, "version": "2.0.77",
           "mods": {"base": "2.0.77", "space-age": "2.0.77"}, "technologies": technologies,
           "science_packs": {pack: {"from_start": False, "unlocked_by": [f"unlock-{pack}"]} for pack in packs}}
    research_catalog.write(writer.path.with_name(research_catalog.FILENAME), raw)
    done = [name for index, pack in enumerate(packs[:9]) for name in (f"unlock-{pack}", f"use-{pack}")]
    writer.emit("goals", 3, goal="rocket_launch", target="rocket_launch",
                completed_goals={"stockpile_fuel": 100, "bootstrap_mining": 200}, status="running")
    research_seed(writer, done)
    playwright.expect(page.locator("#research-strip")).to_be_visible()
    assert page.locator("#research-tiers li").count() <= 9
    playwright.expect(page.locator("#research-tiers li").first).to_contain_text("✓")
    playwright.expect(page.locator("#goals .goal-summary")).to_contain_text("earlier")
    assert page.locator("#goals .goal-node").count() <= 11
    assert page.locator(".thinking").evaluate("node => node.scrollHeight <= node.clientHeight")
    strip = page.locator("#research-strip").bounding_box()
    assert strip["x"] + strip["width"] <= page.locator(".top-context").first.bounding_box()["x"]
    assert not errors
