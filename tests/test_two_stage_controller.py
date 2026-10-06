"""Exercise the actual checkpoint, campaign ledger and dispatch integration."""
from copy import deepcopy

import pytest

from jev_factorio import blocked_persistence as persistence
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.memory import CampaignMemory
from jev_factorio.skills import Plan, Step
from test_blocked_persistence import LiveMockBackend, SOURCE
from test_two_stage_decision import Client


class LiveClient(Client):
    is_mock = False
    uses_http_provider = False

    def evaluate(self, state, questions):
        result = super().evaluate(state, questions)
        for key, answer in result.items():
            if key.endswith("/useful_progress"):
                answer.update(choice="useful", probabilities={
                    label: float(label == "useful") for label in questions[key]["criteria"]})
            elif key == "candidate":
                answer.update(choice="walk-coal", probabilities={
                    label: float(label == "walk-coal") for label in questions[key]["criteria"]})
        return result


def campaign(tmp_path, monkeypatch, *, client=None, backend=None, resume=False):
    import jev_factorio.controller as controller
    monkeypatch.setattr(controller, "gameplay_context", lambda: {"code_revision": SOURCE})
    backend = backend or LiveMockBackend()
    checkpoint = tmp_path / "checkpoint.json"
    if not resume:
        memory = CampaignMemory(
            backend.session_id, "bootstrap_mining", active_goal="bootstrap_mining",
            last_tick=0, status="blocked", reason="low choice confidence",
            stalled_decisions=5, failures={"old-plan": 2},
            history=[{"kind": "retained", "marker": "original history"}])
        persistence.record_attempt(memory, SOURCE, "a" * 64, memory.reason, 0)
        memory.save(checkpoint)
    loop = HierarchicalLoop(
        backend, jev=client or LiveClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        persist_recoverable_blocks=True, two_stage_decisions=True)
    loop._safety.admission = lambda *_args: None
    plans = [Plan("walk-iron", "bootstrap_mining", "Reach observed iron",
                  (Step("walk_to_iron", "near", "iron-ore"),)),
             Plan("walk-coal", "bootstrap_mining", "Reach observed coal",
                  (Step("walk_to_coal", "near", "coal"),))]
    loop._work_candidates = lambda _snapshot: (plans, "")
    return loop, backend, checkpoint


def test_actual_controller_selects_second_jev_choice_and_preserves_history(tmp_path, monkeypatch):
    client = LiveClient()
    loop, backend, checkpoint = campaign(tmp_path, monkeypatch, client=client)
    result = loop.step()
    assert len(client.calls) == 2
    assert backend.actions and backend.actions[0] == "walk_to_coal"
    assert loop.memory.failures == {"old-plan": 2}
    assert loop.memory.history[0] == {"kind": "retained", "marker": "original history"}
    saved = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    assert saved.two_stage_decision["outcome"] == "selected"
    assert saved.blocked_recovery["attempts"][-1]["outcome"] == "selected"


def test_rejection_is_not_rerolled_after_restart_or_tick_churn(tmp_path, monkeypatch):
    client = LiveClient(confidence=.37)
    loop, backend, checkpoint = campaign(tmp_path, monkeypatch, client=client)
    loop.step()
    assert len(client.calls) == 2 and backend.actions == []
    original = deepcopy(loop.memory.two_stage_decision)
    backend.tick += 100
    resumed_client = LiveClient()
    resumed, _, _ = campaign(tmp_path, monkeypatch, client=resumed_client,
                             backend=backend, resume=True)
    result = resumed.step()
    assert resumed_client.calls == [] and backend.actions == []
    assert result["persistent_recovery"]["phase"] == "alternatives_exhausted_waiting"
    from jev_factorio.two_stage_decision import encoded
    assert encoded(resumed.memory.two_stage_decision) == encoded(original)


@pytest.mark.parametrize("phase,expected_calls", [
    ("assessment_ready", 2), ("assessment_pending", 0),
    ("assessment_received", 1), ("choice_ready", 1),
    ("choice_pending", 0), ("choice_received", 0), ("settled", 0),
])
def test_real_checkpoint_recovers_saved_phases_without_replay(
        tmp_path, monkeypatch, phase, expected_calls):
    loop, backend, checkpoint = campaign(tmp_path, monkeypatch)
    save = loop._save

    class PowerLoss(BaseException):
        pass

    def crash():
        save()
        record = loop.memory.two_stage_decision
        if record and record["phase"] == phase:
            raise PowerLoss()

    monkeypatch.setattr(loop, "_save", crash)
    with pytest.raises(PowerLoss):
        loop.step()
    assert backend.actions == []
    client = LiveClient()
    resumed, _, _ = campaign(tmp_path, monkeypatch, client=client, backend=backend, resume=True)
    resumed.step()
    assert len(client.calls) == expected_calls
    if phase.endswith("_pending"):
        assert backend.actions == []
        assert resumed.memory.two_stage_decision["phase"] == phase
    else:
        assert backend.actions[0] == "walk_to_coal"


