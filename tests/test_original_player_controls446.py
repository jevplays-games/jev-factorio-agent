from importlib.resources import files

import pytest


def _fair_runtime(player_index=1):
    runtime = pytest.importorskip("lupa.lua54").LuaRuntime()
    runtime.globals().configured_player_index = player_index
    runtime.execute("""
        handlers = {}
        defines = {
            controllers = {character = 1, god = 2, editor = 3, spectator = 4,
                           ghost = 5, cutscene = 6, remote = 7},
            events = {on_tick = 1, on_script_path_request_finished = 2},
            build_check_type = {manual = 1},
            direction = {north = 0, northeast = 2, east = 4, southeast = 6,
                         south = 8, southwest = 10, west = 12, northwest = 14}
        }
        setter_attempts = {}
        prototypes = {entity = {pipe = {}, ["offshore-pump"] = {}}}
        script = {
            get_event_handler = function(event) return handlers[event] end,
            on_event = function(event, callback) handlers[event] = callback end,
            on_nth_tick = function(interval, callback)
                handlers["nth" .. interval] = callback
            end
        }
        function make_character(unit)
            return {valid = true, unit_number = unit,
                prototype = {collision_box = {}, collision_mask = {}}}
        end
        function make_player(index)
            local character = make_character(index + 8)
            local input_state = {
                walking_state = {walking = false},
                mining_state = {mining = false}
            }
            local player = {
                index = index, connected = true, character = character,
                valid = true, controller_type = defines.controllers.character,
                cheat_mode = false, position = {x = 0, y = 0},
                surface = nil, force = {},
                get_item_count = function() return 0 end,
                can_reach_entity = function() return true end,
                update_selected_entity = function(position)
                    player.selected = resource
                end
            }
            setmetatable(player, {
                __index = function(_, key)
                    if key == "walking_state" or key == "mining_state" then
                        return input_state[key]
                    end
                end,
                __newindex = function(self, key, value)
                    if key == "walking_state" or key == "mining_state" then
                        local controller = rawget(self, "controller_type")
                        setter_attempts[#setter_attempts + 1] = {
                            property = key, controller_type = controller
                        }
                        local supported = controller == defines.controllers.character
                            or controller == defines.controllers.god
                            or controller == defines.controllers.editor
                        if not supported then
                            error("Factorio LuaPlayer." .. key
                                .. " write rejected for controller " .. tostring(controller))
                        end
                        if rawget(self, "fail_setter") == key then
                            rawset(self, "fail_setter", nil)
                            error("API-shaped setter failure: " .. key)
                        end
                        input_state[key] = value
                    else
                        rawset(self, key, value)
                    end
                end
            })
            return player, character
        end
        resource = {
            valid = true, minable = true, name = "coal", surface = {index = 1},
            position = {x = 2, y = 0}
        }
        local primary, primary_character = make_player(1)
        local secondary, secondary_character = make_player(2)
        surface = {
            index = 1,
            request_path = function(parameters)
                requested_path = parameters
                return 17
            end,
            find_entities_filtered = function() return {resource} end
        }
        primary.surface, secondary.surface = surface, surface
        player = configured_player_index == 1 and primary or secondary
        other_player = configured_player_index == 1 and secondary or primary
        character = configured_player_index == 1 and primary_character or secondary_character
        players = {[1] = primary, [2] = secondary}
        game = {
            tick = 0, speed = 1,
            get_player = function(index) return players[index] end
        }
        storage = {
            jev_player_index = configured_player_index,
            agent_characters = {character}
        }
        script.on_event(defines.events.on_tick, function()
            prior_tick_calls = (prior_tick_calls or 0) + 1
        end)
        prior_tick_calls = 0
    """)
    runtime.execute(files("jev_factorio").joinpath("lua/fair_actions.lua").read_text())
    runtime.execute("storage.fair.bind()")
    return runtime


@pytest.fixture
def fair_runtime():
    return _fair_runtime()


def _start_walking(runtime):
    runtime.execute("storage.fair.begin_move{x = 2, y = 0}")
    runtime.execute("""handlers[2]{id = 17, path = {
        {position = {x = 0, y = 0}}, {position = {x = 2, y = 0}}
    }}""")
    runtime.execute("handlers[1]{}")


