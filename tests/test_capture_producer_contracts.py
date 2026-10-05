"""Current producer contracts across capture, verification, and integration analysis.

All native-looking inputs here are synthetic local controls. A successful
capture remains ``native_acceptance=not_accepted``.
"""
from copy import deepcopy
import gzip
import hashlib
import json

import pytest

from jev_factorio.acceptance_io import canonical
from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.campaign_controller import campaign_loop_type
from jev_factorio.coal_controller import coal_loop_type
from jev_factorio.complete_capture import capture, verify
from jev_factorio.integration_evidence import analyze_rows, validate_trial
from jev_factorio.solid_controller import solid_loop_type
from jev_factorio.state import GameSnapshot
from jev_factorio.planning.connection_identity import PREFIX, connection_key
from integration_evidence_fixtures import evidence
from input_routes_fixtures import SOURCE as INPUT_SOURCE, fixture as input_route_fixture, full as full_input_route
from test_complete_capture import test_complete_capture_v2_admission_roundtrip as prepare_complete_fixture
from test_solid_route_integration import Backend as SolidBackend, native_catalog


def _actual_composed_record(tmp_path, trial):
    """Run the real composed _record MRO with bounded synthetic backend state."""
    backend = SolidBackend()
    backend.craft_jobs_supported = True
    backend.coal_supply_supported = True
    backend.checkpoint = tmp_path / "producer-checkpoint.json"
    backend.enable_factory = lambda: native_catalog()
    composed = campaign_loop_type(coal_loop_type(solid_loop_type(BackgroundWorkLoop)))
    configuration = trial["configuration"]
    loop = composed(
        backend,
        target="rocket_launch",
        policy="deterministic",
        factory_scheduling="ready-work",
        tick_seconds=0,
        checkpoint=str(backend.checkpoint),
        solid_intents=deepcopy(trial["solid_intents"]),
        solid_science_policy=False,
        coal_targets=deepcopy(trial["coal_targets"]),
        coal_kit_policy=configuration["coal_kit_policy"],
        coal_economic_admission=configuration.get("coal_economic_admission", False),
        lead_time_supply=True,
        coverage_margin_lookahead=True,
    )
    loop.memory = loop.memory_type(
        backend.state.session_id,
        "rocket_launch",
        active_goal="rocket_launch",
        completed_goals={"stockpile_fuel": 0},
        last_tick=backend.state.tick,
        solid_intents=deepcopy(trial["solid_intents"]),
        solid_science_policy=False,
        solid_epoch={"actor_index": 1, "surface_index": 1, "force_index": 1},
        coal_targets=deepcopy(trial["coal_targets"]),
        coal_kit_policy=configuration["coal_kit_policy"],
        coal_economic_admission=configuration.get("coal_economic_admission", False),
        coal_supply_schema=2 if configuration.get("coal_economic_admission", False) else 1,
        coal_epoch={"actor_index": 1, "surface_index": 1, "force_index": 1},
    )
    return loop._record(backend.state, "observe", "synthetic producer shape", backend.state)


def _inputs(tmp_path):
    prepare_complete_fixture(tmp_path)
    return {
        "rows": [json.loads(line) for line in (tmp_path / "gameplay.jsonl").read_bytes().splitlines()],
        "trial": json.loads((tmp_path / "trial.json").read_bytes()),
        "initial": json.loads((tmp_path / "initial.json").read_bytes()),
        "final": json.loads((tmp_path / "final.json").read_bytes()),
        "preflight": json.loads((tmp_path / "preflight.json").read_bytes()),
        "save": tmp_path / "save.zip",
    }


def _capture_roundtrip(tmp_path, data, label):
    input_dir = tmp_path / f"inputs-{label}"
    input_dir.mkdir()
    initial_bytes = canonical(data["initial"])
    trial = deepcopy(data["trial"])
    trial["initial_checkpoint_sha256"] = hashlib.sha256(initial_bytes).hexdigest()
    preflight = deepcopy(data["preflight"])
    preflight["checkpoint_sha256"] = trial["initial_checkpoint_sha256"]
    paths = {
        "trial": input_dir / "trial.json",
        "initial": input_dir / "initial-checkpoint.json",
        "final": input_dir / "final-checkpoint.json",
        "preflight": input_dir / "preflight.json",
        "gameplay": input_dir / "gameplay.jsonl",
    }
    paths["trial"].write_bytes(canonical(trial))
    paths["initial"].write_bytes(initial_bytes)
    paths["final"].write_bytes(canonical(data["final"]))
    paths["preflight"].write_bytes(canonical(preflight))
    paths["gameplay"].write_bytes(b"".join(canonical(row) for row in data["rows"]))
    output = tmp_path / f"capture-{label}"
    manifest = capture(
        gameplay=paths["gameplay"], trial_path=paths["trial"],
        initial_checkpoint=paths["initial"], final_checkpoint=paths["final"],
        save=data["save"], preflight_path=paths["preflight"], output=output,
    )
    reviewed = verify(output)
    return reviewed, manifest, trial


