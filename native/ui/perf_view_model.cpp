/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "perf_view_model.h"
#include <algorithm>
#include <cstring>
#include <utility>
namespace druta::ui {
namespace {
bool terminated(const char* s,size_t n) {return std::memchr(s,0,n)!=nullptr;}
bool valid_target(const dc_gpu_identity& id) {
    return terminated(id.slot,sizeof(id.slot)) && id.slot[0] && terminated(id.uuid,sizeof(id.uuid)) && id.uuid[0]
        && terminated(id.pairing_error,sizeof(id.pairing_error)) && !id.pairing_error[0] && (id.nvapi_ok||id.nvml_ok);
}
bool same_target(const dc_gpu_identity& a,const dc_gpu_identity& b) {
    return valid_target(a)&&valid_target(b)&&std::strcmp(a.slot,b.slot)==0&&std::strcmp(a.uuid,b.uuid)==0
        && a.nvapi_selected==b.nvapi_selected && a.nvml_selected==b.nvml_selected
        && a.nvapi_handle==b.nvapi_handle && a.nvapi_devid==b.nvapi_devid && a.nvapi_subsys==b.nvapi_subsys
        && a.nvml_has_devid==b.nvml_has_devid && a.nvml_devid==b.nvml_devid && a.nvml_subsys==b.nvml_subsys;
}
enum class Identity { same, unavailable, mismatch };
Identity identity(const PerfViewApi& api,dc_gpu* gpu,const dc_gpu_identity& target) {
    if(!api.identity || !gpu || !valid_target(target)) return Identity::unavailable;
    for(int i=0;i<2;++i) {
        dc_gpu_identity id{}; id.size=sizeof(id);
        if(api.identity(gpu,&id)!=DC_OK) continue;
        if(!valid_target(id)) {
            if(terminated(id.slot,sizeof(id.slot)) && id.slot[0] && std::strcmp(id.slot,target.slot)!=0) return Identity::mismatch;
            if(terminated(id.uuid,sizeof(id.uuid)) && id.uuid[0] && std::strcmp(id.uuid,target.uuid)!=0) return Identity::mismatch;
            if(terminated(id.pairing_error,sizeof(id.pairing_error)) && id.pairing_error[0]) return Identity::mismatch;
            continue;
        }
        return same_target(target,id)?Identity::same:Identity::mismatch;
    }
    return Identity::unavailable;
}
bool valid_snapshot(const dc_perf_snapshot& s,const std::string& name) {
    if(s.size!=sizeof(s) || !terminated(s.group,sizeof(s.group)) || s.group!=name ||
       s.capacity>DC_PERF_MAX_ENTRIES || s.mask_bytes>DC_PERF_MAX_MASK_BYTES || s.entry_count>s.capacity ||
       s.entry_count>DC_PERF_MAX_ENTRIES || s.flag_count>DC_PERF_MAX_FLAGS || s.target_count>DC_PERF_TARGET_COUNT ||
       s.issue_count>DC_PERF_MAX_FLAGS || !terminated(s.info_sha256,sizeof(s.info_sha256)) ||
       !terminated(s.control_sha256,sizeof(s.control_sha256))) return false;
    for(uint32_t i=0;i<s.issue_count;++i)if(!terminated(s.issues[i],sizeof(s.issues[i])))return false;
    for(uint32_t i=0;i<s.flag_count;++i)if(!terminated(s.flags[i].label,sizeof(s.flags[i].label)))return false;
    for(uint32_t i=0;i<s.target_count;++i)if(!terminated(s.targets[i].field,sizeof(s.targets[i].field)) || !terminated(s.targets[i].issue,sizeof(s.targets[i].issue)))return false;
    for(uint32_t i=0;i<s.entry_count;++i) {
        const auto& e=s.entries[i];
        if(e.index>=s.capacity || e.info_bytes>DC_PERF_MAX_INFO_STRIDE || e.control_bytes>DC_PERF_MAX_CONTROL_STRIDE ||
           e.issue_count>1 || !terminated(e.driver_label,sizeof(e.driver_label)) || !terminated(e.issue,sizeof(e.issue)))return false;
        for(uint32_t j=0;j<i;++j)if(s.entries[j].index==e.index)return false;
    }
    return true;
}
void unavailable(PerfGroupView& group,int32_t kind,const std::string& text,bool clear=false) {
    if(clear)group.snapshot.reset();
    group.freshness=group.snapshot?PerfFreshness::stale:PerfFreshness::unavailable;
    group.error_kind=kind; group.message=text;
}
}
PerfViewService::PerfViewService(PerfViewApi api,dc_gpu* gpu,const dc_gpu_identity& target):api_(api),gpu_(gpu),target_(target) {
    const char* names[]={"limits","cf_controller","cf_policy"};
    for(size_t i=0;i<model_.groups.size();++i)model_.groups[i].name=names[i];
}
const PerfViewModel& PerfViewService::refresh() {
    const auto before=identity(api_,gpu_,target_);
    if(before!=Identity::same) {
        for(auto& g:model_.groups)unavailable(g,before==Identity::mismatch?DC_ERR_IDENTITY:DC_ERR_TRANSIENT,
            before==Identity::mismatch?"GPU identity changed":"GPU identity unavailable",before==Identity::mismatch);
        return model_;
    }
    if(!api_.read_group) {for(auto& g:model_.groups)unavailable(g,DC_ERR_UNSUPPORTED,"PERF read export unavailable");return model_;}
    // Stage the entire refresh until the final target check. A target change
    // cannot publish data from an adapter other than the selected one.
    auto next=model_;
    for(auto& g:next.groups) {
        auto result=std::make_unique<dc_perf_group_result>(); result->size=sizeof(*result); result->snapshot.size=sizeof(result->snapshot);
        dc_error error{};error.size=sizeof(error);
        const auto status=api_.read_group(gpu_,g.name.c_str(),2,result.get(),&error);
        g.attempts=result->attempts;
        if(status!=DC_OK || result->status==DC_PERF_STATUS_FAILED) {
            const auto kind=error.kind?error.kind:DC_ERR_TRANSIENT;
            const std::string message=terminated(error.message,sizeof(error.message)) && error.message[0]?error.message:"PERF group read unavailable";
            if (kind==DC_ERR_IDENTITY) {
                for(auto& prior:model_.groups)unavailable(prior,DC_ERR_IDENTITY,"GPU identity changed during PERF read",true);
                return model_;
            }
            unavailable(g,kind,message); continue;
        }
        if(result->has_target && terminated(result->slot,sizeof(result->slot)) && std::strcmp(result->slot,target_.slot)!=0) {
            for(auto& prior:model_.groups)unavailable(prior,DC_ERR_IDENTITY,"PERF response target changed",true);
            return model_;
        }
        if((result->status!=DC_PERF_STATUS_READY && result->status!=DC_PERF_STATUS_EMPTY) || !result->has_target ||
           !terminated(result->slot,sizeof(result->slot)) ||
           !valid_snapshot(result->snapshot,g.name) || (result->status==DC_PERF_STATUS_EMPTY && result->snapshot.entry_count!=0) ||
           (result->status==DC_PERF_STATUS_READY && result->snapshot.entry_count==0)) {
            unavailable(g,DC_ERR_MALFORMED,"PERF group response or target is malformed");continue;
        }
        g.snapshot=std::make_shared<dc_perf_snapshot>(result->snapshot);g.architecture=result->architecture;
        g.freshness=PerfFreshness::current;g.error_kind=DC_ERR_NONE;
        g.message=result->status==DC_PERF_STATUS_EMPTY?"No active entries":"Read complete";
    }
    const auto after=identity(api_,gpu_,target_);
    if(after!=Identity::same) {
        for(auto& g:model_.groups)unavailable(g,after==Identity::mismatch?DC_ERR_IDENTITY:DC_ERR_TRANSIENT,
            after==Identity::mismatch?"GPU identity changed":"GPU identity unavailable after read",after==Identity::mismatch);
        return model_;
    }
    model_=std::move(next);return model_;
}
}
