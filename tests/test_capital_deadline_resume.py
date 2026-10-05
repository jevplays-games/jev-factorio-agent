from copy import deepcopy
import json

import pytest

from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.memory import load_checkpoint
from jev_factorio import capital_controller
from jev_factorio.planning import capital
from test_capital_investments import (
    Backend, CraftBackend, make_loop, offer, scenario, seed_commit,
)


@pytest.mark.parametrize(("offset", "should_dispatch"), [(-1, True), (0, False), (1, False)])
def test_retained_capital_offer_rechecks_deadline_after_checkpoint_resume(
        tmp_path, offset, should_dispatch):
    data, state = scenario()
    plan = offer(data, state)
    backend = Backend(data, state)
    path = tmp_path / "capital.json"
    loop = make_loop(backend, path)
    intent = seed_commit(loop, state, plan)
    loop.memory.failures["unrelated-plan"] = 1
    loop.memory.event("deadline_fixture_history", tick=state.tick, marker="preserve")
    loop.memory.active_plan = plan.to_dict()
    loop.memory.step_index = 0
    loop._save()

    target_tick = intent["deadline_tick"] + offset
    backend.advance(ticks=target_tick - state.tick)
    loop.memory.last_tick = state.tick
    loop._save()
    before = load_checkpoint(path, state.session_id, "rocket_launch")
    assert before.capital_investment == intent
    assert before.active_plan == json.loads(json.dumps(plan.to_dict()))
    before_inventory = deepcopy(state.inventory)
    before_entities = deepcopy(state.factory["entities"])
    before_receipts = deepcopy(state.factory.get("receipts", {}))
    before_history = deepcopy(before.history)
    before_outcomes = deepcopy(before.attempt_outcomes)
    before_failures = deepcopy(before.failures)

    resumed = make_loop(backend, path, resume=True)
    result = resumed.step()

    if should_dispatch:
        assert result["verified"] and result["action"] == "factory_craft"
        assert [action for action, _ in backend.calls] == ["factory_craft"]
        assert resumed.memory.capital_investment == intent
        assert state.inventory != before_inventory
        return

    assert backend.calls == []
    assert result["action"] == "observe" and not result["verified"]
    assert resumed.memory.capital_investment is None
    assert resumed.memory.active_plan is None
    expected_failures = dict(before_failures)
    expected_failures[intent["spec"]["key"]] = 2
    expected_failures[plan.id] = 1
    assert resumed.memory.failures == expected_failures
    assert state.inventory == before_inventory
    assert state.factory["entities"] == before_entities
    assert state.factory.get("receipts", {}) == before_receipts
    assert resumed.memory.attempt_outcomes == before_outcomes
    assert resumed.memory.history[:len(before_history)] == before_history
    abandoned = [entry for entry in resumed.memory.history[len(before_history):]
                 if entry["kind"] == "capital_abandoned"]
    assert len(abandoned) == 1
    assert abandoned[0]["reason"] == "bounded_investment_deadline"
    assert load_checkpoint(path, state.session_id, "rocket_launch").capital_investment is None

    # A repeated composed reload/reconciliation keeps the exhausted budget and
    # does not replay the old paid plan or append another abandonment receipt.
    reloaded = make_loop(backend, path, resume=True)
    observed = reloaded._observe()
    plans, _ = reloaded._work_candidates(observed)
    assert backend.calls == []
    assert reloaded.memory.failures[intent["spec"]["key"]] == 2
    assert not any(capital.MARKER in (candidate.materials or {})
                   and candidate.materials[capital.MARKER]["spec"]["key"] == intent["spec"]["key"]
                   for candidate in plans)
    assert sum(entry["kind"] == "capital_abandoned" for entry in reloaded.memory.history) == 1


class DeadlineCrossingBackend(Backend):
    def __init__(self, data, state, deadline_tick):
        super().__init__(data, state)
        self.deadline_tick = deadline_tick
        self.observation_count = 0

    def observe(self):
        self.observation_count += 1
        if self.observation_count == 2:
            self.advance(ticks=self.deadline_tick - self.state.tick)
        return deepcopy(self.state)


