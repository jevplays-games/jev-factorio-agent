"""Private v3 capture for complete solid/coal integration trials.

This preserves transport ownership and source evidence. It checks internal
consistency only; native authenticity and rollout authority remain external.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import gzip
import io
import os
from pathlib import Path

from .acceptance_capture import RECORD_FIELDS, STATE_FIELDS, FACTORY_FIELDS, DENIED
from .acceptance_io import MAX_JSON, MAX_LOG, canonical, hash_file, load_json, records, sha256, stable_read, write_new
from .integration_evidence import (TRIAL_SCHEMA_V2, TRIAL_SCHEMA_V3,
                                   valid_input_validation_failure, validate_trial)
from .research_log import Redactor
from .dev_preflight import checkpoint_type
from .state import GameSnapshot
from . import coal_supply as coal
from . import solid_routes as solid
from .planning import solid_funding

SCHEMA = 'jev-factorio.complete-capture.v3'
TOP_FIELDS = RECORD_FIELDS | {
    'solid_routes', 'solid_route_evidence', 'solid_route_fault', 'solid_science_policy',
    'solid_investment_evidence', 'solid_funding_schema', 'solid_funding',
    'coal_supply', 'coal_supply_evidence', 'coal_supply_fault',
    'coal_kit_policy', 'coal_kit_evidence', 'coal_economic_admission',
    'coal_admission_evidence', 'input_validation_failure', 'previous_iteration_timing',
}
NATIVE_FIELDS = FACTORY_FIELDS | {'solid_routes', 'coal_supply'}
CRITICAL = ('coal', 'solid', 'owner', 'receipt', 'funding', 'flow', 'commitment', 'pending')
FILES = {'capture-manifest.json', 'trial.json', 'preflight.json', 'initial-checkpoint.json',
         'final-checkpoint.json', 'gameplay.jsonl.gz'}


def checked_checkpoint(raw: bytes) -> dict:
    data = load_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get('session_id'), str) or not isinstance(data.get('target'), str):
        raise ValueError('Invalid composed checkpoint identity')
    checkpoint_type(data).from_bytes(raw, data['session_id'], data['target'])
    return data


def checked_coal_observation(state: dict, checkpoint: dict | None = None) -> None:
    if not isinstance(state, dict) or not isinstance(state.get('factory'), dict):
        raise ValueError('Missing native coal observation')
    snapshot = GameSnapshot(tick=state.get('tick'), session_id=state.get('session_id'),
                            factory=state['factory'])
    rows = coal.sources(snapshot)
    native = snapshot.factory['coal_supply']
    if checkpoint is None:
        return
    if (snapshot.session_id != checkpoint['session_id']
            or native['targets'] != checkpoint['coal_targets']
            or any(native[key] != checkpoint['coal_epoch'][key]
                   for key in ('actor_index', 'surface_index', 'force_index'))):
        raise ValueError('Coal observation differs from checkpoint binding')
    owned = checkpoint['coal_commitments']
    if owned and (not native['committed'] or set(rows) != set(owned)):
        raise ValueError('Coal checkpoint ownership disappeared')
    if native['committed'] and not owned:
        raise ValueError('Untracked native coal commitment')
    for target, saved in owned.items():
        if not coal.reconciles(saved, rows[target]):
            raise ValueError('Coal receipt or component differs from checkpoint')


def _solid_prefix(retained: dict, current: dict) -> bool:
    """Require immutable route identity and every already-paid receipt."""
    identity = ('route', 'layout', 'item', 'source', 'target', 'steps')
    return (all(canonical(retained.get(key)) == canonical(current.get(key)) for key in identity)
            and isinstance(retained.get('parts'), dict)
            and isinstance(current.get('parts'), dict)
            and all(canonical(current['parts'].get(part)) == canonical(paid)
                    for part, paid in retained['parts'].items()))


def _solid_observation(state: dict) -> dict:
    if not isinstance(state, dict) or not isinstance(state.get('factory'), dict):
        raise ValueError('Missing native solid-route observation')
    snapshot = GameSnapshot(tick=state.get('tick'), session_id=state.get('session_id'),
                            factory=state['factory'])
    return solid.routes(snapshot)


def checked_solid_observation(state: dict, checkpoint: dict) -> None:
    """Require endpoint native ownership to equal the composed checkpoint."""
    observed = _solid_observation(state)
    owned = {key: solid.commitment(row) for key, row in observed.items()
             if row['state'] != 'proposed'}
    if canonical(owned) != canonical(checkpoint['solid_commitments']):
        raise ValueError('Solid native route ownership differs from checkpoint')


def checked_solid_funding(row: dict, intents: list[dict]) -> None:
    if (type(row.get('solid_funding_schema')) is not int
            or row['solid_funding_schema'] != 1 or 'solid_funding' not in row
            or type(row.get('tick')) is not int or row['tick'] < 0):
        raise ValueError('Unsupported or missing solid funding record schema')
    funding = row['solid_funding']
    if funding is not None:
        solid_funding.validate_state(funding, row['tick'], intents)
        before = _solid_observation(row['state']).get(funding['route'])
        after = _solid_observation(row['after_state']).get(funding['route'])
        if (not isinstance(before, dict) or before.get('state') != 'proposed'
                or not solid_funding.bound(funding, before)
                or not isinstance(after, dict) or not solid_funding.bound(funding, after)):
            raise ValueError('Active solid funding differs from native proposed route')
        # The producer retains the funding receipt on the same record that
        # commits the route's first paid part. Preserve that real handoff row:
        # it must start from the exact proposed route, be the route-build action,
        # and end at a one-part building prefix. The campaign-wide continuity
        # check below then binds that prefix through the final checkpoint.
        if after['state'] != 'proposed':
            if (after['state'] != 'building' or row.get('action') != solid.COMMAND
                    or len(after['parts']) != 1
                    or set(after['parts']) != {after['steps'][0]['part']}
                    or after['pending']
                    or not _solid_prefix(solid.commitment(before), solid.commitment(after))):
                raise ValueError('Active solid funding differs from native proposed route')


def checked_checkpoint_progress(initial: dict, final: dict) -> None:
    if final['last_tick'] < initial['last_tick']:
        raise ValueError('Final checkpoint tick regressed')
    for key, count in initial['failures'].items():
        if final['failures'].get(key, 0) < count:
            raise ValueError('Checkpoint failure history regressed')
    if ('output_buffers_schema' in initial
            and final.get('output_buffers_schema') != initial['output_buffers_schema']):
        raise ValueError('Checkpoint output-buffer ownership extension regressed')
    for source, old in initial.get('output_commitments', {}).items():
        new = final.get('output_commitments', {}).get(source)
        if (not isinstance(new, dict) or new.get('layout') != old['layout']
                or new.get('source_unit') != old['source_unit']
                or any(new.get('parts', {}).get(part) != paid for part, paid in old['parts'].items())):
            raise ValueError('Checkpoint paid output-buffer ownership regressed')
    for target, old in initial['coal_commitments'].items():
        new = final['coal_commitments'].get(target)
        if (not isinstance(new, dict) or new.get('layout') != old['layout']
                or new.get('target') != old['target']
                or any(new.get('parts', {}).get(part) != paid
                       for part, paid in old['parts'].items())):
            raise ValueError('Checkpoint paid coal ownership regressed')
    for route, old in initial.get('solid_commitments', {}).items():
        new = final.get('solid_commitments', {}).get(route)
        if not isinstance(new, dict) or not _solid_prefix(old, new):
            raise ValueError('Checkpoint paid solid ownership regressed')


def checked_campaign_binding(initial: dict, final: dict, rows: list[dict]) -> None:
    """Bind every retained boundary, even after valid bundle checksums change."""
    session = initial['session_id']
    if (final['session_id'] != session
            or initial['target'] != 'rocket_launch' or final['target'] != initial['target']):
        raise ValueError('Capture campaign identity mismatch')
    epoch = initial['solid_epoch']
    if not epoch or any(checkpoint.get(key) != epoch for checkpoint in (initial, final)
                        for key in ('solid_epoch', 'coal_epoch')):
        raise ValueError('Capture actor epoch mismatch')
    last_tick = None
    runtime_identity = None
    retained_solid = deepcopy(initial['solid_commitments'])
    final_solid = final['solid_commitments']
    for row in rows:
        if row.get('session_id') != session or row.get('target') != initial['target']:
            raise ValueError('Capture record campaign identity mismatch')
        for label in ('state', 'after_state'):
            state = row.get(label)
            if (not isinstance(state, dict) or state.get('session_id') != session
                    or type(state.get('tick')) is not int or state['tick'] < 0
                    or (last_tick is None and state['tick'] < initial['last_tick'])
                    or (last_tick is not None and state['tick'] < last_tick)):
                raise ValueError('Capture observation campaign identity mismatch')
            last_tick = state['tick']
            factory = state.get('factory')
            if not isinstance(factory, dict):
                raise ValueError('Capture native identity binding missing')
            for family in ('solid_routes', 'coal_supply'):
                native = factory.get(family)
                if (not isinstance(native, dict) or native.get('session_id') != session
                        or native.get('tick') != state.get('tick')
                        or any(native.get(key) != value for key, value in epoch.items())):
                    raise ValueError('Capture native identity binding mismatch')
            runtime = factory.get('acceptance_runtime')
            if (not isinstance(runtime, dict) or runtime.get('session_id') != session
                    or type(runtime.get('actor_unit')) is not int or runtime['actor_unit'] < 1
                    or any(type(runtime.get(key)) is not int or runtime[key] < 1
                           for key in ('player_index', 'surface_index', 'force_index'))
                    or not isinstance(runtime.get('mods'), dict)):
                raise ValueError('Capture actor identity binding missing')
            identity = canonical({key: runtime.get(key) for key in
                                  ('session_id', 'actor_unit', 'player_index', 'surface_index',
                                   'force_index', 'mods')})
            if runtime_identity is None:
                runtime_identity = identity
            elif runtime_identity != identity:
                raise ValueError('Capture actor identity changed')

            observed_solid = _solid_observation(state)
            for route, old in retained_solid.items():
                current = observed_solid.get(route)
                if current is None or not solid.reconciles(old, current):
                    raise ValueError('Paid solid route disappeared or regressed in gameplay')
            for route, current in observed_solid.items():
                if current['state'] == 'proposed':
                    continue
                commitment = solid.commitment(current)
                prior = retained_solid.get(route)
                final_owned = final_solid.get(route)
                if prior is not None and not _solid_prefix(prior, commitment):
                    raise ValueError('Paid solid route identity or receipt changed in gameplay')
                if not isinstance(final_owned, dict) or not _solid_prefix(commitment, final_owned):
                    raise ValueError('Final checkpoint does not retain observed solid ownership')
                retained_solid[route] = commitment
    if last_tick is None or last_tick > final['last_tick']:
        raise ValueError('Final checkpoint precedes captured gameplay')
    checked_solid_observation(rows[0]['state'], initial)
    checked_solid_observation(rows[-1]['after_state'], final)


def checked_economic_binding(trial: dict, initial: dict, final: dict, rows: list[dict]) -> None:
    enabled = trial['configuration'].get('coal_economic_admission', False)
    for checkpoint in (initial, final):
        if (checkpoint.get('coal_economic_admission', False) is not enabled
                or checkpoint.get('coal_supply_schema') != (2 if enabled else 1)):
            raise ValueError('Coal economic treatment differs from checkpoint')
    for row in rows:
        if row.get('acceptance_configuration') != trial['configuration']:
            raise ValueError('Coal economic treatment differs from gameplay configuration')
        if enabled:
            if row.get('coal_economic_admission') is not True or not isinstance(row.get('coal_admission_evidence'), dict):
                raise ValueError('Coal economic admission evidence missing')
            for label in ('state', 'after_state'):
                if row[label]['factory']['coal_supply'].get('protocol') != 2:
                    raise ValueError('Coal economic admission requires native protocol 2')


def checked_preflight(preflight: dict, trial: dict, initial: dict, rows=None) -> None:
    """Legacy unsupported evidence and qualified v2 are separate boundaries."""
    if (not isinstance(preflight, dict)
            or preflight.get('checkpoint_sha256') != trial['initial_checkpoint_sha256']
            or preflight.get('vm_uuid') != trial['vm_uuid']
            or preflight.get('production_vm_uuid') != trial['production_vm_uuid']):
        raise ValueError('Preflight differs from complete trial boundary')
    if preflight.get('schema') == 'jev-factorio.dev-preflight.v1':
        if (preflight.get('ready_for_coordinated_validation') is not False
                or not {'solid_preflight_not_supported', 'coal_preflight_not_supported'} <=
                       set(preflight.get('issues', []))):
            raise ValueError('Legacy complete preflight must retain unsupported ownership')
        return
    from .dev_preflight_v2 import REPORT_SCHEMA, SCOPE, inspect_native, query_sha256
    if (preflight.get('schema') != REPORT_SCHEMA or preflight.get('ownership_scope') != SCOPE
            or preflight.get('ready_for_coordinated_validation') is not True
            or preflight.get('issues') != [] or preflight.get('gameplay_started') is not False
            or preflight.get('deployment_authorized') is not False
            or preflight.get('native_acceptance_proven') is not False
            or preflight.get('query_sha256') != query_sha256()
            or inspect_native(preflight.get('native'), initial, initial['session_id'])):
        raise ValueError('Unqualified v2 transport ownership preflight')
    if rows is not None:
        native = preflight['native']
        if not rows or rows[0]['state']['tick'] < native['tick']:
            raise ValueError('Gameplay precedes ownership preflight')
        for row in rows:
            for label in ('state', 'after_state'):
                if row[label].get('tick', -1) < native['tick']:
                    raise ValueError('Gameplay observation precedes ownership preflight')
                runtime = row[label]['factory'].get('acceptance_runtime')
                if (not isinstance(runtime, dict) or any(not same(runtime.get(k), native[k])
                        for k in ('session_id', 'player_index', 'actor_unit', 'surface_index',
                                  'force_index', 'mods'))):
                    raise ValueError('Gameplay differs from preflight actor identity')


def same(left, right):
    return canonical(left) == canonical(right)


def project_record(row: dict, redactor: Redactor, omissions: Counter,
                   solid_intents: list[dict] | None = None) -> dict:
    if not isinstance(row, dict):
        raise ValueError('Invalid gameplay record')
    unknown = set(row) - TOP_FIELDS - {'state', 'after_state', 'decision'}
    if any(any(word in key.lower() for word in CRITICAL) for key in unknown):
        raise ValueError('Unknown treatment or ownership evidence field')
    omissions.update('top:' + key for key in unknown)
    result = {key: deepcopy(row[key]) for key in TOP_FIELDS if key in row}
    for label in ('state', 'after_state'):
        state = row.get(label)
        if not isinstance(state, dict) or not isinstance(state.get('factory'), dict):
            raise ValueError('Missing before/after native observation')
        unknown_state = set(state) - STATE_FIELDS - {'factory'}
        if any(any(word in key.lower() for word in CRITICAL) for key in unknown_state):
            raise ValueError('Unknown state ownership evidence field')
        omissions.update('state:' + key for key in unknown_state)
        factory = state['factory']
        if 'solid_routes' not in factory or 'coal_supply' not in factory:
            raise ValueError('Complete transport observations are missing')
        unknown = set(factory) - NATIVE_FIELDS
        if any(any(word in key.lower() for word in CRITICAL) for key in unknown):
            raise ValueError('Unknown native treatment evidence field')
        omissions.update('factory:' + key for key in unknown)
        result[label] = {key: deepcopy(state[key]) for key in STATE_FIELDS if key in state}
        result[label]['factory'] = {key: deepcopy(factory[key]) for key in NATIVE_FIELDS if key in factory}
    decision = row.get('decision')
    if decision is not None and not isinstance(decision, dict):
        raise ValueError('Invalid decision evidence')
    if decision is not None:
        unknown_decision = set(decision) - {'plan_id', 'source', 'model_called'}
        if any(any(word in key.lower() for word in CRITICAL) for key in unknown_decision):
            raise ValueError('Unknown decision ownership evidence field')
        omissions.update('decision:' + key for key in unknown_decision)
    result['decision'] = ({key: deepcopy(decision[key]) for key in
                           ('plan_id', 'source', 'model_called') if key in decision}
                          if decision is not None else None)
    # Sensitive structured data must not be silently retained or stripped from
    # an ownership chain. Stop capture and require a reviewed schema revision.
    def check(node):
        if isinstance(node, dict):
            if any(key.lower().replace('-', '_') in DENIED for key in node):
                raise ValueError('Sensitive key in selected evidence')
            for item in node.values(): check(item)
        elif isinstance(node, list):
            for item in node: check(item)
    check(result)
    if ('input_validation_failure' in result
            and not valid_input_validation_failure(result['input_validation_failure'])):
        raise ValueError('Invalid input-route validation failure evidence')
    cleaned = redactor.clean(result)
    if solid_intents is not None:
        checked_solid_funding(cleaned, solid_intents)
    return cleaned


def capture(*, gameplay: Path, trial_path: Path, initial_checkpoint: Path,
            final_checkpoint: Path, save: Path, preflight_path: Path, output: Path, environ=None) -> dict:
    if output.exists() or output.is_symlink():
        raise ValueError('Capture destination already exists')
    trial_raw = stable_read(trial_path)
    trial = load_json(trial_raw)
    validate_trial(trial)
    if trial['schema'] not in {TRIAL_SCHEMA_V2, TRIAL_SCHEMA_V3}:
        raise ValueError('Complete capture requires a complete trial')
    initial_raw = stable_read(initial_checkpoint)
    final_raw = stable_read(final_checkpoint)
    if sha256(initial_raw) != trial['initial_checkpoint_sha256']:
        raise ValueError('Initial checkpoint differs from predeclared trial')
    preflight_raw = stable_read(preflight_path)
    preflight = load_json(preflight_raw)
    initial, final = checked_checkpoint(initial_raw), checked_checkpoint(final_raw)
    checked_preflight(preflight, trial, initial)
    checked_checkpoint_progress(initial, final)
    if (initial.get('solid_intents') != trial['solid_intents']
            or final.get('solid_intents') != trial['solid_intents']
            or initial.get('coal_targets') != trial['coal_targets']
            or final.get('coal_targets') != trial['coal_targets']
            or any(cp.get('solid_science_policy') is not trial['configuration']['solid_science_policy']
                   or cp.get('coal_kit_policy') is not trial['configuration']['coal_kit_policy']
                   for cp in (initial, final))):
        raise ValueError('Capture checkpoint treatment mismatch')
    saved = hash_file(save)
    if saved['sha256'] != trial['initial_save_sha256']:
        raise ValueError('Initial save differs from predeclared trial')
    raw = stable_read(gameplay, MAX_LOG)
    redactor = Redactor(dict(os.environ if environ is None else environ))
    omissions = Counter()
    projected = [project_record(row, redactor, omissions, trial['solid_intents'])
                 for row in records(raw)]
    checked_preflight(preflight, trial, initial, projected)
    checked_economic_binding(trial, initial, final, projected)
    for row in projected:
        for label in ('state', 'after_state'):
            checked_coal_observation(row[label])
    checked_campaign_binding(initial, final, projected)
    checked_coal_observation(projected[0]['state'], initial)
    checked_coal_observation(projected[-1]['after_state'], final)
    payload = b''.join(canonical(row) for row in projected)
    if len(payload) > MAX_LOG:
        raise ValueError('Projected gameplay exceeds capture budget')
    content = {'trial.json': canonical(redactor.clean(trial)),
               'preflight.json': canonical(redactor.clean(preflight)),
               'initial-checkpoint.json': canonical(redactor.clean(initial)),
               'final-checkpoint.json': canonical(redactor.clean(final)),
               'gameplay.jsonl.gz': gzip.compress(payload, mtime=0)}
    manifest = {'schema': SCHEMA, 'records': len(projected),
                'decompressed_bytes': len(payload), 'decompressed_sha256': sha256(payload),
                'source_gameplay_sha256': sha256(raw),
                'source_initial_checkpoint_sha256': sha256(initial_raw),
                'source_final_checkpoint_sha256': sha256(final_raw),
                'source_trial_sha256': sha256(trial_raw),
                'source_preflight_sha256': sha256(preflight_raw), 'source_save': saved,
                'projection_omissions': dict(omissions), 'capture_complete': True,
                'native_acceptance': 'not_accepted', 'deployment_authorized': False}
    content['capture-manifest.json'] = canonical(manifest)
    output.mkdir(mode=0o700)
    for name, value in content.items():
        write_new(output / name, value)
    sums = ''.join(sha256(content[name]) + '  ' + name + '\n' for name in sorted(content))
    write_new(output / 'SHA256SUMS', sums.encode())
    return manifest


def verify(directory: Path) -> dict:
    if directory.is_symlink() or {p.name for p in directory.iterdir()} != FILES | {'SHA256SUMS'}:
        raise ValueError('Incomplete complete-capture directory')
    sums = stable_read(directory / 'SHA256SUMS').decode().splitlines()
    expected = {line[66:]: line[:64] for line in sums if len(line) >= 67 and line[64:66] == '  '}
    if len(sums) != len(FILES) or set(expected) != FILES:
        raise ValueError('Invalid checksum manifest')
    content = {name: stable_read(directory / name, MAX_LOG if name.endswith('.gz') else MAX_JSON)
               for name in FILES}
    if any(sha256(content[name]) != expected[name] for name in FILES):
        raise ValueError('Capture checksum mismatch')
    manifest = load_json(content['capture-manifest.json'])
    size = manifest.get('decompressed_bytes')
    if manifest.get('schema') != SCHEMA or type(size) is not int or not 0 < size <= MAX_LOG:
        raise ValueError('Invalid complete-capture schema or size')
    with gzip.GzipFile(fileobj=io.BytesIO(content['gameplay.jsonl.gz'])) as stream:
        raw = stream.read(size + 1)
    if len(raw) != size or sha256(raw) != manifest.get('decompressed_sha256'):
        raise ValueError('Projected gameplay mismatch')
    trial = load_json(content['trial.json'])
    validate_trial(trial)
    projected = records(raw)
    reviewed = [project_record(row, Redactor({}), Counter(), trial['solid_intents'])
                for row in projected]
    if any(canonical(reviewed_row) != canonical(original_row)
           for reviewed_row, original_row in zip(reviewed, projected)):
        raise ValueError('Captured gameplay differs from reviewed record schema')
    projected = reviewed
    preflight = load_json(content['preflight.json'])
    initial = checked_checkpoint(content['initial-checkpoint.json'])
    final = checked_checkpoint(content['final-checkpoint.json'])
    checked_preflight(preflight, trial, initial, projected)
    checked_checkpoint_progress(initial, final)
    checked_economic_binding(trial, initial, final, projected)
    if (trial['schema'] not in {TRIAL_SCHEMA_V2, TRIAL_SCHEMA_V3}
            or len(projected) != manifest.get('records')
            or manifest.get('capture_complete') is not True
            or manifest.get('native_acceptance') != 'not_accepted'
            or manifest.get('deployment_authorized') is not False
            or manifest.get('source_initial_checkpoint_sha256') != trial['initial_checkpoint_sha256']
            or manifest.get('source_save', {}).get('sha256') != trial['initial_save_sha256']
            or any(cp.get('solid_intents') != trial['solid_intents']
                   or cp.get('coal_targets') != trial['coal_targets']
                   or cp.get('solid_science_policy') is not trial['configuration']['solid_science_policy']
                   or cp.get('coal_kit_policy') is not trial['configuration']['coal_kit_policy']
                   for cp in (initial, final))
            or any(row.get('acceptance_configuration') != trial['configuration']
                   or any(not isinstance(row.get(label), dict)
                          or not isinstance(row[label].get('factory'), dict)
                          or not {'solid_routes', 'coal_supply'} <= row[label]['factory'].keys()
                          for label in ('state', 'after_state'))
                   for row in projected)):
        raise ValueError('Complete trial or record count mismatch')
    for row in projected:
        for label in ('state', 'after_state'):
            checked_coal_observation(row[label])
    checked_campaign_binding(initial, final, projected)
    checked_coal_observation(projected[0]['state'], initial)
    checked_coal_observation(projected[-1]['after_state'], final)
    return {'manifest': manifest, 'trial': trial, 'rows': projected}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('gameplay', 'trial', 'preflight', 'initial-checkpoint', 'final-checkpoint', 'save', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args(argv)
    capture(gameplay=args.gameplay, trial_path=args.trial, preflight_path=args.preflight,
            initial_checkpoint=args.initial_checkpoint,
            final_checkpoint=args.final_checkpoint, save=args.save, output=args.output)


if __name__ == '__main__':
    main()