def _rewrite_capture_rows(directory, mutate):
    """Reseal a valid bundle after an adversary edits projected gameplay."""
    gameplay = directory / "gameplay.jsonl.gz"
    rows = [json.loads(line) for line in gzip.decompress(gameplay.read_bytes()).splitlines()]
    mutate(rows)
    payload = b"".join(canonical(row) for row in rows)
    gameplay.write_bytes(gzip.compress(payload, mtime=0))
    manifest_path = directory / "capture-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["decompressed_bytes"] = len(payload)
    manifest["decompressed_sha256"] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_bytes(canonical(manifest))
    names = sorted(path.name for path in directory.iterdir() if path.name != "SHA256SUMS")
    sums = "".join(
        hashlib.sha256((directory / name).read_bytes()).hexdigest() + "  " + name + "\n"
        for name in names
    )
    (directory / "SHA256SUMS").write_text(sums, encoding="ascii")


def _feature_capture_data(tmp_path):
    data = _inputs(tmp_path)
    producer = _actual_composed_record(tmp_path, data["trial"])
    data["trial"]["configuration"]["background_work"] = True
    data["trial"]["campaign_treatment"] = deepcopy(producer["campaign_treatment"])
    for checkpoint in (data["initial"], data["final"]):
        checkpoint.update(
            background_schema=producer["background_schema"],
            background_job=deepcopy(producer["background_job"]),
            background_attempt=deepcopy(producer["background_attempt"]),
            background_step=None,
        )
    emitted = (
        "factory_scheduling", "mining_outposts",
        "background_work", "background_schema", "background_job", "background_attempt",
        "solid_routes", "solid_route_evidence", "solid_route_fault", "solid_science_policy",
        "solid_funding_schema", "solid_funding", "solid_investment_evidence",
        "coal_supply", "coal_supply_evidence", "coal_supply_fault", "coal_kit_policy",
        "coal_kit_evidence", "coal_economic_admission", "coal_admission_evidence",
    )
    for row in data["rows"]:
        row["acceptance_configuration"]["background_work"] = True
        row["campaign_treatment"] = deepcopy(producer["campaign_treatment"])
        for field in emitted:
            row[field] = deepcopy(producer[field])
    data["producer"] = producer
    return data


def _attribution(identity, connection, count):
    return {
        identity: {
            "count": count,
            "allocations": {connection: count},
            "evidence": "audited legacy connection allocation",
        }
    }


def _install_attribution(data, *, add_final_receipt=False):
    identity = PREFIX
    connection = identity + connection_key({
        "source": "pump", "target": "refinery", "kind": "pipe", "fluid": "crude-oil"})
    failures = {identity: 2, connection: 2}
    receipt = _attribution(identity, connection, 2)
    data["initial"].update(failures=deepcopy(failures),
                           connection_failure_attribution=deepcopy(receipt))
    data["final"].update(failures=deepcopy(failures),
                         connection_failure_attribution=deepcopy(receipt))
    if add_final_receipt:
        identity2 = "later:" + PREFIX
        connection2 = identity2 + connection_key({
            "source": "pump-2", "target": "refinery-2", "kind": "pipe", "fluid": "water"})
        failures.update({identity2: 1, connection2: 1})
        data["final"]["failures"] = deepcopy(failures)
        data["final"]["connection_failure_attribution"].update(_attribution(identity2, connection2, 1))
    for row in data["rows"]:
        row["failure_budgets"] = deepcopy(failures if add_final_receipt else data["final"]["failures"])
    return receipt


def test_actual_composed_record_and_capture_preserve_enabled_and_absent_disabled_fields(tmp_path):
    data = _feature_capture_data(tmp_path)
    producer = data["producer"]
    assert producer["schema_version"] == 2
    assert producer["background_work"] is True
    assert producer["background_schema"] == 2
    assert producer["solid_routes"] is True
    assert producer["solid_funding_schema"] == 1
    assert producer["solid_funding"] is None
    assert producer["coal_supply"] is True
    assert producer["coal_economic_admission"] is True
    assert producer["coal_admission_evidence"] == {}
    assert producer["campaign_treatment"]["coverage_margin_lookahead"] is True
    assert producer["campaign_treatment"]["lead_time_supply"] is True
    assert "furnace_output_buffers" not in producer
    assert "furnace_input_belts" not in producer
    assert producer["mining_outposts"] is False  # The base producer emits this disabled flag.
    assert "ore_side_successors" not in producer

    reviewed, manifest, trial = _capture_roundtrip(tmp_path, data, "actual-composition")
    assert reviewed["manifest"]["native_acceptance"] == "not_accepted"
    assert manifest["native_acceptance"] == "not_accepted"
    assert reviewed["rows"][0]["background_schema"] == producer["background_schema"]
    assert reviewed["rows"][0]["campaign_treatment"] == producer["campaign_treatment"]
    analyzed = analyze_rows(reviewed["rows"], trial, data["initial"], data["final"])
    assert "feature_composition_mismatch" not in analyzed["issues"]
    assert "connection_failure_attribution_regressed" not in analyzed["issues"]
    assert analyzed["native_acceptance"] == "not_accepted"