def test_fresh_pre_dispatch_observation_rechecks_committed_deadline():
    data, state = scenario()
    plan = offer(data, state)
    deadline_tick = state.tick + min(
        capital.MAX_INVESTMENT_TICKS,
        max(7200, plan.materials[capital.MARKER]["spec"]["investment_ticks"] * 4),
    )
    backend = DeadlineCrossingBackend(data, state, deadline_tick)
    loop = make_loop(backend)
    intent = seed_commit(loop, state, plan)
    assert intent["deadline_tick"] == deadline_tick
    loop.memory.active_plan = plan.to_dict()
    loop.memory.step_index = 0
    loop._save()
    before_inventory = deepcopy(state.inventory)

    result = loop.step()

    assert backend.observation_count == 2 and state.tick == deadline_tick
    assert backend.calls == []
    assert result["action"] == "observe" and not result["verified"]
    assert loop.memory.capital_investment is None and loop.memory.active_plan is None
    assert loop.memory.failures[intent["spec"]["key"]] == 2
    assert state.inventory == before_inventory
    assert any(entry["kind"] == "capital_abandoned"
               and entry["reason"] == "bounded_investment_deadline"
               for entry in loop.memory.history)


class CraftAckLostBackend(Backend):
    def execute(self, action, parameters):
        result = super().execute(action, parameters)
        if action == "factory_craft":
            raise TimeoutError("synthetic lost craft acknowledgement")
        return result


def test_expired_checkpoint_reconciles_real_pending_attempt_before_expiry(tmp_path):
    data, state = scenario()
    plan = offer(data, state)
    backend = CraftAckLostBackend(data, state)
    path = tmp_path / "pending-capital.json"
    loop = make_loop(backend, path)
    intent = seed_commit(loop, state, plan)
    loop.memory.active_plan = plan.to_dict()
    loop.memory.step_index = 0
    loop._save()

    ambiguous = loop.step()
    assert not ambiguous["verified"]
    assert loop.memory.pending["dispatch"] == "ambiguous"
    assert loop.memory.attempt is not None
    assert len(backend.calls) == 1
    backend.advance(ticks=intent["deadline_tick"] - state.tick)
    loop.memory.last_tick = state.tick
    loop._save()
    saved = load_checkpoint(path, state.session_id, "rocket_launch")
    assert saved.pending["dispatch"] == "ambiguous" and saved.attempt is not None
    before_outcomes = deepcopy(saved.attempt_outcomes)

    resumed = make_loop(backend, path, resume=True)
    reconciled = resumed.step()

    assert reconciled["verified"]
    assert resumed.memory.pending is None and resumed.memory.attempt is None
    assert resumed.memory.capital_investment == intent
    assert resumed.memory.attempt_outcomes[:-1] == before_outcomes
    finished = resumed.memory.attempt_outcomes[-1]
    assert {key: finished[key] for key in saved.attempt} == saved.attempt
    assert finished["outcome"] == "verified" and finished["finished_tick"] == state.tick
    assert [action for action, _ in backend.calls] == ["factory_craft"]
    assert load_checkpoint(path, state.session_id, "rocket_launch").capital_investment == intent


def test_deadline_gate_preserves_composed_background_receipt_until_observed(tmp_path):
    data, state = scenario()
    backend = CraftBackend(data, state)
    path = tmp_path / "background-capital.json"

    def setup(resume=False):
        loop = make_loop(backend, path, resume=resume, kind=BackgroundWorkLoop)
        loop._compile_candidates = lambda snapshot: ([loop._tracked_plan(offer(data, snapshot), snapshot)], "")
        return loop

    loop = setup()
    first = loop.step()
    assert not first["verified"]
    assert loop.memory.background_job is not None
    assert loop.memory.background_attempt is not None
    assert loop.memory.background_step is not None
    intent = deepcopy(loop.memory.capital_investment)
    job = deepcopy(loop.memory.background_job)
    attempt = deepcopy(loop.memory.background_attempt)
    outcomes = deepcopy(loop.memory.attempt_outcomes)

    # This is a valid shortened deadline inside the current capital-state schema;
    # the independently paid background receipt remains in flight past it.
    intent_deadline = state.tick + 2
    loop.memory.capital_investment["deadline_tick"] = intent_deadline
    loop._save()
    loaded = load_checkpoint(path, state.session_id, "rocket_launch")
    assert loaded.background_job == job and loaded.background_attempt == attempt
    assert loaded.capital_investment["deadline_tick"] == intent_deadline

    backend.advance(ticks=intent_deadline - state.tick)
    observed = loop._observe()

    assert observed.tick == intent_deadline
    assert loop.memory.capital_investment["deadline_tick"] == intent_deadline
    assert loop.memory.background_job == job
    assert loop.memory.background_attempt == attempt
    assert loop.memory.background_step is not None
    assert loop.memory.attempt_outcomes == outcomes
    assert len(backend.calls) == 1
    after = load_checkpoint(path, state.session_id, "rocket_launch")
    assert after.capital_investment["deadline_tick"] == intent_deadline
    assert after.background_job == job and after.background_attempt == attempt


