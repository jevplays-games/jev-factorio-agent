"""Exercise qualification against the bundled optional Lua installers."""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import zlib
from copy import deepcopy
from importlib.resources import files

import pytest

from jev_factorio.backends.native_attachment import (
    BOOTSTRAP_MODULE,
    BOOTSTRAP_PROFILE,
    CALLBACKS_EXPR,
    CLOSED_WORLD_PROFILE,
    EXPANDED_OBSERVATION_PROFILE,
    EXPANDED_OBSERVATION_ASSET,
    EXPANDED_OBSERVATION_SHA256,
    LEGACY_MANUAL_CYCLE_PROFILE,
    LEGACY_OBSERVATION_PROFILE,
    LEGACY_OBSERVATION_SHA256,
    LAUNCH_RECONCILIATION_PROBE,
    MANUAL_CYCLE_BOOTSTRAP_PROFILE,
    MANUAL_CYCLE_PROFILE,
    PINNED_ASSETS,
    PROBE, _installer_scripts, connector_observer_bridge_sha256,
    connector_ownership_sha256, cycle_journal_sha256,
    manual_journal_sha256, prepare_install_command,
    prepare_launch_reconciliation_upgrade_command, readback,
    launch_readiness_sha256,
    WATER_ORIGIN_OBSERVATION_ASSET,
    WATER_ORIGIN_OBSERVATION_PROFILE,
    WATER_ORIGIN_OBSERVATION_SHA256,
)
from jev_factorio.backends.native_current_attachment import (
    current_connector_snapshot_command,
    source_bound_direct_profile,
)
from lupa.lua52 import LuaRuntime, lua_type


SESSION = "offline-optional-profile-session"
ACTOR = 17
ROOT = files("jev_factorio").joinpath("lua")

