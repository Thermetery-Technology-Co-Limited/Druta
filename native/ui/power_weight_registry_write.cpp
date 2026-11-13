// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weight_registry_write.h"
#include <windows.h>
#include <algorithm>
#include <map>
#include <stdexcept>
namespace druta::ui {
namespace {
std::wstring lower(std::wstring s){for(auto& c:s)if(c>=L'A'&&c<=L'Z')c=static_cast<wchar_t>(c+L'a'-L'A');return s;}
bool record(const std::wstring& s){const auto k=lower(s);if(k.size()!=13||k.substr(0,11)!=L"romoverride")return false;for(size_t i=11;i<13;++i)if(!((k[i]>=L'0'&&k[i]<=L'9')||(k[i]>=L'a'&&k[i]<=L'f')))return false;return true;}
using Map=std::map<std::wstring,PowerWeightRegistryValue>;
Map mapped(const PowerWeightRegistrySnapshot& input){Map out;for(const auto& v:input)if(!out.emplace(lower(v.name),v).second)throw std::runtime_error("Duplicate case-insensitive registry name");return out;}
bool equal(const PowerWeightRegistryValue* a,const PowerWeightRegistryValue* b){return (!a&&!b)||(a&&b&&a->type==b->type&&a->bytes==b->bytes);}
bool same(const Map& a,const Map& b){if(a.size()!=b.size())return false;for(const auto& item:a){auto it=b.find(item.first);if(it==b.end()||!equal(&item.second,&it->second))return false;}return true;}
bool native_write(const std::wstring& key,const std::wstring& name,const std::optional<PowerWeightRegistryValue>& value,std::string& error){HKEY h=nullptr;const auto opened=RegOpenKeyExW(HKEY_LOCAL_MACHINE,key.c_str(),0,KEY_SET_VALUE|KEY_WOW64_64KEY,&h);if(opened!=ERROR_SUCCESS){error="Cannot open display driver registry key for selected write";return false;}const auto status=value?RegSetValueExW(h,name.c_str(),0,value->type,value->bytes.empty()?nullptr:value->bytes.data(),static_cast<DWORD>(value->bytes.size())):RegDeleteValueW(h,name.c_str());RegCloseKey(h);if(status!=ERROR_SUCCESS){error="Selected registry value write/delete failed";return false;}return true;}
}
PowerWeightRegistryWriteResult write_power_weight_registry(const VoltageScaffoldWindowsTarget& target,const PowerWeightRegistrySnapshot& expected,const PowerWeightRegistrySnapshot& desired,const std::function<bool(const std::wstring&,std::string&)>& journal,const PowerWeightRegistryWriteApi& api){PowerWeightRegistryWriteResult result;
 try{if(!journal)throw std::runtime_error("Registry journal callback unavailable");const auto validated=[&](const PowerWeightRegistrySnapshot& s){return mapped(read_power_weight_registry(target,[&](const std::wstring&){return s;}));};auto before=validated(expected),after=validated(desired);std::map<std::wstring,bool> names;for(const auto& item:before)names[item.first]=true;for(const auto& item:after)names[item.first]=true;
  for(const auto& item:names){const auto b=before.find(item.first),a=after.find(item.first);const auto* bv=b==before.end()?nullptr:&b->second;const auto* av=a==after.end()?nullptr:&a->second;if(equal(bv,av))continue;if(!record(item.first))throw std::runtime_error("Only ROM override names may be changed");if(av&&(av->type!=3||av->bytes.size()<10||av->bytes.size()>256||av->bytes.size()!=size_t(10)+av->bytes[9]))throw std::runtime_error("Desired ROM override record malformed");}
  bool adding=false;for(const auto& item:after)if(record(item.first)&&before.find(item.first)==before.end())adding=true;
  if(adding){for(const auto* state:{&before,&after}){unsigned count=0;for(const auto& item:*state)if(record(item.first))++count;const wchar_t* digits=L"0123456789abcdef";for(unsigned i=0;i<count;++i){std::wstring name=L"romoverride";name+=digits[i>>4];name+=digits[i&15];if(!state->count(name))throw std::runtime_error("ROM override append has a gap");}}}
  auto current=mapped(read_power_weight_registry(target,api.read));if(!same(current,before))throw std::runtime_error("Registry values changed since preview");const std::wstring key(target.driver_key.begin(),target.driver_key.end());
  for(const auto& item:names){const auto b=before.find(item.first),a=after.find(item.first);const auto* bv=b==before.end()?nullptr:&b->second;const auto* av=a==after.end()?nullptr:&a->second;if(equal(bv,av))continue;const auto name=av?av->name:bv->name;std::string error;if(!journal(name,error))throw std::runtime_error(error.empty()?"Registry journal refused selected value":error);result.may_have_changed=true;const std::optional<PowerWeightRegistryValue> value=av?std::optional<PowerWeightRegistryValue>(*av):std::nullopt;if(!(api.write?api.write(key,name,value,error):native_write(key,name,value,error)))throw std::runtime_error(error.empty()?"Selected registry write failed":error);
   if(av)current[item.first]=*av;else current.erase(item.first);const auto observed=mapped(read_power_weight_registry(target,api.read));if(!same(current,observed))throw std::runtime_error("Registry readback differs after selected write");
  }result.ok=true;result.message="Selected registry delta verified";
 }catch(const std::exception& e){result.message=e.what();}return result;
}
}
