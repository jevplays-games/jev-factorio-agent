from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from jev_factorio import blocked_persistence as persistence
from jev_factorio.backends.mock import MockBackend
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.judgments import Decision
from jev_factorio.memory import CampaignMemory
from jev_factorio.skills import Plan, Step


SOURCE = {"commit": "2" * 40, "source_sha256": "c" * 64}


def _inputs(tick: int, *, inventory=2, receipt_item="iron-plate", quantity=1,
            deadline=1000, research_horizon=990):
    state = {
        "tick": tick,
        "session_id": "campaign-session",
        "inventory": {"iron-plate": inventory},
        "factory": {
            "deadline_tick": deadline,
            "research_deadline_tick": tick + research_horizon,
            "receipts": {"native:paid:iron-plate": {"quantity": 1, "tick": 7}},
        },
        "history": [{"kind": "blocked_recovery_wait", "tick": tick},
                    {"kind": "native_receipt", "tick": 7, "item": "iron-plate"}],
    }
    receipt = f"{tick}:factory_insert:utility:lab:{receipt_item}"
    plan = {
        "id": "factory:factory_insert:utility:lab",
        "goal": "rocket_launch",
        "steps": [{"action": "factory_insert", "item": receipt_item,
                   "threshold": 0, "timeout_ticks": 1800,
                   "parameters": {"role": "utility:lab", "item": receipt_item,
                                  "quantity": quantity, "receipt": receipt}}],
    }
    wait = {
        "id": f"background-wait:{tick}:factory_insert:utility:lab:iron-plate",
        "goal": "rocket_launch",
        "steps": [{"action": "factory_wait", "timeout_ticks": deadline - tick}],
    }
    return state, [plan, wait]


def _fingerprint(tick: int, **changes) -> str:
    state, plans = _inputs(tick, **changes)
    return persistence.decision_input_sha256(
        state, plans, session_id="campaign-session", source_revision=SOURCE,
        target="rocket_launch", policy="jev", confidence_floor=0.45,
        current_tick=tick)


def test_clock_derived_receipts_and_absolute_horizons_do_not_retrigger_selection():
    baseline = _fingerprint(10)
    assert _fingerprint(110) == baseline
    assert _fingerprint(1010) == baseline

    # A changed absolute horizon, paid receipt identity, quantity, or inventory
    # is decision evidence and must earn one new fingerprint.
    assert _fingerprint(110, deadline=1001) != baseline
    assert _fingerprint(110, research_horizon=989) != baseline
    assert _fingerprint(110, receipt_item="copper-plate") != baseline
    assert _fingerprint(110, quantity=2) != baseline
    assert _fingerprint(110, inventory=3) != baseline


def test_native_receipt_existence_and_item_identity_remain_in_fingerprint():
    state, plans = _inputs(10)
    before = persistence.decision_input_sha256(
        state, plans, session_id="campaign-session", source_revision=SOURCE,
        target="rocket_launch", policy="jev", confidence_floor=0.45,
        current_tick=10)
    state["factory"]["receipts"]["native:paid:copper-plate"] = {
        "quantity": 1, "tick": 10}
    after = persistence.decision_input_sha256(
        state, plans, session_id="campaign-session", source_revision=SOURCE,
        target="rocket_launch", policy="jev", confidence_floor=0.45,
        current_tick=10)
    assert after != before


def test_recompiled_craft_job_uuid_does_not_retrigger_unchanged_decision():
    from types import SimpleNamespace
    from jev_factorio.background import BackgroundWorkLoop
    from test_factory import recipe

    owner = SimpleNamespace(catalog=SimpleNamespace(recipes={
        "pipe": recipe("pipe", {"iron-plate": 1}),
    }))
    plan = Plan("craft-pipe", "steam_power", "Craft needed pipe", (
        Step("factory_craft", "inventory", "pipe", 1, costs={"iron-plate": 1},
             parameters={"recipe": "pipe", "batches": 1}),
    ))
    first = BackgroundWorkLoop._tracked_plan(owner, plan, None).to_dict()
    second = BackgroundWorkLoop._tracked_plan(owner, plan, None).to_dict()
    assert first["steps"][0]["parameters"]["receipt"] != second["steps"][0]["parameters"]["receipt"]

    def digest(candidate, observed=None):
        context = {"candidate_plans": {candidate["id"]: candidate}}
        if observed is not None:
            context["facts"] = {"factory": {"craft_job": observed}}
        return persistence.decision_input_sha256(
            context, [candidate], session_id="campaign-session", source_revision=SOURCE,
            target="steam_power", policy="jev", confidence_floor=.45, current_tick=10)

    assert digest(first) == digest(second)
    assert digest(first, first["steps"][0]) != digest(first, second["steps"][0])
    changed = deepcopy(second)
    changed["steps"][0]["parameters"]["batches"] = 2
    assert digest(changed) != digest(first)
    assert first != second  # Hashing must not replace the receipts used by dispatch.


def test_route_survey_cache_clock_churn_does_not_retrigger_selection():
    def inputs(tick, *, survey_tick, next_survey_tick, cached,
               receiver_count=0, reason="output_not_commissioned",
               source_unit=2546, resource_count=0, path_expansions=0,
               search_budget_exhausted=False, source_roles=None):
        state, plans = _inputs(tick)
        diagnostic = {
            "schema": 1, "survey_tick": survey_tick, "observed_tick": tick,
            "cached": cached, "next_survey_tick": next_survey_tick,
            "reason": reason, "receiver_count": receiver_count,
            "source_unit": source_unit, "resource_count": resource_count,
            "path_expansions": path_expansions,
            "search_budget_exhausted": search_budget_exhausted,
            "source_roles": source_roles or [],
        }
        factory = state.pop("factory")
        factory["input_routes"] = {
            "protocol": 1, "tick": tick,
            "diagnostics": {"recipe:iron-plate": dict(diagnostic)},
        }
        state["facts"] = {"factory": factory}
        plans[0]["materials"] = {
            "route_diagnostics": {"recipe:iron-plate": dict(diagnostic)},
        }
        state["candidate_plans"] = {plans[0]["id"]: deepcopy(plans[0])}
        return state, plans

    def digest_for(tick, **options):
        state, plans = inputs(tick, **options)
        return persistence.decision_input_sha256(
            state, plans, session_id="campaign-session", source_revision=SOURCE,
            target="rocket_launch", policy="jev", confidence_floor=0.45,
            current_tick=tick)

    before = digest_for(100, survey_tick=90, next_survey_tick=390, cached=True)
    after_refresh = digest_for(400, survey_tick=400, next_survey_tick=700, cached=False)
    assert after_refresh == before

    # A new route result or limit condition is real decision evidence.
    assert digest_for(400, survey_tick=400, next_survey_tick=700, cached=False,
                      receiver_count=1) != before
    assert digest_for(400, survey_tick=400, next_survey_tick=700, cached=False,
                      reason="route_search_budget_exhausted") != before
    assert digest_for(400, survey_tick=400, next_survey_tick=700, cached=False,
                      source_unit=2547) != before
    assert digest_for(400, survey_tick=400, next_survey_tick=700, cached=False,
                      resource_count=1) != before
    assert digest_for(400, survey_tick=400, next_survey_tick=700, cached=False,
                      path_expansions=1) != before
    assert digest_for(400, survey_tick=400, next_survey_tick=700, cached=False,
                      search_budget_exhausted=True) != before
    assert digest_for(400, survey_tick=400, next_survey_tick=700, cached=False,
                      source_roles=["recipe:iron-plate"]) != before


def test_route_cache_clock_keys_remain_semantic_outside_route_diagnostics():
    state, plans = _inputs(100)
    state["planner_cache"] = {"cached": True, "survey_tick": 90,
                              "next_survey_tick": 390}
    before = persistence.decision_input_sha256(
        state, plans, session_id="campaign-session", source_revision=SOURCE,
        target="rocket_launch", policy="jev", confidence_floor=0.45,
        current_tick=100)
    state["planner_cache"]["cached"] = False
    after = persistence.decision_input_sha256(
        state, plans, session_id="campaign-session", source_revision=SOURCE,
        target="rocket_launch", policy="jev", confidence_floor=0.45,
        current_tick=100)
    assert after != before


