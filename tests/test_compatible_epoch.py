"""Actual consumed bridge facts, with signature transport explicitly mocked.

These tests prove data scope, not a native signature or campaign migration.
"""
import base64
from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path

import pytest

from jev_factorio.memory import CampaignMemory
from jev_factorio import compatible_epoch as epoch
from jev_factorio import compatible_recovery as recovery

FACTS = json.loads((Path(__file__).parent / "fixtures" /
                   "terminal108_compatible_epoch_facts.json").read_text())


def setup(tmp_path, monkeypatch):
    memory = CampaignMemory(session_id="48291babb033466fa38fda061b43c264", target="rocket_launch")
    old = deepcopy(FACTS["historical_record"])
    # Platform-local synthetic authority preimage; actual consumed facts remain exact.
    old["lock_path"] = str(tmp_path / "writer.lock")
    old["authorization_sha256"] = recovery.digest_json({k: old[k] for k in recovery._KEYS})
    memory.compatible_source_recoveries = [old]
    memory.blocked_reevaluations = deepcopy(FACTS["rows"])
    memory.history = deepcopy(FACTS["events"])
    record = {k: deepcopy(old[k]) for k in recovery._KEYS}
    record.update(authorization_id="epoch-compatible-test", checkpoint_sha256=FACTS["checkpoint_sha256"],
                  previous_source=deepcopy(FACTS["endpoint"]),
                  current_source={"commit": "f" * 40, "source_sha256": "e" * 64},
                  decision_contract_sha256=FACTS["edges"][-1]["current_contract_sha256"])
    record["authorization_sha256"] = recovery.digest_json(record)
    body = {"schema": "jev.compatible-epoch-boundary.v1", "session_id": memory.session_id,
            "target": memory.target, "terminal_checkpoint_sha256": record["checkpoint_sha256"],
            "authorization_sha256": record["authorization_sha256"],
            "prior_lineage_sha256": hashlib.sha256(epoch._canonical(memory.compatible_source_recoveries)).hexdigest(),
            "previous_source": old["current_source"],
            "previous_contract_sha256": old["decision_contract_sha256"],
            "current_source": record["previous_source"],
            "current_contract_sha256": record["decision_contract_sha256"],
            "edges": deepcopy(FACTS["edges"])}
    witness = {"body": body, "signature_base64": base64.b64encode(b"transport-mocked").decode(),
               "signers_base64": base64.b64encode(b"transport-mocked").decode()}
    monkeypatch.setattr(epoch, "_verify_signature", lambda *args: None)
    record["epoch_witness"] = witness
    return memory, old, record


def test_actual_ordered_bridge_scopes_only_new_epoch(tmp_path, monkeypatch):
    memory, old, record = setup(tmp_path, monkeypatch)
    before = deepcopy(memory.compatible_source_recoveries)
    epoch.validate_epoch_witness(record["epoch_witness"], memory, record,
                                 old["current_source"], old["decision_contract_sha256"],
                                 require_live_history=True)
    memory.compatible_source_recoveries.append(record)
    assert recovery.approved_sources(memory, record["current_source"]) == [
        record["previous_source"], record["current_source"]]
    assert memory.compatible_source_recoveries[:-1] == before
    assert old["previous_source"] not in recovery.approved_sources(memory, record["current_source"])
    assert old["current_source"] not in recovery.approved_sources(memory, record["current_source"])


def test_rolling_history_retains_signed_preimages(tmp_path, monkeypatch):
    memory, old, record = setup(tmp_path, monkeypatch)
    memory.compatible_source_recoveries.append(record)
    for tick in range(70):
        CampaignMemory.event(memory, "ordinary", tick=tick)
    assert not any(h.get("kind") == "blocked_decision_reevaluation_consumed" for h in memory.history)
    assert recovery.validate_lineage(memory)[-1] == record


