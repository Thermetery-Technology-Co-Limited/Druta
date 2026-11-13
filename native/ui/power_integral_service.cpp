// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_integral_service.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <set>
#include <map>
#include <sstream>
#include <stdexcept>
namespace druta::ui {
namespace {
constexpr uint32_t info_command=0x2080A618,control_command=0x2080A61A,set_command=0x2080E61B;
constexpr uint32_t info_bytes=20512,control_bytes=13876;
struct Guard {
    PowerIntegralApi& api;dc_gpu* gpu;bool held;
    Guard(PowerIntegralApi& a,dc_gpu* g):api(a),gpu(g),held(g && a.lock && a.unlock && a.lock(g)==DC_OK){}
    ~Guard(){if(held)api.unlock(gpu);}
};
template<size_t N>bool text_ok(const char(&text)[N]) {return std::memchr(text,0,N)!=nullptr;}
bool same_target(const dc_target& a,const dc_target& b) {
    return text_ok(a.slot) && text_ok(b.slot) && a.slot[0] && std::strcmp(a.slot,b.slot)==0
        && a.architecture==b.architecture && a.device_id==b.device_id && a.subsystem_id==b.subsystem_id && a.handle==b.handle;
}
bool same_identity(const dc_gpu_identity& a,const dc_gpu_identity& b) {
    return text_ok(a.slot) && text_ok(b.slot) && text_ok(a.uuid) && text_ok(b.uuid)
        && text_ok(a.pairing_error) && text_ok(b.pairing_error) && !a.pairing_error[0] && !b.pairing_error[0]
        && a.nvapi_ok && b.nvapi_ok && a.nvapi_selected && b.nvapi_selected
        && a.slot[0] && std::strcmp(a.slot,b.slot)==0 && std::strcmp(a.uuid,b.uuid)==0
        && a.nvapi_handle==b.nvapi_handle && a.nvapi_devid==b.nvapi_devid && a.nvapi_subsys==b.nvapi_subsys
        && a.nvml_selected==b.nvml_selected && a.nvml_has_devid==b.nvml_has_devid
        && a.nvml_devid==b.nvml_devid && a.nvml_subsys==b.nvml_subsys;
}
bool geometry(const dc_power_layout& l) {
    if(l.size!=sizeof(l))return false;
    const uint32_t sizes[]={info_bytes,397048,control_bytes,control_bytes};
    const uint32_t commands[]={info_command,0x2080A619,control_command,set_command};
    for(size_t i=0;i<4;++i)if(l.sizes[i]!=sizes[i] || (l.has_commands && l.commands[i]!=commands[i]))return false;
    return l.info_base==0xCC && l.info_stride==0x104 && l.status_base==0x9C && l.status_stride==0x1730
        && l.control_base==0x14 && l.control_stride==0xC4
        && (!l.has_status_mask_word || l.status_mask_word==1) && (!l.has_control_mask_word || l.control_mask_word==4);
}
uint32_t byte(const std::vector<uint32_t>& words,size_t offset){return (words[offset/4]>>((offset%4)*8))&255;}
uint16_t u16(const std::vector<uint32_t>& words,size_t offset){return static_cast<uint16_t>(byte(words,offset)|(byte(words,offset+1)<<8));}
void put16(std::vector<uint32_t>& words,size_t offset,uint16_t value) {
    const uint32_t shift=static_cast<uint32_t>((offset%4)*8),mask=0xFFFFu<<shift;
    words[offset/4]=(words[offset/4]&~mask)|(static_cast<uint32_t>(value)<<shift);
}
size_t field_index(PowerIntegralField field){return field==PowerIntegralField::lower?0:1;}
size_t field_offset(uint32_t policy,PowerIntegralField field){return 0x14+static_cast<size_t>(policy)*0xC4+(field==PowerIntegralField::lower?12:14);}
std::vector<uint32_t> signature(const std::vector<uint32_t>& info,uint32_t policy) {
    const auto start=info.begin()+(0xCC+static_cast<size_t>(policy)*0x104)/4;
    return {start,start+0x104/4};
}
std::string binding_text(const std::vector<uint32_t>& info,uint32_t policy) {
    std::ostringstream text;text<<std::hex;for(const auto word:signature(info,policy))text<<word<<':';return text.str();
}
PowerIntegralRow decode(const std::vector<uint32_t>& info,const std::vector<uint32_t>& control,uint32_t policy) {
    PowerIntegralRow row;row.policy=policy;
    const size_t a=0xCC+static_cast<size_t>(policy)*0x104,b=0x14+static_cast<size_t>(policy)*0xC4;
    row.type=byte(info,a+4);row.channel=byte(info,a+5);row.unit=byte(info,a+6);
    row.request=control[(b+4)/4];row.lower=u16(control,b+12);row.upper=u16(control,b+14);
    row.reported_lower=u16(info,a+0x24);row.reported_upper=u16(info,a+0x26);
    row.past_samples=static_cast<uint8_t>(byte(control,b+9));row.next_samples=static_cast<uint8_t>(byte(control,b+10));
    if((byte(info,a)!=0x30 && byte(info,a)!=0x40) || control[b/4]!=row.type)row.reason="Policy type or table format is not understood";
    else if(row.unit>1)row.reason="Policy unit is not understood";
    else if(byte(control,b+8)!=2 || !row.past_samples || !row.next_samples || byte(info,a+0x20)!=2
        || !byte(info,a+0x22) || !byte(info,a+0x23))row.reason="Integral mode or sample counts are not understood";
    else if(row.lower>row.upper || !row.upper || row.reported_lower>row.reported_upper || !row.reported_upper)row.reason="Integral bounds are inconsistent";
    else if(info[(a+8)/4]>info[(a+16)/4] || row.request<info[(a+8)/4] || row.request>info[(a+16)/4])row.reason="Ordinary policy request is outside its reported range";
    row.available=row.reason.empty();row.binding=binding_text(info,policy);return row;
}
bool valid_value(const PowerIntegralRow& row,PowerIntegralField field,uint32_t raw) {
    return raw<=65535 && (field==PowerIntegralField::lower?raw<=row.upper:(raw>0 && raw>=row.lower));
}
}
std::optional<uint16_t> power_integral_percent_to_raw(double percent) {
    if(!std::isfinite(percent) || percent<0 || percent>1600)return {};
    const double raw=std::floor(percent*4096.0/100.0+0.5);
    if(raw>65535)return {};return static_cast<uint16_t>(raw);
}
double power_integral_raw_to_percent(uint16_t raw){return static_cast<double>(raw)*100.0/4096.0;}
PowerIntegralService::PowerIntegralService(PowerIntegralApi api,dc_gpu* gpu,const dc_gpu_identity& identity)
    :api_(api),gpu_(gpu),identity_(identity){}
bool PowerIntegralService::has_undo() const {
    for(const auto& row:owned_)for(const auto& field:row)if(field.value.owned)return true;return false;
}
bool PowerIntegralService::binding() {
    if(!api_.target_bind || !api_.identity || !api_.probe_architecture){data_.message="Paired target interfaces unavailable";return false;}
    for(int n=0;n<2;++n) {
        dc_target target{};target.size=sizeof(target);dc_error error{};error.size=sizeof(error);
        dc_gpu_identity id{};id.size=sizeof(id);
        const auto status=api_.target_bind(gpu_,0,&target,&error);
        if(status!=DC_OK){if(error.kind==DC_ERR_TRANSIENT)continue;break;}
        if(api_.identity(gpu_,&id)!=DC_OK)continue;
        if(target.size!=sizeof(target) || id.size!=sizeof(id) || !text_ok(target.slot) || !target.slot[0] || !target.handle
            || (target_ && !same_target(*target_,target)) || !same_identity(identity_,id)
            || std::strcmp(target.slot,id.slot)!=0 || target.handle!=id.nvapi_handle
            || target.device_id!=id.nvapi_devid || target.subsystem_id!=id.nvapi_subsys)break;
        if(api_.probe_architecture(target.architecture)!=1){data_.message="Modern power feedback requires a supported Pascal-or-later PERF architecture";return false;}
        if(!target_)target_=target;
        return true;
    }
    data_.message="Selected adapter identity could not be verified";return false;
}
bool PowerIntegralService::capture_once(Snapshot& state,int32_t& kind) {
    kind=DC_ERR_TRANSIENT;
    if(!binding()){kind=DC_ERR_IDENTITY;return false;}
    if(!api_.abi || !api_.rm || !api_.policy_mask_set){kind=DC_ERR_UNSUPPORTED;data_.message="Power-policy transport interfaces unavailable";return false;}
    dc_error error{};error.size=sizeof(error);state={};state.layout.size=sizeof(state.layout);uint32_t index=0;
    if(api_.abi(gpu_,&state.layout,&index,&error)!=DC_OK){kind=error.kind;data_.message=text_ok(error.message)?error.message:"Malformed ABI error";return false;}
    if(!geometry(state.layout)){kind=DC_ERR_UNSUPPORTED;data_.message="Power-policy packet geometry or command mapping is not understood";return false;}
    auto get=[&](uint32_t command,uint32_t mask,std::vector<uint32_t>& words,uint32_t size) {
        words.resize(size/4);uint32_t count=0;error={};error.size=sizeof(error);
        if(api_.rm(gpu_,command,nullptr,0,command==control_command?1:0,mask,0,words.data(),static_cast<uint32_t>(words.size()),&count,&error)!=DC_OK){
            kind=error.kind;data_.message=text_ok(error.message)?error.message:"Malformed policy error";return false;}
        if(count!=words.size()){kind=DC_ERR_MALFORMED;data_.message="Power-policy packet length is not understood";return false;}return true;
    };
    if(!get(info_command,0,state.info,info_bytes))return false;
    const uint32_t mask=state.info[1];
    if(mask && !get(control_command,mask,state.control,control_bytes))return false;
    if(mask && (state.control[0]!=0 || state.control[1]!=0 || state.control[2]!=0 || state.control[3]!=255 || state.control[4]!=mask)){
        kind=DC_ERR_MALFORMED;data_.message="Policy CONTROL header does not match INFO";return false;}
    if(api_.policy_mask_set(gpu_,mask)!=DC_OK){data_.message="Could not bind live policy mask";return false;}
    if(!binding()){kind=DC_ERR_IDENTITY;return false;}return true;
}
bool PowerIntegralService::capture(Snapshot& state) {
    for(int attempt=0;attempt<2;++attempt){int32_t kind=0;if(capture_once(state,kind))return true;
        if(kind!=DC_ERR_TRANSIENT || attempt)break;
        if(api_.transport_reset)api_.transport_reset(gpu_,DC_TRANSPORT_POWER);
    }
    data_.current=false;
    for(auto& row:owned_)for(auto& field:row)if(field.value.owned){field.value.confirmed=false;field.value.recovery=true;}
    for(auto& row:data_.rows)row.undo={owned_[row.policy][0].value,owned_[row.policy][1].value};
    return false;
}
void PowerIntegralService::publish(const Snapshot& state) {
    data_.rows.clear();data_.target=target_;data_.identity=identity_;data_.current=true;
    for(uint32_t policy=0;policy<32;++policy)if(state.info[1]&(1u<<policy)) {
        auto row=decode(state.info,state.control,policy);
        for(size_t i=0;i<2;++i){auto& owner=owned_[policy][i];if(owner.value.owned){
            const uint16_t actual=i==0?row.lower:row.upper;
            owner.value.confirmed=owner.info_row==signature(state.info,policy) && row.available && actual==owner.value.requested;
            owner.value.conflict=!owner.value.confirmed && actual!=owner.value.initial
                && !(owner.pending && (owner.pending_before==actual || owner.pending_after==actual));
            owner.value.recovery=owner.pending || !owner.value.confirmed;
        }row.undo[i]=owner.value;}
        data_.rows.push_back(std::move(row));
    }
    for(uint32_t policy=0;policy<32;++policy)if(!(state.info[1]&(1u<<policy))
        && (owned_[policy][0].value.owned || owned_[policy][1].value.owned)) {
        PowerIntegralRow row;row.policy=policy;row.reason="Previously edited policy is absent; recovery retained";
        for(size_t i=0;i<2;++i){auto& owner=owned_[policy][i];if(owner.value.owned){owner.value.confirmed=false;owner.value.recovery=true;}row.undo[i]=owner.value;}
        data_.rows.push_back(std::move(row));
    }
}
PowerIntegralData PowerIntegralService::read() {
    Guard guard(api_,gpu_);if(!guard.held){data_.current=false;data_.message="GPU lock unavailable";return data_;}
    Snapshot state;if(capture(state)){publish(state);data_.message="Current feedback requests read.";}
    return data_;
}
bool PowerIntegralService::dispatch(const Snapshot& state,uint32_t policy,PowerIntegralField field,uint16_t raw,Snapshot& after,const std::function<void()>& on_launch) {
    auto projected=state.control;put16(projected,field_offset(policy,field),raw);
    auto request=projected;request[4]=1u<<policy;
    auto& owner=owned_[policy][field_index(field)];
    owner.pending_before=u16(state.control,field_offset(policy,field));owner.pending_after=raw;
    if(!binding()) {
        data_.current=false;
        for(auto& row:data_.rows)row.undo={owned_[row.policy][0].value,owned_[row.policy][1].value};
        return false;
    }
    dc_error error{};error.size=sizeof(error);uint32_t count=0;std::vector<uint32_t> result(control_bytes/4);
    // Exactly one SET; never retry a failed write. A failed call may have issued.
    if(on_launch)on_launch();
    const auto status=api_.rm(gpu_,set_command,request.data(),static_cast<uint32_t>(request.size()),0,0,1,
        result.data(),static_cast<uint32_t>(result.size()),&count,&error);
    const bool read=capture(after);
    if(status==DC_OK && read && after.info==state.info && after.control==projected)return true;
    if(read)publish(after);
    data_.message=status!=DC_OK && text_ok(error.message) && error.message[0]?error.message:"Policy INFO or CONTROL did not read back exactly";
    return false;
}
PowerIntegralResult PowerIntegralService::apply(uint32_t policy,PowerIntegralField field,uint32_t raw,std::optional<uint16_t> expected) {
    PowerIntegralResult result;
    if(data_.profile_recovery_pending){result.message="Recover the uncertain selected profile before a new edit";return result;}
    if(policy>=32 || (field!=PowerIntegralField::lower && field!=PowerIntegralField::upper) || raw>65535){result.message="Invalid policy, field or unsigned ratio";return result;}
    for(const auto& row:owned_)for(const auto& owner:row)if(owner.value.recovery){result.message="Resolve uncertain feedback Undo / Restore before a new edit";return result;}
    Guard guard(api_,gpu_);if(!guard.held){result.message="GPU lock unavailable";return result;}
    Snapshot before;if(!capture(before)){result.message=data_.message;return result;}publish(before);
    for(const auto& entry:owned_)for(const auto& owned:entry)if(owned.value.recovery){result.message="Resolve replaced or uncertain feedback fields before a new edit";return result;}
    if(!(before.info[1]&(1u<<policy))){result.message="Policy is absent";return result;}
    const auto row=decode(before.info,before.control,policy);
    if(!row.available || !valid_value(row,field,raw)){result.message=row.available?"Ratio conflicts with active opposite bound":row.reason;return result;}
    const size_t index=field_index(field);auto& owner=owned_[policy][index];
    const uint16_t current=u16(before.control,field_offset(policy,field));
    if(expected && current!=*expected){result.message="Selected feedback field changed since this draft was read; read and review before applying";return result;}
    if(owner.value.owned && (!owner.value.confirmed || owner.info_row!=signature(before.info,policy))){result.message="Owned feedback field was replaced; leave foreign request untouched";return result;}
    if(current==raw){result.ok=true;result.message="Requested ratio already active; no write";return result;}
    const auto old=owner;const auto old_profile=profile_undo_;
    if(!owner.value.owned){owner.value.initial=current;owner.info_row=signature(before.info,policy);}
    owner.value.owned=true;owner.value.previous=current;owner.value.requested=static_cast<uint16_t>(raw);
    owner.value.can_undo=true;owner.value.confirmed=false;owner.value.recovery=true;owner.value.conflict=false;owner.pending=true;
    result.may_have_changed=true;Snapshot after;
    if(dispatch(before,policy,field,static_cast<uint16_t>(raw),after,[&]{profile_undo_.reset();data_.profile_undo_available=data_.profile_recovery_pending=false;})) {
        owner.pending=false;owner.value.confirmed=true;owner.value.recovery=false;publish(after);result.ok=true;
        result.message="Selected feedback ratio applied and exact packet readback verified";data_.message=result.message;return result;
    }
    const auto failure=data_.message;
    // Rebase restoration onto a fresh GET. Never replay the captured table or
    // overwrite a third party's selected field after an uncertain SET.
    Snapshot fresh;
    if(capture(fresh) && (fresh.info[1]&(1u<<policy)) && signature(fresh.info,policy)==owner.info_row) {
        const auto actual=u16(fresh.control,field_offset(policy,field));
        if(actual==current){owner=old;result.rollback_verified=true;publish(fresh);}
        else if(actual==raw && decode(fresh.info,fresh.control,policy).available
            && valid_value(decode(fresh.info,fresh.control,policy),field,current)) {
            result.rollback_attempted=true;Snapshot restored;
            if(dispatch(fresh,policy,field,current,restored)){owner=old;result.rollback_verified=true;publish(restored);}
        }
    }
    if(result.rollback_verified&&old_profile){profile_undo_=old_profile;data_.profile_undo_available=true;data_.profile_recovery_pending=false;}
    if(!result.rollback_verified){owner.value.confirmed=false;owner.value.recovery=true;Snapshot observed;if(capture(observed))publish(observed);}
    result.message=failure+(result.rollback_verified?". Previous selected field restored and verified":". Selected-field recovery retained; no blind table restoration");
    data_.message=result.message;return result;
}
PowerIntegralResult PowerIntegralService::restore_locked(uint32_t policy,PowerIntegralField field,bool previous) {
    PowerIntegralResult result;
    if(policy>=32 || (field!=PowerIntegralField::lower && field!=PowerIntegralField::upper)){result.message="Invalid policy or field";return result;}
    auto& owner=owned_[policy][field_index(field)];
    if(!owner.value.owned || (previous && !owner.value.can_undo)){result.message="No selected-field Undo / Restore owned by this session";return result;}
    Snapshot before;if(!capture(before)){result.message=data_.message;return result;}
    if(!(before.info[1]&(1u<<policy)) || signature(before.info,policy)!=owner.info_row){owner.value.confirmed=false;owner.value.recovery=true;
        publish(before);result.message="Original policy binding changed; recovery retained";data_.message=result.message;return result;}
    const uint16_t desired=previous?owner.value.previous:owner.value.initial;
    const uint16_t actual=u16(before.control,field_offset(policy,field));
    if(actual!=owner.value.requested && actual!=desired && !(owner.pending && (owner.pending_before==actual || owner.pending_after==actual))){owner.value.confirmed=false;owner.value.recovery=true;owner.value.conflict=true;publish(before);
        result.message="Selected field was replaced; foreign request left untouched";return result;}
    Snapshot after=before;
    if(actual!=desired) {
        const auto row=decode(before.info,before.control,policy);
        if(!row.available || !valid_value(row,field,desired)){result.message="Captured field conflicts with current policy or opposite bound";return result;}
        result.may_have_changed=true;
        // Track both outcomes of a failed restore by keeping desired in previous.
        owner.value.previous=desired;owner.value.can_undo=true;owner.value.confirmed=false;owner.value.recovery=true;owner.pending=true;
        if(!dispatch(before,policy,field,desired,after)){result.message=data_.message+". Recovery retained";return result;}
    }
    if(desired==owner.value.initial)owner={};
    else {owner.pending=false;owner.value.requested=desired;owner.value.can_undo=false;owner.value.confirmed=true;owner.value.recovery=false;owner.value.conflict=false;}
    publish(after);result.ok=true;result.message=previous?"Previous selected-field request restored and verified":"Captured initial selected field restored and verified";data_.message=result.message;return result;
}
PowerIntegralResult PowerIntegralService::restore(uint32_t policy,PowerIntegralField field) {
    if(profile_undo_)return {false,false,false,false,"Use grouped profile Undo / recovery first"};
    Guard guard(api_,gpu_);if(!guard.held)return {false,false,false,false,"GPU lock unavailable"};return restore_locked(policy,field,false);
}
PowerIntegralResult PowerIntegralService::undo(uint32_t policy,PowerIntegralField field) {
    if(profile_undo_)return {false,false,false,false,"Use grouped profile Undo / recovery first"};
    Guard guard(api_,gpu_);if(!guard.held)return {false,false,false,false,"GPU lock unavailable"};return restore_locked(policy,field,true);
}
namespace {
void require(bool condition,const char* message){if(!condition)throw std::runtime_error(message);}
void profile_keys(const ProfileJson& object,std::initializer_list<const char*> names){require(object.is_object()&&object.size()==names.size(),"Invalid integral profile fields");for(const auto name:names)require(object.contains(name),"Missing integral profile field");}
int64_t profile_integer(const ProfileJson& value,int64_t maximum=65535){require(value.is_number_integer(),"Integral profile needs an integer");if(value.is_number_unsigned())require(value.get<uint64_t>()<=uint64_t(maximum),"Integral integer out of range");const auto n=value.get<int64_t>();require(n>=0&&n<=maximum,"Integral integer out of range");return n;}
const PowerIntegralRow& selected_row(const PowerIntegralData& data,uint32_t policy){require(data.current&&data.target.has_value(),"Read current integral policies first");const auto row=std::find_if(data.rows.begin(),data.rows.end(),[&](const auto& r){return r.policy==policy;});require(row!=data.rows.end()&&row->available,"Selected integral policy unavailable");return *row;}
uint16_t current_value(const PowerIntegralRow& row,PowerIntegralField field){return field==PowerIntegralField::lower?row.lower:row.upper;}
PowerIntegralPlan make_plan(const PowerIntegralData& data,const std::vector<PowerIntegralRequest>& requests,bool validate_ranges=true){
    PowerIntegralPlan plan;require(!requests.empty()&&requests.size()<=64,"Select integral fields");require(data.current&&data.target.has_value(),"Read current integral policies first");plan.identity=data.identity;plan.target=*data.target;
    std::set<std::pair<uint32_t,size_t>> seen;std::map<uint32_t,std::pair<uint16_t,uint16_t>> bounds;
    for(const auto& r:requests){require(r.policy<32&&(r.field==PowerIntegralField::lower||r.field==PowerIntegralField::upper)&&r.raw<=65535,"Invalid integral selection or ratio");require(seen.insert({r.policy,field_index(r.field)}).second,"Duplicate integral field");const auto& row=selected_row(data,r.policy);
        require(row.type==r.type&&row.channel==r.channel&&row.unit==r.unit,"Integral policy metadata changed");require(r.binding.empty()||r.binding==row.binding,"Integral policy binding changed");const auto before=current_value(row,r.field);require(!r.expected||before==*r.expected,"Integral draft baseline changed");
        if(!bounds.count(r.policy))bounds[r.policy]={row.lower,row.upper};auto& b=bounds[r.policy];if(r.field==PowerIntegralField::lower)b.first=static_cast<uint16_t>(r.raw);else b.second=static_cast<uint16_t>(r.raw);
        plan.requests.push_back(r);plan.before.push_back(before);plan.previous_owners.push_back(row.undo[field_index(r.field)]);plan.bindings.push_back(row.binding);
    }
    if(validate_ranges)for(const auto& b:bounds)require(b.second.second&&b.second.first<=b.second.second,"Selected integral bounds are inverted");plan.ok=true;plan.message="All selected integral bounds preflighted; no writes";return plan;
}
std::vector<size_t> safe_order(const PowerIntegralPlan& plan,const PowerIntegralData& data,bool restoring){
    std::vector<size_t> order;for(uint32_t policy=0;policy<32;++policy){std::optional<size_t> lower,upper;for(size_t i=0;i<plan.requests.size();++i)if(plan.requests[i].policy==policy)(plan.requests[i].field==PowerIntegralField::lower?lower:upper)=i;
        if(!lower&&!upper)continue;const auto& row=selected_row(data,policy);const auto desired_lower=lower?(restoring?plan.before[*lower]:plan.requests[*lower].raw):row.lower;
        if(desired_lower>row.upper){if(upper)order.push_back(*upper);if(lower)order.push_back(*lower);}else{if(lower)order.push_back(*lower);if(upper)order.push_back(*upper);}}
    return order;
}
}
PowerIntegralPlan PowerIntegralService::plan(const Snapshot& state,const std::vector<PowerIntegralRequest>& requests){publish(state);return make_plan(data_,requests);}
PowerIntegralPlan PowerIntegralService::preview(const std::vector<PowerIntegralRequest>& requests){
    try{require(!data_.profile_recovery_pending,"Recover the uncertain integral profile before a new profile");Guard guard(api_,gpu_);require(guard.held,"GPU lock unavailable");Snapshot fresh;if(!capture(fresh))throw std::runtime_error(data_.message);auto prepared=plan(fresh,requests);for(const auto& row:owned_)for(const auto& owner:row)require(!owner.value.recovery,"Resolve uncertain integral fields first");return prepared;}catch(const std::exception& e){PowerIntegralPlan out;out.message=e.what();data_.message=out.message;return out;}
}
PowerIntegralResult PowerIntegralService::verify(const PowerIntegralPlan& receipt,bool original){
    PowerIntegralResult result;
    try {
        const auto n=receipt.requests.size();
        require(receipt.ok&&n>0&&n<=64&&receipt.before.size()==n&&receipt.bindings.size()==n&&receipt.previous_owners.size()==n,"Invalid selected integral receipt");
        require(receipt.identity.size==sizeof(dc_gpu_identity)&&receipt.target.size==sizeof(dc_target)&&same_identity(receipt.identity,identity_)&&target_&&same_target(receipt.target,*target_),"Integral receipt adapter changed");
        std::set<std::pair<uint32_t,size_t>> seen;
        for(size_t i=0;i<n;++i){const auto& r=receipt.requests[i];
            require(r.policy<32&&(r.field==PowerIntegralField::lower||r.field==PowerIntegralField::upper)&&r.raw<=65535&&seen.insert({r.policy,field_index(r.field)}).second&&!receipt.bindings[i].empty()&&(r.binding.empty()||r.binding==receipt.bindings[i]),"Invalid duplicate or malformed integral receipt field");
        }
        Guard guard(api_,gpu_);require(guard.held,"GPU lock unavailable");
        Snapshot fresh;if(!capture(fresh))throw std::runtime_error(data_.message);
        require(target_&&same_target(receipt.target,*target_),"Integral receipt target changed");
        for(size_t i=0;i<n;++i){const auto& r=receipt.requests[i];
            require((fresh.info[1]&(1u<<r.policy))!=0,"Selected integral policy absent");
            const auto row=decode(fresh.info,fresh.control,r.policy);
            require(row.available&&row.binding==receipt.bindings[i]&&row.type==r.type&&row.channel==r.channel&&row.unit==r.unit,"Selected integral policy binding changed");
            require(current_value(row,r.field)==(original?receipt.before[i]:r.raw),"Selected integral field differs");
        }
        result.ok=true;result.message=original?"Original selected integral requests verified":"Selected integral requests verified";
    }catch(const std::exception& e){result.message=e.what();}
    return result;
}
PowerIntegralResult PowerIntegralService::apply(const std::vector<PowerIntegralRequest>& requests,const std::function<void(const PowerIntegralPlan&)>& before_write){
    PowerIntegralResult result;PowerIntegralPlan issued;
    try{require(!data_.profile_recovery_pending,"Recover the uncertain integral profile before a new profile");Guard guard(api_,gpu_);require(guard.held,"GPU lock unavailable");Snapshot fresh;if(!capture(fresh))throw std::runtime_error(data_.message);const auto prepared=plan(fresh,requests);
        for(const auto& row:owned_)for(const auto& owner:row)require(!owner.value.recovery,"Resolve uncertain integral fields first");
        issued=prepared;issued.requests.clear();issued.before.clear();issued.previous_owners.clear();issued.bindings.clear();
        for(const auto i:safe_order(prepared,data_,false)){
            Snapshot current;if(!capture(current))throw std::runtime_error(data_.message);publish(current);
            // Every selected field must still equal its original or our verified
            // target. No later component can silently rebase the selected plan.
            for(size_t j=0;j<prepared.requests.size();++j){const auto& r=prepared.requests[j];const auto& row=selected_row(data_,r.policy);const bool launched=std::any_of(issued.requests.begin(),issued.requests.end(),[&](const auto& q){return q.policy==r.policy&&q.field==r.field;});require(row.binding==prepared.bindings[j]&&current_value(row,r.field)==(launched?r.raw:prepared.before[j]),"Selected integral field changed during profile");}
            const auto& r=prepared.requests[i];if(prepared.before[i]==r.raw)continue;const auto& row=selected_row(data_,r.policy);require(valid_value(row,r.field,r.raw),"Integral transition conflicts with current opposite bound");auto& owner=owned_[r.policy][field_index(r.field)];const auto old=owner;
            if(!owner.value.owned){owner.value.initial=prepared.before[i];owner.info_row=signature(current.info,r.policy);}owner.value.owned=true;owner.value.previous=prepared.before[i];owner.value.requested=static_cast<uint16_t>(r.raw);owner.value.can_undo=true;owner.value.confirmed=false;owner.value.recovery=true;owner.pending=true;
            bool launched=false;Snapshot after;bool verified=false;
            try{verified=dispatch(current,r.policy,r.field,static_cast<uint16_t>(r.raw),after,[&]{auto next=issued;next.requests.push_back(r);next.before.push_back(prepared.before[i]);next.previous_owners.push_back(prepared.previous_owners[i]);next.bindings.push_back(prepared.bindings[i]);if(before_write)before_write(next);issued=std::move(next);profile_undo_=issued;data_.profile_undo_available=true;data_.profile_recovery_pending=true;launched=true;});}catch(...){if(!launched)owner=old;throw;}
            if(!launched)owner=old;result.may_have_changed=result.may_have_changed||launched;require(verified,data_.message.c_str());owner.pending=false;owner.value.confirmed=true;owner.value.recovery=false;publish(after);
        }
        Snapshot final;if(!capture(final))throw std::runtime_error(data_.message);publish(final);for(size_t i=0;i<prepared.requests.size();++i){const auto& r=prepared.requests[i];const auto& row=selected_row(data_,r.policy);require(row.binding==prepared.bindings[i]&&current_value(row,r.field)==r.raw,"Final selected integral readback differs");}
        data_.profile_recovery_pending=false;result.ok=true;result.message="Selected integral profile applied and final readback verified";
    }catch(const std::exception& e){result.message=e.what();result.may_have_changed=!issued.requests.empty();if(result.may_have_changed){data_.profile_recovery_pending=true;result.message+="; launched fields retained for grouped Undo / recovery";}}
    data_.message=result.message;return result;
}
PowerIntegralResult PowerIntegralService::restore_profile_locked(const PowerIntegralPlan& receipt){
    PowerIntegralResult result;bool launched=false;
    bool current_receipt=receipt.ok&&profile_undo_&&receipt.previous_owners.size()==profile_undo_->previous_owners.size()&&receipt.before==profile_undo_->before&&receipt.bindings==profile_undo_->bindings&&receipt.requests.size()==profile_undo_->requests.size()&&same_identity(receipt.identity,profile_undo_->identity)&&same_target(receipt.target,profile_undo_->target);
    if(current_receipt)for(size_t i=0;i<receipt.requests.size();++i){const auto& a=receipt.requests[i];const auto& b=profile_undo_->requests[i];if(a.policy!=b.policy||a.field!=b.field||a.raw!=b.raw||a.type!=b.type||a.channel!=b.channel||a.unit!=b.unit)current_receipt=false;}
    try{require(receipt.ok&&!receipt.requests.empty()&&receipt.requests.size()<=64&&receipt.before.size()==receipt.requests.size()&&receipt.bindings.size()==receipt.requests.size()&&receipt.previous_owners.size()==receipt.requests.size(),"Invalid selected integral receipt");require(same_identity(receipt.identity,identity_)&&target_&&same_target(receipt.target,*target_),"Integral receipt adapter changed");
        Snapshot fresh;if(!capture(fresh))throw std::runtime_error(data_.message);publish(fresh);std::set<std::pair<uint32_t,size_t>> seen;
        for(size_t i=0;i<receipt.requests.size();++i){const auto& r=receipt.requests[i];require(r.policy<32&&(r.field==PowerIntegralField::lower||r.field==PowerIntegralField::upper)&&r.raw<=65535&&seen.insert({r.policy,field_index(r.field)}).second,"Invalid duplicate integral recovery field");const auto& row=selected_row(data_,r.policy);require(row.binding==receipt.bindings[i]&&row.type==r.type&&row.channel==r.channel&&row.unit==r.unit,"Original integral policy changed");const auto actual=current_value(row,r.field);require(actual==r.raw||actual==receipt.before[i],"Selected integral field was replaced; foreign request left untouched");}
        // Validate the complete restored range before the first recovery SET.
        auto restoration=receipt.requests;for(size_t i=0;i<restoration.size();++i){restoration[i].raw=receipt.before[i];restoration[i].expected.reset();}make_plan(data_,restoration);
        for(const auto i:safe_order(receipt,data_,true)){Snapshot current;if(!capture(current))throw std::runtime_error(data_.message);publish(current);for(size_t j=0;j<receipt.requests.size();++j){const auto& r=receipt.requests[j];const auto& row=selected_row(data_,r.policy);require(row.binding==receipt.bindings[j],"Integral recovery binding changed");const auto actual=current_value(row,r.field);require(actual==r.raw||actual==receipt.before[j],"Integral recovery selected field replaced");}
            const auto& r=receipt.requests[i];const auto& row=selected_row(data_,r.policy);if(current_value(row,r.field)==receipt.before[i])continue;require(valid_value(row,r.field,receipt.before[i]),"Recovery conflicts with current opposite bound");auto& owner=owned_[r.policy][field_index(r.field)];owner.pending=true;owner.value.recovery=true;Snapshot after;if(!dispatch(current,r.policy,r.field,receipt.before[i],after,[&]{launched=true;result.may_have_changed=true;profile_undo_=receipt;data_.profile_undo_available=data_.profile_recovery_pending=true;}))throw std::runtime_error(data_.message);publish(after);}
        Snapshot final;if(!capture(final))throw std::runtime_error(data_.message);publish(final);for(size_t i=0;i<receipt.requests.size();++i){const auto& r=receipt.requests[i];const auto& row=selected_row(data_,r.policy);require(row.binding==receipt.bindings[i]&&current_value(row,r.field)==receipt.before[i],"Final integral recovery differs; receipt retained");}
        for(size_t i=0;i<receipt.requests.size();++i){const auto& r=receipt.requests[i];auto& owner=owned_[r.policy][field_index(r.field)];owner={};owner.value=receipt.previous_owners[i];if(owner.value.owned)owner.info_row=signature(final.info,r.policy);}profile_undo_.reset();data_.profile_undo_available=data_.profile_recovery_pending=false;publish(final);result.ok=true;result.rollback_verified=true;result.message="Original selected integral requests restored and verified";
    }catch(const std::exception& e){if(launched||current_receipt){if(launched)profile_undo_=receipt;data_.profile_undo_available=data_.profile_recovery_pending=true;}result.message=std::string(e.what())+(launched||current_receipt?"; grouped recovery retained":"; existing Undo preserved");}
    data_.message=result.message;return result;
}
PowerIntegralResult PowerIntegralService::restore(const PowerIntegralPlan& receipt){Guard guard(api_,gpu_);if(!guard.held)return {false,false,false,false,"GPU lock unavailable"};return restore_profile_locked(receipt);}
PowerIntegralResult PowerIntegralService::undo_profile(){if(!profile_undo_)return {false,false,false,false,"No integral profile Undo"};const auto receipt=*profile_undo_;return restore(receipt);}
PowerIntegralResult PowerIntegralService::recover_profile(){return undo_profile();}

ProfileJson capture_power_integral_profile(const PowerIntegralData& data,const std::vector<PowerIntegralKey>& selection){
    require(data.current&&data.target&&text_ok(data.identity.uuid)&&data.identity.uuid[0]&&text_ok(data.identity.slot),"Current UUID-bound integral capture required");require(!selection.empty()&&selection.size()<=64,"Select integral profile fields");ProfileJson entries=ProfileJson::array();std::set<std::pair<uint32_t,size_t>> seen;
    for(const auto& key:selection){require(key.policy<32&&(key.field==PowerIntegralField::lower||key.field==PowerIntegralField::upper)&&seen.insert({key.policy,field_index(key.field)}).second,"Invalid duplicate integral selection");const auto& row=selected_row(data,key.policy);require(!row.undo[field_index(key.field)].recovery,"Uncertain integral field cannot be saved");entries.push_back({{"policy",key.policy},{"field",int(key.field)},{"raw",current_value(row,key.field)},{"type",row.type},{"channel",row.channel},{"unit",row.unit},{"binding",row.binding}});}
    const auto& target=*data.target;return {{"format","druta-native-power-integral"},{"version",1},{"target",{{"uuid",data.identity.uuid},{"slot",data.identity.slot},{"architecture",target.architecture},{"device_id",target.device_id},{"subsystem_id",target.subsystem_id}}},{"fields",entries}};
}
std::vector<PowerIntegralRequest> stage_power_integral_profile(const ProfileJson& document,const PowerIntegralData& data,bool validate_ranges){
    require(data.current&&data.target.has_value(),"Read current integral policies before staging");std::vector<PowerIntegralRequest> requests;
    const auto add=[&](const ProfileJson& e,PowerIntegralField field,uint32_t raw,const std::string& binding){PowerIntegralRequest r;r.policy=static_cast<uint32_t>(profile_integer(e.at("policy"),31));r.field=field;r.raw=raw;r.type=static_cast<uint32_t>(profile_integer(e.at("type"),255));r.channel=static_cast<uint32_t>(profile_integer(e.at("channel"),255));if(e.at("unit").is_string()){require(e["unit"]=="mW"||e["unit"]=="mA","Unknown integral unit");r.unit=e["unit"]=="mW"?0u:1u;}else r.unit=static_cast<uint32_t>(profile_integer(e.at("unit"),1));r.expected=current_value(selected_row(data,r.policy),field);r.binding=binding;requests.push_back(std::move(r));};
    if(document.contains("format")){
        profile_keys(document,{"format","version","target","fields"});require(document["format"]=="druta-native-power-integral"&&profile_integer(document["version"],1)==1,"Unsupported integral profile");const auto& t=document["target"];profile_keys(t,{"uuid","slot","architecture","device_id","subsystem_id"});require(text_ok(data.identity.uuid)&&data.identity.uuid[0]&&t["uuid"]==data.identity.uuid&&t["slot"]==data.identity.slot&&profile_integer(t["architecture"],UINT32_MAX)==data.target->architecture&&profile_integer(t["device_id"],UINT32_MAX)==data.target->device_id&&profile_integer(t["subsystem_id"],UINT32_MAX)==data.target->subsystem_id,"Integral profile target changed");require(document["fields"].is_array()&&document["fields"].size()<=64,"Invalid integral fields");for(const auto& e:document["fields"]){profile_keys(e,{"policy","field","raw","type","channel","unit","binding"});require(e["binding"].is_string(),"Invalid integral binding");add(e,PowerIntegralField(profile_integer(e["field"],1)),static_cast<uint32_t>(profile_integer(e["raw"])),e["binding"].get<std::string>());}
    }else{
        profile_keys(document,{"schema","scope","saved_at","saved_ts","device","incomplete","power_integral"});require(profile_integer(document["schema"],2)==2&&document["scope"]=="power_integral"&&document["saved_at"].is_string()&&!document["saved_at"].get<std::string>().empty()&&document["saved_ts"].is_number()&&!document["saved_ts"].is_boolean()&&std::isfinite(document["saved_ts"].get<double>())&&document["incomplete"].is_array()&&document["incomplete"].empty(),"Invalid or incomplete Python integral metadata");const auto& device=document["device"];profile_keys(device,{"name","vbios","driver","uuid","slot"});for(const auto name:{"name","vbios","driver","uuid","slot"})require(device[name].is_null()||device[name].is_string(),"Invalid Python device metadata");require(device["slot"]==data.identity.slot&&(device["uuid"].is_null()||device["uuid"]==""||device["uuid"]==data.identity.uuid),"Python integral adapter changed");const auto& p=document["power_integral"];profile_keys(p,{"identity","policies"});const auto& t=p["identity"];profile_keys(t,{"slot","architecture","device_id","subsystem_id"});require(t["slot"]==device["slot"]&&profile_integer(t["architecture"],UINT32_MAX)==data.target->architecture,"Python integral architecture/slot changed");require((t["device_id"].is_null()||profile_integer(t["device_id"],65535)==data.target->device_id)&&(t["subsystem_id"].is_null()||profile_integer(t["subsystem_id"],UINT32_MAX)==data.target->subsystem_id),"Python integral PCI identity changed");require(p["policies"].is_array()&&!p["policies"].empty()&&p["policies"].size()<=32,"Invalid Python integral policies");std::set<int64_t> seen;for(const auto& e:p["policies"]){profile_keys(e,{"policy","type","channel","unit","values"});require(seen.insert(profile_integer(e["policy"],31)).second,"Duplicate Python integral policy");require(e["unit"].is_string(),"Invalid Python integral unit");const auto& v=e["values"];require(v.is_object()&&!v.empty()&&v.size()<=2,"Invalid Python integral values");for(auto i=v.begin();i!=v.end();++i){require(i.key()=="lower_raw"||i.key()=="upper_raw","Unknown integral field");add(e,i.key()=="lower_raw"?PowerIntegralField::lower:PowerIntegralField::upper,static_cast<uint32_t>(profile_integer(i.value())),{});}}}
    for(const auto& lower:requests)if(lower.field==PowerIntegralField::lower)for(const auto& upper:requests)if(upper.policy==lower.policy&&upper.field==PowerIntegralField::upper)require(upper.raw&&lower.raw<=upper.raw,"Saved integral bounds are inverted");
    for(const auto& r:requests)require(r.field!=PowerIntegralField::upper||r.raw>0,"Upper integral bound must be positive");
    (void)make_plan(data,requests,validate_ranges);return requests;
}
}
