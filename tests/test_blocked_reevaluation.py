from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from jev_factorio.backends.mock import MockBackend
from jev_factorio.blocked_reevaluation import (
    validate_blocked_memory,
    validate_checkpoint_capture,
    validate_source_revision,
    _CONTRACT_PATHS,
)
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.judgments import Decision
from jev_factorio.memory import CampaignMemory
from jev_factorio.skills import Plan, Step


OLD_SOURCE = "1" * 40
SOURCE = {
    "blocked_source_revision": OLD_SOURCE,
    "source_head": "2" * 40,
    "previous_contract_sha256": "a" * 64,
    "decision_contract_sha256": "b" * 64,
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", f"safe.directory={root}", *args], cwd=root,
                          check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True).stdout.strip()


def _source_repo(root: Path) -> tuple[str, Path, Path, Path]:
    _git(root, "init", "--quiet", "-b", "main")
    _git(root, "config", "user.name", "Blocked re-evaluation tests")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    (root / ".gitattributes").write_text("*.py text eol=crlf\n", encoding="ascii")
    judgments = root / "src" / "jev_factorio" / "judgments.py"
    support = root / "src" / "jev_factorio" / "planning" / "decision_support.py"
    planner = root / "src" / "jev_factorio" / "planning" / "mining_outposts.py"
    judgments.parent.mkdir(parents=True)
    support.parent.mkdir(parents=True)
    planner.parent.mkdir(parents=True, exist_ok=True)
    judgments.write_bytes(b"def decision():\n    return 'old'\n")
    support.write_bytes(b"def support():\n    return 'old'\n")
    planner.write_bytes(b"def candidates():\n    return ['old']\n")
    for name in _CONTRACT_PATHS:
        path = root / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"def decision_dependency():\n    return 'old'\n")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "initial")
    return _git(root, "rev-parse", "HEAD"), judgments, support, planner


def test_source_contract_is_blob_bound_and_cosmetic_commits_do_not_reauthorize(tmp_path):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    old, judgments, support, _planner = _source_repo(root)
    # Exercise the supported CRLF working-tree form even when Git's local
    # checkout configuration leaves the initial commit's files untouched.
    judgments.write_bytes(judgments.read_bytes().replace(b"\n", b"\r\n"))
    _git(root, "add", str(judgments.relative_to(root)))
    assert _git(root, "status", "--porcelain") == ""
    (root / "README.md").write_text("cosmetic change\n", encoding="ascii")
    _git(root, "add", "README.md")
    _git(root, "commit", "--quiet", "-m", "unrelated cosmetic change")
    assert b"\r\n" in judgments.read_bytes()
    assert _git(root, "status", "--porcelain") == ""

    with pytest.raises(ValueError, match="contract has not changed"):
        validate_source_revision(old, root)

    support.write_bytes(b"def support():\n    return 'new question evidence'\n")
    _git(root, "add", str(support.relative_to(root)))
    _git(root, "commit", "--quiet", "-m", "change decision contract")
    result = validate_source_revision(old, root)
    assert result["blocked_source_revision"] == old
    assert result["source_head"] == _git(root, "rev-parse", "HEAD")
    assert result["decision_contract_sha256"] != result["previous_contract_sha256"]