@pytest.mark.parametrize("missing", ["background_work", "background_job"])
def test_verified_capture_cannot_hide_enabled_composed_feature_fields(missing, tmp_path):
    data = _feature_capture_data(tmp_path)
    reviewed, manifest, trial = _capture_roundtrip(tmp_path, data, "omitted-background")
    assert manifest["capture_complete"] is True
    assert reviewed["manifest"]["native_acceptance"] == "not_accepted"
    altered = deepcopy(reviewed["rows"])
    for row in altered:
        row.pop(missing, None)
    analyzed = analyze_rows(altered, trial, data["initial"], data["final"])
    assert "feature_composition_mismatch" in analyzed["issues"]
    assert not analyzed["integrity_checks_passed"]
    assert analyzed["native_acceptance"] == "not_accepted"

    directory = tmp_path / "capture-omitted-background"
    _rewrite_capture_rows(directory, lambda rows: [row.pop(missing, None) for row in rows])
    message = ("Capture background_work record flag differs from treatment" if missing == "background_work"
               else "Enabled background_work record evidence is missing")
    with pytest.raises(ValueError, match=message):
        verify(directory)


def test_verified_capture_cannot_relabel_enabled_feature_as_disabled(tmp_path):
    data = _feature_capture_data(tmp_path)
    reviewed, _, trial = _capture_roundtrip(tmp_path, data, "conflicting-background")
    altered = deepcopy(reviewed["rows"])
    for row in altered:
        row["background_work"] = False
    analyzed = analyze_rows(altered, trial, data["initial"], data["final"])
    assert "feature_composition_mismatch" in analyzed["issues"]
    assert not analyzed["integrity_checks_passed"]

    directory = tmp_path / "capture-conflicting-background"
    _rewrite_capture_rows(directory, lambda rows: [row.__setitem__("background_work", False) for row in rows])
    with pytest.raises(ValueError, match="Capture background_work record flag differs from treatment"):
        verify(directory)


def test_analyzer_checks_outpost_emitter_flag_and_required_evidence():
    rows, trial, initial, final = evidence()
    trial["configuration"].update(furnace_output_buffers=True,
                                  furnace_input_belts=True,
                                  mining_outposts=True)
    for checkpoint in (initial, final):
        checkpoint.update(output_buffers_schema=1, output_commitments={},
                          input_routes_schema=1, input_commitments={},
                          outposts_schema=1, outpost_commitments={})
    for row in rows:
        row["acceptance_configuration"].update(furnace_output_buffers=True,
                                               furnace_input_belts=True,
                                               mining_outposts=True)
        row.update(furnace_output_buffers=True, buffer_evidence={},
                   furnace_input_belts=True, input_route_evidence={},
                   input_validation_failure={}, mining_outposts=True,
                   mining_outpost_evidence={})
    valid = analyze_rows(rows, trial, initial, final)
    assert "feature_composition_mismatch" not in valid["issues"]
    assert valid["integrity_checks_passed"], valid["issues"]

    for row in rows:
        row.pop("mining_outpost_evidence")
    invalid = analyze_rows(rows, trial, initial, final)
    assert "feature_composition_mismatch" in invalid["issues"]
    assert not invalid["integrity_checks_passed"]
    assert invalid["native_acceptance"] == "not_accepted"


@pytest.mark.parametrize("schema", ["missing", 1, 3, True, 2.0, "2"])
def test_analyzer_requires_exact_current_integer_record_schema(schema):
    rows, trial, initial, final = evidence()
    for row in rows:
        if schema == "missing":
            row.pop("schema_version", None)
        else:
            row["schema_version"] = schema
    result = analyze_rows(rows, trial, initial, final)
    assert "gameplay_schema_mismatch" in result["issues"]
    assert not result["integrity_checks_passed"]
    assert result["native_acceptance"] == "not_accepted"


@pytest.mark.parametrize("change", ["erased", "rewritten"])
def test_verified_capture_must_retain_initial_connection_attribution(change, tmp_path):
    data = _inputs(tmp_path)
    receipt = _install_attribution(data)
    if change == "erased":
        data["final"]["connection_failure_attribution"] = {}
    else:
        data["final"]["connection_failure_attribution"][PREFIX]["evidence"] = "rewritten allocation"
    reviewed, manifest, trial = _capture_roundtrip(tmp_path, data, "attribution-" + change)
    assert manifest["capture_complete"] is True
    result = analyze_rows(reviewed["rows"], trial, data["initial"], data["final"])
    assert "connection_failure_attribution_regressed" in result["issues"]
    assert not result["integrity_checks_passed"]
    assert result["native_acceptance"] == "not_accepted"


def test_verified_capture_retains_audited_attribution_and_allows_new_receipts(tmp_path):
    data = _inputs(tmp_path)
    original = _install_attribution(data, add_final_receipt=True)
    reviewed, manifest, trial = _capture_roundtrip(tmp_path, data, "attribution-prefix-positive")
    assert manifest["capture_complete"] is True
    assert reviewed["manifest"]["native_acceptance"] == "not_accepted"
    assert data["final"]["connection_failure_attribution"][PREFIX] == original[PREFIX]
    result = analyze_rows(reviewed["rows"], trial, data["initial"], data["final"])
    assert "connection_failure_attribution_regressed" not in result["issues"]
    assert result["integrity_checks_passed"], result["issues"]
    assert result["native_acceptance"] == "not_accepted"
    assert data["initial"]["connection_failure_attribution"][PREFIX] == original[PREFIX]


