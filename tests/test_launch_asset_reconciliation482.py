"""Offline attached-runtime qualification for launch reconciliation v2."""
from __future__ import annotations

from copy import deepcopy
import json
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest

from jev_factorio.backends import native_attachment
from jev_factorio.backends.native_attachment import (
    CLOSED_WORLD_PROFILE,
    EXPANDED_OBSERVATION_PROFILE,
    LEGACY_MANUAL_CYCLE_PROFILE,
    LEGACY_OBSERVATION_PROFILE,
    MANUAL_CYCLE_BOOTSTRAP_PROFILE,
    MANUAL_CYCLE_PROFILE,
    PINNED_ASSETS,
    WATER_ORIGIN_OBSERVATION_PROFILE,
    BOOTSTRAP_PROFILE,
    LAUNCH_RECONCILIATION_PROBE,
    NATIVE_SCHEMA,
    launch_reconciliation_available,
    launch_reconciliation_call_guard,
    launch_readiness_sha256,
    prepare_launch_reconciliation_upgrade_command,
    require_asset,
)
from jev_factorio.backends.native_factory import NativeFactory


SESSION = "launch-v2-offline-session"
ACTOR = 17
RETAINED_PROFILES = (
    LEGACY_OBSERVATION_PROFILE,
    EXPANDED_OBSERVATION_PROFILE,
    WATER_ORIGIN_OBSERVATION_PROFILE,
    LEGACY_MANUAL_CYCLE_PROFILE,
    MANUAL_CYCLE_PROFILE,
    CLOSED_WORLD_PROFILE,
    BOOTSTRAP_PROFILE,
    MANUAL_CYCLE_BOOTSTRAP_PROFILE,
)
CATALOG = {
    "version": "2.0.77", "mods": {"base": "2.0.77", "core": "2.0.77"},
    "recipes": {}, "technologies": {}, "machines": {},
    "hand_categories": {}, "stack_sizes": {},
}


def _attachment(profile, *, launch_asset=None):
    modules = {name: False for name in PINNED_ASSETS}
    modules.update({
        "factory": True, "launch_readiness": True,
        "connector_ownership": False, "successors": False,
        "coal_manual_journal_v1": False, "coal_manual_cycle_v2": False,
        "connector_observer_bridge_v1": False, "bootstrap_output_v1": False,
    })
    assets = {"factory": PINNED_ASSETS["factory"],
              "launch_readiness": launch_asset or PINNED_ASSETS["launch_readiness"]}
    return {
        "schema": 1, "qualified": True, "session_id": SESSION, "actor_unit": ACTOR,
        "modules": modules,
        "native_installation": {
            "schema": NATIVE_SCHEMA, "session_id": SESSION, "actor_unit": ACTOR,
            "profile": profile, "assets": assets,
        },
    }


def _proof(profile, *, asset=None):
    return {
        "protocol": 1, "session_id": SESSION, "actor_unit": ACTOR,
        "profile": profile, "asset_sha256": asset or launch_readiness_sha256(),
    }


def _from_lua(value, lua_type):
    if lua_type(value) != "table":
        return value
    items = list(value.items())
    integer_keys = [key for key, _ in items]
    if (items and all(type(key) is int and key >= 1 for key in integer_keys)
            and sorted(integer_keys) == list(range(1, len(items) + 1))):
        return [_from_lua(item, lua_type) for _, item in sorted(items)]
    return {key: _from_lua(item, lua_type) for key, item in items}


def _launch_runtime(*, load_candidate=False, old_manifest=False, profile=False):
    lupa = pytest.importorskip("lupa.lua52")
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(Path(__file__).parent.joinpath("fixtures/launch_runtime.lua").read_text())
    runtime = lua.globals().storage
    runtime.jev_session_id = SESSION
    lua.globals().character.unit_number = ACTOR
    runtime.agent_characters = lua.table_from({1: lua.globals().character})
    runtime.campaign = runtime.campaign
    runtime.fair = runtime.fair
    asset_hash = (PINNED_ASSETS["launch_readiness"] if old_manifest
                  else launch_readiness_sha256())
    runtime.native_installation = lua.table_from({
        "schema": NATIVE_SCHEMA, "session_id": SESSION, "actor_unit": ACTOR,
        "profile": profile,
        "assets": {"launch_readiness": asset_hash},
    }, recursive=True)
    lua.globals().jev_fle_runtime = runtime

    output = []

    def table_to_json(value):
        return json.dumps(_from_lua(value, lupa.lua_type), sort_keys=True,
                          separators=(",", ":"))

    lua.globals().helpers = lua.table_from({"table_to_json": table_to_json})
    lua.globals().rcon = lua.table_from({"print": output.append})
    if load_candidate:
        source = files("jev_factorio").joinpath("lua/launch_readiness.lua").read_text()
        lua.execute(source)
    return lua, output


