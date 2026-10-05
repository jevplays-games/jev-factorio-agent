"""Public CLI resume preflight for the selected composed checkpoint schema."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

import jev_factorio.main as main
from jev_factorio.controller import HierarchicalLoop


class BackendReached(Exception):
    """Forbidden attachment boundary used by offline public-CLI tests."""


SOLID_INTENTS = [{
    "source": "recipe:iron-gear-wheel",
    "target": "recipe:automation-science-pack",
    "item": "iron-gear-wheel",
    "destination": "input",
}]
TREATMENT = {
    "schema": "jev-factorio.production-treatment.v1",
    "solid_intents": SOLID_INTENTS,
    "coal_targets": [],
    "solid_science_policy": False,
    "coal_kit_policy": False,
}


def _composition(name: str):
    from jev_factorio.background import BackgroundWorkLoop
    from jev_factorio.buffer_controller import buffered_loop_type
    from jev_factorio.input_controller import input_loop_type
    from jev_factorio.outpost_controller import outpost_loop_type
    from jev_factorio.solid_controller import solid_loop_type
    from jev_factorio.successor_controller import successor_loop_type

    loop_type = HierarchicalLoop
    flags = []
    if name in {"background", "successor"}:
        loop_type = BackgroundWorkLoop
        flags.append("--background-work")
    if name in {"output", "input", "outpost", "successor"}:
        loop_type = buffered_loop_type(loop_type)
        flags.append("--furnace-output-buffers")
    if name in {"input", "outpost", "successor"}:
        loop_type = input_loop_type(loop_type)
        flags.append("--furnace-input-belts")
    if name == "outpost":
        loop_type = outpost_loop_type(loop_type)
        flags.append("--mining-outposts")
    if name == "successor":
        loop_type = successor_loop_type(loop_type)
        flags.append("--ore-side-successors")
    if name == "treatment":
        loop_type = solid_loop_type(loop_type)
    return loop_type.memory_type, flags


def _produce_checkpoint(path: Path, name: str = "base"):
    memory_type, flags = _composition(name)
    memory = memory_type("cli-preflight-session", "rocket_launch",
                         active_goal="rocket_launch", last_tick=300)
    if name == "treatment":
        memory.solid_intents = json.loads(json.dumps(SOLID_INTENTS))
        memory.solid_epoch = {"actor_index": 1, "surface_index": 1, "force_index": 1}
    memory.save(path)
    # Every positive and adverse mutation begins with an actual producer save
    # that the selected production loader accepts.
    memory_type.load(path, "cli-preflight-session", "rocket_launch")
    return memory_type, flags


def _invoke_cli(monkeypatch, checkpoint: Path, flags=(), *, treatment_path=None,
                policy="deterministic", limit_args=None):
    attempts = []

    def forbidden_backend(name, *args, **kwargs):
        attempts.append((name, kwargs))
        raise BackendReached(name)

    monkeypatch.setattr(main, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setattr(main, "make_backend", forbidden_backend)
    argv = [
        "jev-factorio", "--backend", "fle", "--controller", "hierarchical",
        "--factory-scheduling", "ready-work", "--target", "rocket_launch",
        "--policy", policy, "--resume", "--resume-controller",
        "--checkpoint", str(checkpoint), "--tick-seconds", "0.01",
        *(limit_args if limit_args is not None else ["--steps", "1"]),
        *flags,
    ]
    if treatment_path is not None:
        argv.extend(["--production-treatment", str(treatment_path)])
    monkeypatch.setattr(sys, "argv", argv)
    try:
        main.cli()
    except BackendReached:
        return "backend-boundary", attempts
    except SystemExit as error:
        return error.code, attempts
    raise AssertionError("CLI returned without reaching its expected boundary")


@pytest.mark.parametrize("composition", [
    "output", "input", "outpost", "background", "successor",
])
def test_matching_composed_checkpoint_reaches_only_forbidden_backend_boundary(
        tmp_path, monkeypatch, composition):
    path = tmp_path / "controller.json"
    _, flags = _produce_checkpoint(path, composition)
    before = path.read_bytes()

    result, attempts = _invoke_cli(monkeypatch, path, flags)

    assert result == "backend-boundary"
    assert len(attempts) == 1 and attempts[0][0] == "fle"
    assert path.read_bytes() == before


@pytest.mark.parametrize(("composition", "marker"), [
    ("output", "output_buffers_schema"),
    ("input", "input_routes_schema"),
    ("outpost", "outposts_schema"),
    ("background", "background_schema"),
    ("successor", "successor_schema"),
])
def test_invalid_composed_schema_is_rejected_before_backend_attachment(
        tmp_path, monkeypatch, composition, marker):
    path = tmp_path / "controller.json"
    memory_type, flags = _produce_checkpoint(path, composition)
    data = json.loads(path.read_bytes())
    data[marker] = 999
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        memory_type.load(path, "cli-preflight-session", "rocket_launch")

    result, attempts = _invoke_cli(monkeypatch, path, flags)

    assert result == 2
    assert attempts == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("composition", [
    "output", "input", "outpost", "background", "successor",
])
def test_composition_required_by_checkpoint_cannot_be_omitted_from_cli_flags(
        tmp_path, monkeypatch, composition):
    path = tmp_path / "controller.json"
    _, _ = _produce_checkpoint(path, composition)
    before = path.read_bytes()
    with pytest.raises((TypeError, ValueError)):
        HierarchicalLoop.memory_type.load(path, "cli-preflight-session", "rocket_launch")

    result, attempts = _invoke_cli(monkeypatch, path)

    assert result == 2
    assert attempts == []
    assert path.read_bytes() == before


@pytest.mark.parametrize(("old_composition", "selected_composition"), [
    ("base", "background"),
    ("base", "output"),
    ("output", "input"),
    ("input", "outpost"),
    ("input", "successor"),
])
def test_valid_legacy_idle_checkpoint_can_be_preflighted_for_explicit_composition_migration(
        tmp_path, monkeypatch, old_composition, selected_composition):
    from jev_factorio.memory import CampaignMemory

    path = tmp_path / "controller.json"
    if old_composition == "base":
        CampaignMemory("cli-preflight-session", "rocket_launch",
                      active_goal="rocket_launch", last_tick=300).save(path)
    else:
        _produce_checkpoint(path, old_composition)
    before = path.read_bytes()
    selected_type, flags = _composition(selected_composition)
    selected_type.load(path, "cli-preflight-session", "rocket_launch")

    result, attempts = _invoke_cli(monkeypatch, path, flags)

    assert result == "backend-boundary"
    assert len(attempts) == 1
    assert path.read_bytes() == before


def test_matching_production_treatment_checkpoint_is_preflighted(tmp_path, monkeypatch):
    treatment_path = tmp_path / "treatment.json"
    treatment_path.write_text(json.dumps(TREATMENT), encoding="utf-8")
    path = tmp_path / "controller.json"
    _, _ = _produce_checkpoint(path, "treatment")
    before = path.read_bytes()

    result, attempts = _invoke_cli(monkeypatch, path, treatment_path=treatment_path)

    assert result == "backend-boundary"
    assert len(attempts) == 1
    assert path.read_bytes() == before


def test_treatment_binding_is_rechecked_on_the_selected_capture(tmp_path, monkeypatch):
    path = tmp_path / "controller.json"
    _produce_checkpoint(path, "treatment")
    treatment_path = tmp_path / "treatment.json"
    treatment_path.write_text(json.dumps(TREATMENT), encoding="utf-8")
    before = path.read_bytes()
    original_preflight = main._preflight_selected_checkpoint

    def change_after_earlier_treatment_checks(candidate_path, memory_type, target):
        data = json.loads(Path(candidate_path).read_bytes())
        data["solid_science_policy"] = True
        Path(candidate_path).write_text(json.dumps(data), encoding="utf-8")
        return original_preflight(candidate_path, memory_type, target)

    monkeypatch.setattr(main, "_preflight_selected_checkpoint",
                        change_after_earlier_treatment_checks)

    result, attempts = _invoke_cli(monkeypatch, path, treatment_path=treatment_path)

    assert result == 2
    assert attempts == []
    assert json.loads(path.read_bytes())["solid_science_policy"] is True
    assert path.read_bytes() != before


def test_malformed_resume_checkpoint_is_rejected_without_backend_attempt(
        tmp_path, monkeypatch):
    path = tmp_path / "controller.json"
    _produce_checkpoint(path)
    path.write_bytes(b'{"version":')
    before = path.read_bytes()

    result, attempts = _invoke_cli(monkeypatch, path)

    assert result == 2
    assert attempts == []
    assert path.read_bytes() == before


def test_checkpoint_replaced_during_selected_loader_validation_is_rejected(
        tmp_path, monkeypatch):
    import jev_factorio.memory as memory_module

    path = tmp_path / "controller.json"
    _, flags = _produce_checkpoint(path, "output")
    before = path.read_bytes()
    original_loader = memory_module.load_checkpoint_bytes

    def replace_after_validation(*args, **kwargs):
        result = original_loader(*args, **kwargs)
        path.write_bytes(before + b" ")
        return result

    monkeypatch.setattr(memory_module, "load_checkpoint_bytes", replace_after_validation)

    result, attempts = _invoke_cli(monkeypatch, path, flags)

    assert result == 2
    assert attempts == []
    assert path.read_bytes() == before + b" "


def test_checkpoint_changed_after_preflight_is_rejected_at_attachment_boundary(
        tmp_path, monkeypatch):
    path = tmp_path / "controller.json"
    _, flags = _produce_checkpoint(path, "output")
    before = path.read_bytes()
    original_check = main._checkpoint_capture_matches

    def replace_before_attachment(candidate_path, captured):
        Path(candidate_path).write_bytes(before + b" ")
        return original_check(candidate_path, captured)

    monkeypatch.setattr(main, "_checkpoint_capture_matches", replace_before_attachment)

    result, attempts = _invoke_cli(monkeypatch, path, flags)

    assert result == 2
    assert attempts == []
    assert path.read_bytes() == before + b" "


def test_archived_resume_preflight_verifies_and_closes_each_archive_index(
        tmp_path, monkeypatch):
    import jev_factorio.blocked_recovery_archive as archive_module
    from jev_factorio.blocked_recovery_archive import archive_full_tail
    from test_blocked_recovery_archive import _full_memory

    path = tmp_path / "controller.json"
    memory = _full_memory()
    first_index = archive_full_tail(path, memory)
    memory.save(path)
    first_index.close()
    before = path.read_bytes()
    opened = []
    closed = []
    original_build_index = archive_module.build_index

    def track_index(*args, **kwargs):
        index = original_build_index(*args, **kwargs)
        opened.append(index)
        original_close = index.close

        def close():
            closed.append(index)
            original_close()

        index.close = close
        return index

    monkeypatch.setattr(archive_module, "build_index", track_index)

    result, attempts = _invoke_cli(monkeypatch, path)

    assert result == "backend-boundary"
    assert len(attempts) == 1
    assert len(opened) == 2 and len(closed) == 2
    assert all(index in closed for index in opened)
    assert path.read_bytes() == before


def test_exact_blocked_reevaluation_preflight_composes_before_backend_boundary(
        tmp_path, monkeypatch):
    from jev_factorio import blocked_reevaluation, jev_client, provenance
    from jev_factorio.memory import CampaignMemory

    path = tmp_path / "controller.json"
    CampaignMemory(
        "cli-preflight-session", "rocket_launch", active_goal="rocket_launch",
        last_tick=300, status="blocked", reason="Candidate evidence insufficient",
        stalled_decisions=4).save(path)
    before = path.read_bytes()
    source = {
        "blocked_source_revision": "1" * 40,
        "source_head": "2" * 40,
        "previous_contract_sha256": "a" * 64,
        "decision_contract_sha256": "b" * 64,
    }
    monkeypatch.setattr(blocked_reevaluation, "validate_source_revision",
                        lambda revision: source)
    monkeypatch.setattr(provenance, "gameplay_context", lambda: {})
    monkeypatch.setattr(jev_client, "make_client", lambda **kwargs: object())
    flags = [
        "--reevaluate-blocked-once",
        "--exact-checkpoint-sha256", hashlib.sha256(before).hexdigest(),
        "--blocked-source-revision", source["blocked_source_revision"],
    ]

    result, attempts = _invoke_cli(monkeypatch, path, flags, policy="jev")

    assert result == "backend-boundary"
    assert len(attempts) == 1
    assert path.read_bytes() == before


def test_blocked_reevaluation_remains_bound_to_its_exact_capture(tmp_path, monkeypatch):
    from jev_factorio import blocked_reevaluation, jev_client, provenance
    from jev_factorio.memory import CampaignMemory

    path = tmp_path / "controller.json"
    CampaignMemory(
        "cli-preflight-session", "rocket_launch", active_goal="rocket_launch",
        last_tick=300, status="blocked", reason="Candidate evidence insufficient",
        stalled_decisions=4).save(path)
    before = path.read_bytes()
    source = {
        "blocked_source_revision": "1" * 40,
        "source_head": "2" * 40,
        "previous_contract_sha256": "a" * 64,
        "decision_contract_sha256": "b" * 64,
    }
    monkeypatch.setattr(blocked_reevaluation, "validate_source_revision",
                        lambda revision: source)
    monkeypatch.setattr(provenance, "gameplay_context", lambda: {})
    monkeypatch.setattr(jev_client, "make_client", lambda **kwargs: object())
    original_preflight = main._preflight_selected_checkpoint

    def replace_after_digest_preflight(candidate_path, memory_type, target):
        data = json.loads(Path(candidate_path).read_bytes())
        data["stalled_decisions"] = 5
        Path(candidate_path).write_text(json.dumps(data), encoding="utf-8")
        return original_preflight(candidate_path, memory_type, target)

    monkeypatch.setattr(main, "_preflight_selected_checkpoint",
                        replace_after_digest_preflight)
    flags = [
        "--reevaluate-blocked-once",
        "--exact-checkpoint-sha256", hashlib.sha256(before).hexdigest(),
        "--blocked-source-revision", source["blocked_source_revision"],
    ]

    result, attempts = _invoke_cli(monkeypatch, path, flags, policy="jev")

    assert result == 2
    assert attempts == []
    assert json.loads(path.read_bytes())["stalled_decisions"] == 5
    assert path.read_bytes() != before


def test_compatible_source_authorization_stays_bound_to_migrated_capture(
        tmp_path, monkeypatch):
    import hashlib
    import json
    import requests
    from jev_factorio import jev_client, main, operational_safety
    from jev_factorio.background import BackgroundMemory
    from test_compatible_source_recovery import setup as migration_setup

    backend, original, path, authority = migration_setup(tmp_path, monkeypatch)
    original_job = json.loads(json.dumps(original.memory.background_job))
    original_attempt = json.loads(json.dumps(original.memory.background_attempt))
    original_step = json.loads(json.dumps(original.memory.background_step))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("Provider dispatched"))
    monkeypatch.setattr(operational_safety, "storage_ready", lambda roots, **kwargs: True)
    monkeypatch.setattr(jev_client, "make_client", lambda **kwargs: object())
    authority_path = tmp_path / "authority.json"
    authority_path.write_text(json.dumps(authority, sort_keys=True), encoding="utf-8")
    authority_path.chmod(0o400)
    authority_pin = hashlib.sha256(authority_path.read_bytes()).hexdigest()
    original_preflight = main._preflight_selected_checkpoint

    def replace_after_migration(candidate_path, memory_type, target):
        data = json.loads(Path(candidate_path).read_bytes())
        data["stalled_decisions"] += 1
        Path(candidate_path).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        return original_preflight(candidate_path, memory_type, target)

    monkeypatch.setattr(main, "_preflight_selected_checkpoint", replace_after_migration)
    flags = [
        "--persist-recoverable-blocks", "--background-work",
        "--compatible-source-authorization", str(authority_path),
        "--compatible-source-authorization-sha256", authority_pin,
        "--compatible-source-lock-fd", "9",
    ]

    result, attempts = _invoke_cli(
        monkeypatch, path, flags, policy="jev", limit_args=["--until-complete"])

    assert result == 2
    assert attempts == []
    installed = BackgroundMemory.load(path, authority["session_id"], authority["target"])
    assert installed.compatible_source_recoveries
    assert installed.stalled_decisions == original.memory.stalled_decisions + 1
    assert installed.background_job == original_job
    assert installed.background_attempt == original_attempt
    assert installed.background_step == original_step


@pytest.mark.parametrize("fault", [None, "unknown_reason", "wrong_digest", "missing_source"])
def test_persistent_boiler_recovery_full_cli_preserves_checkpoint(
        tmp_path, monkeypatch, fault):
    from jev_factorio import blocked_persistence, blocked_reevaluation, jev_client, provenance
    from jev_factorio.background import BackgroundWorkLoop
    from jev_factorio.buffer_controller import buffered_loop_type
    from jev_factorio.input_controller import input_loop_type
    from jev_factorio.outpost_controller import outpost_loop_type

    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    memory = kind.memory_type("cli-preflight-session", "rocket_launch",
                              active_goal="rocket_launch", last_tick=300)
    old = {"commit": "1" * 40, "source_sha256": "a" * 64}
    current = {"commit": "2" * 40, "source_sha256": "c" * 64}
    blocked_persistence.record_attempt(memory, old, "d" * 64, None, 290)
    blocked_persistence.finish_attempt(memory, old, "d" * 64, "selected")
    memory.status = "blocked"
    memory.reason = ("unknown failure" if fault == "unknown_reason" else
                     "Current native boiler identity and coal stock are required")
    path = tmp_path / "controller.json"
    memory.save(path)
    before = path.read_bytes()
    source = {"blocked_source_revision": old["commit"], "source_head": current["commit"],
              "previous_contract_sha256": "a" * 64, "decision_contract_sha256": "b" * 64}
    monkeypatch.setattr(blocked_reevaluation, "validate_source_revision", lambda revision: source)
    monkeypatch.setattr(provenance, "gameplay_context", lambda: {
        "run_id": "cli-test", "code_revision": current})
    # No model evaluation or native connection is permitted by this preflight.
    monkeypatch.setattr(jev_client, "make_client", lambda **kwargs: object())
    flags = ["--background-work", "--furnace-output-buffers", "--furnace-input-belts",
             "--mining-outposts", "--campaign-diagnostics", "--profile-observations",
             "--consolidated-observations", "--persist-recoverable-blocks",
             "--persistent-idle-observations", "0", "--reevaluate-blocked-once",
             "--exact-checkpoint-sha256", ("0" * 64 if fault == "wrong_digest" else
                                         hashlib.sha256(before).hexdigest())]
    if fault != "missing_source":
        flags += ["--blocked-source-revision", old["commit"]]
    result, attempts = _invoke_cli(monkeypatch, path, flags, policy="jev",
                                   limit_args=["--until-complete"])
    assert result == ("backend-boundary" if fault is None else 2)
    assert len(attempts) == (1 if fault is None else 0)
    assert path.read_bytes() == before