def test_analyzer_rejects_first_before_state_older_than_checkpoint():
    rows, trial, initial, final = evidence()
    prior_tick = initial["last_tick"] - 1
    state = rows[0]["state"]
    state["tick"] = prior_tick
    state["factory"]["tick"] = prior_tick
    state["factory"]["solid_routes"]["tick"] = prior_tick
    for route in state["factory"]["solid_routes"]["routes"].values():
        route["flow"]["last_tick"] = prior_tick
        route["flow"]["last_positive_tick"] = prior_tick
    retained = set(state["factory"]["receipts"])
    for record in rows:
        for label in ("state", "after_state"):
            receipts = record[label]["factory"]["receipts"]
            for receipt_id in retained:
                receipts[receipt_id]["tick"] = prior_tick
    result = analyze_rows(rows, trial, initial, final)
    assert "initial_checkpoint_before_state" in result["issues"]
    assert not result["integrity_checks_passed"]
    assert result["native_acceptance"] == "not_accepted"


@pytest.mark.parametrize(
    ("lead", "coverage", "valid"),
    [(False, True, False), (False, False, True), (True, True, True), (True, False, True)],
)
def test_campaign_treatment_dependency_and_valid_controls(lead, coverage, valid):
    rows, trial, initial, final = evidence()
    treatment = {
        "schema": 1,
        "lead_time_supply": lead,
        "coverage_margin_lookahead": coverage,
        "profile_observations": False,
        "consolidated_observations": False,
    }
    trial["campaign_treatment"] = deepcopy(treatment)
    for row in rows:
        row["campaign_treatment"] = deepcopy(treatment)
    if valid:
        validate_trial(trial)
        result = analyze_rows(rows, trial, initial, final)
        assert "configuration_mismatch" not in result["issues"]
    else:
        with pytest.raises(ValueError, match="campaign treatment"):
            validate_trial(trial)

# These nine vectors are the legal optional-extension combinations enforced by
# main.py and SupervisorConfig.validate(): input requires buffers; outposts
# require input; successors require background+input and exclude outposts.
# Every case uses the real CLI wrapper order, with production treatment (solid
# plus coal) and campaign diagnostics enabled as supported outer compositions.
_CLI_EXTENSION_MRO_CASES = [
    pytest.param("core", {"background_work": False, "furnace_output_buffers": False,
                          "furnace_input_belts": False, "mining_outposts": False,
                          "ore_side_successors": False}, id="core"),
    pytest.param("background", {"background_work": True, "furnace_output_buffers": False,
                                "furnace_input_belts": False, "mining_outposts": False,
                                "ore_side_successors": False}, id="background"),
    pytest.param("buffers", {"background_work": False, "furnace_output_buffers": True,
                             "furnace_input_belts": False, "mining_outposts": False,
                             "ore_side_successors": False}, id="buffers"),
    pytest.param("background-buffers", {"background_work": True, "furnace_output_buffers": True,
                                        "furnace_input_belts": False, "mining_outposts": False,
                                        "ore_side_successors": False}, id="background-buffers"),
    pytest.param("input", {"background_work": False, "furnace_output_buffers": True,
                           "furnace_input_belts": True, "mining_outposts": False,
                           "ore_side_successors": False}, id="input"),
    pytest.param("background-input", {"background_work": True, "furnace_output_buffers": True,
                                      "furnace_input_belts": True, "mining_outposts": False,
                                      "ore_side_successors": False}, id="background-input"),
    pytest.param("outpost", {"background_work": False, "furnace_output_buffers": True,
                             "furnace_input_belts": True, "mining_outposts": True,
                             "ore_side_successors": False}, id="outpost"),
    pytest.param("background-outpost", {"background_work": True, "furnace_output_buffers": True,
                                        "furnace_input_belts": True, "mining_outposts": True,
                                        "ore_side_successors": False}, id="background-outpost"),
    pytest.param("successor", {"background_work": True, "furnace_output_buffers": True,
                               "furnace_input_belts": True, "mining_outposts": False,
                               "ore_side_successors": True}, id="successor"),
]

_PRODUCER_FEATURE_FIELDS = (
    "factory_scheduling", "mining_outposts", "mining_outpost_evidence",
    "background_work", "background_schema", "background_job", "background_attempt",
    "furnace_output_buffers", "buffer_evidence",
    "furnace_input_belts", "input_route_evidence", "input_validation_failure",
    "ore_side_successors", "successor_evidence", "successor_projects",
    "solid_routes", "solid_route_evidence", "solid_route_fault", "solid_science_policy",
    "solid_funding_schema", "solid_funding", "solid_investment_evidence",
    "coal_supply", "coal_supply_evidence", "coal_supply_fault", "coal_kit_policy",
    "coal_kit_evidence", "coal_economic_admission", "coal_admission_evidence",
    "acceptance_configuration", "campaign_treatment",
)