def test_source_contract_detects_assume_unchanged_working_file(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    old, judgments, _, _planner = _source_repo(root)
    (root / "README.md").write_text("source change\n", encoding="ascii")
    _git(root, "add", "README.md")
    _git(root, "commit", "--quiet", "-m", "source change")
    _git(root, "update-index", "--assume-unchanged", "src/jev_factorio/judgments.py")
    judgments.write_bytes(b"def decision():\n    return 'untracked runtime behavior'\n")
    assert _git(root, "status", "--porcelain") == ""
    with pytest.raises(ValueError, match="working file differs"):
        validate_source_revision(old, root)


def test_candidate_planner_change_is_part_of_decision_contract(tmp_path):
    root = tmp_path / "planner-checkout"
    root.mkdir()
    old, _judgments, _support, planner = _source_repo(root)
    planner.write_bytes(b"def candidates():\n    return ['new decision-relevant candidate']\n")
    _git(root, "add", str(planner.relative_to(root)))
    _git(root, "commit", "--quiet", "-m", "change candidate planning contract")

    result = validate_source_revision(old, root)

    assert result["blocked_source_revision"] == old
    assert result["source_head"] == _git(root, "rev-parse", "HEAD")
    assert result["decision_contract_sha256"] != result["previous_contract_sha256"]


class FLEMockBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.inv["coal"] = 5
        self.actions = []
        self.observations = 0

    def observe(self):
        self.observations += 1
        return replace(super().observe(), world_kind="fle")

    def act(self, action):
        self.actions.append(action)
        return super().act(action)


class LiveSelectionClient:
    model = "offline-test-double"
    is_mock = False
    uses_http_provider = False


def _checkpoint(path: Path, backend: FLEMockBackend, *, stalled=4,
                reason="Candidate evidence insufficient"):
    memory = CampaignMemory(
        backend.session_id, "bootstrap_mining", active_goal="bootstrap_mining",
        completed_goals={"stockpile_fuel": 0}, last_tick=0,
        status="blocked", reason=reason,
        stalled_decisions=stalled, failures={"preserved-failure": 2},
        history=[{"kind": "preserved", "marker": "old history"}],
    )
    memory.save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest(), memory


def _make_loop(tmp_path, monkeypatch, *, selection, max_stalled_decisions=4,
               blocked_reason="Candidate evidence insufficient", persistent=False,
               stalled=4, prior_ledger_source=None):
    from jev_factorio import blocked_reevaluation
    import jev_factorio.controller as controller

    monkeypatch.setattr(blocked_reevaluation, "validate_source_revision", lambda _revision: dict(SOURCE))
    revision = {"commit": "2" * 40, "source_sha256": "c" * 64}
    monkeypatch.setattr(controller, "gameplay_context",
                        (lambda: {"code_revision": revision}) if persistent else (lambda: {}))
    backend = FLEMockBackend()
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint_sha, original = _checkpoint(checkpoint, backend, reason=blocked_reason,
                                           stalled=stalled)
    if prior_ledger_source is not None:
        from jev_factorio import blocked_persistence as persistence
        memory = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
        reason = memory.reason if persistence.is_recoverable_reason(memory.reason) else None
        persistence.record_attempt(memory, prior_ledger_source, "e" * 64, reason, 0)
        persistence.finish_attempt(memory, prior_ledger_source, "e" * 64, "rejected", reason)
        memory.save(checkpoint)
        checkpoint_sha = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    loop = HierarchicalLoop(
        backend, jev=LiveSelectionClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0,
        max_stalled_decisions=max_stalled_decisions,
        reevaluate_blocked_once=True, exact_checkpoint_sha256=checkpoint_sha,
        blocked_source_revision=OLD_SOURCE,
        **({"persist_recoverable_blocks": True, "persistent_idle_observations": 0}
           if persistent else {}),
    )
    if loop._safety is not None:
        loop._safety.admission = lambda _memory, _snapshot: None
        loop._safety.before_dispatch = lambda _session: None
    loop._work_candidates = lambda snapshot: (selection(snapshot), "")
    return backend, checkpoint, checkpoint_sha, original, loop


def _bootstrap_plan(snapshot):
    if snapshot.nearby_resources.get("iron-ore", 999) > 0.5:
        step = Step("walk_to_iron", "near", "iron-ore")
        plan_id = "bootstrap:walk-to-iron"
    elif not any(entity.startswith("burner-mining-drill") for entity in snapshot.placed_entities):
        step = Step("place_burner_drill", "drill_with_output", threshold=1,
                    costs={"burner-mining-drill": 1, "wooden-chest": 1})
        plan_id = "bootstrap:place-drill"
    elif snapshot.drill_fuel <= 0:
        step = Step("fuel_drill", "drill_fueled", costs={"coal": 5})
        plan_id = "bootstrap:fuel-drill"
    else:
        step = Step("idle", "output", threshold=snapshot.iron_ore_collected + 2)
        plan_id = f"bootstrap:wait-{snapshot.iron_ore_collected + 2}"
    return Plan(plan_id, "bootstrap_mining", plan_id, (step,))


def test_boiler_planner_error_is_not_an_automatic_retry_reason(tmp_path, monkeypatch):
    from jev_factorio.blocked_persistence import is_recoverable_reason, validate_memory_state
    reason = 'Current native boiler identity and coal stock are required'
    prior = {'commit': '1' * 40, 'source_sha256': 'b' * 64}
    backend, checkpoint, _, _, loop = _make_loop(
        tmp_path, monkeypatch, selection=lambda snapshot: [_bootstrap_plan(snapshot)],
        blocked_reason=reason, persistent=True, stalled=0, prior_ledger_source=prior)
    assert not is_recoverable_reason(reason)
    memory = CampaignMemory.load(checkpoint, backend.session_id, 'bootstrap_mining')
    with pytest.raises(ValueError, match='does not admit'):
        validate_memory_state(memory, prior)
    validate_memory_state(memory, loop.provenance['code_revision'], allow_source_change=True)


@pytest.mark.parametrize("blocked_reason", [
    "Candidate evidence insufficient", "low choice confidence",
    "Current native boiler identity and coal stock are required",
])
def test_selected_recheck_keeps_counter_until_receipt_and_continues_unbounded_run(
        tmp_path, monkeypatch, blocked_reason):
    backend, checkpoint, _sha, _original, loop = _make_loop(
        tmp_path, monkeypatch, selection=lambda snapshot: [_bootstrap_plan(snapshot)],
        blocked_reason=blocked_reason)
    from jev_factorio import controller

    selected = []

    def choose(_client, _state, plans, *_args):
        selected.append(plans[0].id)
        return Decision(plans[0].id, "jev", model_called=True,
                        diagnostics={"schema": 1, "outcome": "selected"})

    monkeypatch.setattr(controller, "select_plan", choose)
    saves = []
    save = loop._save

    def capture_save():
        save()
        saves.append((loop.memory.status, loop.memory.stalled_decisions,
                      loop.memory.active_plan is not None, loop.memory.pending is not None))

    loop._save = capture_save
    loop.run(until_complete=True)

    assert len(selected) > 1  # Authorization is one-use; execution remains the normal full loop.
    assert backend.actions == ["walk_to_iron", "place_burner_drill", "fuel_drill",
                              "idle", "idle", "idle"]
    assert any(status == "running" and count == 4 and active and not pending
               for status, count, active, pending in saves)
    assert any(status == "running" and count == 4 and active and pending
               for status, count, active, pending in saves)
    assert loop.memory.stalled_decisions == 0  # The ordinary verified action receipt reset it.
    assert loop.memory.status == "completed" and loop.terminal
    assert len(loop.memory.blocked_reevaluations) == 1
    assert loop.memory.blocked_reevaluations[0]["stalled_decisions"] == 4
    assert loop.memory.failures == {"preserved-failure": 2}
    assert loop.memory.history[0] == {"kind": "preserved", "marker": "old history"}


@pytest.mark.parametrize("blocked_reason", [
    "Candidate evidence insufficient", "low choice confidence",
])
def test_rejection_increments_existing_streak_and_same_contract_cannot_replay(
        tmp_path, monkeypatch, blocked_reason):
    backend, checkpoint, _sha, original, loop = _make_loop(
        tmp_path, monkeypatch, selection=lambda snapshot: [_bootstrap_plan(snapshot)],
        blocked_reason=blocked_reason)
    from jev_factorio import controller

    calls = []

    def reject(*_args):
        calls.append(True)
        return Decision(None, "observe", "Candidate evidence insufficient",
                        model_called=True,
                        diagnostics={"schema": 1, "outcome": "all_candidates_rejected"})

    monkeypatch.setattr(controller, "select_plan", reject)
    record = loop.step()
    assert record["status"] == "blocked"
    assert loop.memory.status == "blocked" and loop.memory.stalled_decisions == 5
    assert loop.memory.failures == original.failures
    assert loop.memory.history[0] == original.history[0]
    assert loop.memory.blocked_reevaluations[0]["stalled_decisions"] == 4
    assert loop.memory.blocked_reevaluations[0]["reason"] == blocked_reason
    assert backend.actions == [] and calls == [True]

    retry_backend = FLEMockBackend()
    with pytest.raises(ValueError, match="already consumed"):
        HierarchicalLoop(
            retry_backend, jev=LiveSelectionClient(), policy="jev", target="bootstrap_mining",
            checkpoint=str(checkpoint), resume_controller=True,
            reevaluate_blocked_once=True,
            exact_checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            blocked_source_revision=OLD_SOURCE,
        )
    assert retry_backend.observations == 0
    assert calls == [True]


def test_crash_after_durable_authorization_cannot_repeat_same_contract(tmp_path, monkeypatch):
    backend, checkpoint, _sha, _original, loop = _make_loop(
        tmp_path, monkeypatch, selection=lambda snapshot: [_bootstrap_plan(snapshot)])
    from jev_factorio import controller

    calls = []

    def crash_after_authorization(*_args):
        calls.append(True)
        raise RuntimeError("simulated process loss after durable authorization")

    monkeypatch.setattr(controller, "select_plan", crash_after_authorization)
    with pytest.raises(RuntimeError, match="simulated process loss"):
        loop.step()
    saved = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    assert saved.status == "blocked" and saved.stalled_decisions == 4
    assert saved.blocked_reevaluations[0]["state"] == "consumed"
    assert saved.failures == {"preserved-failure": 2}
    assert saved.history[0] == {"kind": "preserved", "marker": "old history"}
    assert backend.actions == []

    retry_backend = FLEMockBackend()
    with pytest.raises(ValueError, match="already consumed"):
        HierarchicalLoop(
            retry_backend, jev=LiveSelectionClient(), policy="jev", target="bootstrap_mining",
            checkpoint=str(checkpoint), resume_controller=True,
            reevaluate_blocked_once=True,
            exact_checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            blocked_source_revision=OLD_SOURCE,
        )
    assert retry_backend.observations == 0 and calls == [True]


def test_crash_after_selected_plan_commit_resumes_without_reselection(tmp_path, monkeypatch):
    backend, checkpoint, _sha, _original, loop = _make_loop(
        tmp_path, monkeypatch, selection=lambda snapshot: [_bootstrap_plan(snapshot)])
    from jev_factorio import controller

    selected = []

    def choose(_client, _state, plans, *_args):
        selected.append(plans[0].id)
        return Decision(plans[0].id, "jev", model_called=True,
                        diagnostics={"schema": 1, "outcome": "selected"})

    monkeypatch.setattr(controller, "select_plan", choose)
    observe = loop._observe

    def crash_before_dispatch(stage="observe"):
        if stage == "pre_dispatch_observe":
            raise RuntimeError("simulated crash after saved plan commit")
        return observe(stage)

    loop._observe = crash_before_dispatch
    with pytest.raises(RuntimeError, match="after saved plan commit"):
        loop.step()

    saved = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    assert saved.status == "running" and saved.reason == ""
    assert saved.active_plan is not None and saved.step_index == 0 and saved.pending is None
    assert saved.stalled_decisions == 4
    assert saved.blocked_reevaluations[0]["state"] == "consumed"
    assert saved.failures == {"preserved-failure": 2}
    assert selected == [saved.active_plan["id"]]

    resumed_backend = FLEMockBackend()
    resumed_backend.session_id = backend.session_id
    resumed = HierarchicalLoop(
        resumed_backend, jev=LiveSelectionClient(), policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), resume_controller=True,
    )
    if resumed._safety is not None:
        resumed._safety.admission = lambda _memory, _snapshot: None
        resumed._safety.before_dispatch = lambda _session: None
    write_ahead = []
    save = resumed._save

    def capture_write_ahead():
        save()
        if resumed.memory.pending is not None:
            write_ahead.append((resumed.memory.status, resumed.memory.stalled_decisions,
                                resumed.memory.active_plan["id"]))

    resumed._save = capture_write_ahead
    monkeypatch.setattr(controller, "select_plan", lambda *_args: pytest.fail("plan reselected"))
    resumed.step()

    assert selected == [saved.active_plan["id"]]
    assert resumed_backend.actions == ["walk_to_iron"]
    assert resumed.memory.blocked_reevaluations == saved.blocked_reevaluations
    assert ("running", 4, saved.active_plan["id"]) in write_ahead
    assert resumed.memory.stalled_decisions == 0  # The ordinary verified receipt resets the streak.


