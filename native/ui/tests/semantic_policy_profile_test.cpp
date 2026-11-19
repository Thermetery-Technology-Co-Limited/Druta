// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "semantic_policy_profile.h"
#include <cstdio>
#include <functional>
using namespace druta::ui;
int failures=0;void check(bool ok,const char* text){if(!ok){++failures;std::printf("FAIL: %s\n",text);}}
bool rejects(const std::function<void()>& f){try{f();return false;}catch(...){return true;}}
int main(){ProfileJson identity={{"slot","0000:09:00.0"},{"architecture",7},{"device_id",0x1234},{"subsystem_id",0x99887766}};
 ProfileJson policy={{"format","druta-native-voltage-policy"},{"version",1},{"uuid","GPU-alt"},{"identity",identity},{"selection",6},{"values",2}};
 auto d=describe_voltage_policy_profile(policy);check(d.slot=="0000:09:00.0"&&d.uuid=="GPU-alt"&&d.targets.size()==2,"native selected VF policies described");
 auto subset=select_voltage_policy_profile(policy,{"policy:vf-smoothing"});check(subset["selection"]==4&&subset["values"]==0&&subset["identity"]==identity,"native selected bit subset retains identity");
 check(rejects([&]{select_voltage_policy_profile(policy,{"policy:vf-smoothing","policy:vf-smoothing"});})&&rejects([&]{select_voltage_policy_profile(policy,{"policy:voltage-admission"});}),"duplicate and absent policy selection refused");
 ProfileJson py={{"schema",2},{"scope","clock_vf"},{"device",{{"uuid","GPU-alt"},{"slot","0000:09:00.0"}}},{"incomplete",ProfileJson::array()},{"clock_vf",{{"identity",identity},{"policies",{{"clock_vf.monotonicity",true},{"clock_vf.smoothening",false}}}}}};
 auto py_sub=select_voltage_policy_profile(py,{"policy:vf-smoothing"});check(py_sub["clock_vf"]["policies"].size()==1&&py_sub["clock_vf"]["policies"]["clock_vf.smoothening"]==false&&py_sub["device"]==py["device"],"Python VF subset retains all metadata");
 auto bad=py;bad["unrelated"]=1;check(rejects([&]{describe_voltage_policy_profile(bad);}),"unrelated policy fields refused");
 ProfileJson gate={{"schema",2},{"scope","voltage_controls"},{"device",{{"uuid","GPU-alt"}}},{"voltage_controls",{{"kind","gate"},{"identity",identity},{"rows",ProfileJson::array({{{"enabled",true}}})}}}};
 check(describe_voltage_policy_profile(gate).targets==std::vector<std::string>{"policy:voltage-admission"}&&select_voltage_policy_profile(gate,{"policy:voltage-admission"})==gate,"Python gate remains atomic");
 ProfileJson target=identity;target["uuid"]="GPU-alt";
 ProfileJson lower={{"policy",3},{"field",0},{"raw",1000},{"type",5},{"channel",8},{"unit",0},{"binding","stable-binding"}};
 auto upper=lower;upper["field"]=1;upper["raw"]=4000;
 ProfileJson integral={{"format","druta-native-power-integral"},{"version",1},{"target",target},{"fields",ProfileJson::array({lower,upper})}};
 auto di=describe_power_integral_profile(integral);check(di.targets.size()==2&&di.targets[0]=="integral:3:0"&&di.targets[1]=="integral:3:1","native lower and upper described separately");
 auto single=select_power_integral_profile(integral,{"integral:3:1"});check(single["fields"].size()==1&&single["fields"][0]==upper&&single["target"]==target,"native subset preserves selected binding and target");
 bad=integral;bad["fields"][1]["raw"]=0;check(rejects([&]{describe_power_integral_profile(bad);}),"zero upper refused");bad=integral;bad["fields"][1]["raw"]=999;check(rejects([&]{describe_power_integral_profile(bad);}),"inverted selected pair refused");
 bad=integral;bad["fields"][1]["raw"]=65536;check(rejects([&]{describe_power_integral_profile(bad);}),"out of range raw refused");bad=integral;bad["fields"][1]["binding"]="other";check(rejects([&]{describe_power_integral_profile(bad);}),"inconsistent policy metadata refused");
 bad=integral;bad["fields"].push_back(lower);check(rejects([&]{describe_power_integral_profile(bad);}),"duplicate selected field refused");
 ProfileJson python={{"schema",2},{"scope","power_integral"},{"saved_at","2026-11-13"},{"saved_ts",1.0},{"device",{{"name","Alt"},{"vbios",nullptr},{"driver",nullptr},{"uuid","GPU-alt"},{"slot","0000:09:00.0"}}},{"incomplete",ProfileJson::array()},{"power_integral",{{"identity",identity},{"policies",ProfileJson::array({{{"policy",3},{"type",5},{"channel",8},{"unit","mW"},{"values",{{"lower_raw",1000},{"upper_raw",4000}}}}})}}}};
 auto part=select_power_integral_profile(python,{"integral:3:0"});check(part["power_integral"]["policies"][0]["values"].size()==1&&part["device"]==python["device"]&&part["power_integral"]["identity"]==identity,"Python integral subset preserves metadata and exact value");
 bad=python;bad["device"]["slot"]="0000:08:00.0";check(rejects([&]{describe_power_integral_profile(bad);}),"conflicting Python slot refused");bad=python;bad["incomplete"]=ProfileJson::array({"missing"});check(rejects([&]{describe_power_integral_profile(bad);}),"incomplete Python capture refused");
 bad=python;bad["power_integral"]["policies"][0]["values"]["other"]=4;check(rejects([&]{describe_power_integral_profile(bad);}),"unknown field refused rather than dropped");
 check(rejects([&]{select_power_integral_profile(python,{});})&&rejects([&]{select_power_integral_profile(python,{"integral:3:0","integral:3:0"});}),"empty and duplicate selections refused");
 std::printf("Semantic policy profiles: %d failures\n",failures);return failures?1:0;}
