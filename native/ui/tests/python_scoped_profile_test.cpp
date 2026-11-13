// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "python_scoped_profile.h"
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
template<class F>static void rejects(F f){bool refused=false;try{f();}catch(const std::exception&){refused=true;}CHECK(refused);}
static ProfileJson envelope(const char* scope,ProfileJson values){return {{"schema",2},{"scope",scope},{"saved_at","2026-11-13 12:00:00"},{"saved_ts",123.5},{"device",{{"name","Alternate GPU"},{"vbios","informational"},{"driver","any driver"},{"uuid","GPU-other"},{"slot","0000:06:00.0"}}},{"incomplete",ProfileJson::array()},{scope,std::move(values)}};}
int main(){
 auto rails=envelope("rail_limits",{{"1",{{"reliability",-12000},{"vmin",5000}}}});auto parsed=parse_python_rail_profile(rails);CHECK(parsed.requests.size()==2&&parsed.requests[0].first.rail==1);
 ScopedSnapshot live;live.target={"GPU-other","0000:06:00.0"};ScopedCurrent r;r.key=ScopedKey::rail_field(1,0);r.binding="rail:1:0:type:alternate:base:843.25";r.unit="uV";r.value=0;r.low=-100000;r.high=100000;r.fresh=true;r.writable=false;live.entries={r};
 auto native=import_python_rail_profile(rails,live,{r.key});CHECK(native["entries"].size()==1&&native["entries"][0]["value"]==-12000&&native["entries"][0]["binding"]==r.binding);
 rejects([&]{import_python_rail_profile(rails,live,{r.key},true);}); // preview and capture allowed with gate off
 rejects([&]{import_python_rail_profile(rails,live);}); // selected missing VMIN, not unrelated rail 0
 live.entries[0].writable=true;CHECK(plan_scoped_profile(native,live).steps.size()==1);
 auto changed=live;changed.entries[0].binding+="changed";rejects([&]{plan_scoped_profile(native,changed);});
 auto bad=rails;bad["rail_limits"]["1"]["reliability"]=1.5;rejects([&]{parse_python_rail_profile(bad);});bad=rails;bad["rail_limits"]["1"]["reliability"]=true;rejects([&]{parse_python_rail_profile(bad);});
 bad=rails;bad["rail_limits"]["01"]=bad["rail_limits"]["1"];rejects([&]{parse_python_rail_profile(bad);});bad=rails;bad["device"]["uuid"]="GPU-foreign";rejects([&]{import_python_rail_profile(bad,live,{r.key});});
 bad=rails;bad["device"]["driver"]="unlisted driver";CHECK(import_python_rail_profile(bad,live,{r.key})["entries"][0]["value"]==-12000);
 ProfileJson row={{"policy",7},{"type",13},{"channel",22},{"unit","mA"},{"minimum",0},{"maximum",200000},{"requested",130000},{"board",false},{"named_current",true},{"writable",false},{"consistent",true}};
 auto power=envelope("power_action",{{"board_mw",250000},{"rows",ProfileJson::array({row})},{"pins",{{"7",130000}}}});auto p=parse_python_power_profile(power);CHECK(p.rows&&p.rows->size()==1&&p.pins.at(7)==130000&&p.rows->front().unit=="mA");
 validate_python_power_live(p,live.target,p.rows,false);auto current=p.rows;current->front().requested=125000;validate_python_power_live(p,live.target,current,false);
 current->front().channel++;rejects([&]{validate_python_power_live(p,live.target,current,false);});current=p.rows;current->front().unit="mW";rejects([&]{validate_python_power_live(p,live.target,current,false);});
 bad=power;bad["power_action"]["rows"].push_back(row);rejects([&]{parse_python_power_profile(bad);});bad=power;bad["power_action"]["pins"]["07"]=100;rejects([&]{parse_python_power_profile(bad);});
 auto board=envelope("power_action",{{"board_mw",110000},{"rows",nullptr},{"pins",ProfileJson::object()}});auto b=parse_python_power_profile(board);rejects([&]{validate_python_power_live(b,live.target,std::nullopt,false);});validate_python_power_live(b,live.target,std::nullopt,true);
 rejects([&]{validate_python_power_live(p,live.target,std::nullopt,true);});
 std::printf("Python scoped profiles: %u checks\n",checks);return 0;
}
