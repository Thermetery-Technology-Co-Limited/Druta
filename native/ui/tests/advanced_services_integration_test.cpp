// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// CoreClient integration using injectable ABI only; no GPU or hardware access.
#include "core_client.h"
#include "power_arbitration_service.h"
#include "power_integral_service.h"
#include "voltage_policy_service.h"
#include <algorithm>
#include <cstdio>
#include <cstring>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool ok,const char* why){if(!ok){++failures;std::printf("FAIL: %s\n",why);}}
void put8(std::vector<uint32_t>& words,size_t at,uint32_t value){const auto shift=static_cast<uint32_t>((at%4)*8);words[at/4]=(words[at/4]&~(255u<<shift))|(value<<shift);}
void put16(std::vector<uint32_t>& words,size_t at,uint16_t value){put8(words,at,value&255);put8(words,at+1,value>>8);}
uint16_t get16(const std::vector<uint32_t>& words,size_t at){return static_cast<uint16_t>((words[at/4]>>((at%4)*8))&65535);}
constexpr size_t lower0=0x14+12,upper0=0x14+14;
struct Fake {
    dc_gpu_identity id{};dc_target target{};dc_power_layout layout{};
    std::vector<uint32_t> info=std::vector<uint32_t>(20512/4),control=std::vector<uint32_t>(13876/4);
    uint32_t mask=0;int sets=0,reads=0,resets=0,depth=0,fail_reads=0,abi_failures=0,post_failures=0;
    bool fail_set=false,foreign_after_set=false,unrelated_after_set=false,change_identity_after_set=false;
    bool require_mask=true;int bind_calls=0;
    Fake(){id.size=sizeof(id);id.nvapi_ok=id.nvml_ok=id.nvapi_selected=id.nvml_selected=1;
        std::snprintf(id.slot,sizeof(id.slot),"0000:7e:02.0");std::snprintf(id.uuid,sizeof(id.uuid),"GPU-alternate-policy");
        id.nvapi_handle=0x778899;id.nvapi_devid=0x11223344;id.nvapi_subsys=0x55667788;
        target.size=sizeof(target);std::snprintf(target.slot,sizeof(target.slot),"%s",id.slot);
        target.architecture=7;target.handle=id.nvapi_handle;target.device_id=id.nvapi_devid;target.subsystem_id=id.nvapi_subsys;
        layout.size=sizeof(layout);layout.sizes[0]=20512;layout.sizes[1]=397048;layout.sizes[2]=layout.sizes[3]=13876;
        layout.info_base=0xCC;layout.info_stride=0x104;layout.status_base=0x9C;layout.status_stride=0x1730;
        layout.control_base=0x14;layout.control_stride=0xC4;layout.has_status_mask_word=layout.has_control_mask_word=1;
        layout.status_mask_word=1;layout.control_mask_word=4;
        info[1]=3;control[3]=255;control[4]=3;
        for(size_t policy=0;policy<2;++policy){const size_t a=0xCC+policy*0x104,b=0x14+policy*0xC4;
            put8(info,a,0x30);put8(info,a+4,static_cast<uint32_t>(policy+5));put8(info,a+5,static_cast<uint32_t>(policy+10));put8(info,a+6,static_cast<uint32_t>(policy));
            info[(a+8)/4]=10;info[(a+16)/4]=100000;put8(info,a+0x20,2);put8(info,a+0x22,4);put8(info,a+0x23,5);
            put16(info,a+0x24,1024);put16(info,a+0x26,4096);control[b/4]=static_cast<uint32_t>(policy+5);control[(b+4)/4]=1000;
            put8(control,b+8,2);put8(control,b+9,4);put8(control,b+10,5);put8(control,b+11,0xA5);
            put16(control,b+12,1024);put16(control,b+14,4096);control[(b+40)/4]=0xDEADBEEF;
        }
    }
    dc_gpu* gpu(){return reinterpret_cast<dc_gpu*>(this);}
};
Fake& fake(dc_gpu* gpu){return *reinterpret_cast<Fake*>(gpu);}
dc_status DC_CALL lock(dc_gpu* gpu){++fake(gpu).depth;return DC_OK;}
dc_status DC_CALL unlock(dc_gpu* gpu){--fake(gpu).depth;return DC_OK;}
dc_status DC_CALL identity(const dc_gpu* gpu,dc_gpu_identity* out){*out=reinterpret_cast<const Fake*>(gpu)->id;return DC_OK;}
dc_status DC_CALL bind(dc_gpu* gpu,int32_t,dc_target* out,dc_error*){++fake(gpu).bind_calls;*out=fake(gpu).target;return DC_OK;}
int32_t DC_CALL probe(uint32_t arch){return arch>=4 && arch<=10?1:0;}
dc_status DC_CALL abi(dc_gpu* gpu,dc_power_layout* out,uint32_t* index,dc_error* error){auto& f=fake(gpu);
    if(f.abi_failures-->0){error->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}*out=f.layout;*index=0;return DC_OK;}
