"""Read-only qualification of an existing native callback installation.

An existing Factorio runtime keeps Lua closures in memory. Re-evaluating an inner
module replaces those closures while outer route/receipt wrappers still refer to
the old ones. A resumed Python adapter must therefore inspect the full installed
chain before constructing an adapter, and must reuse it without Lua installation.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from ..iteration_timing import decode_native
from ..bootstrap_output import (MODULE as BOOTSTRAP_MODULE, PROFILE as BOOTSTRAP_PROFILE,
                                MANUAL_CYCLE_PROFILE as MANUAL_CYCLE_BOOTSTRAP_PROFILE)


# These bytes were installed from main e759462 in the isolated native fixture.
# Changing an asset requires an explicit reviewed migration, not silent reuse.
PINNED_SOURCE_COMMIT = 'e75946253e9c0d9cda95a503e6c58110533347e4'
PINNED_SOURCE_TREE = '571d72f19c56bd15e1a6fc6feef35172fa68894f'
PINNED_ASSETS = {
    'fair_actions': 'cacb0396a75807bfd9b6987c732cbefe518e96517eb167611707e1ce48d2cbd4',
    'factory': 'f4f42f22b70ed7dc4a85dec627cdcd6be4d28df5c12e8a38066a13f0998ecdbc',
    'launch_readiness': 'b01fe73bc12055d7fc831f84d097094096610033e4d7a1195d623ea190c03bf5',
    'observation': '3cda1c7cf00ada82108a8dd89f52d523a3dff329f6e79a88f7549044ec025488',
    'observation_v2': '983307da88317e690582511d2514046fd8819b2cd6ffa6d0832725b2a450710f',
    'craft_jobs': 'd4034ff9f53076d14346e40b8190fb975a5a1ecf7df857932c6d9bf66ce51781',
    'output_buffers': '5a2cacb48e4623a27f0851b93ff00fac575c75de44c23f64a38130c21bddc39c',
    'input_routes': 'cef2ff8e7a1dc49ec3df6dc2a4faca78409dad303f0cccf87fe5aeb448b145c4',
    'production_sites': 'f215c67e4febf4e77c620e79d1dfd2ce91dae6dd5c1a386a27d1e805ca0dde2b',
    'mining_outposts': 'c517cf286ec1815fd2ea4860ea823036da19dca8074b4803ce7b284faeffb682',
    'solid_routes': '88b8f605e439a1f16783b9f9cc1e222f002f6be6e9b27494dbc309dce55b5809',
    'coal_supply': '3ec3b94b03c86cf963328ef9a6f75551ab285968ccfd50d2e2a25c72e89a242e',
    'successors': '7cd7999d3a4fee0faeb157c81487091b05366d919e17f34ae51a3061274d90ae',
}
OPTIONAL_ASSETS = {'coal_manual_journal_v1', 'coal_manual_cycle_v2',
                   'connector_observer_bridge_v1', BOOTSTRAP_MODULE}

LEGACY_OBSERVATION_PROFILE = 'e759-observation-v2-bound-bootstrap-v2'
LEGACY_OBSERVATION_SHA256 = 'f51ea4aeb66b5c11366dbfe37cb755f2187152fa634928ac8a911f670d746780'
EXPANDED_OBSERVATION_PROFILE = 'e759-observation-v2-expanded-oil-v3'
EXPANDED_OBSERVATION_SHA256 = '5cde46b9aea45840c17f820252611defe7b4eb4e82ba3cfe1d0d5d25225a6351'
EXPANDED_OBSERVATION_ASSET = 'observation_v2_anchor_v3.lua'
WATER_ORIGIN_OBSERVATION_PROFILE = 'e759-observation-v2-water-origin-v4'
WATER_ORIGIN_OBSERVATION_SHA256 = '3e989a8a6686a964f457a5c9820dc7ad68f1e25dcbd8a8ca3d02218271be6989'
WATER_ORIGIN_OBSERVATION_ASSET = 'observation_v2_water_origin_v4.lua'
LEGACY_MANUAL_CYCLE_PROFILE = 'e759-observation-v2-water-origin-v4-manual-cycle-v5'
MANUAL_CYCLE_PROFILE = 'e759-observation-v2-water-origin-v4-manual-cycle-v5-connector-observer-v1'
CLOSED_WORLD_PROFILE = 'e759-observation-v2-water-origin-v4-manual-cycle-v6-connector-observer-v1'
CONNECTOR_OBSERVER_WITNESS_NAME = 'native-connector-observer-v1.witness.jsonl'
LAUNCH_RECONCILIATION_PROTOCOL = 1

_RETAINED_PROFILES = {
    LEGACY_OBSERVATION_PROFILE,
    EXPANDED_OBSERVATION_PROFILE,
    WATER_ORIGIN_OBSERVATION_PROFILE,
    LEGACY_MANUAL_CYCLE_PROFILE,
    MANUAL_CYCLE_PROFILE,
    CLOSED_WORLD_PROFILE,
    BOOTSTRAP_PROFILE,
    MANUAL_CYCLE_BOOTSTRAP_PROFILE,
}


def manual_journal_sha256():
    return hashlib.sha256(files('jev_factorio').joinpath(
        'lua/coal_manual_journal_v1.lua').read_bytes()).hexdigest()


def cycle_journal_sha256():
    return hashlib.sha256(files('jev_factorio').joinpath(
        'lua/coal_manual_cycle_v2.lua').read_bytes()).hexdigest()


def connector_ownership_sha256():
    return hashlib.sha256(files('jev_factorio').joinpath(
        'lua/connector_ownership.lua').read_bytes()).hexdigest()


def connector_observer_bridge_sha256():
    return hashlib.sha256(files('jev_factorio').joinpath(
        'lua/connector_observer_bridge_v1.lua').read_bytes()).hexdigest()


def launch_readiness_sha256():
    """Return the exact current launch asset digest without changing legacy pins."""
    return hashlib.sha256(_asset_source('launch_readiness').read_bytes()).hexdigest()


def launch_reconciliation_available(attachment, proof):
    """Whether a qualified attachment proves the guarded module is actually loaded.

    Existing legacy attachments remain usable for unrelated capabilities. A
    launch or paid payload-load operation additionally needs the in-memory
    version marker and the exact current launch asset digest in its source-bound
    installation manifest.
    """
    if (not isinstance(proof, dict)
            or set(proof) != {'protocol', 'session_id', 'actor_unit', 'profile', 'asset_sha256'}
            or type(proof.get('protocol')) is not int
            or proof['protocol'] != LAUNCH_RECONCILIATION_PROTOCOL
            or not isinstance(proof.get('session_id'), str) or not proof['session_id']
            or type(proof.get('actor_unit')) is not int or proof['actor_unit'] < 1
            or proof.get('asset_sha256') != launch_readiness_sha256()):
        return False
    profile = proof.get('profile')
    if profile is False:
        if attachment is not None:
            from .native_current_attachment import is_supported_direct_installation
            if not is_supported_direct_installation(attachment):
                return False
    elif type(profile) is not str or profile not in _RETAINED_PROFILES:
        return False
    if attachment is None:
        return profile is False
    if (not isinstance(attachment, dict) or attachment.get('qualified') is not True
            or type(attachment.get('session_id')) is not str
            or type(attachment.get('actor_unit')) is not int
            or attachment.get('session_id') != proof['session_id']
            or attachment.get('actor_unit') != proof['actor_unit']):
        return False
    modules = attachment.get('modules')
    native = attachment.get('native_installation')
    if (not isinstance(modules, dict) or modules.get('launch_readiness') is not True
            or not isinstance(native, dict)
            or type(native.get('actor_unit')) is not int
            or type(native.get('session_id')) is not str
            or native.get('schema') != NATIVE_SCHEMA
            or native.get('session_id') != proof['session_id']
            or native.get('actor_unit') != proof['actor_unit']
            or native.get('profile') != profile
            or not isinstance(native.get('assets'), dict)
            or native['assets'].get('launch_readiness') != proof['asset_sha256']):
        return False
    if profile is False:
        from .native_current_attachment import is_supported_direct_installation
        return is_supported_direct_installation(attachment)
    return profile in _RETAINED_PROFILES


def require_launch_reconciliation(attachment, proof):
    if not launch_reconciliation_available(attachment, proof):
        raise RuntimeError(
            'Launch reconciliation module is not qualified for this retained attachment; '
            'reconcile the installed launch asset before launch or payload loading'
        )
    return True


def launch_reconciliation_call_guard(proof):
    """Build an in-command guard so proof cannot go stale before a launch call."""
    if (not isinstance(proof, dict)
            or set(proof) != {'protocol', 'session_id', 'actor_unit', 'profile', 'asset_sha256'}
            or type(proof.get('protocol')) is not int
            or proof['protocol'] != LAUNCH_RECONCILIATION_PROTOCOL
            or not isinstance(proof.get('session_id'), str) or not proof['session_id']
            or type(proof.get('actor_unit')) is not int or proof['actor_unit'] < 1
            or proof.get('asset_sha256') != launch_readiness_sha256()
            or (proof.get('profile') is not False
                and (type(proof.get('profile')) is not str
                     or proof['profile'] not in _RETAINED_PROFILES))):
        raise RuntimeError('Launch reconciliation proof is malformed or stale')
    profile = proof['profile']
    profile_test = ('(n.profile or false)==false' if profile is False
                    else 'n.profile==' + json.dumps(profile))
    return (
        'local rt=assert(jev_fle_runtime);local n=assert(rt.native_installation);'
        'local a=assert(rt.agent_characters and rt.agent_characters[1]);'
        'local r=assert(storage.launch_readiness);'
        'local q=assert(rt.launch_reconciliation);'
        'assert(q.protocol==' + str(LAUNCH_RECONCILIATION_PROTOCOL) + ' '
        'and type(q.verify)=="function" and q.verify()==true);'
        'assert(r.load_reconciliation_protocol==' + str(LAUNCH_RECONCILIATION_PROTOCOL) + ' '
        'and rt.campaign==storage.campaign);'
        'assert(rt.jev_session_id==' + json.dumps(proof['session_id']) + ' '
        'and n.session_id==' + json.dumps(proof['session_id']) + ' '
        'and a.valid and a.unit_number==' + str(proof['actor_unit']) + ' '
        'and n.actor_unit==' + str(proof['actor_unit']) + ' '
        'and n.schema==' + json.dumps(NATIVE_SCHEMA) + ' '
        'and ' + profile_test + ');'
        'assert(type(n.assets)=="table" and n.assets.launch_readiness=='
        + json.dumps(proof['asset_sha256']) + ');'
    )


def prepare_launch_reconciliation_upgrade_command(attachment):
    """Prepare, but never send, a one-use exact-profile legacy module upgrade.

    The caller must separately authorize and execute the returned native
    command, then perform normal source-bound readback before constructing an
    adapter. No launch, transfer, receipt, attempt, or inventory operation is
    performed by preparing this text.
    """
    if (not isinstance(attachment, dict) or attachment.get('qualified') is not True
            or not isinstance(attachment.get('modules'), dict)):
        raise RuntimeError('Launch asset upgrade requires a qualified retained attachment')
    native = attachment.get('native_installation')
    profile = native.get('profile') if isinstance(native, dict) else None
    session_id = attachment.get('session_id')
    actor_unit = attachment.get('actor_unit')
    modules = attachment.get('modules')
    assets = native.get('assets') if isinstance(native, dict) else None
    expected_module_names = (set(PINNED_ASSETS) | {'connector_ownership'} | OPTIONAL_ASSETS)
    if (type(profile) is not str or profile not in _RETAINED_PROFILES
            or not isinstance(native, dict)
            or native.get('schema') != NATIVE_SCHEMA
            or type(native.get('session_id')) is not str
            or type(native.get('actor_unit')) is not int
            or native.get('session_id') != session_id
            or native.get('actor_unit') != actor_unit
            or not isinstance(session_id, str) or not session_id
            or type(actor_unit) is not int or actor_unit < 1
            or not isinstance(modules, dict) or set(modules) != expected_module_names
            or any(type(value) is not bool for value in modules.values())
            or not isinstance(assets, dict)
            or set(assets) != {name for name, present in modules.items() if present}
            or any(not isinstance(name, str) or not isinstance(value, str)
                   or len(value) != 64 or any(char not in '0123456789abcdef' for char in value)
                   or not _retained_profile_asset_matches(name, value, profile)
                   for name, value in assets.items())
            or attachment.get('modules', {}).get('launch_readiness') is not True
            or assets.get('launch_readiness') != PINNED_ASSETS['launch_readiness']):
        raise RuntimeError('Launch asset upgrade does not match a supported pinned legacy attachment')

    new_asset = launch_readiness_sha256()
    source = _asset_source('launch_readiness').read_text()
    session_literal = json.dumps(session_id)
    profile_literal = json.dumps(profile)
    old_hash_literal = json.dumps(PINNED_ASSETS['launch_readiness'])
    new_hash_literal = json.dumps(new_asset)
    actor_literal = str(actor_unit)
    manifest_literal = json.dumps(json.dumps(assets, sort_keys=True, separators=(',', ':')))
    probe_result = 'rcon.print(helpers.table_to_json({schema=1,qualified=ok==true,'
    if PROBE.count(probe_result) != 1:
        raise RuntimeError('Launch callback graph probe has an unexpected source shape')
    graph_check = (
        'local function __jev_launch_graph_ok()\n'
        + PROBE.partition(probe_result)[0]
        + 'return ok==true\nend; '
        'assert(__jev_launch_graph_ok()==true, "Retained callback graph is not qualified"); '
    )
    preflight = (
        'local rt=assert(jev_fle_runtime); local n=assert(rt.native_installation); '
        'local a=assert(rt.agent_characters and rt.agent_characters[1]); '
        'local old=assert(storage.launch_readiness); '
        'local attempts=assert(old.attempts); local receipts=assert(old.receipts); '
        'local expected_assets=helpers.json_to_table(' + manifest_literal + '); '
        'local asset_count,expected_asset_count=0,0; '
        'for name,value in pairs(n.assets) do '
        'asset_count=asset_count+1; assert(expected_assets[name]==value, '
        '"Native asset manifest changed after attachment readback"); end; '
        'for name,value in pairs(expected_assets) do '
        'expected_asset_count=expected_asset_count+1; assert(n.assets[name]==value, '
        '"Native asset manifest changed after attachment readback"); end; '
        'assert(asset_count==expected_asset_count, "Native asset manifest shape changed"); '
        'local old_campaign_launch=assert(storage.campaign.launch); '
        'local old_runtime_launch_readiness=rt.launch_readiness; '
        'local old_launch=assert(old.launch); '
        'local old_protocol=old.load_reconciliation_protocol; '
        'assert(rt.jev_session_id==' + session_literal + ' and a.valid '
        'and a.unit_number==' + actor_literal + '); '
        'assert(n.schema==' + json.dumps(NATIVE_SCHEMA) + ' and '
        'n.session_id==' + session_literal + ' and n.actor_unit==' + actor_literal + '); '
        'assert(n.profile==' + profile_literal + ' and '
        'n.assets.launch_readiness==' + old_hash_literal + '); '
        'assert(old.schema==1 and type(attempts)=="table" and type(receipts)=="table" '
        'and (old.load_reconciliation_protocol==nil or old.load_reconciliation_protocol==false)); '
        'assert(rt.launch_reconciliation==nil); '
    )
    completion = (
        'local completion_ok,completion_error=pcall(function() '
        'assert(storage.launch_readiness==old and old.attempts==attempts '
        'and old.receipts==receipts, "Launch guard upgrade replaced paid state"); '
        'assert(old.load_reconciliation_protocol==' + str(LAUNCH_RECONCILIATION_PROTOCOL) + ', '
        '"Launch guard protocol was not installed"); '
        'assert(n.assets.launch_readiness==' + old_hash_literal + ', '
        '"Launch asset manifest changed before guard verification"); '
        'n.assets.launch_readiness=' + new_hash_literal + '; '
        'assert(n.assets.launch_readiness==' + new_hash_literal + ' '
        'and __jev_launch_graph_ok()==true, "Launch callback graph did not survive upgrade"); '
        'assert(type(rt.launch_reconciliation)=="table" and '
        'rt.launch_reconciliation.protocol==' + str(LAUNCH_RECONCILIATION_PROTOCOL) + ' '
        'and type(rt.launch_reconciliation.verify)=="function" '
        'and rt.launch_reconciliation.verify()==true, "Launch guard capability did not verify"); '
        'end); '
        'if not completion_ok then '
        'n.assets.launch_readiness=' + old_hash_literal + '; '
        'old.launch=old_launch; old.load_reconciliation_protocol=old_protocol; '
        'storage.campaign.launch=old_campaign_launch; '
        'rt.launch_readiness=old_runtime_launch_readiness; rt.launch_reconciliation=nil; '
        'error(completion_error or "Launch guard upgrade completion failed",0) '
        'end; '
    )
    upgrade = (
        'rt.__launch_reconciliation_upgrade_target=' + new_hash_literal + '; '
        'rt.__launch_reconciliation_upgrade=true; '
        'local upgrade_ok,upgrade_error=pcall(function()\n' + source + '\nend); '
        'rt.__launch_reconciliation_upgrade=nil; '
        'rt.__launch_reconciliation_upgrade_target=nil; '
        'assert(upgrade_ok, upgrade_error or "Launch guard source rejected the retained graph"); '
    )
    return 'do\n' + graph_check + preflight + '\n' + upgrade + completion + '\nend'


def connector_snapshot_sha256(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(
        snapshot, sort_keys=True, separators=(',', ':'), allow_nan=False
    ).encode('utf-8')).hexdigest()


def connector_snapshot_observation_command(session_id: str, actor_unit: int) -> str:
    """Build the coherent observer qualification command."""
    return '/sc ' + (
        'local rt=assert(jev_fle_runtime);local c=assert(rt.campaign);'
        'local b=assert(rt.connector_observer_bridge_v1);'
        'local a=assert(rt.agent_characters and rt.agent_characters[1]);'
        'assert(rt.jev_session_id==' + json.dumps(session_id)
        + ' and a.valid and a.unit_number==' + str(actor_unit) + ');'
        'assert(b.protocol==1 and b.snapshot_qualified~=true);'
        'local factory=c.observe();local ownership=assert(factory.connector_ownership);'
        'local direct=c.observe_connector_ownership();'
        'local function same(x,y) if type(x)~=type(y) then return false end;'
        'if type(x)~="table" then return x==y end;'
        'for k,v in pairs(x) do if not same(v,y[k]) then return false end end;'
        'for k in pairs(y) do if x[k]==nil then return false end end;return true end;'
        'assert(ownership.protocol==1 and ownership.session_id==rt.jev_session_id '
        'and ownership.tick==factory.tick and direct.tick==factory.tick '
        'and same(ownership,direct));'
        'local function copy(value) if type(value)~="table" then return value end;'
        'local result={};for key,item in pairs(value) do result[copy(key)]=copy(item) end;'
        'return result end;'
        'rcon.print(helpers.table_to_json({schema=1,session_id=rt.jev_session_id,'
        'actor_unit=a.unit_number,tick=factory.tick,connector_ownership=ownership}));'
        'b.snapshot_ownership=copy(ownership);b.snapshot_tick=factory.tick;'
        'b.snapshot_qualified=true'
    )


def _normalize_empty_connector_routes(snapshot):
    """Normalize Lua's empty table JSON encoding, while rejecting nonempty arrays later."""
    if isinstance(snapshot, dict) and snapshot.get('routes') == []:
        snapshot = {**snapshot, 'routes': {}}
    return snapshot



