/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_session.h"
#include "i2c_catalog.h"
#include "icx_preset.h"
#include "profile_io.h"
#include "i2c_warmup.h"
#include "python_i2c_profile.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <variant>
#include <type_traits>
#ifdef _WIN32
#include <windows.h>
#endif
namespace druta::ui {
struct IcxProfilePlanState {
    std::shared_ptr<const uint8_t> owner;
    dc_gpu_identity identity{};I2cRoute route{};std::string recipe;
    uint64_t selection_revision=0,clock_epoch=0;uint32_t floor=0;
    ProfileJson document,prior;IcxControllerPlan driver;
};
struct IcxProfileReceiptState {
    std::shared_ptr<const IcxProfilePlanState> plan;
    IcxControllerReceipt driver;
    uint64_t clock_epoch=0;uint32_t floor=0;
    bool clamp_issued=false,controller_done=false,clock_done=false,complete=false;
    std::string controller_message;
};
struct I2cProfilePlanState {
    std::shared_ptr<const uint8_t> owner;
    dc_gpu_identity identity{};
    I2cRoute route{};
    std::string recipe;
    uint64_t selection_revision=0;
    I2cFamily family=I2cFamily::unknown;
    std::variant<Nct3933Plan,Up9512rPlan,Ncp4206Plan,OffsetProfilePlan> driver;
};
struct I2cProfileReceiptState {
    std::shared_ptr<const I2cProfilePlanState> plan;
    std::variant<Nct3933Receipt,Up9512rReceipt,Ncp4206Receipt,OffsetProfileReceipt> driver;
};
namespace {
ActionResult failure(const std::string& message) { ActionResult r; r.message=message; return r; }
ActionResult result(const I2cResult& source) { ActionResult r; r.ok=source.ok; r.may_have_changed=source.may_have_changed; r.message=source.message; return r; }
struct DiscoveryCancelled {};
struct ReadOnlyScope {
    bool& flag;bool prior;
    explicit ReadOnlyScope(bool& value,bool next=true):flag(value),prior(value){flag=next;}
    ~ReadOnlyScope(){flag=prior;}
};
bool terminated(const char* text,size_t length){return std::memchr(text,0,length)!=nullptr;}
}
I2cSession::I2cSession(I2cApi api, dc_gpu* gpu, dc_gpu_identity id)
    : api_(std::move(api)), gpu_(gpu), identity_(id), service_(std::make_unique<I2cControls>(static_cast<I2cTransport&>(*this))) {
    if (!api_.sleep) api_.sleep=[](unsigned ms) {
#ifdef _WIN32
        Sleep(ms);
#else
        (void)ms;
#endif
    };
    data_.available=api_.read && api_.identity && api_.lock && api_.unlock && identity_.nvapi_ok && identity_.nvapi_selected && !identity_.pairing_error[0];
    api_.verify_hold.bound=[this]{return bound();};api_.verify_hold.sleep=api_.sleep;
    verify_hold_=std::make_unique<I2cVerifyHold>(api_.verify_hold,gpu_);
    publish_control_profiles();
}
bool I2cSession::bound() const {
    dc_gpu_identity id{}; id.size=sizeof(id);
    return gpu_ && api_.identity && api_.identity(gpu_,&id)==DC_OK && id.size==sizeof(id) && identity_.size==sizeof(identity_)
        && terminated(id.slot,sizeof(id.slot)) && terminated(identity_.slot,sizeof(identity_.slot))
        && terminated(id.uuid,sizeof(id.uuid)) && terminated(identity_.uuid,sizeof(identity_.uuid))
        && terminated(id.pairing_error,sizeof(id.pairing_error)) && terminated(identity_.pairing_error,sizeof(identity_.pairing_error))
        && id.slot[0] && id.nvapi_handle && id.nvapi_ok && id.nvapi_selected &&
        !id.pairing_error[0] && !identity_.pairing_error[0] && id.nvapi_handle==identity_.nvapi_handle && id.nvapi_devid==identity_.nvapi_devid &&
        id.nvapi_subsys==identity_.nvapi_subsys && id.nvml_selected==identity_.nvml_selected &&
        std::strcmp(id.slot,identity_.slot)==0 && std::strcmp(id.uuid,identity_.uuid)==0;
}
void I2cSession::cancellation_boundary() const {
    if(!discovery_readonly_ || !discovery_cancel_allowed_ || !api_.discovery_cancelled)return;
    bool cancelled=true;try{cancelled=api_.discovery_cancelled();}catch(...){}
    if(cancelled)throw DiscoveryCancelled{};
}
void I2cSession::sleep_ms(unsigned ms) {cancellation_boundary();api_.sleep(ms);cancellation_boundary();}
void I2cSession::progress() {data_.discovery=discovery_;if(api_.discovery_progress)try{api_.discovery_progress(discovery_);}catch(...){ /* observer cannot authorize or alter discovery */ }}
void I2cSession::remember_route() {
    if(api_.route_cache_path.empty() || !data_.selected || !bound() || !terminated(identity_.uuid,sizeof(identity_.uuid)) || !identity_.uuid[0])return;
    I2cRouteHint hint{identity_.uuid,data_.family,*data_.selected,data_.control_profile,i2c_route_contract(data_.family,data_.control_profile)};
    if(!valid_i2c_route_hint(hint))return;
    try{remember_i2c_route(api_.route_cache_path,hint);}catch(const std::exception& error){discovery_.label=std::string("Route selected; cache unchanged: ")+error.what();progress();}
}
bool I2cSession::read(uint8_t port,uint8_t addr,uint8_t reg,uint8_t width,uint32_t& value) {
    cancellation_boundary();
    dc_error error{}; error.size=sizeof(error);
    const bool ok=bound() && api_.read && api_.read(gpu_,port,addr,reg,width,&value,&error)==DC_OK && bound();
    cancellation_boundary();return ok;
}
bool I2cSession::write(uint8_t port,uint8_t addr,uint8_t reg,uint8_t width,uint32_t value) {
    dc_error error{}; error.size=sizeof(error);
    return !discovery_readonly_ && clock_request() && api_.write && api_.write(gpu_,port,addr,reg,width,value,&error)==DC_OK && bound();
}
bool I2cSession::read_bytes(I2cRoute route, uint8_t reg, size_t count, std::vector<uint8_t>& out) {
    cancellation_boundary();
    if (!bound() || !count || count > 32) return false;
    std::vector<uint8_t> value(count); dc_error error{}; error.size=sizeof(error);
    if (api_.read_bytes) {
        if (api_.read_bytes(gpu_,route.port,route.addr7,reg,value.data(),static_cast<uint32_t>(count),&error)!=DC_OK || !bound()) return false;
    } else {
        uint32_t raw=0;
        if (count>4 || !read(route.port,route.addr7,reg,static_cast<uint8_t>(count),raw)) return false;
        for(size_t i=0;i<count;++i) value[i]=static_cast<uint8_t>(raw>>(i*8));
    }
    cancellation_boundary();out=std::move(value); return true;
}
bool I2cSession::write_bytes(I2cFamily family,I2cRoute route,uint8_t reg,const std::vector<uint8_t>& bytes) {
    if (discovery_readonly_ || !bound() || !api_.controller_write || bytes.empty() || bytes.size()>32 ||
        (controller_ && controller_->needs_clock_clamp() && !clock_request())) return false;
    dc_error error{}; error.size=sizeof(error);
    selection_write_attempted_=true;
    return api_.controller_write(gpu_,static_cast<uint32_t>(family),route.port,route.addr7,reg,
        bytes.data(),static_cast<uint32_t>(bytes.size()),&error)==DC_OK && bound();
}
std::optional<uint32_t> I2cSession::architecture() const {
    uint32_t value=0; if (!bound() || !api_.architecture || api_.architecture(gpu_,&value)!=DC_OK || !value) return {};
    return value;
}
std::optional<double> I2cSession::reference_mv() const {
    dc_telemetry t{}; t.size=sizeof(t);
    if(!bound() || !api_.telemetry || api_.telemetry(gpu_,&t)!=DC_OK || !(t.present&DC_T_VCORE_MV) || !std::isfinite(t.vcore_mv))return{};
    return t.vcore_mv;
}
std::optional<double> I2cSession::core_clock_mhz() const {
    dc_telemetry t{}; t.size=sizeof(t);
    if(!bound() || !api_.telemetry || api_.telemetry(gpu_,&t)!=DC_OK || !(t.present&DC_T_CORE) || !t.core)return{};
    return static_cast<double>(t.core);
}
std::optional<ControllerOperatingPoint> I2cSession::operating_point() const {
    if(verify_hold_)return verify_hold_->check();
    dc_vf_lock_status vf{}; dc_clk_lock_status clock{};
    if(!bound())return{};
    const bool vf_held=api_.vf_status && api_.vf_status(gpu_,0,0,&vf)==DC_OK && !vf.error[0] && vf.locked;
    const bool clock_held=api_.clock_status && api_.clock_status(gpu_,&clock)==DC_OK && !clock.error[0] && clock.locked;
    if(!vf_held && !clock_held)return{};
    dc_telemetry t{};t.size=sizeof(t);
    if(!api_.telemetry || api_.telemetry(gpu_,&t)!=DC_OK || !(t.present&DC_T_PSTATE))return{};
    ControllerOperatingPoint point; point.pstate=t.pstate;
    if(t.present&DC_T_CORE)point.core_mhz=t.core;
    if(t.present&DC_T_MEM)point.memory_mhz=t.mem;
    return point;
}
bool I2cSession::xoc() const {
    int32_t enabled=0,unrestricted=0;
    return api_.flags && api_.flags(gpu_,&unrestricted,&enabled)==DC_OK && enabled!=0;
}
std::unique_ptr<ControllerDriver> I2cSession::driver(I2cFamily family,I2cRoute route) {
    switch(family) {
    case I2cFamily::nct3933:return make_nct3933(*this,route);
    case I2cFamily::ncp4206:return make_ncp4206(*this,route);
    case I2cFamily::up9512r:return make_up9512r(*this,route);
    case I2cFamily::icx:return make_icx(*this,route);
    case I2cFamily::mp2888:return make_mp2888_offset(*this,route);
    case I2cFamily::mp29816:return make_mp29816(*this,route);
    default:return{};
    }
}
bool I2cSession::select(I2cFamily family,I2cRoute route,const std::string& recipe,bool cancellable) {
    if(owned())return false;
    ReadOnlyScope cancel_scope(discovery_cancel_allowed_,discovery_cancel_allowed_ && cancellable);
    ReadOnlyScope scope(discovery_readonly_);cancellation_boundary();
    if(route.port>7 || route.addr7<8 || route.addr7>0x77 || !bound())return false;
    // The original MP2888 offset recipe is the default. Manual PMBus is an
    // explicit recipe choice, never inferred from an adapter's marketing name.
    const bool manual=family==I2cFamily::mp2888 && recipe=="mp2888a-pmbus";
    if(!recipe.empty() && recipe!=(family==I2cFamily::mp2888?"mp2888a-offset":"controller") && !manual)return false;
    auto service=std::make_unique<I2cControls>(static_cast<I2cTransport&>(*this));
    auto chosen=manual ? nullptr : driver(family,route);
    const bool present=manual?service->select(route):(chosen && chosen->probe());
    cancellation_boundary();if(!present)return false;
    ControllerView view;I2cStatus status;
    if(manual){status=service->status();cancellation_boundary();if(!status.readable)return false;}
    else {view=chosen->read(xoc());cancellation_boundary();if(!view.readable)return false;}
    cancellation_boundary();if(!bound())return false;
    // Linearization point: every cancellable probe/read is complete. Finish
    // publication/cache metadata as one non-cancellable commit, so a late
    // cancel cannot claim that the established selection was retained.
    ReadOnlyScope commit_scope(discovery_cancel_allowed_,false);
    // Commit the freshly inspected controller and service together. A cancelled
    // or partial probe has not touched the established selection or its drafts.
    service_=std::move(service);profile_receipt_.reset();icx_receipt_.reset();
    controller_=std::move(chosen); data_.selected=route;data_.family=family; data_.verified=false;
    data_.controller=std::move(view);data_.status=std::move(status); ++data_.selection_revision;
    data_.control_profile=manual?"mp2888a-pmbus":family==I2cFamily::mp2888?"mp2888a-offset":"controller";
    data_.needs_clock_clamp=manual || controller_->needs_clock_clamp();
    service_->set_gate(data_.gate); captured_preset_.reset();pending_preset_.reset();prepared_preset_.reset();selection_write_attempted_=false;
    refresh(false);remember_route();return true;
}
ActionResult I2cSession::scan(const Command& command) {
    if(owned())return failure("Release the owned I2C request before scanning");
    ReadOnlyScope readonly(discovery_readonly_);
    discovery_={};discovery_.running=true;discovery_.label="Preparing read-only controller probes";progress();
    const auto scope=command.i2c_full_scan ? I2cFamily::unknown : command.i2c_family;
    if(static_cast<size_t>(scope)>=i2c_family_names.size())return failure("Unknown controller scan scope");
    cancellation_boundary();
    if(!command.i2c_full_scan && data_.selected && (scope==I2cFamily::unknown || scope==data_.family)) {
        // A reconnect is a bounded read of the established selection, not a
        // reason to discard it and infer a different route after a NACK.
        for(unsigned attempt=0;attempt<3;++attempt) {
            discovery_.total=3;discovery_.label="Rechecking selected controller";cancellation_boundary();
            auto check=driver(data_.family,*data_.selected);bool present=false;
            if(data_.family==I2cFamily::mp2888 && data_.control_profile=="mp2888a-pmbus"){
                I2cControls temporary(static_cast<I2cTransport&>(*this));present=temporary.select(*data_.selected) && temporary.status().readable;
            } else present=check && check->probe() && check->read(xoc()).readable;
            ++discovery_.completed;progress();cancellation_boundary();
            if(present) { refresh();cancellation_boundary();remember_route();discovery_.running=false;discovery_.label="Selected controller reconnected";progress();auto r=failure("Selected controller reconnected; no scan or write required");r.ok=true;return r; }
            if(attempt<2)sleep_ms(20);
        }
        discovery_.running=false;discovery_.label="Selected controller unreadable; route and recipe retained";progress();
        return failure("Selected controller did not answer; selection and recipe retained. Retry Connect or choose Full scan explicitly.");
    }
    if(!command.i2c_full_scan && !api_.route_cache_path.empty()) {
        try {
            const auto hint=load_i2c_route_cache(api_.route_cache_path,identity_.uuid);
            if(hint && (scope==I2cFamily::unknown || scope==hint->family)) {
                discovery_.total=1;discovery_.label="Rechecking remembered route (unverified)";progress();cancellation_boundary();
                if(select(hint->family,hint->route,hint->recipe)) {
                    I2cCandidate candidate;candidate.route=hint->route;candidate.family=static_cast<uint32_t>(hint->family);candidate.description="Freshly probed remembered route; response verification is not cached";
                    data_.candidates={candidate};discovery_.candidates=data_.candidates;discovery_.completed=1;discovery_.running=false;
                    discovery_.label="Remembered route read; no gate or verification restored";progress();auto action=failure(discovery_.label);action.ok=true;return action;
                }
            }
        }catch(const std::exception& error){discovery_.label=std::string("Route hint unavailable; scoped scan: ")+error.what();progress();}
        cancellation_boundary();
    }
    std::vector<I2cCandidate> found;
    struct Probe {I2cFamily family;I2cRoute route;};std::vector<Probe> probes;
    for(uint32_t kind=1;kind<=6;++kind) {
        const auto family=static_cast<I2cFamily>(kind);if(scope!=I2cFamily::unknown && family!=scope)continue;
        for(uint8_t port=0;port<8;++port) {
            if(family==I2cFamily::icx && port!=1)continue;
            std::vector<uint8_t> addresses;
            if(family==I2cFamily::nct3933) {for(uint8_t a=0x10;a<=0x15;++a)addresses.push_back(a);}
            else if(family==I2cFamily::icx)addresses={0x2d};
            else {const uint8_t first=family==I2cFamily::up9512r?0x25:family==I2cFamily::mp29816?0x30:0x20;addresses.push_back(first);for(uint8_t a=8;a<=0x77;++a)if(a!=first)addresses.push_back(a);}
            for(const auto address:addresses) {
                probes.push_back({family,{port,address}});
            }
        }
    }
    discovery_.completed=0;discovery_.total=static_cast<uint32_t>(probes.size());
    for(const auto& probe:probes) {
        cancellation_boundary();if(!bound()){data_.candidates=found;discovery_.running=false;discovery_.label="Selected GPU changed or unreadable; partial hits remain unverified";progress();return failure(discovery_.label);}
        auto candidate=driver(probe.family,probe.route);
        if(candidate && candidate->probe()) {
            I2cCandidate hit;hit.route=probe.route;hit.family=static_cast<uint32_t>(probe.family);hit.description=std::string(i2c_family_name(probe.family))+"; unverified read-only candidate";found.push_back(std::move(hit));
        }
        ++discovery_.completed;discovery_.candidates=found;
        discovery_.label=std::string(i2c_family_name(probe.family))+" port "+std::to_string(probe.route.port)+", address "+std::to_string(probe.route.addr7);
        progress();cancellation_boundary();
    }
    data_.candidates=found;
    ActionResult action;action.ok=true;
    if(data_.candidates.size()==1 && select(static_cast<I2cFamily>(data_.candidates[0].family),data_.candidates[0].route))
        action.message="One controller selected; read and verify this control method before Apply";
    else action.message=std::to_string(data_.candidates.size())+" controller candidates; select one to inspect. Established selection retained.";
    discovery_.running=false;discovery_.label=action.message;progress();
    return action;
}
bool I2cSession::clock_request() const {
    dc_clk_lock_status status{};
    return data_.clock_owned && bound() && api_.clock_status && api_.clock_status(gpu_,&status)==DC_OK &&
        !status.error[0] && status.locked && status.min_mhz==data_.clock_mhz && status.max_mhz==data_.clock_mhz;
}
std::optional<double> I2cSession::observed_clock() const {
    if (!clock_request() || !api_.telemetry) return {};
    dc_telemetry t{}; t.size=sizeof(t);
    if (api_.telemetry(gpu_,&t)!=DC_OK || !(t.present&DC_T_CORE) || !std::isfinite(t.core) || t.core<=0 || t.core>data_.clock_mhz) return {};
    return t.core;
}
bool I2cSession::clamp(std::string& reason,const std::function<void(uint32_t)>& before_request) {
    if (data_.clock_owned) {
        if (observed_clock()) return true;
        reason="Owned low-clock clamp is not confirmed; retry or release"; return false;
    }
    if (!bound() || !api_.clocks || !api_.clamp || !api_.release || !api_.clock_status || !api_.vf_status || !api_.telemetry) {
        reason="Low-clock ownership interfaces unavailable"; return false;
    }
    if (api_.before_clock_acquire && !api_.before_clock_acquire(reason)) {
        if (reason.empty()) reason="Clock handover was refused";
        return false;
    }
    dc_vf_lock_status vf{}; dc_clk_lock_status lock{};
    if (api_.vf_status(gpu_,0,0,&vf)!=DC_OK || vf.error[0] || vf.locked ||
        api_.clock_status(gpu_,&lock)!=DC_OK || lock.error[0] || lock.locked) {
        reason="Release the existing V/F or frequency hold in its owning window first"; return false;
    }
    size_t count=0;
    const auto probe=api_.clocks(gpu_,nullptr,0,&count);
    if ((probe!=DC_OK && probe!=DC_E_BUFFER) || !count || count>128) { reason="Driver clock table unavailable"; return false; }
    std::vector<dc_clock_row> rows(count);
    if (api_.clocks(gpu_,rows.data(),rows.size(),&count)!=DC_OK || !count || count>rows.size()) { reason="Driver clock table changed"; return false; }
    const dc_clock_row* top=nullptr;
    for (size_t i=0;i<count;++i) if (!top || rows[i].mem_mhz>top->mem_mhz) top=&rows[i];
    if (!top || !top->count || top->count>512) { reason="Top memory row has no understood clock range"; return false; }
    const uint32_t floor=*std::min_element(top->graphics,top->graphics+top->count);
    if (!floor) { reason="Driver clock floor is invalid"; return false; }
    if(before_request)before_request(floor);
    // Save ownership before SET; an error can follow a dispatch.
    ++clock_epoch_;data_.clock_owned=true; data_.clock_mhz=floor;
    dc_outcome out{};
    if (api_.clamp(gpu_,floor,floor,&out)!=DC_OK || !out.ok || !clock_request()) {
        reason="Low-clock request unverified; recovery remains available"; return false;
    }
    std::optional<double> previous; unsigned stable=0;
    for (unsigned attempt=0;attempt<30;++attempt) {
        auto now=observed_clock();
        if (now && previous && *now==*previous) ++stable; else stable=now?1u:0u;
        if (stable>=3) return true;
        previous=now; api_.sleep(100);
    }
    reason="Observed clocks did not settle at the driver-enumerated floor; clamp retained"; return false;
}
void I2cSession::publish_control_profiles() {
    if(data_.selected && data_.family!=I2cFamily::mp2888) {data_.control_profiles.clear();return;}
    I2cControlProfile manual;
    manual.id = "mp2888a-pmbus"; manual.label = "MP2888A (ASUS Ampere)";
    manual.description = "Nominal voltage requests, not measured voltage. Verify checks response at the driver's lowest clock; Release restores captured state.";
    manual.available = data_.available && api_.write;
    manual.step_mv = I2cControls::nominal_step_mv;
    if (data_.status.capable && data_.status.min_mv && data_.status.max_mv)
        manual.presets_mv = I2cControls::nominal_presets(*data_.status.min_mv, *data_.status.max_mv);
    I2cControlProfile offset;offset.id="mp2888a-offset";offset.label="MP2888A (ASUS Turing)";
    offset.description="GPU VID offset recipe. Legacy recipe labels do not restrict the GPU generation or board; each method requires its own Verify.";
    offset.available=data_.available && api_.controller_write;offset.step_mv=6.25;
    if(data_.control_profile.empty())data_.control_profile=manual.id;
    data_.control_profiles = {std::move(offset),std::move(manual)};
}
void I2cSession::refresh(bool read_controller) {
    if(data_.selected && controller_) {
        if(read_controller)data_.controller=controller_->read(xoc());const auto& view=data_.controller;
        data_.verified=view.verified || !view.needs_verification;
        auto& status=data_.status;status.readable=view.readable;status.stale=view.stale;
        const bool retained_cleanup=icx_receipt_&&!icx_receipt_->complete&&(icx_receipt_->controller_done||icx_receipt_->clock_done);
        status.capable=view.capable;status.owned=view.owned||bool(icx_receipt_);status.recovery_pending=view.recovery||retained_cleanup;
        status.response_verified=data_.verified;status.enabled=data_.gate;
        status.request_eligible=view.capable && !status.recovery_pending;
        status.writable=!retained_cleanup && data_.gate && data_.verified && api_.controller_write &&
            std::any_of(view.fields.begin(),view.fields.end(),[](const auto& field){return field.writable;});
        status.message=view.message;
        if(retained_cleanup)status.message+="; iCX receipt cleanup/final verification remains pending";
        if(data_.family==I2cFamily::icx && !captured_preset_ && !selection_write_attempted_) {
            try { captured_preset_=capture_icx_preset(view).dump(); } catch(...) { /* partial read is retryable */ }
        }
    } else if(data_.selected) {if(read_controller)data_.status=service_->status();data_.controller={};}
    data_.captured_preset=captured_preset_.has_value();data_.pending_preset=pending_preset_.has_value();
    data_.preset_preview=pending_preset_?describe_icx_preset(ProfileJson::parse(*pending_preset_)):"No iCX preset staged.";
    publish_control_profiles();
}
ActionResult I2cSession::preset(const Command& command) {
    if(data_.family!=I2cFamily::icx || !controller_)return failure("Select an iCX controller first");
    if(command.i2c_selection_revision!=data_.selection_revision)return failure("Preset belongs to another controller selection");
    ActionResult action;
    try {
        if(command.operation==Command::Operation::i2c_prepare_profile) {
            prepared_preset_.reset();
            prepared_preset_=capture_icx_preset(controller_->read(xoc())).dump();
            ++data_.prepared_preset_revision;action.ok=true;action.message="Fresh iCX settings captured for Save";
        } else if(command.operation==Command::Operation::i2c_save_profile) {
            if(!prepared_preset_ || command.i2c_preset_revision!=data_.prepared_preset_revision)return failure("Save capture expired; choose Save again");
            write_profile(command.profile_path,ProfileJson::parse(*prepared_preset_));action.ok=true;action.message="Captured iCX requested settings saved";
            prepared_preset_.reset();
        } else if(command.operation==Command::Operation::i2c_load_profile) {
            auto document=read_profile(command.profile_path,64*1024);
            icx_preset_requests(document,true);pending_preset_=document.dump();
            action.ok=true;action.message="iCX preset staged; review it, then Apply iCX preset";
        } else if(command.operation==Command::Operation::i2c_stage_captured) {
            if(!captured_preset_)return failure("No complete connection settings captured yet; Read settings first");
            pending_preset_=captured_preset_;action.ok=true;action.message="Captured connection settings staged; no hardware write";
        } else {
            if(!pending_preset_ || !data_.gate || !data_.verified)return failure("Stage a preset and check iCX identity before Apply");
            const auto document=ProfileJson::parse(*pending_preset_);
            const auto requests=icx_preset_requests(document,xoc());
            const auto before=capture_icx_preset(controller_->read(xoc())); // Complete fresh capture required.
            const auto previous=icx_preset_requests(before,true);
            std::string reason;if(!clamp(reason))return failure(reason);
            for(const auto& request:requests) {
                const auto old=std::find_if(previous.begin(),previous.end(),[&](const auto& r){return r.key==request.key;});
                if(old!=previous.end() && old->reset==request.reset && (request.reset || old->value==request.value))continue;
                auto step=controller_->apply(request,{data_.gate,xoc()});action.may_have_changed|=step.may_have_changed;
                if(!step.ok){action.message="iCX preset incomplete; owned restoration retained: "+step.message;return action;}
            }
            const auto after=capture_icx_preset(controller_->read(xoc()));
            action.ok=after.at("settings")==document.at("settings");
            action.message=action.ok?"iCX preset settings read back exactly; controller requests, not physical measurements":"iCX preset readback differs; restoration retained";
            if(action.ok)pending_preset_.reset();
            if(!controller_->owned()){const auto released=release();if(!released.ok){action.ok=false;action.message+="; "+released.message;}}
        }
    }catch(const std::exception& error){action.message=std::string("iCX preset: ")+error.what();}
    return action;
}
bool I2cSession::icx_plan_bound(const IcxProfilePlanState& plan) const {
    const auto& id=plan.identity;
    return plan.owner==profile_owner_ && bound() && data_.family==I2cFamily::icx && controller_ && data_.selected && id.size==sizeof(id)
        && terminated(id.uuid,sizeof(id.uuid)) && terminated(id.slot,sizeof(id.slot)) && id.uuid[0]
        && std::strcmp(id.uuid,identity_.uuid)==0 && std::strcmp(id.slot,identity_.slot)==0
        && id.nvapi_handle==identity_.nvapi_handle && id.nvapi_devid==identity_.nvapi_devid && id.nvapi_subsys==identity_.nvapi_subsys
        && plan.route.port==data_.selected->port && plan.route.addr7==data_.selected->addr7
        && plan.recipe==data_.control_profile && plan.selection_revision==data_.selection_revision;
}
ActionResult I2cSession::preflight_icx_profile(const ProfileJson& document,IcxProfilePlan& out) {
    out={};
    if(!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK)return failure("Could not lock GPU for iCX preflight");
    struct Unlock {I2cApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api_,gpu_};
    try {
        if(!bound() || data_.family!=I2cFamily::icx || !data_.selected || !controller_ || !data_.gate || !data_.verified || !api_.controller_write)
            return failure("Select iCX, enable its gate and qualify its current identity before catalog Apply");
        if(owned())return failure("Release existing I2C requests before an independent iCX catalog transaction");
        auto plan=std::make_shared<IcxProfilePlanState>();plan->owner=profile_owner_;plan->identity=identity_;plan->route=*data_.selected;
        plan->recipe=data_.control_profile;plan->selection_revision=data_.selection_revision;plan->clock_epoch=clock_epoch_;plan->document=document;
        const auto requests=icx_preset_requests(document,xoc());
        plan->prior=capture_icx_preset(controller_->read(xoc()));
        const auto prepared=controller_->preview_icx(requests,{data_.gate,xoc()},plan->driver);
        if(!prepared.ok)return result(prepared);
        if(plan->prior.at("settings")!=document.at("settings")) {
            if(!api_.clock_status || !api_.vf_status || !api_.clocks || !api_.clamp || !api_.release || !api_.telemetry)
                return failure("iCX catalog clock ownership interfaces unavailable");
            dc_clk_lock_status clock{};dc_vf_lock_status vf{};
            if(api_.clock_status(gpu_,&clock)!=DC_OK || api_.vf_status(gpu_,0,0,&vf)!=DC_OK || !terminated(clock.error,sizeof(clock.error))
                || !terminated(vf.error,sizeof(vf.error)) || clock.error[0] || vf.error[0] || clock.locked || vf.locked)
                return failure("Release existing V/F or frequency requests before iCX catalog preflight");
            size_t count=0;const auto query=api_.clocks(gpu_,nullptr,0,&count);
            if((query!=DC_OK && query!=DC_E_BUFFER) || !count || count>128)return failure("iCX catalog clock table unavailable");
            std::vector<dc_clock_row> clocks(count);
            if(api_.clocks(gpu_,clocks.data(),clocks.size(),&count)!=DC_OK || !count || count>clocks.size())return failure("iCX catalog clock table changed");
            const dc_clock_row* top=nullptr;for(size_t i=0;i<count;++i)if(!top || clocks[i].mem_mhz>top->mem_mhz)top=&clocks[i];
            if(!top || !top->count || top->count>512)return failure("iCX catalog clock floor geometry unavailable");
            plan->floor=*std::min_element(top->graphics,top->graphics+top->count);
            if(!plan->floor)return failure("iCX catalog clock floor geometry unavailable");
        }
        if(!icx_plan_bound(*plan))return failure("iCX catalog target changed during read-only preflight");
        out.state=std::move(plan);auto action=failure("iCX target, complete prior and clock eligibility qualified without effects");action.ok=true;return action;
    }catch(const std::exception& error){return failure(std::string("iCX preflight: ")+error.what());}
}
ActionResult I2cSession::apply_icx_profile(const IcxProfilePlan& plan,const std::function<void(const IcxProfileReceipt&)>& journal) {
    if(!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK)return failure("Could not lock GPU for iCX catalog Apply");
    struct Unlock {I2cApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api_,gpu_};
    const auto before_epoch=clock_epoch_;ActionResult action;IcxProfileReceipt receipt;
    try {
        if(!plan.state || !icx_plan_bound(*plan.state) || plan.state->clock_epoch!=clock_epoch_ || !data_.gate || !data_.verified || !api_.controller_write || owned())
            return failure("iCX catalog plan binding, gate or independent ownership changed");
        (void)icx_preset_requests(plan.state->document,xoc());
        const auto current=capture_icx_preset(controller_->read(xoc()));
        if(current.at("settings")!=plan.state->prior.at("settings"))return failure("iCX original requests changed since preflight; no effect issued");
        if(current.at("settings")==plan.state->document.at("settings"))return verify_icx_profile(plan);
        receipt.state=std::make_shared<IcxProfileReceiptState>();receipt.state->plan=plan.state;receipt.state->floor=plan.state->floor;
        receipt.state->clock_epoch=clock_epoch_;
        std::string reason;
        const bool held=clamp(reason,[&](uint32_t floor){
            if(!icx_plan_bound(*plan.state) || floor!=plan.state->floor || clock_epoch_!=plan.state->clock_epoch)
                throw std::runtime_error("iCX planned floor or target changed before clamp dispatch");
            receipt.state->clamp_issued=true;receipt.state->clock_epoch=clock_epoch_+1;
            try{if(journal)journal(receipt);}catch(...){receipt.state->clamp_issued=false;receipt.state->clock_epoch=clock_epoch_;throw;}
            icx_receipt_=receipt.state;
        });
        if(!held){action.message=reason;action.may_have_changed=receipt.state->clamp_issued;try{refresh();}catch(...){}return action;}
        action.may_have_changed=true;
        action=result(controller_->apply_icx(plan.state->driver,{data_.gate,xoc()},[&](const IcxControllerReceipt& driver){
            if(!icx_plan_bound(*plan.state) || clock_epoch_!=receipt.state->clock_epoch || !data_.clock_owned || data_.clock_mhz!=receipt.state->floor || !observed_clock())
                throw std::runtime_error("Owned iCX clock lifecycle changed before controller dispatch");
            receipt.state->driver=driver;
        }));
        action.may_have_changed=true; // The owned clamp was already dispatched.
        if(action.ok){const auto verified=verify_icx_profile(plan);action.ok=verified.ok;if(!verified.ok)action.message=verified.message;}
        try{refresh();}catch(...){}return action;
    }catch(const std::exception& error){action.ok=false;action.message=std::string("iCX catalog Apply: ")+error.what();action.may_have_changed=clock_epoch_!=before_epoch || (receipt.state&&receipt.state->driver.state);try{refresh();}catch(...){}return action;}
}
bool I2cSession::release_icx_clock(IcxProfileReceiptState& receipt,std::string& reason,bool& changed) {
    if(!receipt.clamp_issued){receipt.clock_done=true;return true;}
    if(receipt.clock_epoch!=clock_epoch_){reason="iCX clock lifecycle was replaced; no reset issued";return false;}
    dc_clk_lock_status status{};
    if(!bound() || !api_.clock_status || api_.clock_status(gpu_,&status)!=DC_OK || !terminated(status.error,sizeof(status.error)) || status.error[0]) {
        reason="iCX controller restored; clock ownership unreadable, retry Undo";return false;
    }
    if(receipt.clock_done){if(status.locked){reason="A later clock request replaced completed iCX cleanup";return false;}return true;}
    if(!data_.clock_owned || data_.clock_mhz!=receipt.floor){reason="iCX session no longer owns this clock lifecycle";return false;}
    if(status.locked && (status.min_mhz!=receipt.floor || status.max_mhz!=receipt.floor)) {
        reason="iCX controller restored; foreign clock tuple left untouched";return false;
    }
    if(status.locked) {
        if(!api_.release){reason="iCX clock reset interface unavailable";return false;}
        ++clock_epoch_;receipt.clock_epoch=clock_epoch_;changed=true;dc_outcome outcome{};
        if(api_.release(gpu_,0,&outcome)!=DC_OK || !outcome.ok || api_.clock_status(gpu_,&status)!=DC_OK || !terminated(status.error,sizeof(status.error)) || status.error[0] || status.locked) {
            reason="iCX controller restored; owned clock release unconfirmed, receipt retained";return false;
        }
    }
    data_.clock_owned=false;data_.clock_mhz=0;receipt.clock_done=true;return true;
}
ActionResult I2cSession::verify_icx_profile(const IcxProfilePlan& plan) {
    if(!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK)return failure("Could not lock GPU for iCX profile verification");
    struct Unlock {I2cApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api_,gpu_};
    try {
        if(!plan.state || !icx_plan_bound(*plan.state))return failure("iCX plan target, route or selected driver changed");
        const auto checked=controller_->verify_icx(plan.state->driver);if(!checked.ok)return result(checked);
        if(icx_receipt_ && icx_receipt_->plan==plan.state) {
            if(icx_receipt_->complete || icx_receipt_->clock_epoch!=clock_epoch_ || !data_.clock_owned || data_.clock_mhz!=icx_receipt_->floor || !observed_clock())
                return failure("iCX desired requests do not retain their exact owned clock lifecycle");
        }else if(plan.state->clock_epoch!=clock_epoch_)return failure("iCX planned clock lifecycle changed");
        auto action=failure("Complete selected iCX requests and owned clock lifecycle verified");action.ok=true;return action;
    }catch(const std::exception& error){return failure(std::string("iCX verification: ")+error.what());}
}
ActionResult I2cSession::verify_icx_profile(const IcxProfileReceipt& receipt,bool original) {
    if(!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK)return failure("Could not lock GPU for iCX receipt verification");
    struct Unlock {I2cApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api_,gpu_};
    try {
        if(!receipt.state || !receipt.state->plan || !icx_plan_bound(*receipt.state->plan) || receipt.state->clock_epoch!=clock_epoch_)
            return failure("iCX receipt target or clock lifecycle changed");
        if(receipt.state->driver.state){const auto checked=controller_->verify_icx(receipt.state->driver,original);if(!checked.ok)return result(checked);}
        else {
            const auto live=capture_icx_preset(controller_->read(xoc())).at("settings");
            if(live!=(original?receipt.state->plan->prior:receipt.state->plan->document).at("settings"))return failure("iCX complete semantic state differs");
        }
        if(original) {
            dc_clk_lock_status status{};
            if(!receipt.state->controller_done || !receipt.state->clock_done || data_.clock_owned || !api_.clock_status || api_.clock_status(gpu_,&status)!=DC_OK || !terminated(status.error,sizeof(status.error)) || status.error[0] || status.locked)
                return failure("iCX original controller/clock cleanup is not fully verified");
        }else if(!data_.clock_owned || data_.clock_mhz!=receipt.state->floor || !observed_clock())return failure("iCX issued clock request is unconfirmed");
        auto action=failure(original?"Original issued iCX requests and released clock verified":"Issued iCX requests and owned clock verified");
        if(original&&!receipt.state->controller_message.empty())action.message+="; "+receipt.state->controller_message;
        action.ok=true;return action;
    }catch(const std::exception& error){return failure(std::string("iCX receipt verification: ")+error.what());}
}
ActionResult I2cSession::restore_icx_profile(const IcxProfileReceipt& receipt) {
    if(!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK)return failure("Could not lock GPU for iCX catalog Undo");
    struct Unlock {I2cApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api_,gpu_};
    const auto before_epoch=clock_epoch_;ActionResult action;
    try {
        if(!receipt.state || !receipt.state->plan || !receipt.state->clamp_issued || !icx_plan_bound(*receipt.state->plan) || receipt.state->clock_epoch!=clock_epoch_ || (icx_receipt_&&icx_receipt_!=receipt.state))
            return failure("iCX Undo belongs to another target, selection or ownership lifecycle");
        auto& saved=*receipt.state;
        if(saved.complete)return verify_icx_profile(receipt,true);
        if(!saved.controller_done) {
            if(!data_.clock_owned || data_.clock_mhz!=saved.floor || (saved.driver.state&&!observed_clock()))return failure("Restore the exact owned iCX floor before controller Undo");
            if(saved.driver.state){action=result(controller_->restore_icx(saved.driver));if(!action.ok){try{refresh();}catch(...){}return action;}saved.controller_message=action.message;}
            else if(capture_icx_preset(controller_->read(xoc())).at("settings")!=saved.plan->prior.at("settings"))return failure("Unissued iCX fields changed during clamp failure; no replay issued");
            saved.controller_done=true;
        }else if(saved.driver.state){const auto checked=controller_->verify_icx(saved.driver,true);if(!checked.ok)return result(checked);}
        else if(capture_icx_preset(controller_->read(xoc())).at("settings")!=saved.plan->prior.at("settings"))return failure("Unissued iCX prior changed while clock cleanup was pending");
        std::string reason;bool changed=action.may_have_changed;
        if(!release_icx_clock(saved,reason,changed)){action.ok=false;action.may_have_changed=changed;action.message=reason;try{refresh();}catch(...){}return action;}
        action=verify_icx_profile(receipt,true);action.may_have_changed=changed;
        if(action.ok){saved.complete=true;if(icx_receipt_==receipt.state)icx_receipt_.reset();}
        try{refresh();}catch(...){}return action;
    }catch(const std::exception& error){action.ok=false;action.message=std::string("iCX catalog Undo: ")+error.what();action.may_have_changed=action.may_have_changed || clock_epoch_!=before_epoch;try{refresh();}catch(...){}return action;}
}
bool I2cSession::profile_bound(const I2cProfilePlanState& plan) const {
    const auto& id=plan.identity;
    return plan.owner==profile_owner_ && bound() && data_.family==plan.family && controller_ && data_.selected
        && id.size==sizeof(id) && terminated(id.uuid,sizeof(id.uuid)) && terminated(id.slot,sizeof(id.slot)) && id.uuid[0]
        && std::strcmp(id.uuid,identity_.uuid)==0 && std::strcmp(id.slot,identity_.slot)==0
        && id.nvapi_handle==identity_.nvapi_handle && id.nvapi_devid==identity_.nvapi_devid && id.nvapi_subsys==identity_.nvapi_subsys
        && plan.route.port==data_.selected->port && plan.route.addr7==data_.selected->addr7
        && plan.recipe==data_.control_profile && plan.selection_revision==data_.selection_revision;
}
ActionResult I2cSession::preflight_i2c_profile(const ProfileJson& document,I2cProfilePlan& out) {
    out={};
    if(!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK)return failure("Could not lock GPU for controller profile preflight");
    struct Unlock {I2cApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api_,gpu_};
    try {
        if(!bound() || !data_.selected || !controller_)
            return failure("Explicitly select the saved controller before profile preflight");
        if(owned())return failure("Release existing I2C requests before an independent controller profile");
        auto plan=std::make_shared<I2cProfilePlanState>();plan->owner=profile_owner_;plan->identity=identity_;plan->route=*data_.selected;
        plan->recipe=data_.control_profile;plan->selection_revision=data_.selection_revision;plan->family=data_.family;
        const auto route_matches=[&](I2cRoute route){return route.port==plan->route.port && route.addr7==plan->route.addr7 && plan->recipe=="controller";};
        // uP's released matching path is a read-only comparison, including locked
        // controllers. Its driver admits changed requests only after all gates.
        if(data_.family!=I2cFamily::up9512r && (!data_.gate || !data_.verified || !api_.controller_write))
            return failure("Enable I2C and verify the selected controller before profile preflight");
        I2cResult checked;
        switch(data_.family) {
        case I2cFamily::nct3933: {
            const auto saved=parse_python_nct3933_profile(document);
            if(!route_matches(saved.route))return failure("Saved controller route or recipe differs from the selected controller");
            Nct3933Plan typed;checked=controller_->preview_nct3933({saved.outputs,saved.configuration},saved.selection,typed);plan->driver=typed;break;
        }
        case I2cFamily::up9512r: {
            const auto saved=parse_python_up9512r_profile(document);
            if(!route_matches(saved.route))return failure("Saved controller route or recipe differs from the selected controller");
            Up9512rControl desired;desired.enabled=saved.enabled;std::copy(saved.offsets_mv.begin(),saved.offsets_mv.end(),desired.offsets_mv.begin());
            Up9512rPlan typed;checked=controller_->preview_up9512r(desired,{data_.gate && bool(api_.controller_write),xoc()},typed);plan->driver=typed;break;
        }
        case I2cFamily::ncp4206: {
            const auto saved=parse_python_ncp4206_profile(document);
            if(!route_matches(saved.route))return failure("Saved controller route or recipe differs from the selected controller");
            Ncp4206Plan typed;checked=controller_->preview_ncp4206({saved.enabled,saved.command},xoc(),typed);plan->driver=typed;break;
        }
        case I2cFamily::mp2888:
        case I2cFamily::mp29816: {
            const auto saved=parse_python_offset_profile(document);
            const auto expected_recipe=data_.family==I2cFamily::mp2888?"mp2888a-offset":"controller";
            if(saved.family!=data_.family || saved.route.port!=plan->route.port || saved.route.addr7!=plan->route.addr7 || plan->recipe!=expected_recipe)
                return failure("Saved coarse controller route, family or recipe differs from the selected controller");
            OffsetProfilePlan typed;checked=controller_->preview_offset_profile({saved.offset_mv,saved.page,saved.scale},{data_.gate,xoc()},typed);plan->driver=typed;break;
        }
        default:return failure("This selected controller has no qualified full-tune profile adapter yet");
        }
        if(!checked.ok)return result(checked);
        if(!profile_bound(*plan))return failure("Controller profile binding changed during preflight");
        out.state=std::move(plan);auto action=result(checked);action.message="Exact saved controller state qualified; no write or clock request";return action;
    }catch(const std::exception& error){return failure(std::string("Controller profile preflight: ")+error.what());}
}
ActionResult I2cSession::apply_i2c_profile(const I2cProfilePlan& plan,const std::function<void(const I2cProfileReceipt&)>& journal) {
    if(!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK)return failure("Could not lock GPU for controller profile Apply");
    struct Unlock {I2cApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api_,gpu_};
    ActionResult action;bool issued=false;
    try {
        if(!plan.state || !profile_bound(*plan.state) || owned())
            return failure("Controller profile binding, gate or independent ownership changed");
        if(plan.state->family!=I2cFamily::up9512r && (!data_.gate || !data_.verified || !api_.controller_write))
            return failure("Controller profile gate or response verification changed");
        const auto save=[&](const auto& receipt){
            auto stored=std::make_shared<I2cProfileReceiptState>();stored->plan=plan.state;stored->driver=receipt;
            if(journal)journal({stored});profile_receipt_=stored;issued=true;
        };
        const ControllerContext context{data_.gate && bool(api_.controller_write),xoc()};
        action=result(std::visit([&](const auto& typed){
            using T=std::decay_t<decltype(typed)>;
            if constexpr(std::is_same_v<T,Nct3933Plan>)return controller_->apply_nct3933(typed,context,save);
            else if constexpr(std::is_same_v<T,Up9512rPlan>)return controller_->apply_up9512r(typed,context,save);
            else if constexpr(std::is_same_v<T,Ncp4206Plan>)return controller_->apply_ncp4206(typed,context,save);
            else return controller_->apply_offset_profile(typed,context,save);
        },plan.state->driver));
        action.may_have_changed=action.may_have_changed || issued;refresh();return action;
    }catch(const std::exception& error){action.message=std::string("Controller profile Apply: ")+error.what();action.may_have_changed=issued || owned();refresh();return action;}
}
ActionResult I2cSession::verify_i2c_profile(const I2cProfilePlan& plan) {
    if(!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK)return failure("Could not lock GPU for controller profile verification");
    struct Unlock {I2cApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api_,gpu_};
    if(!plan.state || !profile_bound(*plan.state))return failure("Controller profile verification binding changed");
    try{return result(std::visit([&](const auto& typed){
        using T=std::decay_t<decltype(typed)>;
        if constexpr(std::is_same_v<T,Nct3933Plan>)return controller_->verify_nct3933(typed);
        else if constexpr(std::is_same_v<T,Up9512rPlan>)return controller_->verify_up9512r(typed);
        else if constexpr(std::is_same_v<T,Ncp4206Plan>)return controller_->verify_ncp4206(typed);
        else return controller_->verify_offset_profile(typed);
    },plan.state->driver));}
    catch(const std::exception& error){return failure(std::string("Controller profile verification: ")+error.what());}
}
ActionResult I2cSession::verify_i2c_profile(const I2cProfileReceipt& receipt,bool original) {
    if(!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK)return failure("Could not lock GPU for controller receipt verification");
    struct Unlock {I2cApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api_,gpu_};
    if(!receipt.state || !receipt.state->plan || !profile_bound(*receipt.state->plan))return failure("Controller receipt verification binding changed");
    try{return result(std::visit([&](const auto& typed){
        using T=std::decay_t<decltype(typed)>;
        if constexpr(std::is_same_v<T,Nct3933Receipt>)return controller_->verify_nct3933(typed,original);
        else if constexpr(std::is_same_v<T,Up9512rReceipt>)return controller_->verify_up9512r(typed,original);
        else if constexpr(std::is_same_v<T,Ncp4206Receipt>)return controller_->verify_ncp4206(typed,original);
        else return controller_->verify_offset_profile(typed,original);
    },receipt.state->driver));}
    catch(const std::exception& error){return failure(std::string("Controller receipt verification: ")+error.what());}
}
ActionResult I2cSession::restore_i2c_profile(const I2cProfileReceipt& receipt) {
    if(!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK)return failure("Could not lock GPU for controller profile Undo");
    struct Unlock {I2cApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api_,gpu_};
    if(!receipt.state || !receipt.state->plan || !profile_bound(*receipt.state->plan))return failure("Controller Undo belongs to another session, route or selection");
    ActionResult action;
    try {action=result(std::visit([&](const auto& typed){
        using T=std::decay_t<decltype(typed)>;
        if constexpr(std::is_same_v<T,Nct3933Receipt>)return controller_->restore_nct3933(typed);
        else if constexpr(std::is_same_v<T,Up9512rReceipt>)return controller_->restore_up9512r(typed);
        else if constexpr(std::is_same_v<T,Ncp4206Receipt>)return controller_->restore_ncp4206(typed);
        else return controller_->restore_offset_profile(typed);
    },receipt.state->driver));if(action.ok&&profile_receipt_==receipt.state)profile_receipt_.reset();refresh();return action;}
    catch(const std::exception& error){action.message=std::string("Controller profile Undo: ")+error.what();action.may_have_changed=owned();refresh();return action;}
}
ActionResult I2cSession::release() {
    if(icx_receipt_)return restore_icx_profile({icx_receipt_});
    if (!owned()) { ActionResult r; r.ok=true; r.message="No owned I2C request"; return r; }
    if (!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK) return failure("Could not lock GPU for I2C restoration");
    struct Unlock { I2cApi& api; dc_gpu* gpu; ~Unlock(){ api.unlock(gpu); } } unlock{api_,gpu_};
    bool changed=false;std::string controller_message;
    if(controller_ && controller_->owned()) {
        auto restored=controller_->restore();changed=restored.may_have_changed;
        if(!restored.ok){refresh();data_.message=restored.message;return result(restored);}
        controller_message=restored.message;
    }
    if (service_->has_owned_control()) {
        auto restored=service_->restore(clock_request()); changed=restored.may_have_changed;
        if (!restored.ok) { refresh(); data_.message=restored.message; return result(restored); }
    }
    if(verify_hold_) {
        std::string reason;
        if(!verify_hold_->release(reason))return failure(reason);
    }
    if (data_.clock_owned) {
        dc_clk_lock_status status{};
        if (!bound() || !api_.clock_status || api_.clock_status(gpu_,&status)!=DC_OK || status.error[0])
            return failure("Controller restored; clock ownership unreadable, retry release");
        if (status.locked && (status.min_mhz!=data_.clock_mhz || status.max_mhz!=data_.clock_mhz))
            return failure("Controller restored; frequency request changed externally and was not released");
        if (status.locked) {
            dc_outcome out{}; changed=true;++clock_epoch_;
            if (!api_.release || api_.release(gpu_,0,&out)!=DC_OK || !out.ok ||
                api_.clock_status(gpu_,&status)!=DC_OK || status.error[0] || status.locked)
                return failure("Controller restored; clock release unverified, retry release");
        }
        data_.clock_owned=false; data_.clock_mhz=0;
    }
    profile_receipt_.reset();refresh(); data_.message=controller_message.empty()?"Captured controller state restored; owned clock clamp released":controller_message+"; owned clock clamp released";
    auto r=failure(data_.message); r.ok=true; r.may_have_changed=changed; return r;
}
I2cFullProfileCapture I2cSession::capture_full_profile() {
    I2cFullProfileCapture out;
    if(!data_.selected || data_.family==I2cFamily::icx)return out;
    try {
        if(!api_.lock||!api_.unlock||api_.lock(gpu_)!=DC_OK)throw std::runtime_error("Could not lock selected GPU for I2C profile capture");
        struct Unlock {I2cApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api_,gpu_};
        ReadOnlyScope readonly(discovery_readonly_);
        if(!bound())throw std::runtime_error("Selected GPU identity changed before I2C capture");
        const auto route=*data_.selected;const auto revision=data_.selection_revision;
        const auto check=[&]{if(!bound()||!data_.selected||data_.selection_revision!=revision||data_.selected->port!=route.port||data_.selected->addr7!=route.addr7)throw std::runtime_error("I2C profile target/selection changed during capture");};
        const auto word=[&](uint8_t reg,size_t n){std::vector<uint8_t> bytes;for(unsigned attempt=0;attempt<2;++attempt){check();if(read_bytes(route,reg,n,bytes)){uint32_t result=0;for(size_t i=0;i<n;++i)result|=uint32_t(bytes[i])<<(8*i);return result;}}throw std::runtime_error("Selected controller configuration read failed");};
        const auto require_result=[](const I2cResult& r){if(!r.ok)throw std::runtime_error(r.message);};
        ProfileJson document;bool requires_xoc=false;
        if(data_.family==I2cFamily::nct3933){
            if(!controller_)throw std::runtime_error("Selected NCT driver unavailable");Nct3933State current;require_result(controller_->capture_nct3933(current));
            document=python_i2c_profile_identity(data_.family,route);document["format"]="i2c.current_dac_control";document["current_dac_control"]={{"outputs",current.outputs},{"configuration",current.configuration}};(void)parse_python_nct3933_profile(document);
        }else if(data_.family==I2cFamily::up9512r){
            if(!controller_)throw std::runtime_error("Selected uP driver unavailable");Up9512rControl current;require_result(controller_->capture_up9512r(current));
            const auto lock=word(0x39,1);if(lock!=word(0x39,1))throw std::runtime_error("uP9512R lock changed during capture");if(lock!=0x94){check();return out;}
            document=python_i2c_profile_identity(data_.family,route);document["format"]="i2c.multi_state_offset";document["control"]={{"kind","up9512r-offset-v1"},{"offsets_mv",current.offsets_mv},{"enabled",current.enabled}};(void)parse_python_up9512r_profile(document);requires_xoc=current.enabled&&std::any_of(current.offsets_mv.begin(),current.offsets_mv.end(),[](auto v){return v>50;});
        }else if(data_.family==I2cFamily::ncp4206){
            if(!controller_)throw std::runtime_error("Selected NCP driver unavailable");Ncp4206State current;require_result(controller_->capture_ncp4206(current));
            document=python_i2c_profile_identity(data_.family,route);document["control"]={{"kind","absolute_vid"},{"command",current.command},{"enabled",current.enabled}};(void)parse_python_ncp4206_profile(document);requires_xoc=current.enabled&&(1600.0-(int(current.command)-2)*6.25>1281);
        }else if(data_.family==I2cFamily::mp2888||data_.family==I2cFamily::mp29816){
            ControllerDriver* reader=controller_.get();std::unique_ptr<ControllerDriver> canonical;
            if(data_.family==I2cFamily::mp2888&&data_.control_profile=="mp2888a-pmbus"){
                if(service_->has_owned_control())throw std::runtime_error("MP2888 manual voltage is session-only; not saved");const auto status=service_->status();
                if(!status.readable||status.stale)throw std::runtime_error("MP2888 manual voltage state unreadable");if(status.enabled)throw std::runtime_error("MP2888 manual voltage is session-only; not saved");
                canonical=driver(I2cFamily::mp2888,route);if(!canonical||!canonical->probe())throw std::runtime_error("Canonical MP2888 coarse-offset read failed");reader=canonical.get();
            }else if(data_.control_profile!=(data_.family==I2cFamily::mp2888?"mp2888a-offset":"controller"))throw std::runtime_error("Selected custom recipe has no released profile identity");
            if(!reader)throw std::runtime_error("Selected coarse-offset driver unavailable");int page=-1,scale=-1;
            if(data_.family==I2cFamily::mp29816){const auto first_page=word(0,1),selector=word(0x29,2);if(first_page!=word(0,1)||selector!=word(0x29,2))throw std::runtime_error("MP29816 PAGE/scale changed during capture");page=static_cast<int>(first_page);scale=static_cast<int>((selector>>10)&7);}
            const auto current=reader->read(xoc());if(!current.readable||current.stale)throw std::runtime_error("Selected coarse-offset state unavailable");if(data_.family==I2cFamily::mp29816&&scale!=1){check();return out;}const auto field=std::find_if(current.fields.begin(),current.fields.end(),[](const auto& f){return f.key=="offset"||f.key=="offset_mv";});
            if(field==current.fields.end()||!field->value||field->stale||!std::isfinite(*field->value))throw std::runtime_error("Selected coarse offset not captured");
            document=python_i2c_profile_identity(data_.family,route,page,scale);document["offset_mv"]=*field->value;(void)parse_python_offset_profile(document);requires_xoc=*field->value<(data_.family==I2cFamily::mp2888?-200:-50)||*field->value>(data_.family==I2cFamily::mp2888?100:50);
        }else throw std::runtime_error("Selected controller has no qualified full-profile capture");
        check();out.i2c=std::move(document);out.requires_xoc=requires_xoc;
    }catch(const std::exception& error){out.i2c=nullptr;out.incomplete.push_back(std::string("I2C configured state NOT captured: ")+error.what());}
    return out;
}
ActionResult I2cSession::execute(const Command& cmd) {
    if(cmd.i2c_selection_revision && cmd.i2c_selection_revision!=data_.selection_revision)return failure("Controller selection or recipe changed; no operation issued");
    if((icx_receipt_ || profile_receipt_) && owned() && (cmd.operation==Command::Operation::i2c_apply || cmd.operation==Command::Operation::i2c_reset
        || cmd.operation==Command::Operation::i2c_verify || cmd.operation==Command::Operation::i2c_apply_profile
        || cmd.operation==Command::Operation::i2c_zero_nct_outputs || cmd.operation==Command::Operation::i2c_recipe))
        return failure("Undo or release the owned controller catalog transaction before another controller mutation");
    if(cmd.operation==Command::Operation::i2c_gate) {
        if(!cmd.enabled && owned()){auto restored=release();if(!restored.ok)return restored;}
        data_.gate=cmd.enabled;service_->set_gate(cmd.enabled);
        data_.status.writable=cmd.enabled && data_.verified && data_.status.request_eligible;
        auto action=failure(cmd.enabled?"I2C enabled; choose an IC, then Connect / Scan":"I2C disabled");action.ok=true;data_.message=action.message;return action;
    }
    if (!data_.available || !bound()) return failure("Native I2C transport or selected GPU unavailable");
    if (cmd.operation!=Command::Operation::i2c_select && cmd.operation!=Command::Operation::i2c_scan && cmd.i2c_route &&
        (!data_.selected || cmd.i2c_route->port!=data_.selected->port || cmd.i2c_route->addr7!=data_.selected->addr7))
        return failure("Selected controller route changed; no write issued");
    if (api_.lock(gpu_)!=DC_OK) return failure("Could not lock selected GPU");
    struct Unlock { I2cApi& api; dc_gpu* gpu; ~Unlock(){ api.unlock(gpu); } } unlock{api_,gpu_};
    ActionResult action;
    struct FinishDiscovery {
        std::function<void()> finish;
        ~FinishDiscovery(){finish();}
    } finish_discovery{[this]{if(discovery_.running){discovery_.running=false;progress();}}};
    if(cmd.operation==Command::Operation::i2c_select) {
        discovery_={};discovery_.running=true;discovery_.total=1;discovery_.candidates=data_.candidates;discovery_.label="Inspecting explicit controller selection";progress();
    }
    try {
    switch(cmd.operation) {
    case Command::Operation::i2c_scan:
        action=scan(cmd);break;
    case Command::Operation::i2c_select:
        if (!cmd.i2c_route || !select(cmd.i2c_family==I2cFamily::unknown?I2cFamily::mp2888:cmd.i2c_family,*cmd.i2c_route,cmd.i2c_recipe)) {
            discovery_.label="Candidate selection unavailable; established route retained, no writes issued";data_.message=discovery_.label;
            return failure(discovery_.label);
        }
        action.ok=true;action.message="Controller selected; physical rail assignment is not inferred";break;
    case Command::Operation::i2c_recipe:
        if(data_.family!=I2cFamily::mp2888 || !data_.selected || (cmd.i2c_recipe!="mp2888a-offset" && cmd.i2c_recipe!="mp2888a-pmbus"))return failure("Unknown controller recipe");
        if(owned()){action=release();if(!action.ok)return action;}
        if(!select(data_.family,*data_.selected,cmd.i2c_recipe,false))return failure("Recipe selection failed after restoration");
        action.ok=true;action.message="Control recipe selected; Verify this method before Apply";break;
    case Command::Operation::i2c_read: action.ok=true; action.message="Controller read refreshed"; break;
    case Command::Operation::i2c_release: return release();
    case Command::Operation::i2c_prepare_profile:
    case Command::Operation::i2c_save_profile:
    case Command::Operation::i2c_load_profile:
    case Command::Operation::i2c_stage_captured:
    case Command::Operation::i2c_apply_profile:action=preset(cmd);break;
    case Command::Operation::i2c_zero_nct_outputs: {
        if (!data_.selected || !data_.gate || data_.family!=I2cFamily::nct3933 || !controller_ || !api_.controller_write)
            return failure("Select an NCT3933U controller and enable I2C first");
        const ControllerContext context{data_.gate,xoc()};
        const char* fields[]={"out1","out2","out3"};
        std::string completed;
        action.ok=true;
        for (const char* field:fields) {
            const auto outcome=controller_->apply({field,0,true},context);
            action.may_have_changed=action.may_have_changed || outcome.may_have_changed;
            if (!outcome.ok) {
                action.ok=false;
                action.message="Zero outputs stopped at "+std::string(field)+": "+outcome.message;
                if (outcome.recovery_pending) action.message+="; restoration pending";
                action.message+="; completed: "+(completed.empty()?std::string("none"):completed);
                break;
            }
            if (!completed.empty()) completed+=", ";
            completed+=field;
        }
        if (action.ok) action.message="Zeroed outputs: "+completed+"; physical voltage unknown";
        break;
    }
    case Command::Operation::i2c_apply:
    case Command::Operation::i2c_reset:
    case Command::Operation::i2c_verify: {
        const bool readonly_verify=cmd.operation==Command::Operation::i2c_verify && controller_ &&
            (data_.family==I2cFamily::icx || data_.family==I2cFamily::nct3933);
        if (!data_.selected || (!data_.gate && !readonly_verify)) return failure("Select a controller and enable I2C first");
        if (controller_) {
            if(!api_.controller_write && !readonly_verify)return failure("Controller write interface unavailable; Read remains available");
            std::string reason;
            if(controller_->needs_clock_clamp() && cmd.operation!=Command::Operation::i2c_verify && !clamp(reason)){refresh();data_.message=reason;return failure(reason);}
            const ControllerContext context{data_.gate,xoc()};
            if(cmd.operation==Command::Operation::i2c_verify && !readonly_verify) {
                if (verify_hold_->owned()) return failure("Verification hold is already active or needs recovery");
                if (!api_.verify_hold.architecture || !api_.verify_hold.telemetry)
                    return failure("Verification hold interfaces unavailable");
                if (!data_.clock_owned && api_.before_clock_acquire && !api_.before_clock_acquire(reason)) {
                    if (reason.empty()) reason="Clock handover was refused";
                    return failure(reason);
                }
                const bool needs_load=data_.family==I2cFamily::mp2888 || data_.family==I2cFamily::mp29816;
                std::optional<double> loaded_voltage;
                if(needs_load) {
                    I2cWarmupCallbacks callbacks;callbacks.bound=[this]{return bound();};callbacks.voltage_mv=[this]{return reference_mv();};
                    callbacks.sample=[this]() -> std::optional<I2cWarmupSample> {
                        dc_telemetry t{};t.size=sizeof(t);
                        if(!bound() || !api_.telemetry || api_.telemetry(gpu_,&t)!=DC_OK || !bound())return{};
                        I2cWarmupSample sample;if(t.present&DC_T_MEM)sample.memory_mhz=t.mem;if(t.present&DC_T_PSTATE)sample.pstate=t.pstate;return sample;
                    };
                    auto warmup=i2c_cuda_warmup(identity_.slot,callbacks);
                    if(!warmup.ok){action=failure(warmup.message);break;}
                    loaded_voltage=warmup.voltage_mv;
                }
                if(!verify_hold_->prepare(reason,data_.family!=I2cFamily::up9512r,needs_load,loaded_voltage)) {
                    std::string cleanup; if(!verify_hold_->release(cleanup))reason+="; "+cleanup;
                    action=failure(reason);break;
                }
            }
            auto outcome=cmd.operation==Command::Operation::i2c_verify?controller_->verify(context):
                controller_->apply({cmd.i2c_field,cmd.value,cmd.operation==Command::Operation::i2c_reset},context);
            action=result(outcome);
            if(cmd.operation==Command::Operation::i2c_verify && !controller_->owned()) {
                std::string cleanup;
                if(!verify_hold_->release(cleanup)){action.ok=false;action.message+="; "+cleanup;}
            }
            if(!controller_->owned() && data_.clock_owned){auto released=release();if(!released.ok){action.ok=false;action.message+="; "+released.message;}}
            break;
        }
        if(cmd.operation==Command::Operation::i2c_reset)return release();
        if (!api_.write) return failure("I2C writes unavailable; read-only inspection remains available");
        if (cmd.operation==Command::Operation::i2c_apply && !data_.verified) return failure("Verify controller response before Apply");
        std::string reason; if (!clamp(reason)) { refresh(); data_.message=reason; return failure(reason); }
        if (cmd.operation==Command::Operation::i2c_verify) {
            I2cVerifyCallbacks callbacks; callbacks.core_clock_mhz=[this] {return observed_clock();}; callbacks.sleep_ms=api_.sleep;
            auto verified=service_->verify(clock_request(),callbacks,{}); data_.verified=verified.ok; action=result(verified);
            if (!service_->has_owned_control()) { auto released=release(); if (!released.ok) {action.ok=false;action.message+="; "+released.message;} }
        } else {
            action=result(service_->apply(cmd.value,observed_clock().has_value(),{}));
            if (!service_->has_owned_control()) {
                auto released=release();
                if (!released.ok) { action.ok=false; action.message+="; "+released.message; }
            }
        }
        break;
    }
    default: return failure("Unknown I2C operation");
    }
    }catch(const DiscoveryCancelled&) {
        discovery_.running=false;discovery_.cancelled=true;discovery_.label="Read-only discovery cancelled; partial candidates are unverified. Established selection retained.";
        data_.candidates=discovery_.candidates;data_.message=discovery_.label;progress();return failure(discovery_.label);
    }
    if(cmd.operation==Command::Operation::i2c_select) {
        discovery_.running=false;discovery_.completed=1;discovery_.label=action.message;progress();
    }
    refresh(); data_.message=action.message; return action;
}
}
