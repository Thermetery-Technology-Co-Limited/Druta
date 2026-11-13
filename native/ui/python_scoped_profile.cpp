// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "python_scoped_profile.h"
#include <algorithm>
#include <cmath>
#include <set>
#include <stdexcept>
namespace druta::ui {
namespace {
using J=ProfileJson;
[[noreturn]]void fail(const std::string& why){throw std::runtime_error("Python profile: "+why);}
void keys(const J& j,std::initializer_list<const char*> names){if(!j.is_object()||j.size()!=names.size())fail("missing or unexpected fields");for(auto n:names)if(!j.contains(n))fail("missing field "+std::string(n));}
std::string text(const J& j,bool empty=false){if(!j.is_string())fail("string required");auto s=j.get<std::string>();if((!empty&&s.empty())||s.size()>4096||s.find('\0')!=std::string::npos)fail("invalid string");return s;}
int64_t integer(const J& j,int64_t low,int64_t high){if(!j.is_number_integer()||(j.is_number_unsigned()&&j.get<uint64_t>()>uint64_t(INT64_MAX)))fail("exact integer required");auto n=j.get<int64_t>();if(n<low||n>high)fail("integer outside understood range");return n;}
uint32_t index(const std::string& s,uint32_t limit){if(s.empty()||s.size()>10||s.find_first_not_of("0123456789")!=std::string::npos||(s.size()>1&&s[0]=='0'))fail("noncanonical index");auto n=std::stoull(s);if(n>=limit)fail("index outside understood range");return uint32_t(n);}
ScopedTarget metadata(const J& d,const char* scope){
 keys(d,{"schema","scope","saved_at","saved_ts","device","incomplete",scope});
 if(integer(d["schema"],2,2)!=2||d["scope"]!=scope)fail("unsupported schema/scope");(void)text(d["saved_at"]);
 if(!d["saved_ts"].is_number()||!std::isfinite(d["saved_ts"].get<double>()))fail("invalid timestamp");
 const auto& device=d["device"];keys(device,{"name","vbios","driver","uuid","slot"});
 for(auto it=device.begin();it!=device.end();++it)if(!it.value().is_null())(void)text(it.value(),true);
 if(!d["incomplete"].is_array())fail("invalid completeness metadata");for(const auto& x:d["incomplete"])(void)text(x,true);
 if(!d["incomplete"].empty())fail("snapshot is incomplete");
 return {device["uuid"].is_null()?"":text(device["uuid"],true),text(device["slot"])};
}
void target_matches(const ScopedTarget& saved,const ScopedTarget& live){if(live.uuid.empty()||live.slot.empty()||saved.slot!=live.slot||(!saved.uuid.empty()&&saved.uuid!=live.uuid))fail("adapter UUID or slot differs");}
}
PythonRailProfile parse_python_rail_profile(const J& d){
 PythonRailProfile out;out.target=metadata(d,"rail_limits");const auto& rails=d["rail_limits"];
 if(!rails.is_object()||rails.empty())fail("empty rail selection");
 constexpr const char* fields[]={"reliability","alt_reliability","overvoltage","vmin"};
 for(auto it=rails.begin();it!=rails.end();++it){const auto rail=index(it.key(),2);if(!it.value().is_object()||it.value().empty())fail("empty rail fields");
  for(auto f=it.value().begin();f!=it.value().end();++f){uint32_t field=0;for(;field<4;++field)if(f.key()==fields[field])break;if(field==4)fail("unknown rail field");
   out.requests.push_back({ScopedKey::rail_field(rail,field),integer(f.value(),INT32_MIN,INT32_MAX)});
  }
 }return out;
}
PythonPowerProfile parse_python_power_profile(const J& d){
 PythonPowerProfile out;out.target=metadata(d,"power_action");const auto& data=d["power_action"];keys(data,{"board_mw","rows","pins"});
 out.board_mw=uint32_t(integer(data["board_mw"],1,UINT32_MAX-1));
 if(!data["pins"].is_object())fail("invalid power pins");for(auto it=data["pins"].begin();it!=data["pins"].end();++it)out.pins.emplace(index(it.key(),32),uint32_t(integer(it.value(),0,UINT32_MAX-1)));
 if(data["rows"].is_null()){if(!out.pins.empty())fail("board-only profile contains pins");return out;}
 if(!data["rows"].is_array()||data["rows"].empty()||data["rows"].size()>32)fail("invalid power table");out.rows.emplace();std::set<uint32_t> seen;
 for(const auto& j:data["rows"]){keys(j,{"policy","type","channel","unit","minimum","maximum","requested","board","named_current","writable","consistent"});PythonPowerRow r;
  r.policy=uint32_t(integer(j["policy"],0,31));r.type=uint32_t(integer(j["type"],0,255));r.channel=uint32_t(integer(j["channel"],0,255));
  r.minimum=uint32_t(integer(j["minimum"],0,UINT32_MAX));r.maximum=uint32_t(integer(j["maximum"],0,UINT32_MAX));r.requested=uint32_t(integer(j["requested"],0,UINT32_MAX));
  if(!j["unit"].is_null()){r.unit=text(j["unit"]);if(*r.unit!="mW"&&*r.unit!="mA")fail("unknown power units");}
  for(auto name:{"board","named_current","writable","consistent"})if(!j[name].is_boolean())fail("power flags must be Boolean");
  r.board=j["board"].get<bool>();r.named_current=j["named_current"].get<bool>();r.writable=j["writable"].get<bool>();r.consistent=j["consistent"].get<bool>();
  if(r.minimum>r.maximum||!seen.insert(r.policy).second)fail("duplicate policy or invalid bounds");out.rows->push_back(r);
 }
 for(const auto& p:out.pins)if(!seen.count(p.first))fail("pin is absent from saved table");
 std::sort(out.rows->begin(),out.rows->end(),[](const auto& a,const auto& b){return a.policy<b.policy;});return out;
}
ProfileJson import_python_rail_profile(const J& d,const ScopedSnapshot& live,const std::vector<ScopedKey>& selection,bool writing){
 const auto saved=parse_python_rail_profile(d);target_matches(saved.target,live.target);std::vector<ScopedKey> chosen=selection;
 if(chosen.empty())for(const auto& r:saved.requests)chosen.push_back(r.first);
 auto out=capture_scoped_profile(live,chosen);
 for(size_t i=0;i<chosen.size();++i){const auto& key=chosen[i];const auto found=std::find_if(saved.requests.begin(),saved.requests.end(),[&](const auto& r){return r.first==key;});
  if(found==saved.requests.end())fail("selected rail field is absent from saved profile");
  for(auto& entry:out["entries"])if(entry["kind"]=="rail"&&entry["rail"]==key.rail&&entry["field"]==key.field)entry["value"]=found->second;
 }
 (void)plan_scoped_profile(out,live,writing);return out;
}
void validate_python_power_live(const PythonPowerProfile& saved,const ScopedTarget& target,const std::optional<std::vector<PythonPowerRow>>& current,bool peer_preserving){
 target_matches(saved.target,target);
 if(saved.rows.has_value()!=current.has_value())fail("power table availability changed");
 if(!saved.rows){if(!peer_preserving)fail("board-only peer-preserving write route is not qualified");return;}
 if(saved.rows->size()!=current->size())fail("power policy identities changed");std::set<uint32_t> seen;
 for(const auto& r:*saved.rows){const PythonPowerRow* live=nullptr;for(const auto& c:*current)if(c.policy==r.policy){if(live)fail("duplicate live policy");live=&c;}
  if(!live||!seen.insert(r.policy).second||r.type!=live->type||r.channel!=live->channel||r.unit!=live->unit||r.minimum!=live->minimum||r.maximum!=live->maximum||r.board!=live->board||r.named_current!=live->named_current)fail("power policy identity, geometry or units changed");
  if(!r.board&&(r.writable||r.named_current)&&(r.requested<r.minimum||r.requested>r.maximum))fail("saved policy request is outside its restorable range");
  if(!r.board&&r.requested!=live->requested&&(!live->consistent||!(live->writable||live->named_current)||!live->unit))fail("changed saved policy has no qualified setter");
  const auto p=saved.pins.find(r.policy);if(p!=saved.pins.end()&&(r.board||!live->unit||!live->consistent||!(live->writable||live->named_current)||p->second<r.minimum||p->second>r.maximum))fail("saved pin is not restorable through selected policy");
 }
}
}