dc_status DC_CALL reset(dc_gpu* gpu,int32_t kind){check(kind==DC_TRANSPORT_POWER,"only power transport recovery");++fake(gpu).resets;return DC_OK;}
dc_status DC_CALL mask(dc_gpu* gpu,uint32_t value){fake(gpu).mask=value;return DC_OK;}
dc_status DC_CALL rm(dc_gpu* gpu,uint32_t command,const uint32_t* params,uint32_t param_count,int32_t has_mask,uint32_t policy_mask,int32_t live,uint32_t* out,uint32_t capacity,uint32_t* count,dc_error* error){
    auto& f=fake(gpu);check(f.depth>0,"all policy operations serialized");
    if(command==0x2080E61B){++f.sets;check(param_count==f.control.size() && params && live==1,"SET uses full fresh CONTROL with any-live selector");
        if(f.require_mask && !f.mask){error->kind=DC_ERR_INVALID;return DC_E_FAILED;}
        check(params[4]==1 || params[4]==2,"SET selects exactly one policy");
        // The RM selector changes one row, not the getter's aggregate mask.
        const size_t policy=params[4]==2?1:0,b=(0x14+policy*0xC4)/4;
        std::copy(params+b,params+b+0xC4/4,f.control.begin()+b);
        if(f.foreign_after_set)put16(f.control,upper0,5000);
        if(f.unrelated_after_set){f.control[(0x14+40)/4]=0x12345678;f.unrelated_after_set=false;}
        if(f.change_identity_after_set)std::snprintf(f.id.uuid,sizeof(f.id.uuid),"GPU-replaced");
        f.fail_reads=f.post_failures;f.post_failures=0;
        *count=0;if(f.fail_set){error->kind=DC_ERR_TRANSIENT;std::snprintf(error->message,sizeof(error->message),"SET status uncertain");return DC_E_FAILED;}return DC_OK;
    }
    ++f.reads;if(f.fail_reads-->0){error->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}
    const auto& words=command==0x2080A618?f.info:f.control;
    if(command==0x2080A61A)check(has_mask && policy_mask==f.info[1],"CONTROL GET uses INFO mask");
    if(capacity<words.size())return DC_E_BUFFER;
    std::copy(words.begin(),words.end(),out);*count=static_cast<uint32_t>(words.size());return DC_OK;
}
PowerIntegralApi api(){PowerIntegralApi a;a.lock=lock;a.unlock=unlock;a.identity=identity;a.target_bind=bind;a.probe_architecture=probe;
    a.abi=abi;a.rm=rm;a.transport_reset=reset;a.policy_mask_set=mask;return a;}