def test_route_diagnostic_clock_keys_are_ignored_only_at_known_route_paths():
    state, plans = _inputs(100)
    diagnostic = {"cached": True, "survey_tick": 90, "next_survey_tick": 390,
                  "source_unit": 2546}
    state["untrusted_route_diagnostics"] = {"recipe:iron-plate": dict(diagnostic)}
    state["candidate_plans"] = {
        plans[0]["id"]: {"metadata": {
            "route_diagnostics": {"recipe:iron-plate": dict(diagnostic)},
        }},
    }
    before = persistence.decision_input_sha256(
        state, plans, session_id="campaign-session", source_revision=SOURCE,
        target="rocket_launch", policy="jev", confidence_floor=0.45,
        current_tick=100)
    state["untrusted_route_diagnostics"]["recipe:iron-plate"].update(
        cached=False, survey_tick=100, next_survey_tick=400)
    state["candidate_plans"][plans[0]["id"]]["metadata"][
        "route_diagnostics"]["recipe:iron-plate"].update(
            cached=False, survey_tick=100, next_survey_tick=400)
    after = persistence.decision_input_sha256(
        state, plans, session_id="campaign-session", source_revision=SOURCE,
        target="rocket_launch", policy="jev", confidence_floor=0.45,
        current_tick=100)
    assert after != before


def test_archive_commit_bookkeeping_alone_does_not_authorize_a_model_retry():
    state, plans = _inputs(100)

    def digest(value):
        return persistence.decision_input_sha256(
            value, plans, session_id="campaign-session", source_revision=SOURCE,
            target="rocket_launch", policy="jev", confidence_floor=0.45,
            current_tick=100)

    baseline = digest(state)
    state["history"].append({
        "kind": "blocked_recovery_archive_committed",
        "head_sha256": "a" * 64, "archived_entries": 1024,
    })
    assert digest(state) == baseline


def test_recovery_ledger_is_durable_unique_and_fail_closed_on_unseeded_block(tmp_path):
    memory = CampaignMemory("campaign-session", "rocket_launch", status="blocked",
                            reason="low choice confidence", stalled_decisions=5)
    with pytest.raises(ValueError, match="prior durable recovery attempt"):
        persistence.validate_memory_state(memory, SOURCE)

    persistence.record_attempt(memory, SOURCE, "a" * 64, memory.reason, 90)
    path = tmp_path / "checkpoint.json"
    memory.save(path)
    restored = CampaignMemory.load(path, memory.session_id, memory.target)
    persistence.validate_memory_state(restored, SOURCE)
    assert restored.stalled_decisions == 5
    assert restored.blocked_recovery["attempts"] == memory.blocked_recovery["attempts"]
    assert persistence.was_attempted(restored, SOURCE, "a" * 64)
    with pytest.raises(ValueError, match="already attempted"):
        persistence.record_attempt(restored, SOURCE, "a" * 64, memory.reason, 91)


def test_changed_source_requires_explicit_one_use_authorization():
    memory = CampaignMemory("campaign-session", "rocket_launch", status="blocked",
                            reason="Candidate evidence insufficient", stalled_decisions=4)
    persistence.record_attempt(memory, SOURCE, "a" * 64, memory.reason, 90)
    newer = {"commit": "3" * 40, "source_sha256": "d" * 64}
    with pytest.raises(ValueError, match="source changed"):
        persistence.validate_memory_state(memory, newer)
    persistence.validate_memory_state(memory, newer, allow_source_change=True)
    assert not persistence.was_attempted(memory, newer, "b" * 64,
                                         allow_source_change=True)
    assert memory.blocked_recovery["source_revision"] == SOURCE
    persistence.record_attempt(memory, newer, "b" * 64, memory.reason, 91,
                               allow_source_change=True)
    assert memory.blocked_recovery["source_revision"] == newer
    assert len(memory.blocked_recovery["attempts"]) == 2


def test_wait_backoff_caps_at_five_minutes_and_keeps_blocked_status():
    memory = CampaignMemory("campaign-session", "rocket_launch", status="blocked",
                            reason="low choice confidence", stalled_decisions=5)
    persistence.record_attempt(memory, SOURCE, "a" * 64, memory.reason, 90)
    delays = []
    for _ in range(12):
        delays.append(persistence.record_wait(memory, SOURCE, "a" * 64))
    assert delays[:4] == [4.0, 8.0, 16.0, 32.0]
    assert delays[-1] == 300.0
    assert memory.status == "blocked" and memory.stalled_decisions == 5


class LiveMockBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.actions = []
        self.observations = 0

    def observe(self):
        self.observations += 1
        return replace(super().observe(), world_kind="fle")

    def act(self, action):
        self.actions.append(action)
        return super().act(action)


class LiveClient:
    model = "selection-test-double"
    is_mock = False
    uses_http_provider = False


def test_persistent_controller_skips_same_tick_only_retry_and_retries_changed_inventory(
        tmp_path, monkeypatch):
    import jev_factorio.controller as controller

    monkeypatch.setattr(controller, "gameplay_context", lambda: {"code_revision": SOURCE})
    backend = LiveMockBackend()
    checkpoint = tmp_path / "checkpoint.json"
    memory = CampaignMemory(backend.session_id, "bootstrap_mining",
                            active_goal="bootstrap_mining", last_tick=0,
                            status="blocked", reason="low choice confidence",
                            stalled_decisions=5, failures={"old-plan": 2},
                            history=[{"kind": "preserved", "marker": "history"}])
    persistence.record_attempt(memory, SOURCE, "f" * 64, memory.reason, 0)
    memory.save(checkpoint)
    loop = HierarchicalLoop(
        backend, jev=LiveClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True)
    if loop._safety is not None:
        loop._safety.admission = lambda *_args: None
    loop._work_candidates = lambda snapshot: ([Plan(
        "lab-insert", "bootstrap_mining", "Insert a paid lab item",
        (Step("factory_insert", "transfer", parameters={
            "role": "utility:lab", "item": "iron-plate", "quantity": 1,
            "receipt": f"{snapshot.tick}:factory_insert:utility:lab:iron-plate",
        }),))], "")

    calls = []

    def reject(_client, _state, plans, *_args, **_kwargs):
        calls.append(plans[0].id)
        return Decision(None, "observe", "low choice confidence", model_called=True,
                        diagnostics={"schema": 1, "outcome": "all_candidates_rejected"})

    monkeypatch.setattr(controller, "select_plan", reject)
    first = loop.step()
    assert first["status"] == "blocked" and first["model_call"] is True
    assert loop.memory.stalled_decisions == 6
    assert len(loop.memory.blocked_recovery["attempts"]) == 2
    assert backend.actions == []

    # The next observation has a different Factorio tick and a correspondingly
    # new planned receipt, but no changed game fact. It must not buy a call.
    backend.tick = 100
    waiting = loop.step()
    assert waiting["persistent_recovery"]["phase"] == "alternatives_exhausted_waiting"
    assert waiting["model_call"] is False
    assert len(calls) == 1
    assert loop.memory.stalled_decisions == 6

    # New native inventory is meaningful evidence and earns one durable attempt.
    backend.tick = 101
    backend.inv["iron-ore"] = backend.inv.get("iron-ore", 0) + 1
    changed = loop.step()
    assert changed["model_call"] is True
    assert len(calls) == 2
    assert len(loop.memory.blocked_recovery["attempts"]) == 3
    assert loop.memory.stalled_decisions == 7
    assert loop.memory.failures == {"old-plan": 2}
    assert loop.memory.history[0] == {"kind": "preserved", "marker": "history"}
    saved = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    assert len(saved.blocked_recovery["attempts"]) == 3
    assert saved.status == "blocked" and saved.stalled_decisions == 7


