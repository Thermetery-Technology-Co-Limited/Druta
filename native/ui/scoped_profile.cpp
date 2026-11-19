// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "scoped_profile.h"
#include <algorithm>
#include <cmath>
#include <limits>
#include <set>
#include <stdexcept>
#include <tuple>
namespace druta::ui {
ScopedKey ScopedKey::rail_field(uint32_t r,uint32_t f){ScopedKey k;k.rail=r;k.field=f;return k;}
ScopedKey ScopedKey::policy_request(uint32_t p){ScopedKey k;k.kind=Kind::policy;k.policy=p;return k;}
ScopedKey ScopedKey::board_power(){ScopedKey k;k.kind=Kind::board;return k;}
bool operator==(const ScopedKey& a,const ScopedKey& b){return a.kind==b.kind&&a.rail==b.rail&&a.field==b.field&&a.policy==b.policy;}
namespace {
constexpr size_t maximum_entries=1+32+DC_RAILS_COUNT*DC_RAILS_LIMIT_FIELDS;
[[noreturn]]void refuse(const std::string& why){throw std::runtime_error("Scoped profile: "+why);}
bool keys(const ProfileJson& j,std::initializer_list<const char*> expected){
    if(!j.is_object()||j.size()!=expected.size())return false;
    for(const char* key:expected)if(!j.contains(key))return false;return true;
}
void string_value(const std::string& value,size_t maximum,const char* name){
    if(value.empty()||value.size()>maximum||value.find('\0')!=std::string::npos)refuse(std::string("invalid ")+name);
}
void target(const ScopedTarget& t){string_value(t.uuid,512,"target UUID");string_value(t.slot,128,"target slot");}
bool same_target(const ScopedTarget& a,const ScopedTarget& b){return a.uuid==b.uuid&&a.slot==b.slot;}
int64_t integer(const ProfileJson& j){
    if(!j.is_number_integer()||(j.is_number_unsigned()&&j.get<uint64_t>()>static_cast<uint64_t>(INT64_MAX)))refuse("request/key must be an exact signed-range integer");
    return j.get<int64_t>();
}
void key_valid(const ScopedKey& k){
    if(k.kind==ScopedKey::Kind::rail){if(k.rail>=DC_RAILS_COUNT||k.field>=DC_RAILS_LIMIT_FIELDS||k.policy)refuse("invalid rail selection");}
    else if(k.kind==ScopedKey::Kind::policy){if(k.policy>=32||k.rail||k.field)refuse("invalid policy selection");}
    else if(k.kind!=ScopedKey::Kind::board||k.rail||k.field||k.policy)refuse("invalid board selection");
}
std::string name(const ScopedKey& k){
    return k.kind==ScopedKey::Kind::rail?"rail "+std::to_string(k.rail)+" field "+std::to_string(k.field):
        k.kind==ScopedKey::Kind::policy?"policy "+std::to_string(k.policy):"board power";
}
int rank(const ScopedKey& k){return k.kind==ScopedKey::Kind::board?0:k.kind==ScopedKey::Kind::policy?1:2;}
bool less(const ScopedKey& a,const ScopedKey& b){return std::make_tuple(rank(a),a.policy,a.rail,a.field)<std::make_tuple(rank(b),b.policy,b.rail,b.field);}
void unit_value(const ScopedKey& k,const std::string& unit,int64_t value){
    key_valid(k);
    if(k.kind==ScopedKey::Kind::rail){
        if(unit!="uV"||value<INT32_MIN||value>INT32_MAX)refuse(name(k)+": signed microvolt request/units invalid");
    }else{
        if((k.kind==ScopedKey::Kind::board?unit!="mW":unit!="mW"&&unit!="mA")||value<0||value>=UINT32_MAX||
            (k.kind==ScopedKey::Kind::board&&value==0))refuse(name(k)+": power/current request or units invalid");
    }
    const double display=static_cast<double>(value)/1000.0;
    if(!std::isfinite(display)||std::abs(display*1000.0-static_cast<double>(value))>0.000001)
        refuse(name(k)+": request is not representable through the existing writer precision");
}
const ScopedCurrent& selected(const ScopedSnapshot& current,const ScopedKey& key,bool writing){
    key_valid(key);const ScopedCurrent* found=nullptr;
    for(const auto& row:current.entries)if(row.key==key){if(found)refuse(name(key)+": duplicate current identity");found=&row;}
    if(!found)refuse(name(key)+": selected capability/readback unavailable");
    if(!found->fresh)refuse(name(key)+": fresh selected readback unavailable");
    if(writing&&!found->writable)refuse(name(key)+": selected write gate/capability unavailable");
    string_value(found->binding,4096,"selected binding");unit_value(key,found->unit,found->value);
    if(found->low>found->high||found->value<found->low||found->value>found->high)
        refuse(name(key)+": current request is not restorable in current bounds");
    if(found->pin_value && (key.kind!=ScopedKey::Kind::policy||*found->pin_value<0||*found->pin_value>=UINT32_MAX))
        refuse(name(key)+": existing pin cannot be restored");
    return *found;
}
ProfileJson entry(const ScopedCurrent& row){
    ProfileJson e={{"kind",row.key.kind==ScopedKey::Kind::rail?"rail":row.key.kind==ScopedKey::Kind::policy?"policy":"board"},
        {"binding",row.binding},{"unit",row.unit},{"value",row.value}};
    if(row.key.kind==ScopedKey::Kind::rail){e["rail"]=row.key.rail;e["field"]=row.key.field;}
    else if(row.key.kind==ScopedKey::Kind::policy)e["policy"]=row.key.policy;
    return e;
}
ScopedKey parse_key(const ProfileJson& e){
    if(!e.is_object()||!e.contains("kind")||!e["kind"].is_string())refuse("invalid typed entry");
    const auto kind=e["kind"].get<std::string>();ScopedKey key;
    if(kind=="rail"){
        if(!keys(e,{"kind","rail","field","binding","unit","value"}))refuse("invalid rail entry fields");
        const auto rail=integer(e["rail"]),field=integer(e["field"]);
        if(rail<0||rail>=DC_RAILS_COUNT||field<0||field>=DC_RAILS_LIMIT_FIELDS)refuse("invalid rail/field index");
        key=ScopedKey::rail_field(static_cast<uint32_t>(rail),static_cast<uint32_t>(field));
    }else if(kind=="policy"){
        if(!keys(e,{"kind","policy","binding","unit","value"}))refuse("invalid policy entry fields");
        const auto policy=integer(e["policy"]);if(policy<0||policy>=32)refuse("invalid policy index");key=ScopedKey::policy_request(static_cast<uint32_t>(policy));
    }else if(kind=="board"){
        if(!keys(e,{"kind","binding","unit","value"}))refuse("invalid board entry fields");key=ScopedKey::board_power();
    }else refuse("unknown control kind");
    return key;
}
}
ProfileJson capture_scoped_profile(const ScopedSnapshot& current,const std::vector<ScopedKey>& selection){
    target(current.target);if(selection.empty()||selection.size()>maximum_entries)refuse("empty or oversized selection");
    auto ordered=selection;std::sort(ordered.begin(),ordered.end(),less);
    ProfileJson entries=ProfileJson::array();std::vector<ScopedKey> seen;
    for(const auto& key:ordered){
        if(std::find(seen.begin(),seen.end(),key)!=seen.end())refuse("duplicate selected control");seen.push_back(key);
        entries.push_back(entry(selected(current,key,false)));
    }
    return {{"format","druta-native-scoped"},{"version",1},{"target",{{"uuid",current.target.uuid},{"slot",current.target.slot}}},{"entries",std::move(entries)}};
}
ScopedPlan plan_scoped_profile(const ProfileJson& doc,const ScopedSnapshot& current,bool require_writable){
    target(current.target);
    if(!keys(doc,{"format","version","target","entries"})||doc["format"]!="druta-native-scoped"||
        !doc["version"].is_number_integer()||integer(doc["version"])!=1||!keys(doc["target"],{"uuid","slot"})||
        !doc["target"]["uuid"].is_string()||!doc["target"]["slot"].is_string()||!doc["entries"].is_array()||
        doc["entries"].empty()||doc["entries"].size()>maximum_entries)refuse("invalid format, version, target or entries");
    ScopedPlan plan;plan.target={doc["target"]["uuid"].get<std::string>(),doc["target"]["slot"].get<std::string>()};
    target(plan.target);if(!same_target(plan.target,current.target))refuse("target adapter UUID/slot changed");
    bool board_selected=false;
    for(const auto& e:doc["entries"]){
        const auto key=parse_key(e);
        for(const auto& previous:plan.steps)if(previous.key==key)refuse("duplicate profile control");
        if(!e["binding"].is_string()||!e["unit"].is_string())refuse(name(key)+": invalid binding/units");
        const auto binding=e["binding"].get<std::string>(),unit=e["unit"].get<std::string>();
        string_value(binding,4096,"saved binding");const auto value=integer(e["value"]);unit_value(key,unit,value);
        const auto& live=selected(current,key,require_writable);
        if(binding!=live.binding||unit!=live.unit)refuse(name(key)+": binding/reference/geometry or units changed");
        if(value<live.low||value>live.high)refuse(name(key)+": saved request outside current bounds");
        ScopedStep step;step.key=key;step.before=live.value;step.value=value;step.unit=unit;step.binding=binding;
        step.writable=live.writable;
        step.changed=live.value!=value||(key.kind==ScopedKey::Kind::policy&&live.pin_value!=value);
        step.command.operation=Command::Operation::apply;step.command.value=static_cast<double>(value)/1000.0;step.command.expected_binding=binding;
        if(key.kind==ScopedKey::Kind::rail)step.command.rail=RailId{key.rail,key.field};
        else if(key.kind==ScopedKey::Kind::policy)step.command.power_policy=key.policy;
        else {step.command.control=ControlId::power;board_selected=true;}
        if(key.kind!=ScopedKey::Kind::rail)plan.coupled_power=true;
        plan.steps.push_back(std::move(step));
    }
    std::sort(plan.steps.begin(),plan.steps.end(),[](const auto& a,const auto& b){return less(a.key,b.key);});
    for(auto& step:plan.steps)step.execute_required=step.changed||(board_selected&&step.key.kind==ScopedKey::Kind::policy);
    plan.summary=std::to_string(plan.steps.size())+" selected requests; explicit application only";
    if(plan.coupled_power)plan.summary+="; power writes may change coupled requests: re-read between writes and verify all selected values/pins";
    return plan;
}
std::string scoped_profile_readback_error(const ScopedPlan& plan,const ScopedSnapshot& current){
    try{
        target(plan.target);target(current.target);if(!same_target(plan.target,current.target))refuse("readback target changed");
        if(plan.steps.empty())refuse("empty readback plan");
        std::vector<ScopedKey> seen;
        for(const auto& step:plan.steps){
            if(std::find(seen.begin(),seen.end(),step.key)!=seen.end())refuse("duplicate readback control");seen.push_back(step.key);
            const auto& live=selected(current,step.key,false);unit_value(step.key,step.unit,step.value);
            if(live.binding!=step.binding||live.unit!=step.unit)refuse(name(step.key)+": readback binding/units changed");
            if(live.value!=step.value)refuse(name(step.key)+": selected request readback mismatch");
            if(step.key.kind==ScopedKey::Kind::policy&&live.pin_value!=step.value)refuse(name(step.key)+": selected explicit pin readback mismatch");
        }return {};
    }catch(const std::exception& e){return e.what();}
}
}