def test_authorization_save_failure_preserves_checkpoint_and_never_selects(tmp_path, monkeypatch):
    backend, checkpoint, original_sha, original, loop = _make_loop(
        tmp_path, monkeypatch, selection=lambda snapshot: [_bootstrap_plan(snapshot)])
    from jev_factorio import controller

    monkeypatch.setattr(controller, "select_plan", lambda *_args: pytest.fail("model selection called"))
    original_bytes = checkpoint.read_bytes()
    loop.memory  # The checkpoint restore still occurs only during ordinary observation.

    def failed_save(_path):
        raise OSError("simulated atomic checkpoint failure")

    # Fail only the authorization save after first observation has loaded memory.
    original_observe = loop._observe
    def observe_then_fail(stage="observe"):
        snapshot = original_observe(stage)
        if loop.memory is not None:
            loop.memory.save = failed_save
        return snapshot
    loop._observe = observe_then_fail
    with pytest.raises(OSError, match="simulated atomic checkpoint failure"):
        loop.step()
    assert checkpoint.read_bytes() == original_bytes
    assert hashlib.sha256(original_bytes).hexdigest() == original_sha
    assert loop.memory.stalled_decisions == original.stalled_decisions
    assert loop.memory.failures == original.failures
    assert loop.memory.history == original.history
    assert loop.memory.blocked_reevaluations == []
    assert backend.actions == []