def test_persistent_controller_waits_on_route_cache_churn_then_allows_one_route_change(
        tmp_path, monkeypatch):
    import jev_factorio.controller as controller

    monkeypatch.setattr(controller, "gameplay_context", lambda: {"code_revision": SOURCE})
    backend = LiveMockBackend()
    route = {"cached": True, "survey_tick": 0, "next_survey_tick": 300,
             "receiver_count": 0, "reason": "output_not_commissioned",
             "source_unit": 2546, "resource_count": 0, "path_expansions": 0,
             "search_budget_exhausted": False, "source_roles": []}
    observe = backend.observe

    def observe_with_route_diagnostics():
        snapshot = observe()
        diagnostic = {"schema": 1, "observed_tick": snapshot.tick, **route}
        snapshot.factory["input_routes"] = {
            "protocol": 1, "tick": snapshot.tick,
            "diagnostics": {"recipe:iron-plate": dict(diagnostic)},
        }
        return snapshot

    backend.observe = observe_with_route_diagnostics
    checkpoint = tmp_path / "checkpoint.json"
    memory = CampaignMemory(backend.session_id, "bootstrap_mining",
                            active_goal="bootstrap_mining", last_tick=0,
                            status="blocked", reason="low choice confidence",
                            stalled_decisions=5)
    persistence.record_attempt(memory, SOURCE, "f" * 64, memory.reason, 0)
    memory.save(checkpoint)
    loop = HierarchicalLoop(
        backend, jev=LiveClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True)
    if loop._safety is not None:
        loop._safety.admission = lambda *_args: None

    def candidates(snapshot):
        diagnostic = {"schema": 1, "observed_tick": snapshot.tick, **route}
        return ([Plan(
            "same-plan", "bootstrap_mining", "Same candidate",
            (Step("walk_to_iron", "near", "iron-ore"),),
            materials={"route_diagnostics": {
                "recipe:iron-plate": dict(diagnostic),
            }})], "")

    loop._work_candidates = candidates
    calls = []

    def reject(_client, _state, plans, *_args, **_kwargs):
        calls.append(plans[0].id)
        return Decision(None, "observe", "low choice confidence", model_called=True,
                        diagnostics={"schema": 1, "outcome": "all_candidates_rejected"})

    monkeypatch.setattr(controller, "select_plan", reject)
    first = loop.step()
    assert first["model_call"] is True
    first_fingerprint = loop.memory.blocked_recovery["attempts"][-1][
        "decision_input_sha256"]
    assert len(calls) == 1

    # Cache expiry/refresh changes only diagnostic clocks and its cached flag.
    backend.tick = 100
    route.update(cached=False, survey_tick=100, next_survey_tick=400)
    cache_only = loop.step()
    assert cache_only["persistent_recovery"]["phase"] == \
        "alternatives_exhausted_waiting"
    assert cache_only["model_call"] is False
    assert len(calls) == 1

    # A changed route result is semantic decision evidence and earns one call.
    backend.tick = 101
    route.update(receiver_count=1, reason="receiver_available")
    changed = loop.step()
    assert changed["model_call"] is True
    second_fingerprint = loop.memory.blocked_recovery["attempts"][-1][
        "decision_input_sha256"]
    assert second_fingerprint != first_fingerprint
    assert len(calls) == 2

    # The next cache refresh with the same route result does not buy another call.
    backend.tick = 102
    route.update(cached=True, survey_tick=102, next_survey_tick=402)
    refreshed = loop.step()
    assert refreshed["persistent_recovery"]["phase"] == \
        "alternatives_exhausted_waiting"
    assert refreshed["model_call"] is False
    assert len(calls) == 2
    assert len(loop.memory.blocked_recovery["attempts"]) == 3


def test_running_selection_exhaustion_is_durably_blocked_before_wait(
        tmp_path, monkeypatch):
    import jev_factorio.controller as controller

    monkeypatch.setattr(controller, "gameplay_context", lambda: {"code_revision": SOURCE})
    backend = LiveMockBackend()
    checkpoint = tmp_path / "checkpoint.json"
    memory = CampaignMemory(backend.session_id, "bootstrap_mining",
                            active_goal="bootstrap_mining", last_tick=0,
                            status="running", stalled_decisions=3,
                            failures={"old-plan": 2})
    memory.save(checkpoint)
    loop = HierarchicalLoop(
        backend, jev=LiveClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True)
    if loop._safety is not None:
        loop._safety.admission = lambda *_args: None
    loop._work_candidates = lambda snapshot: ([Plan(
        "lab-insert", "bootstrap_mining", "Insert a paid lab item",
        (Step("factory_insert", "transfer", parameters={
            "role": "utility:lab", "item": "iron-plate", "quantity": 1,
            "receipt": f"{snapshot.tick}:factory_insert:utility:lab:iron-plate",
        }),))], "")
    calls = []

    def reject(_client, _state, plans, *_args, **_kwargs):
        calls.append(plans[0].id)
        return Decision(None, "observe", "low choice confidence", model_called=True,
                        diagnostics={"schema": 1, "outcome": "all_candidates_rejected"})

    monkeypatch.setattr(controller, "select_plan", reject)
    first = loop.step()
    assert first["status"] == "blocked" and first["model_call"] is True
    assert first["persistent_recovery"]["phase"] == "alternatives_exhausted_waiting"
    assert loop.memory.stalled_decisions == 4
    assert len(loop.memory.blocked_recovery["attempts"]) == 1
    assert loop.memory.failures == {"old-plan": 2}

    # The first eligible blocked request was ordinary while status was running.
    # Its exact rejected input is still saved before the next observation cycle.
    backend.tick = 100
    waiting = loop.step()
    assert waiting["persistent_recovery"]["phase"] == "alternatives_exhausted_waiting"
    assert waiting["model_call"] is False
    assert len(calls) == 1
    saved = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    assert len(saved.blocked_recovery["attempts"]) == 1
    assert saved.status == "blocked" and saved.stalled_decisions == 4


def test_first_running_request_is_write_ahead_and_never_replayed_after_crash(
        tmp_path, monkeypatch):
    import jev_factorio.controller as controller

    monkeypatch.setattr(controller, "gameplay_context", lambda: {"code_revision": SOURCE})
    backend = LiveMockBackend()
    checkpoint = tmp_path / "checkpoint.json"
    memory = CampaignMemory(backend.session_id, "bootstrap_mining",
                            active_goal="bootstrap_mining", last_tick=0,
                            status="running", reason="prior running reason",
                            stalled_decisions=0,
                            history=[{"kind": "preserved", "marker": "pre-crash"}])
    memory.save(checkpoint)

    def make_loop():
        loop = HierarchicalLoop(
            backend, jev=LiveClient(), policy="jev", target="bootstrap_mining",
            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
            persist_recoverable_blocks=True)
        if loop._safety is not None:
            loop._safety.admission = lambda *_args: None
        loop._work_candidates = lambda snapshot: ([Plan(
            "lab-insert", "bootstrap_mining", "Insert a paid lab item",
            (Step("factory_insert", "transfer", parameters={
                "role": "utility:lab", "item": "iron-plate", "quantity": 1,
                "receipt": f"{snapshot.tick}:factory_insert:utility:lab:iron-plate",
            }),))], "")
        return loop

    loop = make_loop()
    calls = []

    def crash_after_write_ahead(_client, _state, _plans, *_args, **_kwargs):
        calls.append("first")
        saved = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
        assert saved.status == "running" and saved.reason == "prior running reason"
        assert saved.stalled_decisions == 0
        assert saved.blocked_recovery["attempts"][-1]["outcome"] == "pending"
        raise RuntimeError("simulated process interruption after request start")

    monkeypatch.setattr(controller, "select_plan", crash_after_write_ahead)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        loop.step()

    # A new process sees the durable pending fingerprint. It observes again,
    # but does not repeat a provider call whose answer may have been lost.
    resumed = make_loop()

    def forbidden_replay(*_args, **_kwargs):
        calls.append("replayed")
        raise AssertionError("same-input provider request was replayed")

    monkeypatch.setattr(controller, "select_plan", forbidden_replay)
    record = resumed.step()
    assert calls == ["first"]
    assert record["status"] == "running"
    assert record["reason"] == "prior running reason"
    assert record["outcome"] == "Decision outcome unresolved; observing for changed evidence"
    assert record["persistent_recovery"]["phase"] == "evaluation_outcome_unknown_waiting"
    assert record["persistent_recovery"]["model_call"] is False
    assert resumed.memory.stalled_decisions == 0
    assert resumed.memory.history[0] == {"kind": "preserved", "marker": "pre-crash"}

    # A genuinely changed native fact makes a new request eligible even though
    # the earlier response remains unknown; its exact old input is never replayed.
    backend.tick = 101
    backend.inv["iron-ore"] = backend.inv.get("iron-ore", 0) + 1

    def reject_changed_input(_client, _state, _plans, *_args, **_kwargs):
        calls.append("changed")
        return Decision(None, "observe", "low choice confidence", model_called=True,
                        diagnostics={"schema": 1, "outcome": "all_candidates_rejected"})

    monkeypatch.setattr(controller, "select_plan", reject_changed_input)
    changed = resumed.step()
    assert calls == ["first", "changed"]
    assert changed["status"] == "blocked"
    assert len(resumed.memory.blocked_recovery["attempts"]) == 2
    assert resumed.memory.blocked_recovery["attempts"][0]["outcome"] == "pending"
    assert resumed.memory.blocked_recovery["attempts"][1]["outcome"] == "rejected"


