// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_integral_service.h"
#include <algorithm>
#include <cmath>
#include <cstring>
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
        && a.slot[0] && a.uuid[0] && std::strcmp(a.slot,b.slot)==0 && std::strcmp(a.uuid,b.uuid)==0
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
    row.available=row.reason.empty();return row;
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
    data_.rows.clear();data_.target=target_;data_.current=true;
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
bool PowerIntegralService::dispatch(const Snapshot& state,uint32_t policy,PowerIntegralField field,uint16_t raw,Snapshot& after) {
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
    const auto old=owner;
    if(!owner.value.owned){owner.value.initial=current;owner.info_row=signature(before.info,policy);}
    owner.value.owned=true;owner.value.previous=current;owner.value.requested=static_cast<uint16_t>(raw);
    owner.value.can_undo=true;owner.value.confirmed=false;owner.value.recovery=true;owner.value.conflict=false;owner.pending=true;
    result.may_have_changed=true;Snapshot after;
    if(dispatch(before,policy,field,static_cast<uint16_t>(raw),after)) {
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
    Guard guard(api_,gpu_);if(!guard.held)return {false,false,false,false,"GPU lock unavailable"};return restore_locked(policy,field,false);
}
PowerIntegralResult PowerIntegralService::undo(uint32_t policy,PowerIntegralField field) {
    Guard guard(api_,gpu_);if(!guard.held)return {false,false,false,false,"GPU lock unavailable"};return restore_locked(policy,field,true);
}
}
