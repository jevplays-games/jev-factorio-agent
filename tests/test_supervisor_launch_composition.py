"""Supervisor checkpoint preflight follows the controller composition it will launch."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from jev_factorio.background import BackgroundMemory, BackgroundWorkLoop
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.campaign_controller import campaign_loop_type
from jev_factorio.coal_controller import coal_loop_type
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.input_controller import input_loop_type
from jev_factorio.memory import CampaignMemory
from jev_factorio.outpost_controller import outpost_loop_type
from jev_factorio.solid_controller import solid_loop_type
from jev_factorio.successor_controller import successor_loop_type
from jev_factorio.supervisor import Supervisor, SupervisorConfig
from jev_factorio.treatment import SCHEMA, SCHEMA_V2, digest as treatment_digest, validate as validate_treatment
from jev_factorio.coal_supply import intents
from test_background_work import ReceiptBackend, ScenarioLoop


SESSION = "launch-composition"
TARGET = "rocket_launch"


_CASES = [
    ("legacy", {"factory_scheduling": "serial"}, None),
    ("background", {"factory_scheduling": "ready-work", "background_work": True}, None),
    ("output", {"factory_scheduling": "ready-work", "furnace_output_buffers": True}, None),
    ("input", {"factory_scheduling": "ready-work", "furnace_output_buffers": True,
                "furnace_input_belts": True}, None),
    ("outpost", {"factory_scheduling": "ready-work", "furnace_output_buffers": True,
                  "furnace_input_belts": True, "mining_outposts": True}, None),
    ("successor", {"factory_scheduling": "ready-work", "background_work": True,
                    "furnace_output_buffers": True, "furnace_input_belts": True,
                    "ore_side_successors": True}, None),
    ("solid_treatment", {"factory_scheduling": "ready-work"}, "solid"),
    ("coal_treatment", {"factory_scheduling": "ready-work"}, "coal"),
    ("coal_treatment_v2", {"factory_scheduling": "ready-work"}, "coal_v2"),
    ("campaign_diagnostics", {"campaign_diagnostics": True}, None),
]


def _treatment(kind):
    value = {
        "schema": SCHEMA,
        "solid_intents": intents(["burner-a", "burner-b"]),
        "coal_targets": [],
        "solid_science_policy": False,
        "coal_kit_policy": False,
    }
    if kind in {"coal", "coal_v2"}:
        value["coal_targets"] = ["burner-a", "burner-b"]
        value["coal_kit_policy"] = True
    if kind == "coal_v2":
        value["schema"] = SCHEMA_V2
        value["coal_economic_admission"] = True
    return value


def _memory_type(flags, treatment_value=None):
    """Mirror main.cli's type-composition order without constructing a backend."""
    loop_type = HierarchicalLoop
    if flags.get("background_work"):
        loop_type = BackgroundWorkLoop
    if flags.get("furnace_output_buffers"):
        loop_type = buffered_loop_type(loop_type)
    if flags.get("furnace_input_belts"):
        loop_type = input_loop_type(loop_type)
    if flags.get("ore_side_successors"):
        loop_type = successor_loop_type(loop_type)
    if flags.get("mining_outposts"):
        loop_type = outpost_loop_type(loop_type)
    if treatment_value is not None:
        loop_type = solid_loop_type(loop_type)
        if treatment_value["coal_targets"]:
            loop_type = coal_loop_type(loop_type)
    if flags.get("campaign_diagnostics"):
        loop_type = campaign_loop_type(loop_type)
    return loop_type.memory_type


def _produce_composed_checkpoint(path, flags, treatment_value=None):
    base = CampaignMemory(SESSION, TARGET, status="running", last_tick=7,
                          failures={"retained-plan": 2},
                          history=[{"kind": "prior_receipt", "receipt": "keep"}])
    base.save(path)
    memory_type = _memory_type(flags, treatment_value)
    if treatment_value is None:
        produced = memory_type.from_bytes(path.read_bytes(), SESSION, TARGET)
    else:
        epoch = {"actor_index": 1, "surface_index": 1, "force_index": 1}
        fields = {
            "solid_routes_schema": 1,
            "solid_science_policy": treatment_value["solid_science_policy"],
            "solid_intents": treatment_value["solid_intents"],
            "solid_epoch": epoch,
            "solid_commitments": {},
            "solid_funding": None,
            "solid_funding_catalogs": {},
        }
        if "coal_supply_schema" in memory_type.__dataclass_fields__:
            fields.update(
                coal_supply_schema=2 if treatment_value.get("coal_economic_admission") else 1,
                coal_kit_policy=treatment_value["coal_kit_policy"],
                coal_economic_admission=treatment_value.get("coal_economic_admission", False),
                coal_funding=None,
                coal_targets=treatment_value["coal_targets"],
                coal_epoch=epoch,
                coal_commitments={},
            )
        produced = memory_type.from_bytes(
            json.dumps({**asdict(base), **fields}).encode(), SESSION, TARGET)
    produced.save(path)
    # Exercise the exact final reader as well as the composed producer path.
    memory_type.load(path, SESSION, TARGET)
    return memory_type


