"""Conditional scheduling evidence, never background admission or a receipt."""
from __future__ import annotations

from ..craft_jobs import permits_locked_outputs


def add_craft_overlap_evidence(snapshot, plans, rows):
    """Explain an observed craft/gather pair without changing its choice frontier.

    The craft's inputs are present now; they are not paid until native admission.
    Gathering may overlap only after that admission and a fresh observation.
    """
    if len(plans) != 2 or len({plan.id for plan in plans}) != 2:
        return
    crafts = [p for p in plans if len(p.steps) == 1
              and p.steps[0].action == 'factory_craft_job']
    gathers = [p for p in plans if len(p.steps) == 1
               and p.steps[0].action == 'factory_gather']
    if len(crafts) != 1 or len(gathers) != 1:
        return
    craft, gather = crafts[0], gathers[0]
    cr, gr = rows.get(craft.id), rows.get(gather.id)
    if not isinstance(cr, dict) or not isinstance(gr, dict):
        return
    start, dependency = cr.get('craft_start_evidence'), cr.get('craft_dependency')
    raw, gather_start = gr.get('raw_prerequisite'), gr.get('gather_start_evidence')
    if not all(isinstance(value, dict) for value in (start, dependency, raw, gather_start)):
        return
    cs, gs = craft.steps[0], gather.steps[0]
    outputs = start.get('expected_products_after_native_verification')
    cp, gp = dependency.get('planner_item_path'), raw.get('planner_item_path')
    target = cr.get('local_target')
    if (any(row.get('unknowns') != [] or row.get('work_scope') != 'immediate'
            or type(row.get('urgency')) is not int or row['urgency'] != 0
            or row.get('research_deadline_tick') is not None for row in (cr, gr))
            or not isinstance(target, dict) or target != gr.get('local_target')
            or not isinstance(target.get('item'), str) or not target['item']
            or any(value.get('observed_tick') != snapshot.tick
                   for value in (start, dependency, raw, gather_start))
            or not isinstance(cp, list) or not 2 <= len(cp) <= 32
            or not isinstance(gp, list) or not 2 <= len(gp) <= 32
            or cp[0] != target['item'] or gp[0] != target['item']
            or cp[-1] != cs.item or gp[-1] != (gs.parameters or {}).get('resource')
            or cs.effect != 'craft_job_complete' or gs.effect != 'inventory'
            or not isinstance((cs.parameters or {}).get('receipt'), str)
            or not cs.parameters['receipt']
            or start.get('native_recipe') != (cs.parameters or {}).get('recipe')
            or not all(start.get(key) is True for key in (
                'input_costs_match_native_recipe', 'inputs_in_inventory_now',
                'recipe_unlocked_and_handcraftable', 'player_connected_and_bound',
                'crafting_queue_empty', 'craft_job_protocol_ready',
                'native_receipt_required_for_completion'))
            or not isinstance(outputs, dict) or len(outputs) != 1 or cs.item not in outputs
            or any(type(count) is not int or count <= 0 for count in outputs.values())
            or not cs.costs or gs.costs
            or set(cs.costs) & set(outputs)
            or not all(type(count) is int and count > 0
                       and snapshot.inventory.get(item, 0) >= count
                       for item, count in cs.costs.items())
            or gather_start.get('resource_in_current_observation') is not True
            or gather_start.get('fair_target_identity_observed') is not True
            or gather_start.get('session_id') != snapshot.session_id
            or gather_start.get('target_inventory_after_this_step') != gs.threshold
            or not permits_locked_outputs(gs, set(outputs))):
        return
    cr['independent_gather_overlap'] = {
        'observed_tick': snapshot.tick,
        'session_id': snapshot.session_id,
        'basis': 'current_craft_start_and_independent_raw_gather_output_lock',
        'craft_plan_id': craft.id,
        'gather_plan_id': gather.id,
        'local_target': dict(target),
        'inputs_available_now_not_yet_paid': dict(cs.costs),
        'expected_outputs_locked_until_native_receipt': dict(outputs),
        'gather_consumes_no_inventory': True,
        'gather_does_not_touch_locked_outputs': True,
        'ordering_effect': (
            'Starting this craft first can let its native crafting queue run during '
            'later independent gathering. Gathering first leaves this craft unstarted '
            'for the duration of that gather. Both advance the same current target; '
            'this is an overlap opportunity, not a promise of elapsed time saved.'),
        'requires_native_admission_then_fresh_gather_observation': True,
        'future_gather_selection_and_all_judgment_gates_remain_required': True,
        'does_not_authorize_either_action_or_prove_completion': True,
    }