class _RconClient:
    def __init__(self, lua, output, *, catalog_first=False, after_probe=None):
        self.lua = lua
        self.output = output
        self.catalog_first = catalog_first
        self.after_probe = after_probe
        self.commands = []

    def send_command(self, command):
        self.commands.append(command)
        if self.catalog_first:
            self.catalog_first = False
            return json.dumps(CATALOG)
        script = command.removeprefix("/sc ")
        if script == LAUNCH_RECONCILIATION_PROBE:
            self.output.clear()
            self.lua.execute(script)
            result = self.output.pop()
            if self.after_probe is not None:
                self.lua.execute(self.after_probe)
                self.after_probe = None
            return result
        self.lua.execute(script)
        return '{"ok":true}'


def _backend(lua, output, attachment=None, *, catalog_first=False, after_probe=None):
    client = _RconClient(lua, output, catalog_first=catalog_first, after_probe=after_probe)
    fair = SimpleNamespace(approaches=[])
    fair.approach = lambda *args, **kwargs: fair.approaches.append((args, kwargs))
    backend = SimpleNamespace(
        _native_attachment=attachment,
        _instance=SimpleNamespace(rcon_client=client),
        _fair=fair,
        _tools={},
    )
    return backend, client


@pytest.mark.parametrize("profile", RETAINED_PROFILES)
def test_retained_profiles_keep_exact_pins_but_require_current_live_launch_capability(profile):
    attached = _attachment(profile)
    assert require_asset(attached, "factory")
    assert require_asset(attached, "launch_readiness")
    assert not launch_reconciliation_available(attached, _proof(profile))

    attached["native_installation"]["assets"]["launch_readiness"] = launch_readiness_sha256()
    assert require_asset(attached, "launch_readiness")
    assert launch_reconciliation_available(attached, _proof(profile))

    attached["native_installation"]["assets"]["launch_readiness"] = "0" * 64
    with pytest.raises(RuntimeError, match="legacy profile"):
        require_asset(attached, "launch_readiness")


@pytest.mark.parametrize("change", [
    "missing-protocol", "boolean-protocol", "unknown-profile", "list-profile",
    "wrong-session", "boolean-actor", "wrong-asset", "extra-field",
])
def test_live_launch_capability_proof_fails_closed_on_malformed_or_mismatched_fields(change):
    attached = _attachment(LEGACY_OBSERVATION_PROFILE,
                           launch_asset=launch_readiness_sha256())
    proof = _proof(LEGACY_OBSERVATION_PROFILE)
    if change == "missing-protocol": proof.pop("protocol")
    elif change == "boolean-protocol": proof["protocol"] = True
    elif change == "unknown-profile": proof["profile"] = "unknown-profile"
    elif change == "list-profile": proof["profile"] = []
    elif change == "wrong-session": proof["session_id"] = "other"
    elif change == "boolean-actor": proof["actor_unit"] = True
    elif change == "wrong-asset": proof["asset_sha256"] = "0" * 64
    elif change == "extra-field": proof["unreviewed"] = True
    assert not launch_reconciliation_available(attached, proof)
    with pytest.raises(RuntimeError, match="not qualified"):
        native_attachment.require_launch_reconciliation(attached, proof)


def test_direct_install_proof_does_not_accept_a_retained_profile_or_stale_asset():
    assert launch_reconciliation_available(None, _proof(False))
    assert not launch_reconciliation_available(None, _proof(LEGACY_OBSERVATION_PROFILE))
    assert not launch_reconciliation_available(None, _proof(False, asset="0" * 64))


def test_upgrade_builder_is_prepare_only_and_refuses_nonexact_profile_or_current_asset():
    legacy = _attachment(LEGACY_OBSERVATION_PROFILE)
    command = prepare_launch_reconciliation_upgrade_command(legacy)
    assert "storage.launch_readiness" in command
    assert "rt.launch_reconciliation.verify()==true" in command
    assert launch_readiness_sha256() in command
    assert PINNED_ASSETS["launch_readiness"] in command
    assert "campaign.launch(" not in command

    unknown = deepcopy(legacy)
    unknown["native_installation"]["profile"] = "unknown"
    with pytest.raises(RuntimeError, match="supported pinned"):
        prepare_launch_reconciliation_upgrade_command(unknown)
    current = deepcopy(legacy)
    current["native_installation"]["assets"]["launch_readiness"] = launch_readiness_sha256()
    with pytest.raises(RuntimeError, match="supported pinned"):
        prepare_launch_reconciliation_upgrade_command(current)