def _actual_cli_matrix_record(tmp_path, trial, features, *, fail_input_observation=False,
                              coal_observation=None, observation_state=None):
    """Record one composed observation using typed empty optional telemetry."""
    from jev_factorio.buffer_controller import buffered_loop_type
    from jev_factorio.controller import HierarchicalLoop
    from jev_factorio.input_controller import input_loop_type
    from jev_factorio.outpost_controller import outpost_loop_type
    from jev_factorio.successor_controller import successor_loop_type

    backend = SolidBackend()
    if observation_state is not None:
        # Reuse the scaffold's session and economic/solid source evidence so
        # the real observers see one coherent synthetic snapshot.
        backend.state = GameSnapshot(**deepcopy(observation_state))
    backend.craft_jobs_supported = True
    backend.coal_supply_supported = True
    backend.output_buffers_supported = True
    backend.input_routes_supported = True
    backend.mining_outposts_supported = True
    backend.successors_supported = True
    backend.checkpoint = tmp_path / "matrix-producer-checkpoint.json"
    backend.enable_factory = lambda: native_catalog()

    factory = backend.state.factory
    optional_observations = {
        "furnace_output_buffers": "output_buffers",
        "furnace_input_belts": "input_routes",
        "mining_outposts": "mining_outposts",
        "ore_side_successors": "successors",
    }
    for flag, field in optional_observations.items():
        if not features[flag]:
            continue
        existing = factory.get(field)
        if existing is not None and existing.get("sources"):
            raise AssertionError(f"Refusing to replace existing {field} ownership")
        factory[field] = {
            "protocol": 1,
            "session_id": backend.state.session_id,
            "tick": backend.state.tick,
            "sources": {},
        }
    if features["ore_side_successors"]:
        existing_sites = factory.get("production_sites")
        if existing_sites is not None and existing_sites.get("sources"):
            raise AssertionError("Refusing to replace existing production-site evidence")
        factory["production_sites"] = {
            "protocol": 1,
            "session_id": backend.state.session_id,
            "tick": backend.state.tick,
            "sources": {},
        }

    # This is the same outer-to-inner order selected in main.py. The successor
    # vector is the supported resumed-controller composition; this bounded
    # probe installs its already validated memory directly and does not claim to
    # exercise CLI startup, checkpoint migration, or native resume.
    loop_type = BackgroundWorkLoop if features["background_work"] else HierarchicalLoop
    if features["furnace_output_buffers"]:
        loop_type = buffered_loop_type(loop_type)
    if features["furnace_input_belts"]:
        loop_type = input_loop_type(loop_type)
    if features["ore_side_successors"]:
        loop_type = successor_loop_type(loop_type)
    if features["mining_outposts"]:
        loop_type = outpost_loop_type(loop_type)
    loop_type = solid_loop_type(loop_type)
    loop_type = coal_loop_type(loop_type)
    loop_type = campaign_loop_type(loop_type)

    configuration = trial["configuration"]
    loop = loop_type(
        backend,
        target="rocket_launch",
        policy="deterministic",
        factory_scheduling="ready-work",
        tick_seconds=0,
        checkpoint=str(backend.checkpoint),
        solid_intents=deepcopy(trial["solid_intents"]),
        solid_science_policy=configuration["solid_science_policy"],
        coal_targets=deepcopy(trial["coal_targets"]),
        coal_kit_policy=configuration["coal_kit_policy"],
        coal_economic_admission=configuration["coal_economic_admission"],
        lead_time_supply=True,
        coverage_margin_lookahead=True,
    )
    loop.memory = loop.memory_type(
        backend.state.session_id,
        "rocket_launch",
        active_goal="rocket_launch",
        completed_goals={"stockpile_fuel": 0},
        last_tick=backend.state.tick,
        solid_intents=deepcopy(trial["solid_intents"]),
        solid_science_policy=configuration["solid_science_policy"],
        solid_epoch={"actor_index": 1, "surface_index": 1, "force_index": 1},
        coal_targets=deepcopy(trial["coal_targets"]),
        coal_kit_policy=configuration["coal_kit_policy"],
        coal_economic_admission=configuration["coal_economic_admission"],
        coal_supply_schema=2,
        coal_epoch={"actor_index": 1, "surface_index": 1, "force_index": 1},
    )
    if fail_input_observation:
        assert features["furnace_input_belts"]
        assert isinstance(coal_observation, dict)
        # The current composed observer validates the configured coal protocol
        # before it can record the intentionally malformed input-route sample.
        # Start from the existing schema-v2 no-source fixture and bind it to
        # this backend's current snapshot/solid epoch so the input diagnostic,
        # not an unrelated coal setup fault, is the exercised path.
        coal_supply = deepcopy(coal_observation)
        solid_epoch = backend.state.factory["solid_routes"]
        coal_supply.update(
            session_id=backend.state.session_id,
            tick=backend.state.tick,
            actor_index=solid_epoch["actor_index"],
            surface_index=solid_epoch["surface_index"],
            force_index=solid_epoch["force_index"],
            targets=deepcopy(trial["coal_targets"]),
        )
        admission = coal_supply["admission"]
        admission.update(
            session_id=backend.state.session_id,
            tick=backend.state.tick,
            actor_index=solid_epoch["actor_index"],
            surface_index=solid_epoch["surface_index"],
            force_index=solid_epoch["force_index"],
        )
        backend.state.factory["coal_supply"] = coal_supply
        from jev_factorio.coal_supply import sources as coal_sources
        assert coal_sources(backend.state) == {}
        assert loop._coal_protocol_matches_treatment(backend.state)
        # Keep the pre-observation checkpoint valid. The uncertain record must
        # describe the transition from a supported protocol-1 snapshot to the
        # malformed protocol-0 snapshot, not two identical malformed states.
        before = deepcopy(backend.state)
        backend.state.factory["input_routes"] = {"protocol": 0}
    else:
        before = deepcopy(backend.state)
    observed = loop._observe()
    return loop._record(before, "observe", "synthetic MRO field projection", observed)


