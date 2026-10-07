"""Bounded regression coverage for the recognized legacy rocket capability block."""

from copy import deepcopy
from dataclasses import asdict
import json
from types import FunctionType, MethodType, SimpleNamespace

import pytest
import requests

from jev_factorio.controller import HierarchicalLoop
from jev_factorio import dashboard as dashboard_module
from jev_factorio import jev_client as jev_client_module
from jev_factorio.dashboard import EventWriter, attach
from jev_factorio.jev_client import CloudflareJevClient, JevClient, MockJevClient
from jev_factorio.memory import CampaignMemory
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.factory import compile_factory
from jev_factorio.operational_safety import atomic_json, safety_dir
from jev_factorio.provider_health import (
    ProviderBlocked,
    ProviderCircuit,
    provider_health_terminal_contract_is_compatible,
    supported_provider_health_terminal_contract,
)
from jev_factorio.state import GameSnapshot
from jev_factorio.telemetry import make_attempt, utc_now


LEGACY_REASON = (
    "Missing full-game production/research/construction skills and "
    "version-specific native rocket victory telemetry"
)
SESSION = "mock:legacy-capability-450"


def supported_typesafe_provider(model="offline-typesafe-model"):
    return JevClient(
        api_key="offline-test-only",
        base_url="https://typesafe.example.invalid/v1/systemone",
        model=model,
    )


def supported_cloudflare_provider(model="offline-cloudflare-model"):
    return CloudflareJevClient(
        account_id="offline-account",
        api_token="offline-test-only",
        model=model,
    )


def install_provider_error(monkeypatch, error):
    calls = []

    def post(url, **_kwargs):
        calls.append(url)
        raise error

    monkeypatch.setattr(requests, "post", post)
    return calls


def current_catalog():
    # A current-version validated catalog with the rocket recipe chain lets
    # the real factory compiler reach its first paid launch prerequisite.
    def recipe(name, ingredients, category="crafting"):
        return {
            "name": name,
            "category": category,
            "enabled": True,
            "hidden": False,
            "energy": 1,
            "ingredients": [
                {"name": item, "amount": amount, "type": "item"}
                for item, amount in ingredients.items()
            ],
            "products": [{"name": name, "amount": 1, "type": "item", "probability": 1}],
        }

    return Catalog.from_dict({
        "version": "2.0.77",
        "mods": {"base": "2.0.77"},
        "recipes": {
            "rocket-part": recipe("rocket-part", {
                "steel-plate": 100,
                "electronic-circuit": 100,
                "plastic-bar": 100,
                "low-density-structure": 100,
                "rocket-fuel": 100,
            }, "rocket-building"),
            "cargo-landing-pad": recipe("cargo-landing-pad", {"steel-plate": 1}),
        },
        "technologies": {
            "rocket-silo": {"enabled": True, "prerequisites": [], "effects": []},
        },
        "machines": {"rocket-silo": {"categories": {"rocket-building": True}}},
        "hand_categories": {"crafting": True},
    })


def current_snapshot(*, session_id=SESSION, tick=10, valid_launch=True, connector_binding=None):
    factory = {
        "player_connected": True,
        "player_bound": True,
        "crafting_queue": 0,
        "entities": {},
        "receipts": {},
        "produced": {},
    }
    if valid_launch:
        factory["launch_readiness"] = {
            "schema": 1,
            "supported": True,
            "version": "2.0.77",
            "session_id": session_id,
            "tick": tick,
            "actor_unit": 1,
            "surface_index": 1,
            "force_index": 1,
            "fault": False,
            "attempts": {},
            "receipts": {},
            "pad": {},
            "pad_site": {"id": "site:1", "position": {"x": 0, "y": 0}},
            "fish": {},
            "silo": {},
        }
    if connector_binding is not None:
        factory["connector_ownership"] = deepcopy(connector_binding)
    return GameSnapshot(
        session_id=session_id,
        world_kind="mock",
        tick=tick,
        researched=["rocket-silo"],
        inventory={"cargo-landing-pad": 1},
        factory=factory,
    )


class OfflineRocketBackend:
    def __init__(self, *, catalog=True, valid_launch=True, session_id=SESSION,
                 connector_binding=None):
        self._catalog = current_catalog() if catalog is True else catalog
        self.state = current_snapshot(session_id=session_id, valid_launch=valid_launch,
                                      connector_binding=connector_binding)
        self.actions = []
        self.observations = 0

    def enable_factory(self):
        return self._catalog

    def observe(self):
        self.observations += 1
        return deepcopy(self.state)

    def execute(self, action, parameters):
        self.actions.append((action, deepcopy(parameters)))
        return "offline mock command"

    def act(self, action):
        assert action == "idle"
        self.state.tick += 1
        if "launch_readiness" in self.state.factory:
            self.state.factory["launch_readiness"]["tick"] = self.state.tick
        return "offline mock observation tick"


def legacy_checkpoint(path, *, version=1, status="blocked", reason=LEGACY_REASON,
                      session_id=SESSION, target="rocket_launch", patch=None):
    memory = CampaignMemory(
        session_id,
        target,
        version=version,
        active_goal="rocket_launch",
        completed_goals={"stockpile_fuel": 8, "bootstrap_mining": 8},
        history=[{"kind": "legacy_history_witness", "tick": 8, "receipt": "prior:paid"}],
        last_tick=9,
        status=status,
        reason=reason,
    )
    data = asdict(memory)
    if version == 1:
        data.pop("attempt")
        data.pop("attempt_outcomes")
    data.update(patch or {})
    original = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(original)
    return original


def current_rocket_plan():
    plans, blocker = compile_factory("rocket_launch", current_snapshot(), current_catalog())
    assert blocker == "" and plans
    return plans[0]


@pytest.mark.parametrize("version", [1, 2], ids=["v1-metadata-migration", "supported-v2"])
def test_recognized_legacy_block_reaches_actual_factory_planner_and_records_transition(
    tmp_path, version
):
    checkpoint = tmp_path / f"legacy-v{version}.json"
    connector_binding = {"protocol": 1, "session_id": SESSION, "routes": {}}
    preserved = {"connector_ownership": connector_binding}
    if version == 2:
        plan = current_rocket_plan()
        outcome = make_attempt(
            SESSION, "rocket_launch", plan.to_dict(), 0, {"started_tick": 7})
        outcome.update(outcome="verified", finished_tick=8,
                       finished_at_utc=utc_now(), latency_seconds=None)
        preserved["attempt_outcomes"] = [outcome]
    original = legacy_checkpoint(checkpoint, version=version, patch=preserved)
    backend = OfflineRocketBackend(connector_binding=connector_binding)

    # Read-only restore accepts the historical checkpoint without rewriting it.
    loaded = CampaignMemory.load(checkpoint, SESSION, "rocket_launch")
    assert checkpoint.read_bytes() == original
    assert loaded.version == 2
    assert loaded.status == "blocked" and loaded.reason == LEGACY_REASON

    # This is the current production planner, not an injected compiler sentinel.
    plans, blocker = compile_factory("rocket_launch", backend.observe(), backend._catalog)
    assert blocker == ""
    assert [plan.steps[0].action for plan in plans] == ["factory_launch_pad"]

    loop = HierarchicalLoop(
        backend,
        policy="deterministic",
        target="rocket_launch",
        checkpoint=str(checkpoint),
        resume_controller=True,
        tick_seconds=0,
    )
    record = loop.step()

    assert record["action"] == "factory_launch_pad"
    assert backend.actions == [("factory_launch_pad", {"site": "site:1", "receipt": "launch:pad:10"})]
    assert loop.memory.status == "running"
    assert loop.memory.reason == ""
    assert loop.memory.history[0] == {"kind": "legacy_history_witness", "tick": 8, "receipt": "prior:paid"}
    assert loop.memory.connector_ownership == connector_binding
    if version == 2:
        assert loop.memory.attempt_outcomes == preserved["attempt_outcomes"]
    transitions = [event for event in loop.memory.history
                   if event.get("kind") == "legacy_capability_block_resumed"]
    assert len(transitions) == 1
    assert transitions[0]["tick"] == 10
    persisted = json.loads(checkpoint.read_bytes())
    assert persisted["status"] == "running"
    assert persisted["reason"] == ""
    assert [event["kind"] for event in persisted["history"]].count(
        "legacy_capability_block_resumed") == 1
    assert persisted["connector_ownership"] == connector_binding
    if version == 2:
        assert persisted["attempt_outcomes"] == preserved["attempt_outcomes"]


@pytest.mark.parametrize("case", ["other_reason", "uncertain", "completed", "wrong_goal"])
def test_unrelated_terminal_states_are_not_reactivated(tmp_path, case):
    checkpoint = tmp_path / f"{case}.json"
    kwargs = {"version": 2}
    if case == "other_reason":
        kwargs["reason"] = "unrelated capability or authority hold"
    elif case == "uncertain":
        kwargs["status"] = "uncertain"
    elif case == "completed":
        kwargs["status"] = "completed"
    elif case == "wrong_goal":
        kwargs["patch"] = {"active_goal": "bootstrap_mining"}
    legacy_checkpoint(checkpoint, **kwargs)
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    result = loop.step()

    assert result["action"] == "observe"
    assert backend.actions == []
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)
    assert loop.memory.status == kwargs.get("status", "blocked")


@pytest.mark.parametrize("catalog,valid_launch", [
    (None, True),
    (object(), True),
    (True, False),
], ids=["catalog-unavailable", "catalog-not-validated", "planner-capability-absent"])
def test_missing_or_unusable_current_capability_stays_blocked(
    tmp_path, catalog, valid_launch
):
    checkpoint = tmp_path / "unsupported.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend(catalog=catalog, valid_launch=valid_launch)
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    result = loop.step()

    assert backend.actions == []
    assert loop.memory.status == "blocked"
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)
    if catalog is None or not valid_launch:
        assert result["action"] == "observe"


