// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Exercise the public C ABI against two synthetic read-only NVAPI getters.
#include <drutacore/model_inputs.h>
#include <cstdio>
#include <cstring>
#include <limits>

namespace {
enum class Mode { normal, no_info, no_control, version, mask, mask_limit, changed_identity, dropped,
                  rows, duplicate, drift, guard, rejected, empty, unrecognized_rows };
Mode mode=Mode::normal;
unsigned info_calls=0,control_calls=0,failures=0,subsystem=0x44556677,arch=0x170;
void* const handle=reinterpret_cast<void*>(0x1234);
#define CHECK(x) do { if (!(x)) { ++failures; std::fprintf(stderr,"line %d: %s\n",__LINE__,#x); } } while(false)

template<class T> void put(uint8_t* bytes,size_t offset,T value) {
    std::memcpy(bytes+offset,&value,sizeof(value));
}
uint32_t word(const uint8_t* bytes) {
    uint32_t result;
    std::memcpy(&result,bytes,sizeof(result));
    return result;
}
int DC_CALL init() {return 0;}
int DC_CALL enumerate(void** handles,unsigned* n) {handles[0]=handle;*n=1;return 0;}
int DC_CALL pci(void*,unsigned* id,unsigned* sub,unsigned* rev,unsigned* ext) {
    *id=0x1e0210de;*sub=subsystem;*rev=*ext=0;return 0;
}
int DC_CALL bus(void*,unsigned* out) {*out=2;return 0;}
int DC_CALL device(void*,unsigned* out) {*out=3;return 0;}
int DC_CALL architecture(void*,void* block) {
    static_cast<uint32_t*>(block)[1]=arch;return 0;
}
struct Fixture {unsigned i;uint8_t fuse,version;};
constexpr Fixture rows[]={{254,0x00,1},{0,0x10,1},{19,0x1a,1},{9,0x18,3},{51,0x19,3}};
int DC_CALL info(void* gpu,void* block) {
    CHECK(gpu==handle);
    ++info_calls;
    auto* bytes=static_cast<uint8_t*>(block);
    CHECK(word(bytes)==0x2f9568);
    if (mode==Mode::dropped && info_calls==1) return -1;
    if (mode==Mode::rejected) return -9;
    if (mode==Mode::changed_identity) ++subsystem;
    if (mode==Mode::guard) bytes[0x2b9568]^=1;
    if (mode==Mode::version) bytes[0]^=1;
    if (mode==Mode::mask_limit) bytes[35]|=0x80;
    if (mode==Mode::empty) return 0;
    for (const auto& f:rows) {
        bytes[4+f.i/8]|=static_cast<uint8_t>(1u<<(f.i%8));
        auto* row=bytes+f.i*0x2bc0;
        put(row,0x128,13u);row[0x77c]=f.fuse;row[0x77d]=f.version;
        if (mode==Mode::unrecognized_rows) row[0x77d]=0xff;
        const bool changed=mode==Mode::drift && info_calls==2 && f.i==254;
        const uint32_t active=f.i==9 ? 0xfffffffau : f.i+(changed ? 1u : 0u);
        row[0x240]=f.i==9 ? 1 : 0;
        put(row,0x244,active);put(row,0x264,active);
        put(row,0x24c,818u);put(row,0x268,818u);
        put(row,0x254,51u);row[0x260]=1;
        if (f.i==254) {row[0x25c]=1;put(row,0x258,254u);}
        if (mode==Mode::duplicate && f.i==0) row[0x77c]=0;
        if (mode==Mode::rows && f.i==0) row[0x264]^=1;
        if (mode==Mode::rows && f.i==19) row[0x260]=0;
    }
    return 0;
}
int DC_CALL control(void* gpu,void* block) {
    CHECK(gpu==handle);
    ++control_calls;
    auto* bytes=static_cast<uint8_t*>(block);
    CHECK(word(bytes)==0x29fc8);
    if (mode==Mode::empty) return 0;
    for (const auto& f:rows) {
        CHECK((bytes[4+f.i/8]&(1u<<(f.i%8)))!=0);
        auto* row=bytes+f.i*0xa0;
        put(row,0x68,mode==Mode::rows && f.i==254 ? 2u : 13u);
        put(row,0x6c,-10.f);
        put(row,0x70,mode==Mode::rows && f.i==51 ? std::numeric_limits<float>::infinity() : 1200.f);
    }
    if (mode==Mode::mask) bytes[7]^=1;
    return 0;
}
void* DC_CALL query(unsigned id) {
    switch(id) {
    case 0x0150e828:return reinterpret_cast<void*>(init);
    case 0xe5ac921f:return reinterpret_cast<void*>(enumerate);
    case 0x2ddfb66e:return reinterpret_cast<void*>(pci);
    case 0x1be0b8e5:return reinterpret_cast<void*>(bus);
    case 0x2a0a350f:return reinterpret_cast<void*>(device);
    case 0xd8265d24:return reinterpret_cast<void*>(architecture);
    case 0xb9da41d6:return mode==Mode::no_info ? nullptr : reinterpret_cast<void*>(info);
    case 0x5d387298:return mode==Mode::no_control ? nullptr : reinterpret_cast<void*>(control);
    default:return nullptr;
    }
}
void* DC_CALL resolve(void*,const char* library,const char* name) {
    if (std::strcmp(library,"nvapi")) return nullptr;
    if (!*name) return reinterpret_cast<void*>(1);
    return std::strcmp(name,"nvapi_QueryInterface")==0 ? reinterpret_cast<void*>(query) : nullptr;
}
struct Capture {
    dc_model_inputs result{},prior{};
    dc_error error{};
    dc_gpu* gpu=nullptr;
    explicit Capture(Mode selected) {
        mode=selected;subsystem=0x44556677;info_calls=control_calls=0;
        CHECK(dc_gpu_open("0000:02:03.0",&gpu)==DC_OK && gpu);
        result.size=sizeof(result);result.target.subsystem_id=0x9876;prior=result;
        error.size=sizeof(error);
    }
    ~Capture() {dc_gpu_close(gpu);}
    dc_status get() {return dc_gpu_model_inputs_get(gpu,&result,&error);}
};
void error_case(Mode selected,dc_status expected,int kind,unsigned infos,unsigned controls) {
    Capture test(selected);
    CHECK(test.get()==expected && test.error.kind==kind);
    CHECK(info_calls==infos && control_calls==controls);
    CHECK(!std::memcmp(&test.result,&test.prior,sizeof(test.result)));
}
}

