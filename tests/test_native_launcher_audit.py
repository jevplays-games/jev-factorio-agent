"""Offline regression for the native118/126 producer-consumer field mismatch."""
import json

import pytest

from jev_factorio.native_launcher_audit import UnsupportedContract, audit, main


CONSUMER = '''
class Maintenance:
    def reconcile(self):
        require(result['script_sha256'] == SCRIPT_SHA and
                result['prepared_sha256'] == self.launched['prepared_sha256'], 'crosslink')
        require(result['exit_code'] in (-2, 130), 'interrupted')
'''
BROKEN = '''
raise RuntimeError('Auditing must never execute this source')
def launch():
    durable(RESULT, {'script_sha256': script_sha, 'prepared_sha256': prepared_sha, 'exit_code': code})
# The deployed script shadows a correct historical definition with this one.
def launch():
    durable(RESULT, {'prepared_sha256': prepared_sha, 'exit_code': code})
    durable(RESULT, {'error_class': type(error).__name__})
'''


def test_deployed_shape_uses_effective_definition_and_detects_missing_script_identity():
    report = audit(BROKEN, CONSUMER, consumer='Maintenance.reconcile')
    assert report['compatible'] is False
    assert report['terminal_receipts'][0]['missing_fields'] == ['script_sha256']
    assert report['launch_authorized'] is False
    assert len(report['nonterminal_receipts']) == 1


def test_corrected_producer_satisfies_consumer_without_treating_error_receipt_as_exit():
    fixed = BROKEN.replace("{'prepared_sha256':", "{'script_sha256': script_sha, 'prepared_sha256':")
    report = audit(fixed, CONSUMER, consumer='Maintenance.reconcile')
    assert report['compatible'] is True
    assert report['launch_authorized'] is False
    assert report['nonterminal_receipts'][0]['fields'] == ['error_class']


@pytest.mark.parametrize('writer', [
    "def launch():\n durable(RESULT, receipt)",
    "def launch():\n durable(RESULT, {'exit_code': code, **extra})",
    "def launch():\n durable(RESULT, {'error_class': 'unknown'})",
])
def test_unrecognized_producer_never_passes(writer):
    with pytest.raises(UnsupportedContract):
        audit(writer, CONSUMER, consumer='Maintenance.reconcile')


def test_new_required_consumer_field_is_not_silently_ignored():
    changed = CONSUMER + "\n        require(result['session_id'] == SESSION, 'session')\n"
    report = audit(BROKEN, changed, consumer='Maintenance.reconcile')
    assert report['terminal_receipts'][0]['missing_fields'] == ['script_sha256', 'session_id']


def test_dynamic_lookup_is_unsupported():
    with pytest.raises(UnsupportedContract):
        audit(BROKEN, "def reconcile():\n return result[field]", consumer='reconcile')


def test_cli_is_read_only_and_reports_incompatibility(tmp_path, capsys):
    launcher, maintenance = tmp_path/'launcher.py', tmp_path/'maintenance.py'
    launcher.write_text(BROKEN)
    maintenance.write_text(CONSUMER)
    before = (launcher.read_bytes(), maintenance.read_bytes())
    assert main(['--launcher', str(launcher), '--maintenance', str(maintenance),
                 '--consumer', 'Maintenance.reconcile']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['terminal_receipts'][0]['missing_fields'] == ['script_sha256']
    assert before == (launcher.read_bytes(), maintenance.read_bytes())


def test_cli_does_not_leak_syntax_error_source(tmp_path, capsys):
    invalid = tmp_path/'invalid.py'
    invalid.write_text('private_value = "do-not-print-this')
    assert main(['--launcher', str(invalid), '--maintenance', str(invalid), '--consumer', 'x']) == 2
    output = capsys.readouterr().out
    assert 'do-not-print-this' not in output
    assert json.loads(output)['status'] == 'unsupported_or_unreadable'