_LEGACY_LAUNCH_SOURCE_SHA256 = "b01fe73bc12055d7fc831f84d097094096610033e4d7a1195d623ea190c03bf5"
_LEGACY_LAUNCH_SOURCE_B64 = (
    'eNq9W1lv5MYRftevIOgHD3c5tKRgvYa1bcAJbMCAExhB8rRQCIrsGdHikDQPrSbC+LenqvpuNkfygSzsXU6zzzq+uprbbfR9'
    'PYxTdFeMfHudXWbv30dDVz7wKera5phF/+ii/VC005hGdTtO8BQd6rZu92k08mmChzEq74t2z8eoG6KBT0PNx+yi6cqiicri'
    '0Bf1vo1YVIwjH6bNOHVDseeZepNcqJammNvyPh94UdUtH0cYs/oKVnoey3t+KNhVCvPWRcMu02Ka+KGfRvZ8Sgde8lo8n+Rm'
    'hjMzXsjtDZmcFuaN4n+349z33TDxKhIjIrOHYW6n+sDjRE4/1k2XD13D076o6IHFsIu6518Lim77YpjiNJ6nuqmn49dN0cJU'
    'e2iuYjnHDtaY6q6N+q5up02fIEHnoY2en1ifPaVH+Pt4inhb+QPKDrYzbur2MbmI4I88Mh/nZgIaUFu9Ax4+RtM9b6MdkDBP'
    'R2iIgA8Djcz2fMrLrp04TpUkUdXROPyj2Jf9Mhe4e8baukE+WC1x2w2Hook9ypXFsO8i2StO9JRicx/HrC0O/JZt3N8492Xy'
    'dszoZDQIjk1HF4OJLmLMRYAgev1Nog4/lgOIRFZAh0eeH7pqzFDuf2WxkPxYkEZOvSuakev1kF64L0Ow5WxIMFwH+8GkOHcc'
    'AZNVQ9kNfGUN/1zTMPPQqWC1bpAnUiwxBwXC/7gQU/7LXAOZormFPda7GjiCO4vkmW1x6ZviyAemlGQH58wCK07HnmtF/pk/'
    '5iMsBLvL6yoBKRgBAtq9OHm4F9ACheTvNTS0e6Va8n2s+QXKqIcQ2XxptDswFl6LtiGPkc9tPTEmjpkBbA3QDE/YnLfz4Y4P'
    'eglahoaO87ArSp7XbcWf9GjZmlGr7AlCsuhHbaJX6kyuWKUxsq5A70BFJJ5WN8A70Maybng0FHD+AYhQkMzzSVKJg/wYhbLI'
    'kTpHDpMmfRUhPAKk7jFD1EiXR1f6a8u46BWScomMOSDjRvRyQK37NPrr7mCVnMgH1iff1Q2chlfPqHigdwhAWxtuU9qaw6K0'
    'qQ9AqeuTLemf4VofyBZ8e7ir93M3j2p3AANV1H1q+TDe172jR4KPDAd/vLpV4iy5GxJk8Sp7BIgUAisbxDadfdqvLXbBDn9o'
    'xXhrfxbakj6hZcJBAoTcxa3JGDNdYeKfCndWJaBxollqsVVM5zBcLtR2U3R+4qoei77nxYCThySj4iP4HAU+k/E+bmBYWoPd'
    '16iBq+Bc61iOdhBGAFQNxaftrh7vXVRG8CVQ3m6jf93zqK/bFlATO0b3xQgrKMjqh66aywn8HWteQLexmHgD5o6fMykw+18N'
    'EEd6zBgda94Ak59AKZtjdHV5eRmNPYg5WLCatyDu0d/uefmAc6uZoOuMCF5UnwPmd93hBo0R0gJdoiNaU942oAgoQDB1jXTc'
    '2/TMLAEGZwDY2A3gbxQVuQW6ZVPxHVqWTLdkpF+5pbT5oajbRG2tLABsWpQA8imRaI9wSMCyKXozdgf+Jup2UQG2ooAz7Wog'
    'J5IdKCP8iVRN1HbttpqH4g4AEQkNNg06ARHQoNWl2TWQYDc3eAxclfqTF5HZ8KN7k0aZ04jTqoE5DdzERP+tpD9gSPkQJ98w'
    'Yg2Qlhgb9ELQKQS9DcAYvmIK/DOFXR+1G3lrCzS2OqKE3penXtjHghD6SQAIci4cUGyKzcsQ2uuXEll9e2c6BLAJVPqfInig'
    '/WqDVgtkciw7HnMBRf62EIh0T2/2JQhJ0uDbEC+EmCIzsEfyEnlTm8TK8uDyjHYpnu1JVNw0WL8kP85NrXwZ0d+FdHleTci5'
    'LR6LukHBlMS0zvyinoolcilh+Jyk8hir5EI986MKesPCEQf2dxzmlPoZt5kGh0KLjZJVjclyBmbjaUICSHNCjCZUl7Acyer6'
    'V9899bzEAKRrAcOLY9OBVUD6amgEcwPbwUau+kom0i5t64lr4GZCjgydOUBBdLNzGYhuHsBDcVj2jC0M/0otL9bx4SwPznbn'
    '0qkuH9gedpPh0ym0dgss5qibtLAKh5duvHrh+e2fyfZvLp2f4Apdf+W4GjLaoLdxsrT2Kib/iNu4FdEQvVAB+kf5cGuCl67n'
    'QyHCnYaMfCRncXzisIOAzvHwyCXekmcgJNCBXt4QtxV4keJATwX2WgQTeX7ToiUChHOzMtySVjXecghQqusmcT0RtSPXO7QM'
    'RdCQOAabhfDNgidPnwOskFkRdw8WR8PHJedrS8/fkKf8PTgUjfaQpN7Vo2KN7ZQqRcJ/8X9tDmV+Riycjz28ZIrHm7Ibp9HF'
    'bxMDO1jrwrcMb+2IFlHKyIiFUrQE4sS6QOld+8DZgAMLKDvpQX2yjHtMyDTm5f3cPuR73qLcQ9jyxA4Q72W7pkOGZ09f/OU6'
    'gdyP03jExpNjtzeCzZKE0jcVnrKgvLJN5/psQuFSn14myZm1REow7+apB8Ktr7fS75VreoRDvxKaIBQV1nE92oPFajwk69MK'
    'ciF0YMgXOjIeiAjv5rqpgDngaucIlkyZVP9Fdiha8L6DQIzucLfbcVdzyQdS2Jj54crS8+iaSsZkNJeaA5qFPVyIHLzJ1LFd'
    'ncAxViS0gmFLKiYfrlY36QZKeBzwzMdI0Gk/1BXs7b+cXWfRt7CBSrES7Hg3VBh/8Ogwj5Nx9CHLXDxAqPTIBwoY4ChIhd1u'
    'i7NFG3gBcdp9N+8JrmhJgBhe7+/BowTrMEafwGtK7KCmfEpLV4nE0RWZQM+ukzfX6ZkeR+phzXkH8RPIFCbGSx2LXb1/unqP'
    'YU9VVwXGc7WxZFrJ5QSQpxggWKJE+6Ho1XsdjyFAVU9se/VlCv9dIyJR09Fu8oxBzwBByqe3FSaMy+Pb6njyeiC8serpTQWd'
    'jm+qo20rNtIzoiOhBmPnD6ohWZG3XgqZQxDQNxxsw7wtergQ9l8VK3SGKLevHt5e3VhawJ7BYYqljH4dZ5nqZhQepz85CWNL'
    'iQLaijb+RXWlRAAQ5jXew3o310t4jfqbzfn6j9q+yCGp7F5BqRKwpqLHxnReB4Y16RZlAyIACHUtjOXrcnGEoDERJhVILZ4d'
    'FDZI7WpeOkA+ex4ZCLzI0119eXKCCCAGTmcdn36CwaHw/xw9sGOydHoECSpG7y2Q2C5gI/nP9Vuv13HR6wi9nPmlAtiaVmk1'
    'CykTrpBWTsrN9p/+mFIZ2RJahb8dlZI5zYBCWWLp+HA9BCCQyculLwd6Z1y4Pln1y25M6BIL652Fohc8ZyBPjLUnmRTSSa5l'
    'QlTXDzQYyAy+/p1hRaHPEOJWEM/urC0tBTFiNRqqkxQXL/rRIZMrHOofrQwpDoAKLBVOVAoAAiIgNhYM73kDsdOYidTV1OU/'
    'j0DsVzhGocOcksRlqDDn/1d2+mw5wybHVfyd7MLMbgcMg3W8PIu1zWXOThV8b818SN8I26KuLOe+1jIgaXSO8H9EVlRNrS+C'
    'lQDwTn6g+i44VcCFivKwmJLeg2MLPtgkXI/o2wiL1aD3X0Bk0kKVQ1Za0auhoJE4KvOpjiPLniV7mWZ0irSVXLJ8cVHi1qzw'
    'kxqWGQK5m9yqJPn9m3MiDdHDsiKzaqmOZ+dyRECm4qwyzKJs42QwafdeXs8WEjrKAVlSqBKTZNYZQWNipRtTTmHLtZ2LAIIn'
    'TkqK9DG5kT8zi0u6DcWIXemf9sFW1rMyO1oAbpl88PCEg9QpPEEr8psBRXgQHqJIl4lsmW2dbqSvhwsZMBGqZxWtJJv9HgJo'
    'JqygTMa/+L3uBqVJwIlq+G6SZY6IegUtRTkUO7Rk+S8zn7mIpdilvcq6DwodQ0H0GW8URlBtTiRudApOASKFBLihSFx9WLVt'
    'WASy8tHJSmXFcpfZO1gYAiGsX8kEMq4gKm9UFJOLyVXmHuOrXCXPbApnQd1V+q/yf8Knkrzo7sZyHjRQO3gDP3qZMvICgRDe'
    'CSnR4hKENtBcLszda2eRGEZbFgdxa/D047Tc+8/dHRPJ5hjkk8cp+JMT+NKxyMbEKdjKkZv9vb36ysqNELacuyjg7IuymYal'
    'KeJNA4uCSMEFINHxnZm8KSALBOH9HqzLaHaQUrtI8l+ebI7LBBKegLNn8YthvdbYFYf7dkoGLN5jDeiaIxUqJu/voFBiNmHK'
    'wVWrGgj8VJaHWsesoywTUpvGSSFLLjB34s7IaH/QRc4kXHCv05C5DeSX+43M/XnhTWxwkvboYKUQKuaKl072iDZhq3CoBD6A'
    'NvEms60bdXAkTXezW0M56+5Biijr4WezIex2UtLdw2IXqlppKvKBgExeaZPVJzH8bkaITvzwSl4oM+J4y9g7kffmT5O8b5Y4'
    'JSfvZeoAKUZRi129ZGPFYG1BlVJLOoqf+q3RkcUatl2Vg33reuNhCuzXmeaFyNGTy4DwSnHTiW+pQqAh9OJ1mpN6wpxYWX8w'
    'OCa+IPvzm10CHLQWZEArXstkusYuUtl9trgOgmkb1WqZRqv0pQobZXeA7K/r3QcrNjdW8VSWXYMlG7nX5U2XULQrNmnHMdY4'
    '5awXZcn7Se9YiPbo2m2qgIduBojwShXrKfwSJVLz6HeXdWYqVWqBU7VsXRDybrstt+PUq6XN0in45TvVRMQK3GcAgcWIuBvu'
    'YLPFPHWQ560RnAD9xMWcc14b8cdU5MWd1noU1adix+FeCx5rGTGKnhST4hOWe+hGziaxGs0VGRGrC7aKOhO7OvnrOtd6lncC'
    'Fnd41n0ye6feFRjjHEohCwSYkpnxwjMi1Q1GglKSbKlKLYHxxIdUUFIj7EUFZ3QinwNcN6qYdQ2CWoKEdsiHRIGBkm1r3JHz'
    'WzesxbgP8kXwvp/ZzEvzbtV8J7Zosrz0gQNOVosI0r9jImmhLk6oIxJj1VQTXPcfMfbRN4dVyb0mVImTF+NKIRPaqllMXFq6'
    'K5lMVDFogJt60Kqc3LwcdQJrvOryr+AkWRVvkKVOp7eY19e6cm91skdcqMmMxUILc8Zo6Ys/jknyLs8TLmP7ucsVcmVzg0IF'
    'bchCsAd4bwKiG6y1puDePBJzcTd/ntUyt3+YddFAXPYJ2hnLilBOEx9eZ/OMxZN3MDlJ6khXJ93UqG1NXmkA/qjlWVo8KUEi'
    'bYZcwchWVIwVg8JR/ws2CKroI1hRFSTb6YOFaLBno1m+hXfgN3AN7SXcDd4lNIoh1AAzj9+JLKP+dAONEdzUpOsiwI/7bsCU'
    'FiZhBT7StyPtVtLv259+yF5GHurrJLXObjOELEsS3CzuyIQTWhoWJCTYmKNgFVFHQ6yFO6qNLUZ42KM7uuMuzLQuAokrKwpv'
    '1U2wFIIcjOVBV5ybNKZZaMPyYo24DiNBzJ18cRXT3uBrN+NQ1DqsPqhNVUpEIUlFRsqiJzUwt6NHSdHF6n4h5zEEpBuMd8UE'
    'dz3MjSOfKH753YV5La7wJRhzNVx8ICFejR/psycTxIpSa23uJIl+4DFAtqSqMcJSHxzVGRVXWYzUjV1GqXtLFFzDP8UB3Ys3'
    '6kxOTT7APaKHRwaHQ4qIgoA2b6TFQe7IR0fgZRvz+3tMUt2cQX7ZXjYrxPG+FmHmW0HYA16uZCufgqWatcz+rMpFQIPw1r3N'
    'lS9srBucl6n7IdFlan9HA78KzGuQITIrUEHlRF8UUnoenkW68ESuNP6rv3o0oG++fjTAdXKvKn/KjI8hmAJNYEKgXsji2Tgg'
    'uaQYmE0iU3yj4Lv75F9SeEjhMotM+GgNSs5rBy67cs31RuzSppn3LRL1sKnofJvkLwykXPuwSGVACosYRHrhl8M3EKQBdpyL'
    'jbaV9OtahXX/yr3jIMJx+DL14+cq4fD5rUhi6lQDC3/oYuciTicnofOau6KIXeqmeyC19qKTp3NRyBgSP5sir/ArVpIG9K1n'
    'ukx54bnZn+eQaQYo34+95Bimggo6wBZU+ZUumzu34IWje7oIpdqsb5KsDIPA/omX923XdHus7jmfadxmiODFAMjlCqUAgsW1'
    'Qvom+hRa2PfHDVNXb72I4tnKZSg7ibszXgyqIkEToKEqmflaYRfbLPUg15XoS1pAlR727rSWsHQclu7BWp8gFOe4seFMZhQh'
    'k1nh1zu5+hxl8TFx9+lCZOOVvWIejDkpaMseIZBR4+LDcuabppvll8u+BbQM5sX/AKnBMh4='
)


def _legacy_launch_source():
    source = zlib.decompress(base64.b64decode(_LEGACY_LAUNCH_SOURCE_B64)).decode("utf-8")
    assert hashlib.sha256(source.encode("utf-8")).hexdigest() == _LEGACY_LAUNCH_SOURCE_SHA256
    return source
TARGETS = ["utility:boiler", "recipe:copper-plate"]
SOLID_INTENTS = [
    {"source": f"coal:{target}:chest", "target": target,
     "item": "coal", "destination": "fuel"}
    for target in TARGETS
]


def _from_lua(value):
    if lua_type(value) != "table":
        return value
    pairs = list(value.items())
    if all(type(key) is int and key >= 1 for key, _ in pairs):
        ordered = sorted(pairs)
        if [key for key, _ in ordered] == list(range(1, len(ordered) + 1)):
            return [_from_lua(item) for _, item in ordered]
    return {key: _from_lua(item) for key, item in pairs}


