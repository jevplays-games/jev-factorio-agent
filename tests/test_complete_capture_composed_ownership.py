"""End-to-end capture checks over actual composed controller records.

These modeled snapshots use production mixins, typed checkpoints, ``_observe``
and ``_record``. They are offline contract tests, not native gameplay proof.
"""
from copy import deepcopy
import hashlib
import gzip
import json

import pytest

from jev_factorio.acceptance_io import canonical
from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.coal_controller import coal_loop_type
from jev_factorio.complete_capture import capture, verify
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.input_controller import input_loop_type
from jev_factorio.integration_evidence import TRIAL_SCHEMA_V2
from jev_factorio.outpost_controller import outpost_loop_type
from jev_factorio.solid_controller import solid_loop_type
from jev_factorio.state import GameSnapshot
from jev_factorio.successor_controller import successor_loop_type
from jev_factorio.treatment import SCHEMA, digest
from input_routes_fixtures import commission as commission_input, fixture as input_fixture, full as full_input
from test_mining_outposts import (commission as commission_outpost,
                                  full as full_outpost, state_fixture as outpost_fixture)
from test_successors import flowing_state as successor_fixture, qualify as qualify_successor
from test_complete_capture import test_complete_capture_v2_admission_roundtrip as seed_complete_trial
from test_solid_route_integration import Backend as SolidBackend