def test_quiescent_gate_rejects_pending_and_checkpoint_hash_mismatch(tmp_path):
    backend = FLEMockBackend()
    path = tmp_path / "state.json"
    sha, memory = _checkpoint(path, backend)
    memory.pending = {"action": "mine_iron"}
    with pytest.raises(ValueError, match="quiescent"):
        validate_blocked_memory(memory, 4)

    captured = path.read_bytes()
    with pytest.raises(ValueError, match="differs from the authorized"):
        validate_checkpoint_capture(captured, "0" * 64, CampaignMemory,
                                   "bootstrap_mining", 4)
    assert hashlib.sha256(captured).hexdigest() == sha


def test_persistent_block_below_the_threshold_and_a_tracked_job_are_eligible(tmp_path):
    backend = FLEMockBackend()
    path = tmp_path / "state.json"
    _sha, memory = _checkpoint(path, backend, stalled=1)
    # Ordinary (non-persistent) blocks still need the stalled-decision threshold.
    with pytest.raises(ValueError, match="quiescent eligible blocked decision"):
        validate_blocked_memory(memory, 4)
    memory.blocked_recovery = {"schema": 1, "attempts": [{"outcome": "rejected"}]}
    validate_blocked_memory(memory, 4)  # persistent ledger: terminal at the first frontier
    memory.stalled_decisions = 0  # verified native work reset the counter
    validate_blocked_memory(memory, 4)
    memory.blocked_recovery = {"schema": 1, "attempts": []}
    with pytest.raises(ValueError, match="quiescent eligible blocked decision"):
        validate_blocked_memory(memory, 4)

    memory.blocked_recovery = {"schema": 1, "attempts": [{"outcome": "rejected"}]}
    memory.background_job, memory.background_attempt = {"receipt": "r"}, {"id": "a"}
    validate_blocked_memory(memory, 4)  # consistent tracked craft job
    for job, attempt in (({"receipt": "r"}, None), (None, {"id": "a"})):
        memory.background_job, memory.background_attempt = job, attempt
        with pytest.raises(ValueError, match="quiescent eligible blocked decision"):
            validate_blocked_memory(memory, 4)
    memory.background_job = memory.background_attempt = None
    memory.pending = {"action": "mine_iron"}
    with pytest.raises(ValueError, match="quiescent eligible blocked decision"):
        validate_blocked_memory(memory, 4)