def _runtime():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(r'''
        local event_handlers, nth_handlers = {}, {}
        script = {active_mods={base="2.0.77",core="2.0.77"}}
        function script.on_event(event, handler)
            event_handlers[event] = handler
        end
        function script.get_event_handler(event) return event_handlers[event] end
        function script.on_nth_tick(tick, handler) nth_handlers[tick] = handler end
        defines = {
            events={on_tick=1,on_script_path_request_finished=2,on_player_mined_entity=3,
                on_pre_player_crafted_item=4,on_player_cancelled_crafting=5,
                on_player_crafted_item=6},
            controllers={character=1},
            direction={north=0,east=4,south=8,west=12,northeast=2,southeast=6,
                southwest=10,northwest=14},
            build_check_type={manual=1},
            inventory={fuel=1,assembling_machine_input=2,assembling_machine_output=3,
                furnace_source=4,furnace_result=5,lab_input=6,chest=7,
                character_main=8,cargo_landing_pad_main=9,rocket_silo_rocket=10},
            rocket_silo_status={rocket_ready=1},entity_status={}
        }
        local force={index=1,rockets_launched=0,technologies={}}
        function force.get_item_production_statistics()
            return {get_input_count=function() return 0 end}
        end
        local surface={index=1}
        function surface.find_entities_filtered() return {} end
        local actor={valid=true,unit_number=17,force=force,surface=surface}
        local player={index=1,connected=true,character=actor,force=force,surface=surface,
                      cheat_mode=false,position={x=0,y=0},crafting_queue_size=0}
        function player.get_main_inventory()
            return {get_contents=function() return {} end}
        end
        game={speed=1,tick_paused=false,tick=1234,
              get_player=function(index) if index==1 then return player end end}
        prototypes={item={}}
        jev_fle_runtime={jev_session_id="offline-optional-profile-session",
                         jev_bound_player_index=1,agent_characters={[1]=actor}}
        storage=jev_fle_runtime
        local function copy(value)
            if type(value) ~= "table" then return value end
            local result={};for key,item in pairs(value) do result[copy(key)]=copy(item) end
            return result
        end
        helpers={json_to_table=function(raw) return copy(require_json_to_table(raw)) end}
    ''')

    def json_to_lua(raw):
        return lua.table_from(json.loads(raw), recursive=True)

    def json_encode(value):
        return json.dumps(_from_lua(value), sort_keys=True, separators=(",", ":"))

    output = []
    lua.globals().require_json_to_table = json_to_lua
    lua.globals().helpers.table_to_json = json_encode
    lua.globals().rcon = lua.table_from({"print": output.append})
    return lua, output


_LEGACY_PROFILE_INSTALLS = {
    LEGACY_OBSERVATION_PROFILE: ("observation", "observation_v2", "craft_jobs", "output_buffers"),
    EXPANDED_OBSERVATION_PROFILE: ("observation", "observation_v2", "craft_jobs", "output_buffers"),
    WATER_ORIGIN_OBSERVATION_PROFILE: ("observation", "observation_v2", "craft_jobs", "output_buffers"),
    LEGACY_MANUAL_CYCLE_PROFILE: (
        "observation", "observation_v2", "craft_jobs", "output_buffers", "input_routes",
        "mining_outposts", "connector_ownership", "coal_manual_journal_v1",
    ),
    MANUAL_CYCLE_PROFILE: (
        "observation", "observation_v2", "craft_jobs", "output_buffers", "input_routes",
        "mining_outposts", "connector_ownership", "coal_manual_journal_v1",
        "solid_routes", "connector_observer_bridge_v1",
    ),
    CLOSED_WORLD_PROFILE: (
        "observation", "observation_v2", "craft_jobs", "output_buffers", "input_routes",
        "mining_outposts", "connector_ownership", "coal_manual_journal_v1",
        "solid_routes", "coal_supply", "coal_manual_cycle_v2",
        "connector_observer_bridge_v1",
    ),
    BOOTSTRAP_PROFILE: (
        "observation", "observation_v2", "craft_jobs", "output_buffers", "input_routes",
        "mining_outposts", "connector_ownership", "coal_manual_journal_v1",
        "solid_routes", "coal_supply", "coal_manual_cycle_v2",
        "connector_observer_bridge_v1", BOOTSTRAP_MODULE,
    ),
    MANUAL_CYCLE_BOOTSTRAP_PROFILE: (
        "observation", "observation_v2", "craft_jobs", "output_buffers", "input_routes",
        "mining_outposts", "connector_ownership", "coal_manual_journal_v1",
        "solid_routes", "connector_observer_bridge_v1", BOOTSTRAP_MODULE,
    ),
}

_PROFILE_OBSERVATION = {
    LEGACY_OBSERVATION_PROFILE: ("observation_v2.lua", LEGACY_OBSERVATION_SHA256),
    EXPANDED_OBSERVATION_PROFILE: (EXPANDED_OBSERVATION_ASSET, EXPANDED_OBSERVATION_SHA256),
    WATER_ORIGIN_OBSERVATION_PROFILE: (WATER_ORIGIN_OBSERVATION_ASSET, WATER_ORIGIN_OBSERVATION_SHA256),
    LEGACY_MANUAL_CYCLE_PROFILE: (WATER_ORIGIN_OBSERVATION_ASSET, WATER_ORIGIN_OBSERVATION_SHA256),
    MANUAL_CYCLE_PROFILE: (WATER_ORIGIN_OBSERVATION_ASSET, WATER_ORIGIN_OBSERVATION_SHA256),
    CLOSED_WORLD_PROFILE: (WATER_ORIGIN_OBSERVATION_ASSET, WATER_ORIGIN_OBSERVATION_SHA256),
    BOOTSTRAP_PROFILE: (WATER_ORIGIN_OBSERVATION_ASSET, WATER_ORIGIN_OBSERVATION_SHA256),
    MANUAL_CYCLE_BOOTSTRAP_PROFILE: (WATER_ORIGIN_OBSERVATION_ASSET, WATER_ORIGIN_OBSERVATION_SHA256),
}


def _legacy_profile_runtime(profile):
    """Compose one exact retained installer graph around the pinned launch source."""
    lua, output = _runtime()
    _install(lua, "fair_actions")
    lua.execute("jev_fle_runtime.fair.bind()")
    _install(lua, "factory")
    lua.execute("campaign=jev_fle_runtime.campaign;campaign.launch=function() launches=(launches or 0)+1 end")
    lua.execute("do\n" + _legacy_launch_source() + "\nend")

    selected_observation, _ = _PROFILE_OBSERVATION[profile]
    for name in _LEGACY_PROFILE_INSTALLS[profile]:
        if name == "observation_v2":
            lua.execute("do\n" + ROOT.joinpath(selected_observation).read_text() + "\nend")
        elif name == BOOTSTRAP_MODULE:
            lua.execute("jev_fle_runtime.bootstrap_output_install_authorization={"
                        "session_id=jev_fle_runtime.jev_session_id,actor_unit=17,"
                        "surface_index=1,force_index=1,origin='future_native_paid_bootstrap_only'}")
            _install(lua, name)
        else:
            _install(lua, name)

    if "solid_routes" in _LEGACY_PROFILE_INSTALLS[profile]:
        lua.execute("jev_fle_runtime.campaign.set_solid_intents(helpers.json_to_table(" +
                    json.dumps(json.dumps(SOLID_INTENTS, separators=(",", ":"))) + "))")
    if "coal_supply" in _LEGACY_PROFILE_INSTALLS[profile]:
        lua.execute("jev_fle_runtime.campaign.set_coal_targets(helpers.json_to_table(" +
                    json.dumps(json.dumps(TARGETS, separators=(",", ":"))) + "))")

    hashes = {
        "fair_actions": PINNED_ASSETS["fair_actions"],
        "factory": PINNED_ASSETS["factory"],
        "launch_readiness": _LEGACY_LAUNCH_SOURCE_SHA256,
    }
    for name in _LEGACY_PROFILE_INSTALLS[profile]:
        if name == "observation":
            hashes[name] = PINNED_ASSETS[name]
        elif name == "observation_v2":
            hashes[name] = _PROFILE_OBSERVATION[profile][1]
        elif name in PINNED_ASSETS:
            hashes[name] = PINNED_ASSETS[name]
        elif name == "connector_ownership":
            hashes[name] = connector_ownership_sha256()
        elif name == "coal_manual_journal_v1":
            hashes[name] = manual_journal_sha256()
        elif name == "coal_manual_cycle_v2":
            hashes[name] = cycle_journal_sha256()
        elif name == "connector_observer_bridge_v1":
            hashes[name] = connector_observer_bridge_sha256()
        elif name == BOOTSTRAP_MODULE:
            hashes[name] = hashlib.sha256(ROOT.joinpath(name + ".lua").read_bytes()).hexdigest()
    if "input_routes" in _LEGACY_PROFILE_INSTALLS[profile]:
        hashes["production_sites"] = PINNED_ASSETS["production_sites"]
    encoded_hashes = json.dumps(json.dumps(hashes, sort_keys=True, separators=(",", ":")))
    lua.execute("local rt=jev_fle_runtime;local c=rt.campaign;local f=rt.fair;"
                "local n=rt.native_installation;n.profile=" + json.dumps(profile) + ";"
                "n.assets=helpers.json_to_table(" + encoded_hashes + ");"
                "n.callbacks=" + CALLBACKS_EXPR)
    return lua, output


