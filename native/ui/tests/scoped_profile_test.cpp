// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Pure semantic profile tests: no Session, GPU, DLL, file or clock access.
#include "scoped_profile.h"
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <functional>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static void rejects(const std::function<void()>& action){bool failed=false;try{action();}catch(const std::exception&){failed=true;}CHECK(failed);}
static ScopedCurrent rail(uint32_t index,uint32_t field,int64_t value){
    ScopedCurrent r;r.key=ScopedKey::rail_field(index,field);r.unit="uV";r.binding="rail:"+std::to_string(index)+":"+std::to_string(field)+":3:408c200000000000";
    r.value=value;r.low=-100000;r.high=500000;r.fresh=r.writable=true;return r;
}
static ScopedCurrent policy(uint32_t id,int64_t value,const char* unit="mA"){
    ScopedCurrent r;r.key=ScopedKey::policy_request(id);r.unit=unit;r.binding="policy:"+std::to_string(id)+":7:9:"+unit+":0:300000";
    r.value=value;r.low=0;r.high=300000;r.fresh=r.writable=true;r.pin_value=value;return r;
}
static ScopedCurrent board(int64_t value){
    ScopedCurrent r;r.key=ScopedKey::board_power();r.unit="mW";r.binding="board:qualified-current-route:geometry-4";
    r.value=value;r.low=10000;r.high=400000;r.fresh=r.writable=true;return r;
}
int main(){
    const auto r0=ScopedKey::rail_field(0,0),r1=ScopedKey::rail_field(1,2),p3=ScopedKey::policy_request(3),b=ScopedKey::board_power();
    ScopedSnapshot state;state.target={"GPU-alternate-identity","0000:03:00.0"};state.entries={rail(0,0,-6250),policy(3,123456)};
    auto doc=capture_scoped_profile(state,{r0,p3});auto plan=plan_scoped_profile(doc,state);
    CHECK(plan.steps.size()==2&&plan.steps[0].key==p3&&plan.steps[1].key==r0);
    CHECK(!plan.steps[0].changed&&!plan.steps[1].changed&&plan.coupled_power);
    CHECK(plan.steps[0].command.power_policy==3&&plan.steps[0].command.expected_binding==state.entries[1].binding);
    CHECK(plan.steps[1].command.rail->rail==0&&plan.steps[1].command.value==-6.25);
    CHECK(scoped_profile_readback_error(plan,state).empty());
    // Missing unrelated rails/policies, and malformed unselected data, have no
    // bearing on a valid selected subset. Duplicate selected identities refuse.
    state.entries.push_back(rail(1,2,1000));state.entries.back().fresh=false;
    state.entries.push_back(state.entries.back());
    CHECK(plan_scoped_profile(doc,state).steps.size()==2);
    rejects([&]{capture_scoped_profile(state,{r0,r0});});
    rejects([&]{capture_scoped_profile(state,{r1});});
    auto duplicate=doc;duplicate["entries"].push_back(duplicate["entries"][0]);rejects([&]{plan_scoped_profile(duplicate,state);});
    auto duplicated_current=state;duplicated_current.entries.push_back(state.entries[0]);rejects([&]{plan_scoped_profile(doc,duplicated_current);});
    auto missing=state;missing.entries.erase(missing.entries.begin());rejects([&]{plan_scoped_profile(doc,missing);});
    // No startup, reset replay, unknown scopes, units, identity or geometry.
    for(const auto* key:{"uuid","slot"}){auto changed=doc;changed["target"][key]="other";rejects([&]{plan_scoped_profile(changed,state);});}
    auto bad=doc;bad["startup"]=true;rejects([&]{plan_scoped_profile(bad,state);});
    bad=doc;bad["version"]=true;rejects([&]{plan_scoped_profile(bad,state);});
    bad=doc;bad["entries"][0]["unit"]="A";rejects([&]{plan_scoped_profile(bad,state);});
    bad=doc;bad["entries"][0]["kind"]="opaque-state";rejects([&]{plan_scoped_profile(bad,state);});
    bad=doc;bad["entries"][0]["binding"]="changed-layout";rejects([&]{plan_scoped_profile(bad,state);});
    bad=doc;bad["entries"][0]["extra"]=0;rejects([&]{plan_scoped_profile(bad,state);});
    bad=doc;bad["entries"][0]["policy"]=32;rejects([&]{plan_scoped_profile(bad,state);});
    bad=doc;bad["entries"][1]["field"]=-1;rejects([&]{plan_scoped_profile(bad,state);});
    bad=doc;bad["entries"][1]["value"]=0.5;rejects([&]{plan_scoped_profile(bad,state);});
    bad=doc;bad["entries"][1]["value"]=INT64_MIN;rejects([&]{plan_scoped_profile(bad,state);});
    bad=doc;bad["entries"][0]["value"]=UINT64_MAX;rejects([&]{plan_scoped_profile(bad,state);});
    bad=doc;bad["entries"][0]["value"]=UINT32_MAX;rejects([&]{plan_scoped_profile(bad,state);});
    auto narrow=state;narrow.entries[0].low=0;rejects([&]{plan_scoped_profile(doc,narrow);});
    auto gated=state;gated.entries[0].writable=false;
    CHECK(capture_scoped_profile(gated,{r0}).size()==4); // save remains read-only
    rejects([&]{plan_scoped_profile(doc,gated);});
    auto preview=plan_scoped_profile(doc,gated,false);CHECK(!preview.steps[1].writable);
    gated.entries[0].fresh=false;rejects([&]{plan_scoped_profile(doc,gated,false);});
    // Exact raw units survive tiny signed values and boundary requests through
    // the same 1000:1 display conversion used by native writers.
    ScopedSnapshot precision;precision.target=state.target;precision.entries={rail(0,0,1)};
    precision.entries[0].low=INT32_MIN;precision.entries[0].high=INT32_MAX;
    auto precise=capture_scoped_profile(precision,{r0});CHECK(plan_scoped_profile(precise,precision).steps[0].command.value==0.001);
    precise["entries"][0]["value"]=INT32_MAX;auto boundary=plan_scoped_profile(precise,precision);
    CHECK(std::abs(boundary.steps[0].command.value*1000-INT32_MAX)<0.000001);
    precise["entries"][0]["value"]=INT32_MIN;CHECK(plan_scoped_profile(precise,precision).steps[0].value==INT32_MIN);
    // Pin intent is explicit even when the configured request already matches.
    auto unpinned=state;unpinned.entries[1].pin_value.reset();plan=plan_scoped_profile(doc,unpinned);
    CHECK(plan.steps[0].changed&&plan.steps[0].execute_required);
    CHECK(!scoped_profile_readback_error(plan,unpinned).empty());
    // Board first; reassert every selected policy after a possibly coupled
    // write, including policies unchanged at original preflight.
    ScopedSnapshot coupled;coupled.target=state.target;coupled.entries={rail(0,0,1000),policy(3,120000),board(200000),policy(7,25000,"mW")};
    auto combined=capture_scoped_profile(coupled,{r0,p3,b});combined["entries"][0]["value"]=250000;
    auto combined_plan=plan_scoped_profile(combined,coupled);
    CHECK(combined_plan.steps[0].key==b&&combined_plan.steps[0].changed);
    CHECK(combined_plan.steps[1].key==p3&&!combined_plan.steps[1].changed&&combined_plan.steps[1].execute_required);
    CHECK(!combined_plan.steps[2].execute_required);
    auto after_board=coupled;after_board.entries[2].value=250000;after_board.entries[1].value=150000;
    CHECK(!scoped_profile_readback_error(combined_plan,after_board).empty());
    after_board.entries[1].value=120000;after_board.entries[3].value=30000;
    CHECK(scoped_profile_readback_error(combined_plan,after_board).empty()); // unselected peers are not replayed
    after_board.entries[0].fresh=false;CHECK(!scoped_profile_readback_error(combined_plan,after_board).empty());
    // Late preflight failure cannot return a partially executable plan or
    // mutate the document/current snapshot. Worker owns grouped Undo on writes.
    bad=combined;bad["entries"].back()["binding"]="late-drift";const auto original=bad;
    rejects([&]{plan_scoped_profile(bad,coupled);});CHECK(bad==original&&coupled.entries[2].value==200000);
    auto zero_board=combined;zero_board["entries"][0]["value"]=0;rejects([&]{plan_scoped_profile(zero_board,coupled);});
    auto empty=doc;empty["entries"]=ProfileJson::array();rejects([&]{plan_scoped_profile(empty,state);});
    std::printf("scoped_profile_test: %u checks passed\n",checks);
}