@pytest.mark.parametrize("mutation", ["missing_row", "row_changed", "reordered", "duplicate",
                                    "event_changed", "endpoint", "auth", "checkpoint", "missing_witness",
                                    "duplicate_row", "duplicate_proof", "wrong_contract", "missing_edge"])
def test_bridge_contraries_fail_closed(tmp_path, monkeypatch, mutation):
    memory, old, record = setup(tmp_path, monkeypatch)
    body = record["epoch_witness"]["body"]
    if mutation == "missing_row": memory.blocked_reevaluations.pop(0)
    elif mutation == "row_changed": memory.blocked_reevaluations[0]["state"] = "prepared"
    elif mutation == "reordered": body["edges"].reverse()
    elif mutation == "duplicate": body["edges"].insert(1, deepcopy(body["edges"][0]))
    elif mutation == "event_changed": memory.history[0]["tick"] += 1
    elif mutation == "endpoint": body["current_source"]["source_sha256"] = "d" * 64
    elif mutation == "auth": body["authorization_sha256"] = "d" * 64
    elif mutation == "checkpoint": body["terminal_checkpoint_sha256"] = "d" * 64
    elif mutation == "missing_witness": del record["epoch_witness"]
    elif mutation == "duplicate_row": memory.blocked_reevaluations.append(deepcopy(memory.blocked_reevaluations[0]))
    elif mutation == "duplicate_proof": body["edges"][1]["prepared_sha256"] = body["edges"][0]["prepared_sha256"]
    elif mutation == "wrong_contract": body["current_contract_sha256"] = "a" * 64
    elif mutation == "missing_edge": body["edges"].pop(1)
    memory.compatible_source_recoveries.append(record)
    with pytest.raises(ValueError): recovery.validate_lineage(memory)


def test_live_authorizer_rejects_missing_event(tmp_path, monkeypatch):
    memory, old, record = setup(tmp_path, monkeypatch)
    memory.history.clear()
    with pytest.raises(ValueError):
        epoch.validate_epoch_witness(record["epoch_witness"], memory, record,
                                     old["current_source"], old["decision_contract_sha256"],
                                     require_live_history=True)


def test_forged_public_trust_rejected_before_crypto():
    with pytest.raises(ValueError): epoch._verify_signature(b"body", b"signature", b"forged trust")


@pytest.mark.skipif(os.name != "posix", reason="Native enrolled OpenSSH verifier")
def test_real_enrolled_crypto_rejects_unsigned_witness():
    signers = (Path(__file__).parent / "fixtures" /
               "compatible_epoch_enrolled_signers.txt").read_bytes()
    with pytest.raises(ValueError):
        epoch._verify_signature(b"unsigned epoch", b"not an OpenSSH signature", signers)


@pytest.mark.parametrize("value", [True, 1.0])
def test_typed_consumed_row_cannot_be_rewritten(tmp_path, monkeypatch, value):
    memory, old, record = setup(tmp_path, monkeypatch)
    memory.blocked_reevaluations[0]["schema"] = value
    memory.compatible_source_recoveries.append(record)
    with pytest.raises(ValueError): recovery.validate_lineage(memory)


def test_unsigned_checkpoint_cannot_create_epoch(tmp_path, monkeypatch):
    memory, old, record = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(epoch, "_verify_signature", lambda *args: (_ for _ in ()).throw(ValueError("rejected signature")))
    memory.compatible_source_recoveries.append(record)
    with pytest.raises(ValueError, match="rejected signature"): recovery.validate_lineage(memory)


