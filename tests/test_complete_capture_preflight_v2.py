"""V2 point-in-time ownership is rebound at capture and independent verification."""
import hashlib
import gzip
import json

import pytest

from jev_factorio.acceptance_io import canonical
from jev_factorio.complete_capture import capture, verify
from jev_factorio.dev_preflight_v2 import REPORT_SCHEMA, SCOPE, query_sha256
from test_complete_capture import test_complete_capture_v2_admission_roundtrip as legacy_fixture
from test_transport_ownership_preflight_v2 import native_for_checkpoint, wire_native


def source_files(tmp_path):
    legacy_fixture(tmp_path)
    initial = json.loads((tmp_path / 'initial.json').read_bytes())
    trial = json.loads((tmp_path / 'trial.json').read_bytes())
    preflight = json.loads((tmp_path / 'preflight.json').read_bytes())
    rows = [json.loads(line) for line in (tmp_path / 'gameplay.jsonl').read_bytes().splitlines()]
    # The point-in-time query fixture has no paid route, so begin with a
    # schema-valid empty checkpoint and introduce the retained route afterward.
    initial['solid_commitments'] = {}
    rows[0]['state']['factory']['solid_routes']['routes'] = {}
    rows[0]['state']['factory']['solid_routes']['diagnostics'] = []
    trial['initial_checkpoint_sha256'] = hashlib.sha256(canonical(initial)).hexdigest()
    preflight['checkpoint_sha256'] = trial['initial_checkpoint_sha256']
    (tmp_path / 'initial.json').write_bytes(canonical(initial))
    (tmp_path / 'trial.json').write_bytes(canonical(trial))
    (tmp_path / 'preflight.json').write_bytes(canonical(preflight))
    (tmp_path / 'gameplay.jsonl').write_bytes(b''.join(canonical(row) for row in rows))
    native = native_for_checkpoint(initial)
    native['actor_unit'] = 999998
    preflight.update(schema=REPORT_SCHEMA, ownership_scope=SCOPE,
        query_sha256=query_sha256(), ready_for_coordinated_validation=True, issues=[],
        gameplay_started=False, deployment_authorized=False, native_acceptance_proven=False,
        native=wire_native(native))
    for row in rows:
        for label in ('state', 'after_state'):
            row[label]['factory']['acceptance_runtime']['mods'] = {'base': '2.0.77'}
    (tmp_path / 'gameplay.jsonl').write_bytes(b''.join(canonical(row) for row in rows))
    (tmp_path / 'preflight.json').write_bytes(canonical(preflight))
    return preflight, rows


def build(tmp_path, output):
    return capture(gameplay=tmp_path / 'gameplay.jsonl', trial_path=tmp_path / 'trial.json',
        initial_checkpoint=tmp_path / 'initial.json', final_checkpoint=tmp_path / 'final.json',
        save=tmp_path / 'save.zip', preflight_path=tmp_path / 'preflight.json', output=output)


def rewrite_bundle_with_rows_and_preflight(output, rows, preflight):
    raw = b''.join(canonical(row) for row in rows)
    (output / 'gameplay.jsonl.gz').write_bytes(gzip.compress(raw, mtime=0))
    (output / 'preflight.json').write_bytes(canonical(preflight))
    manifest = json.loads((output / 'capture-manifest.json').read_bytes())
    manifest.update(decompressed_bytes=len(raw), decompressed_sha256=hashlib.sha256(raw).hexdigest())
    (output / 'capture-manifest.json').write_bytes(canonical(manifest))
    (output / 'SHA256SUMS').write_text(''.join(
        hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n'
        for path in sorted(output.iterdir()) if path.name != 'SHA256SUMS'))


def test_qualified_v2_capture_roundtrip_still_does_not_claim_native_acceptance(tmp_path):
    source_files(tmp_path)
    manifest = build(tmp_path, tmp_path / 'v2')
    assert manifest['native_acceptance'] == 'not_accepted'
    assert verify(tmp_path / 'v2')['rows']
    assert json.loads((tmp_path / 'v2' / 'preflight.json').read_bytes())['schema'] == REPORT_SCHEMA
    assert verify(tmp_path / 'capture')['rows']
    assert json.loads((tmp_path / 'capture' / 'preflight.json').read_bytes())['schema'] == 'jev-factorio.dev-preflight.v1'


@pytest.mark.parametrize('entrypoint', ['capture', 'verify'])
@pytest.mark.parametrize('mutation', ['query', 'identity', 'treatment', 'pending', 'authorization', 'late', 'unsupported'])
def test_v2_report_is_revalidated_even_with_consistent_checksums(tmp_path, entrypoint, mutation):
    preflight, rows = source_files(tmp_path)
    output = tmp_path / 'v2'
    if entrypoint == 'verify':
        build(tmp_path, output)
        rows = verify(output)['rows']
    if mutation == 'query':
        preflight['query_sha256'] = '0' * 64
    elif mutation == 'identity':
        preflight['native']['actor_unit'] += 1
    elif mutation == 'treatment':
        preflight['native']['solid_routes']['binding'] = 'different'
    elif mutation == 'pending':
        preflight['native']['idle']['construction_pending'] = True
    elif mutation == 'authorization':
        preflight['deployment_authorized'] = True
    elif mutation == 'late':
        preflight['native']['tick'] = rows[0]['state']['tick'] + 1
    elif mutation == 'unsupported':
        preflight['ready_for_coordinated_validation'] = False
        preflight['issues'] = ['solid_preflight_not_supported', 'coal_preflight_not_supported']
    if entrypoint == 'capture':
        (tmp_path / 'preflight.json').write_bytes(canonical(preflight))
        with pytest.raises(ValueError, match='preflight'):
            build(tmp_path, output)
        assert not output.exists()
    else:
        (output / 'preflight.json').write_bytes(canonical(preflight))
        (output / 'SHA256SUMS').write_text(''.join(
            hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n'
            for path in sorted(output.iterdir()) if path.name != 'SHA256SUMS'))
        with pytest.raises(ValueError, match='preflight'):
            verify(output)


@pytest.mark.parametrize('entrypoint', ['capture', 'verify'])
def test_v2_preflight_tick_floor_applies_to_every_state_and_after_state(tmp_path, entrypoint):
    preflight, rows = source_files(tmp_path)
    output = tmp_path / 'v2'
    if entrypoint == 'verify':
        build(tmp_path, output)
        rows = verify(output)['rows']
    # Keep the record's top-level tick at the preflight boundary (the older
    # implementation checked only this tick) while its after-observation is
    # one tick too early. Bind each native family to that earlier snapshot so
    # the counterexample remains structurally valid.
    state = rows[0]['after_state']
    state['tick'] = preflight['native']['tick'] - 1
    state['factory']['tick'] = state['tick']
    for family in ('solid_routes', 'coal_supply'):
        state['factory'][family]['tick'] = state['tick']
    if isinstance(state['factory']['coal_supply'].get('admission'), dict):
        state['factory']['coal_supply']['admission']['tick'] = state['tick']
    assert rows[0]['tick'] == preflight['native']['tick']
    if entrypoint == 'capture':
        (tmp_path / 'gameplay.jsonl').write_bytes(b''.join(canonical(row) for row in rows))
        (tmp_path / 'preflight.json').write_bytes(canonical(preflight))
        with pytest.raises(ValueError, match='preflight'):
            build(tmp_path, output)
        assert not output.exists()
    else:
        rewrite_bundle_with_rows_and_preflight(output, rows, preflight)
        with pytest.raises(ValueError, match='preflight'):
            verify(output)