def _owner(tmp_path, checkpoint, *, flags=None, session_id=SESSION, launches=None):
    flags = dict(flags or {})
    state_dir = tmp_path / "supervisor-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    launches = launches if launches is not None else []

    def forbidden_process(command, **kwargs):
        launches.append(command)
        raise AssertionError("offline supervisor test must not start a child process")

    config_options = {"factory_scheduling": "serial", **flags}
    config = SupervisorConfig(
        state_dir=state_dir, checkpoint=checkpoint, session_id=session_id,
        started_at=1000, repair_command=["synthetic-no-process"], cwd=tmp_path,
        duration_hours=0.01, poll_seconds=1, **config_options,
    )
    owner = Supervisor(config, clock=lambda: 1000, sleep=lambda _: None,
                       popen=forbidden_process)
    owner.lock_path = lambda: tmp_path / "supervisor.lock"
    owner.snapshot_revision = lambda manual=False: {"commit": "fixture", "diff": ""}
    owner.kill_group = lambda pid: None
    return owner, launches


@pytest.fixture(autouse=True)
def dummy_provider_credentials(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-no-request")
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)


@pytest.mark.parametrize("name,flags,treatment_kind", _CASES, ids=[row[0] for row in _CASES])
def test_initialize_accepts_checkpoint_through_selected_launch_composition(
        tmp_path, name, flags, treatment_kind):
    treatment_value = _treatment(treatment_kind) if treatment_kind else None
    launch_flags = dict(flags)
    if treatment_value is not None:
        treatment_path = tmp_path / "treatment.json"
        treatment_path.write_text(json.dumps(treatment_value))
        launch_flags["production_treatment"] = treatment_path

    checkpoint = tmp_path / "checkpoint.json"
    _produce_composed_checkpoint(checkpoint, flags, treatment_value)
    original = checkpoint.read_bytes()
    owner, launches = _owner(tmp_path, checkpoint, flags=launch_flags)

    owner.initialize(record_only=True)

    assert checkpoint.read_bytes() == original
    assert owner.state["gameplay_configuration"]["factory_scheduling"] == flags.get(
        "factory_scheduling", "serial")
    command = owner.gameplay_command()
    for field in ("background_work", "furnace_output_buffers", "furnace_input_belts",
                  "mining_outposts", "ore_side_successors", "campaign_diagnostics"):
        assert (("--" + field.replace("_", "-")) in command) is bool(flags.get(field, False))
    if treatment_value is not None:
        assert "--production-treatment" in command
    assert launches == []


@pytest.mark.parametrize("mismatch", [
    "solid_policy", "solid_intents", "coal_targets", "coal_kit_policy",
    "coal_economic_schema", "treatment_omitted",
])
def test_treatment_mismatch_rejects_before_first_configuration_pin(
        tmp_path, mismatch):
    checkpoint_kind = "coal_v2" if mismatch == "coal_economic_schema" else (
        "coal" if mismatch in {"coal_targets", "coal_kit_policy"} else "solid")
    captured_treatment = _treatment(checkpoint_kind)
    checkpoint = tmp_path / "checkpoint.json"
    _produce_composed_checkpoint(
        checkpoint, {"factory_scheduling": "ready-work"}, captured_treatment)
    original = checkpoint.read_bytes()

    flags = {"factory_scheduling": "ready-work"}
    if mismatch != "treatment_omitted":
        configured = deepcopy(captured_treatment)
        if mismatch == "solid_policy":
            configured["solid_science_policy"] = not configured["solid_science_policy"]
        elif mismatch == "solid_intents":
            configured["solid_intents"] = intents(["burner-a", "burner-c"])
        elif mismatch == "coal_targets":
            configured["coal_targets"].reverse()
        elif mismatch == "coal_kit_policy":
            configured["coal_kit_policy"] = not configured["coal_kit_policy"]
        elif mismatch == "coal_economic_schema":
            configured = _treatment("coal")
        validate_treatment(configured)
        treatment_path = tmp_path / "treatment.json"
        treatment_path.write_text(json.dumps(configured))
        flags["production_treatment"] = treatment_path

    owner, launches = _owner(tmp_path, checkpoint, flags=flags)
    with pytest.raises(ValueError, match="treatment|checkpoint"):
        owner.initialize(record_only=True)

    assert not owner.state_path.exists()
    assert checkpoint.read_bytes() == original
    assert launches == []