def test_in_memory_block_cannot_resume_without_a_durable_checkpoint():
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch", tick_seconds=0)
    loop.memory = CampaignMemory(
        SESSION, "rocket_launch", active_goal="rocket_launch",
        completed_goals={"stockpile_fuel": 8, "bootstrap_mining": 8},
        last_tick=9, status="blocked", reason=LEGACY_REASON,
    )

    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON


@pytest.mark.parametrize("obligation", ["active_plan", "pending", "reservation"])
def test_legacy_block_with_foreground_or_paid_obligation_is_never_cleared(
    tmp_path, obligation
):
    checkpoint = tmp_path / f"{obligation}.json"
    plan = current_rocket_plan().to_dict()
    patch = {}
    if obligation in {"active_plan", "pending"}:
        patch["active_plan"] = plan
    if obligation == "pending":
        patch["pending"] = {
            "started_tick": 9, "polls": 0, "action": "factory_launch_pad", "dispatch": "prepared"
        }
    if obligation == "reservation":
        patch["reservations"] = {plan["id"]: {"cargo-landing-pad": 1}}
    legacy_checkpoint(checkpoint, version=1, patch=patch)
    before = json.loads(checkpoint.read_bytes())
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    after = json.loads(checkpoint.read_bytes())
    assert after["status"] == "blocked"
    assert after["reason"] == LEGACY_REASON
    assert after["active_plan"] == before["active_plan"]
    assert after["pending"] == before["pending"]
    assert after["reservations"] == before["reservations"]
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in after["history"])


def test_nonempty_background_craft_is_preserved_and_keeps_legacy_state_terminal(tmp_path):
    # Reuse the existing production-shaped background-work fixture to create
    # a loader-valid retained paid job; only its bounded test plan is synthetic.
    from test_background_work import ReceiptBackend, ScenarioLoop

    from jev_factorio.background import BackgroundWorkLoop

    backend = ReceiptBackend()
    checkpoint = tmp_path / "background.json"
    producer = ScenarioLoop(
        backend, policy="deterministic", factory_scheduling="ready-work",
        target="rocket_launch", checkpoint=str(checkpoint), tick_seconds=0)
    producer.memory = producer.memory_type(
        backend.state.session_id, producer.target,
        active_goal="rocket_launch",
        completed_goals={goal: 0 for goal in producer.order[:-1]},
        last_tick=backend.state.tick,
    )
    producer.step()
    saved = json.loads(checkpoint.read_bytes())
    assert saved["background_job"] is not None
    saved["status"], saved["reason"] = "blocked", LEGACY_REASON
    checkpoint.write_text(json.dumps(saved, sort_keys=True, separators=(",", ":")))
    before_job = deepcopy(saved["background_job"])
    before_attempt = deepcopy(saved["background_attempt"])
    before_calls = list(backend.calls)

    resumed = BackgroundWorkLoop(
        backend, policy="deterministic", factory_scheduling="ready-work",
        target="rocket_launch", checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    record = resumed.step()

    assert record["action"] == "observe"
    assert resumed.memory.status == "blocked"
    assert resumed.memory.reason == LEGACY_REASON
    assert resumed.memory.background_job == before_job
    assert resumed.memory.background_attempt == before_attempt
    assert backend.calls == before_calls
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in resumed.memory.history)


def test_failure_budget_is_not_bypassed_by_legacy_capability_resume(tmp_path):
    checkpoint = tmp_path / "budget.json"
    plan = current_rocket_plan()
    legacy_checkpoint(checkpoint, version=2, patch={"failures": {plan.id: 2}})
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    loop.step()

    assert backend.actions == []
    assert loop.memory.status == "blocked"
    assert loop.memory.reason == "Plan failure budget exhausted"
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)


def test_runtime_safety_admission_hold_precedes_legacy_resume(tmp_path, monkeypatch):
    import jev_factorio.operational_safety as operational_safety

    checkpoint = tmp_path / "safety-hold.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    monkeypatch.setattr(operational_safety, "storage_ready", lambda *args, **kwargs: False)
    assert checkpoint.read_bytes() == original

    record = loop.step()

    assert record["action"] == "observe"
    assert loop._safety.phase == "storage_pressure"
    assert backend.actions == []
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert any(row.get("kind") == "legacy_history_witness" for row in loop.memory.history)
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)


def test_unhealthy_provider_circuit_does_not_resume_legacy_block(tmp_path, monkeypatch):
    checkpoint = tmp_path / "provider-hold.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider("offline-unavailable-model")
    calls = install_provider_error(monkeypatch, requests.Timeout("offline provider-health fixture"))
    loop = HierarchicalLoop(backend, jev=provider, policy="deterministic",
                            target="rocket_launch", checkpoint=str(checkpoint),
                            resume_controller=True, tick_seconds=0)
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}

    with pytest.raises(ProviderBlocked):
        loop.jev.evaluate({"offline": True}, questions)
    assert loop.jev.state["phase"] != "healthy"
    provider_state = deepcopy(loop.jev.state)
    assert checkpoint.read_bytes() == original

    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert calls == [provider.base_url]
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert any(row.get("kind") == "legacy_history_witness" for row in loop.memory.history)
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)
    assert loop.jev.state == provider_state


@pytest.mark.parametrize("version", [1, 2], ids=["v1", "v2"])
def test_dashboard_wrapped_provider_circuit_cooldown_keeps_legacy_block_blocked(
    tmp_path, monkeypatch, version
):
    checkpoint = tmp_path / f"dashboard-provider-hold-v{version}.json"
    connector_binding = {"protocol": 1, "session_id": SESSION, "routes": {}}
    preserved = {}
    if version == 2:
        plan = current_rocket_plan()
        outcome = make_attempt(
            SESSION, "rocket_launch", plan.to_dict(), 0, {"started_tick": 7})
        outcome.update(outcome="verified", finished_tick=8,
                       finished_at_utc=utc_now(), latency_seconds=None)
        preserved = {
            "connector_ownership": connector_binding,
            "attempt_outcomes": [outcome],
        }
    original = legacy_checkpoint(checkpoint, version=version, patch=preserved)
    backend = OfflineRocketBackend(
        connector_binding=connector_binding if version == 2 else None)
    provider = supported_typesafe_provider("offline-unavailable-model")
    calls = install_provider_error(monkeypatch, requests.Timeout("offline provider-health fixture"))
    loop = HierarchicalLoop(backend, jev=provider, policy="deterministic",
                            target="rocket_launch", checkpoint=str(checkpoint),
                            resume_controller=True, tick_seconds=0)
    circuit = loop.jev
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}

    with pytest.raises(ProviderBlocked):
        circuit.evaluate({"offline": True}, questions)
    assert circuit.state["phase"] == "cooldown"
    assert checkpoint.read_bytes() == original
    provider_state = deepcopy(circuit.state)

    with EventWriter(tmp_path / f"dashboard-v{version}.jsonl") as writer:
        attach(loop, writer)
        record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert calls == [provider.base_url]
    assert circuit.state == provider_state
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    if version == 2:
        assert loop.memory.connector_ownership == connector_binding
        assert loop.memory.attempt_outcomes == preserved["attempt_outcomes"]
    assert any(row.get("kind") == "legacy_history_witness" for row in loop.memory.history)
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "blocked" and saved["reason"] == LEGACY_REASON
    assert saved["pending"] is None and saved["attempt"] is None
    assert saved["reservations"] == {}
    if version == 2:
        assert saved["connector_ownership"] == connector_binding
        assert saved["attempt_outcomes"] == preserved["attempt_outcomes"]
    assert any(row.get("kind") == "legacy_history_witness" for row in saved["history"])
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in saved["history"])


@pytest.mark.parametrize("version", [1, 2], ids=["v1", "v2"])
def test_dashboard_wrapped_healthy_provider_allows_qualified_legacy_resume(tmp_path, version):
    checkpoint = tmp_path / f"dashboard-provider-healthy-v{version}.json"
    legacy_checkpoint(checkpoint, version=version)
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider("offline-healthy-model")
    loop = HierarchicalLoop(backend, jev=provider, policy="deterministic",
                            target="rocket_launch", checkpoint=str(checkpoint),
                            resume_controller=True, tick_seconds=0)
    circuit = loop.jev
    assert circuit.state["phase"] == "healthy"

    with EventWriter(tmp_path / f"dashboard-healthy-v{version}.jsonl") as writer:
        attach(loop, writer)
        record = loop.step()

    assert record["action"] == "factory_launch_pad"
    assert record["requested_model"] == "offline-healthy-model"
    assert [action for action, _ in backend.actions] == ["factory_launch_pad"]
    assert circuit.state["phase"] == "healthy"
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "running"
    assert [event.get("kind") for event in saved["history"]].count(
        "legacy_capability_block_resumed") == 1