def _install_retained_launch_world(lua):
    """Give the exact old launch loader a small, stateful offline engine world."""
    lua.execute(r'''
        local function inventory(initial)
            local stock=initial or {};local inv={stock=stock,limit=1000}
            inv.get_contents=function()
                local rows={};for name,count in pairs(stock) do if count>0 then
                    rows[#rows+1]={name=name,count=count,quality="normal"} end end
                return rows
            end
            inv.get_item_count=function(name) return stock[name] or 0 end
            inv.is_empty=function() return #inv.get_contents()==0 end
            inv.can_insert=function(q) return q.count>0 and
                (stock[q.name] or 0)+q.count<=inv.limit end
            inv.get_insertable_count=function(name) return inv.limit-(stock[name] or 0) end
            inv.remove=function(q)
                local count=math.min(stock[q.name] or 0,q.count);stock[q.name]=(stock[q.name] or 0)-count
                return count
            end
            inv.insert=function(q)
                local count=math.min(q.count,math.max(0,inv.limit-(stock[q.name] or 0)))
                stock[q.name]=(stock[q.name] or 0)+count;return count
            end
            return inv
        end
        local player=game.get_player(1)
        local main=inventory({["raw-fish"]=1})
        local cargo=inventory()
        player.get_main_inventory=function() return main end
        player.get_item_count=function(name) return main.get_item_count(name) end
        player.can_reach_entity=function(entity) return entity and entity.valid end
        local pad={name="cargo-landing-pad",valid=true,unit_number=303,
            force=player.force,surface=player.surface}
        local rocket={valid=true,unit_number=202}
        local silo={name="rocket-silo",valid=true,unit_number=101,
            surface=player.surface,force=player.force,rocket=rocket,
            rocket_silo_status=defines.rocket_silo_status.rocket_ready,
            send_to_orbit_automatically=false,
            get_inventory=function(index)
                assert(index==defines.inventory.rocket_silo_rocket);return cargo
            end}
        player.surface.find_entities_filtered=function(query)
            if query.name=="cargo-landing-pad" then return {pad} end
            return {}
        end
        jev_fle_runtime.campaign.entities["recipe:rocket-part"]=silo
        jev_fle_runtime.campaign.entities["utility:landing-pad"]=pad
        launches=0
        return_values={main=main,cargo=cargo,silo=silo,pad=pad}
    ''')


def _asset_text(name):
    if name == "launch_readiness":
        return "do\n" + ROOT.joinpath("launch_readiness.lua").read_text() + "\nend"
    if name == "input_routes":
        return "\n".join(
            "do\n" + ROOT.joinpath(part).read_text() + "\nend"
            for part in ("input_routes.lua", "production_sites.lua"))
    if name == "observation_v2":
        from jev_factorio.backends.native_attachment import WATER_ORIGIN_OBSERVATION_ASSET
        return ROOT.joinpath(WATER_ORIGIN_OBSERVATION_ASSET).read_text()
    return ROOT.joinpath(name + ".lua").read_text()


def _install(lua, name):
    source = _asset_text(name)
    prepared = prepare_install_command(source)
    assert prepared.startswith(source + "\n") or prepared == source, name
    lua.execute(prepared)


def _case(*, observations=False, craft=False, buffers=False, inputs=False,
          outposts=False, solid=False, coal=False):
    lua, output = _runtime()
    _install(lua, "fair_actions")
    lua.execute("jev_fle_runtime.fair.bind()")
    _install(lua, "factory")
    _install(lua, "connector_ownership")
    _install(lua, "launch_readiness")
    if observations:
        _install(lua, "observation")
        _install(lua, "observation_v2")
    if craft:
        _install(lua, "craft_jobs")
    if buffers:
        _install(lua, "output_buffers")
    if inputs:
        _install(lua, "input_routes")
    if outposts:
        _install(lua, "mining_outposts")
    if solid:
        _install(lua, "solid_routes")
        payload = SOLID_INTENTS if coal else [{
            "source": "recipe:iron-plate", "target": "utility:boiler",
            "item": "iron-plate", "destination": "input",
        }]
        lua.execute("jev_fle_runtime.campaign.set_solid_intents(helpers.json_to_table(" +
                    json.dumps(json.dumps(payload, separators=(",", ":"))) + "))")
    if coal:
        _install(lua, "coal_supply")
        lua.execute("jev_fle_runtime.campaign.set_coal_targets(helpers.json_to_table(" +
                    json.dumps(json.dumps(TARGETS, separators=(",", ":"))) + "))")
    return lua, output


def _probe(case):
    lua, output = _case(**case)
    output.clear()
    lua.execute(PROBE)
    assert len(output) == 1
    value = output.pop()
    return json.loads(value) if isinstance(value, str) else value


class _ReadbackClient:
    def __init__(self, lua, output, *, execute_snapshot):
        self.lua = lua
        self.output = output
        self.execute_snapshot = execute_snapshot
        self.commands = []
        self.row = None

    def send_command(self, command):
        self.commands.append(command)
        self.output.clear()
        if len(self.commands) == 1:
            assert command == "/sc " + PROBE
            self.lua.execute(PROBE)
            raw = self.output.pop()
            self.row = json.loads(raw) if isinstance(raw, str) else raw
            return json.dumps(self.row)
        assert len(self.commands) == 2
        assert command == current_connector_snapshot_command(self.row)
        if self.execute_snapshot:
            self.lua.execute(command.removeprefix("/sc "))
            return self.output.pop()
        return json.dumps({
            "schema": 1,
            "session_id": self.row["session_id"],
            "actor_unit": self.row["actor_unit"],
            "tick": 1234,
            "connector_ownership": {
                "protocol": 1, "session_id": self.row["session_id"],
                "tick": 1234, "routes": {},
            },
        })


@pytest.mark.parametrize("profile", tuple(_LEGACY_PROFILE_INSTALLS))
def test_guard_only_upgrade_qualifies_every_retained_callback_profile(profile):
    lua, output = _legacy_profile_runtime(profile)
    output.clear()
    lua.execute(PROBE)
    before = json.loads(output.pop())
    assert before["qualified"] is True
    assert before["native_installation"]["profile"] == profile
    assert before["native_installation"]["assets"]["launch_readiness"] == _LEGACY_LAUNCH_SOURCE_SHA256

    kwargs = {}
    if profile == LEGACY_MANUAL_CYCLE_PROFILE:
        kwargs["allow_legacy_manual_cycle_repair"] = True
    if profile in {
        LEGACY_MANUAL_CYCLE_PROFILE, MANUAL_CYCLE_PROFILE, CLOSED_WORLD_PROFILE,
        BOOTSTRAP_PROFILE, MANUAL_CYCLE_BOOTSTRAP_PROFILE,
    }:
        kwargs["allow_unqualified_connector_bridge"] = True
    attachment = readback(_ReadbackClient(lua, output, execute_snapshot=False), **kwargs)
    assert attachment["qualified"] is True

    lua.execute("pre_upgrade_graph={campaign_observe=storage.campaign.observe,"
                "campaign_transfer=storage.campaign.transfer,"
                "campaign_configure=storage.campaign.configure,"
                "fair_tick=storage.fair.tick_handler,callbacks=storage.native_installation.callbacks,"
                "launch=storage.campaign.launch,attempts=storage.launch_readiness.attempts,"
                "receipts=storage.launch_readiness.receipts}")
    command = prepare_launch_reconciliation_upgrade_command(attachment)
    assert "campaign.launch(" not in command
    lua.execute(command)
    assert lua.eval("storage.campaign.observe==pre_upgrade_graph.campaign_observe")
    assert lua.eval("storage.campaign.transfer==pre_upgrade_graph.campaign_transfer")
    assert lua.eval("storage.campaign.configure==pre_upgrade_graph.campaign_configure")
    assert lua.eval("storage.fair.tick_handler==pre_upgrade_graph.fair_tick")
    assert lua.eval("storage.native_installation.callbacks==pre_upgrade_graph.callbacks")
    assert lua.eval("storage.launch_readiness.attempts==pre_upgrade_graph.attempts")
    assert lua.eval("storage.launch_readiness.receipts==pre_upgrade_graph.receipts")
    assert lua.eval("storage.campaign.launch~=pre_upgrade_graph.launch")
    assert lua.globals().jev_fle_runtime.launch_reconciliation.verify() is True

    output.clear()
    lua.execute(PROBE)
    after = json.loads(output.pop())
    assert after["qualified"] is True
    assert after["native_installation"]["profile"] == profile
    assert after["native_installation"]["assets"]["launch_readiness"] == launch_readiness_sha256()
    updated = deepcopy(attachment)
    updated["native_installation"]["assets"]["launch_readiness"] = launch_readiness_sha256()
    fresh = readback(_ReadbackClient(lua, output, execute_snapshot=False), **kwargs)
    assert fresh["qualified"] is True
    assert fresh["native_installation"]["assets"]["launch_readiness"] == launch_readiness_sha256()


