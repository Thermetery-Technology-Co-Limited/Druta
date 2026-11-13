// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_catalog.h"
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
template<class F>static void rejects(F f){bool refused=false;try{f();}catch(const std::exception&){refused=true;}CHECK(refused);}
static ProfileJson target(){return {{"uuid","GPU-other"},{"slot","0000:06:00.0"}};}
static ProfileJson daily(){return {{"format","druta-native-daily"},{"version",1},{"target",target()},{"controls",{{"core",{{"value",100},{"unit","MHz"}}},{"memory",{{"value",400},{"unit","MHz"},{"scale",2}}},{"power",{{"value",200000},{"unit","mW"}}}}}};}
static ProfileJson scoped(){return {{"format","druta-native-scoped"},{"version",1},{"target",target()},{"entries",ProfileJson::array({{{"kind","rail"},{"rail",1},{"field",3},{"binding","qualified-alt-layout"},{"unit","uV"},{"value",15000}},{{"kind","policy"},{"policy",9},{"binding","policy:alternate"},{"unit","mA"},{"value",120000}}})}};}
int main(){
 auto d=daily(),s=scoped();CHECK(inspect_profile(d).valid&&inspect_profile(s).targets.size()==2);
 auto subset=select_profile_targets(d,{"clock:memory"});CHECK(subset["controls"].size()==1&&subset["controls"].contains("memory"));
 auto combined=compose_profiles({{"Daily",d},{"Rails",s}});CHECK(inspect_profile(combined).valid&&flatten_profile_collection(combined).size()==2);
 rejects([&]{compose_profiles({{"A",d},{"B",d}});});auto foreign=s;foreign["target"]["uuid"]="GPU-another";rejects([&]{compose_profiles({{"A",d},{"B",foreign}});});
 auto bad=s;bad["entries"].push_back(bad["entries"][0]);CHECK(!inspect_profile(bad).valid);bad=s;bad["entries"][0]["unit"]="mV";CHECK(!inspect_profile(bad).valid);
 auto advanced=d;advanced["version"]=2;advanced["advanced"]=ProfileJson::array({{{"kind","clock_domain"},{"domain",0},{"value",50000},{"unit","kHz"}}});CHECK(!inspect_profile(advanced).valid); // uniform core and domain 0 alias
 advanced["advanced"][0]["domain"]=2;CHECK(inspect_profile(advanced).valid); // additional memory and ordinary memory are separate channels
 auto curve=ProfileJson{{"format","druta-native-curve"},{"version",1},{"target",target()},{"geometry",{{"n_entries",3},{"freq_div",2},{"gpu_indices",ProfileJson::array({0,2})},{"other_indices",ProfileJson::array({1})}}},{"points",ProfileJson::array({{{"idx",0},{"volt_mv",850.5},{"delta_khz",30000}},{{"idx",2},{"volt_mv",975.25},{"delta_khz",45000}}})}};
 CHECK(inspect_profile(curve).valid);rejects([&]{compose_profiles({{"Daily",d},{"Curve",curve}});});CHECK(inspect_profile(compose_profiles({{"Memory",subset},{"Curve",curve}})).valid);
 auto timing=ProfileJson{{"format","druta-native-timing"},{"version",1},{"target",target()},{"codename","AD-other"},{"band",{{"memory_mhz",7001},{"pstate_before",0},{"pstate_after",2},{"memory_offset",0}}},{"fields",ProfileJson::array({{{"field","FAW"},{"value",22},{"register","CONFIG0"},{"offset",4096},{"lo",8},{"hi",15},{"maximum",255}},{{"field","RCD"},{"value",8},{"register","CONFIG0"},{"offset",4096},{"lo",0},{"hi",7},{"maximum",255}}})}};
 CHECK(inspect_profile(timing).valid);CHECK(select_profile_targets(timing,{"timing:FAW"})["fields"].size()==1);bad=timing;bad["fields"][1]["hi"]=8;bad["fields"][1]["maximum"]=511;CHECK(!inspect_profile(bad).valid);
 CHECK(!inspect_profile(ProfileJson{{"scope","unknown"},{"fields",{{"FAW",22}}},{"nvvdd_offset_mv",50}}).valid);
 auto legacy=ProfileJson{{"fields",{{"FAW",22}}}};CHECK(inspect_profile(legacy).valid&&inspect_profile(legacy).targets==std::vector<std::string>{"timing:*"});
 rejects([&]{compose_profiles({{"Legacy timing",legacy},{"Native timing",timing}});});
 CHECK(inspect_profile(compose_profiles({{"Legacy timing",legacy},{"Memory offset",subset}})).valid); // worker fresh preflight refuses mixed operating bands
 auto fan=ProfileJson{{"format","druta-native-fan"},{"version",1},{"target",target()},{"fan_control_state",{{"source","nvml"},{"fans",ProfileJson::array({{{"id",0},{"manual",true},{"policy",1},{"level",50}}})}}}};
 auto daily_fan=ProfileJson{{"format","druta-native-daily"},{"version",1},{"target",target()},{"controls",{{"fan",{{"mode","manual"},{"value",50},{"unit","%"}}}}}};
 CHECK(inspect_profile(fan).valid&&inspect_profile(fan).targets==std::vector<std::string>{"fan:policy"});rejects([&]{compose_profiles({{"Fan",fan},{"Daily fan",daily_fan}});});
 auto icx=ProfileJson{{"schema","druta.icx.preset.v1"},{"origin","explicit file"},{"settings",{{"nvvdd_mode","auto"},{"nvvdd_request_mv",nullptr},{"fbvdd_request_mv",1350},{"nvvdd_frequency_khz",500},{"fbvdd_frequency_khz",600},{"nvvdd_ocp_enabled",true},{"fbvdd_ocp_enabled",false},{"loadline_level",5}}}};
 CHECK(inspect_profile(icx).valid);auto all=compose_profiles({{"Daily",d},{"Rails",s},{"Timings",timing},{"iCX",icx}});CHECK(flatten_profile_collection(all).size()==4);
 auto selected=select_profile_targets(all,{"rail:1:3","timing:RCD"});CHECK(flatten_profile_collection(selected).size()==2&&inspect_profile(selected).targets.size()==2);
 rejects([&]{select_profile_targets(d,{"clock:memory","clock:memory"});});rejects([&]{select_profile_targets(d,{"unknown"});});
 CHECK(profile_targets_overlap("power:*","power:policy:9")&&!profile_targets_overlap("power:board","power:policy:9"));
 CHECK(!inspect_profile(ProfileJson{{"schema",2},{"scope","voltage_requests"}}).valid);bad=combined;bad["components"][0]["state"]=combined;CHECK(!inspect_profile(bad).valid);
 std::printf("Profile catalog: %u checks\n",checks);return 0;
}