@pytest.mark.parametrize("factory", [
    supported_typesafe_provider,
    supported_cloudflare_provider,
], ids=["typesafe", "cloudflare"])
@pytest.mark.parametrize("dashboard_bound", [False, True], ids=["direct", "dashboard"])
def test_supported_builtin_provider_contract_keeps_healthy_resume(
    tmp_path, monkeypatch, factory, dashboard_bound
):
    checkpoint = tmp_path / f"supported-{factory.__name__}-{dashboard_bound}.json"
    legacy_checkpoint(checkpoint, version=2)
    provider = factory()
    provider_calls = []

    def forbidden_http(url, **_kwargs):
        provider_calls.append(url)
        raise AssertionError("legacy resume must not dispatch a model request")

    monkeypatch.setattr(requests, "post", forbidden_http)
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(
        backend, jev=provider, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    assert loop._provider_health_terminal_contract is not None

    if dashboard_bound:
        with EventWriter(tmp_path / "supported-dashboard.jsonl") as writer:
            attach(loop, writer)
            record = loop.step()
    else:
        record = loop.step()

    assert record["action"] == "factory_launch_pad"
    assert record["requested_model"] == provider.model
    assert backend.actions == [("factory_launch_pad", {
        "receipt": "launch:pad:10", "site": "site:1"})]
    assert provider_calls == []
    assert json.loads(checkpoint.read_bytes())["status"] == "running"


@pytest.mark.parametrize("dashboard_bound", [False, True], ids=["direct", "dashboard"])
def test_builtin_mock_terminal_remains_supported_without_provider_circuit(
    tmp_path, dashboard_bound
):
    checkpoint = tmp_path / f"supported-mock-{dashboard_bound}.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = MockJevClient()
    loop = HierarchicalLoop(
        backend, jev=provider, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    assert loop._provider_health_circuits == ()
    assert loop._provider_health_terminal_contract is not None

    if dashboard_bound:
        with EventWriter(tmp_path / "supported-mock-dashboard.jsonl") as writer:
            attach(loop, writer)
            record = loop.step()
    else:
        record = loop.step()

    assert record["action"] == "factory_launch_pad"
    assert record["requested_model"] == provider.model
    assert [action for action, _ in backend.actions] == ["factory_launch_pad"]
    assert json.loads(checkpoint.read_bytes())["status"] == "running"


@pytest.mark.parametrize(
    "replacement", ["observer-client", "loop-provider", "unregistered-wrapper"]
)
def test_dashboard_provider_binding_rejects_post_attach_client_drift(
    tmp_path, monkeypatch, replacement
):
    checkpoint = tmp_path / f"dashboard-provider-drift-{replacement}.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider("dashboard-original-provider")
    circuit = ProviderCircuit(provider, _provider_path(checkpoint))
    prior_state = deepcopy(circuit.state)
    prior_sidecar = circuit.path.read_bytes() if circuit.path.exists() else None
    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    request_calls = install_provider_error(
        monkeypatch, requests.Timeout("offline replacement provider"))

    with EventWriter(tmp_path / f"dashboard-provider-drift-{replacement}.jsonl") as writer:
        attach(loop, writer)
        observer = loop.jev
        assert observer is not circuit
        if replacement == "observer-client":
            replacement_client = supported_typesafe_provider("replacement-provider-model")
            replacement_circuit = ProviderCircuit(
                replacement_client, tmp_path / "replacement-provider.json", clock=AdvancingClock())
            questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                                    "instructions": "choose yes"}}
            with pytest.raises(ProviderBlocked):
                replacement_circuit.evaluate({"offline": True}, questions)
            replacement_state = deepcopy(replacement_circuit.state)
            replacement_sidecar = replacement_circuit.path.read_bytes()
            observer.model_client = replacement_circuit
        elif replacement == "loop-provider":
            replacement_client = supported_typesafe_provider("replacement-provider-model")
            replacement_circuit = ProviderCircuit(
                replacement_client, tmp_path / "replacement-provider.json", clock=AdvancingClock())
            questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                                    "instructions": "choose yes"}}
            with pytest.raises(ProviderBlocked):
                replacement_circuit.evaluate({"offline": True}, questions)
            replacement_state = deepcopy(replacement_circuit.state)
            replacement_sidecar = replacement_circuit.path.read_bytes()
            loop.jev = replacement_circuit
        else:
            replacement_client = supported_typesafe_provider("replacement-provider-model")
            replacement_circuit = ProviderCircuit(
                replacement_client, tmp_path / "replacement-provider.json", clock=AdvancingClock())
            questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                                    "instructions": "choose yes"}}
            with pytest.raises(ProviderBlocked):
                replacement_circuit.evaluate({"offline": True}, questions)
            replacement_state = deepcopy(replacement_circuit.state)
            replacement_sidecar = replacement_circuit.path.read_bytes()

            class UnregisteredObserver:
                def __init__(self, model_client):
                    self.model_client = model_client

                def __getattr__(self, name):
                    return getattr(self.model_client, name)

            loop.jev = UnregisteredObserver(replacement_circuit)

        record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] is None
    assert backend.actions == []
    assert request_calls == [replacement_client.base_url]
    assert circuit.state == prior_state
    if prior_sidecar is None:
        assert not circuit.path.exists()
    else:
        assert circuit.path.read_bytes() == prior_sidecar
    assert replacement_circuit.state == replacement_state
    assert replacement_circuit.path.read_bytes() == replacement_sidecar
    _assert_legacy_block_is_preserved(checkpoint)


@pytest.mark.parametrize("mutation", [
    "instance-before-registration",
    "instance-after-attach",
    "class-before-attach",
    "class-after-attach",
    "forged-registration",
], ids=[
    "instance-before-registration",
    "instance-after-attach",
    "class-before-attach",
    "class-after-attach",
    "forged-registration",
])
def test_dashboard_evaluator_identity_is_bound_before_legacy_resume(
    tmp_path, monkeypatch, mutation
):
    checkpoint = tmp_path / f"dashboard-evaluator-{mutation}.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    original_provider = MockJevClient()
    hidden_provider = supported_typesafe_provider("hidden-dashboard-evaluator")
    loop = HierarchicalLoop(
        backend, jev=original_provider, policy="jev", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    http_calls = []

    def fake_post(url, **kwargs):
        http_calls.append({"url": url, "model": kwargs["json"].get("model")})
        answers = MockJevClient().evaluate(
            kwargs["json"]["state"], kwargs["json"]["questions"])
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"answers": answers, "usage": {"input_tokens": 1},
                          "model": hidden_provider.model},
        )

    monkeypatch.setattr(requests, "post", fake_post)

    def replace_class_evaluator():
        monkeypatch.setattr(
            dashboard_module._DashboardModelObserver,
            "evaluate", hidden_provider.evaluate)

    with EventWriter(tmp_path / f"dashboard-evaluator-{mutation}.jsonl") as writer:
        if mutation == "class-before-attach":
            replace_class_evaluator()
            attach(loop, writer)
        elif mutation == "instance-before-registration":
            def emit(*_args, **_kwargs):
                return None

            def measured(_kind, _stage, function, *args, **kwargs):
                return function(*args, **kwargs)

            forged = dashboard_module._DashboardModelObserver(
                original_provider, emit, measured)
            forged.evaluate = hidden_provider.evaluate
            loop._register_dashboard_model_observer(forged, original_provider)
            loop.jev = forged
        elif mutation == "forged-registration":
            class ForgedObserver:
                def __init__(self, model_client):
                    self.model_client = model_client

                def __getattr__(self, key):
                    return getattr(self.model_client, key)

                def evaluate(self, state, questions):
                    return hidden_provider.evaluate(state, questions)

            forged = ForgedObserver(original_provider)
            loop._register_dashboard_model_observer(forged, original_provider)
            loop.jev = forged
        else:
            attach(loop, writer)
            observer = loop.jev
            if mutation == "instance-after-attach":
                observer.evaluate = hidden_provider.evaluate
            else:
                replace_class_evaluator()

        assert loop._legacy_provider_health_ready() is False
        record = loop.step()

    saved = json.loads(checkpoint.read_bytes())
    assert record["action"] == "observe"
    assert backend.actions == []
    assert http_calls == []
    assert saved["status"] == "blocked"
    assert saved["reason"] == LEGACY_REASON
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in saved["history"])
    _assert_legacy_block_is_preserved(checkpoint)


@pytest.mark.parametrize("field", ["_measured", "_emit"], ids=["measured", "emitter"])
def test_dashboard_binding_cannot_be_refreshed_after_callback_mutation(
    tmp_path, monkeypatch, field
):
    checkpoint = tmp_path / f"dashboard-no-rebind-{field}.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    original_provider = MockJevClient()
    hidden_provider = supported_typesafe_provider(f"hidden-after-{field}-mutation")
    http_calls = []

    def fake_post(url, **kwargs):
        http_calls.append({"url": url, "model": kwargs["json"].get("model")})
        answers = MockJevClient().evaluate(
            kwargs["json"]["state"], kwargs["json"]["questions"])
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"answers": answers, "usage": {"input_tokens": 1},
                          "model": hidden_provider.model},
        )

    monkeypatch.setattr(requests, "post", fake_post)
    loop = HierarchicalLoop(
        backend, jev=original_provider, policy="jev", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    with EventWriter(tmp_path / f"dashboard-no-rebind-{field}.jsonl") as writer:
        attach(loop, writer)
        observer = loop.jev
        original_binding = dashboard_module._dashboard_model_observer_bindings.get(observer)
        if field == "_measured":
            def route_to_hidden(_kind, _stage, _function, *args, **kwargs):
                return hidden_provider.evaluate(*args, **kwargs)
            observer._measured = route_to_hidden
        else:
            observer._emit = lambda *_args, **_kwargs: None

        assert dashboard_module._register_dashboard_model_observer_binding(
            loop, observer, original_provider) is False
        # The controller registration helper cannot turn a stale dashboard
        # callback set back into an authenticated attach binding either.
        loop._register_dashboard_model_observer(observer, original_provider)
        assert (
            dashboard_module._dashboard_model_observer_bindings.get(observer)
            is original_binding
        )
        assert loop._legacy_provider_health_ready() is False
        record = loop.step()

    saved = json.loads(checkpoint.read_bytes())
    assert record["action"] == "observe"
    assert backend.actions == []
    assert http_calls == []
    assert saved["status"] == "blocked" and saved["reason"] == LEGACY_REASON
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in saved["history"])
    _assert_legacy_block_is_preserved(checkpoint)


