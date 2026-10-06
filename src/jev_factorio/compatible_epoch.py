"""Signed historical epoch boundaries; never a fresh decision allowance.

The supervisor signs evidence it authenticated through the retained source
receipt chain. Reload verifies that attestation, not a new external proof walk.
"""
import base64
import hashlib
import json
import math
import os
import subprocess
import zlib

from .paid_selection_reconciliation import RECONCILIATION_SIGNERS_SHA256

_FIELDS = {"schema", "session_id", "target", "terminal_checkpoint_sha256",
           "prior_lineage_sha256",
           "authorization_sha256", "previous_source", "previous_contract_sha256",
           "current_source", "current_contract_sha256", "edges"}
_EDGE = {"previous_source", "current_source", "previous_contract_sha256",
         "current_contract_sha256", "source_proof_sha256", "prepared_sha256",
         "result_sha256", "row", "history"}
_ROW = {"authorization_id", "blocked_source_revision", "checkpoint_sha256",
        "decision_contract_sha256", "reason", "schema", "source_head",
        "stalled_decisions", "state", "tick"}
_HISTORY = {"blocked_source_revision", "decision_contract_sha256", "kind",
            "source_head", "stalled_decisions", "tick"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _require(value):
    if not value:
        raise ValueError("Invalid signed compatible epoch boundary")


def _decode(value, maximum):
    _require(type(value) is str and len(value) <= 4 * ((maximum + 2) // 3))
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid epoch signature encoding") from error
    _require(0 < len(raw) <= maximum and base64.b64encode(raw).decode() == value)
    return raw


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result)
            result[key] = value
        return result

    def finite_float(value):
        try:
            number = float(value)
        except (OverflowError, ValueError) as error:
            raise ValueError(value) from error
        if not math.isfinite(number):
            raise ValueError(value)
        return number

    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_float=finite_float,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError('Invalid retained epoch evidence JSON') from error


def _retained_history_evidence(evidence, edge, body, owner):
    """Authenticate a complete original journal row and its launch/result links.

    The enclosing enrolled signature attests the operator's captured source
    proof. These exact byte preimages replace only the rolling history cache;
    the current durable consumed row remains mandatory.
    """
    from .compatible_recovery import _digest
    _require(type(evidence) is dict and set(evidence) == {
        'record_sha256', 'record_zlib_base64', 'prepared_base64', 'result_base64'})
    compressed = _decode(evidence['record_zlib_base64'], 128 * 1024)
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, 512 * 1024 + 1)
    except zlib.error as error:
        raise ValueError('Invalid retained epoch compression') from error
    _require(len(raw) <= 512 * 1024 and decoder.eof and not decoder.unused_data
             and not decoder.unconsumed_tail)
    _require(hashlib.sha256(raw).hexdigest() == _digest(evidence['record_sha256']))
    prepared_raw = _decode(evidence['prepared_base64'], 16384)
    result_raw = _decode(evidence['result_base64'], 16384)
    _require(hashlib.sha256(prepared_raw).hexdigest() == edge['prepared_sha256']
             and hashlib.sha256(result_raw).hexdigest() == edge['result_sha256'])
    record, prepared, result = map(_strict_json, (raw, prepared_raw, result_raw))
    _require(all(type(value) is dict for value in (record, prepared, result)))
    provenance = prepared.get('provenance')
    _require(type(provenance) is dict and record.get('run_id') == owner['run_id']
             and provenance.get('run_id') == owner['run_id']
             and type(record.get('execution_id')) is str and record['execution_id']
             and record['execution_id'] == provenance.get('execution_id')
             and record.get('code_revision') == provenance.get('code_revision') == edge['current_source']
             and record.get('session_id') == body['session_id']
             and record.get('target') == body['target']
             and record.get('controller') == 'hierarchical' and record.get('policy') == 'jev'
             and type(record.get('schema_version')) is int and record['schema_version'] == 2
             and type(record.get('tick')) is int and record['tick'] >= edge['row']['tick'])
    history = record.get('history')
    _require(type(history) is list and all(type(event) is dict for event in history))
    matches = [event for event in history if event.get('kind') == edge['history']['kind']
               and event.get('source_head') == edge['current_source']['commit']]
    _require(len(matches) == 1 and _canonical(matches[0]) == _canonical(edge['history']))
    argv = prepared.get('argv')
    _require(type(argv) is list and all(type(value) is str for value in argv)
             and argv.count('--reevaluate-blocked-once') == 1)
    for flag, expected in (('--blocked-source-revision', edge['previous_source']['commit']),
                           ('--exact-checkpoint-sha256', edge['row']['checkpoint_sha256'])):
        _require(argv.count(flag) == 1)
        position = argv.index(flag) + 1
        _require(position < len(argv) and argv[position] == expected)
    _require(result.get('prepared_sha256') == edge['prepared_sha256']
             and _digest(prepared.get('script_sha256')) == result.get('script_sha256')
             and type(result.get('exit_code')) is int)
    _digest(result.get('checkpoint_sha256'))
    return len(raw)


def _verify_signature(raw, signature, signers):
    _require(hashlib.sha256(signers).hexdigest() == RECONCILIATION_SIGNERS_SHA256)
    descriptors = []
    try:
        for label, data in (("epoch-signature", signature), ("epoch-signers", signers)):
            fd = os.memfd_create(label, os.MFD_CLOEXEC)
            descriptors.append(fd)
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                _require(count > 0)
                view = view[count:]
            os.lseek(fd, 0, os.SEEK_SET)
        result = subprocess.run([
            "/usr/bin/ssh-keygen", "-Y", "verify", "-f",
            f"/proc/self/fd/{descriptors[1]}", "-I", "Timothy.Gregg@complete.tech",
            "-n", "file", "-s", f"/proc/self/fd/{descriptors[0]}"],
            input=raw, capture_output=True, pass_fds=tuple(descriptors), timeout=15)
        _require(result.returncode == 0)
    finally:
        for fd in descriptors:
            os.close(fd)


def validate_epoch_witness(witness, memory, record, previous, previous_contract,
                           *, checkpoint_raw=None, require_live_history=False,
                           prior_records=None):
    """Authenticate one complete signed bridge and its unchanged consumed rows.

    Ordinary history can roll away later; the signed event preimages remain.
    A live v1 authorizer requires them in the current history. V2 also binds
    complete retained gameplay and launcher records when that cache rolled.
    """
    from .blocked_persistence import _source
    from .compatible_recovery import _digest
    _require(type(witness) is dict and set(witness) ==
             {"body", "signature_base64", "signers_base64"})
    body = witness["body"]
    _require(type(body) is dict and set(body) == _FIELDS
             and body["schema"] in {"jev.compatible-epoch-boundary.v1", "jev.compatible-epoch-boundary.v2"}
             and body["session_id"] == memory.session_id and body["target"] == memory.target)
    raw = _canonical(body)
    retained_history = body['schema'] == 'jev.compatible-epoch-boundary.v2'
    _require(len(raw) <= (1024 * 1024 if retained_history else 128 * 1024))
    for key in ("terminal_checkpoint_sha256", "authorization_sha256", "prior_lineage_sha256",
                "previous_contract_sha256", "current_contract_sha256"):
        _digest(body[key])
    if prior_records is None:
        prior_records = memory.compatible_source_recoveries
    _require(type(prior_records) is list and
             hashlib.sha256(_canonical(prior_records)).hexdigest() == body["prior_lineage_sha256"])
    _require(_canonical(body["previous_source"]) == _canonical(previous)
             and body["previous_contract_sha256"] == previous_contract
             and _canonical(body["current_source"]) == _canonical(record["previous_source"])
             and body["current_contract_sha256"] == record["decision_contract_sha256"]
             and body["authorization_sha256"] == record["authorization_sha256"]
             and body["terminal_checkpoint_sha256"] == record["checkpoint_sha256"])
    if checkpoint_raw is not None:
        _require(type(checkpoint_raw) is bytes and
                 hashlib.sha256(checkpoint_raw).hexdigest() == body["terminal_checkpoint_sha256"])
    edges = body["edges"]
    _require(type(edges) is list and 0 < len(edges) <= 128)
    prior, contract = _source(previous), previous_contract
    ids, sources, positions, proofs = set(), {prior["commit"]}, [], set()
    ledger = memory.blocked_reevaluations
    _require(type(ledger) is list and all(type(row) is dict for row in ledger)
             and type(memory.history) is list
             and all(type(event) is dict for event in memory.history))
    decoded_history_bytes = 0
    for edge in edges:
        _require(type(edge) is dict and set(edge) ==
                 (_EDGE | {'history_evidence'} if retained_history else _EDGE))
        old, new = _source(edge["previous_source"]), _source(edge["current_source"])
        _digest(old["source_sha256"])
        _digest(new["source_sha256"])
        for key in ("previous_contract_sha256", "current_contract_sha256",
                    "source_proof_sha256", "prepared_sha256", "result_sha256"):
            _digest(edge[key])
        proof = (edge["source_proof_sha256"], edge["prepared_sha256"], edge["result_sha256"])
        _require(all(value not in proofs for value in proof))
        proofs.update(proof)
        _require(_canonical(old) == _canonical(prior)
                 and edge["previous_contract_sha256"] == contract
                 and edge["current_contract_sha256"] != contract
                 and new["commit"] not in sources)
        row, event = edge["row"], edge["history"]
        _require(type(row) is dict and set(row) == _ROW and type(event) is dict
                 and set(event) == _HISTORY and type(row["schema"]) is int
                 and row["schema"] == 1 and row["state"] == "consumed"
                 and type(row["authorization_id"]) is str and row["authorization_id"] not in ids
                 and row["blocked_source_revision"] == old["commit"]
                 and row["source_head"] == new["commit"]
                 and row["decision_contract_sha256"] == edge["current_contract_sha256"]
                 and type(row["tick"]) is int and row["tick"] >= 0
                 and type(row["stalled_decisions"]) is int and row["stalled_decisions"] >= 0
                 and type(row["reason"]) is str)
        _digest(row["checkpoint_sha256"])
        expected = {key: row[key] for key in _HISTORY - {"kind"}}
        expected["kind"] = "blocked_decision_reevaluation_consumed"
        _require(_canonical(event) == _canonical(expected))
        matches = [i for i, item in enumerate(ledger)
                   if item.get("authorization_id") == row["authorization_id"]]
        _require(len(matches) == 1 and _canonical(ledger[matches[0]]) == _canonical(row))
        positions.append(matches[0])
        retained = [item for item in memory.history
                    if item.get("kind") == event["kind"]
                    and item.get("source_head") == event["source_head"]]
        if retained_history:
            decoded_history_bytes += _retained_history_evidence(
                edge['history_evidence'], edge, body, record['owner_invocation'])
            _require(decoded_history_bytes <= 8 * 1024 * 1024)
        _require((not require_live_history or len(retained) == 1 or retained_history)
                 and len(retained) <= 1
                 and all(_canonical(item) == _canonical(event) for item in retained))
        ids.add(row["authorization_id"])
        sources.add(new["commit"])
        prior, contract = new, edge["current_contract_sha256"]
    _require(positions == sorted(set(positions))
             and _canonical(prior) == _canonical(body["current_source"])
             and contract == body["current_contract_sha256"])
    signature = _decode(witness["signature_base64"], 16384)
    signers = _decode(witness["signers_base64"], 32768)
    _verify_signature(raw, signature, signers)
    return body
