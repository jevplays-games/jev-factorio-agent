"""Bounded raw-item headroom from the same command as the native snapshot.

No installed callback, inventory, receipt or ownership record is changed.
These independent insertable counts are advisory, not simultaneous reservations.
"""
from __future__ import annotations

import json

MARKER = 'JEV_ACTOR_RAW_CAPACITY|'
ITEMS = frozenset({'coal', 'wood', 'iron-ore', 'copper-ore', 'stone'})


def observation_command(command: str) -> str:
    return '''do
local rt=assert(jev_fle_runtime)
local before=game.tick
local player=assert(rt.fair.actor())
local actor=assert(player.character)
local session=rt.jev_session_id
local unit,surface,force=actor.unit_number,actor.surface.index,actor.force.index
do
''' + command + '''
end
local inventory=assert(player.get_main_inventory())
local supported,method=pcall(function() return inventory.get_insertable_count end)
local counts={}
local complete=supported and type(method)=="function"
if complete then
    for _,name in ipairs({"coal","wood","iron-ore","copper-ore","stone"}) do
        local ok,count=pcall(function() return method({name=name,quality="normal"}) end)
        if not ok or type(count)~="number" or count<0 or count%1~=0
            or count>4294967295 then complete=false;break end
        counts[name]=count
    end
end
assert(game.tick==before and actor.valid and player.character==actor
    and rt.jev_session_id==session and actor.unit_number==unit
    and actor.surface.index==surface and actor.force.index==force,
    "Native actor capacity crossed observation identity")
rcon.print("JEV_ACTOR_RAW_CAPACITY|"..helpers.table_to_json({schema=1,tick=before,
    session_id=session,actor_unit=unit,surface_index=surface,force_index=force,
    inventory="character_main",quality="normal",method="get_insertable_count",
    complete=complete,items=counts}))
end'''


def decode(raw: str, snapshot: dict, primary: dict | None) -> dict | None:
    """Missing or incomplete evidence cannot extend the primary coal reading."""
    if not isinstance(raw, str) or len(raw.encode()) > 8 * 1024 * 1024:
        raise ValueError('Invalid actor capacity response')
    rows = [line[len(MARKER):] for line in raw.splitlines() if line.startswith(MARKER)]
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) > 4096:
        raise ValueError('Ambiguous or oversized actor capacity response')
    value = json.loads(rows[0])
    if (not isinstance(value, dict) or set(value) != {
            'schema', 'tick', 'session_id', 'actor_unit', 'surface_index', 'force_index',
            'inventory', 'quality', 'method', 'complete', 'items'}
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['complete']) is not bool
            or not isinstance(value['session_id'], str) or not value['session_id']
            or value['session_id'] != snapshot.get('session_id')
            or value['inventory'] != 'character_main' or value['quality'] != 'normal'
            or value['method'] != 'get_insertable_count'):
        raise ValueError('Invalid actor capacity envelope')
    for key in ('tick', 'actor_unit', 'surface_index', 'force_index'):
        if (type(value[key]) is not int or not (0 if key == 'tick' else 1) <= value[key] <= 2**53 - 1
                or value[key] != snapshot.get(key)):
            raise ValueError('Actor capacity observation identity changed')
    if not value['complete']:
        return None
    counts = value['items']
    if (not isinstance(counts, dict) or set(counts) != ITEMS
            or any(type(count) is not int or not 0 <= count <= 2**32 - 1
                   for count in counts.values())):
        raise ValueError('Invalid actor capacity item counts')
    if primary is None:
        return None
    if primary['items']['coal'] != counts['coal']:
        raise ValueError('Actor capacity differs from primary observation')
    return {**primary, 'items': dict(counts)}
