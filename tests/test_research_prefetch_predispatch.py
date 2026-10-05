"""Fresh research-demand checks for precompiled background lab transfers."""
from copy import deepcopy
from dataclasses import replace

import pytest

from jev_factorio.background import BackgroundMemory, BackgroundWorkLoop
from jev_factorio.planning.background_work import independent_candidates
from jev_factorio.skills import Plan, Step
from test_background_work import ReceiptBackend, science_catalog
from test_factory import machine, recipe


def _research_catalog():
    catalog = science_catalog()
    catalog.recipes["background-output"] = recipe(
        "background-output", {"iron-plate": 1})
    catalog.technologies["old-study"] = {
        "enabled": True, "prerequisites": [], "effects": [], "count": 100,
        "energy_ticks": 60,
        "ingredients": [{"name": "automation-science-pack", "amount": 1}],
    }
    catalog.technologies["new-disjoint-study"] = {
        "enabled": True, "prerequisites": [], "effects": [], "count": 100,
        "energy_ticks": 60,
        "ingredients": [{"name": "logistic-science-pack", "amount": 1}],
    }
    catalog.technologies["new-same-pack-study"] = {
        "enabled": True, "prerequisites": [], "effects": [], "count": 200,
        "energy_ticks": 60,
        "ingredients": [{"name": "automation-science-pack", "amount": 1}],
    }
    return catalog


class ResearchBackend(ReceiptBackend):
    def __init__(self):
        super().__init__()
        self.catalog = _research_catalog()
        self.state.inventory.update({
            "iron-plate": 10,
            "automation-science-pack": 20,
            "logistic-science-pack": 20,
            "background-output": 0,
        })
        self.state.factory.update(research="old-study", research_progress=0.0)
        self.state.factory["entities"]["utility:lab"] = machine(
            "lab", input={"automation-science-pack": 0,
                          "logistic-science-pack": 0}, research_speed=1)
        self.lose_insert_ack = False
        self.fail_next_observation = False

    def enable_factory(self):
        return self.catalog

    def observe(self):
        if self.fail_next_observation:
            self.fail_next_observation = False
            raise OSError("synthetic post-insert observation loss")
        return super().observe()

    def execute(self, action, parameters):
        self.calls.append((action, deepcopy(parameters)))
        if action == "factory_craft_job":
            batches = parameters["batches"]
            self.state.inventory["iron-plate"] -= batches
            self.state.factory.update(crafting_queue=1, craft_job={
                **self.state.factory["craft_job_actor"],
                "id": parameters["receipt"], "recipe": parameters["recipe"],
                "requested": batches, "accepted": batches, "finished": 0,
                "started_tick": self.state.tick, "last_progress_tick": self.state.tick,
                "inputs": {"iron-plate": batches},
                "outputs": {"background-output": batches},
                "baseline": {"background-output": self.state.inventory["background-output"]},
                "status": "running", "queue_valid": True, "paid": True,
            })
        elif action == "factory_insert":
            item, quantity = parameters["item"], parameters["quantity"]
            self.state.inventory[item] -= quantity
            lab = self.state.factory["entities"][parameters["role"]]
            lab["input"][item] = lab["input"].get(item, 0) + quantity
            self.state.factory["receipts"][parameters["receipt"]] = {
                "role": parameters["role"], "extracting": False,
                "unit_number": lab["unit_number"], "item": item,
                "quantity": quantity,
            }
            if self.lose_insert_ack:
                self.lose_insert_ack = False
                self.fail_next_observation = True
        elif action != "factory_wait":
            raise AssertionError(action)
        return "synthetic return"


class ResearchPrefetchLoop(BackgroundWorkLoop):
    """Use the public dispatcher and actual source prefetch compiler after job start."""

    def __init__(self, backend, *, checkpoint, resume=False):
        self.pre_dispatch_change = None
        self.generic_transfer = False
        self.last_prefetch_plan = None
        super().__init__(
            backend, policy="deterministic", factory_scheduling="ready-work",
            target="rocket_launch", checkpoint=str(checkpoint),
            resume_controller=resume, tick_seconds=0,
        )
        if not resume:
            self.memory = BackgroundMemory(
                backend.state.session_id, self.target, active_goal=self.target,
                completed_goals={goal: 0 for goal in self.order[:-1]},
                last_tick=backend.state.tick,
            )

    def _compile_candidates(self, snapshot):
        if self._job() is None:
            plan = Plan("start-background-output", self.memory.active_goal,
                        "Start the tracked background output", (
                Step("factory_craft", "inventory", "background-output", 1,
                     costs={"iron-plate": 1}, timeout_ticks=144000,
                     parameters={"recipe": "background-output", "batches": 1}),
            ))
            return [self._tracked_plan(plan, snapshot)], ""

        plans = independent_candidates(
            self.memory.active_goal, snapshot, self.catalog, self._job(), self.planner_type)
        plans = [plan for plan in plans
                 if plan.steps[0].action == "factory_insert"
                 and plan.steps[0].parameters["role"] == "utility:lab"]
        assert plans, "fixture must produce a direct current-research lab prefetch"
        plan = plans[0]
        if self.generic_transfer:
            plan = replace(plan, description="Generic lab transfer", materials=None)
        self.last_prefetch_plan = plan
        return [plan], ""

    def _observe(self, stage="observe"):
        if stage == "pre_dispatch_observe" and self.pre_dispatch_change is not None:
            change = self.pre_dispatch_change
            self.pre_dispatch_change = None
            if "research" in change:
                self.backend.state.factory["research"] = change["research"]
            if "research_progress" in change:
                self.backend.state.factory["research_progress"] = change["research_progress"]
            if "researched" in change:
                self.backend.state.researched = list(change["researched"])
            if "input" in change:
                item, quantity = change["input"]
                self.backend.state.factory["entities"]["utility:lab"]["input"][item] = quantity
        return super()._observe(stage)


