// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "frequency_lock.h"
#include <algorithm>
#include <cstring>
#include <limits>
namespace druta::ui {
namespace {
struct Guard {
    FrequencyLockApi& api;dc_gpu* gpu;bool held;
    Guard(FrequencyLockApi& a,dc_gpu* g):api(a),gpu(g),held(g && a.lock && a.unlock && a.lock(g)==DC_OK){}
    ~Guard(){if(held)api.unlock(gpu);}
};
template<size_t N>bool terminated(const char(&text)[N]) {return std::memchr(text,0,N)!=nullptr;}
bool same(const dc_gpu_identity& a,const dc_gpu_identity& b) {
    return terminated(a.slot) && terminated(b.slot) && terminated(a.uuid) && terminated(b.uuid)
        && terminated(a.pairing_error) && terminated(b.pairing_error) && a.slot[0] && (b.nvapi_ok || b.nvml_ok)
        && !a.pairing_error[0] && !b.pairing_error[0] && std::strcmp(a.slot,b.slot)==0 && std::strcmp(a.uuid,b.uuid)==0
        && a.nvapi_selected==b.nvapi_selected && a.nvml_selected==b.nvml_selected && a.nvapi_handle==b.nvapi_handle
        && a.nvapi_devid==b.nvapi_devid && a.nvapi_subsys==b.nvapi_subsys && a.nvml_has_devid==b.nvml_has_devid
        && a.nvml_devid==b.nvml_devid && a.nvml_subsys==b.nvml_subsys;
}
bool range_valid(uint32_t low,uint32_t high) {return low>0 && low<=high;}
}
FrequencyLock::FrequencyLock(FrequencyLockApi api,dc_gpu* gpu,const dc_gpu_identity& target):api_(api),gpu_(gpu),target_(target){}
bool FrequencyLock::identity_ok() {
    if(api_.identity)for(int attempt=0;attempt<3;++attempt){
        dc_gpu_identity id{};id.size=sizeof(id);
        if(api_.identity(gpu_,&id)!=DC_OK)continue;
        if(same(target_,id))return true;
        break; // A successful response for a different target is not transient.
    }
    data_.tables_current=false;data_.current_known=false;data_.legacy_known=false;
    if(needs_cleanup()){data_.ownership.confirmed=false;data_.ownership.recovery=true;}
    return false;
}
bool FrequencyLock::read_clock(dc_clk_lock_status& status) {
    if(!api_.clock_status)return false;
    for(int attempt=0;attempt<3;++attempt){status={};if(api_.clock_status(gpu_,&status)==DC_OK && terminated(status.error)
        && !status.error[0] && (status.locked==0 || (status.locked==1 && range_valid(status.min_mhz,status.max_mhz))))return true;}
    return false;
}
bool FrequencyLock::read_legacy_owned(int32_t& owned) {
    if(!api_.legacy_owned)return false;
    for(int n=0;n<3;++n)if(api_.legacy_owned(gpu_,&owned)==DC_OK && (owned==0 || owned==1))return true;
    return false;
}
bool FrequencyLock::learn_accepted(FrequencyRange& accepted) {
    if(!api_.last_lock)return false;
    for(int n=0;n<3;++n) {
        uint32_t low=0,high=0;
        if(api_.last_lock(gpu_,&low,&high)!=DC_OK || !range_valid(low,high))continue;
        accepted={low,high};
        if(pending_dispatch_) {
            // This metadata is the ABI handle's own accepted SET tuple, not
            // arbitrary current device state. Track it for cleanup even when
            // a fresh driver table resnapped it outside the old UI bounds.
            if(std::find(candidates_.begin(),candidates_.end(),accepted)==candidates_.end())candidates_.push_back(accepted);
            unexpected_acceptance_=dispatched_ && !(accepted==*dispatched_);
            pending_dispatch_=false;
        }
        return true;
    }
    return false;
}
bool FrequencyLock::discover() {
    data_.tables_current=false;data_.nvml_capable=false;data_.legacy_known=false;
    if(api_.legacy_supported)for(int n=0;n<3;++n){int32_t yes=0;if(api_.legacy_supported(gpu_,&yes)==DC_OK && (yes==0 || yes==1)){
        data_.legacy_known=true;data_.legacy_available=yes!=0;break;}}
    bool arch_read=false;uint32_t arch=0;
    if(api_.architecture)for(int n=0;n<3;++n)if(api_.architecture(gpu_,&arch)==DC_OK && arch){data_.architecture=arch;arch_read=true;break;}
    // NVML documents Set/ResetGpuLockedClocks for Volta and newer. This is an
    // architectural gate; actual exports, tables and request readback qualify it.
    if(!arch_read){data_.message="GPU architecture unreadable; Retry capabilities. Captured tables are retained.";return false;}
    if(arch<5){data_.message="NVML frequency locking requires Volta or newer. Legacy P0 is probed independently.";return false;}
    if(!api_.export_address || !api_.capabilities || !api_.lockable_clocks || !api_.clock_status
        || !api_.clocks_lock || !api_.last_lock || !api_.clocks_reset){data_.message="Frequency-lock capability interfaces unavailable";return false;}
    void* setter=nullptr;void* resetter=nullptr;
    if(api_.export_address("nvml","nvmlDeviceSetGpuLockedClocks",&setter)!=DC_OK || !setter
        || api_.export_address("nvml","nvmlDeviceResetGpuLockedClocks",&resetter)!=DC_OK || !resetter){
        data_.message="NVML frequency-lock Set/Reset exports unavailable";return false;}
    dc_capabilities caps{};bool caps_read=false;
    for(int n=0;n<3;++n){caps={};caps.size=sizeof(caps);if(api_.capabilities(gpu_,&caps)==DC_OK
        && (caps.present&(DC_C_GFX_MIN|DC_C_GFX_MAX))==(DC_C_GFX_MIN|DC_C_GFX_MAX)
        && range_valid(caps.gfx_min,caps.gfx_max)){caps_read=true;break;}}
    if(!caps_read){data_.message="Lockable bounds unreadable; captured tables retained. Retry capabilities.";return false;}
    std::vector<FrequencyClockRow> rows;
    for(int attempt=0;attempt<3 && rows.empty();++attempt) {
        size_t count=0;if(api_.lockable_clocks(gpu_,nullptr,0,&count)!=DC_OK || !count || count>4096)continue;
        std::vector<dc_clock_row> raw(count);size_t used=count;
        if(api_.lockable_clocks(gpu_,raw.data(),raw.size(),&used)!=DC_OK || used!=raw.size())continue;
        bool valid=true;std::vector<FrequencyClockRow> candidate;
        for(const auto& row:raw) {
            if(!row.mem_mhz || !row.count || row.count>512 || (!candidate.empty() && row.mem_mhz<=candidate.back().memory_mhz)){valid=false;break;}
            FrequencyClockRow normalized;normalized.memory_mhz=row.mem_mhz;
            normalized.graphics_mhz.assign(row.graphics,row.graphics+row.count);
            if(std::any_of(normalized.graphics_mhz.begin(),normalized.graphics_mhz.end(),[](uint32_t v){return v==0;})){valid=false;break;}
            std::sort(normalized.graphics_mhz.begin(),normalized.graphics_mhz.end());
            normalized.graphics_mhz.erase(std::unique(normalized.graphics_mhz.begin(),normalized.graphics_mhz.end()),normalized.graphics_mhz.end());
            candidate.push_back(std::move(normalized));
        }
        if(valid && !candidate.empty())rows=std::move(candidate);
    }
    if(rows.empty()){data_.message="Lockable-clock table geometry/read unavailable; Retry capabilities. Captured rows retained.";return false;}
    // The ABI setter snaps against rows.back(), the top memory clock. The
    // corresponding cached cap must agree; other memory rows can differ.
    if(rows.back().graphics_mhz.front()!=caps.gfx_min || rows.back().graphics_mhz.back()!=caps.gfx_max){
        data_.message="Top-memory lockable table and capability bounds disagree; Retry capabilities.";return false;}
    data_.rows=std::move(rows);data_.minimum_mhz=caps.gfx_min;data_.maximum_mhz=caps.gfx_max;
    data_.tables_current=true;data_.nvml_capable=true;data_.message="Current-adapter lockable tables read; these are not the V/F curve's boost ceiling.";return true;
}
void FrequencyLock::observe_clock(const dc_clk_lock_status& status) {
    data_.current_known=true;data_.current=status.locked?std::optional<FrequencyRange>{{status.min_mhz,status.max_mhz}}:std::nullopt;
    if(data_.ownership.mode!=FrequencyLockMode::nvml)return;
    if(!data_.current){data_.ownership={};candidates_.clear();pending_dispatch_=unexpected_acceptance_=false;dispatched_.reset();return;}
    const bool tracked=std::find(candidates_.begin(),candidates_.end(),*data_.current)!=candidates_.end();
    if(!tracked){
        if(pending_dispatch_){data_.ownership.confirmed=false;data_.ownership.recovery=true;
            data_.message="Pending SET has an unrecognized readback; accepted metadata or an exact tracked tuple is required before reset.";return;}
        data_.ownership={};candidates_.clear();unexpected_acceptance_=false;dispatched_.reset();
        data_.message="Tracked frequency request was replaced; foreign request left untouched.";return;
    }
    data_.ownership.range=data_.current;
}
FrequencyLockData FrequencyLock::read() {
    Guard guard(api_,gpu_);
    if(!guard.held || !identity_ok()) {
        data_.tables_current=false;data_.current_known=false;data_.legacy_known=false;
        if(needs_cleanup()){data_.ownership.confirmed=false;data_.ownership.recovery=true;}
        data_.message="GPU identity or handle lock unreadable/changed; no frequency operation issued.";return data_;
    }
    discover();FrequencyRange accepted{};const bool accepted_read=learn_accepted(accepted);dc_clk_lock_status status{};
    if(read_clock(status)) {
        observe_clock(status);
        if(data_.ownership.mode==FrequencyLockMode::nvml && data_.current) {
            data_.ownership.confirmed=false;data_.ownership.recovery=true;
            if(accepted_read && accepted==*data_.current && !pending_dispatch_ && !unexpected_acceptance_){
                data_.ownership.confirmed=true;data_.ownership.recovery=false;candidates_={*data_.current};
            }
        }
    }
    else {data_.current_known=false;if(data_.ownership.mode==FrequencyLockMode::nvml){data_.ownership.confirmed=false;data_.ownership.recovery=true;}}
    if(data_.ownership.mode==FrequencyLockMode::legacy_p0) {
        int32_t owned=0;
        if(!read_legacy_owned(owned)){
            data_.ownership.confirmed=false;data_.ownership.recovery=true;data_.message="Owned legacy P0 status unreadable; Release remains required.";
        } else if(!owned)data_.ownership={};
    }
    return data_;
}
FrequencyLockResult FrequencyLock::apply(int64_t minimum,int64_t maximum) {
    FrequencyLockResult out;Guard guard(api_,gpu_);
    if(!guard.held || !identity_ok()){out.message="GPU identity or handle lock unavailable";return out;}
    if(!discover() || !data_.nvml_capable){out.message=data_.message;return out;}
    if(minimum<=0 || minimum>maximum || maximum>std::numeric_limits<uint32_t>::max()
        || minimum<*data_.minimum_mhz || maximum>*data_.maximum_mhz){out.message="Requested range is outside this adapter's lockable bounds";return out;}
    if(data_.ownership.mode==FrequencyLockMode::legacy_p0){auto released=release_locked();if(!released.ok)return released;out.may_have_changed=released.may_have_changed;}
    dc_clk_lock_status before{};
    if(!read_clock(before)){out.message="Current frequency request unreadable; no SET issued";return out;}
    FrequencyRange earlier{};learn_accepted(earlier);
    observe_clock(before);
    if(pending_dispatch_ || unexpected_acceptance_){out.message="Resolve/release the prior unqualified frequency SET before another request";return out;}
    if(before.locked && data_.ownership.mode!=FrequencyLockMode::nvml){out.message="Another frequency request is present; it was left untouched";return out;}
    if(data_.legacy_known && data_.legacy_available && api_.legacy_owned) {
        int32_t legacy=0;if(!read_legacy_owned(legacy) || legacy){out.message="Untracked or unreadable legacy P0 ownership; complete handover first";return out;}
    }
    const auto& table=data_.rows.back().graphics_mhz;
    const auto snap=[&](int64_t requested){return *std::prev(std::upper_bound(table.begin(),table.end(),static_cast<uint32_t>(requested)));};
    const FrequencyRange wanted{snap(minimum),snap(maximum)};
    if(!identity_ok()){out.message="GPU identity changed during preflight; no frequency SET issued";return out;}
    if(data_.ownership.mode!=FrequencyLockMode::nvml)candidates_.clear();
    if(std::find(candidates_.begin(),candidates_.end(),wanted)==candidates_.end())candidates_.push_back(wanted);
    pending_dispatch_=true;unexpected_acceptance_=false;dispatched_=wanted;
    data_.ownership.mode=FrequencyLockMode::nvml;data_.ownership.range=wanted;data_.ownership.confirmed=false;data_.ownership.recovery=true;
    dc_outcome result{};dc_status status=DC_E_FAILED;out.may_have_changed=true;
    try {status=api_.clocks_lock(gpu_,wanted.minimum,wanted.maximum,&result);}catch(...){/* retain possible ownership */}
    FrequencyRange accepted_range{};const bool accepted=identity_ok() && learn_accepted(accepted_range);
    dc_clk_lock_status after{};const bool got=identity_ok() && read_clock(after);
    if(got)observe_clock(after);else data_.current_known=false;
    const bool exact=got && after.locked && FrequencyRange{after.min_mhz,after.max_mhz}==wanted;
    if(status==DC_OK && result.ok && accepted && accepted_range==wanted && exact){
        data_.ownership={FrequencyLockMode::nvml,true,false,wanted};candidates_={wanted};out.ok=true;
        out.message="Frequency request confirmed ["+std::to_string(wanted.minimum)+".."+std::to_string(wanted.maximum)+"] MHz (snapped down onto the top-memory table).";
    } else if(accepted && !(accepted_range==wanted))out.message="Frequency SET accepted unexpected snapped range ["+
        std::to_string(accepted_range.minimum)+".."+std::to_string(accepted_range.maximum)+"] MHz; owned recovery retained. Release before retrying.";
    else out.message="Frequency SET was not fully confirmed; readback/owned Release or recovery is required. "+std::string(terminated(result.message)?result.message:"");
    data_.message=out.message;return out;
}
FrequencyLockResult FrequencyLock::lock_max() {
    const auto current=read();if(!current.tables_current || !current.maximum_mhz)return {false,false,current.message};
    return apply(*current.maximum_mhz,*current.maximum_mhz);
}
FrequencyLockResult FrequencyLock::hold_legacy_p0() {
    FrequencyLockResult out;Guard guard(api_,gpu_);
    if(!guard.held || !identity_ok()){out.message="GPU identity or handle lock unavailable";return out;}
    discover();
    if(!data_.legacy_known || !data_.legacy_available || !api_.legacy_owned || !api_.legacy_hold || !api_.legacy_release){out.message="Legacy P0 capability unavailable or unreadable";return out;}
    if(data_.ownership.mode==FrequencyLockMode::nvml){auto r=release_locked();if(!r.ok)return r;out.may_have_changed=r.may_have_changed;}
    int32_t owned=0;if(!read_legacy_owned(owned)){out.message="Legacy P0 ownership unreadable";return out;}
    if(owned && data_.ownership.mode!=FrequencyLockMode::legacy_p0){out.message="Untracked legacy P0 request; complete handover before acquiring";return out;}
    if(!identity_ok()){out.message="GPU identity changed during P0 preflight; no request issued";return out;}
    data_.ownership={FrequencyLockMode::legacy_p0,false,true,{}};
    dc_outcome result{};dc_status status=DC_E_FAILED;out.may_have_changed=true;
    try {status=api_.legacy_hold(gpu_,&result);}catch(...){/* keep uncertain owned request visible */}
    const bool read=identity_ok() && read_legacy_owned(owned);
    if(read && !owned)data_.ownership={};
    else if(read && status==DC_OK && result.ok){data_.ownership.confirmed=true;data_.ownership.recovery=false;out.ok=true;}
    out.message=out.ok?"Legacy P0 and top memory band verified; this is not a maximum-core clock lock.":"Legacy P0 was not confirmed; inspect/release its retained session ownership.";
    data_.message=out.message;return out;
}
FrequencyLockResult FrequencyLock::release_locked() {
    FrequencyLockResult out;
    if(!identity_ok()){if(needs_cleanup()){data_.ownership.confirmed=false;data_.ownership.recovery=true;}out.message="GPU identity unreadable/changed; release blocked";return out;}
    if(data_.ownership.mode==FrequencyLockMode::none){out.ok=true;out.message="No owned frequency/P0 request";return out;}
    if(data_.ownership.mode==FrequencyLockMode::legacy_p0) {
        int32_t owned=0;if(!api_.legacy_release || !read_legacy_owned(owned)){
            data_.ownership.confirmed=false;data_.ownership.recovery=true;out.message="Owned P0 status unreadable; no release issued";return out;}
        if(!owned){data_.ownership={};out.ok=true;out.message="Owned P0 request is already absent";return out;}
        if(!identity_ok()){data_.ownership.confirmed=false;data_.ownership.recovery=true;out.message="GPU changed during P0 release preflight";return out;}
        dc_outcome result{};out.may_have_changed=true;dc_status status=DC_E_FAILED;
        try {status=api_.legacy_release(gpu_,&result);}catch(...){}
        if(identity_ok() && read_legacy_owned(owned) && owned==0){data_.ownership={};out.ok=status==DC_OK && result.ok;out.message="Legacy P0 ownership released; fan policy is unchanged";}
        else {data_.ownership.confirmed=false;data_.ownership.recovery=true;out.message="P0 release unconfirmed; owned recovery retained";}
        return out;
    }
    dc_clk_lock_status before{};
    if(!api_.clocks_reset || !read_clock(before)){data_.current_known=false;data_.ownership.confirmed=false;data_.ownership.recovery=true;out.message="Frequency status unreadable; no reset issued";return out;}
    FrequencyRange accepted{};learn_accepted(accepted);
    observe_clock(before);
    if(data_.ownership.mode==FrequencyLockMode::none){out.ok=true;out.message=before.locked?"Foreign replacement left untouched; tracked request is absent":"Owned frequency request already absent";return out;}
    if(data_.current && std::find(candidates_.begin(),candidates_.end(),*data_.current)==candidates_.end()){
        data_.ownership.confirmed=false;data_.ownership.recovery=true;
        out.message="Pending SET readback is not an exact tracked request; no blind reset issued. Retry metadata or use the driver reset action.";return out;
    }
    if(!identity_ok()){data_.ownership.confirmed=false;data_.ownership.recovery=true;out.message="GPU changed during release preflight; no reset issued";return out;}
    dc_outcome result{};dc_status status=DC_E_FAILED;out.may_have_changed=true;
    try {status=api_.clocks_reset(gpu_,0,&result);}catch(...){}
    dc_clk_lock_status after{};
    const bool read_after=identity_ok() && read_clock(after);
    if(read_after) {
        observe_clock(after);
        if(!after.locked || data_.ownership.mode==FrequencyLockMode::none){data_.ownership={};candidates_.clear();pending_dispatch_=unexpected_acceptance_=false;dispatched_.reset();out.ok=status==DC_OK && result.ok;
            out.message=after.locked?"Tracked frequency lock released; a different request is now present":"Owned frequency lock release read back unlocked";return out;}
    }
    if(!read_after)data_.current_known=false;
    data_.ownership.confirmed=false;data_.ownership.recovery=true;out.message="Frequency release unconfirmed; exact tracked ownership retained for recovery";return out;
}
FrequencyLockResult FrequencyLock::release() {
    Guard guard(api_,gpu_);if(!guard.held)return {false,false,"GPU lock unavailable"};
    auto out=release_locked();data_.message=out.message;return out;
}
}