@pytest.mark.parametrize("mutation", [
    "storage.campaign.craft_jobs.previous_observe=function() end",
    "storage.output_buffers.previous_observe=function() end",
    "storage.native_installation.callbacks.observe=function() end",
    "script.on_tick= function() end",
])
def test_guard_only_upgrade_rejects_unrecognized_graph_before_mutation(mutation):
    lua, output = _legacy_profile_runtime(LEGACY_OBSERVATION_PROFILE)
    attachment = readback(_ReadbackClient(lua, output, execute_snapshot=False))
    command = prepare_launch_reconciliation_upgrade_command(attachment)
    lua.execute("pre_upgrade_campaign_launch=storage.campaign.launch;"
                "pre_upgrade_manifest=storage.native_installation.assets.launch_readiness;"
                "pre_upgrade_attempts=storage.launch_readiness.attempts;"
                "pre_upgrade_receipts=storage.launch_readiness.receipts")
    if mutation == "script.on_tick= function() end":
        lua.execute("script.on_event(defines.events.on_tick,function() end)")
    else:
        lua.execute(mutation)
    with pytest.raises(Exception, match="Retained callback graph"):
        lua.execute(command)
    assert lua.eval("storage.campaign.launch==pre_upgrade_campaign_launch")
    assert lua.eval("storage.native_installation.assets.launch_readiness==pre_upgrade_manifest")
    assert lua.eval("storage.launch_readiness.attempts==pre_upgrade_attempts")
    assert lua.eval("storage.launch_readiness.receipts==pre_upgrade_receipts")
    assert lua.eval("jev_fle_runtime.launch_reconciliation==nil")
    assert lua.eval("storage.launch_readiness.load_reconciliation_protocol==nil")


def test_upgrade_rejects_nonlaunch_manifest_drift_before_mutation():
    lua, output = _legacy_profile_runtime(LEGACY_OBSERVATION_PROFILE)
    attachment = readback(_ReadbackClient(lua, output, execute_snapshot=False))
    command = prepare_launch_reconciliation_upgrade_command(attachment)
    lua.execute("pre_upgrade_campaign_launch=storage.campaign.launch;"
                "pre_upgrade_manifest=storage.native_installation.assets.launch_readiness;"
                "pre_upgrade_attempts=storage.launch_readiness.attempts;"
                "pre_upgrade_receipts=storage.launch_readiness.receipts;"
                "storage.native_installation.assets.craft_jobs=string.rep('0',64)")
    with pytest.raises(Exception, match="Native asset manifest changed after attachment readback"):
        lua.execute(command)
    assert lua.eval("storage.campaign.launch==pre_upgrade_campaign_launch")
    assert lua.eval("storage.native_installation.assets.launch_readiness==pre_upgrade_manifest")
    assert lua.eval("storage.launch_readiness.attempts==pre_upgrade_attempts")
    assert lua.eval("storage.launch_readiness.receipts==pre_upgrade_receipts")
    assert lua.eval("jev_fle_runtime.launch_reconciliation==nil")
    assert lua.eval("storage.launch_readiness.load_reconciliation_protocol==nil")


def test_upgrade_rejects_changed_fair_path_handler_before_mutation():
    lua, output = _legacy_profile_runtime(LEGACY_OBSERVATION_PROFILE)
    attachment = readback(_ReadbackClient(lua, output, execute_snapshot=False))
    command = prepare_launch_reconciliation_upgrade_command(attachment)
    lua.execute("pre_upgrade_campaign_launch=storage.campaign.launch;"
                "pre_upgrade_manifest=storage.native_installation.assets.launch_readiness;"
                "script.on_event(defines.events.on_script_path_request_finished,function() end)")
    with pytest.raises(Exception, match="Retained callback graph"):
        lua.execute(command)
    assert lua.eval("storage.campaign.launch==pre_upgrade_campaign_launch")
    assert lua.eval("storage.native_installation.assets.launch_readiness==pre_upgrade_manifest")
    assert lua.eval("jev_fle_runtime.launch_reconciliation==nil")
    assert lua.eval("storage.launch_readiness.load_reconciliation_protocol==nil")


def test_actual_legacy_loader_receipt_survives_guard_upgrade_and_launches_once():
    from test_launch_asset_reconciliation482 import _backend
    from jev_factorio.backends.native_factory import NativeFactory

    lua, output = _legacy_profile_runtime(LEGACY_OBSERVATION_PROFILE)
    _install_retained_launch_world(lua)
    lua.execute('jev_fle_runtime.campaign.load_launch_payload({role="recipe:rocket-part",'
                'silo_unit=101,rocket_unit=202,item="raw-fish",receipt="load:legacy-actual"})')
    assert lua.eval('return_values.main.get_item_count("raw-fish")==0')
    assert lua.eval('return_values.cargo.get_item_count("raw-fish")==1')
    assert lua.eval('storage.launch_readiness.attempts.load.receipt=="load:legacy-actual"')
    assert lua.eval('storage.launch_readiness.receipts["load:legacy-actual"].quantity==1')

    attachment = readback(_ReadbackClient(lua, output, execute_snapshot=False))
    backend, old_client = _backend(lua, output, attachment, catalog_first=True)
    retained = NativeFactory(backend)
    assert len(old_client.commands) == 1
    command = retained.prepare_launch_reconciliation_upgrade()
    assert len(old_client.commands) == 1
    retained.command(command)
    assert len(old_client.commands) == 2

    output.clear()
    lua.execute(PROBE)
    upgraded_probe = json.loads(output.pop())
    assert upgraded_probe["qualified"] is True
    current_attachment = deepcopy(attachment)
    current_attachment["native_installation"]["assets"]["launch_readiness"] = launch_readiness_sha256()
    current_backend, current_client = _backend(lua, output, current_attachment)
    current = NativeFactory.__new__(NativeFactory)
    current.backend = current_backend
    current.call("launch", "recipe:rocket-part")

    assert current_client.commands[0] == "/sc " + LAUNCH_RECONCILIATION_PROBE
    assert lua.globals().launches == 1
    assert lua.eval('storage.launch_readiness.attempts.load.receipt=="load:legacy-actual"')
    assert lua.eval('storage.launch_readiness.receipts["load:legacy-actual"].quantity==1')
    assert lua.eval('storage.launch_readiness.attempts.launch~=nil')
    assert lua.eval('storage.launch_readiness.receipts.launch~=nil')


@pytest.mark.parametrize("receipt_mutation", ["missing", "quantity", "session", "tick"])
def test_guarded_launch_keeps_actual_loader_attempt_when_receipt_no_longer_matches(receipt_mutation):
    lua, output = _legacy_profile_runtime(LEGACY_OBSERVATION_PROFILE)
    _install_retained_launch_world(lua)
    lua.execute('jev_fle_runtime.campaign.load_launch_payload({role="recipe:rocket-part",'
                'silo_unit=101,rocket_unit=202,item="raw-fish",receipt="load:legacy-actual"})')
    attachment = readback(_ReadbackClient(lua, output, execute_snapshot=False))
    command = prepare_launch_reconciliation_upgrade_command(attachment)
    lua.execute(command)
    lua.execute({
        "missing": 'storage.launch_readiness.receipts["load:legacy-actual"]=nil',
        "quantity": 'storage.launch_readiness.receipts["load:legacy-actual"].quantity=2',
        "session": 'storage.launch_readiness.receipts["load:legacy-actual"].session_id="other"',
        "tick": 'storage.launch_readiness.receipts["load:legacy-actual"].tick=1233',
    }[receipt_mutation])
    with pytest.raises(Exception, match="Paid payload load is unresolved"):
        lua.eval('jev_fle_runtime.campaign.launch("recipe:rocket-part")')
    assert lua.globals().launches == 0
    assert lua.eval('storage.launch_readiness.attempts.load.receipt=="load:legacy-actual"')
    assert lua.eval('return_values.cargo.get_item_count("raw-fish")==1')
    assert lua.eval('return_values.main.get_item_count("raw-fish")==0')
    assert lua.eval('storage.launch_readiness.attempts.launch==nil')


def test_upgrade_completion_failure_restores_old_capability_manifest_and_callback():
    lua, output = _legacy_profile_runtime(LEGACY_OBSERVATION_PROFILE)
    attachment = readback(_ReadbackClient(lua, output, execute_snapshot=False))
    command = prepare_launch_reconciliation_upgrade_command(attachment)
    lua.execute("old_campaign_launch=storage.campaign.launch;"
                "old_readiness_launch=storage.launch_readiness.launch;"
                "old_protocol=storage.launch_readiness.load_reconciliation_protocol;"
                "old_launch_asset=storage.native_installation.assets.launch_readiness;"
                "old_attempts=storage.launch_readiness.attempts;"
                "old_receipts=storage.launch_readiness.receipts;"
                "local prior=script.get_event_handler;local calls=0;"
                "script.get_event_handler=function(event) calls=calls+1;"
                "if calls==13 then return function() end end;return prior(event) end")
    with pytest.raises(Exception, match="Launch guard .* (did not survive upgrade|did not verify)"):
        lua.execute(command)
    assert lua.eval("storage.campaign.launch==old_campaign_launch")
    assert lua.eval("storage.launch_readiness.launch==old_readiness_launch")
    assert lua.eval("storage.launch_readiness.load_reconciliation_protocol==old_protocol")
    assert lua.eval("storage.native_installation.assets.launch_readiness==old_launch_asset")
    assert lua.eval("storage.launch_readiness.attempts==old_attempts")
    assert lua.eval("storage.launch_readiness.receipts==old_receipts")
    assert lua.eval("jev_fle_runtime.launch_reconciliation==nil")


