"""Epoch-bearing ordinary CLI guards retain actual ledger/history facts.

Bridge rows/events are recorded public game facts. Signature/source transports
are mocked explicitly; this suite does not claim native migration or gameplay.
"""
from copy import deepcopy
from dataclasses import asdict
import json
import sys
import pytest
from test_compatible_epoch import setup
from jev_factorio.blocked_persistence import validate_checkpoint_metadata


def checkpoint(tmp_path, monkeypatch):
    memory, _, record = setup(tmp_path, monkeypatch)
    memory.compatible_source_recoveries.append(record)
    memory.status, memory.reason = "blocked", "model abstention"
    memory.blocked_recovery = {"schema": 1, "session_id": memory.session_id,
        "source_revision": deepcopy(record["current_source"]), "attempts": [{
            "source_revision": deepcopy(record["previous_source"]),
            "decision_input_sha256": "c" * 64, "reason": None, "tick": 1,
            "outcome": "failed"}], "last_input_sha256": "c" * 64, "wait_level": 1}
    return asdict(memory), record["current_source"], record["owner_invocation"]


def test_epoch_metadata_validates_complete_consumed_rows_and_history(tmp_path, monkeypatch):
    data, source, owner = checkpoint(tmp_path, monkeypatch)
    before = deepcopy(data)
    validate_checkpoint_metadata(data, source, owner_context=owner)
    assert data == before


@pytest.mark.parametrize("mutation", ["missing_rows", "missing_history", "changed_row", "changed_event", "wrong_owner"])
def test_epoch_metadata_contraries_fail_closed(tmp_path, monkeypatch, mutation):
    data, source, owner = checkpoint(tmp_path, monkeypatch)
    if mutation == "missing_rows": del data["blocked_reevaluations"]
    elif mutation == "missing_history": del data["history"]
    elif mutation == "changed_row": data["blocked_reevaluations"][0]["state"] = "prepared"
    elif mutation == "changed_event": data["history"][0]["tick"] += 1
    else: owner = {**owner, "run_id": "different-run"}
    with pytest.raises(ValueError): validate_checkpoint_metadata(data, source, owner_context=owner)


def test_ordinary_cli_epoch_checkpoint_reaches_pre_provider_boundary(tmp_path, monkeypatch):
    from jev_factorio import main, jev_client, provenance
    data, source, owner = checkpoint(tmp_path, monkeypatch)
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(data))
    raw = path.read_bytes()
    monkeypatch.setenv("JEV_FACTORIO_PROVENANCE", json.dumps({**owner, "code_revision": source}))
    monkeypatch.setattr(provenance, "source_revision", lambda *a, **kw: source)
    class PreProviderBoundary(Exception): pass
    def stop(**kwargs): raise PreProviderBoundary()
    monkeypatch.setattr(jev_client, "make_client", stop)
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: pytest.fail("Backend must not be constructed"))
    monkeypatch.setattr(sys, "argv", ["jev-factorio", "--backend", "fle", "--controller", "hierarchical",
        "--policy", "jev", "--target", "rocket_launch", "--until-complete", "--resume", "--resume-controller",
        "--checkpoint", str(path), "--tick-seconds", "0.25", "--persist-recoverable-blocks", "--persistent-idle-observations", "0"])
    with pytest.raises(PreProviderBoundary): main.cli()
    assert path.read_bytes() == raw