def test_exact_observer_outside_attach_cannot_mint_dashboard_binding(
    tmp_path, monkeypatch
):
    checkpoint = tmp_path / "dashboard-forged-origin.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    original_provider = MockJevClient()
    hidden_provider = supported_typesafe_provider("hidden-forged-origin")
    http_calls = []

    def fake_post(url, **kwargs):
        http_calls.append({"url": url, "model": kwargs["json"].get("model")})
        answers = MockJevClient().evaluate(
            kwargs["json"]["state"], kwargs["json"]["questions"])
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"answers": answers, "usage": {"input_tokens": 1},
                          "model": hidden_provider.model},
        )

    monkeypatch.setattr(requests, "post", fake_post)
    loop = HierarchicalLoop(
        backend, jev=original_provider, policy="jev", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    def emit(*_args, **_kwargs):
        return None

    def route_to_hidden(_kind, _stage, _function, *args, **kwargs):
        return hidden_provider.evaluate(*args, **kwargs)

    forged = dashboard_module._DashboardModelObserver(
        original_provider, emit, route_to_hidden)
    assert dashboard_module._register_dashboard_model_observer_binding(
        loop, forged, original_provider) is False
    loop._register_dashboard_model_observer(forged, original_provider)
    assert loop._dashboard_provider_observer_binding is None
    loop.jev = forged

    with EventWriter(tmp_path / "dashboard-forged-origin.jsonl") as writer:
        record = loop.step()

    saved = json.loads(checkpoint.read_bytes())
    assert record["action"] == "observe"
    assert backend.actions == []
    assert http_calls == []
    assert saved["status"] == "blocked" and saved["reason"] == LEGACY_REASON
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in saved["history"])
    _assert_legacy_block_is_preserved(checkpoint)


def test_copied_attach_code_with_foreign_globals_cannot_mint_binding(
    tmp_path, monkeypatch
):
    checkpoint = tmp_path / "dashboard-copied-attach.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    original_provider = MockJevClient()
    hidden_provider = supported_typesafe_provider("hidden-copied-attach")
    http_calls = []

    def fake_post(url, **kwargs):
        http_calls.append({"url": url, "model": kwargs["json"].get("model")})
        answers = MockJevClient().evaluate(
            kwargs["json"]["state"], kwargs["json"]["questions"])
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"answers": answers, "usage": {"input_tokens": 1},
                          "model": hidden_provider.model},
        )

    monkeypatch.setattr(requests, "post", fake_post)
    loop = HierarchicalLoop(
        backend, jev=original_provider, policy="jev", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    def route_to_hidden(_kind, _stage, _function, *args, **kwargs):
        return hidden_provider.evaluate(*args, **kwargs)

    def forged_constructor(model_client, emit, _measured):
        return dashboard_module._SUPPORTED_DASHBOARD_MODEL_OBSERVER_TYPE(
            model_client, emit, route_to_hidden)

    copied_globals = dict(dashboard_module.attach.__globals__)
    copied_globals["_DashboardModelObserver"] = forged_constructor
    copied_attach = FunctionType(
        dashboard_module.attach.__code__, copied_globals, "copied_attach",
        dashboard_module.attach.__defaults__, dashboard_module.attach.__closure__)

    with EventWriter(tmp_path / "dashboard-copied-attach.jsonl") as writer:
        copied_attach(loop, writer)
        assert copied_attach.__code__ is dashboard_module.attach.__code__
        assert copied_attach.__globals__ is not dashboard_module.attach.__globals__
        assert loop._legacy_provider_health_ready() is False
        record = loop.step()

    saved = json.loads(checkpoint.read_bytes())
    assert record["action"] == "observe"
    assert backend.actions == []
    assert http_calls == []
    assert saved["status"] == "blocked" and saved["reason"] == LEGACY_REASON
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in saved["history"])
    _assert_legacy_block_is_preserved(checkpoint)


def test_attach_rejects_substituted_model_observer_constructor(
    tmp_path, monkeypatch
):
    checkpoint = tmp_path / "dashboard-substituted-constructor.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    original_provider = MockJevClient()
    hidden_provider = supported_typesafe_provider("hidden-substituted-constructor")
    http_calls = []

    def fake_post(url, **kwargs):
        http_calls.append({"url": url, "model": kwargs["json"].get("model")})
        answers = MockJevClient().evaluate(
            kwargs["json"]["state"], kwargs["json"]["questions"])
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"answers": answers, "usage": {"input_tokens": 1},
                          "model": hidden_provider.model},
        )

    monkeypatch.setattr(requests, "post", fake_post)
    loop = HierarchicalLoop(
        backend, jev=original_provider, policy="jev", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    original_constructor = dashboard_module._SUPPORTED_DASHBOARD_MODEL_OBSERVER_TYPE

    def route_to_hidden(_kind, _stage, _function, *args, **kwargs):
        return hidden_provider.evaluate(*args, **kwargs)

    def forged_constructor(model_client, emit, _measured):
        return original_constructor(model_client, emit, route_to_hidden)

    with EventWriter(tmp_path / "dashboard-substituted-constructor.jsonl") as writer:
        monkeypatch.setattr(dashboard_module, "_DashboardModelObserver", forged_constructor)
        attach(loop, writer)
        assert type(loop.jev) is original_constructor
        assert loop.jev.model_client is original_provider
        assert loop._legacy_provider_health_ready() is False
        assert loop._dashboard_provider_observer_binding is None
        record = loop.step()

    saved = json.loads(checkpoint.read_bytes())
    assert record["action"] == "observe"
    assert backend.actions == []
    assert http_calls == []
    assert saved["status"] == "blocked" and saved["reason"] == LEGACY_REASON
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in saved["history"])
    _assert_legacy_block_is_preserved(checkpoint)


@pytest.mark.parametrize("drift", ["emit-code", "measured-code", "emit-closure", "measured-closure"])
def test_dashboard_binding_detects_callback_code_and_closure_drift(
    tmp_path, monkeypatch, drift
):
    checkpoint = tmp_path / f"dashboard-callback-signature-{drift}.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = MockJevClient()
    hidden_provider = supported_typesafe_provider(f"hidden-callback-{drift}")
    callback_invocations = []
    http_calls = []
    probe_state = {"candidate_plans": {}}
    probe_questions = {
        "callback_probe": {"type": "choice", "criteria": {"safe": "safe"},
                           "instructions": "choose safe"},
    }

    def fake_post(url, **kwargs):
        http_calls.append({"url": url, "model": kwargs["json"].get("model")})
        answers = MockJevClient().evaluate(
            kwargs["json"]["state"], kwargs["json"]["questions"])
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"answers": answers, "usage": {"input_tokens": 1},
                          "model": hidden_provider.model},
        )

    monkeypatch.setattr(requests, "post", fake_post)
    loop = HierarchicalLoop(
        backend, jev=provider, policy="jev", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    with EventWriter(tmp_path / f"dashboard-callback-signature-{drift}.jsonl") as writer:
        attach(loop, writer)
        observer = loop.jev
        callback_field = "_measured" if drift.startswith("measured") else "_emit"
        callback = getattr(observer, callback_field)
        restore_callback = None

        def malicious_callback(*_args, **_kwargs):
            callback_invocations.append(drift)
            return hidden_provider.evaluate(probe_state, probe_questions)

        if drift.endswith("code"):
            def replacement_factory(payload):
                def replacement(*_args, **_kwargs):
                    payload["calls"].append(payload["label"])
                    return payload["provider"].evaluate(
                        payload["state"], payload["questions"])
                return replacement

            replacement = replacement_factory({
                "calls": callback_invocations, "label": drift,
                "provider": hidden_provider,
                "state": probe_state, "questions": probe_questions,
            })
            original_code = callback.__code__
            callback.__code__ = replacement.__code__
            restore_callback = lambda: setattr(callback, "__code__", original_code)
        else:
            freevar = "observer_emit" if callback_field == "_measured" else "observer_writer"
            freevars = callback.__code__.co_freevars
            cell = callback.__closure__[freevars.index(freevar)]
            original_value = cell.cell_contents
            if freevar == "observer_emit":
                cell.cell_contents = malicious_callback
            else:
                cell.cell_contents = SimpleNamespace(emit=malicious_callback)
            restore_callback = lambda: setattr(cell, "cell_contents", original_value)

        try:
            assert getattr(observer, callback_field) is callback
            assert loop._legacy_provider_health_ready() is False
            record = loop.step()
        finally:
            restore_callback()

    saved = json.loads(checkpoint.read_bytes())
    assert record["action"] == "observe"
    assert backend.actions == []
    assert http_calls == []
    assert callback_invocations == []
    assert saved["status"] == "blocked" and saved["reason"] == LEGACY_REASON
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in saved["history"])
    _assert_legacy_block_is_preserved(checkpoint)