def test_running_threshold_checkpoint_failure_stops_before_model_call(tmp_path, monkeypatch):
    import jev_factorio.controller as controller

    monkeypatch.setattr(controller, "gameplay_context", lambda: {"code_revision": SOURCE})
    backend = LiveMockBackend()
    checkpoint = tmp_path / "checkpoint.json"
    memory = CampaignMemory(backend.session_id, "bootstrap_mining",
                            active_goal="bootstrap_mining", last_tick=0,
                            status="running", reason="prior running reason",
                            stalled_decisions=3)
    memory.save(checkpoint)
    original_bytes = checkpoint.read_bytes()
    loop = HierarchicalLoop(
        backend, jev=LiveClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True)
    loop._work_candidates = lambda snapshot: ([Plan(
        "lab-insert", "bootstrap_mining", "Insert a paid lab item",
        (Step("factory_insert", "transfer", parameters={
            "role": "utility:lab", "item": "iron-plate", "quantity": 1,
            "receipt": f"{snapshot.tick}:factory_insert:utility:lab:iron-plate",
        }),))], "")
    calls = []

    def forbidden_call(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("selection must follow the durable write-ahead save")

    monkeypatch.setattr(controller, "select_plan", forbidden_call)

    def fail_save(_self, _path):
        raise OSError("injected pre-request checkpoint failure")

    monkeypatch.setattr(CampaignMemory, "save", fail_save)
    with pytest.raises(OSError, match="pre-request checkpoint failure"):
        loop.step()

    assert calls == []
    assert checkpoint.read_bytes() == original_bytes
    assert loop.memory.status == "running"
    assert loop.memory.reason == "prior running reason"
    assert loop.memory.stalled_decisions == 3
    assert loop.memory.blocked_recovery["attempts"] == []


def test_until_complete_waits_for_changed_evidence_and_stops_on_interrupt(tmp_path, monkeypatch):
    import jev_factorio.controller as controller
    import jev_factorio.loop as loop_module

    monkeypatch.setattr(controller, "gameplay_context", lambda: {"code_revision": SOURCE})
    backend = LiveMockBackend()
    checkpoint = tmp_path / "checkpoint.json"
    memory = CampaignMemory(backend.session_id, "bootstrap_mining",
                            active_goal="bootstrap_mining", last_tick=0,
                            status="blocked", reason="Candidate evidence insufficient",
                            stalled_decisions=5)
    persistence.record_attempt(memory, SOURCE, "e" * 64, memory.reason, 0)
    memory.save(checkpoint)
    loop = HierarchicalLoop(
        backend, jev=LiveClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True)
    if loop._safety is not None:
        loop._safety.admission = lambda *_args: None
    loop._work_candidates = lambda snapshot: ([Plan(
        "same-plan", "bootstrap_mining", "Same candidate",
        (Step("walk_to_iron", "near", "iron-ore"),))], "")
    requests = []

    def reject(*_args, **_kwargs):
        requests.append(True)
        return Decision(None, "observe", "Candidate evidence insufficient",
                        model_called=True,
                        diagnostics={"schema": 1, "outcome": "all_candidates_rejected"})

    monkeypatch.setattr(controller, "select_plan", reject)
    waits = []

    def interrupt_after_observation_cycle(delay):
        waits.append(delay)
        if len(waits) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(loop_module, "_interruptible_sleep", interrupt_after_observation_cycle)
    with pytest.raises(KeyboardInterrupt):
        loop.run(until_complete=True)

    assert backend.observations == 2
    assert requests == [True]
    assert waits == [2.0, 4.0]
    assert loop.memory.status == "blocked" and loop.memory.stalled_decisions == 6
    assert loop.memory.active_plan is None and backend.actions == []


def test_persistent_selection_enters_normal_verified_execution_without_reset_shortcut(
        tmp_path, monkeypatch):
    import jev_factorio.controller as controller

    monkeypatch.setattr(controller, "gameplay_context", lambda: {"code_revision": SOURCE})
    backend = LiveMockBackend()
    checkpoint = tmp_path / "checkpoint.json"
    memory = CampaignMemory(backend.session_id, "bootstrap_mining",
                            active_goal="bootstrap_mining",
                            completed_goals={"stockpile_fuel": 0}, last_tick=0,
                            status="blocked", reason="low choice confidence",
                            stalled_decisions=5, failures={"old-plan": 2})
    persistence.record_attempt(memory, SOURCE, "d" * 64, memory.reason, 0)
    memory.save(checkpoint)
    loop = HierarchicalLoop(
        backend, jev=LiveClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True)
    if loop._safety is not None:
        loop._safety.admission = lambda *_args: None
    plan = Plan("walk-to-iron", "bootstrap_mining", "Approach iron ore",
                (Step("walk_to_iron", "near", "iron-ore"),))
    loop._work_candidates = lambda _snapshot: ([plan], "")
    selections = []

    def choose(_client, _state, plans, *_args, **_kwargs):
        selections.append(plans[0].id)
        return Decision(plans[0].id, "jev", model_called=True,
                        diagnostics={"schema": 1, "outcome": "selected"})

    monkeypatch.setattr(controller, "select_plan", choose)
    record = loop.step()

    assert selections == [plan.id]
    assert backend.actions == ["walk_to_iron"]
    assert record["verified"] is True
    assert loop.memory.status == "running"
    assert loop.memory.active_plan is None and loop.memory.pending is None
    assert loop.memory.stalled_decisions == 0  # Only the ordinary verified receipt clears it.
    assert loop.memory.failures == {"old-plan": 2}
    saved = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    assert saved.blocked_recovery["attempts"][-1]["reason"] == "low choice confidence"
    assert saved.attempt_outcomes[-1]["outcome"] == "verified"


def test_persistent_mode_needs_a_continuous_live_resume_configuration():
    from dataclasses import asdict
    from jev_factorio.research_log import RunConfiguration, _configuration

    valid = RunConfiguration(
        backend="fle", controller="hierarchical", policy="jev", target="rocket_launch",
        resume=True, resume_controller=True, checkpoint_enabled=True, until_complete=True,
        persist_recoverable_blocks=True, persistent_idle_observations=6)
    _configuration(asdict(valid))
    with pytest.raises(ValueError, match="Persistent blocked recovery"):
        _configuration(asdict(replace(valid, until_complete=False)))
    with pytest.raises(ValueError, match="Persistent blocked recovery"):
        _configuration(asdict(replace(valid, backend="mock")))


_DEFAULT_IDLE_ARGUMENT = object()

def _idle_loop(tmp_path, monkeypatch, *, idle_observations=_DEFAULT_IDLE_ARGUMENT, backend=None, log_file=None,
               rejection_reason="Candidate evidence insufficient"):
    import jev_factorio.controller as controller

    monkeypatch.setattr(controller, "gameplay_context", lambda: {"code_revision": SOURCE})
    backend = backend or LiveMockBackend()
    checkpoint = tmp_path / "checkpoint.json"
    if not checkpoint.exists():
        memory = CampaignMemory(backend.session_id, "bootstrap_mining",
                                active_goal="bootstrap_mining", last_tick=0,
                                status="blocked", reason=rejection_reason,
                                stalled_decisions=5)
        persistence.record_attempt(memory, SOURCE, "e" * 64, memory.reason, 0)
        memory.save(checkpoint)
    loop = HierarchicalLoop(
        backend, jev=LiveClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True,
        **({"persistent_idle_observations": idle_observations} if idle_observations is not _DEFAULT_IDLE_ARGUMENT else {}),
        log_file=log_file)
    if loop._safety is not None:
        loop._safety.admission = lambda *_args: None
    current = {"id": "same-plan"}
    loop._work_candidates = lambda snapshot: ([Plan(
        current["id"], "bootstrap_mining", "Same candidate",
        (Step("walk_to_iron", "near", "iron-ore"),))], "")
    requests = []

    def reject(*_args, **_kwargs):
        requests.append(True)
        return Decision(None, "observe", rejection_reason,
                        model_called=True,
                        diagnostics={"schema": 1, "outcome": "all_candidates_rejected"})

    monkeypatch.setattr(controller, "select_plan", reject)
    return loop, backend, checkpoint, requests, current


def test_idle_wait_bound_ends_the_invocation_and_preserves_the_blocked_checkpoint(
        tmp_path, monkeypatch):
    loop, backend, checkpoint, requests, _ = _idle_loop(
        tmp_path, monkeypatch, idle_observations=3)
    waits = []
    # Drive a bounded number of observations directly. If the persistent idle
    # counter ever stops advancing, this regression fails instead of spinning
    # and repeatedly writing the checkpoint forever.
    for _ in range(16):
        loop.step()
        if loop.terminal:
            break
        waits.append(loop.persistent_recovery_wait_seconds())
    else:
        pytest.fail("persistent idle bound did not terminate within 16 observations")

    assert loop.terminal is True
    assert requests == [True] and backend.actions == []
    assert waits[:8] == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0]
    assert all(delay >= 256.0 for delay in waits[7:])
    # The exhausting observation ends the run before another sleep.
    assert len(waits) == 9
    assert loop.memory.status == "blocked"
    assert loop.memory.reason == "Candidate evidence insufficient"
    assert loop.memory.stalled_decisions == 6
    assert loop.memory.active_plan is None and loop.memory.pending is None
    assert loop._persistent_recovery_status["phase"] == "idle_wait_exhausted"
    assert loop._persistent_recovery_status["model_call"] is False
    assert loop.persistent_recovery_wait_seconds() == 0.0

    saved = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    assert saved.status == "blocked" and saved.stalled_decisions == 6
    assert saved.reason == "Candidate evidence insufficient"
    assert len(saved.blocked_recovery["attempts"]) == 2


