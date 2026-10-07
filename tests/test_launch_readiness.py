"""Endgame correctness over explicit synthetic evidence, not a native launch run."""
from copy import deepcopy
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest

from jev_factorio import launch_readiness as contract
from jev_factorio.factory_contract import allowed
from jev_factorio.planning.factory import FactoryPlanner, compile_factory
from jev_factorio.planning.ready_work import ReadyWorkPlanner
from jev_factorio.planning.launch import opportunistic
from jev_factorio.planning.demand import SupplyLedger
from jev_factorio.skills import Plan, Step
from test_factory import catalog, machine, recipe, snapshot


def scenario(pad=True, cargo=None, inventory=None):
    data=catalog()
    data.recipes.update({'rocket-part':recipe('rocket-part',{},'rocket-building'),
        'cargo-landing-pad':recipe('cargo-landing-pad',{'steel-plate':1}),
        'satellite':recipe('satellite',{'electronic-circuit':1}),
        'electronic-circuit':recipe('electronic-circuit',{'iron-plate':1})})
    state=snapshot(researched=['rocket-silo'],inventory=inventory or {})
    state.factory['entities'][contract.SILO]=machine('rocket-silo',unit_number=30,rocket_ready=True,rocket_parts=100,parts_required=100)
    state.factory['launch_readiness']={'schema':1,'version':'2.0.77','supported':True,'session_id':state.session_id,
        'tick':state.tick,'actor_unit':10,'surface_index':1,'force_index':1,'fault':False,
        'pad':{'name':'cargo-landing-pad','unit_number':50,'position':{'x':0,'y':2},'accepts':{'raw-fish':True,'satellite':True}} if pad else {},
        'pad_site':{'id':'landing:1','position':{'x':0,'y':2}},
        'fish':{},'silo':{'unit_number':30,'rocket_unit':31,'ready':True,'automatic':False,'cargo_available':True,'cargo':cargo or {}},
        'attempts':{},'receipts':{}}
    return data,state


def step_for(state,data):return FactoryPlanner(data,state,'rocket_launch').plan().steps[0]


def test_ready_rocket_does_not_authorize_launch_without_pad_or_payload():
    data,state=scenario(pad=False,inventory={'cargo-landing-pad':1,'raw-fish':1})
    step=step_for(state,data)
    assert step.action=='factory_launch_pad' and step.allowed(state)
    assert not allowed('factory_launch',{'role':contract.SILO},state)
    state.factory['launch_readiness']['pad']={'name':'cargo-landing-pad','unit_number':50,'position':{'x':0,'y':2},'accepts':{'raw-fish':True,'satellite':True}}
    assert step_for(state,data).action=='factory_launch_payload'
    assert not allowed('factory_launch',{'role':contract.SILO},state)


@pytest.mark.parametrize('item',contract.PAYLOADS)
def test_existing_pad_and_loaded_payload_skip_extra_build_and_load(item):
    data,state=scenario(cargo={item:1})
    step=step_for(state,data)
    assert step.action=='factory_launch' and step.allowed(state) and not step.satisfied(state)
    state.victory=True;state.victory_source='model_claim'
    assert not step.satisfied(state)
    state.victory_source='native:base-game-rocket-launch'
    assert step.satisfied(state)


def test_missing_evidence_fails_closed_even_for_ready_rocket():
    data,state=scenario();state.factory.pop('launch_readiness')
    assert not allowed('factory_launch',{'role':contract.SILO},state)
    plans,reason=compile_factory('rocket_launch',state,data)
    assert plans==[] and 'launch-readiness' in reason


@pytest.mark.parametrize('key,value',[('schema',True),('version','2.1.0'),('supported',False),('tick',-1),
    ('actor_unit',True),('surface_index',0),('session_id','other'),('receipts',None)])
def test_invalid_launch_evidence_never_authorizes_action(key,value):
    data,state=scenario(cargo={'raw-fish':1});state.factory['launch_readiness'][key]=value
    assert not allowed('factory_launch',{'role':contract.SILO},state)


