// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "fan_profile.h"
#include "python_scoped_profile.h"
#include <algorithm>
#include <cstring>
#include <cmath>
#include <set>
#include <stdexcept>
namespace druta::ui { namespace {
using J=ProfileJson;
[[noreturn]]void fail(const char* why){throw std::runtime_error(std::string("Fan profile: ")+why);}
void keys(const J& j,std::initializer_list<const char*> required,std::initializer_list<const char*> optional={}){
 if(!j.is_object())fail("object required");for(auto key:required)if(!j.contains(key))fail("missing field");
 for(auto it=j.begin();it!=j.end();++it)if(std::find(required.begin(),required.end(),it.key())==required.end()&&std::find(optional.begin(),optional.end(),it.key())==optional.end())fail("unknown field");
}
uint32_t integer(const J& j,uint32_t hi=UINT32_MAX){if(!j.is_number_integer()||(j.is_number_unsigned()&&j.get<uint64_t>()>hi)||j.get<int64_t>()<0||uint64_t(j.get<int64_t>())>hi)fail("integer outside range");return j.get<uint32_t>();}
uint32_t level(const J& j){if(!j.is_number())fail("numeric fan level required");const auto n=j.get<double>();if(!std::isfinite(n)||n<0||n>100||std::trunc(n)!=n)fail("fan level must be a whole percentage");return uint32_t(n);}
std::string string(const J& j,bool empty=false){if(!j.is_string())fail("string required");auto s=j.get<std::string>();if((s.empty()&&!empty)||s.size()>256||s.find('\0')!=std::string::npos)fail("invalid text");return s;}
template<size_t N>std::string bounded(const char(&s)[N]){if(!std::memchr(s,0,N))fail("unterminated identity");return std::string(s);}
bool source(const std::string& s){return s=="nvml"||s=="nvapi_client"||s=="nvapi_cooler";}
void current(const dc_fan_state& s){if(s.size!=sizeof(s)||!source(bounded(s.source))||!s.count||s.count>32)fail("current fan layout unavailable");std::set<uint32_t> ids;
 for(uint32_t i=0;i<s.count;++i){const auto& r=s.fans[i];if(!ids.insert(r.id).second||r.level>100||r.min>r.max||r.max>100||(r.manual!=0&&r.manual!=1))fail("invalid current fan row");
  if(bounded(s.source)=="nvapi_cooler" ? (r.default_policy!=2&&r.default_policy!=4&&r.default_policy!=8&&r.default_policy!=16) : r.default_policy!=0)fail("invalid current default policy");
  if(r.manual?r.policy!=1:r.policy!=r.default_policy)fail("current policy has no qualified restoration");}}
}
FanProfile parse_fan_profile(const J& d){FanProfile p;const J* state=nullptr;
 if(d.is_object()&&d.value("format",J())=="druta-native-fan"){
  keys(d,{"format","version","target","fan_control_state"});if(integer(d["version"],1)!=1)fail("unsupported version");keys(d["target"],{"uuid","slot"});p.uuid=string(d["target"]["uuid"]);p.slot=string(d["target"]["slot"]);state=&d["fan_control_state"];
 }else{
  keys(d,{"schema","scope","saved_at","saved_ts","device","incomplete","fan_control_state"});if(d["scope"]!="fan")fail("unsupported scope");
  auto proxy=d;proxy.erase("fan_control_state");proxy["scope"]="rail_limits";proxy["rail_limits"]={{"0",{{"reliability",0}}}};
  auto target=parse_python_rail_profile(proxy).target;p.uuid=target.uuid;p.slot=target.slot;state=&d["fan_control_state"];
 }
 keys(*state,{"source","fans"});const auto src=string((*state)["source"]);if(!source(src))fail("unknown request source");
 const auto& rows=(*state)["fans"];if(!rows.is_array()||rows.empty()||rows.size()>32)fail("invalid fan count");auto& out=p.requests;out.size=sizeof(out);out.is_mapping=1;std::memcpy(out.source,src.c_str(),src.size()+1);out.count=uint32_t(rows.size());std::set<uint32_t> ids;
 for(uint32_t i=0;i<out.count;++i){const auto& j=rows[i];keys(j,{"id","level","manual","policy"},{"default_policy","min","max","rpm"});auto& r=out.fans[i];r.id=integer(j["id"]);if(!ids.insert(r.id).second||!j["manual"].is_boolean())fail("duplicate ID or non-Boolean policy");r.manual=j["manual"].get<bool>();r.level=int32_t(level(j["level"]));r.policy=integer(j["policy"]);r.has_id=r.has_manual=r.has_policy=r.level_valid=1;
  if(r.manual&&r.policy!=1)fail("manual request policy differs");if(src!="nvapi_cooler"&&r.policy!=uint32_t(r.manual))fail("inconsistent policy");if(src=="nvapi_cooler"&&!r.manual&&r.policy!=2&&r.policy!=4&&r.policy!=8&&r.policy!=16)fail("unknown automatic cooler policy");
  for(auto key:{"default_policy","min","max","rpm"})if(j.contains(key))integer(j[key],std::string(key)=="min"||std::string(key)=="max"?100:UINT32_MAX);
  if(j.contains("min")&&j.contains("max")&&integer(j["min"],100)>integer(j["max"],100))fail("contradictory saved bounds");
 }return p;
}
ProfileJson capture_fan_profile(const dc_gpu_identity& id,const dc_fan_state& fans){current(fans);const auto uuid=bounded(id.uuid),slot=bounded(id.slot);if(uuid.empty()||slot.empty()||!bounded(id.pairing_error).empty()||(!id.nvapi_ok&&!id.nvml_ok))fail("paired UUID and slot required to save");J rows=J::array();
 for(uint32_t i=0;i<fans.count;++i){const auto& f=fans.fans[i];rows.push_back({{"id",f.id},{"manual",f.manual!=0},{"level",f.level},{"policy",f.policy}});}
 J out={{"format","druta-native-fan"},{"version",1},{"target",{{"uuid",uuid},{"slot",slot}}},{"fan_control_state",{{"source",bounded(fans.source)},{"fans",rows}}}};(void)parse_fan_profile(out);return out;
}
dc_fan_snapshot stage_fan_profile(const FanProfile& p,const dc_gpu_identity& id,const dc_fan_state& live){current(live);if(bounded(id.slot)!=p.slot||(!p.uuid.empty()&&bounded(id.uuid)!=p.uuid)||!bounded(id.pairing_error).empty()||(!id.nvapi_ok&&!id.nvml_ok))fail("current adapter differs");
 if(p.requests.count!=live.count)fail("fan count changed");dc_fan_snapshot out=p.requests;std::memcpy(out.source,live.source,sizeof(out.source));const bool same=bounded(p.requests.source)==bounded(live.source);
 for(uint32_t i=0;i<live.count;++i){const auto& now=live.fans[i];const dc_fan_snapshot_row* saved=&p.requests.fans[i];if(same){saved=nullptr;for(uint32_t k=0;k<p.requests.count;++k)if(p.requests.fans[k].id==now.id)saved=&p.requests.fans[k];if(!saved)fail("fan IDs changed");}
  auto& r=out.fans[i];r=*saved;r.id=now.id;if(r.manual&&(r.level<int32_t(now.min)||r.level>int32_t(now.max)))fail("manual duty outside current bounds");if(!same)r.policy=r.manual?1:now.default_policy;
  if(!r.manual&&r.policy!=now.default_policy)fail("saved automatic policy is no longer the current default");
 }return out;
}
bool fan_profile_matches(const dc_fan_snapshot& expected,const dc_fan_state& actual){try{current(actual);if(expected.count!=actual.count||bounded(expected.source)!=bounded(actual.source))return false;for(uint32_t i=0;i<expected.count;++i){const auto& e=expected.fans[i];bool matched=false;for(uint32_t j=0;j<actual.count;++j){const auto& a=actual.fans[j];if(e.id==a.id&&e.manual==a.manual&&e.policy==a.policy&&(!e.manual||e.level==int32_t(a.level)))matched=true;}if(!matched)return false;}return true;}catch(...){return false;}}
}