def connector_ownership_only_snapshot_command_v1(session_id: str, actor_unit: int) -> str:
    """Build a bounded direct query; never enter the factory/coal observer chain."""
    if not (isinstance(session_id, str) and session_id
            and type(actor_unit) is int and actor_unit >= 1):
        raise ValueError('Invalid connector snapshot owner binding')
    session = json.dumps(session_id)
    actor = str(actor_unit)
    return '/sc ' + (
        'local rt=assert(jev_fle_runtime);local c=assert(rt.campaign);'
        'local f=assert(rt.fair);local solid=assert(rt.solid_routes);'
        'local b=assert(rt.connector_observer_bridge_v1);'
        'local n=assert(rt.native_installation);local cb=assert(n.callbacks);'
        'local a=assert(rt.agent_characters and rt.agent_characters[1]);'
        'local p=assert(f.actor());local ledger=assert(c.connector_ledger);'
        'assert(rt.jev_session_id==' + session + ' and n.session_id==' + session
        + ' and n.actor_unit==' + actor + ');'
        'assert(n.schema=="jev.native-installation.v2" and a.valid and a.unit_number==' + actor
        + ' and p.connected and p.character==a and p.force==a.force and p.surface==a.surface);'
        'assert(b.protocol==1 and b.observer==c.observe and c.observe==solid.observer '
        'and b.snapshot_qualified~=true);'
        'assert(cb.observe==c.observe and cb.connector_observe==c.observe_connector_ownership '
        'and type(c.observe_connector_ownership)=="function");'
        'assert(ledger.protocol==1 and type(ledger.routes)=="table" '
        'and ledger.active==nil and next(ledger.routes)==nil);'
        'local tick=game.tick;local ownership=c.observe_connector_ownership();'
        'assert(type(ownership)=="table" and ownership.protocol==1 '
        'and ownership.session_id==rt.jev_session_id and ownership.tick==tick '
        'and ownership.active==nil and type(ownership.routes)=="table" '
        'and next(ownership.routes)==nil);'
        'assert(game.tick==tick and ledger.active==nil and next(ledger.routes)==nil);'
        'local function copy(value) if type(value)~="table" then return value end;'
        'local result={};for key,item in pairs(value) do result[copy(key)]=copy(item) end;'
        'return result end;'
        'b.snapshot_ownership=copy(ownership);b.snapshot_tick=tick;b.snapshot_qualified=true;'
        'rcon.print(helpers.table_to_json({schema=1,session_id=rt.jev_session_id,'
        'actor_unit=a.unit_number,tick=tick,connector_ownership=ownership}))'
    )


