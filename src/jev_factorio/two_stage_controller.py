"""Integration with the campaign's single-owner checkpoint and attempt ledger."""
from copy import deepcopy

from .skills import Plan
from . import two_stage_decision as protocol


def archive_previous(controller):
    previous = controller.memory.two_stage_decision
    if previous is None:
        return
    if previous["phase"] != "settled":
        raise ValueError("Unresolved two-stage decision cannot be replaced")
    from .operational_safety import atomic_json, read_json
    path = (controller._safety.directory / "two-stage-decisions" /
            (protocol.digest(previous) + ".json"))
    existing = read_json(path)
    if existing is not None:
        if protocol.encoded(existing) != protocol.encoded(previous):
            raise ValueError("Retained two-stage decision archive changed")
    else:
        atomic_json(path, previous)


def prepare(controller, snapshot, state, plans, context, questions, offered,
            metadata, input_sha256, *, source_authorized, authorization_reason):
    archive_previous(controller)
    binding = {
        "session_id": snapshot.session_id, "target": controller.target,
        "source_revision": deepcopy(controller.provenance["code_revision"]),
        "input_sha256": input_sha256, "state_sha256": metadata["state_sha256"],
        "frontier_sha256": metadata["frontier_sha256"],
        "native_sha256": protocol.native_digest(snapshot),
        "confidence_floor": controller.confidence_floor,
        "max_request_bytes": controller.max_request_bytes,
    }
    record = protocol.prepare(
        binding=binding, context=context, questions=questions, offered=offered,
        input_candidate_ids=[p.id for p in plans],
        answer_quantum=getattr(controller.jev, "answer_quantum", 0))
    # One durable checkpoint contains both the pending outer attempt and its
    # exact prepared first phase. No file-only marker grants a provider call.
    controller._record_persistent_attempt(
        snapshot, input_sha256, source_authorized=source_authorized,
        authorization_reason=authorization_reason, selection_batch=metadata, save=False)
    controller.memory.two_stage_decision = record
    from .planner_fault_recovery import REASON as PLANNER_FAULT
    if source_authorized and controller.memory.reason == PLANNER_FAULT:
        # The retained proof and consumed admission preserve the original
        # fault. This same commit begins a resumable decision workflow; it is
        # not evidence of useful gameplay and grants no extra provider call.
        controller.memory.status, controller.memory.reason = "running", ""
    controller._save()


def pending(controller):
    record = controller.memory.two_stage_decision
    if record is None:
        return False
    from .blocked_persistence import find_attempt, selection_batch_metadata, decision_input_sha256
    binding = record["binding"]
    attempt = find_attempt(
        controller.memory, binding["source_revision"], binding["input_sha256"],
        archive_index=controller._blocked_recovery_archive_index)
    if attempt is None:
        raise ValueError("Two-stage decision lost its durable outer attempt")
    prepared = record["prepared"]
    tick = prepared["context"]["facts"]["tick"]
    metadata = selection_batch_metadata(
        prepared["context"], prepared["questions"], prepared["plans"],
        state_sha256=binding["state_sha256"], frontier_sha256=binding["frontier_sha256"],
        current_tick=tick)
    input_sha256 = decision_input_sha256(
        prepared["context"], prepared["plans"], session_id=binding["session_id"],
        source_revision=binding["source_revision"], target=binding["target"], policy="jev",
        confidence_floor=binding["confidence_floor"], current_tick=tick,
        questions=prepared["questions"], selection_batch=metadata)
    if (attempt.get("selection_batch") != metadata or input_sha256 != binding["input_sha256"]):
        raise ValueError("Two-stage request differs from its durable outer attempt")
    if attempt["outcome"] != "pending":
        return False
    if binding["source_revision"] != controller.provenance["code_revision"]:
        raise ValueError("Pending two-stage decision requires its original source")
    if (binding["confidence_floor"] != controller.confidence_floor
            or binding["max_request_bytes"] != controller.max_request_bytes):
        raise ValueError("Pending two-stage decision configuration changed")
    return True


def advance(controller):
    record = controller.memory.two_stage_decision

    def commit(value):
        controller.memory.two_stage_decision = deepcopy(value)
        controller._save()
        controller._trace.emit("two_stage_phase", {
            "protocol": protocol.PROTOCOL, "phase": value["phase"],
            "outcome": value["outcome"], "record_sha256": protocol.digest(value),
            "decision_input_sha256": value["binding"]["input_sha256"],
        })

    def fresh():
        snapshot = controller._observe()
        if (controller._execution_barrier(snapshot)
                or controller.memory.status in {"uncertain", "completed"}
                or controller.memory.pending is not None
                or controller.memory.attempt is not None
                or controller.memory.transfer_recovery is not None):
            # An execution barrier is not stale evidence and must not be
            # converted to an ordinary recoverable selection rejection.
            raise ValueError("Native execution barrier during two-stage selection")
        return protocol.native_digest(snapshot)

    decision = protocol.advance(record, controller._trace.client(controller.jev),
                                commit=commit, fresh_native_digest=fresh)
    controller._decision = decision
    plans = [Plan.from_dict(p) for p in record["prepared"]["plans"]]
    chosen = next((p for p in plans if p.id == decision.plan_id), None)
    return {"decision": decision, "chosen": chosen,
            "input_sha256": record["binding"]["input_sha256"],
            "finalized": False, "trace_done": False}