def _matrix_capture_data(tmp_path, features, *, fail_input_observation=False):
    data = _inputs(tmp_path)
    coal_observation = (
        data["rows"][0]["state"]["factory"]["coal_supply"]
        if fail_input_observation else None
    )
    producer = _actual_cli_matrix_record(
        tmp_path, data["trial"], features, fail_input_observation=fail_input_observation,
        coal_observation=coal_observation, observation_state=data["rows"][0]["state"])
    if fail_input_observation:
        data["final"]["status"] = producer["status"]
        for row in data["rows"]:
            row["status"] = producer["status"]
            row["reason"] = producer["reason"]
    data["producer"] = producer
    data["trial"]["configuration"] = deepcopy(producer["acceptance_configuration"])
    data["trial"]["campaign_treatment"] = deepcopy(producer["campaign_treatment"])

    checkpoint_extensions = {
        "background_work": ("background_schema", ("background_job", "background_attempt", "background_step")),
        "furnace_output_buffers": ("output_buffers_schema", ("output_commitments",)),
        "furnace_input_belts": ("input_routes_schema", ("input_commitments",)),
        "mining_outposts": ("outposts_schema", ("outpost_commitments",)),
        "ore_side_successors": ("successor_schema", ("successor_projects", "successor_receipts")),
    }
    for checkpoint in (data["initial"], data["final"]):
        for flag, (schema_field, fields) in checkpoint_extensions.items():
            if features[flag]:
                checkpoint[schema_field] = producer.get(schema_field, 2 if flag == "background_work" else 1)
                for field in fields:
                    if flag == "background_work":
                        # This fixture's current producer is schema 2 with no
                        # active task, whose exact emitted checkpoint step is null.
                        checkpoint[field] = (None if field == "background_step"
                                             else deepcopy(producer[field]))
                    else:
                        checkpoint[field] = {}
            else:
                checkpoint.pop(schema_field, None)
                for field in fields:
                    checkpoint.pop(field, None)

    # Only current producer feature fields are projected. The scaffold's state,
    # receipts, identity, campaign window, and checkpoint history remain the
    # bounded synthetic inputs prepared by the existing complete-capture test.
    observed_factory = producer["after_state"]["factory"]
    emitted_envelopes = {
        "furnace_output_buffers": ("output_buffers", "buffer_evidence"),
        "furnace_input_belts": ("input_routes", "input_route_evidence"),
        "mining_outposts": ("mining_outposts", "mining_outpost_evidence"),
        "ore_side_successors": ("successors", "successor_evidence"),
    }
    for row in data["rows"]:
        for field in _PRODUCER_FEATURE_FIELDS:
            row.pop(field, None)
            if field in producer:
                row[field] = deepcopy(producer[field])
        for flag, (native_field, record_field) in emitted_envelopes.items():
            if not features[flag]:
                continue
            # These matrix cases carry no paid route rows. Rebind the actual
            # empty protocol envelope to each scaffold boundary's tick/session
            # so the complete-record validator sees a typed observation, not
            # a top-level receipt transplanted onto an unrelated factory state.
            for label in ("state", "after_state"):
                state = row[label]
                if fail_input_observation and flag == "furnace_input_belts" and label == "state":
                    envelope = {
                        "protocol": 1,
                        "session_id": state["session_id"],
                        "tick": state["tick"],
                        "sources": {},
                    }
                else:
                    envelope = deepcopy(observed_factory[native_field])
                if envelope.get("sources"):
                    raise AssertionError("The empty composition matrix cannot rebind paid route evidence")
                if not (fail_input_observation and flag == "furnace_input_belts"
                        and label == "after_state"):
                    envelope["session_id"] = state["session_id"]
                    envelope["tick"] = state["tick"]
                state["factory"][native_field] = envelope
            row[record_field] = deepcopy(row["after_state"]["factory"][native_field])
        if features["ore_side_successors"]:
            for label in ("state", "after_state"):
                state = row[label]
                sites = deepcopy(observed_factory["production_sites"])
                sites["session_id"] = state["session_id"]
                sites["tick"] = state["tick"]
                state["factory"]["production_sites"] = sites
    return data


def _assert_emitted_feature_shape(producer, features):
    assert producer["factory_scheduling"] == "ready-work"
    assert producer["acceptance_configuration"]["factory_scheduling"] == "ready-work"
    assert producer["mining_outposts"] is features["mining_outposts"]
    if features["mining_outposts"]:
        assert isinstance(producer["mining_outpost_evidence"], dict)
    else:
        assert "mining_outpost_evidence" not in producer
    optional = {
        "background_work": ("background_schema", "background_job", "background_attempt"),
        "furnace_output_buffers": ("buffer_evidence",),
        "furnace_input_belts": ("input_route_evidence", "input_validation_failure"),
        "ore_side_successors": ("successor_evidence", "successor_projects"),
    }
    for flag, evidence_fields in optional.items():
        if features[flag]:
            assert producer[flag] is True
            assert all(field in producer for field in evidence_fields)
            if flag == "background_work":
                assert type(producer["background_schema"]) is int
                assert producer["background_schema"] in {2, 3}
                for field in ("background_job", "background_attempt"):
                    assert producer[field] is None or isinstance(producer[field], dict)
            else:
                assert all(isinstance(producer[field], dict) for field in evidence_fields)
        else:
            assert flag not in producer
            assert all(field not in producer for field in evidence_fields)
    assert producer["solid_routes"] is True
    assert producer["solid_funding_schema"] == 1
    assert "solid_funding" in producer and producer["solid_funding"] is None
    assert producer["coal_supply"] is True
    assert producer["coal_economic_admission"] is True
    assert isinstance(producer["coal_supply_evidence"], dict)
    assert isinstance(producer["coal_kit_evidence"], dict)
    assert isinstance(producer["coal_admission_evidence"], dict)