def test_mismatched_first_treatment_can_retry_matching_without_resetting_campaign(tmp_path):
    retained = _treatment("solid")
    checkpoint = tmp_path / "checkpoint.json"
    _produce_composed_checkpoint(checkpoint, {"factory_scheduling": "ready-work"}, retained)
    original = checkpoint.read_bytes()
    configured = deepcopy(retained)
    configured["solid_science_policy"] = True
    treatment_path = tmp_path / "treatment.json"
    treatment_path.write_text(json.dumps(configured))
    owner, launches = _owner(
        tmp_path, checkpoint,
        flags={"factory_scheduling": "ready-work", "production_treatment": treatment_path})

    with pytest.raises(ValueError, match="treatment"):
        owner.initialize(record_only=True)
    assert not owner.state_path.exists()
    assert checkpoint.read_bytes() == original

    treatment_path.write_text(json.dumps(retained))
    owner.initialize(record_only=True)

    assert owner.state["started_at"] == 1000
    assert owner.state["cutoff"] == 1036
    assert owner.state["gameplay_configuration"]["production_treatment_sha256"] == treatment_digest(retained)
    assert checkpoint.read_bytes() == original
    assert launches == []


def test_matching_true_solid_policy_treatment_remains_accepted(tmp_path):
    retained = _treatment("solid")
    retained["solid_science_policy"] = True
    treatment_path = tmp_path / "treatment.json"
    treatment_path.write_text(json.dumps(retained))
    checkpoint = tmp_path / "checkpoint.json"
    _produce_composed_checkpoint(checkpoint, {"factory_scheduling": "ready-work"}, retained)
    original = checkpoint.read_bytes()
    owner, launches = _owner(
        tmp_path, checkpoint,
        flags={"factory_scheduling": "ready-work", "production_treatment": treatment_path})

    owner.initialize(record_only=True)

    assert owner.state["gameplay_configuration"]["production_treatment_sha256"] == treatment_digest(retained)
    assert checkpoint.read_bytes() == original
    assert launches == []


def test_bound_treatment_checkpoint_mismatch_preserves_immutable_state(tmp_path):
    retained = _treatment("solid")
    treatment_path = tmp_path / "treatment.json"
    treatment_path.write_text(json.dumps(retained))
    checkpoint = tmp_path / "checkpoint.json"
    _produce_composed_checkpoint(checkpoint, {"factory_scheduling": "ready-work"}, retained)
    original_checkpoint = checkpoint.read_bytes()
    owner, launches = _owner(
        tmp_path, checkpoint,
        flags={"factory_scheduling": "ready-work", "production_treatment": treatment_path})
    owner.initialize(record_only=True)
    original_state = owner.state_path.read_bytes()

    changed = deepcopy(retained)
    changed["solid_science_policy"] = True
    treatment_path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="treatment|changed"):
        owner.initialize(record_only=True)

    assert owner.state_path.read_bytes() == original_state
    assert checkpoint.read_bytes() == original_checkpoint
    assert launches == []


@pytest.mark.parametrize("name,flags,treatment_kind", [
    row for row in _CASES if row[0] not in {"legacy", "campaign_diagnostics"}
], ids=[row[0] for row in _CASES if row[0] not in {"legacy", "campaign_diagnostics"}])
def test_omitted_extension_flags_reject_before_first_configuration_pin(
        tmp_path, name, flags, treatment_kind):
    treatment_value = _treatment(treatment_kind) if treatment_kind else None
    checkpoint = tmp_path / "checkpoint.json"
    _produce_composed_checkpoint(checkpoint, flags, treatment_value)
    original = checkpoint.read_bytes()
    wrong, launches = _owner(tmp_path, checkpoint, flags={"factory_scheduling": "ready-work"})

    with pytest.raises(ValueError, match="launch composition"):
        wrong.initialize(record_only=True)

    assert not wrong.state_path.exists()
    assert checkpoint.read_bytes() == original
    assert launches == []