def test_dashboard_registration_is_scoped_to_each_loop_instance(tmp_path):
    loops = []
    for index in range(2):
        checkpoint = tmp_path / f"dashboard-independent-{index}.json"
        legacy_checkpoint(checkpoint, version=2)
        backend = OfflineRocketBackend()
        provider = supported_typesafe_provider(model=f"offline-model-{index}")
        circuit = ProviderCircuit(provider, _provider_path(checkpoint))
        loop = HierarchicalLoop(
            backend, jev=circuit, policy="deterministic", target="rocket_launch",
            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
        loops.append((loop, backend, circuit, provider))

    with (EventWriter(tmp_path / "dashboard-independent-0.jsonl") as writer0,
          EventWriter(tmp_path / "dashboard-independent-1.jsonl") as writer1):
        attach(loops[0][0], writer0)
        attach(loops[1][0], writer1)
        records = [loops[0][0].step(), loops[1][0].step()]

    for index, ((loop, backend, circuit, provider), record) in enumerate(zip(loops, records)):
        assert record["action"] == "factory_launch_pad"
        assert record["requested_model"] == f"offline-model-{index}"
        assert [action for action, _ in backend.actions] == ["factory_launch_pad"]
        assert circuit.state["phase"] == "healthy"


@pytest.mark.parametrize("replacement", ["observer-client", "loop-provider"])
def test_dashboard_drift_from_unwrapped_provider_fails_closed_without_recursion(
    tmp_path, replacement
):
    class LocalProvider:
        model = "offline-local-model"
        last_model = "offline-local-model"
        last_usage = None

        def evaluate(self, state, questions):
            raise AssertionError("deterministic rejection must not call the provider")

    checkpoint = tmp_path / f"dashboard-unwrapped-drift-{replacement}.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    original_provider = LocalProvider()
    loop = HierarchicalLoop(
        backend, jev=original_provider, policy="deterministic",
        target="rocket_launch", checkpoint=str(checkpoint),
        resume_controller=True, tick_seconds=0)
    cycle = ProviderCircuit(original_provider, tmp_path / "unbound-cycle.json")
    cycle.client = cycle

    with EventWriter(tmp_path / f"dashboard-unwrapped-drift-{replacement}.jsonl") as writer:
        attach(loop, writer)
        if replacement == "observer-client":
            loop.jev.model_client = cycle
        else:
            loop.jev = cycle
        record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] is None
    assert backend.actions == []
    assert cycle.state["phase"] == "healthy"
    assert not cycle.path.exists()
    _assert_legacy_block_is_preserved(checkpoint)


def test_resume_requires_matching_checkpoint_session_before_any_action(tmp_path):
    checkpoint = tmp_path / "wrong-session.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend(session_id="mock:changed-session")
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    with pytest.raises(ValueError, match="version, session, or target mismatch"):
        loop.step()

    assert backend.actions == []
    assert checkpoint.read_bytes() == original


def test_resume_requires_matching_checkpoint_target_before_any_action(tmp_path):
    checkpoint = tmp_path / "wrong-target.json"
    original = legacy_checkpoint(checkpoint, version=2, target="iron_smelting")
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    with pytest.raises(ValueError, match="version, session, or target mismatch"):
        loop.step()

    assert backend.actions == []
    assert checkpoint.read_bytes() == original


def test_resume_requires_matching_actor_ledger_before_legacy_migration(tmp_path):
    checkpoint = tmp_path / "changed-actor.json"
    binding = {"protocol": 1, "session_id": SESSION, "routes": {}}
    original = legacy_checkpoint(
        checkpoint, version=2, patch={"connector_ownership": binding})
    changed = {"protocol": 1, "session_id": SESSION, "routes": {"unexpected": {}}}
    backend = OfflineRocketBackend(connector_binding=changed)
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    with pytest.raises(ValueError, match="Uncheckpointed connector route appeared"):
        loop.step()

    assert backend.actions == []
    assert checkpoint.read_bytes() == original


def test_transition_save_failure_prevents_selection_dispatch_and_checkpoint_change(
    tmp_path, monkeypatch
):
    checkpoint = tmp_path / "sync-failure.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()

    def fail_save(self, path):
        raise OSError("synthetic transition checkpoint failure")

    monkeypatch.setattr(CampaignMemory, "save", fail_save)
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    with pytest.raises(OSError, match="transition checkpoint failure"):
        loop.step()

    assert backend.actions == []
    assert checkpoint.read_bytes() == original
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)


def test_full_bounded_history_stays_blocked_instead_of_evicting_a_witness(tmp_path):
    checkpoint = tmp_path / "full-history.json"
    history = [{"kind": "retained_witness", "ordinal": index}
               for index in range(64)]
    legacy_checkpoint(checkpoint, version=2, patch={"history": history})
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    result = loop.step()

    assert result["action"] == "observe"
    assert backend.actions == []
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert loop.memory.history == history


def test_successful_transition_does_not_repeat_after_checkpoint_reload(tmp_path):
    checkpoint = tmp_path / "restart.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    first = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                             checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    first.step()
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "running"
    assert [event["kind"] for event in saved["history"]].count(
        "legacy_capability_block_resumed") == 1
    before = list(backend.actions)

    resumed = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                               checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    resumed.step()

    assert [action for action, _ in backend.actions].count("factory_launch_pad") == 1
    assert len(backend.actions) == len(before)
    after = json.loads(checkpoint.read_bytes())
    assert [event["kind"] for event in after["history"]].count(
        "legacy_capability_block_resumed") == 1


def _provider_path(checkpoint):
    return safety_dir(checkpoint) / "provider.json"


def _assert_legacy_block_is_preserved(checkpoint):
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "blocked"
    assert saved["reason"] == LEGACY_REASON
    assert any(row.get("kind") == "legacy_history_witness" for row in saved["history"])
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in saved["history"])


class DirectCircuitProvider:
    uses_http_provider = True
    base_url = "https://offline.invalid/v1"
    model = "legacy-450-offline"
    answer_quantum = 0

    def __init__(self, error=None, *, model=None):
        self.error = error
        self.calls = 0
        self.last_usage = None
        self.last_model = None
        if model is not None:
            self.model = model

    def evaluate(self, state, questions):
        self.calls += 1
        if self.error is not None:
            raise self.error
        raise AssertionError("legacy capability migration must not call the provider")


class AdvancingClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def reach_next_probe(self, circuit):
        self.now = max(self.now, float(circuit.state["next_probe_at"]) + 1)


class PlainProviderAdapter:
    uses_http_provider = False
    model = "offline-plain-provider"
    last_model = "offline-plain-provider"
    last_usage = None

    def __init__(self):
        self.client = None
        self.calls = 0

    def evaluate(self, state, questions):
        self.calls += 1
        if self.client is None:
            raise AssertionError("deterministic legacy resume must not call the provider")
        return self.client.evaluate(state, questions)


def test_unregistered_generic_adapter_keeps_ordinary_model_call_behavior():
    from jev_factorio.backends.mock import MockBackend

    backend = MockBackend()
    provider = PlainProviderAdapter()
    provider.client = MockJevClient()
    loop = HierarchicalLoop(
        backend, jev=provider, policy="jev", target="bootstrap_mining",
        resume_controller=False, tick_seconds=0)

    record = loop.step()

    assert provider.calls == 1
    assert record["model_call"] is True
    assert record["requested_model"] == provider.model
    assert loop._provider_health_terminal_contract is None


def test_provider_health_contract_is_bound_to_exact_builtin_implementation():
    provider = supported_typesafe_provider("contract-binding")
    original = supported_provider_health_terminal_contract(provider)
    assert original is not None
    assert original[0] == "typesafe-sync-v1"
    assert original[4:] == ("sync", True)
    assert provider_health_terminal_contract_is_compatible(
        original, async_decisions=False, has_circuit=True)
    assert not provider_health_terminal_contract_is_compatible(
        original, async_decisions=False, has_circuit=False)

    class UnreviewedJevSubclass(JevClient):
        pass

    subclass = UnreviewedJevSubclass(
        api_key="offline-test-only",
        base_url="https://typesafe.example.invalid/v1/systemone",
        model="contract-binding",
    )
    assert supported_provider_health_terminal_contract(subclass) is None

    class HostileMetaclass(type):
        def __hash__(cls):
            raise AssertionError("provider classification must not invoke metaclass hash")

        def __eq__(cls, other):
            raise AssertionError("provider classification must not invoke metaclass equality")

    class OpaqueProvider(metaclass=HostileMetaclass):
        __slots__ = ()
        uses_http_provider = True
        model = "opaque-provider"

        def evaluate(self, *_args):
            raise AssertionError("offline contract test must not dispatch")

    assert supported_provider_health_terminal_contract(OpaqueProvider()) is None

    # A per-instance dispatch replacement cannot inherit the builtin binding.
    provider.evaluate = lambda *_args: {"ready": {"choice": "yes"}}
    assert supported_provider_health_terminal_contract(provider) is None

    # Identity-bearing fields are part of the binding, not mutable labels.
    provider = supported_typesafe_provider("contract-binding")
    bound = supported_provider_health_terminal_contract(provider)
    provider.model = "changed-contract-binding"
    assert supported_provider_health_terminal_contract(provider) != bound

    from jev_factorio.jev_client import AsyncMockJevClient

    async_contract = supported_provider_health_terminal_contract(AsyncMockJevClient())
    assert async_contract is not None and async_contract[4] == "async"
    assert not provider_health_terminal_contract_is_compatible(
        async_contract, async_decisions=False, has_circuit=False)


def test_builtin_provider_health_contract_rejects_evaluator_code_and_global_drift():
    provider = supported_typesafe_provider("evaluator-implementation-binding")
    assert supported_provider_health_terminal_contract(provider) is not None

    evaluator = jev_client_module.JevClient.evaluate
    original_code = evaluator.__code__

    def alternate_evaluate(self, state, questions):
        return {"answers": state, "questions": questions}

    try:
        # Replacing code on the same function object preserves the old identity
        # check, but must invalidate the provider-health authority.
        evaluator.__code__ = alternate_evaluate.__code__
        assert supported_provider_health_terminal_contract(provider) is None
    finally:
        evaluator.__code__ = original_code

    assert supported_provider_health_terminal_contract(provider) is not None
    original_requests = jev_client_module.requests
    try:
        # The evaluator resolves this global at call time. Rebinding it must
        # not leave the previously registered implementation trusted.
        jev_client_module.requests = SimpleNamespace(post=lambda *_args, **_kwargs: None)
        assert supported_provider_health_terminal_contract(provider) is None
    finally:
        jev_client_module.requests = original_requests

    assert supported_provider_health_terminal_contract(provider) is not None


