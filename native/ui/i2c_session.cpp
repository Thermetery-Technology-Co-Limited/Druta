/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_session.h"
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
    I2cControlProfile manual;
    manual.id = "mp2888a-pmbus"; manual.label = "MP2888A manual PMBus";
    manual.description = "Nominal voltage requests, not measured voltage. Verify checks response at the driver's lowest clock; Release restores captured state.";
    manual.available = data_.available && api_.write;
    manual.step_mv = I2cControls::nominal_step_mv;
    if (data_.status.capable && data_.status.min_mv && data_.status.max_mv)
        manual.presets_mv = I2cControls::nominal_presets(*data_.status.min_mv, *data_.status.max_mv);
    data_.control_profile = manual.id;
    data_.control_profiles = {std::move(manual)};
}
void I2cSession::refresh() { if (data_.selected) data_.status=service_.status(); publish_control_profiles(); }
ActionResult I2cSession::release() {
    if (!owned()) { ActionResult r; r.ok=true; r.message="No owned I2C request"; return r; }
    if (!api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK) return failure("Could not lock GPU for I2C restoration");
    struct Unlock { I2cApi& api; dc_gpu* gpu; ~Unlock(){ api.unlock(gpu); } } unlock{api_,gpu_};
    bool changed=false;
    if (service_.has_owned_control()) {
        auto restored=service_.restore(clock_request()); changed=restored.may_have_changed;
        if (!restored.ok) { refresh(); data_.message=restored.message; return result(restored); }
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
    refresh(); data_.message="Captured controller state restored; owned clock clamp released";
    auto r=failure(data_.message); r.ok=true; r.may_have_changed=changed; return r;
}
ActionResult I2cSession::execute(const Command& cmd) {
    if (!data_.available || !bound()) return failure("Native I2C transport or selected GPU unavailable");
    if (cmd.operation!=Command::Operation::i2c_select && cmd.operation!=Command::Operation::i2c_scan && cmd.i2c_route &&
        (!data_.selected || cmd.i2c_route->port!=data_.selected->port || cmd.i2c_route->addr7!=data_.selected->addr7))
        return failure("Selected controller route changed; no write issued");
    if (api_.lock(gpu_)!=DC_OK) return failure("Could not lock selected GPU");
    struct Unlock { I2cApi& api; dc_gpu* gpu; ~Unlock(){ api.unlock(gpu); } } unlock{api_,gpu_};
    ActionResult action;
    switch(cmd.operation) {
    case Command::Operation::i2c_scan:
        if (owned()) return failure("Release the owned I2C session before scanning");
        data_.candidates=service_.scan(); action.ok=true;
        if (data_.candidates.size() == 1 && service_.select(data_.candidates.front().route)) {
            data_.selected = data_.candidates.front().route; data_.gate = data_.verified = false;
            action.message = "One compatible controller selected; press Verify response";
        } else action.message=std::to_string(data_.candidates.size())+" controller candidate(s); select a route to inspect";
        break;
    case Command::Operation::i2c_select:
        if (owned() || !cmd.i2c_route || !service_.select(*cmd.i2c_route)) return failure("Candidate selection failed; no writes issued");
        data_.selected=cmd.i2c_route; data_.gate=data_.verified=false; action.ok=true; action.message="Controller candidate selected; physical rail unassigned"; break;
    case Command::Operation::i2c_gate:
        if (!data_.selected) return failure("Select a controller candidate first");
        service_.set_gate(cmd.enabled); data_.gate=cmd.enabled; data_.status.writable=cmd.enabled && data_.verified && data_.status.request_eligible;
        action.ok=true; action.message=cmd.enabled?"I2C armed for this session":"I2C disarmed; Release remains available"; data_.message=action.message; return action;
    case Command::Operation::i2c_read: action.ok=true; action.message="Controller read refreshed"; break;
    case Command::Operation::i2c_release: return release();
    case Command::Operation::i2c_apply:
    case Command::Operation::i2c_verify: {
        if (cmd.operation == Command::Operation::i2c_verify && cmd.enabled && data_.selected) {
            service_.set_gate(true); data_.gate = true;
        }
        if (!data_.selected || !data_.gate) return failure("Select a controller and enable I2C first");
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
