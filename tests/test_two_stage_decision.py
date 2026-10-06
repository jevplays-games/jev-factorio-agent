"""Durable strict selection; synthetic tests never establish native acceptance."""
from copy import deepcopy

import pytest
import requests

from jev_factorio import two_stage_decision as protocol
from jev_factorio.backends.mock import MockBackend
from jev_factorio.jev_client import MockJevClient
from jev_factorio.judgments import question_batch
from jev_factorio.skills import Plan, Step


SOURCE = {"commit": "a" * 40, "source_sha256": "b" * 64}


def prepared():
    snapshot = MockBackend().observe()
    plans = [Plan("coal", "stockpile_fuel", "Gather five coal",
                  (Step("mine_coal", "inventory", "coal", 5),)),
             Plan("walk", "stockpile_fuel", "Reach observed coal",
                  (Step("walk_to_coal", "near", "coal", 0.5),))]
    state = {"facts": snapshot.for_jev(), "active_goal": "stockpile_fuel",
             "decision_protocol": protocol.PROTOCOL}
    context, questions, offered = question_batch(state, plans, max_bytes=48000)
    binding = {"session_id": snapshot.session_id, "target": "bootstrap_mining",
               "source_revision": SOURCE, "input_sha256": "1" * 64,
               "state_sha256": "2" * 64, "frontier_sha256": "3" * 64,
               "native_sha256": protocol.native_digest(snapshot),
               "confidence_floor": 0.45, "max_request_bytes": 48000}
    return protocol.prepare(binding=binding, context=context, questions=questions,
                            offered=offered, input_candidate_ids=[p.id for p in plans])


class Client(MockJevClient):
    def __init__(self, *, confidence=0.9, reject=None, timeout=None):
        self.calls = []
        self.confidence, self.reject, self.timeout = confidence, reject, timeout

    def evaluate(self, state, questions):
        phase = state["decision_phase"]["phase"]
        self.calls.append((deepcopy(state), deepcopy(questions)))
        if phase == self.timeout:
            raise requests.Timeout("delivery uncertain")
        result = super().evaluate(state, questions)
        if phase == "assessment":
            assert "candidate" not in questions
            if self.reject:
                result[self.reject + "/needs_observation"]["noul"] = 0.8
        else:
            assert set(questions) == {"candidate"}
            assert state["validated_assessments"]
            ids = [key for key in questions["candidate"]["criteria"] if key != "observe"]
            selected = ids[-1]
            result["candidate"].update(
                choice=selected, confidence=self.confidence,
                probabilities={key: float(key == selected)
                               for key in questions["candidate"]["criteria"]})
        return result


def run(record, client, *, native=None, commit=None):
    records = []
    result = protocol.advance(
        record, client, commit=commit or (lambda row: records.append(deepcopy(row))),
        fresh_native_digest=native or (lambda: record["binding"]["native_sha256"]))
    return result, records


def test_assessments_feed_second_choice_and_exact_jev_choice_wins():
    record, client = prepared(), Client()
    result, rows = run(record, client)
    assert len(client.calls) == 2
    assert result.plan_id == "walk"  # Not the first/ranked default candidate.
    assert result.source == "jev"
    assert rows[-1]["outcome"] == "selected"
    assert [row["phase"] for row in rows] == [
        "assessment_pending", "assessment_received", "choice_ready",
        "choice_pending", "choice_received", "settled"]
    for row in rows:
        protocol.validate(row, record["binding"]["session_id"], "bootstrap_mining")


def test_individual_rejection_is_excluded_from_choice_but_retained_as_evidence():
    client = Client(reject="walk")
    result, _ = run(prepared(), client)
    assert result.plan_id == "coal"
    state, questions = client.calls[-1]
    assert set(questions["candidate"]["criteria"]) == {"coal", "observe"}
    assert state["validated_assessments"]["walk"]["rejections"] == ["missing_start_evidence"]
    assert "walk" not in state["candidate_plans"]


