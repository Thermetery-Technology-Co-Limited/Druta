/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_verify_hold.h"
#include <algorithm>
#include <cmath>
#include <utility>
namespace druta::ui {
I2cVerifyHold::I2cVerifyHold(I2cVerifyHoldApi api,dc_gpu* gpu):api_(std::move(api)),gpu_(gpu) {}
bool I2cVerifyHold::bound() const { return gpu_ && api_.bound && api_.bound(); }
std::optional<ControllerOperatingPoint> I2cVerifyHold::check() const {
    if(!bound() || !api_.telemetry) return {};
    if(kind_==Kind::vf) {
        dc_vf_lock_status status{};
        if(!api_.vf_status || api_.vf_status(gpu_,1,held_.domain,&status)!=DC_OK || status.error[0] || !status.locked ||
           status.lock.domain!=held_.domain || status.lock.lock_mode!=held_.lock_mode || status.lock.volt_uv!=held_.volt_uv) return {};
    } else if(kind_==Kind::legacy) {
        int32_t held=0; if(!api_.legacy_owned || api_.legacy_owned(gpu_,&held)!=DC_OK || !held) return {};
    } else return {};
    dc_telemetry t{}; t.size=sizeof(t);
    if(api_.telemetry(gpu_,&t)!=DC_OK || !bound() || !(t.present&DC_T_PSTATE) || t.pstate!=0) return {};
    ControllerOperatingPoint p; p.pstate=0;
    if((t.present&DC_T_CORE) && t.core) p.core_mhz=t.core;
    if((t.present&DC_T_MEM) && t.mem) p.memory_mhz=t.mem;
    return p;
}
bool I2cVerifyHold::prepare(std::string& reason,bool require_stable_clocks,bool require_loaded_voltage,std::optional<double> loaded_voltage_mv) {
    if(kind_!=Kind::none || owned_) { reason="Verification hold is already active or needs recovery"; return false; }
    if(require_loaded_voltage && (!loaded_voltage_mv || !std::isfinite(*loaded_voltage_mv) || *loaded_voltage_mv<=0)) { reason="A completed settled GPU workload and its observed voltage are required before verification"; return false; }
    if(!bound() || !api_.architecture || !api_.telemetry || !api_.sleep) { reason="Verification hold interfaces unavailable"; return false; }
    uint32_t arch=0; if(api_.architecture(gpu_,&arch)!=DC_OK || !arch || !bound()) { reason="GPU architecture unavailable; retry verification"; return false; }
    if(arch==2) {
        int32_t supported=0, existing=0;
        if(!api_.legacy_supported || !api_.legacy_owned || !api_.legacy_request || !api_.legacy_release ||
           api_.legacy_supported(gpu_,&supported)!=DC_OK || !supported || api_.legacy_owned(gpu_,&existing)!=DC_OK) {
            reason="Legacy P0 hold unavailable or ownership unreadable"; return false;
        }
        kind_=Kind::legacy;
        if(!existing) {
            int32_t acquired=0; dc_outcome out{};
            const auto status=api_.legacy_request(gpu_,&acquired,&out); owned_=acquired!=0;
            if(status!=DC_OK || !out.ok || !owned_) { reason="Legacy P0 request failed; release any owned request before retry"; if(!owned_)kind_=Kind::none; return false; }
        }
    } else {
        if(!api_.vf_status || !api_.clock_status || !api_.vf_entry || !api_.vf_set || !api_.vf_pending ||
           !api_.vf_recovery_get || !api_.vf_recovery_set || !api_.vf_recover) { reason="V/F hold interfaces unavailable"; return false; }
        if(api_.vf_pending(gpu_)) { reason="Existing V/F hold recovery is pending"; return false; }
        dc_vf_lock_status state{};
        if(api_.vf_status(gpu_,0,0,&state)!=DC_OK || state.error[0] || !bound()) { reason="Existing V/F hold state unreadable"; return false; }
        if(state.locked) { held_=state.lock; if(held_.lock_mode!=3 || !held_.volt_uv) { reason="Existing V/F hold has unknown mode or value"; return false; } kind_=Kind::vf; }
        else {
            dc_clk_lock_status clock{};
            if(api_.clock_status(gpu_,&clock)!=DC_OK || clock.error[0] || clock.locked) { reason="An existing frequency hold is active or unreadable; preserve it and retry"; return false; }
            // P0 is confirmed after acquiring the hold; CUDA may have ended in P2.
            int32_t has=0; double mv=0; dc_vf_curve curve{}; curve.size=sizeof(curve);
            if(loaded_voltage_mv) { has=1; mv=*loaded_voltage_mv; }
            if((!loaded_voltage_mv && (!api_.vcore || api_.vcore(gpu_,&has,&mv)!=DC_OK)) || !api_.curve || !has || !std::isfinite(mv) || mv<=0 ||
               api_.curve(gpu_,&curve)!=DC_OK || curve.error[0] || !curve.count || curve.count>DC_VFP_POINTS || !bound()) {
                reason="Loaded voltage or current V/F curve unavailable; retry with a readable operating point"; return false;
            }
            double selected=0; for(uint32_t i=0;i<curve.count;++i) if(std::isfinite(curve.points[i].volt_mv) && curve.points[i].volt_mv>selected && curve.points[i].volt_mv<=mv) selected=curve.points[i].volt_mv;
            if(selected<=0 || selected*1000>double(UINT32_MAX)) { reason="No V/F point at or below the observed voltage"; return false; }
            dc_vf_point_lock previous{};
            if(api_.vf_entry(gpu_,6,&previous)!=DC_OK || previous.domain!=6 || previous.lock_mode!=0 || !bound()) { reason="V/F verification target unavailable or in use"; return false; }
            const auto target=static_cast<uint32_t>(std::llround(selected*1000));
            recovery_={}; recovery_.size=sizeof(recovery_); recovery_.pending=1; recovery_.domain=6;
            recovery_.previous_mode=previous.lock_mode; recovery_.previous_uv=previous.volt_uv; recovery_.requested_mode=3; recovery_.requested_uv=target; recovery_.verification=1;
            held_=previous; held_.domain=6; held_.lock_mode=3; held_.volt_uv=target;
            kind_=Kind::vf; owned_=true; dc_outcome outcome{};
            if(api_.vf_set(gpu_,target,1,6,&outcome)!=DC_OK || !outcome.ok || !bound()) {
                reason="Temporary V/F hold failed or is uncertain; release its recovery before retry"; return false;
            }
        }
    }
    std::optional<ControllerOperatingPoint> last; int stable=0;
    for(int n=0;n<150;++n) {
        auto p=check();
        bool valid=p && (!require_stable_clocks || (p->core_mhz && p->memory_mhz));
        bool same=valid && (!require_stable_clocks || (last && p->core_mhz==last->core_mhz && p->memory_mhz==last->memory_mhz));
        stable=same ? stable+1 : (valid?1:0); if(stable>=2) return true;
        last=p; api_.sleep(100);
    }
    reason="Exact hold and stable physical P0 were not observed; controller verification was not started"; return false;
}
bool I2cVerifyHold::release(std::string& reason) {
    if(!owned_) { kind_=Kind::none; reason="No temporary verification hold owned"; return true; }
    if(!bound()) { reason="Selected GPU binding unavailable; verification hold recovery retained"; return false; }
    dc_outcome outcome{};
    if(kind_==Kind::legacy) {
        if(!api_.legacy_release || api_.legacy_release(gpu_,&outcome)!=DC_OK || !outcome.ok) { reason="Legacy P0 release failed; recovery retained"; return false; }
    } else if(kind_==Kind::vf) {
        if(!api_.vf_pending || !api_.vf_recovery_get || !api_.vf_recovery_set || !api_.vf_recover) { reason="V/F recovery interface unavailable"; return false; }
        if(api_.vf_pending(gpu_)) {
            dc_vf_lock_recovery current{}; current.size=sizeof(current);
            if(api_.vf_recovery_get(gpu_,&current)!=DC_OK || !current.pending || current.domain!=recovery_.domain ||
               current.previous_mode!=recovery_.previous_mode || current.previous_uv!=recovery_.previous_uv ||
               current.requested_mode!=recovery_.requested_mode || current.requested_uv!=recovery_.requested_uv) {
                reason="A different V/F recovery is pending; leaving it untouched"; return false;
            }
        } else if(api_.vf_recovery_set(gpu_,&recovery_)!=DC_OK) { reason="Could not register exact V/F restoration; recovery retained"; return false; }
        if(api_.vf_recover(gpu_,&outcome)!=DC_OK || !outcome.ok || api_.vf_pending(gpu_) || !bound()) { reason="Exact V/F hold restoration failed; recovery retained"; return false; }
    } else { reason="Unknown verification hold ownership"; return false; }
    owned_=false; kind_=Kind::none; reason="Temporary verification hold restored"; return true;
}
}