@pytest.mark.parametrize("kind", ["walk", "mine"])
def test_replaced_character_failure_clears_owned_player_inputs_without_rebinding(
    fair_runtime, kind
):
    runtime = fair_runtime
    if kind == "walk":
        _start_walking(runtime)
        assert runtime.eval("player.walking_state.walking") is True
        assert runtime.eval("storage.fair.job.status") == "walking"
    else:
        runtime.execute('storage.fair.begin_mine({x = 2, y = 0}, "coal", 2)')
        assert runtime.eval("player.mining_state.mining") is True

    runtime.execute("""
        saved_job_unit = storage.fair.job.unit
        saved_job_lease = storage.fair.job.lease
        saved_job_request = storage.fair.job.request
        saved_job_path_requests = storage.fair.job.path_requests
        saved_job_path_deadline = storage.fair.job.path_deadline
        saved_job_entity = storage.fair.job.entity
        saved_job_quantity = storage.fair.job.quantity
        other_player.walking_state = {walking = true, direction = 4}
        other_player.mining_state = {mining = true}
        replacement = make_character(99)
        replacement.marker = "must-not-be-mutated"
        player.character = replacement
        handlers[1]{}
    """)

    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("storage.fair.job.error") == "Fair player/session invariant failed"
    assert runtime.eval("storage.fair.job.unit") == 9
    assert runtime.eval("storage.fair.job.unit == saved_job_unit") is True
    assert runtime.eval("storage.fair.job.lease == saved_job_lease") is True
    if kind == "walk":
        assert runtime.eval("storage.fair.job.request == saved_job_request") is True
        assert runtime.eval("storage.fair.job.path_requests == saved_job_path_requests") is True
        assert runtime.eval("storage.fair.job.path_deadline == saved_job_path_deadline") is True
    else:
        assert runtime.eval("storage.fair.job.entity == saved_job_entity") is True
        assert runtime.eval("storage.fair.job.quantity == saved_job_quantity") is True
    assert runtime.eval("player.character == replacement") is True
    assert runtime.eval("storage.agent_characters[1] == character") is True
    assert runtime.eval("replacement.marker") == "must-not-be-mutated"
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False
    assert runtime.eval("other_player.walking_state.walking") is True
    assert runtime.eval("other_player.mining_state.mining") is True
    assert runtime.eval("prior_tick_calls") == 0


@pytest.mark.parametrize("broken_binding", [
    "player.connected = false",
    "storage.agent_characters[1] = nil",
    "player.character = nil",
    "character.valid = false",
])
def test_actor_failure_clears_or_skips_owned_inputs_for_disconnected_or_invalid_character(
    fair_runtime, broken_binding
):
    runtime = fair_runtime
    _start_walking(runtime)
    runtime.execute("other_player.walking_state = {walking = true, direction = 4}")
    runtime.execute("setter_attempts = {}")
    runtime.execute(broken_binding)
    runtime.execute("handlers[1]{}")

    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("other_player.walking_state.walking") is True
    assert runtime.eval("storage.agent_characters[1] == nil") is (
        broken_binding == "storage.agent_characters[1] = nil"
    )
    if broken_binding in {"player.character = nil", "character.valid = false"}:
        assert runtime.eval("#setter_attempts") == 0
        assert runtime.eval("player.walking_state.walking") is True
        assert runtime.eval("storage.fair.job.cleanup_result") == "skipped:invalid_character"
    else:
        assert runtime.eval("#setter_attempts") == 2
        assert runtime.eval("player.walking_state.walking") is False
        assert runtime.eval("player.mining_state.mining") is False


def test_nondefault_player_selection_drift_cleans_only_the_bound_player():
    runtime = _fair_runtime(player_index=2)
    runtime.execute('storage.fair.begin_mine({x = 2, y = 0}, "coal", 2)')
    runtime.execute("""
        other_player.walking_state = {walking = true, direction = 4}
        other_player.mining_state = {mining = true}
        storage.jev_player_index = 1
        handlers[1]{}
    """)

    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("storage.fair.job.unit") == 10
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False
    assert runtime.eval("other_player.walking_state.walking") is True
    assert runtime.eval("other_player.mining_state.mining") is True
    assert runtime.eval("player.character == character") is True


