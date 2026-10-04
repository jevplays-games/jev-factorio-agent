import json
import subprocess
import sys
from copy import deepcopy
from collections import Counter
from pathlib import Path

import pytest

from jev_factorio.diagnostics import captured_snapshot, reconciliation_report
from attempt_helpers import ReceiptBackend, controller, install_plan


@pytest.mark.parametrize("variant,expected", [
    ("missing", "missing_receipt_unresolved"), ("partial", "partial_receipt_unresolved"),
    ("full", "full_matching_receipt_in_capture"), ("wrong_entity", "mismatched_receipt_unresolved"),
    ("wrong_direction", "mismatched_receipt_unresolved"), ("wrong_item", "mismatched_receipt_unresolved"),
    ("stale", "stale_observation"),
])
def test_captured_receipts_never_authorize_replay(monkeypatch, tmp_path, variant, expected):
    backend = ReceiptBackend("delayed")
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    loop.step()
    path = tmp_path / "checkpoint.json"
    before = path.read_bytes()
    if variant != "missing":
        backend.publish()
        receipt = next(iter(backend.state.factory["receipts"].values()))
        if variant == "partial":
            receipt["quantity"] = 10
        elif variant == "wrong_entity":
            receipt["unit_number"] = 8
        elif variant == "wrong_direction":
            receipt["extracting"] = True
        elif variant == "wrong_item":
            receipt["item"] = "other"
        elif variant == "stale":
            backend.state.tick = 0
    report = reconciliation_report(loop.memory, captured_snapshot(backend.state.for_jev()))
    assert report["assessment"] == expected
    assert report["read_only"] and report["replay_authorized"] is False
    assert report["evidence_class"] == "synthetic"
    assert path.read_bytes() == before and len(backend.calls) == 1
    assert loop.memory.pending is not None


def test_snapshot_session_mismatch_is_not_reconciled(monkeypatch, tmp_path):
    backend = ReceiptBackend("delayed")
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    loop.step()
    backend.state.session_id = "other"
    with pytest.raises(ValueError, match="session"):
        reconciliation_report(loop.memory, backend.state)


def test_diagnostic_cli_is_offline_read_only_and_reports_saved_log(monkeypatch, tmp_path):
    backend = ReceiptBackend("delayed")
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    loop.step()
    path = tmp_path / "checkpoint.json"
    before = path.read_bytes()
    command = [sys.executable, "-m", "jev_factorio.diagnostics", str(path),
               "--session-id", "receipt-session", "--target", "bootstrap_mining",
               "--log", str(tmp_path / "run.jsonl")]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["assessment"] == "missing_receipt_unresolved"
    assert before == path.read_bytes()
    bad = subprocess.run([*command[:-2], "--snapshot", str(tmp_path / "absent")],
                         capture_output=True, text=True, timeout=10)
    assert bad.returncode == 2 and "no files or game state were changed" in bad.stderr


def test_diagnostic_imports_do_not_load_backend_modules():
    code = "import jev_factorio.diagnostics, sys; assert not any(k.startswith('jev_factorio.backends') or k == 'fle' or k.startswith('fle.') for k in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_background_step_survives_checkpoint_capture_and_diagnostics_but_row_projection_is_stable(tmp_path):
    from jev_factorio.acceptance_capture import project_record
    from jev_factorio.dev_preflight import checkpoint_read
    from jev_factorio.research_log import Redactor
    from test_background_work import ReceiptBackend as BackgroundReceiptBackend
    from test_background_work import controller as background_controller

    backend = BackgroundReceiptBackend()
    loop = background_controller(backend, tmp_path)
    record = loop.step()
    expected_step = deepcopy(loop.memory.background_step)
    assert expected_step is not None and loop.memory.background_schema == 3

    report = reconciliation_report(loop.memory)
    assert report["background_step"] == expected_step
    captured, _ = checkpoint_read(Path(loop.checkpoint), idle=False)
    assert captured["background_step"] == expected_step

    # Acceptance gameplay rows are a bounded projection; the private full
    # checkpoint is the authority that retains this exact step.
    projected_input = {**record, "background_step": expected_step}
    omissions = Counter()
    projected = project_record(projected_input, Redactor({}), omissions)
    assert "background_step" not in projected
    assert omissions["top:background_step"] == 1


@pytest.mark.parametrize("field,value", [("tick", True), ("factory", []), ("inventory", None)])
def test_incomplete_or_malformed_capture_rejected(field, value):
    raw = ReceiptBackend().state.for_jev()
    raw[field] = value
    with pytest.raises(ValueError):
        captured_snapshot(raw)