def _composed_capture_case(tmp_path, family="input", paid_routes=True):
    """Make a full v2 boundary around one unchanged MRO-produced row."""
    seed_complete_trial(tmp_path)
    trial_path = tmp_path / "trial.json"
    initial_path = tmp_path / "initial.json"
    final_path = tmp_path / "final.json"
    preflight_path = tmp_path / "preflight.json"
    gameplay_path = tmp_path / "gameplay.jsonl"
    save_path = tmp_path / "save.zip"

    trial = json.loads(trial_path.read_bytes())
    trial["schema"] = TRIAL_SCHEMA_V2
    trial["configuration"].pop("coal_economic_admission", None)
    output_enabled = family != "disabled"
    input_enabled = family != "disabled"
    trial["configuration"].update(
        furnace_output_buffers=output_enabled,
        furnace_input_belts=input_enabled,
        mining_outposts=family == "outpost",
        ore_side_successors=family == "successor",
        background_work=family == "successor",
        solid_science_policy=False,
        coal_kit_policy=False,
    )
    trial["treatment_sha256"] = digest({
        "schema": SCHEMA,
        "solid_intents": trial["solid_intents"],
        "coal_targets": trial["coal_targets"],
        "solid_science_policy": False,
        "coal_kit_policy": False,
    })

    if family == "disabled":
        source = None
    elif family == "successor" and paid_routes:
        source, _ = successor_fixture()
    else:
        source = input_fixture()
        if family == "input" and paid_routes:
            full_input(source)
            commission_input(source)
        else:
            output = source.factory["output_buffers"]["sources"]["recipe:iron-plate"]
            for part in output["parts"].values():
                source.factory["entities"].pop(part["role"], None)
            output.update(parts={}, state="proposed", topology=False, flow={})
        if family == "successor":
            source.factory["production_sites"] = {
                "protocol": 1, "session_id": source.session_id,
                "tick": source.tick, "sources": {}}
            source.factory["successors"] = {
                "protocol": 1, "session_id": source.session_id,
                "tick": source.tick, "sources": {}}

    # Start with the exact schema-valid solid campaign scaffold. For already-
    # paid routes, the typed memory below models a prior durable checkpoint
    # seeded from producer-contract-valid receipt fixtures; this test does not
    # simulate the payment dispatch itself. The real composed observers then
    # validate/persist the current boundary, and _record() emits the full row
    # passed to public capture()/verify().
    seed_rows = [json.loads(line) for line in gameplay_path.read_bytes().splitlines()]
    snapshot = GameSnapshot(**deepcopy(seed_rows[0]["state"]))
    native_solid = snapshot.factory["solid_routes"]
    for route, native in native_solid["routes"].items():
        for paid in native["parts"].values():
            snapshot.factory["entities"].pop(paid["role"], None)
        native.update(state="proposed", topology=False, parts={}, flow={}, pending={}, reason="proposal")
    native_solid["diagnostics"] = [
        {"intent_index": index, "state": "proposed", "reason": "ready_layout"}
        for index, _ in enumerate(trial["solid_intents"], 1)
    ]
    snapshot.world_kind = "mock"

    # The route fixtures are generated from the output/input producer
    # contracts; bind them to this modeled controller session and tick.
    if source is not None:
        snapshot.factory["entities"].update(deepcopy(source.factory["entities"]))
    if family != "disabled":
        for name in ("output_buffers", "input_routes"):
            envelope = deepcopy(source.factory[name])
            envelope["session_id"] = snapshot.session_id
            envelope["tick"] = snapshot.tick
            snapshot.factory[name] = envelope
    if family == "successor":
        for name in ("production_sites", "successors"):
            envelope = deepcopy(source.factory[name])
            envelope["session_id"] = snapshot.session_id
            envelope["tick"] = snapshot.tick
            snapshot.factory[name] = envelope
    elif family == "outpost":
        outpost_state, _ = outpost_fixture()
        if paid_routes:
            full_outpost(outpost_state)
        outpost_envelope = deepcopy(outpost_state.factory["mining_outposts"])
        outpost_envelope["session_id"] = snapshot.session_id
        outpost_envelope["tick"] = snapshot.tick
        snapshot.factory["mining_outposts"] = outpost_envelope
        for row in outpost_envelope["sources"].values():
            for paid in row["parts"].values():
                snapshot.factory["entities"][paid["role"]] = deepcopy(
                    outpost_state.factory["entities"][paid["role"]])
    snapshot.factory["acceptance_runtime"] = {
        "schema": 1,
        "session_id": snapshot.session_id,
        "actor_unit": 999998,
        "player_index": native_solid["actor_index"],
        "surface_index": native_solid["surface_index"],
        "force_index": native_solid["force_index"],
        "mods": {"base": "2.0-offline-fixture"},
        "speed": 1,
        "tick_paused": False,
    }
    snapshot.factory["coal_supply"] = {
        "protocol": 1,
        "session_id": snapshot.session_id,
        "tick": snapshot.tick,
        "actor_index": native_solid["actor_index"],
        "surface_index": native_solid["surface_index"],
        "force_index": native_solid["force_index"],
        "targets": deepcopy(trial["coal_targets"]),
        "committed": False,
        "sources": {},
        "reason": "no_supported_bundle",
    }
    snapshot.factory["tick"] = snapshot.tick

    if family == "disabled":
        kind = HierarchicalLoop
    else:
        kind = buffered_loop_type(BackgroundWorkLoop if family == "successor" else HierarchicalLoop)
        kind = input_loop_type(kind)
        if family == "outpost":
            kind = outpost_loop_type(kind)
        elif family == "successor":
            kind = successor_loop_type(kind)
    kind = solid_loop_type(kind)
    kind = coal_loop_type(kind)
    backend = SolidBackend()
    backend.state = snapshot
    backend.output_buffers_supported = True
    backend.input_routes_supported = True
    backend.coal_supply_supported = True
    backend.mining_outposts_supported = family == "outpost"
    backend.successors_supported = family == "successor"
    backend.craft_jobs_supported = family == "successor"
    backend.checkpoint = tmp_path / "controller-checkpoint.json"

    output_rows = snapshot.factory.get("output_buffers", {}).get("sources", {})
    input_rows = snapshot.factory.get("input_routes", {}).get("sources", {})
    output_commitments = {} if family in {"successor", "disabled"} else {
        source: {"source_unit": route["source_unit"], "layout": route["layout"],
                 "parts": deepcopy(route["parts"])}
        for source, route in output_rows.items() if route["parts"]}
    input_commitments = {} if family == "disabled" else {
        source: {
            "layout": route["layout"],
            "source_unit": route["source_unit"],
            "parts": deepcopy(route["parts"]),
        }
        for source, route in input_rows.items() if route["parts"]
    }
    outpost_commitments = {}
    successor_projects = {}
    successor_receipts = {}
    if family == "outpost":
        outpost_commitments = {
            resource: {key: deepcopy(route[key]) for key in
                       ("layout", "surface_index", "force_index", "steps", "parts", "flow")}
            for resource, route in snapshot.factory["mining_outposts"]["sources"].items()
            if route["state"] != "proposed"
        }
    elif family == "successor":
        from jev_factorio.successors import MAX_PROJECT_TICKS

        for source_role, route in snapshot.factory["successors"]["sources"].items():
            successor_projects[source_role] = {
                "anchor": route["anchor"],
                "predecessor_unit": route["predecessor_unit"],
                "source_unit": route["source_unit"],
                "started_tick": route["started_tick"],
                "deadline_tick": route["started_tick"] + MAX_PROJECT_TICKS,
                "status": "active",
            }
            successor_receipts[source_role] = {
                "output_layout": output_rows[source_role]["layout"],
                "input_layout": input_rows[source_role]["layout"],
                "output": deepcopy(output_rows[source_role]["parts"]),
                "input": deepcopy(input_rows[source_role]["parts"]),
                "use": deepcopy(route["use"]),
                "qualification": deepcopy(route["qualification"]),
            }
    if family == "outpost":
        from jev_factorio.mining_outposts import current as outpost_current, sources as outpost_sources

        parsed_outposts = outpost_sources(snapshot)
        assert parsed_outposts == snapshot.factory["mining_outposts"]["sources"]
        assert all(outpost_current(route, snapshot) for route in parsed_outposts.values()), {
            source_role: {"route": route, "entities": snapshot.factory["entities"]}
            for source_role, route in parsed_outposts.items()
        }
    epoch = {key: native_solid[key] for key in ("actor_index", "surface_index", "force_index")}
    memory_fields = {
        "active_goal": "rocket_launch",
        "last_tick": snapshot.tick,
        "solid_intents": deepcopy(trial["solid_intents"]),
        "solid_epoch": deepcopy(epoch),
        "coal_targets": deepcopy(trial["coal_targets"]),
        "coal_epoch": deepcopy(epoch),
    }
    if output_enabled:
        memory_fields["output_commitments"] = output_commitments
    if input_enabled:
        memory_fields["input_commitments"] = input_commitments
    if family == "outpost":
        memory_fields["outpost_commitments"] = outpost_commitments
    elif family == "successor":
        memory_fields["successor_projects"] = successor_projects
        memory_fields["successor_receipts"] = successor_receipts
    memory = kind.memory_type(snapshot.session_id, "rocket_launch", **memory_fields)
    memory.save(backend.checkpoint)
    # Parse the exact serialized checkpoint before attaching the producer.
    kind.memory_type.from_bytes(
        backend.checkpoint.read_bytes(), snapshot.session_id, "rocket_launch")
    loop = kind(
        backend,
        target="rocket_launch",
        policy="deterministic",
        factory_scheduling="ready-work",
        tick_seconds=0,
        checkpoint=str(backend.checkpoint),
        resume_controller=True,
        solid_intents=deepcopy(trial["solid_intents"]),
        solid_science_policy=False,
        coal_targets=deepcopy(trial["coal_targets"]),
        coal_kit_policy=False,
        coal_economic_admission=False,
    )
    before = loop._observe()
    fault_names = ("_buffer_fault", "_input_fault", "_outpost_fault", "_successor_fault",
                   "_solid_fault", "_coal_fault")
    assert not any(getattr(loop, name, False) for name in fault_names), {
        "faults": {name: getattr(loop, name, False) for name in fault_names},
        "reason": loop.memory.reason,
    }
    initial_raw = backend.checkpoint.read_bytes()
    emitted = loop._record(before, "observe", "modeled composed observation", before, True)
    final_raw = backend.checkpoint.read_bytes()
    assert emitted["acceptance_configuration"] == trial["configuration"]
    if source is not None:
        assert emitted["state"]["factory"]["input_routes"] == source.factory["input_routes"] | {
            "session_id": snapshot.session_id, "tick": snapshot.tick}
        assert emitted["state"]["factory"]["output_buffers"] == source.factory["output_buffers"] | {
            "session_id": snapshot.session_id, "tick": snapshot.tick}

    trial["initial_checkpoint_sha256"] = hashlib.sha256(initial_raw).hexdigest()
    save_path.write_bytes(b"modeled-capture-save")
    trial["initial_save_sha256"] = hashlib.sha256(save_path.read_bytes()).hexdigest()
    trial_path.write_bytes(canonical(trial))
    initial_path.write_bytes(initial_raw)
    final_path.write_bytes(final_raw)
    preflight = json.loads(preflight_path.read_bytes())
    preflight["checkpoint_sha256"] = trial["initial_checkpoint_sha256"]
    preflight_path.write_bytes(canonical(preflight))
    gameplay_path.write_bytes(canonical(emitted))
    return {
        "trial_path": trial_path,
        "initial_path": initial_path,
        "final_path": final_path,
        "preflight_path": preflight_path,
        "gameplay_path": gameplay_path,
        "save_path": save_path,
        "output_commitments": output_commitments,
        "input_commitments": input_commitments,
        "outpost_commitments": outpost_commitments,
        "successor_projects": successor_projects,
        "successor_receipts": successor_receipts,
        "family": family,
        "row": emitted,
        "loop": loop,
        "backend": backend,
    }