@pytest.mark.parametrize("reason", ["model abstention", "provider circuit unavailable",
                                     "low benefit confidence"])
def test_re_evaluation_rejects_other_terminal_reasons(reason, tmp_path):
    backend = FLEMockBackend()
    path = tmp_path / "state.json"
    _sha, memory = _checkpoint(path, backend)
    memory.reason = reason
    with pytest.raises(ValueError, match="quiescent eligible blocked decision"):
        validate_blocked_memory(memory, 4)


@pytest.mark.parametrize("gate", ["admission", "provider"])
def test_operational_denial_does_not_consume_re_evaluation(gate, tmp_path, monkeypatch):
    backend, _checkpoint_path, _sha, _original, loop = _make_loop(
        tmp_path, monkeypatch, selection=lambda snapshot: [_bootstrap_plan(snapshot)])
    from jev_factorio import controller

    monkeypatch.setattr(controller, "select_plan", lambda *_args: pytest.fail("model selection called"))
    if gate == "admission":
        loop._safety.admission = lambda _memory, _snapshot: "maintenance admission is closed"
    else:
        from jev_factorio.provider_health import ProviderCircuit
        loop.jev = ProviderCircuit(LiveSelectionClient())
        loop.jev.state["phase"] = "exhausted"

    loop.step()

    assert loop.memory.status == "blocked"
    assert loop.memory.blocked_reevaluations == []
    assert loop._reevaluate_blocked_once is True
    assert backend.actions == []


