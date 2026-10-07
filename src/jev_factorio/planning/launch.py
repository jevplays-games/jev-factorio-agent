"""Prepare a first base-game launch using real owned stock and bounded receipts."""
from __future__ import annotations

from .. import launch_readiness as contract


def prerequisite(planner):
    """Called after rocket-silo research; returns work before a launch request."""
    row = contract.evidence(planner.snapshot)
    if row['fault']:
        raise ValueError('Launch readiness requires reconciliation; paid history retained')
    silo = row['silo']
    if row['attempts'].get('launch'):
        return planner._wait('rocket_launched', timeout=18000, identity='launch:submitted')
    if not contract.load_reconciled(planner.snapshot, row):
        raise ValueError('Unresolved rocket cargo transfer; retain its original receipt')
    if not row['pad']:
        if row['attempts'].get('pad'):
            raise ValueError('Unresolved landing-pad build; do not repeat placement')
        work = planner._need('cargo-landing-pad', 1)
        if work: return work
        if not row['pad_site']:
            raise ValueError('No bounded clear landing-pad site; no production removed')
        site = row['pad_site']['id']
        return planner._plan('factory_launch_pad', 'launch_pad', parameters={
            'site': site, 'receipt': f'launch:pad:{planner.snapshot.tick}'}, costs={'cargo-landing-pad': 1},
            timeout=18000, identity='first-pad', description='Build one paid landing pad without replacing production')
    if contract.payload(silo):
        if not row['pad']['accepts'].get(contract.payload(silo)):
            raise ValueError('Landing pad lacks room for launch products; no cargo discarded')
        if silo.get('automatic'):
            raise ValueError('Automatic launch enabled; do not alter existing settings')
        return None
    if silo.get('cargo'):
        raise ValueError('Unexpected rocket cargo; do not discard or replace it')
    carried = next((p for p in contract.PAYLOADS if planner.snapshot.inventory.get(p, 0) >= 1), None)
    if carried is None:
        # Collection from an already owned chest can avoid an unnecessary craft.
        for item in contract.PAYLOADS:
            for role, entity in sorted(planner.entities.items()):
                if entity.get('output', {}).get(item, 0) >= 1:
                    return planner._transfer(role, item, 1, extracting=True)
        fish = fish_plan(planner)
        if fish: return fish
        # Fish outside current normal reach is not authority to teleport/scout water.
        return planner._need('satellite', 1)
    if not row['pad']['accepts'].get(carried):
        raise ValueError('Landing pad lacks room for launch products; no cargo discarded')
    if not silo.get('ready'): return None  # Keep producing the rocket, payload held aside.
    if row['attempts'].get('load'):
        raise ValueError('Unresolved rocket cargo transfer; retain its original receipt')
    if silo.get('automatic') or not silo.get('cargo_available') or not silo.get('rocket_unit'):
        raise ValueError('Rocket cargo unavailable or automatic launch enabled; no settings changed')
    return planner._plan('factory_launch_payload', 'launch_payload', parameters={
        'role': contract.SILO, 'silo_unit': silo['unit_number'], 'rocket_unit': silo['rocket_unit'],
        'item': carried, 'receipt': f'launch:load:{planner.snapshot.tick}'}, costs={carried: 1},
        timeout=18000, identity='first-payload', description=f'Load one paid {carried} into this rocket cargo inventory')


def fish_plan(planner):
    row = contract.evidence(planner.snapshot)
    if (row['fault'] or row['attempts'].get('fish') or not row['fish']
            or any(planner.snapshot.inventory.get(p, 0) >= 1 for p in contract.PAYLOADS)
            or contract.payload(row['silo']) or planner.factory.get('crafting_queue', 0)):
        return None
    return planner._plan('factory_launch_fish', 'launch_fish', parameters={
        'target': row['fish']['id'], 'receipt': f'launch:fish:{planner.snapshot.tick}'},
        timeout=1800, identity='first-fish', description='Mine one currently reachable fish using timed native controls')


def opportunistic(planner, primary):
    """Only replace a passive safe wait; never delay selected urgent supply work."""
    if (not primary or 'launch_readiness' not in planner.factory
            or planner.goal != 'rocket_launch' or planner.speculative
            or (primary.materials or {}).get('capital_investment')
            or getattr(planner, '_buffer_service', False)
            or primary.steps[0].action != 'factory_wait'
            or primary.steps[0].effect not in {'machine_output', 'research_progress'}):
        return primary
    try:
        return fish_plan(planner) or primary
    except ValueError:
        return primary
