// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "policy_workspace.h"
#include <algorithm>
#include <stdexcept>
namespace druta::ui {
namespace {
bool valid(const ScopedTarget& t,const ScopedCurrent& r){return !t.uuid.empty()&&!t.slot.empty()&&r.key.kind==ScopedKey::Kind::policy&&r.key.policy<32&&!r.binding.empty();}
bool matches(const ProfileJson& v,const ScopedTarget& t,const ScopedCurrent& r){
 return v.is_object()&&v.contains("uuid")&&v["uuid"].is_string()&&v["uuid"]==t.uuid&&v.contains("slot")&&v["slot"].is_string()&&v["slot"]==t.slot&&
 v.contains("policy")&&v["policy"].is_number_integer()&&v["policy"]==r.key.policy&&v.contains("binding")&&v["binding"].is_string()&&v["binding"]==r.binding;
}
}
PolicyAnnotations::PolicyAnnotations(ProfileJson doc):document_(std::move(doc)){if(!document_.is_object())throw std::runtime_error("Annotation document must be an object");}
std::string PolicyAnnotations::get(const ScopedTarget& t,const ScopedCurrent& r)const{
 if(!valid(t,r)||!document_.contains("annotations")||!document_["annotations"].is_array())return {};
 for(const auto& v:document_["annotations"])if(matches(v,t,r)&&v.contains("label")&&v["label"].is_string())return v["label"].get<std::string>();return {};
}
void PolicyAnnotations::set(const ScopedTarget& t,const ScopedCurrent& r,const std::string& label){
 if(!valid(t,r)||label.size()>256||label.find('\0')!=std::string::npos)throw std::runtime_error("Invalid annotation target, binding or label");
 if(!document_.contains("annotations"))document_["annotations"]=ProfileJson::array();
 if(!document_["annotations"].is_array())throw std::runtime_error("Unknown annotation schema; document preserved");
 auto& items=document_["annotations"];for(auto& v:items)if(matches(v,t,r)){v["label"]=label;return;}
 items.push_back({{"uuid",t.uuid},{"slot",t.slot},{"policy",r.key.policy},{"binding",r.binding},{"label",label}});
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