@pytest.mark.parametrize('key,value',[('unit_number',99),('rocket_unit',True),('automatic',True),
    ('cargo_available',False),('cargo',{'iron-plate':1}),('ready',False)])
def test_wrong_or_unready_silo_cannot_launch(key,value):
    data,state=scenario(cargo={'raw-fish':1});state.factory['launch_readiness']['silo'][key]=value
    assert not allowed('factory_launch',{'role':contract.SILO},state)


def test_reachable_fish_beats_satellite_crafting_and_costs_no_fabricated_items():
    data,state=scenario();state.factory['launch_readiness']['fish']={'id':'fish:1','position':{'x':2,'y':0},'reachable':True,'yield':5}
    step=step_for(state,data)
    assert step.action=='factory_launch_fish' and step.allowed(state) and not step.costs
    state.inventory['satellite']=1
    assert step_for(state,data).action=='factory_launch_payload'


def test_owned_chest_payload_precedes_crafting():
    data,state=scenario();state.factory['entities']['stock:1']=machine('wooden-chest',output={'satellite':1})
    assert step_for(state,data).action=='factory_extract'


def test_no_reachable_fish_uses_paid_satellite_recipe():
    data,state=scenario(inventory={'electronic-circuit':1})
    step=step_for(state,data)
    assert step.action=='factory_craft' and step.parameters['recipe']=='satellite' and step.costs=={'electronic-circuit':1}


def test_already_attempted_fish_is_not_retried_by_rotating_target_id():
    data,state=scenario(inventory={'electronic-circuit':1})
    row=state.factory['launch_readiness'];row['fish']={'id':'fish:changed','position':{'x':2,'y':0},'reachable':True,'yield':5}
    row['attempts']['fish']={'receipt':'past-failure'}
    assert step_for(state,data).parameters['recipe']=='satellite'


def test_payload_held_while_rocket_finishes_and_not_spendable_in_forecasts():
    data,state=scenario(inventory={'raw-fish':5,'satellite':1})
    assert SupplyLedger.capture(state,data).carried['raw-fish']==4
    assert SupplyLedger.capture(state,data).carried['satellite']==1
    assert not allowed('factory_insert',{'role':contract.SILO,'item':'raw-fish','quantity':5,'receipt':'x'},state)
    craft=Step('factory_craft','inventory',parameters={'recipe':'test','batches':1},costs={'raw-fish':5})
    assert not craft.allowed(state)
    assert step_for(state,data).parameters['item']=='raw-fish'


def test_exact_receipt_required_not_inventory_alone():
    data,state=scenario(inventory={'raw-fish':1});step=step_for(state,data)
    row=state.factory['launch_readiness'];row['silo']['cargo']={'raw-fish':1}
    assert not step.satisfied(state)
    row['attempts']['load']={'receipt':step.parameters['receipt'],'silo_unit':30,'rocket_unit':31,
        'item':'raw-fish','tick':state.tick}
    row['receipts'][step.parameters['receipt']]={'kind':'load','session_id':state.session_id,'actor_unit':10,
        'tick':state.tick,'quantity':1,'item':'raw-fish','silo_unit':30,'rocket_unit':31}
    assert step.satisfied(state)
    row['receipts'][step.parameters['receipt']]['quantity']=True
    assert not step.satisfied(state)


def test_serialized_pending_command_keeps_exact_receipt_and_identity():
    data,state=scenario(inventory={'raw-fish':1});plan=FactoryPlanner(data,state,'rocket_launch').plan()
    restored=Plan.from_dict(plan.to_dict());assert plan==restored
    state.factory['launch_readiness']['silo']['rocket_unit']=32
    assert not restored.steps[0].allowed(state) and not restored.steps[0].satisfied(state)