@pytest.mark.parametrize("dashboard_bound", [False, True], ids=["direct", "dashboard"])
@pytest.mark.parametrize(
    "mutate_before_constructor", [False, True],
    ids=["after-construction", "before-construction"],
)
def test_mutated_builtin_evaluator_code_cannot_resume_legacy_checkpoint(
    tmp_path, monkeypatch, dashboard_bound, mutate_before_constructor,
):
    checkpoint = tmp_path / f"provider-evaluator-code-{dashboard_bound}-{mutate_before_constructor}.json"
    original_checkpoint = legacy_checkpoint(checkpoint, version=2)
    original_state = json.loads(original_checkpoint)
    backend = OfflineRocketBackend()
    original_evaluator = jev_client_module.MockJevClient.evaluate
    original_code = original_evaluator.__code__
    saved_evaluator = FunctionType(
        original_code, original_evaluator.__globals__, original_evaluator.__name__,
        original_evaluator.__defaults__, original_evaluator.__closure__,
    )
    hidden_http_calls = []

    def fake_post(url, **kwargs):
        hidden_http_calls.append({"url": url, "model": kwargs["json"].get("model")})
        mock = MockJevClient()
        answers = MethodType(saved_evaluator, mock)(
            kwargs["json"]["state"], kwargs["json"]["questions"])
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "answers": answers,
                "usage": {"input_tokens": 1},
                "model": "hidden-typesafe-evaluator",
            },
        )

    def compromised_evaluate(self, state, questions):
        hidden = JevClient(
            api_key="offline-test-only",
            base_url="https://typesafe.example.invalid/v1/systemone",
            model="hidden-typesafe-evaluator",
        )
        return hidden.evaluate(state, questions)

    monkeypatch.setattr(jev_client_module.requests, "post", fake_post)
    if mutate_before_constructor:
        original_evaluator.__code__ = compromised_evaluate.__code__
    try:
        provider = MockJevClient()
        loop = HierarchicalLoop(
            backend, jev=provider, policy="jev", target="rocket_launch",
            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        )
        if dashboard_bound:
            with EventWriter(tmp_path / "events.jsonl") as writer:
                attach(loop, writer)
                if not mutate_before_constructor:
                    original_evaluator.__code__ = compromised_evaluate.__code__
                assert loop._legacy_provider_health_ready() is False
                record = loop.step()
        else:
            if not mutate_before_constructor:
                original_evaluator.__code__ = compromised_evaluate.__code__
            assert loop._legacy_provider_health_ready() is False
            record = loop.step()
    finally:
        original_evaluator.__code__ = original_code

    assert record["action"] == "observe"
    assert record["requested_model"] == provider.model
    assert hidden_http_calls == []
    assert backend.actions == []
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "blocked" and saved["reason"] == LEGACY_REASON
    assert saved["session_id"] == original_state["session_id"]
    assert saved["active_goal"] == original_state["active_goal"]
    assert saved["history"][:len(original_state["history"])] == original_state["history"]
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in saved.get("history", []))


def test_provider_health_contract_rejects_mutable_default_drift():
    evaluator = jev_client_module.JevClient.evaluate
    provider = supported_typesafe_provider("evaluator-default-binding")
    assert evaluator.__defaults__ is None
    assert supported_provider_health_terminal_contract(provider) is not None
    try:
        evaluator.__defaults__ = ("new-default",)
        assert supported_provider_health_terminal_contract(provider) is None
    finally:
        evaluator.__defaults__ = None
    assert supported_provider_health_terminal_contract(provider) is not None


def _closure_hidden_http_adapter(inner):
    class ClosureBackedHttpAdapter:
        uses_http_provider = True
        base_url = inner.client.base_url
        model = inner.client.model

        def evaluate(self, state, questions):
            return inner.evaluate(state, questions)

    return ClosureBackedHttpAdapter()


@pytest.mark.parametrize("dashboard_bound", [False, True], ids=["direct", "dashboard"])
def test_closure_hidden_circuit_hold_cannot_authorize_legacy_resume(
    tmp_path, dashboard_bound
):
    checkpoint = tmp_path / f"closure-hidden-{dashboard_bound}.json"
    original = legacy_checkpoint(checkpoint, version=2)
    inner_path = tmp_path / "hidden-inner-provider.json"
    provider = DirectCircuitProvider(
        requests.Timeout("offline closure-hidden cooldown"),
        model="closure-hidden-provider",
    )
    inner = ProviderCircuit(provider, inner_path, clock=AdvancingClock())
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}
    with pytest.raises(ProviderBlocked):
        inner.evaluate({"offline": True}, questions)
    inner_state = deepcopy(inner.state)
    inner_sidecar = inner_path.read_bytes()
    setup_calls = provider.calls
    wrapper = _closure_hidden_http_adapter(inner)
    assert vars(wrapper) == {}
    assert any(cell.cell_contents is inner
               for cell in (wrapper.evaluate.__closure__ or ()))

    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(
        backend, jev=wrapper, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    assert loop._legacy_provider_health_ready() is False
    context = (EventWriter(tmp_path / "closure-hidden.jsonl")
               if dashboard_bound else None)
    if context is None:
        record = loop.step()
    else:
        with context as writer:
            attach(loop, writer)
            assert loop._legacy_provider_health_ready() is False
            record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] == wrapper.model
    assert backend.actions == []
    assert provider.calls == setup_calls
    assert inner.state == inner_state
    assert inner_path.read_bytes() == inner_sidecar
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    _assert_legacy_block_is_preserved(checkpoint)
    assert checkpoint.read_bytes() != original  # observation saved; migration did not


def _inject_hidden_provider_circuit(tmp_path, adapter, mutation):
    provider = DirectCircuitProvider(
        requests.Timeout("offline inserted circuit cooldown"),
        model="offline-hidden-circuit-model")
    circuit_path = tmp_path / f"hidden-provider-{mutation}.json"
    circuit = ProviderCircuit(provider, circuit_path, clock=AdvancingClock())
    sidecar = None
    if mutation == "cooldown-circuit":
        questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                               "instructions": "choose yes"}}
        with pytest.raises(ProviderBlocked):
            circuit.evaluate({"offline": True}, questions)
        assert circuit.state["phase"] == "cooldown"
        sidecar = circuit_path.read_bytes()
    else:
        circuit.client = circuit
    adapter.client = circuit
    return provider, circuit, deepcopy(circuit.state), sidecar


@pytest.mark.parametrize("dashboard_bound", [False, True], ids=["plain", "dashboard-observer"])
@pytest.mark.parametrize("mutation", ["cooldown-circuit", "circuit-cycle"])
def test_plain_provider_chain_mutation_fails_closed_without_metadata_or_paid_state_reset(
    tmp_path, dashboard_bound, mutation
):
    checkpoint = tmp_path / f"plain-provider-{dashboard_bound}-{mutation}.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = PlainProviderAdapter()
    loop = HierarchicalLoop(
        backend, jev=provider, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    assert loop._provider_health_chain_valid is True
    # This adapter is intentionally not a registered health authority. A
    # visible or later-mutated ``client`` field cannot grant resume authority.
    assert loop._legacy_provider_health_ready() is False

    if dashboard_bound:
        with EventWriter(tmp_path / f"plain-provider-{mutation}.jsonl") as writer:
            attach(loop, writer)
            observer = loop.jev
            assert observer is not provider
            assert loop._dashboard_provider_observer_binding == (observer, provider)
            assert loop._legacy_provider_health_ready() is False
            hidden_provider, hidden_circuit, hidden_state, hidden_sidecar = (
                _inject_hidden_provider_circuit(tmp_path, provider, mutation))
            assert loop._legacy_provider_health_ready() is False
            assert loop._provider_record_attribute("model") is None
            record = loop.step()
    else:
        hidden_provider, hidden_circuit, hidden_state, hidden_sidecar = (
            _inject_hidden_provider_circuit(tmp_path, provider, mutation))
        assert loop._legacy_provider_health_ready() is False
        assert loop._provider_record_attribute("model") is None
        record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] is None
    assert backend.actions == []
    assert provider.calls == 0
    assert hidden_provider.calls == (1 if mutation == "cooldown-circuit" else 0)
    assert hidden_circuit.client is (
        hidden_circuit if mutation == "circuit-cycle" else hidden_provider)
    assert hidden_circuit.state == hidden_state
    if hidden_sidecar is None:
        assert not hidden_circuit.path.exists()
    else:
        assert hidden_circuit.path.read_bytes() == hidden_sidecar
    assert provider.client is hidden_circuit
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    _assert_legacy_block_is_preserved(checkpoint)
    assert checkpoint.read_bytes() != original  # blocked observation persists; migration does not


@pytest.mark.parametrize("dashboard_bound", [False, True], ids=["plain", "dashboard-observer"])
def test_unregistered_provider_without_circuit_cannot_authorize_legacy_resume(
    tmp_path, dashboard_bound
):
    checkpoint = tmp_path / f"plain-provider-healthy-{dashboard_bound}.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = PlainProviderAdapter()
    loop = HierarchicalLoop(
        backend, jev=provider, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop._provider_health_circuits == ()
    assert loop._legacy_provider_health_ready() is False
    assert loop._provider_record_attribute("model") == provider.model
    if dashboard_bound:
        with EventWriter(tmp_path / "plain-provider-healthy.jsonl") as writer:
            attach(loop, writer)
            assert loop._dashboard_provider_observer_binding == (loop.jev, provider)
            assert loop._legacy_provider_health_ready() is False
            record = loop.step()
    else:
        record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] == provider.model
    assert provider.calls == 0
    assert backend.actions == []
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert checkpoint.read_bytes() != original
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "blocked" and saved["reason"] == LEGACY_REASON
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in saved["history"])


@pytest.mark.parametrize("mutate_before_constructor", [False, True],
                         ids=["after-construction", "before-construction"])
