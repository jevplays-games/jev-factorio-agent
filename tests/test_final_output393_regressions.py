"""Final output ownership must remain tied to emitted telemetry.

The controller records below use its real MRO over bounded synthetic state.
The 32-row analyzable campaign window remains a separate modeled fixture; it
provides the public analyzer's required economics and duration without being
represented as a native or unchanged producer log.
"""
from copy import deepcopy

import pytest

from jev_factorio import integration_evidence as report
from jev_factorio.memory import load_checkpoint
from jev_factorio.output_buffers import expected_commitments
from integration_evidence_fixtures import evidence
from test_integration_evidence import configure_output_buffers
from test_output_buffer_integration import BufferBackend, loop_for, setup as output_setup


def _actual_output_record(tmp_path):
    """Run real OutputBufferMixin observe/record and validate its checkpoint."""
    state, catalog, row = output_setup(True)
    backend = BufferBackend()
    backend.state, backend.catalog, backend.row = state, catalog, row
    loop = loop_for(backend, tmp_path)
    # The ready route represents a previously retained checkpoint owner; the
    # real observer correctly refuses to adopt an already-paid route with no
    # matching transaction or prior durable identity.
    loop.memory.output_commitments[row["source"]] = {
        "source_unit": row["source_unit"],
        "layout": row["layout"],
        "parts": deepcopy(row["parts"]),
    }
    snapshot = loop._observe()
    record = loop._record(snapshot, "observe", "synthetic matched output", snapshot)
    memory = load_checkpoint(tmp_path / "state.json", state.session_id, "rocket_launch")
    return record, expected_commitments(memory)


def _analyzer_args_from_actual_record(tmp_path, *, retain_initial=False):
    emitted, owners = _actual_output_record(tmp_path)
    args = evidence()
    configure_output_buffers(args, True)
    rows, trial, initial, final = args

    # Keep the controller-emitted route and paid identities intact. Only bind
    # each envelope's session/tick to the modeled analyzer fixture observation.
    producer_state = emitted["after_state"]
    route_sources = deepcopy(emitted["buffer_evidence"]["sources"])
    producer_entities = producer_state["factory"]["entities"]
    used_entities = {
        name: deepcopy(producer_entities[name])
        for source in route_sources.values()
        for name in (source["source"], *(
            paid["role"] for paid in source["parts"].values()))
        if name in producer_entities
    }
    for record in rows:
        for label in ("state", "after_state"):
            state = record[label]
            state["factory"]["entities"].update(deepcopy(used_entities))
            state["factory"]["output_buffers"] = {
                "protocol": 1,
                "session_id": state["session_id"],
                "tick": state["tick"],
                "sources": deepcopy(route_sources),
            }
        record["buffer_evidence"] = deepcopy(
            record["after_state"]["factory"]["output_buffers"]
        )

    initial["output_commitments"] = deepcopy(owners if retain_initial else {})
    final["output_commitments"] = deepcopy(owners)
    return args, emitted, owners


def test_actual_composed_record_and_union_checkpoint_keep_output_identity(tmp_path):
    record, owners = _actual_output_record(tmp_path)
    observed = record["after_state"]["factory"]["output_buffers"]
    assert record["schema_version"] == 2
    assert record["furnace_output_buffers"] is True
    assert record["buffer_evidence"] == observed
    assert set(owners) == set(observed["sources"])
    for source, owner in owners.items():
        native = observed["sources"][source]
        assert owner == {
            "source_unit": native["source_unit"],
            "layout": native["layout"],
            "parts": native["parts"],
        }


def _actual_successor_record(tmp_path):
    from jev_factorio.memory import load_checkpoint
    from jev_factorio.output_buffers import expected_commitments
    from test_successors import Backend as SuccessorBackend, GROWTH, KIND, flowing_state
    from jev_factorio.successor_controller import _empty_receipts

    state, successor = flowing_state()
    backend = SuccessorBackend(state)
    checkpoint = tmp_path / "successor-state.json"
    loop = KIND(
        backend, policy="deterministic", target="rocket_launch",
        factory_scheduling="ready-work", tick_seconds=0, checkpoint=str(checkpoint),
    )
    project = {
        "anchor": successor["anchor"],
        "predecessor_unit": successor["predecessor_unit"],
        "source_unit": successor["source_unit"],
        "started_tick": successor["started_tick"],
        "deadline_tick": successor["started_tick"] + 216000,
        "status": "active",
    }
    output = state.factory["output_buffers"]["sources"][GROWTH]
    input_route = state.factory["input_routes"]["sources"][GROWTH]
    receipts = _empty_receipts()
    receipts.update(
        output_layout=output["layout"], input_layout=input_route["layout"],
        output=deepcopy(output["parts"]), input=deepcopy(input_route["parts"]),
        use=deepcopy(successor["use"]), qualification=deepcopy(successor["qualification"]),
    )
    memory = loop.memory_type(
        state.session_id, "rocket_launch", active_goal="rocket_launch", last_tick=state.tick,
    )
    memory.successor_projects[GROWTH] = project
    memory.successor_receipts[GROWTH] = receipts
    memory.input_commitments[GROWTH] = {
        "source_unit": input_route["source_unit"], "layout": input_route["layout"],
        "parts": deepcopy(input_route["parts"]),
    }
    loop.memory = memory
    snapshot = loop._observe()
    record = loop._record(snapshot, "observe", "retained successor output", snapshot)
    typed = load_checkpoint(checkpoint, state.session_id, "rocket_launch")
    combined = expected_commitments(typed)

    assert not loop._buffer_fault
    assert not loop._input_fault
    assert not loop._successor_fault
    assert record["furnace_output_buffers"] is True
    assert record["ore_side_successors"] is True
    assert record["buffer_evidence"] == record["after_state"]["factory"]["output_buffers"]
    return record, typed, combined