def test_opportunistic_fish_only_replaces_passive_work():
    data,state=scenario();state.factory['launch_readiness']['fish']={'id':'fish:1','position':{'x':2,'y':0},'reachable':True,'yield':5}
    planner=ReadyWorkPlanner(data,state,'rocket_launch')
    wait=planner._wait('research_progress','automation',0.1)
    assert opportunistic(planner,wait).steps[0].action=='factory_launch_fish'
    urgent=planner._transfer('utility:lab','automation-science-pack',1)
    assert opportunistic(planner,urgent)==urgent
    planner.allow_service_visits=False;state.factory['crafting_queue']=1
    assert opportunistic(planner,wait)==wait


def lua_case(code):
    lua=pytest.importorskip('lupa').LuaRuntime()
    lua.execute((Path(__file__).parent/'fixtures/launch_runtime.lua').read_text())
    lua.execute(files('jev_factorio').joinpath('lua/launch_readiness.lua').read_text())
    lua.execute(code)
    return lua


def test_lua_pad_is_paid_once_and_receipt_survives_lost_ack():
    lua_case('''local row=observe();local p={site=row.pad_site.id,receipt="p1"}
        storage.campaign.prepare_launch_pad(p);storage.campaign.build_launch_pad(p)
        assert(builds==1 and main.get_item_count("cargo-landing-pad")==0)
        assert(observe().receipts.p1.paid==1 and observe().pad.unit_number==50)
        assert(not pcall(storage.campaign.build_launch_pad,p) and builds==1)''')


def test_lua_ambiguous_pad_failure_cannot_place_again():
    lua_case('''local p={site=observe().pad_site.id,receipt="p1"};placement_error=true
        assert(not pcall(storage.campaign.build_launch_pad,p));assert(builds==1)
        placement_error=false
        assert(not pcall(storage.campaign.build_launch_pad,p) and builds==1)
        assert(observe().attempts.pad.receipt=="p1")''')


def test_lua_uses_existing_pad_and_never_replaces_it():
    lua_case('''add_pad();local row=observe();assert(row.pad.unit_number==50 and not next(row.pad_site))
        assert(not pcall(storage.campaign.build_launch_pad,{site="other",receipt="p1"}))
        assert(builds==0 and main.get_item_count("cargo-landing-pad")==1)''')


def test_lua_fish_uses_timed_mining_controls_and_event_receipt():
    lua_case('''local row=observe();local p={target=row.fish.id,receipt="fish1"}
        storage.campaign.begin_launch_fish(p)
        assert(player.mining_state.mining and storage.fair.job.item=="raw-fish")
        assert(main.get_item_count("raw-fish")==0 and not observe().receipts.fish1)
        game.tick=game.tick+24;harvest_event()
        assert(observe().receipts.fish1.quantity==5 and main.get_item_count("raw-fish")==5)
        assert(not pcall(storage.campaign.begin_launch_fish,p) and mines==1)''')


def test_lua_fish_moving_out_of_reach_or_obscured_has_no_mining_side_effect():
    lua_case('''local p={target=observe().fish.id,receipt="fish1"};fish.reachable=false
        assert(not pcall(storage.campaign.begin_launch_fish,p))
        assert(not next(storage.launch_readiness.attempts) and not player.mining_state)
        fish.reachable=true;obscured=true
        assert(not pcall(storage.campaign.begin_launch_fish,p) and not player.mining_state)''')


def test_lua_foreign_mining_event_cannot_certify_inventory_or_fish_loss():
    lua_case('''local p={target=observe().fish.id,receipt="fish1"};storage.campaign.begin_launch_fish(p)
        events[42]{entity=fish,player_index=99,buffer=make_inventory({["raw-fish"]=5})}
        main.insert{name="raw-fish",count=5};fish.valid=false
        assert(not observe().receipts.fish1)''')


def test_lua_payload_is_transferred_to_exact_rocket_inventory_and_not_recipe_input():
    lua_case('''observe();add_pad();main.insert{name="raw-fish",count=1}
        local p={role="recipe:rocket-part",silo_unit=30,rocket_unit=31,item="raw-fish",receipt="c1"}
        storage.campaign.load_launch_payload(p)
        assert(main.get_item_count("raw-fish")==0 and cargo.get_item_count("raw-fish")==1)
        assert(observe().receipts.c1.quantity==1)
        assert(not pcall(storage.campaign.load_launch_payload,p) and cargo.get_item_count("raw-fish")==1)''')


