// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "policy_workspace.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>
namespace druta::ui {
namespace {
bool valid(const ScopedTarget& t,const ScopedCurrent& r){return !t.uuid.empty()&&!t.slot.empty()&&r.key.kind==ScopedKey::Kind::policy&&r.key.policy<32&&!r.binding.empty();}
bool matches(const ProfileJson& v,const ScopedTarget& t,const ScopedCurrent& r){
 return v.is_object()&&v.contains("uuid")&&v["uuid"].is_string()&&v["uuid"]==t.uuid&&v.contains("slot")&&v["slot"].is_string()&&v["slot"]==t.slot&&
 v.contains("policy")&&v["policy"].is_number_integer()&&v["policy"]==r.key.policy&&v.contains("binding")&&v["binding"].is_string()&&v["binding"]==r.binding &&
 (!v.contains("channel") || (r.channel && v["channel"].is_number_integer() && v["channel"]==*r.channel)) &&
 (!v.contains("type") || (r.policy_type && v["type"].is_number_integer() && v["type"]==*r.policy_type));
}
}
PolicyAnnotations::PolicyAnnotations(ProfileJson doc):document_(std::move(doc)){if(!document_.is_object())throw std::runtime_error("Annotation document must be an object");}
std::string PolicyAnnotations::get(const ScopedTarget& t,const ScopedCurrent& r)const{
 if(!valid(t,r)||!document_.contains("annotations")||!document_["annotations"].is_array())return {};
 for(const auto& v:document_["annotations"])if(matches(v,t,r)&&v.contains("label")&&v["label"].is_string())return v["label"].get<std::string>();return {};
}
void PolicyAnnotations::set(const ScopedTarget& t,const ScopedCurrent& r,const std::string& label){
 if(!valid(t,r)||label.size()>80||label.find('\0')!=std::string::npos)throw std::runtime_error("Invalid annotation target, binding or label");
 if(!document_.contains("annotations"))document_["annotations"]=ProfileJson::array();
 if(!document_["annotations"].is_array())throw std::runtime_error("Unknown annotation schema; document preserved");
 auto& items=document_["annotations"];for(auto& v:items)if(matches(v,t,r)){v["label"]=label;if(r.channel)v["channel"]=*r.channel;if(r.policy_type)v["type"]=*r.policy_type;return;}
 ProfileJson entry={{"uuid",t.uuid},{"slot",t.slot},{"policy",r.key.policy},{"binding",r.binding},{"label",label}};
 if(r.channel)entry["channel"]=*r.channel;if(r.policy_type)entry["type"]=*r.policy_type;items.push_back(std::move(entry));
}
void PolicyAnnotations::set_channel(const ScopedSnapshot& snapshot,const ScopedKey& selected,const std::string& label){
 const ScopedCurrent* source=nullptr;unsigned count=0;for(const auto& r:snapshot.entries)if(r.key==selected){source=&r;++count;}
 if(count!=1 || !source->fresh || !source->channel || !source->policy_type || !valid(snapshot.target,*source))throw std::runtime_error("Fresh uniquely bound policy channel unavailable");
 PolicyAnnotations updated(document_);
 for(const auto& row:snapshot.entries)if(row.key.kind==ScopedKey::Kind::policy && row.fresh && row.channel==source->channel && row.policy_type && valid(snapshot.target,row)){
  unsigned matches=0;for(const auto& candidate:snapshot.entries)if(candidate.key==row.key)++matches;
  if(matches==1)updated.set(snapshot.target,row,label);
 }
 document_=std::move(updated.document_);
}
std::optional<PolicyCurrentDisplay> policy_current_display(const std::string& note,const std::string& unit,std::optional<double> amps){
 if(unit!="mA" || !amps || !std::isfinite(*amps) || *amps<0)return {};
 unsigned divisor=0;if(note=="PCIE 8pin")divisor=3;else if(note=="EPS 8pin")divisor=4;else if(note=="12VHPWR/12V-2x6")divisor=6;else if(note=="PCIE")divisor=1;else return {};
 PolicyCurrentDisplay out;out.divisor=divisor;out.even_split=divisor>1;out.amps=*amps/divisor;
 const bool slot=note=="PCIE";const double white=slot?5.5:5, yellow=slot?6.25:10,red=slot?7:13;
 out.band=out.amps<=white?PolicyCurrentBand::white:out.amps<=yellow?PolicyCurrentBand::yellow:out.amps<=red?PolicyCurrentBand::red:PolicyCurrentBand::crimson;return out;
}
size_t import_python_policy_names(const ProfileJson& store,const ScopedSnapshot& snapshot,PolicyAnnotations& annotations){
 if(snapshot.target.uuid.empty() || !store.is_object() || !store.contains(snapshot.target.uuid) || !store[snapshot.target.uuid].is_object())return 0;
 PolicyAnnotations updated(annotations.document());size_t count=0;const auto& card=store[snapshot.target.uuid];
 for(const auto& row:snapshot.entries){if(!row.fresh || !row.channel || !row.policy_type || !valid(snapshot.target,row))continue;
  unsigned duplicates=0;for(const auto& candidate:snapshot.entries)if(candidate.key==row.key)++duplicates;if(duplicates!=1)continue;
  const auto key=std::to_string(row.key.policy);if(!card.contains(key))continue;const auto& entry=card[key];
  if(!entry.is_object() || !entry.contains("name") || !entry["name"].is_string() || !entry.contains("channel") || !entry["channel"].is_number_integer() || entry["channel"]!=*row.channel || !entry.contains("type") || !entry["type"].is_number_integer() || entry["type"]!=*row.policy_type)continue;
  auto name=entry["name"].get<std::string>();const auto first=name.find_first_not_of(" \t\r\n"),last=name.find_last_not_of(" \t\r\n");if(first==std::string::npos)continue;name=name.substr(first,last-first+1);if(name.size()>80)name.resize(80);updated.set(snapshot.target,row,name);++count;
 }
 annotations=std::move(updated);return count;
}
PolicyMaxPreview preview_policy_max(const ScopedSnapshot& snapshot,const std::vector<ScopedKey>& selected){
 PolicyMaxPreview out;out.target=snapshot.target;std::vector<ScopedKey> accepted;
 for(const auto& key:selected){PolicyMaxRow row;row.key=key;
  if(key.kind!=ScopedKey::Kind::policy){row.reason="Only explicitly selected policy requests are supported";out.rows.push_back(row);continue;}
  if(std::find(accepted.begin(),accepted.end(),key)!=accepted.end()||std::count(selected.begin(),selected.end(),key)>1){row.reason="Duplicate selected policy";out.rows.push_back(row);continue;}
  const ScopedCurrent* current=nullptr;unsigned count=0;for(const auto& item:snapshot.entries)if(item.key==key){current=&item;++count;}
  if(count!=1){row.reason=count?"Duplicate current policy identity":"Current policy unavailable";out.rows.push_back(row);continue;}
  row.binding=current->binding;row.unit=current->unit;row.before=current->value;row.maximum=current->high;
  if(!current->fresh)row.reason="Fresh request and bounds unavailable";
  else if(!current->writable)row.reason="Write gate or capability unavailable";
  else try{auto doc=capture_scoped_profile(snapshot,{key});doc["entries"][0]["value"]=current->high;plan_scoped_profile(doc,snapshot);row.included=true;accepted.push_back(key);}
  catch(const std::exception& e){row.reason=e.what();}
  out.rows.push_back(std::move(row));
 }
 if(accepted.empty()){out.message="No selected policies have qualified writable limits";return out;}
 try{auto doc=capture_scoped_profile(snapshot,accepted);for(auto& entry:doc["entries"]){const auto id=entry["policy"].get<uint32_t>();
   for(const auto& row:out.rows)if(row.included&&row.key.policy==id){entry["value"]=row.maximum;break;}}
  out.plan=plan_scoped_profile(doc,snapshot);out.document=std::move(doc);out.message="Review selected maximum requests and skipped policies; Apply is explicit.";
 }catch(const std::exception& e){out.message=e.what();out.plan.reset();out.document.reset();}
 return out;
}
}