@pytest.mark.parametrize("persistent", [False, True])
def test_authorized_reevaluation_is_consumed_by_a_model_free_passive_wait(
        tmp_path, monkeypatch, persistent):
    from jev_factorio import controller, judgments

    wait = Plan("background-wait:1:factory_craft_job:iron-gear-wheel", "bootstrap_mining",
                "Observe the tracked native crafting queue",
                (Step("factory_wait", "crafting_idle", timeout_ticks=60),))
    backend, checkpoint, _sha, _original, loop = _make_loop(
        tmp_path, monkeypatch, selection=lambda _snapshot: [wait], persistent=persistent)
    monkeypatch.setattr(controller, "select_plan", judgments.select_plan)

    loop.step()

    assert loop._decision.model_called is False and loop._decision.plan_id == wait.id
    assert loop.memory.status == "running" and loop._reevaluate_blocked_once is False
    saved = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    assert [e["state"] for e in saved.blocked_reevaluations] == ["consumed"]
    assert any(e.get("kind") == "plan_committed" and e["source"] == "passive-wait"
               for e in saved.history)
    # The authorization is spent and the checkpoint is no longer blocked.
    with pytest.raises(ValueError, match="already consumed|quiescent eligible blocked"):
        HierarchicalLoop(
            FLEMockBackend(), jev=LiveSelectionClient(), policy="jev",
            target="bootstrap_mining", checkpoint=str(checkpoint), resume_controller=True,
            reevaluate_blocked_once=True,
            exact_checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            blocked_source_revision=OLD_SOURCE)