@pytest.mark.parametrize('change',['rocket.unit_number=32','silo.send_to_orbit_automatically=true',
    'silo.reachable=false','cargo.insert{name="iron-plate",count=1}','cargo.limit=0'])
def test_lua_payload_failure_keeps_paid_item(change):
    lua_case('''observe();add_pad();main.insert{name="raw-fish",count=1};'''+change+'''
        assert(not pcall(storage.campaign.load_launch_payload,{role="recipe:rocket-part",silo_unit=30,
            rocket_unit=31,item="raw-fish",receipt="c1"}))
        assert(main.get_item_count("raw-fish")==1 and not next(storage.launch_readiness.attempts))''')


def test_lua_launch_requires_pad_payload_and_is_never_replayed():
    lua_case('''observe();assert(not pcall(storage.campaign.launch,"recipe:rocket-part"));assert(launches==0)
        add_pad();assert(not pcall(storage.campaign.launch,"recipe:rocket-part"));assert(launches==0)
        cargo.insert{name="raw-fish",count=1};storage.campaign.launch("recipe:rocket-part")
        assert(launches==1 and observe().receipts.launch)
        assert(not pcall(storage.campaign.launch,"recipe:rocket-part") and launches==1)''')


def test_lua_lost_launch_ack_keeps_intent_without_replay():
    lua_case('''observe();add_pad();cargo.insert{name="raw-fish",count=1};launch_error=true
        assert(not pcall(storage.campaign.launch,"recipe:rocket-part") and launches==1)
        launch_error=false;assert(not pcall(storage.campaign.launch,"recipe:rocket-part") and launches==1)
        assert(observe().attempts.launch and not observe().receipts.launch)''')


def test_lua_reservation_preserves_ordinary_transfer_signature_and_read_only_extraction():
    lua_case('''observe();add_pad();main.insert{name="raw-fish",count=1}
        assert(not pcall(storage.campaign.transfer,"stock:1","raw-fish",1,"id",false))
        storage.campaign.transfer("stock:1","raw-fish",1,"id",true)
        assert(transfer_seen.receipt=="id" and transfer_seen.extracting==true)
        storage.campaign.transfer("recipe:iron-plate","iron-ore",50,"id2",false)
        assert(transfer_seen.receipt=="id2" and transfer_seen.extracting==false)''')


@pytest.mark.parametrize('change',['script.active_mods.base="2.1.0"','script.active_mods["space-age"]="2.0.77"'])
def test_lua_unsupported_version_or_mod_cannot_prepare_or_launch(change):
    lua_case(change+''';local row=observe();assert(not row.supported)
        assert(not pcall(storage.campaign.launch,"recipe:rocket-part") and launches==0 and builds==0)''')


def test_lua_pad_proposals_respect_two_tile_grid_from_odd_actor_position():
    lua_case('''player.position={x=1,y=3}
        surface.can_place_entity=function(q) return q.position.x%2==0 and q.position.y%2==0 end
        local row=observe();assert(row.pad_site.id)
        assert(row.pad_site.position.x%2==0 and row.pad_site.position.y%2==0 and builds==0)''')


def test_lua_harvest_handler_reattachment_keeps_one_previous_callback():
    lua=lua_case('''previous_calls=0''')
    # Install another ordinary scenario callback, then attach twice: no recursive chain.
    lua.execute('events[42]=function(_) previous_calls=previous_calls+1 end')
    source=files('jev_factorio').joinpath('lua/launch_readiness.lua').read_text()
    lua.execute(source);lua.execute(source)
    lua.execute('''local p={target=observe().fish.id,receipt="fish1"};storage.campaign.begin_launch_fish(p)
        harvest_event();assert(previous_calls==1 and observe().receipts.fish1.quantity==5)''')