def test_idle_bound_is_process_local_so_a_restart_waits_again(tmp_path, monkeypatch):
    loop, backend, checkpoint, requests, _ = _idle_loop(
        tmp_path, monkeypatch, idle_observations=2)
    for _ in range(16):
        loop.step()
        if loop.terminal:
            break
    else:
        pytest.fail("persistent idle bound did not terminate within 16 observations")
    assert loop.terminal is True

    restarted, _, _, more_requests, _ = _idle_loop(
        tmp_path, monkeypatch, idle_observations=2, backend=backend)
    restarted.step()
    assert restarted.terminal is False
    assert more_requests == []  # the unchanged fingerprint is already recorded; no new bill


def test_zero_idle_observations_disables_the_bound(tmp_path, monkeypatch):
    import jev_factorio.loop as loop_module

    loop, _, _, requests, _ = _idle_loop(tmp_path, monkeypatch, idle_observations=0)
    waits = []

    def stop_after_many(delay):
        waits.append(delay)
        if len(waits) == 25:
            raise KeyboardInterrupt

    monkeypatch.setattr(loop_module, "_interruptible_sleep", stop_after_many)
    with pytest.raises(KeyboardInterrupt):
        loop.run(until_complete=True)
    assert loop.terminal is False
    assert waits[-1] == 300.0 and requests == [True]


def test_changed_evidence_resets_the_idle_count_and_bills_one_new_decision(
        tmp_path, monkeypatch):
    loop, _, _, requests, current = _idle_loop(tmp_path, monkeypatch, idle_observations=3)
    for _ in range(9):
        loop.step()
        assert loop.terminal is False
    assert loop._persistent_idle_waits == 2 and requests == [True]

    current["id"] = "different-plan"  # a changed decision fingerprint
    loop.step()
    assert requests == [True, True]
    assert loop._persistent_idle_waits == 0
    assert loop.terminal is False


def test_unresolved_decision_outcome_is_never_abandoned_by_the_idle_bound(
        tmp_path, monkeypatch):
    loop, _, checkpoint, requests, _ = _idle_loop(tmp_path, monkeypatch, idle_observations=1)
    loop.step()
    loop.memory.blocked_recovery["attempts"][-1]["outcome"] = "pending"
    loop.memory.blocked_recovery["wait_level"] = 9
    for _ in range(4):
        loop.step()
        assert loop.terminal is False
        assert loop._persistent_recovery_status["phase"] == "evaluation_outcome_unknown_waiting"
    assert loop._persistent_idle_waits == 0


@pytest.mark.parametrize("bad", [-1, 1001, True, 6.0, "6", None])
def test_idle_observation_bound_is_validated(tmp_path, monkeypatch, bad):
    with pytest.raises(ValueError, match="idle observations"):
        _idle_loop(tmp_path, monkeypatch, idle_observations=bad)


def test_large_tick_seconds_does_not_make_early_waits_count_as_idle(tmp_path, monkeypatch):
    loop, _, _, _, _ = _idle_loop(tmp_path, monkeypatch, idle_observations=2)
    loop.tick_seconds = 500.0
    for _ in range(6):
        loop.step()
        assert loop.terminal is False
    assert loop._persistent_idle_waits == 0


@pytest.mark.parametrize(("job", "attempt", "waits"), [
    (None, None, True),
    ({"receipt": "r"}, {"id": "a"}, True),    # tracked, verifiable craft job
    ({"receipt": "r"}, None, False),          # half a record is ambiguous
    (None, {"id": "a"}, False),
])
def test_blocked_wait_survives_a_tracked_background_job_but_not_half_a_record(
        tmp_path, monkeypatch, job, attempt, waits):
    loop, _, _, requests, _ = _idle_loop(tmp_path, monkeypatch, idle_observations=0)
    loop.step()
    assert loop.memory.status == "blocked" and requests == [True]
    loop.memory.background_job, loop.memory.background_attempt = job, attempt
    assert loop._persistent_block_active() is waits
    assert loop.terminal is (not waits)


def test_pending_attempt_or_plan_still_ends_the_wait_with_a_tracked_job(tmp_path, monkeypatch):
    loop, _, _, _, _ = _idle_loop(tmp_path, monkeypatch, idle_observations=0)
    loop.step()
    loop.memory.background_job, loop.memory.background_attempt = {"r": 1}, {"id": "a"}
    assert loop._persistent_block_active() is True
    loop.memory.attempt = {"id": "in-flight"}
    assert loop._persistent_block_active() is False