@pytest.mark.parametrize("asset_is_current", [False, True])
@pytest.mark.parametrize("profile", RETAINED_PROFILES)
def test_old_attached_constructor_reuses_old_module_but_launch_fails_before_approach(
        profile, asset_is_current):
    lua, output = _launch_runtime(load_candidate=False, old_manifest=not asset_is_current,
                                  profile=profile)
    lua.execute("""
        jev_fle_runtime.launch_readiness={schema=1,legacy=true}
        jev_fle_runtime.campaign.launch=function(role) launches=launches+1 end
    """)
    asset = launch_readiness_sha256() if asset_is_current else PINNED_ASSETS["launch_readiness"]
    attachment = _attachment(profile, launch_asset=asset)
    backend, client = _backend(lua, output, attachment, catalog_first=True)
    native = NativeFactory(backend)
    assert len(client.commands) == 1  # Constructor only reads the catalog; it does not reinstall.
    with pytest.raises(RuntimeError, match="not qualified"):
        native.execute("factory_launch", {"role": "recipe:rocket-part"})
    assert len(client.commands) == 2
    assert client.commands[1] == "/sc " + LAUNCH_RECONCILIATION_PROBE
    assert backend._fair.approaches == []
    assert lua.globals().launches == 0
    assert not any("launch_readiness.lua" in command for command in client.commands)


@pytest.mark.parametrize("profile", RETAINED_PROFILES)
@pytest.mark.parametrize("function", [
    "launch", "load_launch_payload", "prepare_launch_pad", "build_launch_pad", "begin_launch_fish",
])
def test_direct_launch_lua_entrypoints_reject_old_retained_module_before_native_call(function, profile):
    lua, output = _launch_runtime(load_candidate=False, old_manifest=True, profile=profile)
    lua.execute("jev_fle_runtime.launch_readiness={schema=1,legacy=true}")
    backend, client = _backend(lua, output, _attachment(profile))
    native = NativeFactory.__new__(NativeFactory)
    native.backend = backend

    with pytest.raises(RuntimeError, match="not qualified"):
        native.call(function)

    assert client.commands == ["/sc " + LAUNCH_RECONCILIATION_PROBE]
    assert lua.globals().launches == 0 and lua.globals().builds == 0 and lua.globals().mines == 0


@pytest.mark.parametrize("profile", RETAINED_PROFILES)
@pytest.mark.parametrize("action,parameters", [
    ("factory_launch_pad", {"site": "landing:1", "receipt": "launch:pad:1"}),
    ("factory_launch_fish", {"target": "fish:1", "receipt": "launch:fish:1"}),
    ("factory_launch_payload", {"role": "recipe:rocket-part", "silo_unit": 30,
                                 "rocket_unit": 31, "item": "raw-fish",
                                 "receipt": "launch:load:1"}),
])
def test_paid_launch_dispatch_rejects_old_retained_module_before_approach(action, parameters, profile):
    lua, output = _launch_runtime(load_candidate=False, old_manifest=True, profile=profile)
    lua.execute("jev_fle_runtime.launch_readiness={schema=1,legacy=true}")
    backend, client = _backend(lua, output, _attachment(profile))
    native = NativeFactory.__new__(NativeFactory)
    native.backend = backend

    with pytest.raises(RuntimeError, match="not qualified"):
        native.execute(action, parameters)

    assert client.commands == ["/sc " + LAUNCH_RECONCILIATION_PROBE]
    assert backend._fair.approaches == []
    assert lua.globals().builds == 0 and lua.globals().mines == 0


@pytest.mark.parametrize("profile", RETAINED_PROFILES)
def test_current_loaded_module_admits_matching_retained_attachment(profile):
    lua, output = _launch_runtime(load_candidate=True, profile=profile)
    lua.execute("add_pad();cargo.insert{name='raw-fish',count=1}")
    attached = _attachment(profile, launch_asset=launch_readiness_sha256())
    backend, client = _backend(lua, output, attached)
    native = NativeFactory.__new__(NativeFactory)
    native.backend = backend

    native.call("launch", "recipe:rocket-part")

    assert client.commands[0] == "/sc " + LAUNCH_RECONCILIATION_PROBE
    assert "q.verify()==true" in client.commands[1]
    assert lua.globals().launches == 1
    assert lua.eval("storage.launch_readiness.attempts.launch~=nil")