Fake* active=nullptr;
uint32_t DC_CALL version(){return DC_ABI_VERSION;}
dc_status DC_CALL enumerate(dc_gpu_entry*,size_t,size_t* count){*count=0;return DC_OK;}
dc_status DC_CALL open_gpu(const char*,dc_gpu** out){*out=active->gpu();return DC_OK;}
void DC_CALL close_gpu(dc_gpu*){}
int arbitration_reads=0;
uint32_t DC_CALL max_words(){return 16;}
dc_status DC_CALL table(dc_gpu*,dc_power_layout* l,dc_error*){++arbitration_reads;l->sizes[0]=16;l->sizes[1]=24;l->sizes[2]=20;l->info_base=8;return DC_OK;}
dc_status DC_CALL get_table(dc_gpu*,int32_t block,uint32_t* out,uint32_t capacity,uint32_t* count,int32_t* present){
    *present=block==DC_CL_TABLE_INFO;*count=*present?capacity:0;
    if(*present)std::fill(out,out+capacity,0);return DC_OK;
}
dc_status DC_CALL geometry(const dc_power_layout*,char*,uint32_t){return DC_OK;}
dc_status DC_CALL decode(const dc_power_layout*,const dc_arbitration_words*,const dc_arbitration_words*,const dc_arbitration_words*,dc_arbitration_report* out){
    *out={};out->size=sizeof(*out);out->schema=1;out->available=1;out->has_mask=1;return DC_OK;
}
dc_status DC_CALL range(dc_gpu*,uint32_t,dc_offset_range* out){out->present=1;out->low=-100;out->high=100;out->current=12;return DC_OK;}
CoreApi core_api(){CoreApi a;a.abi_version=version;a.enumerate=enumerate;a.open=open_gpu;a.close=close_gpu;a.identity=identity;a.range=range;
    a.power_integral=api();a.arbitration={identity,lock,unlock,max_words,table,get_table,geometry,decode,nullptr};return a;}
#if defined(_MSC_VER)
#define NOINLINE __declspec(noinline)
#else
#define NOINLINE __attribute__((noinline))
#endif
NOINLINE void integrated(){
    Fake f;active=&f;arbitration_reads=0;auto backend=create_core_backend_for_test(core_api());std::string error;
    check(backend->open(f.id.slot,error),"open matching adapter");
    check(f.reads==0 && f.sets==0 && arbitration_reads==0,"open does not read diagnostics or write");
    auto initial=backend->read(false);auto detailed=backend->read(true);
    check(f.reads==0 && arbitration_reads==0 && f.sets==0,"ordinary reads never refresh explicit services");
    check(initial.power_integral && initial.voltage_policies && initial.power_arbitration,"initial models published");
    Command c;c.operation=Command::Operation::power_integral_read;
    check(backend->execute(c).ok,"explicit integral read succeeds");
    auto state=backend->read(false);
    check(state.power_integral && state.power_integral->current && state.power_integral->rows.size()==2 && f.reads>0,"integral rows published");
    c.operation=Command::Operation::refresh_power_arbitration;
    check(backend->execute(c).ok,"explicit arbitration read succeeds");
    state=backend->read(false);
    check(state.power_arbitration && state.power_arbitration->freshness==ArbitrationFreshness::current && arbitration_reads==1,"arbitration model published");
    const int reads=f.reads,arbs=arbitration_reads;
    c.operation=Command::Operation::voltage_policy_read;
    check(!backend->execute(c).ok,"missing voltage exports reported unavailable");
    state=backend->read(true);
    check(state.voltage_policies && state.voltage_policies->rows[0].availability!=VoltagePolicyAvailability::available,"voltage failure published");
    check(state.controls[static_cast<size_t>(ControlId::core)].current.has_value(),"missing service API does not remove other controls");
    check(f.reads==reads && arbitration_reads==arbs,"ordinary refresh retains diagnostic models without reread");
    c={};c.operation=Command::Operation::power_integral_apply;c.integral_policy=0;c.integral_upper=true;c.integral_raw=5000;c.expected_request_raw=4096;
    check(backend->execute(c).ok && f.sets==1,"explicit integral apply writes selected field");
    state=backend->read(false);check(state.power_integral->rows[0].upper==5000,"applied integral readback published");
    c.operation=Command::Operation::power_integral_undo;
    check(backend->execute(c).ok && get16(f.control,upper0)==4096 && f.sets==2,"integral undo restores previous field");
    backend->close();check(f.sets==2,"close adds no implicit write");active=nullptr;
}
}
int main(){integrated();std::printf("Advanced services integration: %d failures\n",failures);return failures?1:0;}