def test_active_background_checkpoint_rejects_wrong_first_flags_then_corrected_retry_is_clean(
        tmp_path):
    backend = ReceiptBackend()
    checkpoint = tmp_path / "state.json"
    producer = ScenarioLoop(backend, policy="deterministic", factory_scheduling="ready-work",
                            target=TARGET, checkpoint=str(checkpoint), tick_seconds=0)
    producer.memory = producer.memory_type(
        backend.state.session_id, TARGET, active_goal=TARGET,
        completed_goals={goal: 0 for goal in producer.order[:-1]},
        last_tick=backend.state.tick)
    result = producer.step()
    assert result["background_job"]
    original = checkpoint.read_bytes()
    before = json.loads(original)
    assert before["background_attempt"] and before["background_step"]

    wrong, wrong_launches = _owner(
        tmp_path, checkpoint, session_id=backend.state.session_id,
        flags={"factory_scheduling": "ready-work"})
    with pytest.raises(ValueError, match="launch composition"):
        wrong.initialize(record_only=True)
    assert not wrong.state_path.exists()
    assert checkpoint.read_bytes() == original
    assert wrong_launches == []

    corrected, launches = _owner(
        tmp_path, checkpoint, session_id=backend.state.session_id,
        flags={"factory_scheduling": "ready-work", "background_work": True})
    corrected.initialize(record_only=True)
    command = corrected.gameplay_command()

    assert "--background-work" in command
    assert corrected.state["gameplay_configuration"]["background_work"] is True
    assert corrected.state["started_at"] == 1000
    assert corrected.state["cutoff"] == 1036
    assert checkpoint.read_bytes() == original
    after = json.loads(checkpoint.read_bytes())
    for key in ("background_job", "background_attempt", "background_step", "history",
                "failures", "pending", "attempt", "reservations", "last_tick"):
        assert after[key] == before[key]
    assert launches == []


def test_run_preflights_background_composition_and_matching_terminal_run_never_spawns_child(
        tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    BackgroundMemory(SESSION, TARGET, status="completed", last_tick=7).save(checkpoint)
    original = checkpoint.read_bytes()
    wrong, wrong_launches = _owner(
        tmp_path, checkpoint, flags={"factory_scheduling": "ready-work"})

    with pytest.raises(ValueError, match="launch composition"):
        wrong.run()
    assert not wrong.state_path.exists()
    assert checkpoint.read_bytes() == original
    assert wrong_launches == []

    matching, launches = _owner(
        tmp_path, checkpoint,
        flags={"factory_scheduling": "ready-work", "background_work": True})
    assert matching.run() == 0
    assert matching.state["gameplay_configuration"]["background_work"] is True
    assert matching.state["phase"] == "completed"
    assert checkpoint.read_bytes() == original
    assert launches == []


def test_malformed_checkpoint_rejects_without_supervisor_binding_or_checkpoint_rewrite(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_bytes(b'{"session_id":')
    original = checkpoint.read_bytes()
    owner, launches = _owner(
        tmp_path, checkpoint,
        flags={"factory_scheduling": "ready-work", "background_work": True})

    with pytest.raises(ValueError):
        owner.initialize(record_only=True)

    assert not owner.state_path.exists()
    assert checkpoint.read_bytes() == original
    assert launches == []


def test_existing_bound_composition_cannot_be_silently_changed(tmp_path):
    flags = {"factory_scheduling": "ready-work", "background_work": True}
    checkpoint = tmp_path / "checkpoint.json"
    _produce_composed_checkpoint(checkpoint, flags)
    original_checkpoint = checkpoint.read_bytes()
    owner, launches = _owner(tmp_path, checkpoint, flags=flags)
    owner.initialize(record_only=True)
    original_state = owner.state_path.read_bytes()

    owner.config.background_work = False
    with pytest.raises(ValueError):
        owner.initialize(record_only=True)

    assert owner.state_path.read_bytes() == original_state
    assert checkpoint.read_bytes() == original_checkpoint
    assert launches == []