def test_lua_paid_payload_partial_insert_refunds_but_never_replays():
    lua_case('''observe();add_pad();main.insert{name="satellite",count=1}
        cargo.insert=function(_)return 0 end
        local p={role="recipe:rocket-part",silo_unit=30,rocket_unit=31,item="satellite",receipt="c1"}
        assert(not pcall(storage.campaign.load_launch_payload,p))
        assert(main.get_item_count("satellite")==1 and observe().attempts.load and not observe().receipts.c1)
        assert(not pcall(storage.campaign.load_launch_payload,p) and main.get_item_count("satellite")==1)''')


def test_lua_reattachment_keeps_paid_pad_and_submitted_launch_intents():
    lua=lua_case('''local p={site=observe().pad_site.id,receipt="p1"};storage.campaign.build_launch_pad(p)
        cargo.insert{name="raw-fish",count=1};storage.campaign.launch("recipe:rocket-part")''')
    lua.execute(files('jev_factorio').joinpath('lua/launch_readiness.lua').read_text())
    lua.execute('''assert(observe().receipts.p1.paid==1 and observe().attempts.launch)
        assert(not pcall(storage.campaign.launch,"recipe:rocket-part") and launches==1 and builds==1)''')


def test_backend_pad_walks_and_uses_native_receipt_in_one_build_rpc():
    from types import SimpleNamespace
    from jev_factorio.backends.launch_readiness import execute
    calls=[]
    class Native:
        backend=SimpleNamespace(_fair=SimpleNamespace(approach=lambda p,n:calls.append(('approach',p.x,p.y,n))))
        def require_launch_reconciliation(self):
            return None
        def call(self, function, parameters):
            calls.append((function,dict(parameters)))
            return '{"name":"cargo-landing-pad","position":{"x":2,"y":4}}'
    parameters={'site':'s1','receipt':'p1'}
    execute(Native(),'factory_launch_pad',parameters)
    assert [c[0] for c in calls]==['prepare_launch_pad','approach','build_launch_pad']
    assert calls[0][1]==calls[-1][1]==parameters


def test_backend_fish_does_not_walk_or_instantly_mine():
    from types import SimpleNamespace
    from jev_factorio.backends.launch_readiness import execute
    calls=[]
    native=SimpleNamespace(call=lambda *args:calls.append(args),
        require_launch_reconciliation=lambda:None,
        backend=SimpleNamespace(_fair=SimpleNamespace(wait=lambda **kwargs:calls.append(('wait',kwargs)))))
    execute(native,'factory_launch_fish',{'target':'fish:1','receipt':'f1'})
    assert [c[0] for c in calls]==['begin_launch_fish','wait']


def test_lua_crafting_cannot_eat_reserved_payload_but_other_recipes_are_unchanged():
    lua_case('''observe();add_pad();main.insert{name="raw-fish",count=1}
        force.recipes["bad"]={ingredients={{type="item",name="raw-fish",amount=1}}}
        force.recipes["good"]={ingredients={{type="item",name="iron-plate",amount=1}}}
        assert(not pcall(storage.campaign.craft,"bad",1))
        assert(not pcall(storage.campaign.launch_assert_spend,{["raw-fish"]=1}))
        storage.campaign.craft("good",2);assert(craft_seen.name=="good" and craft_seen.batches==2)''')


def test_new_launch_commands_keep_existing_invalid_ownership_barriers():
    data,state=scenario(inventory={'raw-fish':1})
    step=step_for(state,data)
    state.factory['input_routes']={'schema':True,'sources':{'invalid':{}}}
    assert not step.allowed(state)
    state.factory['launch_readiness']['silo']['cargo']={'raw-fish':1}
    assert not allowed('factory_launch',{'role':contract.SILO},state)


def test_automatic_loaded_rocket_reports_blocker_without_changing_settings():
    data,state=scenario(cargo={'raw-fish':1})
    state.factory['launch_readiness']['silo']['automatic']=True
    plans,reason=compile_factory('rocket_launch',state,data)
    assert not plans and 'Automatic launch' in reason
    assert state.factory['launch_readiness']['silo']['automatic'] is True


