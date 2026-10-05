"""Economic deferral covers carried construction and payment boundaries.

These are composed-controller doubles, not positive native economic evidence.
"""
from copy import deepcopy
from dataclasses import asdict

import pytest

from jev_factorio import coal_supply
from jev_factorio.planning import coal_admission, coal_funding
from jev_factorio.planning.coal_supply import candidates
from test_coal_kit_funding import Backend, controller, offers


def set_economic_protocol(backend):
    data = backend.state.factory["coal_supply"]
    data["protocol"] = 2
    data["admission"] = {
        "protocol": 1,
        **{key: data[key] for key in (
            "session_id", "tick", "actor_index", "surface_index", "force_index")},
        "qualified": False,
        "reason": "electric_conversion_and_construction_cost_unknown",
    }
    advance = backend.advance

    def advance_with_bound_admission():
        advance()
        current = backend.state.factory["coal_supply"]
        current["admission"].update({
            key: current[key]
            for key in ("session_id", "tick", "actor_index", "surface_index", "force_index")
        })

    backend.advance = advance_with_bound_admission


def carried_backend(version):
    backend = Backend()
    if version == 2:
        set_economic_protocol(backend)
    backend.state.inventory.update(
        coal_supply.remaining_kit(coal_supply.sources(backend.state), backend.state))
    return backend


def allow_synthetic_selection(loop, patch):
    """Seed controller-boundary fixtures without faking native v7 evidence."""
    patch.setattr(loop, '_coal_admission_allows_start', lambda _snapshot: True)


def test_unqualified_carried_kit_never_starts_or_pays(tmp_path):
    # Economic-admission treatment is protocol 2. Protocol 1 is covered by
    # the explicit cross-wiring rejection controls in test_coal_protocol_binding.
    backend = carried_backend(2)
    stock = deepcopy(backend.state.inventory)
    loop = controller(backend, tmp_path, coal_economic_admission=True)
    _, plans = offers(loop)
    assert not plans
    assert loop._coal_admission_evidence["eligible"] is False
    loop.step()
    assert not backend.calls
    assert backend.state.inventory == stock
    assert not loop.memory.coal_commitments
    assert loop.memory.coal_funding is None
    assert loop.memory.pending is None


def test_unqualified_direct_build_fails_commit_and_dispatch_boundaries(tmp_path):
    backend = carried_backend(2)
    loop = controller(backend, tmp_path, coal_economic_admission=True)
    snapshot = loop._observe()
    # The raw geometry planner is deliberately independent of controller policy.
    plan = candidates(snapshot, loop.memory.active_goal)[0]
    before = asdict(loop.memory)
    assert not loop._step_allowed(plan.steps[0], snapshot)
    assert not loop._investment_step_allowed(plan, plan.steps[0], snapshot)
    with pytest.raises(ValueError, match="economic admission"):
        loop._commit_solid(plan, snapshot)
    assert asdict(loop.memory) == before
    assert not backend.calls


def test_unoffered_kit_cannot_create_funding_at_commit(tmp_path):
    backend = carried_backend(2)
    backend.state.inventory['electric-mining-drill'] = 0
    backend.state.factory['entities']['kit:storage'] = {
        'unit_number': 6000, 'name': 'wooden-chest', 'position': {'x': 2, 'y': 2},
        'output': {'electric-mining-drill': 2}}
    loop = controller(backend, tmp_path, coal_economic_admission=True)
    snapshot = loop._observe()
    plan, _ = coal_funding.candidate(snapshot, loop.catalog, **loop._coal_funding_options())
    assert plan is not None
    before = asdict(loop.memory)
    with pytest.raises(ValueError, match="economic admission"):
        loop._commit_solid(plan, snapshot)
    assert asdict(loop.memory) == before
    assert loop.memory.coal_funding is None and not backend.calls


def test_legacy_carried_policy_still_builds(tmp_path):
    backend = carried_backend(1)
    loop = controller(backend, tmp_path)
    record = loop.step()
    assert record['action'] == coal_supply.COMMAND and record['verified']
    assert len(backend.calls) == 1