@pytest.mark.parametrize("case", [
    pytest.param({}, id="default-without-observation"),
    pytest.param({"observations": True}, id="observation-only"),
    pytest.param({"buffers": True}, id="buffers-without-observation-or-background"),
    pytest.param({"observations": True, "buffers": True}, id="buffers-without-background"),
    pytest.param({"craft": True, "buffers": True}, id="background-buffers-without-observation"),
    pytest.param({"observations": True, "craft": True, "buffers": True},
                 id="background-buffers-with-observation"),
    pytest.param({"solid": True}, id="minimal-solid-without-observation"),
    pytest.param({"observations": True, "solid": True}, id="minimal-solid"),
    pytest.param({"solid": True, "coal": True}, id="minimal-solid-and-coal-without-observation"),
    pytest.param({"observations": True, "solid": True, "coal": True}, id="minimal-solid-and-coal"),
    pytest.param({"observations": True, "craft": True, "buffers": True,
                  "inputs": True, "outposts": True}, id="existing-current-module-profile"),
])
def test_actual_bundled_optional_profile_has_a_qualified_installed_callback_chain(case):
    row = _probe(case)
    assert row["qualified"] is True
    assert row["session_id"] == SESSION
    assert row["actor_unit"] == ACTOR
    assert row["native_installation"]["session_id"] == SESSION
    assert row["native_installation"]["actor_unit"] == ACTOR


def test_actual_optional_callback_replacement_is_rejected():
    lua, output = _case(observations=True, buffers=True)
    lua.execute("jev_fle_runtime.output_buffers.previous_observe=function() end")
    output.clear()
    lua.execute(PROBE)
    assert json.loads(output.pop())["qualified"] is False


def test_actual_observation_callback_replacement_is_rejected():
    lua, output = _case(observations=True)
    lua.execute("jev_fle_runtime.campaign.observation_snapshot_v2=function() end")
    output.clear()
    lua.execute(PROBE)
    assert json.loads(output.pop())["qualified"] is False


def test_actual_solid_callback_replacement_is_rejected():
    lua, output = _case(observations=True, solid=True)
    lua.execute("jev_fle_runtime.solid_routes.observer=function() end")
    output.clear()
    lua.execute(PROBE)
    assert json.loads(output.pop())["qualified"] is False


@pytest.mark.parametrize("case", [
    pytest.param({}, id="default"),
    pytest.param({"observations": True}, id="observation-only"),
    pytest.param({"buffers": True}, id="buffers-without-observation-or-background"),
    pytest.param({"observations": True, "buffers": True}, id="buffers-without-background"),
    pytest.param({"craft": True, "buffers": True},
                 id="background-work-with-buffers"),
    pytest.param({"observations": True, "craft": True, "buffers": True},
                 id="background-work-with-buffers-and-observations"),
    pytest.param({"observations": True, "craft": True, "buffers": True,
                  "inputs": True, "outposts": True}, id="existing-current-full-profile"),
])
def test_exact_non_solid_source_profile_completes_public_readback(case):
    lua, output = _case(**case)
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    attached = readback(client)
    assert len(client.commands) == 2
    assert attached["connector_snapshot_qualified"] is True
    assert attached["connector_snapshot_ownership"]["routes"] == {}
    profile = source_bound_direct_profile(client.row)
    assert profile in {
        "default", "observation_only", "buffers_without_background",
        "buffers_without_background_with_observations", "current_full",
        "background_work_with_buffers", "background_work_with_buffers_and_observations",
    }
    if profile in {"buffers_without_background", "buffers_without_background_with_observations",
                   "background_work_with_buffers", "background_work_with_buffers_and_observations"}:
        assert "c.observe()" not in client.commands[1]
    assert "c.observe()" not in client.commands[1]


def test_background_work_with_output_buffers_has_standalone_public_readback():
    # BackgroundWorkLoop is a Python controller wrapper: it introduces no Lua
    # installer asset. Exercise its actual production composition independently
    # from input routes/outposts, then run the same public native readback path.
    from jev_factorio.background import BackgroundWorkLoop
    from jev_factorio.buffer_controller import buffered_loop_type

    background_with_buffers = buffered_loop_type(BackgroundWorkLoop)
    assert issubclass(background_with_buffers, BackgroundWorkLoop)
    lua, output = _case(craft=True, buffers=True)
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    attached = readback(client)
    assert source_bound_direct_profile(client.row) == "background_work_with_buffers"
    assert attached["connector_snapshot_qualified"] is True
    assert attached["connector_snapshot_ownership"]["routes"] == {}
    assert "c.observe()" not in client.commands[1]


def test_live_cli_selects_background_and_buffer_composition_without_starting_a_world(
        monkeypatch, tmp_path):
    # Exercise the real CLI parser/selection path while replacing only the
    # backend and loop execution. Native installer/readback behavior is covered
    # independently above against the actual bundled Lua assets.
    from jev_factorio import controller, main

    selected = []

    class BackendStub:
        craft_jobs_supported = True
        output_buffers_supported = True

    def capture_loop(self, *args, **kwargs):
        selected.append(type(self).__mro__)
        self.catalog = object()

    monkeypatch.setattr(main, "make_backend", lambda *args, **kwargs: BackendStub())
    monkeypatch.setattr(controller.HierarchicalLoop, "__init__", capture_loop)
    monkeypatch.setattr(controller.HierarchicalLoop, "run", lambda self, **kwargs: None)
    monkeypatch.setattr(sys, "argv", [
        "jev-factorio", "--backend", "fle", "--controller", "hierarchical",
        "--policy", "deterministic", "--target", "iron_smelting",
        "--factory-scheduling", "ready-work", "--background-work",
        "--furnace-output-buffers", "--checkpoint", str(tmp_path / "controller.json"),
        "--tick-seconds", "1", "--steps", "0",
    ])

    main.cli()

    assert selected
    names = {kind.__name__ for kind in selected[0]}
    assert "BackgroundWorkLoop" in names
    assert "OutputBufferLoop" in names


@pytest.mark.parametrize("change", ["paid_cell", "pending_cell", "offer"])
def test_buffer_readback_preserves_existing_owner_state(change):
    lua, output = _case(craft=True, buffers=True)
    if change == "paid_cell":
        lua.execute("jev_fle_runtime.output_buffers.cells.retained={parts={chest={paid=1,receipt='paid'}}}")
    elif change == "pending_cell":
        lua.execute("jev_fle_runtime.output_buffers.cells.retained={parts={},pending={phase='prepared'}}")
    elif change == "offer":
        lua.execute("jev_fle_runtime.output_buffers.offers.retained={parts={},layout='output:stale'}")
    retained = ("helpers.table_to_json({cells=jev_fle_runtime.output_buffers.cells,"
                "offers=jev_fle_runtime.output_buffers.offers})")
    before = lua.eval(retained)
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    with pytest.raises(Exception, match="assertion failed"):
        readback(client)
    assert len(client.commands) == 2
    assert not output
    assert lua.eval(retained) == before


@pytest.mark.parametrize(("job", "queue"), [
    pytest.param({"status": "running", "paid": True, "accepted": 1}, 1,
                 id="paid-running-job"),
    pytest.param({"status": "completed", "paid": True, "finished": 1}, 0,
                 id="retained-completed-job"),
    pytest.param({"status": "invalid", "paid": True, "error": "queue_mismatch"}, 0,
                 id="ambiguous-invalid-job"),
])
def test_background_direct_readback_preserves_paid_craft_job_for_reconciliation(job, queue):
    lua, output = _case(craft=True, buffers=True)
    lua.globals().jev_fle_runtime.campaign.craft_jobs.job = lua.table_from(job)
    lua.globals().game.get_player(1).crafting_queue_size = queue
    retained = ("helpers.table_to_json({job=jev_fle_runtime.campaign.craft_jobs.job,"
                "queue=game.get_player(1).crafting_queue_size})")
    before = lua.eval(retained)
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    with pytest.raises(Exception, match="assertion failed"):
        readback(client)
    assert len(client.commands) == 2
    assert not output
    assert lua.eval(retained) == before


def test_background_direct_readback_rejects_untracked_native_crafting_queue():
    lua, output = _case(observations=True, craft=True, buffers=True)
    lua.globals().game.get_player(1).crafting_queue_size = 1
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    with pytest.raises(Exception, match="assertion failed"):
        readback(client)
    assert len(client.commands) == 2
    assert lua.globals().game.get_player(1).crafting_queue_size == 1