def test_blocked_persistent_run_commits_a_lone_passive_wait_without_a_model_call(
        tmp_path, monkeypatch):
    import jev_factorio.controller as controller
    from jev_factorio import judgments

    loop, backend, checkpoint, requests, _ = _idle_loop(
        tmp_path, monkeypatch, idle_observations=0)
    monkeypatch.setattr(controller, "select_plan", judgments.select_plan)

    def no_model(*_args, **_kwargs):
        raise AssertionError("a lone passive wait must not call the model")

    monkeypatch.setattr(loop.jev, "evaluate", no_model, raising=False)
    wait = Plan("background-wait:1:factory_craft_job:iron-gear-wheel", "bootstrap_mining",
                "Observe the tracked native crafting queue",
                (Step("factory_wait", "crafting_idle", timeout_ticks=60),))
    loop._work_candidates = lambda snapshot: ([wait], "")

    loop.step()

    assert requests == []
    assert loop.memory.status != "blocked" and loop.terminal is False
    events = [e for e in loop.memory.history if e.get("kind") == "plan_committed"]
    assert events and events[-1]["plan"] == wait.id and events[-1]["source"] == "passive-wait"
    rows = loop.memory.blocked_recovery["attempts"]
    assert rows[-1]["outcome"] == "selected"  # recorded like any selection
    assert loop._decision.model_called is False


def _log_lines(path):
    import json
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_persistent_wait_log_deltas_round_trip_and_returns_stay_full(
        tmp_path, monkeypatch):
    from jev_factorio.acceptance_io import records
    from jev_factorio.wait_record_codec import MARKER

    log = tmp_path / "gameplay.jsonl"
    loop, _, _, requests, _ = _idle_loop(
        tmp_path, monkeypatch, idle_observations=0, log_file=str(log))
    loop._record_extras = lambda: {"buffer_evidence": {"pad": "e" * 6000}}
    returned = [loop.step() for _ in range(4)]
    wires = _log_lines(log)
    expanded = records(log.read_bytes())
    assert len(wires) == len(expanded) == 4 and requests == [True]

    decision_line, *wait_lines = wires
    assert MARKER not in decision_line
    assert decision_line["buffer_evidence"] == {"pad": "e" * 6000}
    assert decision_line["model_call"] is True

    for line in wait_lines:
        assert set(line) == {"_jev_lossless_wait_record"}
    for line in expanded[1:]:
        assert line["buffer_evidence"] == {"pad": "e" * 6000}
        assert isinstance(line["state"], dict) and isinstance(line["after_state"], dict)
        assert isinstance(line["performance"], dict)
        assert line["outcome"] == \
            "Blocked; all currently feasible alternatives were already evaluated"
        assert line["persistent_recovery"]["phase"] == "alternatives_exhausted_waiting"
        assert line["model_call"] is False

    # The object handed to in-process consumers such as the dashboard stays complete.
    assert returned[-1]["buffer_evidence"] == {"pad": "e" * 6000}
    assert "compact_record" not in returned[-1]
    assert expanded == returned


def test_idle_exhausted_delta_round_trips_and_a_restart_writes_a_full_anchor(
        tmp_path, monkeypatch):
    from jev_factorio.acceptance_io import records
    from jev_factorio.wait_record_codec import MARKER

    log = tmp_path / "gameplay.jsonl"
    loop, _, _, requests, current = _idle_loop(
        tmp_path, monkeypatch, idle_observations=2, log_file=str(log))
    loop._record_extras = lambda: {"buffer_evidence": {"pad": "e" * 6000}}
    for _ in range(40):
        loop.step()
        if loop.terminal:
            break
    assert loop.terminal is True
    last = records(log.read_bytes())[-1]
    assert last["persistent_recovery"]["phase"] == "idle_wait_exhausted"
    assert last["buffer_evidence"] == {"pad": "e" * 6000}

    # A fresh decision in a new process is logged in full.
    restarted, _, _, _, changed = _idle_loop(
        tmp_path, monkeypatch, idle_observations=2, backend=loop.backend, log_file=str(log))
    restarted._record_extras = loop._record_extras
    changed["id"] = "different-plan"
    restarted.step()
    newest = _log_lines(log)[-1]
    assert MARKER not in newest
    assert records(log.read_bytes())[-1]["buffer_evidence"] == {"pad": "e" * 6000}


def test_compact_flag_never_leaks_to_a_later_record_without_a_log_file(
        tmp_path, monkeypatch):
    loop, _, _, _, current = _idle_loop(tmp_path, monkeypatch, idle_observations=0)
    loop.step()
    loop.step()
    assert loop._compact_next_record is False


def test_compact_flag_is_cleared_when_a_record_wrapper_raises(tmp_path, monkeypatch):
    loop, _, _, _, _ = _idle_loop(tmp_path, monkeypatch, idle_observations=0)
    loop.step()

    def broken(*_args, **_kwargs):
        raise RuntimeError("wrapper failed before recording")

    loop._record = broken
    with pytest.raises(RuntimeError):
        loop.step()
    assert loop._compact_next_record is False


def _alternative_loop(tmp_path, monkeypatch, count, *, backend=None, jev=None):
    import jev_factorio.controller as controller
    import jev_factorio.judgments as judgments

    monkeypatch.setattr(controller, "gameplay_context", lambda: {"code_revision": SOURCE})
    backend = backend or LiveMockBackend()
    checkpoint = tmp_path / "alternative-checkpoint.json"
    memory = CampaignMemory(
        backend.session_id, "bootstrap_mining", active_goal="bootstrap_mining",
        last_tick=0, status="blocked", reason="low choice confidence",
        stalled_decisions=5, history=[{"kind": "retained", "marker": "before batches"}],
    )
    persistence.record_attempt(memory, SOURCE, "a" * 64, memory.reason, 0)
    memory.save(checkpoint)
    loop = HierarchicalLoop(
        backend, jev=jev or LiveClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True)
    if loop._safety is not None:
        loop._safety.admission = lambda *_args: None
    plans = [Plan(
        f"candidate-{index}", "bootstrap_mining", f"Candidate {index}",
        (Step("walk_to_iron", "near", "iron-ore"),),
    ) for index in range(count)]
    loop._work_candidates = lambda _snapshot: (plans, "")
    original_batch = judgments.question_batch

    def one_candidate(state, candidates, max_bytes=32000, max_candidates=16):
        return original_batch(state, candidates[:1], max_bytes=max_bytes, max_candidates=1)

    monkeypatch.setattr(judgments, "question_batch", one_candidate)
    return loop, backend, checkpoint, plans