def connector_snapshot_command(session_id: str, actor_unit: int, *, mode: str = "coherent") -> str:
    """Select an exact supported v1 qualification command; never accept arbitrary Lua."""
    if mode == "coherent":
        return connector_snapshot_observation_command(session_id, actor_unit)
    if mode == "ownership-only-v1":
        return connector_ownership_only_snapshot_command_v1(session_id, actor_unit)
    raise ValueError("Unknown connector snapshot command mode")


def connector_snapshot_command_sha256s(session_id: str, actor_unit: int) -> frozenset[str]:
    """Allow only the two exact source-built v1 command variants."""
    return frozenset(hashlib.sha256(connector_snapshot_command(
        session_id, actor_unit, mode=mode).encode('utf-8')).hexdigest()
        for mode in ('coherent', 'ownership-only-v1'))


def _private_read(path: Path, *, maximum: int) -> bytes:
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('Connector snapshot witness is a symlink')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError('Private connector evidence cannot be read') from exc
    try:
        opened = os.fstat(fd)
        current = path.stat()
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                or opened.st_size > maximum
                or (os.name == 'posix' and (opened.st_uid != os.geteuid()
                    or stat.S_IMODE(opened.st_mode) != 0o600))):
            raise RuntimeError('Connector snapshot witness identity or privacy changed')
        data = os.read(fd, maximum + 1)
        if len(data) > maximum:
            raise RuntimeError('Connector snapshot witness exceeds its bound')
        return data
    finally:
        os.close(fd)