def test_actual_successor_mro_retains_output_receipt_in_typed_checkpoint(tmp_path):
    from test_successors import GROWTH

    record, typed, combined = _actual_successor_record(tmp_path)
    output = record["after_state"]["factory"]["output_buffers"]["sources"][GROWTH]
    assert combined[GROWTH] == {
        "source_unit": output["source_unit"], "layout": output["layout"],
        "parts": output["parts"],
    }


def _successor_analyzer_args(tmp_path):
    from copy import deepcopy

    emitted, typed, combined = _actual_successor_record(tmp_path)
    args = evidence()
    configure_output_buffers(args, True)
    rows, trial, initial, final = args
    trial["configuration"].update(
        background_work=True, furnace_input_belts=True, ore_side_successors=True,
    )
    project = deepcopy(typed.successor_projects)
    successor_receipts = deepcopy(typed.successor_receipts)
    input_commitments = deepcopy(typed.input_commitments)
    for checkpoint in (initial, final):
        checkpoint.update(
            background_schema=typed.background_schema,
            background_job=deepcopy(typed.background_job),
            background_attempt=deepcopy(typed.background_attempt),
            input_routes_schema=typed.input_routes_schema,
            input_commitments=deepcopy(input_commitments),
            successor_schema=typed.successor_schema,
            successor_projects=deepcopy(project),
            successor_receipts=deepcopy(successor_receipts),
        )

    source_factory = emitted["after_state"]["factory"]
    for record in rows:
        record["acceptance_configuration"] = deepcopy(trial["configuration"])
        record.update(
            background_work=True,
            background_schema=emitted["background_schema"],
            background_job=deepcopy(emitted["background_job"]),
            background_attempt=deepcopy(emitted["background_attempt"]),
            furnace_output_buffers=True,
            furnace_input_belts=True,
            input_validation_failure={},
            ore_side_successors=True,
            successor_projects=deepcopy(project),
        )
        for label in ("state", "after_state"):
            state = record[label]
            factory = state["factory"]
            factory["entities"].update(deepcopy(source_factory["entities"]))
            for field in ("output_buffers", "input_routes", "successors", "production_sites"):
                envelope = deepcopy(source_factory[field])
                envelope["session_id"] = state["session_id"]
                envelope["tick"] = state["tick"]
                factory[field] = envelope
        after_factory = record["after_state"]["factory"]
        record["buffer_evidence"] = deepcopy(after_factory["output_buffers"])
        record["input_route_evidence"] = deepcopy(after_factory["input_routes"])
        record["successor_evidence"] = deepcopy(after_factory["successors"])

    return args, combined


def test_analyzer_accepts_actual_successor_output_from_emitted_mro(tmp_path):
    from test_successors import GROWTH

    args, combined = _successor_analyzer_args(tmp_path)
    result = report.analyze_rows(*args)
    assert result["integrity_checks_passed"], result["issues"]
    assert result["native_acceptance"] == "not_accepted"
    assert GROWTH in combined


@pytest.mark.parametrize("change", ["paid_receipt", "checkpoint_owner_removed"])
def test_analyzer_rejects_successor_output_owner_disagreement(tmp_path, change):
    from test_successors import GROWTH

    args, _ = _successor_analyzer_args(tmp_path)
    rows, _, _, final = args
    if change == "paid_receipt":
        envelope = rows[-1]["after_state"]["factory"]["output_buffers"]
        envelope["sources"][GROWTH]["parts"]["chest"]["receipt"] = "changed-successor-receipt"
        rows[-1]["buffer_evidence"] = deepcopy(envelope)
    else:
        final["successor_receipts"][GROWTH]["output"] = {}
        final["successor_receipts"][GROWTH]["output_layout"] = None

    result = report.analyze_rows(*args)
    assert not result["integrity_checks_passed"]
    assert result["native_acceptance"] == "not_accepted"
    if change == "paid_receipt":
        assert ("final_composed_ownership_not_observed" in result["issues"]
                or "final_successor_output_receipts_mismatch" in result["issues"])
    else:
        assert "observed_composed_commitment_missing" in result["issues"]