@pytest.mark.parametrize('stalled', [0, 1])
@pytest.mark.parametrize('blocked_reason', [
    'Candidate evidence insufficient', 'Current native boiler identity and coal stock are required'])
def test_persistent_block_below_threshold_is_reevaluated_and_commits_a_passive_wait(
        tmp_path, monkeypatch, stalled, blocked_reason):
    """Persisted blocks remain valid after a verified craft resets the streak."""
    from jev_factorio import controller, judgments

    wait = Plan("background-wait:1:factory_craft_job:pipe", "bootstrap_mining",
                "Observe the tracked native crafting queue",
                (Step("factory_wait", "crafting_idle", timeout_ticks=60),))
    prior = {"commit": "1" * 40, "source_sha256": "b" * 64}
    backend, checkpoint, _sha, _original, loop = _make_loop(
        tmp_path, monkeypatch, selection=lambda _snapshot: [wait], persistent=True,
        stalled=stalled, prior_ledger_source=prior, blocked_reason=blocked_reason)
    monkeypatch.setattr(controller, "select_plan", judgments.select_plan)

    loop.step()

    assert loop._decision.model_called is False and loop._decision.plan_id == wait.id
    assert loop.memory.status == "running" and loop._reevaluate_blocked_once is False
    saved = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    assert [e["state"] for e in saved.blocked_reevaluations] == ["consumed"]
    assert saved.blocked_reevaluations[0]['stalled_decisions'] == stalled
    assert saved.blocked_recovery["source_revision"]["commit"] == "2" * 40
    assert saved.blocked_recovery["attempts"][-1]["outcome"] == "selected"
    consumed = saved.blocked_reevaluations
    saved.save(checkpoint)
    assert CampaignMemory.load(checkpoint, backend.session_id,
                               'bootstrap_mining').blocked_reevaluations == consumed
    # The live campaign loads through every composed checkpoint extension.
    # Exercise the public loader, retaining a paid buffer owner and history.
    from jev_factorio.background import BackgroundWorkLoop
    from jev_factorio.buffer_controller import buffered_loop_type
    from jev_factorio.input_controller import input_loop_type
    from jev_factorio.outpost_controller import outpost_loop_type
    from jev_factorio.solid_controller import solid_loop_type
    from jev_factorio.coal_controller import coal_loop_type
    from jev_factorio.memory import load_checkpoint
    full_loop = coal_loop_type(solid_loop_type(outpost_loop_type(
        input_loop_type(buffered_loop_type(BackgroundWorkLoop)))))
    full = full_loop.memory_type(**asdict(saved))
    full.solid_epoch = {'actor_index': 1, 'surface_index': 1, 'force_index': 1}
    full.coal_epoch = dict(full.solid_epoch)
    full.coal_targets = ['utility:boiler', 'recipe:copper-plate']
    full.solid_intents = [{'source': 'coal:' + target + ':chest',
                          'target': target, 'item': 'coal', 'destination': 'fuel'}
                         for target in full.coal_targets]
    full.output_commitments = {'recipe:iron-plate': {
        'source_unit': 44, 'layout': 'output:44:test', 'parts': {'chest': {
            'role': 'output-chest:44', 'unit_number': 45,
            'receipt': 'paid-chest-receipt', 'paid': 1}}}}
    full_path = tmp_path / 'composed-checkpoint.json'
    full.save(full_path)
    reopened = load_checkpoint(full_path, backend.session_id, 'bootstrap_mining')
    assert asdict(reopened) == asdict(full)
    reopened.save(full_path)
    assert asdict(load_checkpoint(full_path, backend.session_id,
                                  'bootstrap_mining')) == asdict(full)
    for invalid_count in (-1, True, False, '0', 0.0):
        invalid = json.loads(checkpoint.read_bytes())
        invalid['blocked_reevaluations'][0]['stalled_decisions'] = invalid_count
        with pytest.raises(ValueError, match='Invalid blocked-decision re-evaluation ledger entry'):
            CampaignMemory.from_bytes(json.dumps(invalid).encode(),
                                      backend.session_id, 'bootstrap_mining')
    duplicate = json.loads(checkpoint.read_bytes())
    duplicate['blocked_reevaluations'].append(dict(duplicate['blocked_reevaluations'][0]))
    with pytest.raises(ValueError, match='Decision contract was already re-evaluated'):
        CampaignMemory.from_bytes(json.dumps(duplicate).encode(),
                                  backend.session_id, 'bootstrap_mining')