def test_idle_frontier_expires_once_after_deadline_without_touching_paid_entity():
    data, state = scenario()
    loop = make_loop(Backend(data, state))
    plan = offer(data, state)
    intent = seed_commit(loop, state, plan)
    state.tick = intent["deadline_tick"]
    loop.memory.last_tick = state.tick
    entity = deepcopy(state.factory["entities"]["recipe:automation-science-pack"])
    prior_history = deepcopy(loop.memory.history)

    loop._work_candidates(state)

    assert loop.memory.capital_investment is None
    assert loop.memory.failures[intent["spec"]["key"]] == 2
    assert state.factory["entities"]["recipe:automation-science-pack"] == entity
    assert loop.memory.history[:len(prior_history)] == prior_history
    assert [entry["reason"] for entry in loop.memory.history[len(prior_history):]
            if entry["kind"] == "capital_abandoned"] == ["bounded_investment_deadline"]


@pytest.mark.parametrize('persistent', [False, True])
def test_idle_policy_hold_observes_capital_deadline_without_reset_or_dispatch(persistent):
    data, state = scenario()
    backend = Backend(data, state)
    loop = make_loop(backend)
    plan = offer(data, state)
    intent = deepcopy(seed_commit(loop, state, plan))
    loop.persist_recoverable_blocks = persistent
    loop.memory.status = 'blocked'
    loop.memory.reason = 'Candidate evidence insufficient'
    loop.memory.failures['unrelated-plan'] = 1
    before_history = deepcopy(loop.memory.history)
    before_outcomes = deepcopy(loop.memory.attempt_outcomes)
    loop._compile_candidates = lambda snapshot: ([offer(data, snapshot)], '')
    backend.advance(ticks=intent['deadline_tick'] - state.tick)
    plans, _ = capital_controller.frontier(loop, state)
    assert backend.calls == []
    assert loop.memory.status == 'blocked'
    assert loop.memory.reason == 'Candidate evidence insufficient'
    assert loop.memory.attempt_outcomes == before_outcomes
    assert loop.memory.history[:len(before_history)] == before_history
    assert loop.memory.failures['unrelated-plan'] == 1
    if not persistent:
        assert loop.memory.capital_investment == intent
        assert loop.memory.history == before_history
        return
    assert loop.memory.capital_investment is None
    assert loop.memory.failures[intent['spec']['key']] == 2
    assert not any((p.materials or {}).get(capital.MARKER, {}).get('spec', {}).get('key')
                   == intent['spec']['key'] for p in plans)
    assert sum(e['kind'] == 'capital_abandoned' for e in loop.memory.history) == 1
    capital_controller.frontier(loop, state)
    assert sum(e['kind'] == 'capital_abandoned' for e in loop.memory.history) == 1


def test_inconsistent_background_hold_cannot_abandon_capital():
    data, state = scenario()
    backend = Backend(data, state)
    loop = make_loop(backend)
    intent = deepcopy(seed_commit(loop, state, offer(data, state)))
    loop.persist_recoverable_blocks = True
    loop.memory.status = 'blocked'
    loop.memory.reason = 'Candidate evidence insufficient'
    loop.memory.background_job = {'id': 'unresolved-native-work'}
    loop.memory.background_attempt = None
    loop._compile_candidates = lambda snapshot: ([offer(data, snapshot)], '')
    backend.advance(ticks=intent['deadline_tick'] - state.tick)
    capital_controller.frontier(loop, state)
    assert loop.memory.capital_investment == intent
    assert backend.calls == []