def _advance_snapshot_tick(snapshot, tick):
    snapshot.tick = tick
    snapshot.factory["tick"] = tick
    for value in snapshot.factory.values():
        if isinstance(value, dict) and "tick" in value:
            value["tick"] = tick


def _capture(case, output):
    return capture(
        gameplay=case["gameplay_path"],
        trial_path=case["trial_path"],
        initial_checkpoint=case["initial_path"],
        final_checkpoint=case["final_path"],
        save=case["save_path"],
        preflight_path=case["preflight_path"],
        output=output,
        environ={},
    )


def _checkpoint_extension(family):
    return {
        "output": ("output_buffers_schema", "output_commitments"),
        "input": ("input_routes_schema", "input_commitments"),
        "outpost": ("outposts_schema", "outpost_commitments"),
        "successor": ("successor_schema", "successor_projects", "successor_receipts"),
    }[family]


def _drop_case_extension(case, family, boundary):
    path_key = "initial_path" if boundary == "initial" else "final_path"
    checkpoint = json.loads(case[path_key].read_bytes())
    for key in _checkpoint_extension(family):
        checkpoint.pop(key, None)
    case[path_key].write_bytes(canonical(checkpoint))
    if boundary == "initial":
        trial = json.loads(case["trial_path"].read_bytes())
        trial["initial_checkpoint_sha256"] = hashlib.sha256(case["initial_path"].read_bytes()).hexdigest()
        case["trial_path"].write_bytes(canonical(trial))
        preflight = json.loads(case["preflight_path"].read_bytes())
        preflight["checkpoint_sha256"] = trial["initial_checkpoint_sha256"]
        case["preflight_path"].write_bytes(canonical(preflight))