@pytest.mark.parametrize("phase", ["assessment", "choice"])
def test_timeout_stays_blocked_after_restart_and_changed_native_state(tmp_path, monkeypatch, phase):
    loop, backend, _ = campaign(tmp_path, monkeypatch, client=LiveClient(timeout=phase))
    loop.step()
    client = LiveClient()
    backend.inv["iron-ore"] = 3
    resumed, _, _ = campaign(tmp_path, monkeypatch, client=client, backend=backend, resume=True)
    resumed.step()
    assert client.calls == [] and backend.actions == []
    assert resumed.memory.two_stage_decision["phase"] == phase + "_pending"


def test_saved_phase_cannot_be_spliced_into_a_different_outer_batch(tmp_path, monkeypatch):
    from jev_factorio.two_stage_decision import digest
    from jev_factorio.two_stage_controller import pending
    loop, _, _ = campaign(tmp_path, monkeypatch, client=LiveClient(confidence=.37))
    loop.step()
    record = loop.memory.two_stage_decision
    record['binding']['state_sha256'] = 'f' * 64
    record['prepared_sha256'] = digest({'binding': record['binding'], 'prepared': record['prepared']})
    with pytest.raises(ValueError, match='durable outer attempt'):
        pending(loop)


@pytest.mark.parametrize('value', ['true', 1, None])
def test_configuration_rejects_non_boolean_two_stage_mode(value):
    from dataclasses import asdict
    from jev_factorio.research_log import RunConfiguration, _configuration
    config = asdict(RunConfiguration(backend='mock', controller='hierarchical', policy='jev',
                                    target='bootstrap_mining', mock_model=True, steps=8))
    config['two_stage_decisions'] = value
    with pytest.raises(ValueError, match='flag'):
        _configuration(config)


def test_cli_rejects_combined_protocols_before_provider_or_backend(monkeypatch, capsys):
    import sys
    from jev_factorio import main, jev_client

    def forbidden(*args, **kwargs):
        pytest.fail("Combined protocols acquired a provider or backend")

    monkeypatch.setattr(main, "make_backend", forbidden)
    monkeypatch.setattr(jev_client, "make_client", forbidden)
    monkeypatch.setattr(jev_client, "make_async_client", forbidden)
    monkeypatch.setattr(sys, "argv", ["jev-factorio", "--two-stage-decisions",
                                      "--async-decisions"])
    with pytest.raises(SystemExit) as error:
        main.cli()
    assert error.value.code == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_legacy_configuration_is_readable_but_new_mode_requires_persistence():
    from dataclasses import asdict
    from jev_factorio.research_log import RunConfiguration, _configuration
    config = asdict(RunConfiguration(backend='mock', controller='hierarchical', policy='jev',
                                    target='bootstrap_mining', mock_model=True, steps=8))
    config.pop('two_stage_decisions')
    _configuration(config)
    config['two_stage_decisions'] = True
    with pytest.raises(ValueError, match='persistent strict JEV'):
        _configuration(config)


def test_absent_two_stage_record_preserves_historical_scope_digest():
    from dataclasses import asdict
    from jev_factorio.compatible_recovery import scope
    from jev_factorio.provenance import digest_json
    memory = CampaignMemory('retained-session', 'bootstrap_mining')
    old = asdict(memory)
    old.pop('two_stage_decision')
    old.pop('planner_fault_recovery')
    assert scope(memory)['state_sha256'] == digest_json(old)
    memory.two_stage_decision = {'present': 'must remain bound'}
    current = asdict(memory)
    current.pop('planner_fault_recovery')
    assert scope(memory)['state_sha256'] == digest_json(current)


def test_optional_protocol_modules_preserve_old_contract_and_bind_new_source(tmp_path):
    from test_blocked_reevaluation import _source_repo, _git
    from jev_factorio.blocked_reevaluation import validate_source_revision
    old, _, _, _ = _source_repo(tmp_path)
    (tmp_path / 'README.md').write_text('Unrelated documentation\n')
    _git(tmp_path, 'add', '.')
    _git(tmp_path, 'commit', '-qm', 'documentation only')
    old_contract = validate_source_revision(old, tmp_path, require_changed_contract=False)
    module = tmp_path / 'src/jev_factorio/two_stage_decision.py'
    module.write_bytes(b'PROTOCOL = "test-new-protocol"\n')
    _git(tmp_path, 'add', '.')
    _git(tmp_path, 'commit', '-qm', 'add two-stage protocol')
    new_contract = validate_source_revision(old, tmp_path)
    assert old_contract['decision_contract_sha256'] == new_contract['previous_contract_sha256']
    assert new_contract['decision_contract_sha256'] != new_contract['previous_contract_sha256']
    _git(tmp_path, 'update-index', '--assume-unchanged', str(module.relative_to(tmp_path)))
    module.write_bytes(b'PROTOCOL = "unreviewed-change"\n')
    with pytest.raises(ValueError, match='differs from its HEAD blob'):
        validate_source_revision(old, tmp_path)