@pytest.mark.parametrize("case", [
    pytest.param({"observations": True, "solid": True}, id="minimal-solid"),
    pytest.param({"solid": True}, id="minimal-solid-without-observation"),
    pytest.param({"solid": True, "coal": True}, id="minimal-solid-and-coal-without-observation"),
    pytest.param({"observations": True, "solid": True, "coal": True}, id="minimal-solid-and-coal"),
    pytest.param({"observations": True, "craft": True, "buffers": True,
                  "inputs": True, "outposts": True, "solid": True, "coal": True},
                 id="existing-prerequisites-solid-and-coal"),
])
def test_solid_readback_uses_non_mutating_source_bound_owner_query(case):
    lua, output = _case(**case)
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    attached = readback(client)
    assert len(client.commands) == 2
    assert attached["connector_snapshot_qualified"] is True
    assert "c.observe()" not in client.commands[1]
    assert lua.eval("next(jev_fle_runtime.solid_routes.cells)==nil") is True
    if case.get("coal"):
        assert lua.eval("not jev_fle_runtime.coal_supply.committed") is True


@pytest.mark.parametrize("change", [
    "solid_cell", "solid_offer_paid", "solid_offer_pending", "solid_offer_fault",
    "solid_offer_committed", "solid_offer_manual", "coal_committed", "coal_pending_part",
    "coal_paid_part", "coal_manual_pending", "coal_manual_receipt", "coal_manual_total",
    "coal_fault", "coal_row_committed", "connector_active", "connector_route",
])
def test_solid_readback_preserves_paid_or_pending_owner_state(change):
    case = {"solid": True, "coal": change.startswith("coal_")}
    lua, output = _case(**case)
    if change == "solid_cell":
        lua.execute("jev_fle_runtime.solid_routes.cells.retained={}")
    elif change == "solid_offer_paid":
        lua.execute("jev_fle_runtime.solid_routes.offers.retained={route='retained',parts={paid={}}}")
    elif change == "solid_offer_pending":
        lua.execute("jev_fle_runtime.solid_routes.offers.retained={route='retained',parts={},pending={phase='prepared'}}")
    elif change == "solid_offer_fault":
        lua.execute("jev_fle_runtime.solid_routes.offers.retained={route='retained',parts={},fault='receipt_reconciliation_failed'}")
    elif change == "solid_offer_committed":
        lua.execute("jev_fle_runtime.solid_routes.offers.retained={route='retained',parts={},committed=true}")
    elif change == "solid_offer_manual":
        lua.execute("jev_fle_runtime.solid_routes.offers.retained={route='retained',parts={},manual_pending={receipt='pending'}}")
    elif change == "coal_committed":
        lua.execute("jev_fle_runtime.coal_supply.committed=true")
    elif change == "coal_pending_part":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={},pending={phase='prepared'},manual_receipts={},manual_total=0}")
    elif change == "coal_paid_part":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={chest={receipt='paid'}},manual_receipts={},manual_total=0}")
    elif change == "coal_manual_pending":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={},manual_pending={receipt='pending'},manual_receipts={},manual_total=0}")
    elif change == "coal_manual_receipt":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={},manual_receipts={paid=1},manual_total=1}")
    elif change == "coal_manual_total":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={},manual_receipts={},manual_total=1}")
    elif change == "coal_fault":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={},manual_receipts={},manual_total=0,fault='manual_transfer_ambiguous'}")
    elif change == "coal_row_committed":
        lua.execute("jev_fle_runtime.coal_supply.rows.retained={parts={},manual_receipts={},manual_total=0,committed=true}")
    elif change == "connector_active":
        lua.execute("jev_fle_runtime.campaign.connector_ledger.active='retained'")
    elif change == "connector_route":
        lua.execute("jev_fle_runtime.campaign.connector_ledger.routes.retained={}")
    retained_state = (
        "helpers.table_to_json({solid_cells=jev_fle_runtime.solid_routes "
        "and jev_fle_runtime.solid_routes.cells or {},solid_offers=jev_fle_runtime.solid_routes "
        "and jev_fle_runtime.solid_routes.offers or {},coal_committed=jev_fle_runtime.coal_supply "
        "and jev_fle_runtime.coal_supply.committed or false,coal_rows=jev_fle_runtime.coal_supply "
        "and jev_fle_runtime.coal_supply.rows or {},connector_active=jev_fle_runtime.campaign "
        "and jev_fle_runtime.campaign.connector_ledger.active or false,connector_routes="
        "jev_fle_runtime.campaign.connector_ledger.routes or {}})"
    )
    before = lua.eval(retained_state)
    client = _ReadbackClient(lua, output, execute_snapshot=True)
    with pytest.raises(Exception, match="assertion failed"):
        readback(client)
    assert len(client.commands) == 2
    assert not output
    assert lua.eval(retained_state) == before


@pytest.mark.parametrize(("case", "expected"), [
    pytest.param({}, "default", id="default"),
    pytest.param({"observations": True}, "observation_only", id="observation-only"),
    pytest.param({"buffers": True}, "buffers_without_background", id="buffers-without-observation"),
    pytest.param({"observations": True, "buffers": True},
                 "buffers_without_background_with_observations", id="buffers-with-background-observation"),
    pytest.param({"craft": True, "buffers": True},
                 "background_work_with_buffers", id="background-work-with-buffers"),
    pytest.param({"observations": True, "craft": True, "buffers": True},
                 "background_work_with_buffers_and_observations", id="background-work-with-buffers-and-observations"),
    pytest.param({"observations": True, "solid": True}, "solid_only_with_observations", id="solid-only-with-observations"),
    pytest.param({"solid": True}, "solid_only", id="solid-only-without-observation"),
    pytest.param({"observations": True, "solid": True}, "solid_only_with_observations", id="solid-only-with-observations"),
    pytest.param({"observations": True, "solid": True, "coal": True},
                 "solid_and_coal_with_observations", id="solid-and-coal-with-observations"),
    pytest.param({"solid": True, "coal": True}, "solid_and_coal", id="solid-and-coal-without-observation"),
    pytest.param({"observations": True, "craft": True, "buffers": True,
                  "inputs": True, "outposts": True}, "current_full", id="current-full"),
    pytest.param({"observations": True, "craft": True, "buffers": True,
                  "inputs": True, "outposts": True, "solid": True, "coal": True},
                 "solid_and_coal_with_prerequisites", id="solid-coal-with-prerequisites"),
])
def test_only_exact_bundled_installer_module_profiles_are_directly_eligible(case, expected):
    row = _probe(case)
    assert row["qualified"] is True
    assert source_bound_direct_profile(row) == expected


def test_unlisted_optional_combinations_remain_bridge_or_reconciliation_gated():
    row = _probe({"observations": True, "craft": True})
    assert row["qualified"] is True
    assert source_bound_direct_profile(row) is None

    current = _probe({"observations": True, "craft": True, "buffers": True,
                      "inputs": True, "outposts": True})
    current["modules"]["mining_outposts"] = False
    assert source_bound_direct_profile(current) is None


def test_installer_inventory_is_bounded_to_supported_manifests():
    manifests = list(_installer_scripts().values())
    assert ("factory",) in manifests
    assert ("solid_routes",) in manifests


_FULL_OWNER_CASES = [
    pytest.param(False, id="current-full"),
    pytest.param(True, id="solid-coal-with-prerequisites"),
]

