// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "saving_profile.h"
#include "power_saving_service.h"
#include "clock_gating_service.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <set>
#include <stdexcept>
namespace druta::ui {
namespace {
void need(bool value,const char* message){if(!value)throw std::runtime_error(message);}
void keys(const ProfileJson& doc,std::initializer_list<const char*> names){need(doc.is_object()&&doc.size()==names.size(),"Invalid saving-profile fields");for(auto name:names)need(doc.contains(name),"Missing saving-profile field");}
uint32_t integer(const ProfileJson& value,uint32_t maximum=UINT32_MAX){need(value.is_number_integer(),"Saving profile requires exact integers");if(value.is_number_unsigned()){const auto n=value.get<uint64_t>();need(n<=maximum,"Saving profile integer out of range");return static_cast<uint32_t>(n);}const auto n=value.get<int64_t>();need(n>=0&&uint64_t(n)<=maximum,"Saving profile integer out of range");return static_cast<uint32_t>(n);}
std::string string(const ProfileJson& value,size_t maximum=256){need(value.is_string(),"Saving profile requires a string");auto s=value.get<std::string>();need(!s.empty()&&s.size()<=maximum&&s.find('\0')==std::string::npos,"Invalid saving-profile string");return s;}
template<size_t N>std::string text(const char(&value)[N]){const auto end=std::find(value,value+N,'\0');need(end!=value+N,"Unterminated live saving identity");return {value,end};}
}
std::string saving_profile_target(const PythonSavingRequest& r){return r.clock?"clock-gating:"+std::to_string(r.family):"saving:"+std::to_string(r.family)+":"+std::to_string(r.index);}
PythonSavingRequest parse_python_saving_profile(const ProfileJson& doc){
    need(doc.is_object()&&doc.contains("scope"),"Saving profile scope required");const auto scope=string(doc.at("scope"));need(scope=="lowpower_requests"||scope=="pcie_speed","Unsupported saving profile scope");
    const std::set<std::string> allowed={"schema","scope","saved_at","saved_ts","device","incomplete",scope};for(auto i=doc.begin();i!=doc.end();++i)need(allowed.count(i.key())!=0,"Saving profile contains unrelated action fields");
    need(integer(doc.at("schema"),2)==2,"Unsupported saving profile schema");need(doc.at("device").is_object(),"Saving profile device metadata required");
    if(scope=="pcie_speed")keys(doc,{"schema","scope","saved_at","saved_ts","device","incomplete","pcie_speed"});
    if(doc.contains("saved_at"))need(doc["saved_at"].is_string()&&!doc["saved_at"].get<std::string>().empty(),"Invalid saving timestamp");if(doc.contains("saved_ts"))need(doc["saved_ts"].is_number()&&std::isfinite(doc["saved_ts"].get<double>()),"Invalid saving timestamp");
    if(doc.contains("incomplete"))need(doc["incomplete"].is_array()&&doc["incomplete"].empty(),"Incomplete saving profile cannot be applied");
    PythonSavingRequest out;const auto& device=doc["device"];if(scope=="pcie_speed")keys(device,{"name","vbios","driver","uuid","slot"});
    for(const auto name:{"name","vbios","driver","uuid","slot"})if(device.contains(name))need(device[name].is_null()||device[name].is_string(),"Invalid saved device metadata");if(device.contains("uuid")&&!device["uuid"].is_null()&&device["uuid"]!="")out.uuid=string(device["uuid"]);
    const auto& payload=doc.at(scope);const auto& identity=payload.at("identity");keys(identity,{"slot","architecture","device_id","subsystem_id"});out.slot=string(identity["slot"]);out.architecture=integer(identity["architecture"]);
    if(!identity["device_id"].is_null())out.device_id=integer(identity["device_id"],scope=="pcie_speed"?65535:UINT32_MAX);if(!identity["subsystem_id"].is_null())out.subsystem_id=integer(identity["subsystem_id"]);
    if(scope=="lowpower_requests")need(out.device_id.has_value()&&out.subsystem_id.has_value(),"Low-power identity requires complete PCI values");
    if(device.contains("slot")&&!device["slot"].is_null())need(string(device["slot"])==out.slot,"Conflicting saved adapter slots");if(scope=="pcie_speed")need(device["slot"].is_string()&&device["slot"]==out.slot,"PCIe profile slot mismatch");
    if(scope=="pcie_speed"){keys(payload,{"identity","target_generation"});out.clock=true;out.family=3;out.value=integer(payload["target_generation"],5);need(out.value>=1,"PCIe generation must be positive");}
    else{keys(payload,{"kind","index","identity","mode"});const auto kind=string(payload["kind"]),mode=string(payload["mode"]);need(mode=="released"||mode=="disabled"||mode=="enabled","Unknown low-power request mode");
        if(kind=="individual"){out.family=1;out.index=integer(payload["index"],15);need(std::set<uint32_t>{0,4,10,11,12,13,14,15}.count(out.index)!=0,"Unknown individual power controller index");}
        else{need(payload["index"].is_null(),"Low-power index is only valid for individual controllers");if(kind=="elpg")out.family=0;else if(kind=="difr")out.family=4;else{out.clock=true;if(kind=="elcg")out.family=0;else if(kind=="blcg")out.family=1;else if(kind=="slcg")out.family=2;else need(false,"Unknown low-power request kind");}}
        need(mode!="enabled"||(out.clock&&out.family!=1),"Enabled mode is not supported by this low-power family");out.value=mode=="released"?0u:mode=="disabled"?1u:2u;
    }return out;
}
SavingProfileSplit split_saving_profile(const ProfileJson& document){
    std::vector<ProfileJson> children;if(document.is_object()&&document.value("scope",std::string{})=="combined"){
        keys(document,{"schema","scope","saved_at","saved_ts","components"});need(integer(document["schema"],2)==2&&document["saved_at"].is_string()&&!document["saved_at"].get<std::string>().empty()&&document["saved_ts"].is_number()&&std::isfinite(document["saved_ts"].get<double>()),"Invalid combined saving metadata");const auto& c=document["components"];need(c.is_array()&&!c.empty()&&c.size()<=128,"Invalid combined saving components");for(const auto& entry:c){keys(entry,{"name","state"});(void)string(entry["name"]);children.push_back(entry["state"]);}
    }else children.push_back(document);
    SavingProfileSplit result;std::set<std::string> occupied;std::optional<PythonSavingRequest> identity;
    for(const auto& child:children){const auto parsed=parse_python_saving_profile(child);need(occupied.insert(saving_profile_target(parsed)).second,"Duplicate saving-profile target");if(identity){need(identity->slot==parsed.slot&&identity->architecture==parsed.architecture&&identity->device_id==parsed.device_id&&identity->subsystem_id==parsed.subsystem_id&&(identity->uuid.empty()||parsed.uuid.empty()||identity->uuid==parsed.uuid),"Combined saving profile identities differ");if(identity->uuid.empty())identity->uuid=parsed.uuid;}else identity=parsed;(parsed.clock?result.clock_gating:result.power_saving).push_back(child);}return result;
}
namespace {
ProfileSelection describe(const ProfileJson& doc,bool clock){
    ProfileSelection result;std::set<std::string> targets;
    const bool native_clock=doc.is_object()&&doc.contains("format")&&doc["format"]=="druta-native-clock-gating";
    const bool native_saving=doc.is_object()&&doc.value("scope",std::string{})=="power_saving_requests";
    if(native_clock||native_saving){need(native_clock==clock,"Profile selects the other saving service");const ProfileJson* rows=nullptr;
        if(clock){keys(doc,{"format","version","target","entries"});need(integer(doc["version"],1)==1,"Unsupported clock profile version");keys(doc["target"],{"uuid","slot"});result.uuid=string(doc["target"]["uuid"]);result.slot=string(doc["target"]["slot"]);rows=&doc["entries"];}
        else{keys(doc,{"schema","scope","uuid","slot","requests"});need(integer(doc["schema"],1)==1,"Unsupported saving profile version");result.uuid=string(doc["uuid"]);result.slot=string(doc["slot"]);rows=&doc["requests"];}
        need(rows->is_array()&&!rows->empty()&&rows->size()<=(clock?4u:24u),"Invalid saving profile request count");
        for(const auto& row:*rows){const auto family=integer(row.at("family"),clock?3u:4u);(void)string(row.at("binding"),16384);std::string target;
            if(clock){if(family==3){keys(row,{"family","binding","generation"});need(integer(row["generation"],5)>=1,"Invalid saved PCIe target");}else{keys(row,{"family","binding","mode"});const auto mode=integer(row["mode"],2);need(family!=1||mode!=2,"BLCG enabled request is unsupported");}target="clock-gating:"+std::to_string(family);}
            else{keys(row,{"family","index","value","binding"});const auto index=integer(row["index"],31),value=integer(row["value"]);const std::set<uint32_t> individual={0,4,10,11,12,13,14,15};const std::set<uint32_t> adaptive={0,2,4,5,6,7};need((family==1||family==2)?individual.count(index)!=0:family==3?adaptive.count(index)!=0:index==0,"Invalid saving family index");need(family==2?value>0:value<=1,"Invalid saved request value");target="saving:"+std::to_string(family)+":"+std::to_string(index);}
            need(targets.insert(target).second,"Duplicate saving-profile target");result.targets.push_back(target);
        }
    }else{const auto split=split_saving_profile(doc);need(clock?split.power_saving.empty():split.clock_gating.empty(),"Cross-service profile requires split_saving_profile before standalone staging");const auto& children=clock?split.clock_gating:split.power_saving;need(!children.empty(),"Profile selects no requests for this service");for(const auto& child:children){const auto parsed=parse_python_saving_profile(child);result.slot=parsed.slot;if(!parsed.uuid.empty())result.uuid=parsed.uuid;result.targets.push_back(saving_profile_target(parsed));}}
    return result;
}
ProfileJson select(const ProfileJson& doc,const std::vector<std::string>& selected,bool clock){const auto description=describe(doc,clock);need(!selected.empty(),"Select saving profile targets");std::set<std::string> keep;for(const auto& target:selected)need(std::find(description.targets.begin(),description.targets.end(),target)!=description.targets.end()&&keep.insert(target).second,"Unknown or duplicate saving profile selection");auto out=doc;
    if(doc.contains("format")||(doc.contains("scope")&&doc["scope"]=="power_saving_requests")){const char* key=clock?"entries":"requests";ProfileJson rows=ProfileJson::array();for(size_t i=0;i<description.targets.size();++i)if(keep.count(description.targets[i]))rows.push_back(doc[key][i]);out[key]=rows;}
    else if(doc.value("scope",std::string{})=="combined"){ProfileJson children=ProfileJson::array();for(const auto& entry:doc["components"])if(keep.count(saving_profile_target(parse_python_saving_profile(entry["state"]))))children.push_back(entry);out["components"]=children;}
    (void)describe(out,clock);return out;
}
}
ProfileSelection describe_power_saving_profile(const ProfileJson& doc){return describe(doc,false);}
ProfileJson select_power_saving_profile(const ProfileJson& doc,const std::vector<std::string>& selection){return select(doc,selection,false);}
ProfileSelection describe_clock_gating_profile(const ProfileJson& doc){return describe(doc,true);}
ProfileJson select_clock_gating_profile(const ProfileJson& doc,const std::vector<std::string>& selection){return select(doc,selection,true);}
void match_python_saving_target(const PythonSavingRequest& saved,const dc_gpu_identity& live,const std::optional<dc_target>& target){
    need(target.has_value()&&target->size==sizeof(dc_target)&&target->handle&&live.size==sizeof(live)&&live.nvapi_ok&&live.nvapi_selected&&!text(live.pairing_error).size(),"Read qualified paired saving target first");need(saved.slot==text(live.slot)&&saved.slot==text(target->slot)&&(saved.uuid.empty()||saved.uuid==text(live.uuid))&&saved.architecture==target->architecture&&saved.device_id&&*saved.device_id==target->device_id&&saved.subsystem_id&&*saved.subsystem_id==target->subsystem_id&&target->handle==live.nvapi_handle&&target->device_id==live.nvapi_devid&&target->subsystem_id==live.nvapi_subsys,"Saved saving-profile target differs from current paired adapter");
}
}