int main() {
    dc_set_export_resolver(resolve,nullptr);
    constexpr Mode successful[]={Mode::normal,Mode::dropped};
    for (const auto selected:successful) {
        Capture test(selected);
        CHECK(test.get()==DC_OK && test.error.kind==DC_ERR_NONE);
        CHECK(info_calls==(selected==Mode::normal ? 2u : 3u) && control_calls==2);
        CHECK(!std::strcmp(test.result.target.slot,"0000:02:03.0") && test.result.target.device_id==0x1e02);
        for (const auto& row:test.result.rows) CHECK(row.state==DC_MODEL_READY);
        const auto& speedo=test.result.rows[DC_MODEL_SPEEDO];
        CHECK(speedo.index==254 && speedo.active==254 && speedo.raw_hardware==818 &&
              speedo.default_value==51 && speedo.override_present==1 && speedo.override_value==254 &&
              speedo.minimum==-10.f && speedo.maximum==1200.f);
        CHECK(test.result.rows[DC_MODEL_IDDQ_NVVDD].active==-6);
    }
    error_case(Mode::no_info,DC_E_UNAVAILABLE,DC_ERR_UNSUPPORTED,0,0);
    error_case(Mode::no_control,DC_E_UNAVAILABLE,DC_ERR_UNSUPPORTED,0,0);
    error_case(Mode::version,DC_E_FAILED,DC_ERR_MALFORMED,1,0);
    error_case(Mode::mask_limit,DC_E_FAILED,DC_ERR_MALFORMED,1,0);
    error_case(Mode::guard,DC_E_FAILED,DC_ERR_MALFORMED,1,0);
    error_case(Mode::mask,DC_E_FAILED,DC_ERR_MALFORMED,1,1);
    error_case(Mode::changed_identity,DC_E_FAILED,DC_ERR_IDENTITY,1,0);
    error_case(Mode::rejected,DC_E_UNAVAILABLE,DC_ERR_UNSUPPORTED,1,0);
    {
        Capture test(Mode::rows);
        CHECK(test.get()==DC_OK);
        const auto& r=test.result.rows;
        CHECK(r[DC_MODEL_SPEEDO].state==DC_MODEL_INVALID && r[DC_MODEL_SPEEDO1].state==DC_MODEL_INVALID &&
              r[DC_MODEL_SPEEDO2].state==DC_MODEL_NOT_READY && r[DC_MODEL_IDDQ_MSVDD].state==DC_MODEL_INVALID);
        CHECK(r[DC_MODEL_IDDQ_NVVDD].state==DC_MODEL_READY && r[DC_MODEL_SPEEDO].raw_hardware==0);
    }
    {
        Capture test(Mode::duplicate);
        CHECK(test.get()==DC_OK && test.result.rows[DC_MODEL_SPEEDO].state==DC_MODEL_AMBIGUOUS &&
              test.result.rows[DC_MODEL_SPEEDO].index==UINT32_MAX && test.result.rows[DC_MODEL_SPEEDO1].state==DC_MODEL_ABSENT);
    }
    {
        Capture test(Mode::drift);
        CHECK(test.get()==DC_OK && test.result.rows[DC_MODEL_SPEEDO].state==DC_MODEL_NOT_READY &&
              test.result.rows[DC_MODEL_SPEEDO].active==0 && test.result.rows[DC_MODEL_IDDQ_NVVDD].state==DC_MODEL_READY);
    }
    constexpr Mode absent[]={Mode::empty,Mode::unrecognized_rows};
    for (const auto selected:absent) {
        Capture test(selected);
        CHECK(test.get()==DC_OK);
        for (const auto& row:test.result.rows) CHECK(row.state==DC_MODEL_ABSENT && row.index==UINT32_MAX);
    }
    arch=0x110;
    error_case(Mode::normal,DC_E_UNAVAILABLE,DC_ERR_UNSUPPORTED,0,0);
    dc_set_export_resolver(nullptr,nullptr);
    return failures ? 1 : 0;
}