def _drop_capture_extension(directory, family, boundary):
    path = directory / ("initial-checkpoint.json" if boundary == "initial" else "final-checkpoint.json")
    checkpoint = json.loads(path.read_bytes())
    for key in _checkpoint_extension(family):
        checkpoint.pop(key, None)
    path.write_bytes(canonical(checkpoint))
    if boundary == "initial":
        trial_path = directory / "trial.json"
        trial = json.loads(trial_path.read_bytes())
        trial["initial_checkpoint_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        trial_path.write_bytes(canonical(trial))
        preflight_path = directory / "preflight.json"
        preflight = json.loads(preflight_path.read_bytes())
        preflight["checkpoint_sha256"] = trial["initial_checkpoint_sha256"]
        preflight_path.write_bytes(canonical(preflight))
        manifest_path = directory / "capture-manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["source_initial_checkpoint_sha256"] = trial["initial_checkpoint_sha256"]
        manifest["source_trial_sha256"] = hashlib.sha256(trial_path.read_bytes()).hexdigest()
        manifest_path.write_bytes(canonical(manifest))
    _reseal_capture(directory)


def _drop_capture_checkpoint_owner(directory, family):
    path = directory / "final-checkpoint.json"
    final = json.loads(path.read_bytes())
    if family == "output":
        final["output_commitments"].pop("recipe:iron-plate")
    elif family == "input":
        final["input_commitments"].pop("recipe:iron-plate")
    elif family == "outpost":
        final["outpost_commitments"].pop("iron-ore")
    elif family == "successor":
        final["successor_receipts"]["growth:iron-plate"]["output"].pop("inserter")
    path.write_bytes(canonical(final))
    _reseal_capture(directory)


def _drop_paid_observation(row, family):
    factory = row["after_state"]["factory"]
    if family == "output":
        native = factory["output_buffers"]["sources"]["recipe:iron-plate"]
        native["parts"].pop("inserter")
        native.update(state="building", topology=False, flow={})
        row["buffer_evidence"] = deepcopy(factory["output_buffers"])
    elif family == "input":
        native = factory["input_routes"]["sources"]["recipe:iron-plate"]
        native["parts"].pop("drill")
        native.update(state="building", topology=False, flow={})
        row["input_route_evidence"] = deepcopy(factory["input_routes"])
    elif family == "outpost":
        native = factory["mining_outposts"]["sources"]["iron-ore"]
        native["parts"].pop("drill")
        native.update(state="building", topology=False, flow={})
        row["mining_outpost_evidence"] = deepcopy(factory["mining_outposts"])
    elif family == "successor":
        factory["successors"]["sources"].pop("growth:iron-plate")
        row["successor_evidence"] = deepcopy(factory["successors"])


def _change_paid_receipt(row, family):
    factory = row["after_state"]["factory"]
    if family == "output":
        native = factory["output_buffers"]["sources"]["recipe:iron-plate"]
        native["parts"]["chest"]["receipt"] = "tampered-output-receipt"
        row["buffer_evidence"] = deepcopy(factory["output_buffers"])
    elif family == "input":
        native = factory["input_routes"]["sources"]["recipe:iron-plate"]
        native["parts"]["drill"]["receipt"] = "tampered-input-receipt"
        row["input_route_evidence"] = deepcopy(factory["input_routes"])
    elif family == "outpost":
        native = factory["mining_outposts"]["sources"]["iron-ore"]
        native["parts"]["drill"]["receipt"] = "tampered-outpost-receipt"
        row["mining_outpost_evidence"] = deepcopy(factory["mining_outposts"])
    elif family == "successor":
        native = factory["output_buffers"]["sources"]["growth:iron-plate"]
        native["parts"]["chest"]["receipt"] = "tampered-successor-output-receipt"
        row["buffer_evidence"] = deepcopy(factory["output_buffers"])


def _change_owner_layout(row, family):
    factory = row["after_state"]["factory"]
    if family == "output":
        native = factory["output_buffers"]["sources"]["recipe:iron-plate"]
        native["layout"] = "tampered-output-layout"
        native["flow"] = {}
        row["buffer_evidence"] = deepcopy(factory["output_buffers"])
    elif family == "input":
        native = factory["input_routes"]["sources"]["recipe:iron-plate"]
        native["layout"] = "tampered-input-layout"
        native["flow"] = {}
        row["input_route_evidence"] = deepcopy(factory["input_routes"])
    elif family == "outpost":
        native = factory["mining_outposts"]["sources"]["iron-ore"]
        native["layout"] = "tampered-outpost-layout"
        native["flow"] = {}
        row["mining_outpost_evidence"] = deepcopy(factory["mining_outposts"])
    elif family == "successor":
        native = factory["output_buffers"]["sources"]["growth:iron-plate"]
        native["layout"] = "tampered-successor-layout"
        native["flow"] = {}
        row["buffer_evidence"] = deepcopy(factory["output_buffers"])


def _change_paid_unit(row, family):
    factory = row["after_state"]["factory"]
    if family == "output":
        native = factory["output_buffers"]["sources"]["recipe:iron-plate"]
        paid = native["parts"]["chest"]
        evidence_field, envelope = "buffer_evidence", "output_buffers"
    elif family == "input":
        native = factory["input_routes"]["sources"]["recipe:iron-plate"]
        paid = native["parts"]["drill"]
        evidence_field, envelope = "input_route_evidence", "input_routes"
    elif family == "outpost":
        native = factory["mining_outposts"]["sources"]["iron-ore"]
        paid = native["parts"]["drill"]
        evidence_field, envelope = "mining_outpost_evidence", "mining_outposts"
    elif family == "successor":
        native = factory["output_buffers"]["sources"]["growth:iron-plate"]
        paid = native["parts"]["chest"]
        evidence_field, envelope = "buffer_evidence", "output_buffers"
    paid["unit_number"] = 99991
    factory["entities"][paid["role"]]["unit_number"] = 99991
    row[evidence_field] = deepcopy(factory[envelope])


def _change_optional_envelope_tick(row, family):
    factory = row["after_state"]["factory"]
    if family == "output":
        envelope, evidence = "output_buffers", "buffer_evidence"
    elif family == "input":
        envelope, evidence = "input_routes", "input_route_evidence"
    elif family == "outpost":
        envelope, evidence = "mining_outposts", "mining_outpost_evidence"
    elif family == "successor":
        envelope, evidence = "successors", "successor_evidence"
    factory[envelope]["tick"] += 1
    row[evidence] = deepcopy(factory[envelope])


def _drop_checkpoint_owner(case, family):
    final = json.loads(case["final_path"].read_bytes())
    if family == "output":
        final["output_commitments"].pop("recipe:iron-plate")
    elif family == "input":
        final["input_commitments"].pop("recipe:iron-plate")
    elif family == "outpost":
        final["outpost_commitments"].pop("iron-ore")
    elif family == "successor":
        final["successor_receipts"]["growth:iron-plate"]["output"].pop("inserter")
    case["final_path"].write_bytes(canonical(final))


def _reseal_capture(directory):
    names = ("capture-manifest.json", "trial.json", "preflight.json", "initial-checkpoint.json",
             "final-checkpoint.json", "gameplay.jsonl.gz")
    sums = "".join(hashlib.sha256((directory / name).read_bytes()).hexdigest() + "  " + name + "\n"
                    for name in sorted(names))
    (directory / "SHA256SUMS").write_bytes(sums.encode())


def _rewrite_captured_rows(directory, mutate):
    path = directory / "gameplay.jsonl.gz"
    rows = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
    mutate(rows[0])
    raw = b"".join(canonical(row) for row in rows)
    path.write_bytes(gzip.compress(raw, mtime=0))
    manifest_path = directory / "capture-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest.update(decompressed_bytes=len(raw), decompressed_sha256=hashlib.sha256(raw).hexdigest())
    manifest_path.write_bytes(canonical(manifest))
    _reseal_capture(directory)


def test_actual_composed_output_input_checkpoint_capture_verify_roundtrip(tmp_path):
    case = _composed_capture_case(tmp_path)
    manifest = _capture(case, tmp_path / "capture-composed-valid")
    reviewed = verify(tmp_path / "capture-composed-valid")
    assert manifest["native_acceptance"] == "not_accepted"
    assert reviewed["manifest"]["native_acceptance"] == "not_accepted"
    projected = reviewed["rows"][0]
    source_row = json.loads(case["gameplay_path"].read_bytes())
    assert source_row == case["row"]
    assert projected["acceptance_configuration"] == case["row"]["acceptance_configuration"]
    assert projected["buffer_evidence"] == case["row"]["buffer_evidence"]
    assert projected["input_route_evidence"] == case["row"]["input_route_evidence"]
    for label in ("state", "after_state"):
        assert projected[label]["factory"]["output_buffers"] == case["row"][label]["factory"]["output_buffers"]
        assert projected[label]["factory"]["input_routes"] == case["row"][label]["factory"]["input_routes"]
    initial = json.loads((tmp_path / "capture-composed-valid" / "initial-checkpoint.json").read_bytes())
    final = json.loads((tmp_path / "capture-composed-valid" / "final-checkpoint.json").read_bytes())
    assert initial["output_commitments"] == case["output_commitments"]
    assert final["input_commitments"] == case["input_commitments"]


def test_composed_capture_rejects_missing_enabled_output_extension(tmp_path):
    case = _composed_capture_case(tmp_path)
    for name in ("initial_path", "final_path"):
        boundary = json.loads(case[name].read_bytes())
        boundary.pop("output_buffers_schema", None)
        boundary.pop("output_commitments", None)
        case[name].write_bytes(canonical(boundary))
    trial = json.loads(case["trial_path"].read_bytes())
    trial["initial_checkpoint_sha256"] = hashlib.sha256(case["initial_path"].read_bytes()).hexdigest()
    case["trial_path"].write_bytes(canonical(trial))
    preflight = json.loads(case["preflight_path"].read_bytes())
    preflight["checkpoint_sha256"] = trial["initial_checkpoint_sha256"]
    case["preflight_path"].write_bytes(canonical(preflight))
    with pytest.raises(ValueError):
        _capture(case, tmp_path / "capture-missing-output")


@pytest.mark.parametrize("family", ["output", "input", "outpost", "successor"])
@pytest.mark.parametrize("boundary", ["initial", "final"])
def test_capture_rejects_missing_enabled_checkpoint_extension(tmp_path, family, boundary):
    case_family = "input" if family == "output" else family
    case = _composed_capture_case(tmp_path, family=case_family)
    _drop_case_extension(case, family, boundary)
    with pytest.raises(ValueError):
        _capture(case, tmp_path / f"capture-missing-{family}-{boundary}")


@pytest.mark.parametrize("family", ["output", "input", "outpost", "successor"])
@pytest.mark.parametrize("boundary", ["initial", "final"])
def test_verify_rejects_resealed_missing_enabled_checkpoint_extension(tmp_path, family, boundary):
    case_family = "input" if family == "output" else family
    case = _composed_capture_case(tmp_path, family=case_family)
    directory = tmp_path / f"capture-before-missing-{family}-{boundary}"
    _capture(case, directory)
    _drop_capture_extension(directory, family, boundary)
    with pytest.raises(ValueError):
        verify(directory)


@pytest.mark.parametrize("family", ["outpost", "successor"])
def test_actual_composed_optional_owner_family_roundtrips(tmp_path, family):
    case = _composed_capture_case(tmp_path, family=family)
    manifest = _capture(case, tmp_path / f"capture-{family}-valid")
    reviewed = verify(tmp_path / f"capture-{family}-valid")
    assert manifest["native_acceptance"] == "not_accepted"
    assert reviewed["manifest"]["native_acceptance"] == "not_accepted"
    assert reviewed["rows"][0]["acceptance_configuration"] == case["row"]["acceptance_configuration"]
    if family == "outpost":
        assert json.loads(case["initial_path"].read_bytes())["outpost_commitments"] == case["outpost_commitments"]
        assert reviewed["rows"][0]["state"]["factory"]["mining_outposts"]
    else:
        assert json.loads(case["initial_path"].read_bytes())["successor_receipts"] == case["successor_receipts"]
        assert reviewed["rows"][0]["state"]["factory"]["successors"]


def test_public_capture_verify_accepts_multi_record_outpost_flow_prefix(tmp_path):
    case = _composed_capture_case(tmp_path, family="outpost")
    loop, backend = case["loop"], case["backend"]
    first = case["row"]

    _advance_snapshot_tick(backend.state, 1100)
    before = loop._observe()
    _advance_snapshot_tick(backend.state, 1300)
    commission_outpost(backend.state)
    after = loop._observe()
    assert not loop._outpost_fault
    second = loop._record(before, "observe", "modeled outpost flow receipt advanced", after, True)
    assert second["state"]["tick"] < second["after_state"]["tick"]
    assert second["mining_outpost_evidence"]["sources"]["iron-ore"]["flow"]["conservation"] is True

    case["final_path"].write_bytes(backend.checkpoint.read_bytes())
    case["gameplay_path"].write_bytes(canonical(first) + canonical(second))
    directory = tmp_path / "capture-outpost-flow-prefix"
    manifest = _capture(case, directory)
    reviewed = verify(directory)
    assert manifest["records"] == 2 and len(reviewed["rows"]) == 2
    assert manifest["native_acceptance"] == reviewed["manifest"]["native_acceptance"] == "not_accepted"
    initial = json.loads(case["initial_path"].read_bytes())
    final = json.loads(case["final_path"].read_bytes())
    assert not initial["outpost_commitments"]["iron-ore"]["flow"]
    assert final["outpost_commitments"]["iron-ore"]["flow"]["conservation"] is True


def test_public_capture_verify_accepts_multi_record_successor_receipt_prefix(tmp_path):
    case = _composed_capture_case(tmp_path, family="successor")
    loop, backend = case["loop"], case["backend"]
    source = "growth:iron-plate"
    first = case["row"]
    assert first["successor_projects"][source]["status"] != "qualified"
    initial = json.loads(case["initial_path"].read_bytes())
    assert not initial["successor_receipts"][source]["qualification"]

    _advance_snapshot_tick(backend.state, 2000)
    before = loop._observe()
    qualify_successor(backend.state, backend.state.factory["successors"]["sources"][source])
    _advance_snapshot_tick(backend.state, backend.state.tick)
    after = loop._observe()
    assert not loop._successor_fault and not loop._buffer_fault and not loop._input_fault
    second = loop._record(before, "observe", "modeled successor qualification receipt advanced", after, True)
    assert second["successor_projects"][source]["status"] == "qualified"
    assert after.factory["successors"]["sources"][source]["qualification"]

    case["final_path"].write_bytes(backend.checkpoint.read_bytes())
    case["gameplay_path"].write_bytes(canonical(first) + canonical(second))
    directory = tmp_path / "capture-successor-receipt-prefix"
    manifest = _capture(case, directory)
    reviewed = verify(directory)
    assert manifest["records"] == 2 and len(reviewed["rows"]) == 2
    assert manifest["native_acceptance"] == reviewed["manifest"]["native_acceptance"] == "not_accepted"
    final = json.loads(case["final_path"].read_bytes())
    assert not initial["successor_receipts"][source]["qualification"]
    assert final["successor_receipts"][source]["qualification"]


def test_actual_successor_factory_place_binds_zero_source_through_capture_verify(tmp_path):
    """Exercise the supported pending factory_place 0->bound source transition."""
    from jev_factorio.planning.successors import SuccessorPlanner, marked
    from jev_factorio.successors import initial_kit
    from test_factory import machine
    from test_input_route_integration import native_catalog
    from test_successors import GROWTH, site_offer

    case = _composed_capture_case(tmp_path, family="successor", paid_routes=False)
    loop, backend = case["loop"], case["backend"]
    state = backend.state
    source = GROWTH

    # Use the production site's native contract and actual controller planner.
    # The proposed site has no native successor source row; the controller
    # commits its unbound project immediately before the pending placement.
    predecessor = "recipe:iron-plate"
    predecessor_unit = state.factory["entities"][predecessor]["unit_number"]
    assert state.factory["entities"][predecessor]["name"] == "stone-furnace"
    state.factory["production_sites"] = {
        "protocol": 1, "session_id": state.session_id, "tick": state.tick, "sources": {}}
    state.factory["successors"] = {
        "protocol": 1, "session_id": state.session_id, "tick": state.tick, "sources": {}}
    site = site_offer(state)
    state.inventory = initial_kit(site, "iron-ore")
    state.drill_status = "working"
    state.drill_output_connected = True
    state.placed_entities = ["burner-mining-drill:modeled-predecessor"]
    state.iron_ore_collected = 5
    # The controller commits its source_unit=0 project only after observing
    # this genuinely unowned native boundary; the adapter does not emit a
    # successor source row until the paid furnace exists.
    _advance_snapshot_tick(state, state.tick)
    before = backend.observe()
    planner = SuccessorPlanner(native_catalog(), before, "rocket_launch")
    plan = marked(planner.continuation(source, site["anchor"]), source, site, before)
    loop._work_candidates = lambda observed: ([plan], "")
    initial_raw = {}

    def execute(action, parameters):
        initial_raw["dispatch_entered"] = True
        initial_raw["actual_action"] = action
        initial_raw["actual_parameters"] = deepcopy(parameters)
        initial_raw["pending"] = deepcopy(loop.memory.pending)
        initial_raw["attempt"] = deepcopy(loop.memory.attempt)
        initial_raw["pending_action"] = (loop.memory.pending or {}).get("action")
        initial_raw["attempt_action"] = (loop.memory.attempt or {}).get("action")
        assert action == "factory_place"
        assert parameters == {"role": source, "name": "stone-furnace", "anchor": site["anchor"]}
        assert loop.memory.pending["action"] == "factory_place"
        assert loop.memory.attempt["action"] == "factory_place"
        initial_raw["checkpoint"] = backend.checkpoint.read_bytes()
        initial_raw["checkpoint_bytes"] = len(initial_raw["checkpoint"])
        checkpoint = loop.memory_type.from_bytes(
            initial_raw["checkpoint"], state.session_id, "rocket_launch")
        assert checkpoint.successor_projects[source]["source_unit"] == 0
        assert checkpoint.pending["action"] == "factory_place"
        assert checkpoint.active_plan["steps"][checkpoint.step_index]["action"] == "factory_place"

        state.inventory["stone-furnace"] -= 1
        bound_unit = predecessor_unit + 1000
        state.factory["entities"][source] = machine(
            unit_number=bound_unit, position=deepcopy(site["position"]), products_finished=0)
        site.update(state="owned", source_unit=bound_unit)
        state.factory["successors"]["sources"][source] = {
            "source": source, "item": "iron-plate", "anchor": site["anchor"],
            "predecessor": predecessor, "predecessor_unit": predecessor_unit, "source_unit": bound_unit,
            "paid": 1, "phase": "output_building", "seeded": 0,
            "trial_collected": 0, "credit": 0, "attribution_resets": 0,
            "started_tick": state.tick, "remaining_ore": 1000,
            "use": {}, "qualification": {},
        }
        _advance_snapshot_tick(state, state.tick + 1)
        return "modeled furnace placement returned"

    backend.execute = execute
    emitted = loop.step()
    dispatch_diagnostic = {key: value for key, value in initial_raw.items()
                           if key not in {"checkpoint", "pending", "attempt"}}
    assert emitted["action"] == "factory_place" and emitted["verified"] is True, {
        "outcome": emitted["outcome"], "status": loop.memory.status,
        "reason": loop.memory.reason, "dispatch": dispatch_diagnostic,
    }
    assert source not in emitted["state"]["factory"]["successors"]["sources"]
    bound_unit = emitted["after_state"]["factory"]["successors"]["sources"][source]["source_unit"]
    assert bound_unit != predecessor_unit and bound_unit > 0
    assert emitted["attempt"] is None
    assert emitted["attempt_outcomes"][-1]["action"] == "factory_place"
    assert loop.memory.successor_projects[source]["source_unit"] == bound_unit

    trial = json.loads(case["trial_path"].read_bytes())
    trial["initial_checkpoint_sha256"] = hashlib.sha256(initial_raw["checkpoint"]).hexdigest()
    case["trial_path"].write_bytes(canonical(trial))
    preflight = json.loads(case["preflight_path"].read_bytes())
    preflight["checkpoint_sha256"] = trial["initial_checkpoint_sha256"]
    case["preflight_path"].write_bytes(canonical(preflight))
    case["initial_path"].write_bytes(initial_raw["checkpoint"])
    case["final_path"].write_bytes(backend.checkpoint.read_bytes())
    case["gameplay_path"].write_bytes(canonical(emitted))

    directory = tmp_path / "capture-successor-construction-prefix"
    manifest = _capture(case, directory)
    reviewed = verify(directory)
    assert manifest["records"] == reviewed["manifest"]["records"] == 1
    assert manifest["native_acceptance"] == reviewed["manifest"]["native_acceptance"] == "not_accepted"


@pytest.mark.parametrize("family", ["input", "outpost", "successor"])
def test_actual_composed_enabled_extensions_accept_empty_paid_maps(tmp_path, family):
    case = _composed_capture_case(tmp_path, family=family, paid_routes=False)
    initial = json.loads(case["initial_path"].read_bytes())
    if family == "outpost":
        assert initial["outposts_schema"] == 1 and initial["outpost_commitments"] == {}
    elif family == "successor":
        assert initial["successor_schema"] == 1
        assert initial["successor_projects"] == initial["successor_receipts"] == {}
    assert initial["output_buffers_schema"] == 1 and initial["output_commitments"] == {}
    assert initial["input_routes_schema"] == 1 and initial["input_commitments"] == {}
    manifest = _capture(case, tmp_path / f"capture-{family}-empty-owners")
    reviewed = verify(tmp_path / f"capture-{family}-empty-owners")
    assert manifest["native_acceptance"] == "not_accepted"
    assert reviewed["manifest"]["native_acceptance"] == "not_accepted"


def test_actual_composed_disabled_optional_features_remain_absent_and_valid(tmp_path):
    case = _composed_capture_case(tmp_path, family="disabled")
    initial = json.loads(case["initial_path"].read_bytes())
    assert not set(_checkpoint_extension("output")) & initial.keys()
    assert not set(_checkpoint_extension("input")) & initial.keys()
    row = case["row"]
    for flag, evidence in (("furnace_output_buffers", "buffer_evidence"),
                           ("furnace_input_belts", "input_route_evidence"),
                           ("mining_outpost_evidence", "mining_outpost_evidence"),
                           ("ore_side_successors", "successor_evidence")):
        assert flag not in row and evidence not in row
    assert row["mining_outposts"] is False
    manifest = _capture(case, tmp_path / "capture-disabled-optional-features")
    reviewed = verify(tmp_path / "capture-disabled-optional-features")
    assert manifest["native_acceptance"] == "not_accepted"
    assert reviewed["manifest"]["native_acceptance"] == "not_accepted"


@pytest.mark.parametrize("family", ["output", "input", "outpost", "successor"])
@pytest.mark.parametrize("boundary", ["final_checkpoint", "after_state"])
def test_capture_rejects_paid_owner_loss_for_each_enabled_family(tmp_path, family, boundary):
    case_family = "input" if family == "output" else family
    case = _composed_capture_case(tmp_path, family=case_family)
    if boundary == "final_checkpoint":
        _drop_checkpoint_owner(case, family)
    else:
        row = json.loads(case["gameplay_path"].read_bytes())
        _drop_paid_observation(row, family)
        case["gameplay_path"].write_bytes(canonical(row))
    with pytest.raises(ValueError):
        _capture(case, tmp_path / f"capture-owner-loss-{family}-{boundary}")


@pytest.mark.parametrize("family", ["output", "input", "outpost", "successor"])
def test_verify_rejects_resealed_paid_owner_loss_in_composed_observation(tmp_path, family):
    case_family = "input" if family == "output" else family
    case = _composed_capture_case(tmp_path, family=case_family)
    directory = tmp_path / f"capture-before-owner-loss-{family}"
    _capture(case, directory)
    _rewrite_captured_rows(directory, lambda row: _drop_paid_observation(row, family))
    with pytest.raises(ValueError):
        verify(directory)


@pytest.mark.parametrize("family", ["output", "input", "outpost", "successor"])
def test_verify_rejects_resealed_final_checkpoint_paid_owner_loss(tmp_path, family):
    case_family = "input" if family == "output" else family
    case = _composed_capture_case(tmp_path, family=case_family)
    directory = tmp_path / f"capture-before-checkpoint-owner-loss-{family}"
    _capture(case, directory)
    _drop_capture_checkpoint_owner(directory, family)
    with pytest.raises(ValueError):
        verify(directory)


@pytest.mark.parametrize("family", ["output", "input", "outpost", "successor"])
def test_capture_rejects_paid_receipt_change_for_each_enabled_family(tmp_path, family):
    case_family = "input" if family == "output" else family
    case = _composed_capture_case(tmp_path, family=case_family)
    row = json.loads(case["gameplay_path"].read_bytes())
    _change_paid_receipt(row, family)
    case["gameplay_path"].write_bytes(canonical(row))
    with pytest.raises(ValueError):
        _capture(case, tmp_path / f"capture-receipt-change-{family}")


@pytest.mark.parametrize("family", ["output", "input", "outpost", "successor"])
def test_verify_rejects_resealed_paid_receipt_change_for_each_enabled_family(tmp_path, family):
    case_family = "input" if family == "output" else family
    case = _composed_capture_case(tmp_path, family=case_family)
    directory = tmp_path / f"capture-before-receipt-change-{family}"
    _capture(case, directory)
    _rewrite_captured_rows(directory, lambda row: _change_paid_receipt(row, family))
    with pytest.raises(ValueError):
        verify(directory)


@pytest.mark.parametrize("family,record_flag", [
    ("input", "furnace_output_buffers"),
    ("input", "furnace_input_belts"),
    ("outpost", "mining_outposts"),
    ("successor", "ore_side_successors"),
    ("successor", "background_work"),
])
def test_capture_rejects_record_flag_that_differs_from_composed_treatment(
        tmp_path, family, record_flag):
    case = _composed_capture_case(tmp_path, family=family)
    row = json.loads(case["gameplay_path"].read_bytes())
    row[record_flag] = False
    case["gameplay_path"].write_bytes(canonical(row))
    with pytest.raises(ValueError):
        _capture(case, tmp_path / f"capture-flag-mismatch-{record_flag}")


@pytest.mark.parametrize("family", ["output", "input", "outpost", "successor"])
def test_capture_rejects_paid_owner_layout_drift_for_each_enabled_family(tmp_path, family):
    case_family = "input" if family == "output" else family
    case = _composed_capture_case(tmp_path, family=case_family)
    row = json.loads(case["gameplay_path"].read_bytes())
    _change_owner_layout(row, family)
    case["gameplay_path"].write_bytes(canonical(row))
    with pytest.raises(ValueError):
        _capture(case, tmp_path / f"capture-layout-drift-{family}")


@pytest.mark.parametrize("family", ["output", "input", "outpost", "successor"])
def test_verify_rejects_resealed_paid_owner_layout_drift_for_each_enabled_family(tmp_path, family):
    case_family = "input" if family == "output" else family
    case = _composed_capture_case(tmp_path, family=case_family)
    directory = tmp_path / f"capture-before-layout-drift-{family}"
    _capture(case, directory)
    _rewrite_captured_rows(directory, lambda row: _change_owner_layout(row, family))
    with pytest.raises(ValueError):
        verify(directory)


@pytest.mark.parametrize("family", ["output", "input", "outpost", "successor"])
@pytest.mark.parametrize("entry", ["capture", "verify"])
def test_paid_component_unit_drift_is_rejected_for_each_enabled_family(tmp_path, family, entry):
    case_family = "input" if family == "output" else family
    case = _composed_capture_case(tmp_path, family=case_family)
    if entry == "capture":
        row = json.loads(case["gameplay_path"].read_bytes())
        _change_paid_unit(row, family)
        case["gameplay_path"].write_bytes(canonical(row))
        with pytest.raises(ValueError):
            _capture(case, tmp_path / f"capture-unit-drift-{family}")
    else:
        directory = tmp_path / f"capture-before-unit-drift-{family}"
        _capture(case, directory)
        _rewrite_captured_rows(directory, lambda row: _change_paid_unit(row, family))
        with pytest.raises(ValueError):
            verify(directory)


@pytest.mark.parametrize("family", ["output", "input", "outpost", "successor"])
@pytest.mark.parametrize("entry", ["capture", "verify"])
def test_optional_envelope_tick_mismatch_is_rejected_for_each_enabled_family(tmp_path, family, entry):
    case_family = "input" if family == "output" else family
    case = _composed_capture_case(tmp_path, family=case_family)
    if entry == "capture":
        row = json.loads(case["gameplay_path"].read_bytes())
        _change_optional_envelope_tick(row, family)
        case["gameplay_path"].write_bytes(canonical(row))
        with pytest.raises(ValueError):
            _capture(case, tmp_path / f"capture-stale-envelope-{family}")
    else:
        directory = tmp_path / f"capture-before-stale-envelope-{family}"
        _capture(case, directory)
        _rewrite_captured_rows(directory, lambda row: _change_optional_envelope_tick(row, family))
        with pytest.raises(ValueError):
            verify(directory)
