/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_session.h"
#include "i2c_catalog.h"
#include "icx_preset.h"
#include "profile_io.h"
#include "i2c_warmup.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#ifdef _WIN32
#include <windows.h>
#endif
namespace druta::ui {
namespace {
ActionResult failure(const std::string& message) { ActionResult r; r.message=message; return r; }
ActionResult result(const I2cResult& source) { ActionResult r; r.ok=source.ok; r.may_have_changed=source.may_have_changed; r.message=source.message; return r; }
}
I2cSession::I2cSession(I2cApi api, dc_gpu* gpu, dc_gpu_identity id)
    : api_(std::move(api)), gpu_(gpu), identity_(id), service_(*this) {
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
    return gpu_ && api_.identity && api_.identity(gpu_,&id)==DC_OK && id.nvapi_ok && id.nvapi_selected &&
        !id.pairing_error[0] && id.nvapi_handle==identity_.nvapi_handle && id.nvapi_devid==identity_.nvapi_devid &&
        id.nvapi_subsys==identity_.nvapi_subsys && id.nvml_selected==identity_.nvml_selected &&
        std::strcmp(id.slot,identity_.slot)==0 && std::strcmp(id.uuid,identity_.uuid)==0;
}
bool I2cSession::read(uint8_t port,uint8_t addr,uint8_t reg,uint8_t width,uint32_t& value) {
    dc_error error{}; error.size=sizeof(error);
    return bound() && api_.read && api_.read(gpu_,port,addr,reg,width,&value,&error)==DC_OK && bound();
}
bool I2cSession::write(uint8_t port,uint8_t addr,uint8_t reg,uint8_t width,uint32_t value) {
    dc_error error{}; error.size=sizeof(error);
    return clock_request() && api_.write && api_.write(gpu_,port,addr,reg,width,value,&error)==DC_OK && bound();
}
bool I2cSession::read_bytes(I2cRoute route, uint8_t reg, size_t count, std::vector<uint8_t>& out) {
    if (!bound() || !count || count > 32) return false;
    std::vector<uint8_t> value(count); dc_error error{}; error.size=sizeof(error);
    if (api_.read_bytes) {
        if (api_.read_bytes(gpu_,route.port,route.addr7,reg,value.data(),static_cast<uint32_t>(count),&error)!=DC_OK || !bound()) return false;
    } else {
        uint32_t raw=0;
        if (count>4 || !read(route.port,route.addr7,reg,static_cast<uint8_t>(count),raw)) return false;
        for(size_t i=0;i<count;++i) value[i]=static_cast<uint8_t>(raw>>(i*8));
    }
    out=std::move(value); return true;
}
bool I2cSession::write_bytes(I2cFamily family,I2cRoute route,uint8_t reg,const std::vector<uint8_t>& bytes) {
    if (!bound() || !api_.controller_write || bytes.empty() || bytes.size()>32 ||
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
bool I2cSession::select(I2cFamily family,I2cRoute route,const std::string& recipe) {
    if(owned())return false;
    // The original MP2888 offset recipe is the default. Manual PMBus is an
    // explicit recipe choice, never inferred from an adapter's marketing name.
    const bool manual=family==I2cFamily::mp2888 && recipe=="mp2888a-pmbus";
    auto chosen=manual ? nullptr : driver(family,route);
    if(manual ? !service_.select(route) : (!chosen || !chosen->probe()))return false;
    controller_=std::move(chosen); data_.selected=route;data_.family=family; data_.verified=false;
    data_.controller={};data_.status={}; ++data_.selection_revision;
    data_.control_profile=manual?"mp2888a-pmbus":family==I2cFamily::mp2888?"mp2888a-offset":"controller";
    data_.needs_clock_clamp=manual || controller_->needs_clock_clamp();
    service_.set_gate(data_.gate); captured_preset_.reset();pending_preset_.reset();prepared_preset_.reset();selection_write_attempted_=false;
    refresh();return true;
}
ActionResult I2cSession::scan(const Command& command) {
    if(owned())return failure("Release the owned I2C request before scanning");
    const auto scope=command.i2c_full_scan ? I2cFamily::unknown : command.i2c_family;
    if(static_cast<size_t>(scope)>=i2c_family_names.size())return failure("Unknown controller scan scope");
    if(!command.i2c_full_scan && data_.selected && (scope==I2cFamily::unknown || scope==data_.family)) {
        // A reconnect is a bounded read of the established selection, not a
        // reason to discard it and infer a different route after a NACK.
        for(unsigned attempt=0;attempt<3;++attempt) {
            auto check=driver(data_.family,*data_.selected);
            if(check && check->probe()) { refresh();auto r=failure("Selected controller reconnected; no scan or write required");r.ok=true;return r; }
            if(attempt<2)api_.sleep(20);
        }
        return failure("Selected controller did not answer; selection and recipe retained. Retry Connect or choose Full scan explicitly.");
    }
    std::vector<I2cCandidate> found;
    for(uint32_t kind=1;kind<=6;++kind) {
        const auto family=static_cast<I2cFamily>(kind);if(scope!=I2cFamily::unknown && family!=scope)continue;
        for(uint8_t port=0;port<8;++port) {
            if(family==I2cFamily::icx && port!=1)continue;
            std::vector<uint8_t> addresses;
            if(family==I2cFamily::nct3933) {for(uint8_t a=0x10;a<=0x15;++a)addresses.push_back(a);}
            else if(family==I2cFamily::icx)addresses={0x2d};
            else {const uint8_t first=family==I2cFamily::up9512r?0x25:family==I2cFamily::mp29816?0x30:0x20;addresses.push_back(first);for(uint8_t a=8;a<=0x77;++a)if(a!=first)addresses.push_back(a);}
            for(const auto address:addresses) {
                auto candidate=driver(family,{port,address});if(!candidate || !candidate->probe())continue;
                I2cCandidate hit;hit.route={port,address};hit.family=kind;hit.description=i2c_family_name(family);found.push_back(std::move(hit));
            }
        }
    }
    data_.candidates=std::move(found);
    ActionResult action;action.ok=true;
    if(data_.candidates.size()==1 && select(static_cast<I2cFamily>(data_.candidates[0].family),data_.candidates[0].route))
        action.message="One controller selected; read and verify this control method before Apply";
    else {controller_.reset();data_.selected.reset();data_.controller={};data_.status={};data_.verified=false;captured_preset_.reset();pending_preset_.reset();++data_.selection_revision;
        action.message=std::to_string(data_.candidates.size())+" controller candidates; select one to inspect";}
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
bool I2cSession::clamp(std::string& reason) {
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
    // Save ownership before SET; an error can follow a dispatch.
    data_.clock_owned=true; data_.clock_mhz=floor;
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
void I2cSession::refresh() {
    if(data_.selected && controller_) {
        data_.controller=controller_->read(xoc());const auto& view=data_.controller;
        data_.verified=view.verified || !view.needs_verification;
        auto& status=data_.status;status.readable=view.readable;status.stale=view.stale;
        status.capable=view.capable;status.owned=view.owned;status.recovery_pending=view.recovery;
        status.response_verified=data_.verified;status.enabled=data_.gate;
        status.request_eligible=view.capable && !view.recovery;
        status.writable=data_.gate && data_.verified && api_.controller_write &&
            std::any_of(view.fields.begin(),view.fields.end(),[](const auto& field){return field.writable;});
        status.message=view.message;
        if(data_.family==I2cFamily::icx && !captured_preset_ && !selection_write_attempted_) {
            try { captured_preset_=capture_icx_preset(view).dump(); } catch(...) { /* partial read is retryable */ }
        }
    } else if(data_.selected) {data_.status=service_.status();data_.controller={};}
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
ActionResult I2cSession::release() {
    if (!owned()) { ActionResult r; r.ok=true; r.message="No owned I2C request"; return r; }
    if (!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK) return failure("Could not lock GPU for I2C restoration");
    struct Unlock { I2cApi& api; dc_gpu* gpu; ~Unlock(){ api.unlock(gpu); } } unlock{api_,gpu_};
    bool changed=false;std::string controller_message;
    if(controller_ && controller_->owned()) {
        auto restored=controller_->restore();changed=restored.may_have_changed;
        if(!restored.ok){refresh();data_.message=restored.message;return result(restored);}
        controller_message=restored.message;
    }
    if (service_.has_owned_control()) {
        auto restored=service_.restore(clock_request()); changed=restored.may_have_changed;
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
            dc_outcome out{}; changed=true;
            if (!api_.release || api_.release(gpu_,0,&out)!=DC_OK || !out.ok ||
                api_.clock_status(gpu_,&status)!=DC_OK || status.error[0] || status.locked)
                return failure("Controller restored; clock release unverified, retry release");
        }
        data_.clock_owned=false; data_.clock_mhz=0;
    }
    refresh(); data_.message=controller_message.empty()?"Captured controller state restored; owned clock clamp released":controller_message+"; owned clock clamp released";
    auto r=failure(data_.message); r.ok=true; r.may_have_changed=changed; return r;
}
ActionResult I2cSession::execute(const Command& cmd) {
    if(cmd.i2c_selection_revision && cmd.i2c_selection_revision!=data_.selection_revision)return failure("Controller selection or recipe changed; no operation issued");
    if(cmd.operation==Command::Operation::i2c_gate) {
        if(!cmd.enabled && owned()){auto restored=release();if(!restored.ok)return restored;}
        data_.gate=cmd.enabled;service_.set_gate(cmd.enabled);
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
    switch(cmd.operation) {
    case Command::Operation::i2c_scan:
        action=scan(cmd);break;
    case Command::Operation::i2c_select:
        if (!cmd.i2c_route || !select(cmd.i2c_family==I2cFamily::unknown?I2cFamily::mp2888:cmd.i2c_family,*cmd.i2c_route,cmd.i2c_recipe)) return failure("Candidate selection failed; no writes issued");
        action.ok=true;action.message="Controller selected; physical rail assignment is not inferred";break;
    case Command::Operation::i2c_recipe:
        if(data_.family!=I2cFamily::mp2888 || !data_.selected || (cmd.i2c_recipe!="mp2888a-offset" && cmd.i2c_recipe!="mp2888a-pmbus"))return failure("Unknown controller recipe");
        if(owned()){action=release();if(!action.ok)return action;}
        if(!select(data_.family,*data_.selected,cmd.i2c_recipe))return failure("Recipe selection failed after restoration");
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
            auto verified=service_.verify(clock_request(),callbacks,{}); data_.verified=verified.ok; action=result(verified);
            if (!service_.has_owned_control()) { auto released=release(); if (!released.ok) {action.ok=false;action.message+="; "+released.message;} }
        } else {
            action=result(service_.apply(cmd.value,observed_clock().has_value(),{}));
            if (!service_.has_owned_control()) {
                auto released=release();
                if (!released.ok) { action.ok=false; action.message+="; "+released.message; }
            }
        }
        break;
    }
    default: return failure("Unknown I2C operation");
    }
    refresh(); data_.message=action.message; return action;
}
}
