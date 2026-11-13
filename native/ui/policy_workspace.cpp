// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "policy_workspace.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>
namespace druta::ui {
namespace {
struct Rune {uint32_t value;size_t begin,end;};
std::vector<Rune> decode_label(const std::string& text){std::vector<Rune> out;for(size_t i=0;i<text.size();){const size_t begin=i;const auto first=static_cast<unsigned char>(text[i++]);uint32_t cp=0;unsigned count=0;
 if(first<0x80){cp=first;}else if(first>=0xc2&&first<=0xdf){cp=first&31u;count=1;}else if(first>=0xe0&&first<=0xef){cp=first&15u;count=2;}else if(first>=0xf0&&first<=0xf4){cp=first&7u;count=3;}else throw std::runtime_error("Invalid UTF-8 policy label");
 for(unsigned n=0;n<count;++n){if(i==text.size())throw std::runtime_error("Truncated UTF-8 policy label");const auto next=static_cast<unsigned char>(text[i++]);if((next&0xc0)!=0x80)throw std::runtime_error("Invalid UTF-8 policy label");cp=(cp<<6)|(next&63u);}
 if(!cp||(count==1&&cp<0x80)||(count==2&&cp<0x800)||(count==3&&cp<0x10000)||(cp>=0xd800&&cp<=0xdfff)||cp>0x10ffff)throw std::runtime_error("Invalid Unicode policy label");out.push_back({cp,begin,i});}return out;}
bool whitespace(uint32_t cp){return (cp>=9&&cp<=13)||(cp>=0x1c&&cp<=0x20)||cp==0x85||cp==0xa0||cp==0x1680||(cp>=0x2000&&cp<=0x200a)||cp==0x2028||cp==0x2029||cp==0x202f||cp==0x205f||cp==0x3000;}
bool valid(const ScopedTarget& t,const ScopedCurrent& r){return !t.uuid.empty()&&!t.slot.empty()&&r.key.kind==ScopedKey::Kind::policy&&r.key.policy<32&&(!r.binding.empty()||(r.channel&&r.policy_type));}
std::string label_key(const ScopedTarget& t,const ScopedCurrent& r){ProfileJson key=ProfileJson::array({t.uuid,t.slot,r.key.policy});if(r.channel&&r.policy_type){key.push_back(*r.channel);key.push_back(*r.policy_type);}else key.push_back(r.binding);return key.dump();}
bool matches(const ProfileJson& v,const ScopedTarget& t,const ScopedCurrent& r){
 if(!(v.is_object()&&v.contains("uuid")&&v["uuid"].is_string()&&v["uuid"]==t.uuid&&v.contains("slot")&&v["slot"].is_string()&&v["slot"]==t.slot&&v.contains("policy")&&v["policy"].is_number_integer()&&v["policy"]==r.key.policy))return false;
 if(v.contains("channel")&&v.contains("type"))return r.channel&&r.policy_type&&v["channel"].is_number_integer()&&v["type"].is_number_integer()&&v["channel"]==*r.channel&&v["type"]==*r.policy_type;
 return v.contains("binding")&&v["binding"].is_string()&&v["binding"]==r.binding &&
 (!v.contains("channel") || (r.channel && v["channel"].is_number_integer() && v["channel"]==*r.channel)) &&
 (!v.contains("type") || (r.policy_type && v["type"].is_number_integer() && v["type"]==*r.policy_type));
}
}
size_t policy_label_codepoints(const std::string& text){return decode_label(text).size();}
void validate_policy_label(const std::string& text){if(policy_label_codepoints(text)>80)throw std::runtime_error("Policy label exceeds 80 Unicode characters");}
std::string normalize_policy_label(const std::string& text,bool truncate){const auto runes=decode_label(text);size_t first=0,last=runes.size();while(first<last&&whitespace(runes[first].value))++first;while(last>first&&whitespace(runes[last-1].value))--last;if(last-first>80){if(!truncate)throw std::runtime_error("Policy label exceeds 80 Unicode characters");last=first+80;}return first==last?std::string{}:text.substr(runes[first].begin,runes[last-1].end-runes[first].begin);}
PolicyAnnotations::PolicyAnnotations(ProfileJson doc):document_(std::move(doc)){if(!document_.is_object())throw std::runtime_error("Annotation document must be an object");}
std::string PolicyAnnotations::get(const ScopedTarget& t,const ScopedCurrent& r)const{
 if(!valid(t,r)||!document_.contains("annotations")||!document_["annotations"].is_array())return {};
 for(const auto& v:document_["annotations"])if(matches(v,t,r)&&v.contains("label")&&v["label"].is_string())return v["label"].get<std::string>();return {};
}
void PolicyAnnotations::set(const ScopedTarget& t,const ScopedCurrent& r,const std::string& label){
 if(!valid(t,r))throw std::runtime_error("Invalid annotation target or binding");validate_policy_label(label);const auto& normalized=label;
 if(!document_.contains("annotations"))document_["annotations"]=ProfileJson::array();
 if(!document_["annotations"].is_array())throw std::runtime_error("Unknown annotation schema; document preserved");
 auto& items=document_["annotations"];for(auto& v:items)if(matches(v,t,r)){v["label"]=normalized;v["binding"]=r.binding;if(r.channel)v["channel"]=*r.channel;if(r.policy_type)v["type"]=*r.policy_type;revisions_[label_key(t,r)]=++next_revision_;return;}
 ProfileJson entry={{"uuid",t.uuid},{"slot",t.slot},{"policy",r.key.policy},{"binding",r.binding},{"label",normalized}};
 if(r.channel)entry["channel"]=*r.channel;if(r.policy_type)entry["type"]=*r.policy_type;items.push_back(std::move(entry));revisions_[label_key(t,r)]=++next_revision_;
}
uint64_t PolicyAnnotations::revision(const ScopedTarget& t,const ScopedCurrent& r)const{const auto found=revisions_.find(label_key(t,r));return found==revisions_.end()?0:found->second;}
void PolicyAnnotations::set_channel(const ScopedSnapshot& snapshot,const ScopedKey& selected,const std::string& label){
 const ScopedCurrent* source=nullptr;unsigned count=0;for(const auto& r:snapshot.entries)if(r.key==selected){source=&r;++count;}
 if(count!=1 || !source->fresh || !source->channel || !source->policy_type || !valid(snapshot.target,*source))throw std::runtime_error("Fresh uniquely bound policy channel unavailable");
 PolicyAnnotations updated(*this);
 for(const auto& row:snapshot.entries)if(row.key.kind==ScopedKey::Kind::policy && row.fresh && row.channel==source->channel && row.policy_type && valid(snapshot.target,row)){
  unsigned matches=0;for(const auto& candidate:snapshot.entries)if(candidate.key==row.key)++matches;
  if(matches==1)updated.set(snapshot.target,row,label);
 }
 *this=std::move(updated);
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
 PolicyAnnotations updated(annotations);size_t count=0;const auto& card=store[snapshot.target.uuid];
 for(const auto& row:snapshot.entries){if(!row.fresh || !row.channel || !row.policy_type || !valid(snapshot.target,row))continue;
  unsigned duplicates=0;for(const auto& candidate:snapshot.entries)if(candidate.key==row.key)++duplicates;if(duplicates!=1)continue;
  const auto key=std::to_string(row.key.policy);if(!card.contains(key))continue;const auto& entry=card[key];
  if(!entry.is_object() || !entry.contains("name") || !entry["name"].is_string() || !entry.contains("channel") || !entry["channel"].is_number_integer() || entry["channel"]!=*row.channel || !entry.contains("type") || !entry["type"].is_number_integer() || entry["type"]!=*row.policy_type)continue;
  auto name=normalize_policy_label(entry["name"].get<std::string>(),true);if(name.empty())continue;updated.set(snapshot.target,row,name);++count;
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