def test_expired_walking_lease_clears_inputs_and_ignores_late_path_for_job():
    runtime = _fair_runtime()
    _start_walking(runtime)
    runtime.execute("""
        other_player.mining_state = {mining = true}
        game.tick = 181
        handlers[1]{}
        late_path_status = storage.fair.job.status
        handlers[2]{id = 17, path = {
            {position = {x = 0, y = 0}}, {position = {x = 2, y = 0}}
        }}
    """)

    assert runtime.eval("late_path_status") == "failed"
    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("storage.fair.job.error") == "Control lease expired or character changed"
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False
    assert runtime.eval("other_player.mining_state.mining") is True


def test_path_pending_lease_expiry_clears_inputs_before_delayed_result():
    runtime = _fair_runtime()
    runtime.execute("""
        storage.fair.begin_move{x = 2, y = 0}
        assert(storage.fair.job.status == "path_pending")
        local request_id = storage.fair.job.request
        local lease = storage.fair.job.lease
        player.walking_state = {walking = true, direction = 4}
        player.mining_state = {mining = true}
        other_player.walking_state = {walking = true, direction = 4}
        game.tick = lease + 1
        handlers[1]{}
        assert(storage.fair.job.status == "failed")
        assert(storage.fair.job.error == "Control lease expired or character changed")
        assert(storage.fair.job.request == request_id)
        handlers[2]{id = request_id, path = {
            {position = {x = 0, y = 0}}, {position = {x = 2, y = 0}}
        }}
    """)

    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False
    assert runtime.eval("other_player.walking_state.walking") is True


@pytest.mark.parametrize("kind", ["walk", "mine"])
def test_same_actor_jobs_still_complete_and_clear_controls(fair_runtime, kind):
    runtime = fair_runtime
    if kind == "walk":
        _start_walking(runtime)
        runtime.execute("player.position = {x = 2, y = 0}; handlers[1]{}")
    else:
        runtime.execute('storage.fair.begin_mine({x = 2, y = 0}, "coal", 1)')
        runtime.execute("handlers[1]{}")
        assert runtime.eval("player.mining_state.mining") is True
        runtime.execute("player.get_item_count = function() return 1 end; handlers[1]{}")

    assert runtime.eval("storage.fair.job.status") == "completed"
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False


@pytest.mark.parametrize("mode", ["character", "god", "editor"])
@pytest.mark.parametrize("kind", ["walk", "mine"])
def test_supported_controller_modes_terminalize_replacement_and_clear_inputs(
    fair_runtime, mode, kind
):
    runtime = fair_runtime
    if kind == "walk":
        _start_walking(runtime)
    else:
        runtime.execute('storage.fair.begin_mine({x = 2, y = 0}, "coal", 2)')
    runtime.execute(f"""
        setter_attempts = {{}}
        original_unit = storage.fair.job.unit
        original_lease = storage.fair.job.lease
        original_request = storage.fair.job.request
        replacement = make_character(99)
        player.controller_type = defines.controllers.{mode}
        player.character = replacement
        handler_ok = pcall(handlers[1], {{}})
    """)

    assert runtime.eval("handler_ok") is True
    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("storage.fair.job.error") == "Fair player/session invariant failed"
    assert runtime.eval("storage.fair.job.unit == original_unit") is True
    assert runtime.eval("storage.fair.job.lease == original_lease") is True
    assert runtime.eval("storage.fair.job.request == original_request") is True
    assert runtime.eval("player.character == replacement") is True
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False
    assert runtime.eval("#setter_attempts") == 2
    assert runtime.eval("other_player.walking_state.walking") is False
    assert runtime.eval("other_player.mining_state.mining") is False


@pytest.mark.parametrize("mode", ["spectator", "ghost"])
@pytest.mark.parametrize("kind", ["walk", "mine"])
def test_unsupported_controller_modes_skip_setters_and_still_fail_original_job(
    fair_runtime, mode, kind
):
    runtime = fair_runtime
    if kind == "walk":
        _start_walking(runtime)
    else:
        runtime.execute('storage.fair.begin_mine({x = 2, y = 0}, "coal", 2)')
    runtime.execute(f"""
        original_unit = storage.fair.job.unit
        original_lease = storage.fair.job.lease
        original_request = storage.fair.job.request
        original_path_deadline = storage.fair.job.path_deadline
        setter_attempts = {{}}
        player.controller_type = defines.controllers.{mode}
        player.character = nil
        handler_ok = pcall(handlers[1], {{}})
    """)

    assert runtime.eval("handler_ok") is True
    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("storage.fair.job.error") == "Fair player/session invariant failed"
    assert runtime.eval("storage.fair.job.unit == original_unit") is True
    assert runtime.eval("storage.fair.job.lease == original_lease") is True
    assert runtime.eval("storage.fair.job.request == original_request") is True
    assert runtime.eval("storage.fair.job.path_deadline == original_path_deadline") is True
    assert runtime.eval("#setter_attempts") == 0
    assert runtime.eval("storage.fair.job.cleanup_result") == "skipped:unsupported_controller"
    assert runtime.eval("other_player.walking_state.walking") is False
    assert runtime.eval("other_player.mining_state.mining") is False