def _loop(tmp_path):
    backend = ResearchBackend()
    loop = ResearchPrefetchLoop(backend, checkpoint=tmp_path / "state.json")
    started = loop.step()
    assert started["background_job"]
    assert [action for action, _ in backend.calls] == ["factory_craft_job"]
    return backend, loop


def _inserted(backend):
    return [parameters for action, parameters in backend.calls if action == "factory_insert"]


@pytest.mark.parametrize("fresh_change", [
    {"research": "new-disjoint-study"},
    {"research": ""},
    {"research": "old-study", "researched": ["old-study"]},
    {"research": "old-study", "input": ("automation-science-pack", 18)},
    {"research": "old-study", "research_progress": 0.95},
    {"research": "old-study", "input": ("automation-science-pack", 20)},
])
def test_stale_prefetch_rejects_before_mutation_and_replans_from_fresh_research(
        tmp_path, fresh_change):
    backend, loop = _loop(tmp_path)
    before_job = deepcopy(loop.memory.background_job)
    before_inventory = deepcopy(backend.state.inventory)
    loop.pre_dispatch_change = fresh_change

    first = loop.step()

    assert loop.last_prefetch_plan.description.startswith("Prefetch research supply:")
    assert first["status"] == "running"
    assert _inserted(backend) == []
    assert loop.memory.pending is None
    assert loop.memory.active_plan is None
    assert loop.memory.background_job == before_job
    assert backend.state.inventory == before_inventory
    expected_lab_stock = fresh_change.get("input", ("automation-science-pack", 0))[1]
    assert backend.state.factory["entities"]["utility:lab"]["input"][
        "automation-science-pack"] == expected_lab_stock
    assert [action for action, _ in backend.calls].count("factory_craft_job") == 1

    if fresh_change.get("research") == "new-disjoint-study":
        second = loop.step()
        assert second["status"] == "running"
        inserted = _inserted(backend)
        assert len(inserted) == 1
        assert inserted[0]["item"] == "logistic-science-pack"


@pytest.mark.parametrize("fresh_research", ["old-study", "new-same-pack-study"])
def test_prefetch_remains_valid_when_same_pack_is_still_due(
        tmp_path, fresh_research):
    backend, loop = _loop(tmp_path)
    if fresh_research != "old-study":
        loop.pre_dispatch_change = {"research": fresh_research}

    result = loop.step()

    inserted = _inserted(backend)
    assert len(inserted) == 1
    assert inserted[0]["item"] == "automation-science-pack"
    assert result["verified"] is True
    assert loop.memory.pending is None
    assert loop.memory.background_job is not None
    assert [action for action, _ in backend.calls].count("factory_craft_job") == 1


def test_unmarked_generic_lab_transfer_keeps_existing_fresh_action_semantics(tmp_path):
    backend, loop = _loop(tmp_path)
    loop.generic_transfer = True
    loop.pre_dispatch_change = {"research": "new-disjoint-study"}

    result = loop.step()

    inserted = _inserted(backend)
    assert len(inserted) == 1
    assert inserted[0]["item"] == "automation-science-pack"
    assert result["verified"] is True
    assert loop.memory.pending is None
    assert loop.memory.background_job is not None
    assert [action for action, _ in backend.calls].count("factory_craft_job") == 1


def test_legacy_named_prefetch_without_research_binding_replans_before_mutation(tmp_path):
    backend, loop = _loop(tmp_path)
    snapshot = loop._observe()
    plan = independent_candidates(
        loop.memory.active_goal, snapshot, loop.catalog, loop._job(), loop.planner_type)[0]
    materials = dict(plan.materials or {})
    materials.pop("background_research_prefetch", None)
    legacy_plan = replace(plan, materials=materials or None)
    loop.memory.active_plan = legacy_plan.to_dict()
    loop.memory.step_index = 0
    loop._save()

    rejected = loop.step()

    assert rejected["status"] == "running"
    assert _inserted(backend) == []
    assert loop.memory.active_plan is None
    assert loop.memory.pending is None
    assert loop.memory.background_job is not None
    assert [action for action, _ in backend.calls].count("factory_craft_job") == 1

    replanned = loop.step()

    assert replanned["status"] == "running"
    assert len(_inserted(backend)) == 1
    assert _inserted(backend)[0]["item"] == "automation-science-pack"
    assert replanned["verified"] is True


def test_already_dispatched_prefetch_reconciles_its_receipt_without_rewriting_or_replay(tmp_path):
    backend, loop = _loop(tmp_path)
    backend.lose_insert_ack = True

    with pytest.raises(OSError, match="synthetic post-insert observation loss"):
        loop.step()
    pending = deepcopy(loop.memory.pending)
    assert pending["dispatch"] == "returned"
    assert pending["action"] == "factory_insert"
    assert len(_inserted(backend)) == 1

    # The transfer already has a native receipt. A later research replacement
    # must reconcile that same receipt, never replan or rewrite the paid action.
    backend.state.factory["research"] = "new-disjoint-study"
    result = loop.step()

    assert result["verified"] is True
    assert loop.memory.pending is None
    assert loop.memory.background_job is not None
    assert len(_inserted(backend)) == 1
    assert backend.state.factory["receipts"][_inserted(backend)[0]["receipt"]][
        "quantity"] == 20
