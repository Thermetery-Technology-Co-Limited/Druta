// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "semantic_policy_profile.h"
#include "voltage_policy_service.h"
#include <algorithm>
#include <cmath>
#include <map>
#include <set>
#include <stdexcept>
namespace druta::ui {namespace {
using J=ProfileJson;
[[noreturn]]void fail(const char* message){throw std::runtime_error(message);}
void keys(const J& j,std::initializer_list<const char*> names){if(!j.is_object()||j.size()!=names.size())fail("Invalid profile fields");for(auto n:names)if(!j.contains(n))fail("Missing profile field");}
uint32_t number(const J& v,uint32_t max){if(!v.is_number_integer()||v.is_boolean())fail("Exact unsigned integer required");if(v.is_number_unsigned()){auto n=v.get<uint64_t>();if(n>max)fail("Integer outside range");return uint32_t(n);}auto n=v.get<int64_t>();if(n<0||uint64_t(n)>max)fail("Integer outside range");return uint32_t(n);}
std::string text(const J& j,bool empty=false){if(!j.is_string())fail("String required");auto s=j.get<std::string>();if((!empty&&s.empty())||s.size()>4096||s.find('\0')!=std::string::npos)fail("Invalid text");return s;}
std::set<std::string> selection(const ProfileSelection& desc,const std::vector<std::string>& targets){if(targets.empty())fail("Empty selection");std::set<std::string> chosen;for(const auto& t:targets)if(std::find(desc.targets.begin(),desc.targets.end(),t)==desc.targets.end()||!chosen.insert(t).second)fail("Unknown or duplicate selected target");return chosen;}
std::string integral_name(uint32_t policy,uint32_t field){return "integral:"+std::to_string(policy)+":"+std::to_string(field);}
void target_meta(const J& t,bool native,ProfileSelection& out){if(native)keys(t,{"uuid","slot","architecture","device_id","subsystem_id"});else keys(t,{"slot","architecture","device_id","subsystem_id"});
 out.slot=text(t["slot"]);number(t["architecture"],UINT32_MAX);if(native){out.uuid=text(t["uuid"]);number(t["device_id"],UINT32_MAX);number(t["subsystem_id"],UINT32_MAX);}
 else {if(!t["device_id"].is_null())number(t["device_id"],65535);if(!t["subsystem_id"].is_null())number(t["subsystem_id"],UINT32_MAX);}}
void integral_row(const J& e,uint32_t& policy){policy=number(e["policy"],31);number(e["type"],255);number(e["channel"],255);
 if(e["unit"].is_string()){const auto unit=text(e["unit"]);if(unit!="mW"&&unit!="mA")fail("Unknown integral unit");}else number(e["unit"],1);}
}
ProfileSelection describe_voltage_policy_profile(const J& doc){const auto parsed=validate_voltage_policy_profile(doc);ProfileSelection out;out.slot=parsed.slot;out.uuid=parsed.uuid;
 if(parsed.request.selection&voltage_admission)out.targets.push_back("policy:voltage-admission");if(parsed.request.selection&voltage_monotonicity)out.targets.push_back("policy:vf-monotonicity");if(parsed.request.selection&voltage_smoothing)out.targets.push_back("policy:vf-smoothing");return out;}
J select_voltage_policy_profile(const J& doc,const std::vector<std::string>& targets){const auto desc=describe_voltage_policy_profile(doc);const auto chosen=selection(desc,targets);J out=doc;
 if(out.contains("format")){uint32_t mask=0;if(chosen.count("policy:voltage-admission"))mask|=voltage_admission;if(chosen.count("policy:vf-monotonicity"))mask|=voltage_monotonicity;if(chosen.count("policy:vf-smoothing"))mask|=voltage_smoothing;
  out["selection"]=mask;out["values"]=out["values"].get<uint32_t>()&mask;
 }else if(out["scope"]=="clock_vf"){auto& policies=out["clock_vf"]["policies"];if(!chosen.count("policy:vf-monotonicity"))policies.erase("clock_vf.monotonicity");if(!chosen.count("policy:vf-smoothing"))policies.erase("clock_vf.smoothening");}
 (void)describe_voltage_policy_profile(out);return out;}
ProfileSelection describe_power_integral_profile(const J& doc){ProfileSelection out;std::set<std::pair<uint32_t,uint32_t>> seen;std::map<uint32_t,std::pair<int,int>> bounds;std::map<uint32_t,std::string> native_bindings;
 auto add=[&](uint32_t policy,uint32_t field,uint32_t raw){if(!seen.insert({policy,field}).second)fail("Duplicate integral field");if(field==1&&!raw)fail("Upper bound must be positive");auto it=bounds.find(policy);if(it==bounds.end())it=bounds.emplace(policy,std::make_pair(-1,-1)).first;if(field==0)it->second.first=int(raw);else it->second.second=int(raw);out.targets.push_back(integral_name(policy,field));};
 if(doc.contains("format")){keys(doc,{"format","version","target","fields"});if(doc["format"]!="druta-native-power-integral"||number(doc["version"],1)!=1)fail("Unsupported integral format");target_meta(doc["target"],true,out);
  const auto& fields=doc["fields"];if(!fields.is_array()||fields.empty()||fields.size()>64)fail("Invalid integral selection");
  for(const auto& e:fields){keys(e,{"policy","field","raw","type","channel","unit","binding"});uint32_t policy;integral_row(e,policy);(void)text(e["binding"]);const auto signature=J::array({e["type"],e["channel"],e["unit"],e["binding"]}).dump();
   const auto prior=native_bindings.emplace(policy,signature);if(!prior.second&&prior.first->second!=signature)fail("Conflicting integral policy metadata");add(policy,number(e["field"],1),number(e["raw"],65535));}
 }else{keys(doc,{"schema","scope","saved_at","saved_ts","device","incomplete","power_integral"});if(number(doc["schema"],2)!=2||doc["scope"]!="power_integral")fail("Unsupported integral scope");(void)text(doc["saved_at"]);if(!doc["saved_ts"].is_number()||doc["saved_ts"].is_boolean()||!std::isfinite(doc["saved_ts"].get<double>())||!doc["incomplete"].is_array()||!doc["incomplete"].empty())fail("Incomplete profile metadata");
  const auto& device=doc["device"];keys(device,{"name","vbios","driver","uuid","slot"});for(const auto* name:{"name","vbios","driver","uuid","slot"})if(!device[name].is_null())(void)text(device[name],true);
  const auto& payload=doc["power_integral"];keys(payload,{"identity","policies"});target_meta(payload["identity"],false,out);if(device["slot"].is_null()||text(device["slot"])!=out.slot)fail("Conflicting adapter slot");if(!device["uuid"].is_null())out.uuid=text(device["uuid"],true);
  const auto& policies=payload["policies"];if(!policies.is_array()||policies.empty()||policies.size()>32)fail("Invalid integral policies");std::set<uint32_t> policies_seen;
  for(const auto& e:policies){keys(e,{"policy","type","channel","unit","values"});uint32_t policy;integral_row(e,policy);if(!e["unit"].is_string()||!policies_seen.insert(policy).second)fail("Duplicate policy or invalid unit");const auto& values=e["values"];if(!values.is_object()||values.empty()||values.size()>2)fail("Invalid integral values");for(auto it=values.begin();it!=values.end();++it){if(it.key()!="lower_raw"&&it.key()!="upper_raw")fail("Unknown integral field");add(policy,it.key()=="lower_raw"?0u:1u,number(it.value(),65535));}}
 }
 for(const auto& item:bounds)if(item.second.first>=0&&item.second.second>=0&&item.second.first>item.second.second)fail("Integral bounds inverted");return out;}
J select_power_integral_profile(const J& doc,const std::vector<std::string>& targets){const auto desc=describe_power_integral_profile(doc);const auto chosen=selection(desc,targets);J out=doc;
 if(out.contains("format")){J kept=J::array();for(const auto& e:out["fields"])if(chosen.count(integral_name(e["policy"].get<uint32_t>(),e["field"].get<uint32_t>())))kept.push_back(e);out["fields"]=std::move(kept);}
 else {J kept=J::array();for(auto row:out["power_integral"]["policies"]){const auto policy=row["policy"].get<uint32_t>();auto& values=row["values"];if(!chosen.count(integral_name(policy,0)))values.erase("lower_raw");if(!chosen.count(integral_name(policy,1)))values.erase("upper_raw");if(!values.empty())kept.push_back(std::move(row));}out["power_integral"]["policies"]=std::move(kept);}
 (void)describe_power_integral_profile(out);return out;}
}
