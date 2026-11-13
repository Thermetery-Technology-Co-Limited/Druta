/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "power_arbitration_service.h"
#include <algorithm>
#include <cstring>
#include <vector>
namespace druta::ui {
namespace {
template<size_t N>bool terminated(const char (&s)[N]) {return std::memchr(s,0,N)!=nullptr;}
bool target_valid(const dc_gpu_identity& id) {
    return terminated(id.slot)&&id.slot[0]&&terminated(id.uuid)&&id.uuid[0]&&terminated(id.pairing_error)&&!id.pairing_error[0]&&(id.nvapi_ok||id.nvml_ok);
}
enum class Identity {same,unavailable,mismatch};
Identity identity(const PowerArbitrationApi& api,dc_gpu* gpu,const dc_gpu_identity& expected) {
    if(!api.identity||!gpu||!target_valid(expected))return Identity::unavailable;
    for(int i=0;i<2;++i) {
        dc_gpu_identity id{};id.size=sizeof(id);if(api.identity(gpu,&id)!=DC_OK)continue;
        if((terminated(id.slot)&&id.slot[0]&&std::strcmp(id.slot,expected.slot)) ||
           (terminated(id.uuid)&&id.uuid[0]&&std::strcmp(id.uuid,expected.uuid)) ||
           (terminated(id.pairing_error)&&id.pairing_error[0]))return Identity::mismatch;
        if(!target_valid(id))continue;
        return id.nvapi_selected==expected.nvapi_selected && id.nvml_selected==expected.nvml_selected &&
            id.nvapi_handle==expected.nvapi_handle && id.nvapi_devid==expected.nvapi_devid && id.nvapi_subsys==expected.nvapi_subsys &&
            id.nvml_has_devid==expected.nvml_has_devid && id.nvml_devid==expected.nvml_devid && id.nvml_subsys==expected.nvml_subsys
            ?Identity::same:Identity::mismatch;
    }return Identity::unavailable;
}
struct Lock { const PowerArbitrationApi& api;dc_gpu* gpu;bool held;
    Lock(const PowerArbitrationApi& a,dc_gpu* g):api(a),gpu(g),held(a.lock&&a.unlock&&g&&a.lock(g)==DC_OK){}
    ~Lock(){if(held)api.unlock(gpu);}
};
bool flag(int32_t value) {return value==0 || value==1;}
bool vector_valid(const dc_arbitration_vector& v) {
    return terminated(v.reason)&&terminated(v.method)&&flag(v.available)&&flag(v.has_computed_output)&&
        v.consistent>=-1&&v.consistent<=1&&v.count<=DC_ARBITRATION_INPUTS_MAX;
}
bool report_valid(const dc_arbitration_report& r) {
    if(r.size!=sizeof(r)||r.schema!=1||!terminated(r.reason)||!flag(r.available)||!flag(r.has_mask)||
       r.policy_count>DC_ARBITRATION_POLICIES_MAX || (r.available&&!r.has_mask) || (!r.available&&r.has_mask))return false;
    for(uint32_t i=0;i<r.policy_count;++i){const auto& row=r.policies[i];
        if(row.policy>=DC_ARBITRATION_POLICIES_MAX || (r.available && !(r.mask & (uint32_t{1}<<row.policy))) ||
           !flag(row.available)||!flag(row.has_unit)||!terminated(row.unit)||!terminated(row.reason)||row.observation_count>DC_ARBITRATION_OBSERVATIONS_MAX ||
           !vector_valid(row.host)||!vector_valid(row.pmu))return false;
        for(uint32_t j=0;j<row.observation_count;++j)if(!terminated(row.observations[j]))return false;
        for(uint32_t j=0;j<i;++j)if(r.policies[j].policy==row.policy)return false;
    }
    if(r.available){uint32_t count=0, mask=r.mask;while(mask){count+=mask&1u;mask>>=1;}if(count!=r.policy_count)return false;}
    return true;
}
void fail(PowerArbitrationModel& model,int32_t kind,const std::string& message,bool clear=false) {
    if(clear)model.report.reset();model.freshness=model.report?ArbitrationFreshness::stale:ArbitrationFreshness::unavailable;
    model.error_kind=kind;model.message=message;
}
}
PowerArbitrationService::PowerArbitrationService(PowerArbitrationApi api,dc_gpu* gpu,const dc_gpu_identity& target):api_(api),gpu_(gpu),target_(target){}
const PowerArbitrationModel& PowerArbitrationService::refresh() {
    model_.attempts=0;model_.status_observed=false;model_.status_present=false;
    if(!api_.identity||!api_.lock||!api_.unlock||!api_.max_words||!api_.policy_table||!api_.table_get||!api_.geometry||!api_.decode){
        fail(model_,DC_ERR_UNSUPPORTED,"Power arbitration read interfaces unavailable");return model_;}
    const uint32_t maximum=api_.max_words();
    if(!maximum){fail(model_,DC_ERR_MALFORMED,"Power packet capacity unavailable or invalid");return model_;}
    auto prepare_retry=[&]() {
        const auto target=identity(api_,gpu_,target_);
        if(target!=Identity::same) {
            fail(model_,target==Identity::mismatch?DC_ERR_IDENTITY:DC_ERR_TRANSIENT,
                target==Identity::mismatch?"GPU identity changed":"GPU identity unavailable before retry",target==Identity::mismatch);
            return false;
        }
        if(api_.reset_transport && api_.reset_transport(gpu_,DC_TRANSPORT_POWER)!=DC_OK) {
            fail(model_,DC_ERR_TRANSIENT,"Power transport cache could not be reset for retry");return false;
        }
        return true;
    };
    for(uint32_t attempt=1;attempt<=2;++attempt){
        model_.attempts=attempt;Lock lock(api_,gpu_);
        if(!lock.held){fail(model_,DC_ERR_TRANSIENT,"GPU lock unavailable");continue;}
        const auto before=identity(api_,gpu_,target_);
        if(before!=Identity::same){fail(model_,before==Identity::mismatch?DC_ERR_IDENTITY:DC_ERR_TRANSIENT,
            before==Identity::mismatch?"GPU identity changed":"GPU identity unavailable",before==Identity::mismatch);
            if(before==Identity::mismatch)break;continue;}
        dc_power_layout layout{};layout.size=sizeof(layout);dc_error error{};error.size=sizeof(error);
        if(api_.policy_table(gpu_,&layout,&error)!=DC_OK){
            const int32_t kind=error.kind?error.kind:DC_ERR_TRANSIENT;
            const std::string text=terminated(error.message)&&error.message[0]?error.message:"Power policy table unavailable";
            fail(model_,kind,text,kind==DC_ERR_IDENTITY);if(kind==DC_ERR_IDENTITY || kind==DC_ERR_UNSUPPORTED || kind==DC_ERR_MALFORMED || kind==DC_ERR_INVALID)break;
            if(attempt<2 && !prepare_retry())break;continue;
        }
        char why[256]{};
        if(layout.size!=sizeof(layout)||api_.geometry(&layout,why,sizeof(why))!=DC_OK){
            fail(model_,DC_ERR_UNSUPPORTED,terminated(why)&&why[0]?why:"Power arbitration layout is not understood");break;
        }
        const int blocks[]={DC_CL_TABLE_INFO,DC_CL_TABLE_CONTROL,DC_CL_TABLE_STATUS};
        const unsigned layout_index[]={0,2,1};
        std::vector<uint32_t> words[3]; dc_arbitration_words packets[3]{};bool fetched=true;
        for(size_t i=0;i<3;++i){
            const auto bytes=layout.sizes[layout_index[i]];
            if(!bytes||bytes%4||bytes/4>maximum){fail(model_,DC_ERR_MALFORMED,"Power packet size exceeds validated capacity");fetched=false;break;}
            words[i].resize(bytes/4);uint32_t count=0;int32_t present=0;
            const auto status=api_.table_get(gpu_,blocks[i],words[i].data(),static_cast<uint32_t>(words[i].size()),&count,&present);
            if(i==2 && status==DC_OK){model_.status_observed=true;model_.status_present=present==1;}
            if(status!=DC_OK){fail(model_,DC_ERR_TRANSIENT,"Power policy packet read unavailable");fetched=false;break;}
            if((present!=0&&present!=1)||count>words[i].size() || (present && count!=words[i].size()) || (!present && count)){
                fail(model_,DC_ERR_MALFORMED,"Power policy packet length or presence is invalid");fetched=false;break;}
            packets[i].present=present;packets[i].valid=1;packets[i].count=count;packets[i].words=present?words[i].data():nullptr;
        }
        if(!fetched){if(model_.error_kind==DC_ERR_TRANSIENT && attempt<2 && prepare_retry())continue;break;}
        auto report=std::make_shared<dc_arbitration_report>();report->size=sizeof(*report);
        const auto decoded=api_.decode(&layout,&packets[0],&packets[1],&packets[2],report.get());
        const auto after=identity(api_,gpu_,target_);
        if(after!=Identity::same){fail(model_,after==Identity::mismatch?DC_ERR_IDENTITY:DC_ERR_TRANSIENT,
            after==Identity::mismatch?"GPU identity changed during arbitration read":"GPU identity unavailable after arbitration read",after==Identity::mismatch);
            if(after==Identity::mismatch)break;continue;}
        if(decoded!=DC_OK || !report_valid(*report)) {fail(model_,DC_ERR_MALFORMED,"Power arbitration response is malformed");break;}
        if(!report->available){
            const std::string reason=report->reason[0]?report->reason:"Power arbitration decode unavailable";
            const bool missing_status=model_.status_observed&&!model_.status_present;
            fail(model_,missing_status?DC_ERR_TRANSIENT:DC_ERR_MALFORMED,
                missing_status?"STATUS packet unavailable; "+reason:reason);
            if(missing_status && attempt<2 && prepare_retry())continue;
            break;
        }
        model_.report=std::move(report);model_.freshness=ArbitrationFreshness::current;model_.error_kind=DC_ERR_NONE;
        model_.message="Read complete; policy packets were acquired sequentially";return model_;
    }
    return model_;
}
}