@pytest.mark.parametrize(("provider_phase", "attempts"), [
    ("cooldown", 2), ("exhausted", 8),
])
def test_persistent_provider_block_is_durable_operator_handoff_and_not_replayed(
        tmp_path, monkeypatch, provider_phase, attempts):
    import jev_factorio.controller as controller
    from jev_factorio.provider_health import ProviderCircuit

    class NeverCalledClient:
        model = "test-provider-model"
        url = "https://provider.invalid/test"
        uses_http_provider = False
        last_usage = None
        last_model = None

        def __init__(self):
            self.calls = 0

        def evaluate(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("cooldown/exhausted provider must not be probed")

    client = NeverCalledClient()
    provider_path = tmp_path / "provider.json"
    circuit = ProviderCircuit(client, provider_path, clock=lambda: 10.0)
    circuit.state.update(
        phase=provider_phase, category="rate_limit", attempts=attempts,
        next_probe_at=100.0, incident_id="test-incident",
        first_failure_at=1.0, budget_category="rate_limit", budget_limit=8)
    circuit._save()
    provider_bytes = provider_path.read_bytes()

    loop, backend, checkpoint, _plans = _alternative_loop(
        tmp_path, monkeypatch, 2, jev=circuit)
    memory = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    memory.status = "running"
    memory.reason = "low choice confidence"
    memory.stalled_decisions = 4
    memory.failures = {"retained-plan": 2}
    memory.save(checkpoint)

    first = loop.step()
    assert first["status"] == "blocked"
    assert first["model_call"] is False
    assert first["persistent_recovery"]["phase"] == "provider_blocked"
    assert first["persistent_recovery"]["provider_category"] == "rate_limit"
    assert first["persistent_recovery"]["provider_phase"] == provider_phase
    assert "operator recovery" in first["reason"]
    assert loop.memory.stalled_decisions == 4
    assert loop.memory.failures == {"retained-plan": 2}
    assert loop.memory.blocked_recovery["attempts"][-1]["outcome"] == "provider_blocked"
    handoff = [event for event in loop.memory.history
               if event.get("kind") == "provider_circuit_operator_recovery_required"]
    assert len(handoff) == 1 and handoff[0]["model_called"] is False
    assert client.calls == 0 and backend.actions == []
    assert provider_path.read_bytes() == provider_bytes

    invalid_memory = deepcopy(loop.memory)
    invalid_memory.history = [event for event in invalid_memory.history
                              if event.get("kind") !=
                              "provider_circuit_operator_recovery_required"]
    with pytest.raises(ValueError, match="does not admit this blocked reason"):
        persistence.validate_memory_state(invalid_memory, SOURCE)
    invalid_checkpoint = json.loads(checkpoint.read_text(encoding="utf-8"))
    invalid_checkpoint["history"] = invalid_memory.history
    with pytest.raises(ValueError, match="does not admit this blocked reason"):
        persistence.validate_checkpoint_metadata(invalid_checkpoint, SOURCE)

    resumed = HierarchicalLoop(
        backend, jev=circuit, policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True)
    monkeypatch.setattr(controller, "select_plan",
                        lambda *_args, **_kwargs: pytest.fail("provider selection retried"))
    second = resumed.step()
    assert second["status"] == "blocked" and second["model_call"] is False
    assert resumed.terminal is True
    assert resumed.memory.stalled_decisions == 4
    assert resumed.memory.failures == {"retained-plan": 2}
    assert client.calls == 0 and backend.actions == []
    assert provider_path.read_bytes() == provider_bytes


def test_persistent_called_provider_failure_hands_off_after_wal_and_never_replays(
        tmp_path, monkeypatch):
    import requests
    import jev_factorio.controller as controller
    from jev_factorio.provider_health import ProviderCircuit

    backend = LiveMockBackend()
    checkpoint = tmp_path / "called-provider-checkpoint.json"
    provider_path = tmp_path / "called-provider.json"

    class RateLimitedClient:
        model = "test-provider-model"
        url = "https://provider.invalid/test"
        uses_http_provider = False
        last_usage = None
        last_model = None

        def __init__(self):
            self.calls = 0
            self.wal_seen_before_call = False

        def evaluate(self, *_args, **_kwargs):
            self.calls += 1
            saved = CampaignMemory.load(
                checkpoint, backend.session_id, "bootstrap_mining")
            pending = saved.blocked_recovery["attempts"][-1]
            provider = json.loads(provider_path.read_text(encoding="utf-8"))
            assert pending["outcome"] == "pending"
            assert "selection_batch" in pending
            assert provider["in_flight"] is not None
            self.wal_seen_before_call = True
            response = requests.Response()
            response.status_code = 429
            raise requests.HTTPError("bounded test rate limit", response=response)

    client = RateLimitedClient()
    circuit = ProviderCircuit(client, provider_path, clock=lambda: 10.0)
    loop, backend, checkpoint, _plans = _alternative_loop(
        tmp_path, monkeypatch, 2, backend=backend, jev=circuit)
    memory = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    memory.status = "running"
    memory.reason = "low choice confidence"
    memory.stalled_decisions = 4
    memory.failures = {"retained-plan": 2}
    memory.save(checkpoint)

    first = loop.step()
    assert client.wal_seen_before_call is True and client.calls == 1
    assert first["status"] == "blocked" and first["model_call"] is True
    assert first["decision"]["model_called"] is True
    assert first["persistent_recovery"]["phase"] == "provider_blocked"
    assert first["persistent_recovery"]["provider_category"] == "rate_limit"
    assert loop.memory.blocked_recovery["attempts"][-1]["outcome"] == "provider_blocked"
    assert loop.memory.stalled_decisions == 4
    assert loop.memory.failures == {"retained-plan": 2}
    assert backend.actions == []

    provider_bytes = provider_path.read_bytes()
    provider_state = json.loads(provider_bytes)
    assert provider_state["phase"] == "cooldown"
    assert provider_state["category"] == "rate_limit"
    assert provider_state["in_flight"] is None

    resumed = HierarchicalLoop(
        backend, jev=ProviderCircuit(client, provider_path, clock=lambda: 10.0),
        policy="jev", target="bootstrap_mining", checkpoint=str(checkpoint),
        resume_controller=True, tick_seconds=0, persist_recoverable_blocks=True)
    monkeypatch.setattr(controller, "select_plan",
                        lambda *_args, **_kwargs: pytest.fail("provider selection retried"))
    second = resumed.step()
    assert second["status"] == "blocked" and second["model_call"] is False
    assert resumed.terminal is True
    assert client.calls == 1 and backend.actions == []
    assert resumed.memory.stalled_decisions == 4
    assert resumed.memory.failures == {"retained-plan": 2}
    assert provider_path.read_bytes() == provider_bytes


def test_bounded_alternatives_are_writeahead_distinct_and_exhaust_once(tmp_path, monkeypatch):
    import jev_factorio.controller as controller

    loop, backend, checkpoint, _plans = _alternative_loop(tmp_path, monkeypatch, 3)
    offered_calls = []

    def reject(_client, _state, remaining, *_args, prepared_batch=None, **_kwargs):
        context, questions, offered = prepared_batch
        ids = [plan.id for plan in offered]
        assert len(ids) == 1 and ids[0] in {plan.id for plan in remaining}
        saved = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
        row = saved.blocked_recovery["attempts"][-1]
        assert row["outcome"] == "pending"
        assert [item["plan_id"] for item in row["selection_batch"]["offered"]] == ids
        assert set(context["candidate_plans"]) == set(ids)
        assert set(questions) >= {"candidate"}
        offered_calls.append(ids[0])
        return Decision(None, "observe", "Candidate evidence insufficient",
                        model_called=True,
                        diagnostics={"schema": 1, "outcome": "all_candidates_rejected"})

    monkeypatch.setattr(controller, "select_plan", reject)
    first = loop.step()
    assert offered_calls == ["candidate-0", "candidate-1", "candidate-2"]
    assert first["persistent_recovery"]["phase"] == "alternatives_exhausted_waiting"
    assert first["persistent_recovery"]["evaluated_batches"] == 3
    assert first["persistent_recovery"]["unseen_candidates"] == 0
    assert loop.memory.reason == "low choice confidence"
    assert loop.memory.stalled_decisions == 6
    attempts = loop.memory.blocked_recovery["attempts"]
    batches = [row for row in attempts if "selection_batch" in row]
    assert len(batches) == 3
    assert len({row["decision_input_sha256"] for row in batches}) == 3
    assert all(row["outcome"] == "rejected" for row in batches)
    assert len({row["selection_batch"]["state_sha256"] for row in batches}) == 1
    assert len({row["selection_batch"]["request_sha256"] for row in batches}) == 3
    assert sum(event.get("kind") == "blocked_recovery_alternatives_exhausted"
               for event in loop.memory.history) == 1
    assert backend.actions == []

    # Restarted unchanged state observes the durable exhausted frontier and
    # neither reoffers candidates nor duplicates the diagnostic event.
    restarted = HierarchicalLoop(
        backend, jev=LiveClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True)
    if restarted._safety is not None:
        restarted._safety.admission = lambda *_args: None
    restarted._work_candidates = lambda _snapshot: (_plans, "")
    second = restarted.step()
    assert offered_calls == ["candidate-0", "candidate-1", "candidate-2"]
    assert second["persistent_recovery"]["phase"] == "alternatives_exhausted_waiting"
    assert sum(event.get("kind") == "blocked_recovery_alternatives_exhausted"
               for event in restarted.memory.history) == 1


def test_alternative_batch_limit_is_not_reported_as_exhaustion(tmp_path, monkeypatch):
    import jev_factorio.controller as controller

    loop, backend, checkpoint, _plans = _alternative_loop(tmp_path, monkeypatch, 4)
    memory = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    memory.status = "running"
    memory.reason = ""
    memory.stalled_decisions = 0
    memory.save(checkpoint)
    loop.memory = memory
    calls = []

    def reject(_client, _state, _remaining, *_args, prepared_batch=None, **_kwargs):
        calls.append(prepared_batch[2][0].id)
        return Decision(None, "observe", "low choice confidence", model_called=True,
                        diagnostics={"schema": 1, "outcome": "low_choice_confidence"})

    monkeypatch.setattr(controller, "select_plan", reject)
    record = loop.step()
    assert calls == ["candidate-0", "candidate-1", "candidate-2"]
    assert record["persistent_recovery"]["phase"] == "alternative_batch_limit_waiting"
    assert record["persistent_recovery"]["evaluated_batches"] == 3
    assert record["persistent_recovery"]["unseen_candidates"] == 1
    assert loop.memory.status == "blocked"
    assert loop.memory.reason == "low choice confidence"
    assert loop.memory.stalled_decisions == 1
    assert not any(event.get("kind") == "blocked_recovery_alternatives_exhausted"
                   for event in loop.memory.history)
    assert sum(event.get("kind") == "blocked_recovery_alternative_batch_limit"
               for event in loop.memory.history) == 1


def test_pending_alternative_batch_stops_before_unseen_candidates_after_restart(
        tmp_path, monkeypatch):
    import jev_factorio.controller as controller

    loop, backend, checkpoint, plans = _alternative_loop(tmp_path, monkeypatch, 3)
    calls = []

    def crash_after_wal(_client, _state, _remaining, *_args, prepared_batch=None, **_kwargs):
        calls.append(prepared_batch[2][0].id)
        saved = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
        assert saved.blocked_recovery["attempts"][-1]["outcome"] == "pending"
        raise RuntimeError("simulated lost response")

    monkeypatch.setattr(controller, "select_plan", crash_after_wal)
    with pytest.raises(RuntimeError, match="simulated lost response"):
        loop.step()

    resumed = HierarchicalLoop(
        backend, jev=LiveClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True)
    if resumed._safety is not None:
        resumed._safety.admission = lambda *_args: None
    resumed._work_candidates = lambda _snapshot: (plans, "")
    monkeypatch.setattr(controller, "select_plan",
                        lambda *_args, **_kwargs: pytest.fail("pending batch was replayed"))
    record = resumed.step()
    assert calls == ["candidate-0"]
    assert record["persistent_recovery"]["phase"] == "evaluation_outcome_unknown_waiting"
    assert resumed.memory.blocked_recovery["attempts"][-1]["outcome"] == "pending"


def test_selection_batch_hash_normalizes_planned_craft_job_uuid_and_order():
    import jev_factorio.judgments as judgments
    from types import SimpleNamespace
    from jev_factorio.background import BackgroundWorkLoop
    from test_factory import recipe

    owner = SimpleNamespace(catalog=SimpleNamespace(recipes={
        "pipe": recipe("pipe", {"iron-plate": 1}),
    }))
    source_plan = Plan("craft-pipe", "steam_power", "Craft pipe", (
        Step("factory_craft", "inventory", "pipe", 1, costs={"iron-plate": 1},
             parameters={"recipe": "pipe", "batches": 1}),
    ))
    first = BackgroundWorkLoop._tracked_plan(owner, source_plan, None)
    second = BackgroundWorkLoop._tracked_plan(owner, source_plan, None)
    state = {"facts": {"tick": 50}, "active_goal": {"goal": "steam_power"}, "history": []}
    context_a, questions_a, offered_a = judgments.question_batch(state, [first], max_candidates=1)
    context_b, questions_b, offered_b = judgments.question_batch(state, [second], max_candidates=1)
    assert first.to_dict() != second.to_dict()
    common = dict(state_sha256="1" * 64, frontier_sha256="2" * 64, current_tick=50)
    batch_a = persistence.selection_batch_metadata(context_a, questions_a, offered_a, **common)
    batch_b = persistence.selection_batch_metadata(context_b, questions_b, offered_b, **common)
    assert batch_a["request_sha256"] == batch_b["request_sha256"]
    assert batch_a["offered"] == batch_b["offered"]


def test_default_persistence_keeps_same_controller_observing_without_rebilling(tmp_path, monkeypatch):
    loop, backend, checkpoint, requests, _ = _idle_loop(tmp_path, monkeypatch)
    assert loop.persistent_idle_observations == 0
    for _ in range(12):
        loop.step()
    before = deepcopy(loop.memory.blocked_recovery['attempts'])
    stalled = loop.memory.stalled_decisions
    for _ in range(20):
        loop.step()
    assert loop.terminal is False and loop._persistent_idle_exhausted is False
    assert requests == [True] and backend.actions == []
    assert loop.memory.blocked_recovery['attempts'] == before
    assert loop.memory.stalled_decisions == stalled
    assert loop.memory.status == 'blocked' and loop.memory.pending is None
    assert loop.persistent_recovery_wait_seconds() == 300
    saved = CampaignMemory.load(checkpoint, backend.session_id, 'bootstrap_mining')
    assert saved.blocked_recovery['attempts'] == before
    assert saved.stalled_decisions == stalled and saved.status == 'blocked'


def test_model_abstention_waits_without_rebilling_after_restart(tmp_path, monkeypatch):
    backend = LiveMockBackend()
    checkpoint = tmp_path / "checkpoint.json"
    memory = CampaignMemory(backend.session_id, "bootstrap_mining",
                            active_goal="bootstrap_mining", status="running",
                            stalled_decisions=3)
    memory.save(checkpoint)
    loop, backend, checkpoint, requests, _ = _idle_loop(
        tmp_path, monkeypatch, backend=backend, rejection_reason="model abstention")
    loop.step()
    assert loop.memory.status == "blocked"
    assert loop.memory.reason == "model abstention"
    assert not loop.terminal and backend.actions == [] and requests == [True]
    rows = deepcopy(loop.memory.blocked_recovery["attempts"])
    assert rows[-1]["outcome"] == "rejected"
    assert rows[-1]["reason"] == "model abstention"
    for _ in range(8):
        loop.step()
    assert requests == [True] and backend.actions == [] and not loop.terminal
    restored, _, _, restored_requests, _ = _idle_loop(
        tmp_path, monkeypatch, backend=backend, rejection_reason="model abstention")
    restored.step()
    assert restored_requests == [] and not restored.terminal
    assert restored.memory.blocked_recovery["attempts"] == rows
    backend.inv["iron-ore"] = 1
    restored.step()
    assert restored_requests == [True] and backend.actions == []
    assert restored.memory.reason == "model abstention" and not restored.terminal


@pytest.mark.parametrize("contrary", ["pending", "attempt", "active_plan", "provider", "disabled", "uncertain"])
def test_model_abstention_does_not_override_terminal_safety_guards(tmp_path, monkeypatch, contrary):
    loop, _, _, _, _ = _idle_loop(
        tmp_path, monkeypatch, rejection_reason="model abstention")
    loop.step()
    assert loop.memory.reason == "model abstention" and not loop.terminal
    if contrary in ("pending", "attempt", "active_plan"):
        setattr(loop.memory, contrary, {"id": "unresolved"})
    elif contrary == "provider":
        loop._persistence_failed = True
    elif contrary == "disabled":
        loop.persist_recoverable_blocks = False
    else:
        loop.memory.status = "uncertain"
    assert loop.terminal