def test_http_provider_cannot_disable_its_durable_health_requirement(
    tmp_path, monkeypatch, mutate_before_constructor
):
    checkpoint = tmp_path / f"http-health-marker-{mutate_before_constructor}.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider("health-marker-binding")
    if mutate_before_constructor:
        provider.uses_http_provider = False
    loop = HierarchicalLoop(
        backend, jev=provider, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    circuit = loop._provider_health_circuit
    if not mutate_before_constructor:
        assert circuit is not None
        provider.uses_http_provider = False
    forbidden_http = install_provider_error(
        monkeypatch, AssertionError("blocked migration must not dispatch HTTP"))

    assert loop._legacy_provider_health_ready() is False
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert forbidden_http == []
    if mutate_before_constructor:
        assert loop._provider_health_terminal_contract is None
    else:
        assert loop._provider_health_terminal_contract is not None
    assert supported_provider_health_terminal_contract(provider) is None
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in loop.memory.history)
    _assert_legacy_block_is_preserved(checkpoint)
    assert checkpoint.read_bytes() != original


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


@pytest.mark.parametrize("version", [1, 2], ids=["v1", "v2"])
def test_checkpoint_bound_healthy_provider_circuit_is_reused_and_can_resume(
    tmp_path, monkeypatch, version
):
    checkpoint = tmp_path / f"direct-circuit-healthy-v{version}.json"
    original = legacy_checkpoint(checkpoint, version=version)
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider()
    provider_calls = install_provider_error(
        monkeypatch, AssertionError("legacy resume must not dispatch a model request"))
    circuit = ProviderCircuit(provider, _provider_path(checkpoint))
    before_state = deepcopy(circuit.state)

    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop.jev is circuit
    assert loop._provider_health_circuits == (circuit,)
    assert loop._provider_health_chain_valid is True
    assert loop._legacy_provider_health_ready() is True
    assert checkpoint.read_bytes() == original
    assert not circuit.path.exists()

    with EventWriter(tmp_path / f"direct-circuit-healthy-v{version}.jsonl") as writer:
        attach(loop, writer)
        record = loop.step()

    assert record["action"] == "factory_launch_pad"
    assert record["requested_model"] == provider.model
    assert [action for action, _ in backend.actions] == ["factory_launch_pad"]
    assert provider_calls == []
    assert loop._provider_health_circuit is circuit
    assert circuit.state == before_state
    assert not circuit.path.exists()
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "running"
    assert [row["kind"] for row in saved["history"]].count(
        "legacy_capability_block_resumed") == 1


@pytest.mark.parametrize(
    "failure,expected_phase,attempts",
    [
        (requests.Timeout("offline timeout"), "cooldown", 1),
        (_http_error(401), "exhausted", 3),
    ],
    ids=["cooldown-is-retained", "auth-budget-is-retained"],
)
@pytest.mark.parametrize("version", [1, 2], ids=["v1", "v2"])
def test_supplied_circuit_holds_legacy_migration_without_resetting_state(
    tmp_path, failure, expected_phase, attempts, version
):
    checkpoint = tmp_path / f"direct-circuit-{expected_phase}-v{version}.json"
    original = legacy_checkpoint(checkpoint, version=version)
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider(failure)
    clock = AdvancingClock()
    circuit = ProviderCircuit(provider, _provider_path(checkpoint), clock=clock)
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}

    for _ in range(attempts):
        if circuit.state["phase"] != "healthy":
            clock.reach_next_probe(circuit)
        with pytest.raises(ProviderBlocked):
            circuit.evaluate({"offline": True}, questions)

    assert circuit.state["phase"] == expected_phase
    assert circuit.state["attempts"] == attempts
    assert circuit.state["in_flight"] is None
    prior_state = deepcopy(circuit.state)
    prior_sidecar = circuit.path.read_bytes()
    assert checkpoint.read_bytes() == original

    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    assert loop.jev is circuit
    assert loop._provider_health_circuits == (circuit,)

    with EventWriter(tmp_path / f"direct-circuit-hold-v{version}.jsonl") as writer:
        attach(loop, writer)
        record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert provider.calls == attempts
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert circuit.state == prior_state
    assert circuit.path.read_bytes() == prior_sidecar
    _assert_legacy_block_is_preserved(checkpoint)
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in loop.memory.history)


def test_nested_typed_provider_circuits_retain_inner_health_and_both_sidecars(
    tmp_path, monkeypatch
):
    checkpoint = tmp_path / "nested-provider.json"
    original = legacy_checkpoint(checkpoint, version=2)
    inner_path = tmp_path / "inner-provider.json"
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider("nested-health-provider")
    request_calls = install_provider_error(monkeypatch, requests.Timeout("offline inner timeout"))
    clock = AdvancingClock()
    inner = ProviderCircuit(provider, inner_path, clock=clock)
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}
    with pytest.raises(ProviderBlocked):
        inner.evaluate({"offline": True}, questions)
    outer = ProviderCircuit(inner, _provider_path(checkpoint))
    inner_state = deepcopy(inner.state)
    outer_state = deepcopy(outer.state)
    inner_sidecar = inner_path.read_bytes()
    checkpoint_sidecar_existed = outer.path.exists()

    loop = HierarchicalLoop(
        backend, jev=outer, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop.jev is outer
    assert loop._provider_health_circuits == (outer, inner)
    assert loop._legacy_provider_health_ready() is False
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] == provider.model
    assert backend.actions == []
    assert request_calls == [provider.base_url]
    assert outer.state == outer_state
    assert inner.state == inner_state
    assert inner_path.read_bytes() == inner_sidecar
    assert outer.path.exists() is checkpoint_sidecar_existed
    _assert_legacy_block_is_preserved(checkpoint)
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON


def test_healthy_nested_typed_provider_chain_remains_eligible(tmp_path, monkeypatch):
    checkpoint = tmp_path / "nested-provider-healthy.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider("nested-healthy-provider")
    provider_calls = install_provider_error(
        monkeypatch, AssertionError("legacy resume must not dispatch a model request"))
    inner = ProviderCircuit(provider, tmp_path / "inner-healthy-provider.json")
    outer = ProviderCircuit(inner, _provider_path(checkpoint))
    inner_state = deepcopy(inner.state)
    outer_state = deepcopy(outer.state)

    loop = HierarchicalLoop(
        backend, jev=outer, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop._provider_health_chain_valid is True
    assert loop._legacy_provider_health_ready() is True
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "factory_launch_pad"
    assert [action for action, _ in backend.actions] == ["factory_launch_pad"]
    assert provider_calls == []
    assert outer.state == outer_state
    assert inner.state == inner_state
    assert not outer.path.exists()
    assert not inner.path.exists()
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "running"
    assert [row["kind"] for row in saved["history"]].count(
        "legacy_capability_block_resumed") == 1


def test_post_construction_same_identity_client_replacement_fails_closed(tmp_path):
    checkpoint = tmp_path / "replaced-terminal-client.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    original_client = supported_typesafe_provider("replacement-bind-model")
    circuit = ProviderCircuit(original_client, _provider_path(checkpoint))
    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    replacement_client = supported_typesafe_provider("replacement-bind-model")
    prior_state = deepcopy(circuit.state)

    # A new client with the same serialized provider identity is still a
    # different authority from the one retained at construction.
    circuit.client = replacement_client

    assert loop._legacy_provider_health_ready() is False
    record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] is None
    assert backend.actions == []
    assert original_client.model == replacement_client.model
    assert loop._provider_health_client is original_client
    assert circuit.client is replacement_client
    assert circuit.state == prior_state
    assert not circuit.path.exists()
    _assert_legacy_block_is_preserved(checkpoint)
    assert checkpoint.read_bytes() != original  # ordinary blocked observation is saved


def test_post_construction_unhealthy_nested_circuit_replacement_fails_closed(
    tmp_path, monkeypatch
):
    checkpoint = tmp_path / "replaced-nested-client.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    original_client = supported_typesafe_provider("outer-provider-model")
    outer = ProviderCircuit(original_client, _provider_path(checkpoint))
    loop = HierarchicalLoop(
        backend, jev=outer, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    replacement_client = supported_typesafe_provider("replacement-provider-identity")
    replacement_path = tmp_path / "replacement-inner-provider.json"
    replacement_clock = AdvancingClock()
    inner = ProviderCircuit(replacement_client, replacement_path, clock=replacement_clock)
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}
    request_calls = install_provider_error(
        monkeypatch, requests.Timeout("offline nested replacement timeout"))
    with pytest.raises(ProviderBlocked):
        inner.evaluate({"offline": True}, questions)
    inner_state = deepcopy(inner.state)
    inner_sidecar = replacement_path.read_bytes()
    outer_state = deepcopy(outer.state)
    outer.client = inner

    assert loop._legacy_provider_health_ready() is False
    record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] is None
    assert backend.actions == []
    assert request_calls == [replacement_client.base_url]
    assert outer.client is inner
    assert outer.state == outer_state
    assert inner.state == inner_state
    assert replacement_path.read_bytes() == inner_sidecar
    _assert_legacy_block_is_preserved(checkpoint)
    assert checkpoint.read_bytes() != original  # ordinary blocked observation is saved


def test_post_construction_provider_circuit_cycle_fails_closed(tmp_path, monkeypatch):
    checkpoint = tmp_path / "replaced-circuit-cycle.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider("cycle-provider")
    provider_calls = install_provider_error(
        monkeypatch, AssertionError("legacy migration must not dispatch a model request"))
    circuit = ProviderCircuit(provider, _provider_path(checkpoint))
    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    prior_state = deepcopy(circuit.state)
    circuit.client = circuit

    assert loop._legacy_provider_health_ready() is False
    record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] is None
    assert backend.actions == []
    assert provider_calls == []
    assert circuit.client is circuit
    assert circuit.state == prior_state
    assert not circuit.path.exists()
    _assert_legacy_block_is_preserved(checkpoint)
    assert checkpoint.read_bytes() != original  # ordinary blocked observation is saved