_RETAINED_OWNER_FIXTURES = {
    "output_paid_offer": """
        local c=jev_fle_runtime.campaign;local e={valid=true,unit_number=19001}
        c.entities['output-chest:19001']=e
        jev_fle_runtime.output_buffers.offers['recipe:iron-plate']={source='recipe:iron-plate',
            source_unit=17,layout='output:17:retained',state='building',parts={chest={entity=e,
            role='output-chest:19001',unit_number=19001,receipt='paid-output',paid=1}}}
    """,
    "output_paid_cell": """
        local c=jev_fle_runtime.campaign;local e={valid=true,unit_number=19002}
        c.entities['output-arm:19002']=e
        jev_fle_runtime.output_buffers.cells['recipe:iron-plate']={source='recipe:iron-plate',
            source_unit=17,layout='output:17:retained',state='building',parts={inserter={entity=e,
            role='output-arm:19002',unit_number=19002,receipt='paid-output-cell',paid=1}}}
    """,
    "output_stale_offer": """
        jev_fle_runtime.output_buffers.offers['recipe:iron-plate']={source='recipe:iron-plate',
            source_unit=17,layout='output:stale',state='proposed',parts={}}
    """,
    "output_fault_cell": """
        jev_fle_runtime.output_buffers.cells['recipe:iron-plate']={source='recipe:iron-plate',
            source_unit=17,layout='output:retained',fault='buffer_identity_or_handler_changed',parts={}}
    """,
    "input_paid_cell": """
        local c=jev_fle_runtime.campaign;local e={valid=true,unit_number=19003}
        c.entities['input:recipe:iron-plate:drill']=e
        jev_fle_runtime.input_routes.cells['recipe:iron-plate']={source='recipe:iron-plate',
            parts={drill={entity=e,role='input:recipe:iron-plate:drill',unit_number=19003,
            receipt='paid-input',paid=1}}}
    """,
    "input_stale_offer": """
        jev_fle_runtime.input_routes.offers['recipe:iron-plate']={source='recipe:iron-plate',
            layout='input:stale',parts={}}
    """,
    "input_fault_cell": """
        jev_fle_runtime.input_routes.cells['recipe:iron-plate']={source='recipe:iron-plate',
            layout='input:retained',fault='input_identity_changed',parts={}}
    """,
    "outpost_paid_cell": """
        local c=jev_fle_runtime.campaign;local e={valid=true,unit_number=19004}
        c.entities['outpost:iron-ore:drill']=e
        jev_fle_runtime.mining_outposts.cells['iron-ore']={resource='iron-ore',
            layout='outpost:iron-ore:retained',parts={drill={entity=e,
            role='outpost:iron-ore:drill',unit_number=19004,receipt='paid-outpost',paid=1}}}
        jev_fle_runtime.mining_outposts.receipts['paid-outpost']=19004
    """,
    "outpost_stale_offer": """
        jev_fle_runtime.mining_outposts.offers['iron-ore']={resource='iron-ore',
            layout='outpost:iron-ore:stale',parts={}}
    """,
    "outpost_fault_cell": """
        jev_fle_runtime.mining_outposts.cells['iron-ore']={resource='iron-ore',
            layout='outpost:iron-ore:retained',fault='outpost_identity_topology_or_flow_mismatch',parts={}}
    """,
    "outpost_receipt": """
        jev_fle_runtime.mining_outposts.receipts['already-paid-outpost']=19005
    """,
    "production_owned": """
        local c=jev_fle_runtime.campaign
        local e={valid=true,name='stone-furnace',unit_number=19006,position={x=4.5,y=5.5}}
        c.entities['recipe:iron-plate']=e
        jev_fle_runtime.production_sites.owned['recipe:iron-plate']={
            role='recipe:iron-plate',entity=e,source_unit=19006,
            position={x=4.5,y=5.5},specs={}}
    """,
    "production_stale_offer": """
        jev_fle_runtime.production_sites.offers['recipe:iron-plate']={
            role='recipe:iron-plate',anchor='cell-site:iron-ore:stale',specs={}}
    """,
    "orphan_output_role": """
        jev_fle_runtime.campaign.entities['output-chest:19007']={valid=true,unit_number=19007}
    """,
    "orphan_input_role": """
        jev_fle_runtime.campaign.entities['input:recipe:iron-plate:drill']={valid=true,unit_number=19008}
    """,
    "orphan_outpost_role": """
        jev_fle_runtime.campaign.entities['outpost:iron-ore:drill']={valid=true,unit_number=19009}
    """,
    "paid_craft_job": """
        jev_fle_runtime.campaign.craft_jobs.job={status='running',paid=true,accepted=1}
        game.get_player(1).crafting_queue_size=1
    """,
    "untracked_crafting_queue": "game.get_player(1).crafting_queue_size=1",
    "connector_active": "jev_fle_runtime.campaign.connector_ledger.active='retained'",
    "connector_route": "jev_fle_runtime.campaign.connector_ledger.routes.retained={state='building',paid=1}",
}

_SOLID_COAL_OWNER_FIXTURES = {
    "solid_stale_offer": """
        jev_fle_runtime.solid_routes.offers.retained={route='retained',parts={}}
    """,
    "solid_pending": "jev_fle_runtime.solid_routes.pending={receipt='pending'}",
    "coal_pending": "jev_fle_runtime.coal_supply.pending={receipt='pending'}",
    "coal_fault": "jev_fle_runtime.coal_supply.fault='ambiguous_transfer'",
}


def _owner_state_json(lua):
    return lua.eval("""
        helpers.table_to_json({
            output_cells=jev_fle_runtime.output_buffers.cells,
            output_offers=jev_fle_runtime.output_buffers.offers,
            input_cells=jev_fle_runtime.input_routes.cells,
            input_offers=jev_fle_runtime.input_routes.offers,
            outpost_cells=jev_fle_runtime.mining_outposts.cells,
            outpost_offers=jev_fle_runtime.mining_outposts.offers,
            outpost_receipts=jev_fle_runtime.mining_outposts.receipts,
            production_owned=jev_fle_runtime.production_sites.owned,
            production_offers=jev_fle_runtime.production_sites.offers,
            solid_cells=jev_fle_runtime.solid_routes and jev_fle_runtime.solid_routes.cells or {},
            solid_offers=jev_fle_runtime.solid_routes and jev_fle_runtime.solid_routes.offers or {},
            solid_pending=jev_fle_runtime.solid_routes and jev_fle_runtime.solid_routes.pending or false,
            solid_fault=jev_fle_runtime.solid_routes and jev_fle_runtime.solid_routes.fault or false,
            coal_rows=jev_fle_runtime.coal_supply and jev_fle_runtime.coal_supply.rows or {},
            coal_pending=jev_fle_runtime.coal_supply and jev_fle_runtime.coal_supply.pending or false,
            coal_fault=jev_fle_runtime.coal_supply and jev_fle_runtime.coal_supply.fault or false,
            coal_committed=jev_fle_runtime.coal_supply and jev_fle_runtime.coal_supply.committed or false,
            craft_job=jev_fle_runtime.campaign.craft_jobs.job or false,
            crafting_queue=game.get_player(1).crafting_queue_size,
            connector_active=jev_fle_runtime.campaign.connector_ledger.active or false,
            connector_routes=jev_fle_runtime.campaign.connector_ledger.routes,
            entity_owners={
                output=jev_fle_runtime.campaign.entities['output-chest:19001'] or false,
                output_arm=jev_fle_runtime.campaign.entities['output-arm:19002'] or false,
                orphan_output=jev_fle_runtime.campaign.entities['output-chest:19007'] or false,
                input=jev_fle_runtime.campaign.entities['input:recipe:iron-plate:drill'] or false,
                outpost=jev_fle_runtime.campaign.entities['outpost:iron-ore:drill'] or false,
                production_site_recipe=jev_fle_runtime.campaign.entities['recipe:iron-plate'] or false,
            },
        })
    """)


@pytest.mark.parametrize("full_solid_coal", _FULL_OWNER_CASES)
@pytest.mark.parametrize("fixture", sorted(_RETAINED_OWNER_FIXTURES))
def test_full_direct_profiles_reject_retained_module_owners_before_readback_mutation(
        full_solid_coal, fixture):
    case = {"observations": True, "craft": True, "buffers": True,
            "inputs": True, "outposts": True}
    if full_solid_coal:
        case.update(solid=True, coal=True)
    lua, output = _case(**case)
    lua.execute(_RETAINED_OWNER_FIXTURES[fixture])
    before = _owner_state_json(lua)
    client = _ReadbackClient(lua, output, execute_snapshot=True)

    with pytest.raises(Exception):
        readback(client)

    assert len(client.commands) == 2
    assert "c.observe()" not in client.commands[1]
    assert not output
    assert _owner_state_json(lua) == before


@pytest.mark.parametrize("fixture", sorted(_SOLID_COAL_OWNER_FIXTURES))
def test_full_solid_coal_profile_rejects_all_retained_route_state_before_readback_mutation(
        fixture):
    lua, output = _case(observations=True, craft=True, buffers=True, inputs=True,
                        outposts=True, solid=True, coal=True)
    lua.execute(_SOLID_COAL_OWNER_FIXTURES[fixture])
    before = _owner_state_json(lua)
    client = _ReadbackClient(lua, output, execute_snapshot=True)

    with pytest.raises(Exception, match="assertion failed"):
        readback(client)

    assert len(client.commands) == 2
    assert not output
    assert _owner_state_json(lua) == before


def test_full_solid_coal_prerequisite_profile_has_empty_owner_public_readback():
    lua, output = _case(observations=True, craft=True, buffers=True, inputs=True,
                        outposts=True, solid=True, coal=True)
    client = _ReadbackClient(lua, output, execute_snapshot=True)

    attached = readback(client)

    assert source_bound_direct_profile(client.row) == "solid_and_coal_with_prerequisites"
    assert attached["connector_snapshot_qualified"] is True
    assert attached["connector_snapshot_ownership"]["routes"] == {}
    assert "c.observe()" not in client.commands[1]


def test_current_full_keeps_ordinary_recipe_role_eligible_without_site_owner():
    # production_sites.lua:210-212 classifies an entity under recipe:* as an
    # existing manual cell when sites.owned has no corresponding entry. The
    # direct connector query must leave that ordinary factory role untouched.
    lua, output = _case(observations=True, craft=True, buffers=True,
                        inputs=True, outposts=True)
    lua.execute("""
        local e={valid=true,name='stone-furnace',unit_number=19010,
            position={x=4.5,y=5.5}}
        jev_fle_runtime.campaign.entities['recipe:iron-plate']=e
    """)
    client = _ReadbackClient(lua, output, execute_snapshot=True)

    attached = readback(client)

    assert source_bound_direct_profile(client.row) == "current_full"
    assert attached["connector_snapshot_qualified"] is True
    assert lua.eval("jev_fle_runtime.campaign.entities['recipe:iron-plate'].unit_number") == 19010
    assert lua.eval("next(jev_fle_runtime.production_sites.owned)==nil") is True
    assert "c.observe()" not in client.commands[1]