@pytest.mark.parametrize("relative", [name for name in _CONTRACT_PATHS
    if name not in {"src/jev_factorio/judgments.py",
                    "src/jev_factorio/planning/decision_support.py",
                    "src/jev_factorio/planning/mining_outposts.py"}])
def test_composed_planner_dependency_change_changes_contract(tmp_path, relative):
    root = tmp_path / "planner-dependency"
    root.mkdir()
    old, *_ = _source_repo(root)
    path = root / relative
    path.write_bytes(b"def decision_dependency():\n    return 'new candidate behavior'\n")
    _git(root, "add", relative)
    _git(root, "commit", "--quiet", "-m", "change planner dependency")
    result = validate_source_revision(old, root)
    assert result["decision_contract_sha256"] != result["previous_contract_sha256"]


def test_input_route_change_allows_fresh_contract_once_and_keeps_old_ledger(tmp_path):
    root = tmp_path / "input-route-contract"
    root.mkdir()
    old, *_ = _source_repo(root)
    route = root / "src/jev_factorio/planning/input_routes.py"
    route.write_bytes(b"def candidates():\n    return ['kit', 'manual-current-science']\n")
    _git(root, "add", str(route.relative_to(root)))
    _git(root, "commit", "--quiet", "-m", "offer proposed route manual frontier")
    contract = validate_source_revision(old, root)
    memory = CampaignMemory("contract-test", "rocket_launch", last_tick=1, status="blocked",
        reason="Candidate evidence insufficient", stalled_decisions=4)
    entry = {"schema": 1, "authorization_id": "1" * 32,
        "blocked_source_revision": old, "source_head": contract["source_head"],
        "decision_contract_sha256": contract["previous_contract_sha256"],
        "checkpoint_sha256": "a" * 64, "stalled_decisions": 4,
        "reason": memory.reason, "tick": 1, "state": "consumed"}
    memory.blocked_reevaluations = [entry]
    checkpoint = tmp_path / "blocked.json"
    memory.save(checkpoint)
    raw = checkpoint.read_bytes()
    admitted = validate_checkpoint_capture(raw, hashlib.sha256(raw).hexdigest(),
        CampaignMemory, "rocket_launch", decision_contract_sha256=contract["decision_contract_sha256"])
    assert admitted.blocked_reevaluations == [entry]
    admitted.blocked_reevaluations.append({**entry, "authorization_id": "2" * 32,
        "decision_contract_sha256": contract["decision_contract_sha256"]})
    admitted.save(checkpoint)
    raw = checkpoint.read_bytes()
    with pytest.raises(ValueError, match="already consumed"):
        validate_checkpoint_capture(raw, hashlib.sha256(raw).hexdigest(), CampaignMemory,
            "rocket_launch", decision_contract_sha256=contract["decision_contract_sha256"])
    assert CampaignMemory.load(checkpoint, "contract-test", "rocket_launch").blocked_reevaluations == admitted.blocked_reevaluations
    (root / "tests").mkdir()
    (root / "tests/test_cosmetic.py").write_text("# unrelated test change\n")
    _git(root, "add", "tests/test_cosmetic.py")
    prior = _git(root, "rev-parse", "HEAD")
    _git(root, "commit", "--quiet", "-m", "tests do not change decision contract")
    with pytest.raises(ValueError, match="contract has not changed"):
        validate_source_revision(prior, root)