_MATRIX_ADVERSE_FIELDS = {
    "core": (
        "factory_scheduling", "mining_outposts", "solid_routes", "solid_route_evidence",
        "solid_science_policy", "solid_funding_schema", "solid_investment_evidence",
        "coal_supply", "coal_supply_evidence", "coal_kit_policy", "coal_kit_evidence",
        "coal_economic_admission", "coal_admission_evidence",
    ),
    "background": ("background_work", "background_schema", "background_job", "background_attempt"),
    "buffers": ("furnace_output_buffers", "buffer_evidence"),
    "input": ("furnace_input_belts", "input_route_evidence", "input_validation_failure"),
    "outpost": ("mining_outposts", "mining_outpost_evidence"),
    "successor": ("ore_side_successors", "successor_evidence", "successor_projects"),
}


def test_actual_input_route_failure_diagnostic_survives_capture_and_verify(tmp_path):
    features = {
        "background_work": False, "furnace_output_buffers": True,
        "furnace_input_belts": True, "mining_outposts": False,
        "ore_side_successors": False,
    }
    data = _matrix_capture_data(tmp_path, features, fail_input_observation=True)
    producer = data["producer"]
    assert producer["input_validation_failure"] == {
        "stage": "route_schema", "exception_class": "ValueError"}
    assert producer["status"] == "uncertain"
    assert producer["input_route_evidence"] == {"protocol": 0}
    assert producer["after_state"]["factory"]["input_routes"] == {"protocol": 0}
    assert producer["state"]["factory"]["input_routes"]["protocol"] == 1
    assert data["initial"].get("input_commitments", {}) == {}
    assert data["final"].get("input_commitments", {}) == {}

    reviewed, manifest, trial = _capture_roundtrip(tmp_path, data, "input-diagnostic")
    assert manifest["capture_complete"] is True
    assert manifest["native_acceptance"] == "not_accepted"
    assert reviewed["rows"][0]["input_validation_failure"] == producer["input_validation_failure"]
    analyzed = analyze_rows(reviewed["rows"], trial, data["initial"], data["final"])
    assert "feature_composition_mismatch" not in analyzed["issues"]
    assert analyzed["integrity_checks_passed"] is False
    assert "controller_or_route_failure" in analyzed["issues"]
    assert "invalid_final_composed_observation" in analyzed["issues"]
    assert analyzed["native_acceptance"] == "not_accepted"


@pytest.mark.parametrize("tamper", [
    "invalid_before_observation", "status_not_uncertain", "extra_protocol_fields",
    "forged_stage", "missing_diagnostic", "ordinary_unsupported_protocol",
    "retained_paid_owner",
])
def test_input_failure_diagnostic_cannot_bypass_capture_ownership(tamper, tmp_path):
    features = {
        "background_work": False, "furnace_output_buffers": True,
        "furnace_input_belts": True, "mining_outposts": False,
        "ore_side_successors": False,
    }
    failure_case = tamper != "ordinary_unsupported_protocol"
    data = _matrix_capture_data(tmp_path, features, fail_input_observation=failure_case)
    if tamper == "invalid_before_observation":
        for row in data["rows"]:
            row["state"]["factory"]["input_routes"] = {"protocol": 0}
    elif tamper == "status_not_uncertain":
        data["final"]["status"] = "running"
        for row in data["rows"]:
            row["status"] = "running"
    elif tamper == "extra_protocol_fields":
        for row in data["rows"]:
            invalid = {"protocol": 0, "sources": {}}
            row["after_state"]["factory"]["input_routes"] = deepcopy(invalid)
            row["input_route_evidence"] = deepcopy(invalid)
    elif tamper == "forged_stage":
        for row in data["rows"]:
            row["input_validation_failure"] = {
                "stage": "live_route", "exception_class": "ValueError",
                "source": INPUT_SOURCE,
            }
    elif tamper == "missing_diagnostic":
        for row in data["rows"]:
            row["input_validation_failure"] = {}
    elif tamper == "ordinary_unsupported_protocol":
        for row in data["rows"]:
            invalid = {"protocol": 0}
            row["after_state"]["factory"]["input_routes"] = deepcopy(invalid)
            row["input_route_evidence"] = deepcopy(invalid)
    elif tamper == "retained_paid_owner":
        paid_state = full_input_route(input_route_fixture())
        route = paid_state.factory["input_routes"]["sources"][INPUT_SOURCE]
        owner = {
            "layout": route["layout"],
            "source_unit": route["source_unit"],
            "parts": deepcopy(route["parts"]),
        }
        data["initial"]["input_commitments"] = {INPUT_SOURCE: deepcopy(owner)}
        data["final"]["input_commitments"] = {INPUT_SOURCE: deepcopy(owner)}
    with pytest.raises(ValueError):
        _capture_roundtrip(tmp_path, data, f"diagnostic-{tamper}")