@pytest.mark.parametrize('action',['pad','fish','load','launch'])
def test_controller_restart_reconciles_exact_receipt_after_lost_ack_without_duplicate_action(tmp_path,action):
    from jev_factorio.controller import HierarchicalLoop
    data,state=scenario(pad=action!='pad',inventory={'cargo-landing-pad':1} if action=='pad' else {'raw-fish':1})
    row=state.factory['launch_readiness']
    if action=='fish':
        state.inventory={}
        row['fish']={'id':'fish:1','position':{'x':2,'y':0},'reachable':True,'yield':5}
    if action=='launch': row['silo']['cargo']={'raw-fish':1}
    plan=FactoryPlanner(data,state,'rocket_launch').plan()
    calls=[]
    class Backend:
        def enable_factory(self):return data
        def observe(self):return deepcopy(state)
        def execute(self,kind,p):
            calls.append((kind,deepcopy(p)))
            state.tick+=1;row['tick']=state.tick
            base={'kind':action,'session_id':state.session_id,'actor_unit':row['actor_unit'],'tick':state.tick}
            if action=='pad':
                state.inventory['cargo-landing-pad']-=1
                row['pad']={'name':'cargo-landing-pad','unit_number':50,'position':{'x':0,'y':2},'accepts':{'raw-fish':True,'satellite':True}}
                row['receipts'][p['receipt']]={**base,'site':p['site'],'paid':1,'unit_number':50}
            elif action=='fish':
                state.inventory['raw-fish']=5
                row['receipts'][p['receipt']]={**base,'target':p['target'],'quantity':5}
            elif action=='load':
                state.inventory['raw-fish']-=1;row['silo']['cargo']={'raw-fish':1}
                row['receipts'][p['receipt']]={**base,'item':p['item'],'silo_unit':30,'rocket_unit':31,'quantity':1}
                row['attempts'][action]={'receipt':p['receipt'],'silo_unit':30,'rocket_unit':31,
                    'item':p['item'],'tick':state.tick}
            else:
                state.victory=True;state.victory_source='native:base-game-rocket-launch'
            if action!='load':row['attempts'][action]={'tick':state.tick}
            raise TimeoutError('Synthetic lost native acknowledgment')
    backend=Backend()
    def make(resume):
        loop=HierarchicalLoop(backend,policy='deterministic',target='rocket_launch',
            factory_scheduling='ready-work',checkpoint=str(tmp_path/'controller.json'),resume_controller=resume,tick_seconds=0)
        if not resume:
            loop.memory=loop.memory_type(state.session_id,'rocket_launch',active_goal='rocket_launch',
                completed_goals={goal:1 for goal in loop.order[:-1]},last_tick=state.tick,failures={'historic':2})
        loop._compile_candidates=lambda observation:([plan],'')
        return loop
    first=make(False);record=first.step()
    assert len(calls)==1 and first.memory.pending['dispatch']=='ambiguous'
    resumed=make(True);result=resumed.step()
    assert len(calls)==1 and resumed.memory.pending is None
    assert resumed.memory.failures['historic']==2
    assert result['verified'] or resumed.memory.status=='completed'


def test_payload_reservation_is_additional_to_other_committed_stock():
    data,state=scenario(inventory={'raw-fish':5})
    ledger=SupplyLedger.capture(state,data,reserved={'raw-fish':2})
    assert ledger.carried['raw-fish']==2 and ledger.reserved['raw-fish']==3


def test_full_existing_pad_prevents_satellite_load_and_launch_not_fish():
    data,state=scenario(inventory={'satellite':1})
    state.factory['launch_readiness']['pad']['accepts']['satellite']=False
    plans,reason=compile_factory('rocket_launch',state,data)
    assert not plans and 'lacks room' in reason
    state.factory['launch_readiness']['silo']['cargo']={'satellite':1}
    assert not contract.ready(state)
    state.factory['launch_readiness']['silo']['cargo']={'raw-fish':1}
    assert contract.ready(state)