def test_current_direct_module_call_checks_capability_atomically_and_launches_once():
    lua, output = _launch_runtime(load_candidate=True)
    lua.execute("""
        add_pad();cargo.insert{name="raw-fish",count=1}
    """)
    backend, client = _backend(lua, output)
    native = NativeFactory.__new__(NativeFactory)
    native.backend = backend

    native.call("launch", "recipe:rocket-part")

    assert len(client.commands) == 2
    assert client.commands[0] == "/sc " + LAUNCH_RECONCILIATION_PROBE
    assert "q.verify()==true" in client.commands[1]
    assert "n.assets.launch_readiness" in client.commands[1]
    assert lua.globals().launches == 1
    assert lua.eval("storage.launch_readiness.attempts.launch~=nil")
    assert lua.eval("storage.launch_readiness.receipts.launch~=nil")


def test_current_proof_rejects_stale_runtime_module_state_pointer():
    lua, output = _launch_runtime(load_candidate=True)
    lua.execute("jev_fle_runtime.launch_readiness={schema=1,legacy=true}")
    lua.execute(LAUNCH_RECONCILIATION_PROBE)

    proof = json.loads(output.pop())
    assert proof["protocol"] is False
    assert not launch_reconciliation_available(None, proof)


@pytest.mark.parametrize("mutation", [
    "jev_fle_runtime.native_installation.assets.launch_readiness='" + "0" * 64 + "'",
    "jev_fle_runtime.native_installation.profile='changed-profile'",
    "jev_fle_runtime.campaign.launch=function() launches=launches+100 end",
])
def test_launch_call_rechecks_live_binding_inside_same_native_command(mutation):
    lua, output = _launch_runtime(load_candidate=True)
    lua.execute("add_pad();cargo.insert{name='raw-fish',count=1}")
    backend, client = _backend(lua, output, after_probe=mutation)
    native = NativeFactory.__new__(NativeFactory)
    native.backend = backend

    with pytest.raises(Exception):
        native.call("launch", "recipe:rocket-part")

    assert len(client.commands) == 2
    assert "q.verify()==true" in client.commands[1]
    assert lua.globals().launches == 0
    assert lua.eval("storage.launch_readiness.attempts.launch==nil")


def _legacy_upgrade_runtime():
    lua, output = _launch_runtime(load_candidate=False, old_manifest=True)
    lua.execute("""
        jev_fle_runtime.launch_readiness={schema=1,legacy=true}
        local attempt={receipt="load:unchanged",silo_unit=30,rocket_unit=31,
                       item="raw-fish",tick=100}
        local receipt={kind="load",session_id="test-factory",actor_unit=10,tick=100,
                       item="raw-fish",quantity=1,silo_unit=30,rocket_unit=31}
        storage.launch_readiness={schema=1,serial=9,session_id="test-factory",actor_unit=10,
            attempts={load=attempt},receipts={['load:unchanged']=receipt},
            surface_index=1,force_index=1,pending_fish={receipt="fish:held"},
            pad_offer={id="landing:held"},silo_unit=30}
        original_state=storage.launch_readiness
        original_attempts=original_state.attempts
        original_receipts=original_state.receipts
        original_pending=original_state.pending_fish
        original_offer=original_state.pad_offer
    """)
    state = lua.globals().storage.launch_readiness
    state.session_id = SESSION
    state.actor_unit = ACTOR
    state.receipts["load:unchanged"].session_id = SESSION
    state.receipts["load:unchanged"].actor_unit = ACTOR
    lua.globals().jev_fle_runtime.native_installation.profile = LEGACY_OBSERVATION_PROFILE
    lua.globals().jev_fle_runtime.native_installation.assets.launch_readiness = (
        PINNED_ASSETS["launch_readiness"])
    return lua, output


