// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "fan_profile.h"
#include <cstdio>
#include <cstring>
#include <functional>
using namespace druta::ui;
int failures=0;void check(bool b,const char* why){if(!b){++failures;std::printf("FAIL: %s\n",why);}}
bool rejects(const std::function<void()>& fn){try{fn();return false;}catch(...){return true;}}
dc_gpu_identity identity(){dc_gpu_identity id{};id.size=sizeof(id);id.nvapi_ok=id.nvapi_selected=1;std::strcpy(id.slot,"0000:09:00.0");std::strcpy(id.uuid,"GPU-alt");return id;}
dc_fan_state current(){dc_fan_state s{};s.size=sizeof(s);std::strcpy(s.source,"nvapi_cooler");s.count=2;
 s.fans[0].id=7;s.fans[0].level=45;s.fans[0].manual=1;s.fans[0].policy=1;s.fans[0].default_policy=2;s.fans[0].min=20;s.fans[0].max=100;
 s.fans[1].id=13;s.fans[1].level=31;s.fans[1].manual=0;s.fans[1].policy=2;s.fans[1].default_policy=2;s.fans[1].min=20;s.fans[1].max=100;return s;}
int main(){auto id=identity();auto live=current();const auto doc=capture_fan_profile(id,live);const auto saved=parse_fan_profile(doc);
 auto staged=stage_fan_profile(saved,id,live);check(staged.count==2&&staged.fans[0].id==7&&staged.fans[0].manual&&staged.fans[0].level==45&&staged.fans[1].id==13&&!staged.fans[1].manual&&staged.fans[1].policy==2,"mixed manual/automatic requests round trip");
 auto reordered=live;std::swap(reordered.fans[0],reordered.fans[1]);staged=stage_fan_profile(saved,id,reordered);
 check(staged.fans[0].id==13&&!staged.fans[0].manual&&staged.fans[1].id==7&&staged.fans[1].level==45,"same source matches reordered IDs");
 auto changed=live;changed.fans[1].id=12;check(rejects([&]{stage_fan_profile(saved,id,changed);}),"same-source alternate IDs refused");
 changed=live;changed.count=1;check(rejects([&]{stage_fan_profile(saved,id,changed);}),"fan count change refused");
 auto converted=live;std::strcpy(converted.source,"nvml");converted.fans[0].id=0;converted.fans[1].id=1;converted.fans[0].default_policy=0;converted.fans[1].policy=converted.fans[1].default_policy=0;
 staged=stage_fan_profile(saved,id,converted);check(staged.fans[0].id==0&&staged.fans[0].manual&&staged.fans[0].level==45&&staged.fans[1].id==1&&!staged.fans[1].manual&&staged.fans[1].policy==0,"cross-source conversion follows positional mapping and current default");
 changed=live;changed.fans[0].min=46;check(rejects([&]{stage_fan_profile(saved,id,changed);}),"manual level below current bounds refused");
 changed=live;changed.fans[1].default_policy=4;changed.fans[1].policy=4;check(rejects([&]{stage_fan_profile(saved,id,changed);}),"saved automatic policy cannot override changed default");
 changed=converted;changed.fans[1].manual=1;changed.fans[1].policy=1;changed.fans[1].default_policy=1;check(rejects([&]{stage_fan_profile(saved,id,changed);}),"invalid current automatic default cannot become auto request");
 changed=live;changed.fans[1].level=80;check(fan_profile_matches(stage_fan_profile(saved,id,live),changed),"automatic duty fluctuation ignored");changed.fans[0].level=46;check(!fan_profile_matches(stage_fan_profile(saved,id,live),changed),"manual request difference detected");
 auto other=id;std::strcpy(other.uuid,"GPU-other");check(rejects([&]{stage_fan_profile(saved,other,live);}),"changed UUID refused");other=id;std::strcpy(other.slot,"0000:08:00.0");check(rejects([&]{stage_fan_profile(saved,other,live);}),"changed slot refused");
 other=id;other.uuid[0]=0;check(rejects([&]{capture_fan_profile(other,live);}),"native save requires UUID");
 ProfileJson python={{"schema",2},{"scope","fan"},{"saved_at","2026-11-13"},{"saved_ts",1.0},{"device",{{"name","GPU"},{"vbios",nullptr},{"driver",nullptr},{"uuid",nullptr},{"slot","0000:09:00.0"}}},{"incomplete",ProfileJson::array()},{"fan_control_state",doc["fan_control_state"]}};
 const auto legacy=parse_fan_profile(python);check(legacy.uuid.empty()&&stage_fan_profile(legacy,other,live).count==2,"legacy absent UUID stages against same paired slot");
 auto bad=doc;bad["fan_control_state"]["fans"][0]["min"]=60;bad["fan_control_state"]["fans"][0]["max"]=90;
 check(stage_fan_profile(parse_fan_profile(bad),id,live).fans[0].level==45,"historical saved range does not override current bounds");
 auto no_uuid=id;no_uuid.uuid[0]=0;check(rejects([&]{stage_fan_profile(saved,no_uuid,live);}),"saved UUID cannot match missing current UUID");
 bad=doc;bad["fan_control_state"]["source"]="unknown";check(rejects([&]{parse_fan_profile(bad);}),"unknown source refused");
 bad=doc;bad["fan_control_state"]["fans"][0]["manual"]=1;check(rejects([&]{parse_fan_profile(bad);}),"manual must be Boolean");
 bad=doc;bad["fan_control_state"]["fans"][0]["level"]=45.0;check(parse_fan_profile(bad).requests.fans[0].level==45,"integer-valued numeric fan request preserved");
 bad=doc;bad["fan_control_state"]["fans"][0]["level"]=45.5;check(rejects([&]{parse_fan_profile(bad);}),"fractional duty refused");
 bad=doc;bad["fan_control_state"]["fans"][0]["level"]=101;check(rejects([&]{parse_fan_profile(bad);}),"duty above 100 refused");
 bad=doc;bad["fan_control_state"]["fans"][1]["id"]=7;check(rejects([&]{parse_fan_profile(bad);}),"duplicate IDs refused");
 bad=doc;bad["fan_control_state"]["fans"][1]["policy"]=1;check(rejects([&]{parse_fan_profile(bad);}),"automatic policy conflict refused");
 bad=doc;bad["fan_control_state"]["fans"][0]["min"]=70;bad["fan_control_state"]["fans"][0]["max"]=30;check(rejects([&]{parse_fan_profile(bad);}),"contradictory saved bounds refused");
 bad=python;bad["incomplete"]=ProfileJson::array({"fan unreadable"});check(rejects([&]{parse_fan_profile(bad);}),"incomplete Python snapshot refused");
 bad=python;bad["device"]["unexpected"]=1;check(rejects([&]{parse_fan_profile(bad);}),"unexpected metadata refused");
 bad=python;bad["saved_ts"]=true;check(rejects([&]{parse_fan_profile(bad);}),"malformed timestamp refused");
 std::printf("Fan profile: %d failures\n",failures);return failures?1:0;}