@pytest.mark.parametrize("confidence", [0.24, 0.30, 0.37, 0.40, 0.44])
def test_historical_choice_confidences_still_block_without_fallback(confidence):
    result, rows = run(prepared(), Client(confidence=confidence))
    assert result.plan_id is None and result.reason == "low choice confidence"
    assert result.diagnostics["candidate_rejections"] == {}
    assert rows[-1]["outcome"] == "low_choice_confidence"


@pytest.mark.parametrize("phase,expected_calls", [
    ("assessment_pending", 0), ("assessment_received", 1),
    ("choice_ready", 1), ("choice_pending", 0),
    ("choice_received", 0), ("settled", 0),
])
def test_restart_never_resends_a_saved_or_ambiguous_phase(phase, expected_calls):
    record = prepared()
    saved = deepcopy(record)

    class PowerLoss(BaseException):
        pass

    def commit(row):
        nonlocal saved
        saved = deepcopy(row)
        if row["phase"] == phase:
            raise PowerLoss()

    with pytest.raises(PowerLoss):
        run(record, Client(), commit=commit)
    restarted_client = Client()
    result, _ = run(saved, restarted_client)
    assert len(restarted_client.calls) == expected_calls
    if phase.endswith("_pending"):
        assert result.plan_id is None
        assert result.diagnostics["outcome"] == "provider_blocked"
    else:
        assert result.plan_id == "walk"


@pytest.mark.parametrize("phase", ["assessment", "choice"])
def test_timeout_preserves_uncertain_phase_across_restart(phase):
    record, client = prepared(), Client(timeout=phase)
    result, rows = run(record, client)
    assert result.plan_id is None
    assert rows[-1]["phase"] == phase + "_pending"
    client = Client()
    result, _ = run(rows[-1], client)
    assert client.calls == [] and result.plan_id is None


@pytest.mark.parametrize("changed_at", [1, 2, 3, 4])
def test_native_changes_before_either_request_or_after_choice_prevent_action(changed_at):
    record, client = prepared(), Client()
    observations = 0

    def observe():
        nonlocal observations
        observations += 1
        return "f" * 64 if observations >= changed_at else record["binding"]["native_sha256"]

    result, rows = run(record, client, native=observe)
    assert result.plan_id is None
    assert result.diagnostics["outcome"] == "stale_evidence"
    assert len(client.calls) <= (0 if changed_at == 1 else 1 if changed_at <= 3 else 2)
    assert rows[-1]["phase"] == "settled"


def test_failed_pending_checkpoint_prevents_first_provider_request():
    client = Client()

    def commit(row):
        raise OSError("fsync failed")

    with pytest.raises(OSError):
        run(prepared(), client, commit=commit)
    assert client.calls == []


def test_invalid_choice_cannot_execute_an_assessment_rejected_candidate():
    class InvalidChoice(Client):
        def evaluate(self, state, questions):
            result = super().evaluate(state, questions)
            if "candidate" in result:
                result["candidate"]["choice"] = "walk"
            return result

    result, rows = run(prepared(), InvalidChoice(reject="walk"))
    assert result.plan_id is None
    assert rows[-1]["outcome"] == "invalid_answer"


def test_saved_assessment_mutation_cannot_change_prepared_choice():
    record = prepared()
    _, rows = run(record, Client())
    changed = deepcopy(next(row for row in rows if row["phase"] == "choice_ready"))
    changed["assessment"]["walk/needs_observation"]["noul"] = 0.8
    with pytest.raises(ValueError, match="saved assessment"):
        run(changed, Client())


def test_native_digest_ignores_tick_only_but_retains_inventory_change():
    from dataclasses import replace
    original = MockBackend().observe()
    assert protocol.native_digest(original) == protocol.native_digest(replace(original, tick=999))
    changed = replace(original, inventory={**original.inventory, "coal": 100})
    assert protocol.native_digest(original) != protocol.native_digest(changed)