def test_analyzer_accepts_matching_emitted_output_and_initial_empty_owner(tmp_path):
    args, _, _ = _analyzer_args_from_actual_record(tmp_path)
    report_value = report.analyze_rows(*args)
    assert report_value["integrity_checks_passed"], report_value["issues"]
    assert report_value["native_acceptance"] == "not_accepted"


def test_analyzer_accepts_matching_emitted_output_and_retained_initial_owner(tmp_path):
    args, _, _ = _analyzer_args_from_actual_record(tmp_path, retain_initial=True)
    report_value = report.analyze_rows(*args)
    assert report_value["integrity_checks_passed"], report_value["issues"]
    assert report_value["native_acceptance"] == "not_accepted"


@pytest.mark.parametrize(
    ("change", "expected_issue"),
    [
        ("missing_source", "final_composed_ownership_not_observed"),
        ("source_unit", "invalid_final_composed_observation"),
        ("layout", "final_composed_ownership_not_observed"),
        ("source_identity", "invalid_final_composed_observation"),
        ("item", "invalid_final_composed_observation"),
        ("chest_role", "final_composed_ownership_not_observed"),
        ("chest_unit", "invalid_final_composed_observation"),
        ("chest_receipt", "final_composed_ownership_not_observed"),
        ("chest_paid", "invalid_final_composed_observation"),
        ("chest_missing", "invalid_final_composed_observation"),
        ("unsupported_protocol", "invalid_final_composed_observation"),
        ("stale_tick", "invalid_final_composed_observation"),
        ("wrong_session", "invalid_final_composed_observation"),
        ("malformed_sources", "invalid_final_composed_observation"),
        ("missing_envelope", "invalid_final_composed_observation"),
        ("top_level_mismatch", "final_output_evidence_log_mismatch"),
    ],
)
def test_analyzer_rejects_final_output_telemetry_disagreement(
    tmp_path, change, expected_issue
):
    args, _, owners = _analyzer_args_from_actual_record(tmp_path)
    rows = args[0]
    final_record = rows[-1]
    envelope = final_record["after_state"]["factory"]["output_buffers"]
    source = next(iter(owners))
    if change == "missing_source":
        envelope["sources"].clear()
    elif change == "source_unit":
        envelope["sources"][source]["source_unit"] += 1
    elif change == "layout":
        envelope["sources"][source]["layout"] = "replacement-layout"
    elif change == "source_identity":
        envelope["sources"][source]["source"] = "recipe:copper-plate"
    elif change == "item":
        envelope["sources"][source]["item"] = "copper-plate"
    elif change == "chest_role":
        envelope["sources"][source]["parts"]["chest"]["role"] = "replacement-chest"
    elif change == "chest_unit":
        envelope["sources"][source]["parts"]["chest"]["unit_number"] += 1
    elif change == "chest_receipt":
        envelope["sources"][source]["parts"]["chest"]["receipt"] = "replacement-receipt"
    elif change == "chest_paid":
        envelope["sources"][source]["parts"]["chest"]["paid"] = 0
    elif change == "chest_missing":
        envelope["sources"][source]["parts"].pop("chest")
    elif change == "unsupported_protocol":
        envelope["protocol"] = 2
    elif change == "stale_tick":
        envelope["tick"] -= 1
    elif change == "wrong_session":
        envelope["session_id"] = "other-session"
    elif change == "malformed_sources":
        envelope["sources"] = []
    elif change == "missing_envelope":
        final_record["after_state"]["factory"].pop("output_buffers")
    elif change == "top_level_mismatch":
        final_record["buffer_evidence"] = {}
    if change not in {"top_level_mismatch", "missing_envelope"}:
        final_record["buffer_evidence"] = deepcopy(envelope)

    result = report.analyze_rows(*args)
    assert expected_issue in result["issues"], result["issues"]
    assert not result["integrity_checks_passed"]
    assert not result["measurement_checks_passed"]
    assert result["native_acceptance"] == "not_accepted"


def test_analyzer_rejects_paid_final_output_missing_from_checkpoint(tmp_path):
    args, _, _ = _analyzer_args_from_actual_record(tmp_path)
    args[3]["output_commitments"] = {}
    result = report.analyze_rows(*args)
    assert "observed_composed_commitment_missing" in result["issues"]
    assert not result["integrity_checks_passed"]
    assert result["native_acceptance"] == "not_accepted"