def test_authorizer_binds_full_current_checkpoint_without_mutating_it(tmp_path, monkeypatch):
    from dataclasses import asdict
    import hashlib
    from jev_factorio import blocked_reevaluation
    memory, old, record = setup(tmp_path, monkeypatch)
    memory.status, memory.reason, memory.stalled_decisions = "blocked", "model abstention", 4
    memory.blocked_recovery = {"schema": 1, "session_id": memory.session_id,
        "source_revision": deepcopy(record["previous_source"]), "attempts": [],
        "last_input_sha256": None, "wait_level": 0}
    raw = epoch._canonical(asdict(memory))
    authority = {key: deepcopy(record[key]) for key in recovery._KEYS}
    authority.update(checkpoint_sha256=hashlib.sha256(raw).hexdigest(), scope=recovery.scope(memory))
    witness = deepcopy(record["epoch_witness"])
    witness["body"].update(terminal_checkpoint_sha256=authority["checkpoint_sha256"],
                           authorization_sha256=recovery.digest_json(authority))
    monkeypatch.setattr(blocked_reevaluation, "validate_source_revision", lambda *a, **k: {
        "source_head": authority["current_source"]["commit"],
        "decision_contract_sha256": authority["decision_contract_sha256"]})
    monkeypatch.setattr(recovery, "validate_budget_contract", lambda *a: None)
    before = deepcopy(asdict(memory))
    result = recovery.validate_authorization(authority, raw, memory,
        authority["current_source"], authority["owner_invocation"], epoch_witness=witness)
    assert result["epoch_witness"] == witness and asdict(memory) == before
    with pytest.raises(ValueError):
        recovery.validate_authorization(authority, raw + b" ", memory,
            authority["current_source"], authority["owner_invocation"], epoch_witness=witness)


def test_rehashed_old_record_scope_still_rejected(tmp_path, monkeypatch):
    memory, old, record = setup(tmp_path, monkeypatch)
    old["scope"]["state_sha256"] = "f" * 64
    old["authorization_sha256"] = recovery.digest_json({k: old[k] for k in recovery._KEYS})
    memory.compatible_source_recoveries.append(record)
    with pytest.raises(ValueError): recovery.validate_lineage(memory)


def test_second_epoch_binds_prefix_including_first_witness(tmp_path, monkeypatch):
    memory, old, first = setup(tmp_path, monkeypatch)
    memory.compatible_source_recoveries.append(first)
    current = {"commit": "a" * 40, "source_sha256": "b" * 64}
    row = deepcopy(FACTS["rows"][-1])
    row.update(authorization_id="second-epoch-once", blocked_source_revision=first["current_source"]["commit"],
               source_head=current["commit"], decision_contract_sha256="c" * 64,
               stalled_decisions=4, tick=row["tick"] + 1)
    event = {key: row[key] for key in epoch._HISTORY - {"kind"}}
    event["kind"] = "blocked_decision_reevaluation_consumed"
    memory.blocked_reevaluations.append(row)
    memory.history.append(event)
    second = {key: deepcopy(first[key]) for key in recovery._KEYS}
    second.update(authorization_id="second-compatible", previous_source=current,
                  current_source={"commit": "b" * 40, "source_sha256": "c" * 64},
                  decision_contract_sha256="c" * 64)
    second["authorization_sha256"] = recovery.digest_json(second)
    witness = deepcopy(first["epoch_witness"])
    witness["body"].update(authorization_sha256=second["authorization_sha256"],
        prior_lineage_sha256=hashlib.sha256(epoch._canonical(memory.compatible_source_recoveries)).hexdigest(),
        previous_source=first["current_source"], previous_contract_sha256=first["decision_contract_sha256"],
        current_source=current, current_contract_sha256="c" * 64,
        edges=[{"previous_source": first["current_source"], "current_source": current,
                "previous_contract_sha256": first["decision_contract_sha256"],
                "current_contract_sha256": "c" * 64, "source_proof_sha256": "d" * 64,
                "prepared_sha256": "e" * 64, "result_sha256": "f" * 64,
                "row": row, "history": event}])
    second["epoch_witness"] = witness
    memory.compatible_source_recoveries.append(second)
    assert recovery.approved_sources(memory, second["current_source"]) == [current, second["current_source"]]
    first["epoch_witness"]["signature_base64"] = base64.b64encode(b"changed historical signature").decode()
    with pytest.raises(ValueError): recovery.validate_lineage(memory)