def _connector_witness(path, result: dict, receipt_path) -> None:
    if path is None:
        path = os.environ.get('JEV_NATIVE_CONNECTOR_OBSERVER_WITNESS')
    if not path or Path(path).name != CONNECTOR_OBSERVER_WITNESS_NAME:
        raise RuntimeError('Qualified connector snapshot requires its fixed durable witness')
    if Path(path).is_symlink() or not Path(path).is_file():
        raise RuntimeError('Qualified connector snapshot requires its fixed durable witness')
    if not receipt_path:
        receipt_path = os.environ.get('JEV_NATIVE_ATTACHMENT_RECEIPT')
    if not receipt_path:
        raise RuntimeError('Connector snapshot witness requires the original attachment receipt')
    receipt_sha256 = hashlib.sha256(_private_read(Path(receipt_path), maximum=65536)).hexdigest()
    raw = _private_read(Path(path), maximum=65536)
    if not raw.endswith(b'\n'):
        raise RuntimeError('Connector snapshot witness is incomplete')

    def unique_pairs(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('duplicate key')
            out[key] = value
        return out

    try:
        rows = [json.loads(line.decode('utf-8'), object_pairs_hook=unique_pairs)
                for line in raw.splitlines()]
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError('Connector snapshot witness requires reconciliation') from exc
    if (len(rows) not in {2, 3} or not all(isinstance(row, dict) for row in rows)):
        raise RuntimeError('Connector snapshot witness requires reconciliation')
    first = rows[0]
    expected_keys = {'schema', 'phase', 'session_id', 'actor_unit',
                     'checkpoint_sha256', 'receipt_sha256', 'lock_identity',
                     'bridge_asset_sha256', 'command_sha256'}
    if (set(first) != expected_keys
            or first['schema'] != 'jev.native-connector-observer-witness.v1'
            or first['phase'] != 'dispatching'
            or first['session_id'] != result['session_id']
            or first['actor_unit'] != result['actor_unit']
            or first['receipt_sha256'] != receipt_sha256
            or first['bridge_asset_sha256'] != result['native_installation']['assets'].get(
                'connector_observer_bridge_v1')
            or not isinstance(first['command_sha256'], str)
            or first['command_sha256'] not in connector_snapshot_command_sha256s(
                result['session_id'], result['actor_unit'])
            or any(not (isinstance(first[key], str) and len(first[key]) == 64
                        and all(c in '0123456789abcdef' for c in first[key]))
                   for key in ('checkpoint_sha256', 'receipt_sha256',
                               'bridge_asset_sha256', 'command_sha256'))
            or not isinstance(first['lock_identity'], dict)
            or set(first['lock_identity']) != {'device', 'inode'}
            or any(type(value) is not int or value < 0
                   for value in first['lock_identity'].values())):
        raise RuntimeError('Connector snapshot witness identity changed')
    phases = [row.get('phase') for row in rows[1:]]
    if phases not in (['qualified'], ['unknown', 'qualified']):
        raise RuntimeError('Connector snapshot witness is not durably qualified')
    if len(rows) == 3 and (set(rows[1]) != {'phase', 'reason'}
                           or not isinstance(rows[1]['reason'], str)
                           or not 1 <= len(rows[1]['reason']) <= 128):
        raise RuntimeError('Connector snapshot witness ambiguity record is malformed')
    proof = rows[-1]
    snapshot = result['connector_snapshot_ownership']
    if (set(proof) != {'phase', 'snapshot_tick', 'snapshot_sha256'}
            or type(proof['snapshot_tick']) is not int
            or proof['snapshot_tick'] != result['connector_snapshot_tick']
            or not isinstance(snapshot, dict)
            or proof['snapshot_sha256'] != connector_snapshot_sha256(snapshot)):
        raise RuntimeError('Connector snapshot witness differs from the emitted native snapshot')


def _asset_source(name, profile=False):
    if name == 'observation_v2':
        if profile == LEGACY_OBSERVATION_PROFILE:
            return files('jev_factorio').joinpath('lua/observation_v2.lua')
        asset = (EXPANDED_OBSERVATION_ASSET if profile == EXPANDED_OBSERVATION_PROFILE
                 else WATER_ORIGIN_OBSERVATION_ASSET)
        return files('jev_factorio').joinpath('lua/' + asset)
    return files('jev_factorio').joinpath('lua/' + name + '.lua')


def _retained_profile_asset_matches(name, value, profile):
    """Keep the original profile pins while recognizing only the new launch asset."""
    if name == 'launch_readiness':
        return (type(value) is str
                and value in {PINNED_ASSETS[name], launch_readiness_sha256()})
    if profile in {LEGACY_OBSERVATION_PROFILE, EXPANDED_OBSERVATION_PROFILE,
                   WATER_ORIGIN_OBSERVATION_PROFILE}:
        expected = {
            LEGACY_OBSERVATION_PROFILE: LEGACY_OBSERVATION_SHA256,
            EXPANDED_OBSERVATION_PROFILE: EXPANDED_OBSERVATION_SHA256,
            WATER_ORIGIN_OBSERVATION_PROFILE: WATER_ORIGIN_OBSERVATION_SHA256,
        }[profile] if name == 'observation_v2' else PINNED_ASSETS.get(name)
    elif profile in {MANUAL_CYCLE_PROFILE, MANUAL_CYCLE_BOOTSTRAP_PROFILE}:
        expected = (
            WATER_ORIGIN_OBSERVATION_SHA256 if name == 'observation_v2'
            else connector_ownership_sha256() if name == 'connector_ownership'
            else manual_journal_sha256() if name == 'coal_manual_journal_v1'
            else connector_observer_bridge_sha256() if name == 'connector_observer_bridge_v1'
            else hashlib.sha256(_asset_source(BOOTSTRAP_MODULE).read_bytes()).hexdigest()
                 if name == BOOTSTRAP_MODULE and profile == MANUAL_CYCLE_BOOTSTRAP_PROFILE
            else PINNED_ASSETS.get(name)
        )
    elif profile == LEGACY_MANUAL_CYCLE_PROFILE:
        expected = (
            WATER_ORIGIN_OBSERVATION_SHA256 if name == 'observation_v2'
            else connector_ownership_sha256() if name == 'connector_ownership'
            else manual_journal_sha256() if name == 'coal_manual_journal_v1'
            else PINNED_ASSETS.get(name)
        )
    elif profile in {CLOSED_WORLD_PROFILE, BOOTSTRAP_PROFILE}:
        expected = (
            WATER_ORIGIN_OBSERVATION_SHA256 if name == 'observation_v2'
            else connector_ownership_sha256() if name == 'connector_ownership'
            else manual_journal_sha256() if name == 'coal_manual_journal_v1'
            else cycle_journal_sha256() if name == 'coal_manual_cycle_v2'
            else connector_observer_bridge_sha256() if name == 'connector_observer_bridge_v1'
            else hashlib.sha256(_asset_source(BOOTSTRAP_MODULE).read_bytes()).hexdigest()
                 if name == BOOTSTRAP_MODULE and profile == BOOTSTRAP_PROFILE
            else PINNED_ASSETS.get(name)
        )
    else:
        expected = PINNED_ASSETS.get(name)
    return value == expected


LAUNCH_RECONCILIATION_PROBE = r'''local rt=jev_fle_runtime
local capability=rt and rt.launch_reconciliation
local n=rt and rt.native_installation
local a=rt and rt.agent_characters and rt.agent_characters[1]
local loaded=false
if capability and capability.protocol==1 and type(capability.verify)=="function" then
    local ok,value=pcall(capability.verify)
    loaded=ok and value==true
end
rcon.print(helpers.table_to_json({
    protocol=loaded and capability.protocol or false,
    session_id=rt and rt.jev_session_id or false,
    actor_unit=a and a.unit_number or false,
    profile=n and n.profile or false,
    asset_sha256=n and n.assets and n.assets.launch_readiness or false
}))'''


PROBE = r'''local rt=jev_fle_runtime
local c=rt and rt.campaign
local f=rt and rt.fair
local l=rt and rt.launch_readiness
local j=c and c.craft_jobs
local b=rt and rt.output_buffers
local i=rt and rt.input_routes
local s=rt and rt.solid_routes
local q=rt and rt.coal_supply
local o=rt and rt.mining_outposts
local p=rt and rt.production_sites
local x=rt and rt.successors
local mj=rt and rt.coal_manual_journal_v1
local cj=rt and rt.coal_manual_cycle_v2
local bo=rt and rt.bootstrap_output_v1
local n=rt and rt.native_installation
local nc=n and n.callbacks
local a=rt and rt.agent_characters and rt.agent_characters[1]
local player=rt and game.get_player(rt.jev_bound_player_index or 1)
local function good(x) return type(x)=="function" end
local ok=rt and type(rt.jev_session_id)=="string" and #rt.jev_session_id>0
    and a and a.valid and player and player.connected and player.character==a
    and player.force==a.force and player.surface==a.surface and not player.cheat_mode
    and game.speed==1 and not game.tick_paused and f and good(f.actor)
    and good(f.bind) and good(f.observe) and good(f.place) and good(f.tick_handler)
    and good(f.path_handler)
    and script.get_event_handler(defines.events.on_script_path_request_finished)==f.path_handler
if c then ok=ok and good(c.observe) and good(c.transfer) and good(c.configure)
    and l and l.schema==1 and c.launch==l.launch and c.craft==l.craft
    and good(l.observer)
    and script.get_event_handler(defines.events.on_player_mined_entity)==l.mined_handler
    and (c.observation_snapshot==nil or good(c.observation_snapshot))
    and (c.observation_snapshot_v2==nil or good(c.observation_snapshot_v2))
end
if j then ok=ok and l and good(j.observe_wrapper) and good(j.previous_observe)
    and j.previous_observe==l.observer end
if not j then ok=ok
    and script.get_event_handler(defines.events.on_pre_player_crafted_item)==nil
    and script.get_event_handler(defines.events.on_player_cancelled_crafting)==nil
    and script.get_event_handler(defines.events.on_player_crafted_item)==nil end
if j then ok=ok
    and script.get_event_handler(defines.events.on_pre_player_crafted_item)==j.pre_handler
    and script.get_event_handler(defines.events.on_player_cancelled_crafting)==j.cancel_handler
    and script.get_event_handler(defines.events.on_player_crafted_item)==j.crafted_handler end
if b then ok=ok and l and b.protocol==1 and good(b.observer) and good(b.transfer)
    and b.previous_observe==(j and j.observe_wrapper or l.observer)
    and b.previous_transfer==l.transfer
    and script.get_event_handler(defines.events.on_tick)==b.tick_handler end
if not b then ok=ok and script.get_event_handler(defines.events.on_tick)==f.tick_handler end
if i then ok=ok and b and i.protocol==1 and i.previous_observe==b.observer
    and i.previous_transfer==b.transfer and good(i.observer) and good(i.transfer) end
    if s then ok=ok and s.protocol==1 and s.implementation_revision==4
    and s.contract_family=="straight-solid-corridor-v1"
    and s.reservation_contract=="full-corridor-manhattan-v1" and type(s.coal_api)=="table"
    and c.observe==s.observer and c.transfer==s.transfer and c.configure==s.configure end
local bridge=rt and rt.connector_observer_bridge_v1
if bridge then ok=ok and bridge.protocol==1 and good(bridge.previous_observe)
    and good(bridge.observer) and bridge.observer==c.observe
    and good(bridge.previous_solid_observer)
    and bridge.previous_observe==bridge.previous_solid_observer
    and s and c.observe==s.observer end
if q then ok=ok and q.revision==4 and s and s.coal==q
    and c.prepare_coal_source==q.prepare and c.build_coal_source==q.build
    and type(q.admission_evidence)=="boolean" end
if o then ok=ok and o.protocol==1 and i and good(c.observe_mining_outposts) end
if p then ok=ok and p.protocol==1 and i and good(c.observe_production_sites) end
if x then ok=ok and x.protocol==1 and i and b and p and j and not o
    and c.successors_enabled==true and good(c.observe_successors) end
if mj then ok=ok and mj.protocol==1 and mj.session_id==rt.jev_session_id
    and mj.actor_index==player.index
    and mj.actor_unit==a.unit_number and mj.surface_index==a.surface.index
    and mj.force_index==a.force.index and good(mj.tick_handler)
    and good(mj.begin) and good(mj.finish) and good(mj.observe) end
if cj then ok=ok and cj.protocol==2 and mj and cj.session_id==rt.jev_session_id
    and cj.actor_index==player.index and cj.actor_unit==a.unit_number
    and cj.surface_index==a.surface.index and cj.force_index==a.force.index
    and good(cj.tick_handler) and good(cj.combined_tick_handler)
    and good(cj.begin) and good(cj.begin_delivery)
    and good(cj.finish_delivery) and good(cj.finish) end
if bo then ok=ok and bo.protocol==1 and bo.phase=="ready" and bo.session_id==rt.jev_session_id
    and bo.actor_unit==a.unit_number and bo.surface_index==a.surface.index
    and bo.force_index==a.force.index and good(bo.observe) and good(bo.extract)
    and good(bo.place) and good(bo.bind_paid) and good(bo.reconcile_pending)
    and good(bo.complete_install) and f.bootstrap_place==bo.place
    and bo.original_place==f.place and bo.original_transfer==c.transfer end
if c and c.connector_ledger then ok=ok and c.connector_ledger.protocol==1
    and type(c.connector_ledger.routes)=="table" and good(c.connector_begin)
    and good(c.connector_finish) and good(c.connector_page)
    and good(c.observe_connector_ownership) end
if n then ok=ok and type(n.assets)=="table" and type(nc)=="table"
    and nc.fair_tick==(f and f.tick_handler)
    and nc.observe==(c and c.observe)
    and nc.snapshot_v1==(c and c.observation_snapshot)
    and nc.snapshot_v2==(c and c.observation_snapshot_v2)
    and nc.transfer==(c and c.transfer)
    and nc.configure==(c and c.configure)
    and nc.connector_begin==(c and c.connector_begin)
    and nc.connector_finish==(c and c.connector_finish)
    and nc.connector_page==(c and c.connector_page)
    and nc.connector_observe==(c and c.observe_connector_ownership)
    and nc.journal_tick==(mj and mj.tick_handler)
    and nc.cycle_tick==(cj and cj.combined_tick_handler)
    and nc.bootstrap_observe==(bo and bo.observe)
    and nc.bootstrap_extract==(bo and bo.extract)
    and nc.bootstrap_place==(bo and bo.place)
    and nc.bootstrap_bind_paid==(bo and bo.bind_paid)
    and nc.bootstrap_reconcile_pending==(bo and bo.reconcile_pending)
    and nc.bootstrap_complete_install==(bo and bo.complete_install) end
if c and s then ok=ok and c.observe==s.observer and c.transfer==s.transfer
elseif c and i then ok=ok and c.observe==i.observer and c.transfer==i.transfer
elseif c and b then ok=ok and c.observe==b.observer and c.transfer==b.transfer
elseif c and j then ok=ok and c.observe==j.observe_wrapper and c.transfer==l.transfer
elseif c then ok=ok and c.observe==l.observer and c.transfer==l.transfer end
-- This probe must remain metadata-only. The retained solid-routes observer
-- updates in-memory route and coal diagnostics, so its emitted snapshot is
-- qualified by a separate journaled native observation after migration.
local connector_observer_bridge_qualified=false
if c and c.connector_ledger and good(c.observe_connector_ownership)
    and bridge and s and nc then
    connector_observer_bridge_qualified=bridge.protocol==1
        and bridge.observer==c.observe and c.observe==s.observer
        and nc.observe==c.observe
        and bridge.previous_observe==bridge.previous_solid_observer
end
local connector_snapshot_qualified=bridge and bridge.snapshot_qualified==true or false
local connector_snapshot_tick=bridge and bridge.snapshot_tick or 0
local connector_snapshot_ownership=bridge and bridge.snapshot_ownership or false
local modules={fair_actions=true,factory=c~=nil,launch_readiness=l~=nil,
    observation=c and good(c.observation_snapshot) or false,
    observation_v2=c and good(c.observation_snapshot_v2) or false,craft_jobs=j~=nil,
    output_buffers=b~=nil,input_routes=i~=nil,production_sites=p~=nil,
    mining_outposts=o~=nil,solid_routes=s~=nil,coal_supply=q~=nil,
    successors=x~=nil,connector_ownership=c and c.connector_ledger~=nil or false,
    coal_manual_journal_v1=mj~=nil,coal_manual_cycle_v2=cj~=nil,
    connector_observer_bridge_v1=bridge~=nil,bootstrap_output_v1=bo~=nil}
rcon.print(helpers.table_to_json({schema=1,qualified=ok==true,
    session_id=rt and rt.jev_session_id or "",actor_unit=a and a.unit_number or 0,
    modules=modules,solid_intents=s and s.intents or {},coal_targets=q and q.targets or {},
    coal_admission_evidence=q and q.admission_evidence or false,
    connector_observer_bridge_qualified=connector_observer_bridge_qualified,
    connector_snapshot_qualified=connector_snapshot_qualified,
    connector_snapshot_tick=connector_snapshot_tick,
    connector_snapshot_ownership=connector_snapshot_ownership,
    native_installation=n and {schema=n.schema,session_id=n.session_id,
        actor_unit=n.actor_unit,assets=n.assets,profile=n.profile or false} or false}))'''


NATIVE_SCHEMA = 'jev.native-installation.v2'
CALLBACKS_EXPR = (
    '{fair_tick=f and f.tick_handler or nil, '
    'observe=c and c.observe or nil, '
    'snapshot_v1=c and c.observation_snapshot or nil, '
    'snapshot_v2=c and c.observation_snapshot_v2 or nil, '
    'transfer=c and c.transfer or nil, '
    'configure=c and c.configure or nil, '
    'connector_begin=c and c.connector_begin or nil, '
    'connector_finish=c and c.connector_finish or nil, '
    'connector_page=c and c.connector_page or nil, '
    'connector_observe=c and c.observe_connector_ownership or nil, '
    'journal_tick=jev_fle_runtime.coal_manual_journal_v1 '
    'and jev_fle_runtime.coal_manual_journal_v1.tick_handler or nil, '
    'cycle_tick=jev_fle_runtime.coal_manual_cycle_v2 '
    'and jev_fle_runtime.coal_manual_cycle_v2.combined_tick_handler or nil, '
    'bootstrap_observe=jev_fle_runtime.bootstrap_output_v1 and jev_fle_runtime.bootstrap_output_v1.observe or nil, '
    'bootstrap_extract=jev_fle_runtime.bootstrap_output_v1 and jev_fle_runtime.bootstrap_output_v1.extract or nil, '
    'bootstrap_place=jev_fle_runtime.bootstrap_output_v1 and jev_fle_runtime.bootstrap_output_v1.place or nil, '
    'bootstrap_bind_paid=jev_fle_runtime.bootstrap_output_v1 and jev_fle_runtime.bootstrap_output_v1.bind_paid or nil, '
    'bootstrap_reconcile_pending=jev_fle_runtime.bootstrap_output_v1 and jev_fle_runtime.bootstrap_output_v1.reconcile_pending or nil, '
    'bootstrap_complete_install=jev_fle_runtime.bootstrap_output_v1 and jev_fle_runtime.bootstrap_output_v1.complete_install or nil}'
)


@lru_cache(maxsize=1)
def _installer_scripts():
    """Recognize only exact bundled installers; never mark arbitrary RCON Lua."""
    root = files('jev_factorio').joinpath('lua')
    names = tuple(PINNED_ASSETS) + ('connector_ownership', 'coal_manual_journal_v1',
                                    'connector_observer_bridge_v1')
    scripts = {}
    for name in names:
        source = _asset_source(name)
        if not source.is_file():
            continue
        body = source.read_text()
        scripts[body] = (name,)
        scripts['do\n' + body + '\nend'] = (name,)
    scripts['\n'.join('do\n' + root.joinpath(name + '.lua').read_text() + '\nend'
                      for name in ('input_routes', 'production_sites'))] = (
                          'input_routes', 'production_sites')
    return scripts


def prepare_install_command(script: str, attachment=None) -> str:
    """Append a source receipt in the same Lua command as an installer.

    A failed or partially executed command cannot acquire a complete receipt.
    Installation is supported only for the enumerated source assets. Each
    later installer extends the live receipt without changing prior hashes.
    """
    names = _installer_scripts().get(script)
    if names is None:
        return script
    if attachment is not None:
        raise RuntimeError('Native module reinstallation during resume requires reconciliation')
    hashes = {name: hashlib.sha256(_asset_source(name).read_bytes()).hexdigest()
              for name in names}
    encoded = json.dumps(hashes, sort_keys=True)
    return script + '\n' + (
        'local rt=assert(jev_fle_runtime); '
        'local a=assert(rt.agent_characters and rt.agent_characters[1]); '
        'assert(a.valid and a.unit_number and type(rt.jev_session_id)=="string"); '
        'local n=rt.native_installation; '
        'if not n then n={schema="' + NATIVE_SCHEMA + '",'
        'session_id=rt.jev_session_id,actor_unit=a.unit_number,assets={}}; '
        'rt.native_installation=n end; '
        'assert(n.schema=="' + NATIVE_SCHEMA + '" and '
        'n.session_id==rt.jev_session_id and n.actor_unit==a.unit_number); '
        'local incoming=helpers.json_to_table(' + json.dumps(encoded) + '); '
        'for name,hash in pairs(incoming) do '
        'assert(n.assets[name]==nil or n.assets[name]==hash, '
        '"Native asset revision changed"); n.assets[name]=hash end; '
        'local c=rt.campaign; local f=rt.fair; '
        'n.callbacks=' + CALLBACKS_EXPR
    )


def readback(client, *, receipt_path=None, connector_witness_path=None,
             checkpoint_binding=None,
             completed_craft=None, background_craft=None, output_commitments=None,
             allow_legacy_manual_cycle_repair=False,
             allow_unqualified_connector_bridge=False):
    result = decode_native(client.send_command('/sc ' + PROBE))
    if background_craft is not None:
        from .native_current_attachment import is_current_direct_installation
        if (completed_craft is not None or not isinstance(result, dict)
                or not is_current_direct_installation(result)
                or not isinstance(checkpoint_binding, dict) or not checkpoint_binding.get('routes')):
            raise RuntimeError('Background craft requires the current checkpoint-bound direct profile')
    if isinstance(result, dict) and result.get('connector_snapshot_qualified') is True:
        result['connector_snapshot_ownership'] = _normalize_empty_connector_routes(
            result.get('connector_snapshot_ownership'))
    if (not isinstance(result, dict) or set(result) != {
            'schema', 'qualified', 'session_id', 'actor_unit', 'modules',
            'solid_intents', 'coal_targets', 'coal_admission_evidence',
            'connector_observer_bridge_qualified', 'connector_snapshot_qualified',
            'connector_snapshot_tick', 'connector_snapshot_ownership', 'native_installation'}
            or result['schema'] != 1 or result['qualified'] is not True
            or type(result['connector_observer_bridge_qualified']) is not bool
            or type(result['connector_snapshot_qualified']) is not bool
            or type(result['connector_snapshot_tick']) is not int
            or result['connector_snapshot_tick'] < 0
            or (result['connector_snapshot_qualified'] is False
                and (result['connector_snapshot_tick'] != 0
                     or result['connector_snapshot_ownership'] is not False))
            or (result['connector_snapshot_qualified'] is True
                and (not isinstance(result['connector_snapshot_ownership'], dict)
                     or result['connector_snapshot_ownership'].get('protocol') != 1
                     or result['connector_snapshot_ownership'].get('session_id')
                        != result['session_id']
                     or result['connector_snapshot_ownership'].get('tick')
                        != result['connector_snapshot_tick']
                     or not isinstance(result['connector_snapshot_ownership'].get('routes'), dict)))
            or not isinstance(result['session_id'], str) or not result['session_id']
            or type(result['actor_unit']) is not int or result['actor_unit'] < 1
            or not isinstance(result['modules'], dict)
            or set(result['modules']) != set(PINNED_ASSETS) | {'connector_ownership'} | OPTIONAL_ASSETS
            or any(type(flag) is not bool for flag in result['modules'].values())):
        raise RuntimeError('Existing native callback installation requires reconciliation')
    native = result['native_installation']
    if native is not False:
        if (not isinstance(native, dict)
                or set(native) != {'schema', 'session_id', 'actor_unit', 'assets', 'profile'}
                or native['schema'] != NATIVE_SCHEMA
                or native['session_id'] != result['session_id']
                or native['actor_unit'] != result['actor_unit']
                or not isinstance(native['assets'], dict)
                or set(native['assets']) != {
                    name for name, present in result['modules'].items() if present}
                or any(type(value) is not str or len(value) != 64
                       or any(c not in '0123456789abcdef' for c in value)
                       for value in native['assets'].values())):
            raise RuntimeError('Native installed-source manifest requires reconciliation')
        profile = native['profile']
        if profile in {LEGACY_OBSERVATION_PROFILE, EXPANDED_OBSERVATION_PROFILE,
                       WATER_ORIGIN_OBSERVATION_PROFILE}:
            observation_hash = {
                LEGACY_OBSERVATION_PROFILE: LEGACY_OBSERVATION_SHA256,
                EXPANDED_OBSERVATION_PROFILE: EXPANDED_OBSERVATION_SHA256,
                WATER_ORIGIN_OBSERVATION_PROFILE: WATER_ORIGIN_OBSERVATION_SHA256,
            }[profile]
            if (result['modules']['connector_ownership']
                    or result['modules']['successors']
                    or native['assets'].get('factory') != PINNED_ASSETS['factory']
                    or native['assets'].get('observation_v2') != observation_hash
                    or any(not _retained_profile_asset_matches(name, value, profile)
                           for name, value in native['assets'].items())):
                raise RuntimeError('Observation migration profile requires reconciliation')
        elif profile in {MANUAL_CYCLE_PROFILE, MANUAL_CYCLE_BOOTSTRAP_PROFILE}:
            if (result['modules']['connector_ownership'] is not True
                    or result['modules'][BOOTSTRAP_MODULE] != (profile == MANUAL_CYCLE_BOOTSTRAP_PROFILE)
                    or result['modules']['successors']
                    or result['modules']['coal_manual_journal_v1'] is not True
                    or result['modules']['coal_manual_cycle_v2'] is not False
                    or result['modules']['connector_observer_bridge_v1'] is not True
                    or result['connector_observer_bridge_qualified'] is not True
                    or native['assets'].get('factory') != PINNED_ASSETS['factory']
                    or native['assets'].get('observation_v2') != WATER_ORIGIN_OBSERVATION_SHA256
                    or native['assets'].get('connector_ownership') != connector_ownership_sha256()
                    or native['assets'].get('coal_manual_journal_v1') != manual_journal_sha256()
                    or native['assets'].get('connector_observer_bridge_v1') != connector_observer_bridge_sha256()
                    or any(not _retained_profile_asset_matches(name, value, profile)
                           for name, value in native['assets'].items())):
                raise RuntimeError('Manual-cycle migration profile requires reconciliation')
        elif profile == LEGACY_MANUAL_CYCLE_PROFILE:
            if (not allow_legacy_manual_cycle_repair
                    or result['modules']['connector_ownership'] is not True
                    or result['modules']['successors']
                    or result['modules']['coal_manual_journal_v1'] is not True
                    or result['modules']['coal_manual_cycle_v2'] is not False
                    or result['modules']['connector_observer_bridge_v1'] is not False
                    or result['connector_observer_bridge_qualified'] is not False
                    or native['assets'].get('factory') != PINNED_ASSETS['factory']
                    or native['assets'].get('observation_v2') != WATER_ORIGIN_OBSERVATION_SHA256
                    or native['assets'].get('connector_ownership') != connector_ownership_sha256()
                    or native['assets'].get('coal_manual_journal_v1') != manual_journal_sha256()
                    or any(not _retained_profile_asset_matches(name, value, profile)
                           for name, value in native['assets'].items())):
                raise RuntimeError('Legacy v5 observer requires the one-use repair migration')
        elif profile in {CLOSED_WORLD_PROFILE, BOOTSTRAP_PROFILE}:
            if (result['modules']['connector_ownership'] is not True
                    or result['modules'][BOOTSTRAP_MODULE] != (profile == BOOTSTRAP_PROFILE)
                    or result['modules']['successors']
                    or result['modules']['coal_manual_journal_v1'] is not True
                    or result['modules']['coal_manual_cycle_v2'] is not True
                    or result['modules']['connector_observer_bridge_v1'] is not True
                    or result['connector_observer_bridge_qualified'] is not True
                    or native['assets'].get('factory') != PINNED_ASSETS['factory']
                    or native['assets'].get('observation_v2') != WATER_ORIGIN_OBSERVATION_SHA256
                    or native['assets'].get('connector_ownership') != connector_ownership_sha256()
                    or native['assets'].get('coal_manual_journal_v1') != manual_journal_sha256()
                    or native['assets'].get('coal_manual_cycle_v2') != cycle_journal_sha256()
                    or native['assets'].get('connector_observer_bridge_v1') != connector_observer_bridge_sha256()
                    or any(not _retained_profile_asset_matches(name, value, profile)
                           for name, value in native['assets'].items())):
                raise RuntimeError('Closed-world migration profile requires reconciliation')
        elif profile is not False:
            raise RuntimeError('Unknown native installation profile requires reconciliation')
        from .native_current_attachment import is_supported_direct_installation
        supported_direct = is_supported_direct_installation(result)
        if output_commitments is not None and not supported_direct:
            raise RuntimeError('Paid output attachment requires a supported direct installation')
        if (result['modules']['connector_ownership'] and not supported_direct
                and not (profile == LEGACY_MANUAL_CYCLE_PROFILE
                         and allow_legacy_manual_cycle_repair)
                and (result['modules']['connector_observer_bridge_v1'] is not True
                     or result['connector_observer_bridge_qualified'] is not True)):
            raise RuntimeError('Connector ownership observer bridge requires reconciliation')
        for name, expected in native['assets'].items():
            source = _asset_source(name, profile)
            if profile in {LEGACY_OBSERVATION_PROFILE, EXPANDED_OBSERVATION_PROFILE,
                           WATER_ORIGIN_OBSERVATION_PROFILE, MANUAL_CYCLE_PROFILE,
                           LEGACY_MANUAL_CYCLE_PROFILE,
                           CLOSED_WORLD_PROFILE, BOOTSTRAP_PROFILE, MANUAL_CYCLE_BOOTSTRAP_PROFILE} \
                    and name not in {'observation_v2', 'connector_ownership',
                                     'coal_manual_journal_v1', 'coal_manual_cycle_v2',
                                     'connector_observer_bridge_v1'}:
                continue  # Exact e759 hash is pinned; retained closure is reused.
            if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise RuntimeError('Native Lua source differs from installed manifest')
        if supported_direct:
            from .native_current_attachment import qualify_current_connector_snapshot
            return qualify_current_connector_snapshot(
                client, result, checkpoint_binding=checkpoint_binding, completed_craft=completed_craft,
                background_craft=background_craft, output_commitments=output_commitments)
        if (result['modules']['connector_ownership']
                and not (profile == LEGACY_MANUAL_CYCLE_PROFILE
                         and allow_legacy_manual_cycle_repair)
                and not allow_unqualified_connector_bridge):
            if result['connector_snapshot_qualified'] is not True:
                raise RuntimeError('Connector snapshot has not passed its one-use native qualification')
            _connector_witness(connector_witness_path, result, receipt_path)
        return result
    if result['modules']['connector_ownership']:
        raise RuntimeError('Unversioned connector ownership requires reconciliation')
    receipt_path = receipt_path or os.environ.get('JEV_NATIVE_ATTACHMENT_RECEIPT')
    if not receipt_path:
        raise RuntimeError('Existing native installation requires a source-bound attachment receipt')
    path = Path(receipt_path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('Native attachment receipt is missing or is a symlink')
    if os.name == 'posix':
        stat = path.stat()
        if stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
            raise RuntimeError('Native attachment receipt must be owned by the controller and private')
    try:
        receipt = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('Native attachment receipt cannot be read') from exc
    if (not isinstance(receipt, dict)
            or set(receipt) != {'schema', 'session_id', 'actor_unit',
                                'installed_source_commit', 'installed_source_tree',
                                'installed_assets'}
            or receipt['schema'] != 'jev.native-attachment.v1'
            or receipt['session_id'] != result['session_id']
            or receipt['actor_unit'] != result['actor_unit']
            or receipt['installed_source_commit'] != PINNED_SOURCE_COMMIT
            or receipt['installed_source_tree'] != PINNED_SOURCE_TREE
            or receipt['installed_assets'] != PINNED_ASSETS):
        raise RuntimeError('Native attachment receipt does not match the retained session and source')
    return result


def require_asset(attachment, name):
    if attachment is None:
        return False
    if attachment['modules'].get(name) is not True:
        raise RuntimeError('Required native capability was not installed in this session')
    manifest = attachment.get('native_installation')
    profile = manifest.get('profile') if isinstance(manifest, dict) else False
    asset = _asset_source(name, profile).read_bytes()
    expected = (manifest['assets'].get(name) if isinstance(manifest, dict)
                else PINNED_ASSETS.get(name))
    if (isinstance(manifest, dict) and type(profile) is str
            and profile in _RETAINED_PROFILES
            and name in PINNED_ASSETS
            and name not in {'observation_v2', 'connector_ownership',
                             'coal_manual_journal_v1', 'coal_manual_cycle_v2',
                             'connector_observer_bridge_v1'}):
        valid_expected = {PINNED_ASSETS.get(name)}
        if name == 'launch_readiness':
            valid_expected.add(launch_readiness_sha256())
        if type(expected) is not str or expected not in valid_expected:
            raise RuntimeError('Retained native asset differs from the legacy profile')
        return True
    if expected is None or hashlib.sha256(asset).hexdigest() != expected:
        raise RuntimeError('Native Lua source differs from the verified installed revision')
    return True