def test_lua_full_pad_refuses_satellite_without_spending_or_recording_a_launch_attempt():
    lua_case('''observe();add_pad();pad_full=true;main.insert{name="satellite",count=1}
        local p={role="recipe:rocket-part",silo_unit=30,rocket_unit=31,item="satellite",receipt="c1"}
        assert(not pcall(storage.campaign.load_launch_payload,p) and main.get_item_count("satellite")==1)
        assert(not observe().attempts.load)
        cargo.insert{name="satellite",count=1}
        assert(not pcall(storage.campaign.launch,"recipe:rocket-part") and launches==0)
        assert(not observe().attempts.launch)''')


@pytest.mark.parametrize('cargo',[{'raw-fish':2},{'satellite':2},{'raw-fish':1,'satellite':1}])
def test_multiple_payloads_are_not_silently_consumed_or_misclassified(cargo):
    data,state=scenario(cargo=cargo)
    assert not contract.ready(state)
    plans,reason=compile_factory('rocket_launch',state,data)
    assert not plans and 'Unexpected rocket cargo' in reason


def test_lua_mixed_cargo_does_not_bypass_satellite_destination_capacity():
    lua_case('''observe();add_pad();pad_full=true
        cargo.insert{name="raw-fish",count=1};cargo.insert{name="satellite",count=1}
        assert(not pcall(storage.campaign.launch,"recipe:rocket-part") and launches==0)
        assert(not storage.launch_readiness.attempts.launch and cargo.get_item_count("satellite")==1)''')


def test_reattachment_with_interposed_wrappers_does_not_create_recursive_callbacks():
    lua=lua_case('''observe();add_pad();previous_calls=0
        local old=events[42]
        events[42]=function(event) previous_calls=previous_calls+1;old(event) end
        local observed=storage.campaign.observe
        storage.campaign.observe=function()return observed() end
        local transferred=storage.campaign.transfer
        storage.campaign.transfer=function(...)return transferred(...) end''')
    lua.execute(files('jev_factorio').joinpath('lua/launch_readiness.lua').read_text())
    lua.execute('''local p={target=observe().fish.id,receipt="fish1"}
        storage.campaign.begin_launch_fish(p);harvest_event()
        assert(previous_calls==1 and observe().receipts.fish1.quantity==5)
        storage.campaign.transfer("stock:1","iron-plate",1,"t1",false)
        assert(transfer_seen.receipt=="t1")''')


@pytest.mark.parametrize('room', [0, 1, 999])
def test_partial_pad_capacity_does_not_authorize_loading_or_launch(room):
    lua=lua_case('')
    lua.execute(f'add_pad();pad_room={room};main.stock["satellite"]=1')
    lua.execute('''local row=observe()
        assert(row.pad.accepts.satellite==false)
        assert(not pcall(storage.campaign.load_launch_payload,{
            role="recipe:rocket-part",silo_unit=30,rocket_unit=31,item="satellite",receipt="short-pad"}))
        assert(not storage.launch_readiness.attempts.load and main.stock.satellite==1 and cargo.is_empty())
        cargo.stock.satellite=1
        assert(not pcall(storage.campaign.launch,"recipe:rocket-part"))
        assert(not storage.launch_readiness.attempts.launch and launches==0)
    ''')


@pytest.mark.parametrize('room', [0, 1, 4])
def test_partial_main_inventory_capacity_does_not_start_fish_mining(room):
    lua=lua_case('');lua.execute(f'main.limit={room}')
    lua.execute('''local row=observe()
        assert(not pcall(storage.campaign.begin_launch_fish,{target=row.fish.id,receipt="short-fish"}))
        assert(not storage.launch_readiness.attempts.fish and mines==0 and fish.valid)
    ''')


def test_exact_five_item_fish_capacity_is_enough():
    lua=lua_case('');lua.execute('''main.limit=5;local row=observe()
        storage.campaign.begin_launch_fish{target=row.fish.id,receipt="five-fish"}
        harvest_event()
        assert(main.get_item_count("raw-fish")==5 and storage.launch_readiness.receipts["five-fish"])
    ''')