@pytest.mark.parametrize(("label", "features"), _CLI_EXTENSION_MRO_CASES)
def test_actual_cli_extension_mro_fields_project_through_capture_verify(label, features, tmp_path):
    case_dir = tmp_path / label
    case_dir.mkdir()
    data = _matrix_capture_data(case_dir, features)
    producer = data["producer"]
    _assert_emitted_feature_shape(producer, features)
    assert producer["schema_version"] == 2
    assert producer["campaign_treatment"]["lead_time_supply"] is True
    assert producer["campaign_treatment"]["coverage_margin_lookahead"] is True
    assert producer["acceptance_configuration"] == data["trial"]["configuration"]

    reviewed, manifest, trial = _capture_roundtrip(case_dir, data, f"matrix-{label}")
    assert manifest["capture_complete"] is True
    assert manifest["native_acceptance"] == "not_accepted"
    assert reviewed["manifest"]["native_acceptance"] == "not_accepted"
    for field in _PRODUCER_FEATURE_FIELDS:
        if field in producer:
            assert reviewed["rows"][0][field] == producer[field]
        else:
            assert field not in reviewed["rows"][0]

    analyzed = analyze_rows(reviewed["rows"], trial, data["initial"], data["final"])
    assert "feature_composition_mismatch" not in analyzed["issues"], (label, analyzed["issues"])
    assert analyzed["native_acceptance"] == "not_accepted"

    # These adversarial controls start from the verified in-memory projected
    # rows. They exercise the analyzer's producer binding and do not claim that
    # a modified on-disk capture or native observation was accepted.
    for missing in _MATRIX_ADVERSE_FIELDS.get(label, ()):
        adverse = deepcopy(reviewed["rows"])
        for row in adverse:
            row.pop(missing, None)
        rejected = analyze_rows(adverse, trial, data["initial"], data["final"])
        assert "feature_composition_mismatch" in rejected["issues"], (label, missing, rejected["issues"])
        assert not rejected["integrity_checks_passed"], (label, missing)
        assert rejected["native_acceptance"] == "not_accepted"


def test_actual_analyzer_rejects_malformed_input_diagnostics(tmp_path):
    features = {
        "background_work": False, "furnace_output_buffers": True,
        "furnace_input_belts": True, "mining_outposts": False,
        "ore_side_successors": False,
    }
    data = _matrix_capture_data(tmp_path, features)
    reviewed, _, trial = _capture_roundtrip(tmp_path, data, "malformed-input-diagnostic")
    for diagnostic in (
        {"unreviewed_diagnostic_field": "value"},
        {"stage": "unrecognized", "exception_class": "RuntimeError"},
        {"stage": "commitment", "exception_class": "ValueError",
         "source": "unreviewed-source"},
        {"stage": "commitment", "exception_class": "ValueError",
         "authorization": "synthetic-secret"},
    ):
        malformed = deepcopy(reviewed["rows"])
        malformed[0]["input_validation_failure"] = diagnostic
        rejected = analyze_rows(malformed, trial, data["initial"], data["final"])
        assert "feature_composition_mismatch" in rejected["issues"], diagnostic
        assert not rejected["integrity_checks_passed"], diagnostic
        assert rejected["native_acceptance"] == "not_accepted"


@pytest.mark.parametrize(
    ("flag", "malformed"),
    [
        ("background_work", "false"),
        ("furnace_output_buffers", 1),
        ("furnace_input_belts", "false"),
        ("ore_side_successors", 1),
    ],
)
def test_actual_analyzer_requires_boolean_shape_for_present_disabled_flags(
        flag, malformed, tmp_path):
    features = {
        "background_work": False, "furnace_output_buffers": False,
        "furnace_input_belts": False, "mining_outposts": False,
        "ore_side_successors": False,
    }
    data = _matrix_capture_data(tmp_path, features)
    assert flag not in data["producer"]
    assert all(flag not in row for row in data["rows"])

    reviewed, manifest, trial = _capture_roundtrip(
        tmp_path, data, f"disabled-flag-{flag}")
    assert manifest["capture_complete"] is True
    assert reviewed["manifest"]["native_acceptance"] == "not_accepted"

    baseline = analyze_rows(reviewed["rows"], trial, data["initial"], data["final"])
    assert "feature_composition_mismatch" not in baseline["issues"]
    assert baseline["native_acceptance"] == "not_accepted"

    explicit_false = deepcopy(reviewed["rows"])
    for row in explicit_false:
        row[flag] = False
    false_control = analyze_rows(explicit_false, trial, data["initial"], data["final"])
    assert "feature_composition_mismatch" not in false_control["issues"]
    assert false_control["native_acceptance"] == "not_accepted"

    adverse = deepcopy(reviewed["rows"])
    for row in adverse:
        row[flag] = malformed
    rejected = analyze_rows(adverse, trial, data["initial"], data["final"])
    assert "feature_composition_mismatch" in rejected["issues"], (flag, malformed)
    assert not rejected["integrity_checks_passed"], (flag, malformed)
    assert rejected["native_acceptance"] == "not_accepted"

    enabled = deepcopy(reviewed["rows"])
    for row in enabled:
        row[flag] = True
    enabled_mismatch = analyze_rows(enabled, trial, data["initial"], data["final"])
    assert "feature_composition_mismatch" in enabled_mismatch["issues"], flag
    assert enabled_mismatch["native_acceptance"] == "not_accepted"