def test_changed_economic_observation_rechecked_after_selection(monkeypatch, tmp_path):
    backend = carried_backend(2)
    loop = controller(backend, tmp_path, coal_economic_admission=True)
    # The composed-controller double has no qualified native profile. Inject
    # only the selection boundary so this test can verify the fresh-snapshot
    # recheck; the public typed evaluator remains untouched and still defers.
    with monkeypatch.context() as patch:
        allow_synthetic_selection(loop, patch)
        snapshot, plans = offers(loop)
        plan = next(plan for plan in plans if plan.steps[0].action == coal_supply.COMMAND)
        loop._commit_solid(plan, snapshot)
    fresh = loop._observe()
    assert not loop._step_allowed(plan.steps[0], fresh)
    assert not loop._investment_step_allowed(plan, plan.steps[0], fresh)
    assert loop._coal_admission_evidence['eligible'] is False
    assert not backend.calls and not loop.memory.coal_commitments


@pytest.mark.parametrize('recovery', ['paid', 'prepared', 'lost_ack'])
def test_retained_network_continues_after_economics_defers(recovery, monkeypatch, tmp_path):
    backend = carried_backend(2)
    backend.prepared_once = recovery == 'prepared'
    backend.lost_ack = recovery == 'lost_ack'
    loop = controller(backend, tmp_path, coal_economic_admission=True)
    # Seed a controller transaction with the fixture-only selection seam. The
    # production same-RPC first-payment check is separately covered by the Lua
    # contract tests; this double does not claim positive native economics.
    with monkeypatch.context() as patch:
        allow_synthetic_selection(loop, patch)
        first = loop.step()
    assert first['verified'] is (recovery == 'paid')
    assert len(backend.calls) == 1
    assert coal_admission.evaluate(backend.state)['eligible'] is False
    pending = deepcopy(loop.memory.pending)
    failures = deepcopy(loop.memory.failures)
    backend.lost_ack = False
    resumed = controller(backend, tmp_path, resume=True, coal_economic_admission=True)
    result = resumed.step()
    assert result['verified'], result
    assert len(backend.calls) == (1 if recovery == 'lost_ack' else 2)
    if recovery == 'prepared':
        assert backend.calls[0] == backend.calls[1]
        assert pending is not None
    assert resumed.memory.failures == failures
    assert resumed.memory.pending is None
    assert resumed.memory.coal_commitments


def test_prepared_retry_exception_is_bound_to_checkpoint_and_native_receipt(monkeypatch, tmp_path):
    backend = carried_backend(2)
    backend.prepared_once = True
    loop = controller(backend, tmp_path, coal_economic_admission=True)
    with monkeypatch.context() as patch:
        allow_synthetic_selection(loop, patch)
        first = loop.step()
    assert not first['verified'] and loop.memory.pending['dispatch'] == 'ambiguous'

    resumed = controller(backend, tmp_path, resume=True, coal_economic_admission=True)
    snapshot = resumed._observe()
    from jev_factorio.skills import Plan
    step = Plan.from_dict(resumed.memory.active_plan).steps[resumed.memory.step_index]
    assert resumed._coal_prepared_first_payment_retry(step, snapshot)

    changed = deepcopy(snapshot)
    row = changed.factory['coal_supply']['sources'][step.parameters['target']]
    row['pending']['receipt'] = 'not-the-checkpoint-receipt'
    assert not resumed._coal_prepared_first_payment_retry(step, changed)
    assert len(backend.calls) == 1


def test_paid_funding_continues_when_new_admission_defers(monkeypatch, tmp_path):
    backend = Backend()
    set_economic_protocol(backend)
    loop = controller(backend, tmp_path, coal_economic_admission=True)
    with monkeypatch.context() as patch:
        allow_synthetic_selection(loop, patch)
        assert loop.step()['verified']
    assert len(backend.calls) == 1 and loop.memory.coal_funding is not None
    assert coal_admission.evaluate(backend.state)['eligible'] is False
    resumed = controller(backend, tmp_path, resume=True, coal_economic_admission=True)
    record = resumed.step()
    assert record['verified'] and record['action'] == coal_supply.COMMAND
    assert len(backend.calls) == 2
    assert resumed.memory.coal_funding is None and resumed.memory.coal_commitments
