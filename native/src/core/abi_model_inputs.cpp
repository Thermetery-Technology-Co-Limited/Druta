// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Read-only VFE input inventory. Packet layouts and selectors follow vfe_inputs.py.
#include "drutacore/model_inputs.h"
#include "backend/vbios_image.h"
#include "core/abi_internal.h"
#include "core/mutex.h"
#include "core/slot.h"
#include <array>
#include <cmath>
#include <cstring>
#include <tuple>

namespace {
using druta::backend::Gpu;
using druta::backend::GuardedBuffer;
using druta::backend::Target;
using Getter = int(DRUTA_CDECL*)(void*,void*);
constexpr uint32_t info_id=0xb9da41d6,control_id=0x5d387298;
constexpr uint32_t info_version=0x2f9568,control_version=0x29fc8;
constexpr size_t info_size=0x2b9568,control_size=0x9fc8,info_stride=0x2bc0,control_stride=0xa0;
constexpr unsigned capacity=255,mask_size=32;
static_assert((capacity-1)*info_stride+0x77e<=info_size && (capacity-1)*control_stride+0xa0<=control_size);

struct Failure {int kind;std::string message;};
Target current_target(Gpu& gpu) {
    const auto bound=druta::backend::bind_vbios_target(gpu);
    if (!bound.target) throw Failure{static_cast<int>(bound.error.kind),bound.error.message};
    const auto slot=druta::parse_slot(bound.target->slot);
    if (!slot || slot->domain) throw Failure{DC_ERR_IDENTITY,"NVAPI did not identify the selected PCI segment"};
    if (!druta::backend::probe_architecture(bound.target->architecture))
        throw Failure{DC_ERR_UNSUPPORTED,"This architecture has no known VFE model-input format"};
    return *bound.target;
}
void check_target(Gpu& gpu,const Target& before) {
    const auto now=current_target(gpu);
    if (now.handle!=before.handle || now.slot!=before.slot || now.architecture!=before.architecture ||
        now.device_id!=before.device_id || now.subsystem_id!=before.subsystem_id)
        throw Failure{DC_ERR_IDENTITY,"Selected model-input target changed during capture"};
}
void get(Gpu& gpu,const Target& target,Getter getter,GuardedBuffer& bytes,uint32_t version) {
    bytes.set_word(0,version);
    const int status=getter(target.handle,bytes.data());
    if (!bytes.guards_intact()) throw Failure{DC_ERR_MALFORMED,"Model-input getter changed its buffer guard"};
    check_target(gpu,target);
    if (status) throw Failure{status==-3 || status==-9 || status==-104 ? DC_ERR_UNSUPPORTED : DC_ERR_TRANSIENT,
                              "Model-input getter returned NVAPI status "+std::to_string(status)};
    if (bytes.word(0)!=version) throw Failure{DC_ERR_MALFORMED,"Model-input getter returned a different version"};
}
template<class T> T load(const uint8_t* p) {
    T value;
    std::memcpy(&value,p,sizeof(value));
    return value;
}
int64_t typed_value(const uint8_t* p) {
    return *p ? int64_t(load<int32_t>(p+4)) : int64_t(load<uint32_t>(p+4));
}
int model_role(uint8_t fuse,uint8_t version_field) {
    if (version_field==1) {
        if (fuse==0) return DC_MODEL_SPEEDO;
        if (fuse==0x10) return DC_MODEL_SPEEDO1;
        if (fuse==0x1a) return DC_MODEL_SPEEDO2;
    } else if (version_field==3) {
        if (fuse==0x18) return DC_MODEL_IDDQ_NVVDD;
        if (fuse==0x19) return DC_MODEL_IDDQ_MSVDD;
    }
    return -1;
}
struct Reading {
    dc_model_input row{};
    std::array<uint32_t,8> configuration{};
};
using Sample=std::array<Reading,DC_MODEL_INPUT_COUNT>;
void unavailable(dc_model_input& row,uint32_t state,uint32_t index,const char* why) {
    row={};row.state=state;row.index=index;DC_COPY(row.reason,why);
}
Sample capture(Gpu& gpu,const Target& target,Getter info_get,Getter control_get) {
    GuardedBuffer info(info_size),control(control_size);
    get(gpu,target,info_get,info,info_version);
    const auto* mask=info.data()+4;
    if (mask[31]&0x80) throw Failure{DC_ERR_MALFORMED,"VFE INFO mask exceeds 255 variables"};
    std::memcpy(control.data()+4,mask,mask_size);
    get(gpu,target,control_get,control,control_version);
    if (std::memcmp(mask,control.data()+4,mask_size))
        throw Failure{DC_ERR_MALFORMED,"VFE INFO and CONTROL variable masks disagree"};
    Sample sampled{};
    for (auto& input:sampled) unavailable(input.row,DC_MODEL_ABSENT,UINT32_MAX,"Selector absent");
    for (uint32_t i=0;i<capacity;++i) {
        if (!(mask[i/8]&(1u<<(i%8)))) continue;
        const auto* def=info.data()+i*info_stride;
        const auto* ctl=control.data()+i*control_stride;
        if (load<uint32_t>(def+0x128)!=13) continue;
        const int role=model_role(def[0x77c],def[0x77d]);
        if (role<0) continue;
        auto& current=sampled[role];
        auto& row=current.row;
        if (row.state!=DC_MODEL_ABSENT) {
            unavailable(row,DC_MODEL_AMBIGUOUS,UINT32_MAX,"Several rows share this sensed-fuse selector");
            continue;
        }
        if (load<uint32_t>(ctl+0x68)!=13) {
            unavailable(row,DC_MODEL_INVALID,i,"INFO and CONTROL variable types disagree");
            continue;
        }
        const auto low=load<float>(ctl+0x6c),high=load<float>(ctl+0x70);
        if (!std::isfinite(low) || !std::isfinite(high) || low>high) {
            unavailable(row,DC_MODEL_INVALID,i,"Invalid model-input range");
            continue;
        }
        if (def[0x240]>1 || def[0x248]>1 || def[0x250]>1 || def[0x25c]>1 || def[0x260]>1 ||
            load<uint32_t>(def+0x244)!=load<uint32_t>(def+0x264) ||
            load<uint32_t>(def+0x24c)!=load<uint32_t>(def+0x268)) {
            unavailable(row,DC_MODEL_INVALID,i,"Invalid flags or inconsistent raw/evaluated mirrors");
            continue;
        }
        if (!def[0x260]) {
            unavailable(row,DC_MODEL_NOT_READY,i,"Driver has not completed the fuse-version check");
            continue;
        }
        row={};row.state=DC_MODEL_READY;row.index=i;row.minimum=low;row.maximum=high;
        row.active=typed_value(def+0x240);row.raw_hardware=typed_value(def+0x248);
        row.default_value=typed_value(def+0x250);
        row.override_present=def[0x25c];row.override_value=load<uint32_t>(def+0x258);
        current.configuration={uint32_t(def[0x240]|(def[0x248]<<1)|(def[0x250]<<2)),def[0x23c],def[0x26c],
                               load<uint32_t>(def+0x374),load<uint32_t>(def+0x378),ctl[0x94],
                               load<uint32_t>(ctl+0x98),load<uint32_t>(ctl+0x9c)};
    }
    return sampled;
}
auto fields(const dc_model_input& r) {
    return std::tie(r.state,r.index,r.active,r.raw_hardware,r.default_value,r.override_present,r.override_value,
                    r.minimum,r.maximum);
}
dc_model_inputs read(Gpu& gpu) {
    const auto target=current_target(gpu);
    const auto info=gpu.nvapi.get<Getter>(info_id),control=gpu.nvapi.get<Getter>(control_id);
    if (!info || !control) throw Failure{DC_ERR_UNSUPPORTED,"VFE model-input INFO or CONTROL getter is unavailable"};
    const auto first=capture(gpu,target,info,control),second=capture(gpu,target,info,control);
    dc_model_inputs out{};out.size=sizeof(out);out.target.size=sizeof(out.target);
    out.target.handle=uint64_t(reinterpret_cast<uintptr_t>(target.handle));
    out.target.architecture=target.architecture;out.target.device_id=target.device_id;
    out.target.subsystem_id=target.subsystem_id;DC_COPY(out.target.slot,target.slot);
    for (size_t i=0;i<second.size();++i) {
        const auto& a=first[i];const auto& b=second[i];
        if (fields(a.row)==fields(b.row) && a.configuration==b.configuration)
            out.rows[i]=b.row;
        else unavailable(out.rows[i],DC_MODEL_NOT_READY,a.row.index==b.row.index ? b.row.index : UINT32_MAX,
                         "Model input changed between reads");
    }
    return out;
}
void diagnostic(dc_error* out,int kind,const std::string& message) {
    if (!out) return;
    *out={};out->size=sizeof(*out);out->kind=kind;
    out->fixed=kind==DC_ERR_MALFORMED || kind==DC_ERR_UNSUPPORTED;
    DC_COPY(out->message,message);
}
} // namespace

extern "C" DC_API dc_status DC_CALL dc_gpu_model_inputs_get(dc_gpu* handle,dc_model_inputs* out,dc_error* error) {
    if (!handle || !out || out->size!=sizeof(*out) || (error && error->size!=sizeof(*error))) return DC_E_ARGUMENT;
    try {
        druta::LockGuard guard(handle->gpu.lock());
        for (unsigned attempt=0;;++attempt) {
            try {*out=read(handle->gpu);break;}
            catch (const Failure& e) {if (attempt || e.kind!=DC_ERR_TRANSIENT) throw;}
        }
        diagnostic(error,DC_ERR_NONE,{});return DC_OK;
    } catch (const Failure& e) {
        diagnostic(error,e.kind,e.message);return e.kind==DC_ERR_UNSUPPORTED ? DC_E_UNAVAILABLE : DC_E_FAILED;
    } catch (...) {
        if (error) { *error={};error->size=sizeof(*error);error->kind=DC_ERR_TRANSIENT; }
        return DC_E_FAILED;
    }
}