@pytest.mark.parametrize("property_name", ["walking_state", "mining_state"])
def test_cleanup_setter_fault_is_reported_after_failed_job_is_terminal(
    fair_runtime, property_name
):
    runtime = fair_runtime
    _start_walking(runtime)
    runtime.execute(f"""
        original_unit = storage.fair.job.unit
        original_lease = storage.fair.job.lease
        original_request = storage.fair.job.request
        original_path_deadline = storage.fair.job.path_deadline
        setter_attempts = {{}}
        replacement = make_character(99)
        player.fail_setter = "{property_name}"
        player.character = replacement
        handler_ok, handler_error = pcall(handlers[1], {{}})
    """)

    assert runtime.eval("handler_ok") is False
    assert "API-shaped setter failure" in runtime.eval("handler_error")
    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("storage.fair.job.error") == "Fair player/session invariant failed"
    expected_error = f"API-shaped setter failure: {property_name}"
    assert expected_error in runtime.eval("storage.fair.job.cleanup_error")
    assert runtime.eval("storage.fair.job.cleanup_error") == runtime.eval("handler_error")
    assert runtime.eval("storage.fair.job.unit == original_unit") is True
    assert runtime.eval("storage.fair.job.lease == original_lease") is True
    assert runtime.eval("storage.fair.job.request == original_request") is True
    assert runtime.eval("storage.fair.job.path_deadline == original_path_deadline") is True
    assert runtime.eval("#setter_attempts") == 2
    assert runtime.eval("player.walking_state.walking") is (property_name == "walking_state")
    assert runtime.eval("player.mining_state.mining") is False
    runtime.execute("storage.fair.stop()")
    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("storage.fair.job.error") == "Fair player/session invariant failed"
    assert expected_error in runtime.eval("storage.fair.job.cleanup_error")
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False


def test_cleanup_fault_cannot_turn_reached_goal_into_completed_job(fair_runtime):
    runtime = fair_runtime
    _start_walking(runtime)
    runtime.execute("""
        original_unit = storage.fair.job.unit
        original_lease = storage.fair.job.lease
        player.position = {x = 2, y = 0}
        player.fail_setter = "mining_state"
        handler_ok, handler_error = pcall(handlers[1], {})
    """)

    assert runtime.eval("handler_ok") is False
    assert "API-shaped setter failure: mining_state" in runtime.eval("handler_error")
    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("storage.fair.job.unit == original_unit") is True
    assert runtime.eval("storage.fair.job.lease == original_lease") is True
    assert runtime.eval("storage.fair.job.error").startswith(
        "Player controls could not be cleared:"
    )
    assert "API-shaped setter failure: mining_state" in runtime.eval(
        "storage.fair.job.cleanup_error"
    )


@pytest.mark.parametrize("unavailable", ["missing", "invalid"])
def test_unavailable_selected_player_is_not_written_but_job_is_terminal(
    fair_runtime, unavailable
):
    runtime = fair_runtime
    _start_walking(runtime)
    runtime.execute("""
        original_unit = storage.fair.job.unit
        original_lease = storage.fair.job.lease
        setter_attempts = {}
    """)
    if unavailable == "missing":
        runtime.execute("players[1] = nil")
    else:
        runtime.execute("player.valid = false; player.character = make_character(99)")
    runtime.execute("handler_ok = pcall(handlers[1], {})")

    assert runtime.eval("handler_ok") is True
    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("storage.fair.job.error") == "Fair player/session invariant failed"
    assert runtime.eval("storage.fair.job.unit == original_unit") is True
    assert runtime.eval("storage.fair.job.lease == original_lease") is True
    assert runtime.eval("#setter_attempts") == 0