def test_generic_wrapper_cannot_hide_a_typed_provider_circuit_hold(tmp_path):
    class ProviderWrapper:
        uses_http_provider = True

        def __init__(self, client):
            self.client = client
            # Arbitrary wrapper state is not a provider-health authority.
            self.state = {"phase": "healthy"}

        def __getattr__(self, name):
            return getattr(self.client, name)

        def evaluate(self, state, questions):
            return self.client.evaluate(state, questions)

    checkpoint = tmp_path / "wrapped-provider.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider(requests.Timeout("offline wrapped timeout"))
    inner_path = tmp_path / "wrapped-inner-provider.json"
    inner = ProviderCircuit(provider, inner_path, clock=AdvancingClock())
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}
    with pytest.raises(ProviderBlocked):
        inner.evaluate({"offline": True}, questions)
    prior_state = deepcopy(inner.state)
    prior_sidecar = inner_path.read_bytes()
    wrapper = ProviderWrapper(inner)

    loop = HierarchicalLoop(
        backend, jev=wrapper, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert isinstance(loop.jev, ProviderCircuit)
    assert loop._provider_health_chain_valid is False
    assert loop._legacy_provider_health_ready() is False
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert provider.calls == 1
    assert wrapper.state == {"phase": "healthy"}
    assert inner.state == prior_state
    assert inner_path.read_bytes() == prior_sidecar
    _assert_legacy_block_is_preserved(checkpoint)


class _SlottedProviderWrapperBehavior:
    __slots__ = ()
    uses_http_provider = True

    def __init__(self, client):
        self.client = client
        self.dict_lookups = 0

    def __getattr__(self, name):
        if name == "__dict__":
            self.dict_lookups += 1
            raise AttributeError(name)
        return getattr(self.client, name)

    def evaluate(self, state, questions):
        return self.client.evaluate(state, questions)


class _PureSlotsBase:
    __slots__ = ()


class _DictionaryBase:
    pass


class _PureSlottedProviderWrapper(_PureSlotsBase, _SlottedProviderWrapperBehavior):
    __slots__ = ("client", "dict_lookups")


class _DictionaryBackedSlottedProviderWrapper(_DictionaryBase, _SlottedProviderWrapperBehavior):
    __slots__ = ("client", "dict_lookups")


@pytest.mark.parametrize("wrapper_type", [
    _PureSlottedProviderWrapper,
    _DictionaryBackedSlottedProviderWrapper,
], ids=["pure-slots", "inherited-dict-plus-slots"])
@pytest.mark.parametrize("supplied_outer", [False, True],
                         ids=["controller-wraps-http-adapter", "caller-supplies-outer-circuit"])
def test_slotted_wrapper_cannot_hide_a_typed_provider_circuit_hold(
    tmp_path, wrapper_type, supplied_outer
):

    checkpoint = tmp_path / ("slotted-explicit.json" if supplied_outer
                             else "slotted-auto.json")
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider(requests.Timeout("offline slotted setup timeout"))
    inner_path = tmp_path / "slotted-inner-provider.json"
    inner = ProviderCircuit(provider, inner_path, clock=AdvancingClock())
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}
    with pytest.raises(ProviderBlocked):
        inner.evaluate({"offline": True}, questions)
    assert inner.state["phase"] == "cooldown"
    prior_inner = deepcopy(inner.state)
    prior_inner_sidecar = inner_path.read_bytes()
    wrapper = wrapper_type(inner)

    if supplied_outer:
        outer_path = _provider_path(checkpoint)
        outer = ProviderCircuit(wrapper, outer_path)
        prior_outer = deepcopy(outer.state)
        # The constructor rejects an opaque retained terminal before a public
        # loop can treat the outer circuit as the complete health authority.
        with pytest.raises(ValueError):
            HierarchicalLoop(
                backend, jev=outer, policy="deterministic", target="rocket_launch",
                checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
        assert not outer_path.exists()
        assert outer.state == prior_outer
        assert inner.state == prior_inner
        assert inner_path.read_bytes() == prior_inner_sidecar
        assert backend.actions == []
        assert provider.calls == 1  # only the offline cooldown setup
        assert wrapper.dict_lookups == 0
        assert checkpoint.read_bytes() == original
        return

    loop = HierarchicalLoop(
        backend, jev=wrapper, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    outer = loop.jev
    prior_outer = deepcopy(outer.state)
    record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] is None
    assert backend.actions == []
    assert provider.calls == 1  # no provider retry after the offline setup
    assert wrapper.dict_lookups == 0
    assert loop._provider_health_chain_valid is False
    assert outer.state == prior_outer
    assert inner.state == prior_inner
    assert inner_path.read_bytes() == prior_inner_sidecar
    _assert_legacy_block_is_preserved(checkpoint)
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)


def test_healthy_phase_with_live_provider_reservation_does_not_resume(tmp_path, monkeypatch):
    checkpoint = tmp_path / "provider-reservation.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider("reservation-provider")
    request_calls = install_provider_error(
        monkeypatch, SystemExit("offline simulated process crash after reservation"))
    circuit = ProviderCircuit(provider, _provider_path(checkpoint), clock=lambda: 1234.0)
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}

    with pytest.raises(SystemExit, match="simulated process crash"):
        circuit.evaluate({"offline": True}, questions)
    assert circuit.state["phase"] == "healthy"
    assert circuit.state["in_flight"]["healthy_start"] is True
    prior_state = deepcopy(circuit.state)
    prior_sidecar = circuit.path.read_bytes()

    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop._legacy_provider_health_ready() is False
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert request_calls == [provider.base_url]
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert circuit.state == prior_state
    assert circuit.path.read_bytes() == prior_sidecar
    _assert_legacy_block_is_preserved(checkpoint)


def test_circuit_bound_to_another_checkpoint_is_not_silently_rebound(tmp_path):
    checkpoint = tmp_path / "controller.json"
    original = legacy_checkpoint(checkpoint, version=2)
    other_path = tmp_path / "different-run" / "provider.json"
    other_path.parent.mkdir()
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider("wrong-checkpoint-provider")
    circuit = ProviderCircuit(provider, other_path)
    prior_state = deepcopy(circuit.state)

    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop.jev is circuit
    assert loop._legacy_provider_health_ready() is False
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert circuit.state == prior_state
    assert not other_path.exists()
    _assert_legacy_block_is_preserved(checkpoint)


def test_stale_healthy_circuit_cannot_override_newer_sidecar_hold(tmp_path, monkeypatch):
    checkpoint = tmp_path / "stale-sidecar.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider_path = _provider_path(checkpoint)
    stale_client = supported_typesafe_provider("shared-sidecar-model")
    stale_circuit = ProviderCircuit(stale_client, provider_path)
    failing_client = supported_typesafe_provider("shared-sidecar-model")
    newer_circuit = ProviderCircuit(failing_client, provider_path, clock=AdvancingClock())
    request_calls = install_provider_error(monkeypatch, requests.Timeout("offline newer timeout"))
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}

    with pytest.raises(ProviderBlocked):
        newer_circuit.evaluate({"offline": True}, questions)
    newer_state = deepcopy(newer_circuit.state)
    assert stale_circuit.state["phase"] == "healthy"
    assert newer_state["phase"] == "cooldown"
    assert newer_state["identity"] == stale_circuit.identity
    sidecar_bytes = newer_circuit.path.read_bytes()
    assert checkpoint.read_bytes() == original

    loop = HierarchicalLoop(
        backend, jev=stale_circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop._legacy_provider_health_ready() is False
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert request_calls == [failing_client.base_url]
    assert newer_circuit.path.read_bytes() == sidecar_bytes
    assert stale_circuit.state["phase"] == "healthy"
    _assert_legacy_block_is_preserved(checkpoint)


def test_provider_sidecar_identity_mismatch_fails_closed_without_rebinding(tmp_path):
    checkpoint = tmp_path / "sidecar-identity.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider("sidecar-identity-provider")
    circuit = ProviderCircuit(provider, _provider_path(checkpoint))
    bad_sidecar = deepcopy(circuit.state)
    bad_sidecar["identity"] = "0" * 64
    atomic_json(circuit.path, bad_sidecar)
    sidecar_bytes = circuit.path.read_bytes()
    original_memory = deepcopy(circuit.state)

    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop._legacy_provider_health_ready() is False
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert circuit.state == original_memory
    assert circuit.path.read_bytes() == sidecar_bytes
    _assert_legacy_block_is_preserved(checkpoint)


def test_provider_identity_change_after_circuit_creation_fails_closed(tmp_path):
    checkpoint = tmp_path / "provider-identity.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider("identity-change-provider")
    circuit = ProviderCircuit(provider, _provider_path(checkpoint))
    prior_state = deepcopy(circuit.state)
    provider.model = "changed-provider-identity"

    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop.jev is circuit
    assert loop._legacy_provider_health_ready() is False
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert circuit.state == prior_state
    _assert_legacy_block_is_preserved(checkpoint)


@pytest.mark.parametrize("shape", ["cycle", "too-deep"], ids=["cycle", "depth-bound"])
def test_malformed_provider_circuit_chain_fails_closed(tmp_path, shape):
    checkpoint = tmp_path / f"provider-chain-{shape}.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = supported_typesafe_provider(f"malformed-chain-{shape}")
    if shape == "cycle":
        circuit = ProviderCircuit(provider, _provider_path(checkpoint))
        circuit.client = circuit
    else:
        circuit = provider
        for index in range(9):
            circuit = ProviderCircuit(
                circuit,
                _provider_path(checkpoint) if index == 8 else None,
            )

    with pytest.raises(ValueError, match="cyclic or too deep"):
        HierarchicalLoop(
            backend, jev=circuit, policy="deterministic", target="rocket_launch",
            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert backend.actions == []
    assert checkpoint.read_bytes() == original