def test_explicit_upgrade_preserves_retained_paid_state_and_only_changes_launch_asset_binding():
    from test_native_optional_profiles import (
        _ReadbackClient, _install_retained_launch_world, _legacy_profile_runtime,
    )
    from jev_factorio.backends.native_attachment import readback

    lua, output = _legacy_profile_runtime(LEGACY_OBSERVATION_PROFILE)
    _install_retained_launch_world(lua)
    lua.execute("builds=0;mines=0")
    lua.execute('jev_fle_runtime.campaign.load_launch_payload({role="recipe:rocket-part",'
                'silo_unit=101,rocket_unit=202,item="raw-fish",receipt="load:retained-actual"})')
    lua.execute("""
        local r=storage.launch_readiness
        r.pending_fish={receipt="fish:retained",target="fish:1"}
        r.pad_offer={id="landing:retained"};r.serial=9;r.silo_unit=101
        original_state=r;original_attempts=r.attempts;original_receipts=r.receipts
        original_pending=r.pending_fish;original_offer=r.pad_offer
        original_load_attempt=r.attempts.load
        original_load_receipt=r.receipts["load:retained-actual"]
    """)
    attachment = readback(_ReadbackClient(lua, output, execute_snapshot=False))
    backend, client = _backend(lua, output, attachment, catalog_first=True)
    native = NativeFactory(backend)
    assert len(client.commands) == 1  # Retained attachment is read-only at construction.
    command = native.prepare_launch_reconciliation_upgrade()
    assert len(client.commands) == 1  # Preparing is not authority to send the upgrade.
    native.command(command)
    assert len(client.commands) == 2

    assert lua.eval("storage.launch_readiness==original_state")
    assert lua.eval("storage.launch_readiness.attempts==original_attempts")
    assert lua.eval("storage.launch_readiness.receipts==original_receipts")
    assert lua.eval("storage.launch_readiness.pending_fish==original_pending")
    assert lua.eval("storage.launch_readiness.pad_offer==original_offer")
    assert lua.eval("storage.launch_readiness.serial==9 and storage.launch_readiness.silo_unit==101")
    assert lua.eval("storage.launch_readiness.attempts.load==original_load_attempt")
    assert lua.eval("storage.launch_readiness.receipts['load:retained-actual']==original_load_receipt")
    assert lua.eval("storage.launch_readiness.receipts['load:retained-actual'].quantity==1")
    assert lua.eval("storage.launch_readiness.attempts.launch==nil")
    assert lua.eval("storage.launch_readiness.receipts.launch==nil")
    assert lua.globals().launches == 0 and lua.globals().builds == 0 and lua.globals().mines == 0
    assert lua.globals().storage.native_installation.assets.launch_readiness == launch_readiness_sha256()

    output.clear()
    lua.execute(LAUNCH_RECONCILIATION_PROBE)
    proof = json.loads(output.pop())
    updated = deepcopy(attachment)
    updated["native_installation"]["assets"]["launch_readiness"] = launch_readiness_sha256()
    assert launch_reconciliation_available(updated, proof)

    upgraded_backend, upgraded_client = _backend(lua, output, updated)
    upgraded_native = NativeFactory.__new__(NativeFactory)
    upgraded_native.backend = upgraded_backend
    upgraded_native.call("launch", "recipe:rocket-part")
    assert upgraded_client.commands[0] == "/sc " + LAUNCH_RECONCILIATION_PROBE
    assert lua.globals().launches == 1
    assert lua.eval("storage.launch_readiness.attempts.load.receipt=='load:retained-actual'")
    assert lua.eval("storage.launch_readiness.receipts['load:retained-actual'].quantity==1")


@pytest.mark.parametrize("mutation", ["session", "actor", "profile", "asset"])
def test_upgrade_preflight_rejects_changed_identity_before_loading_candidate(mutation):
    lua, _ = _legacy_upgrade_runtime()
    attachment = _attachment(LEGACY_OBSERVATION_PROFILE)
    command = prepare_launch_reconciliation_upgrade_command(attachment)
    lua.execute({
        "session": "jev_fle_runtime.jev_session_id='changed-session'",
        "actor": "jev_fle_runtime.agent_characters[1].unit_number=18",
        "profile": "storage.native_installation.profile='unknown-profile'",
        "asset": "storage.native_installation.assets.launch_readiness='" + "0" * 64 + "'",
    }[mutation])
    with pytest.raises(Exception):
        lua.execute(command)
    assert lua.eval("jev_fle_runtime.launch_reconciliation==nil")
    assert lua.eval("storage.launch_readiness.attempts.load.receipt=='load:unchanged'")
    assert lua.eval("storage.launch_readiness.attempts.launch==nil")
    assert lua.globals().launches == 0 and lua.globals().builds == 0
