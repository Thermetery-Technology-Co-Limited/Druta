// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weight_capture.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
namespace {
unsigned checks=0;int images=0,infos=0,abis=0,windows=0,registries=0,locks=0,mode=0,resets=0;dc_gpu_identity adapter{};
uint32_t generation=7;
int clock_binds=0,perf_binds=0;
constexpr int kRm26Info=17, kChangingInfoLayout=18;
bool rm26_info_layout(){return mode==kRm26Info||(mode==kChangingInfoLayout&&abis%2==0);}
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
void put(std::vector<uint8_t>& b,size_t at,uint32_t value){std::memcpy(b.data()+at,&value,4);}
dc_status DC_CALL id(const dc_gpu*,dc_gpu_identity* out){*out=adapter;return DC_OK;}
dc_status fill_target(dc_target* target){target->size=sizeof(*target);std::strcpy(target->slot,adapter.slot);target->architecture=generation;target->handle=adapter.nvapi_handle;target->device_id=adapter.nvapi_devid;target->subsystem_id=adapter.nvapi_subsys;return DC_OK;}
dc_status DC_CALL bind(dc_gpu*,int32_t,dc_target* target,dc_error* error){++perf_binds;if(generation<4&&mode!=16){error->kind=DC_ERR_UNSUPPORTED;return DC_E_FAILED;}return fill_target(target);}
dc_status DC_CALL bind_clock(dc_gpu*,dc_target* target,dc_error* error){++clock_binds;if(mode==15&&clock_binds==1){error->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}return fill_target(target);}
int32_t DC_CALL arch(uint32_t a){return a==7;}
dc_status DC_CALL lock(dc_gpu*){++locks;return DC_OK;}dc_status DC_CALL unlock(dc_gpu*){--locks;return DC_OK;}
dc_status DC_CALL reset(dc_gpu*,int32_t kind){CHECK(kind==DC_TRANSPORT_POWER);++resets;return DC_OK;}
dc_status DC_CALL image(dc_gpu*,uint8_t* bytes,uint32_t capacity,uint32_t* count,dc_error* error){++images;CHECK(capacity==DC_VBIOS_IMAGE_CAPACITY);
 if(mode==1){error->kind=DC_ERR_UNSUPPORTED;return DC_E_UNAVAILABLE;}if(mode==2&&images==1){error->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}if(mode==3){error->kind=DC_ERR_MALFORMED;return DC_E_FAILED;}if(mode==4){error->kind=DC_ERR_IDENTITY;return DC_E_FAILED;}
 std::fill_n(bytes,32,uint8_t{0xa5});*count=32;if(mode==5)bytes[31]=uint8_t(images);if(mode==6)++adapter.nvapi_handle;if(mode==14)generation=3;return DC_OK;}
dc_status DC_CALL abi(dc_gpu*,dc_power_layout* layout,uint32_t* index,dc_error* error){
 ++abis;
 if(mode==7){error->kind=DC_ERR_UNSUPPORTED;return DC_E_UNAVAILABLE;}
 const bool rm26=rm26_info_layout();
 layout->size=sizeof(*layout);
 layout->sizes[0]=rm26?5520u:20512u;
 layout->has_commands=1;
 layout->commands[0]=rm26?0x20802618u:0x2080a618u;
 layout->info_base=rm26?0x3cu:0xccu;
 layout->info_stride=rm26?0x84u:(mode==8?0x100u:0x104u);
 *index=0;
 return DC_OK;
}
dc_status DC_CALL rm(dc_gpu*,uint32_t command,const uint32_t* params,uint32_t params_count,int32_t has_mask,uint32_t mask,int32_t any_live,uint32_t* out,uint32_t capacity,uint32_t* count,dc_error* error){++infos;
 CHECK(command==0x2080a618&&!params&&!params_count&&!has_mask&&!mask&&!any_live&&capacity==(rm26_info_layout()?5520u:20512u)/4);
 if(mode==9&&infos==1){error->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}if(mode==10){error->kind=DC_ERR_MALFORMED;return DC_E_FAILED;}
 if(rm26_info_layout()){std::fill_n(out,capacity,0u);*count=capacity;return DC_OK;}
 std::vector<uint8_t> bytes(20512);put(bytes,4,1);put(bytes,0xcc,0x40);put(bytes,0xcc+8,1);put(bytes,0xcc+12,2);put(bytes,0xcc+16,3);put(bytes,0x41cc,1);bytes[0x41d0+7]=0x10;if(mode==11)bytes[0x5000]=uint8_t(infos);std::memcpy(out,bytes.data(),bytes.size());*count=capacity;return DC_OK;}
PowerWeightCaptureApi api(){PowerWeightCaptureApi a;a.evidence.target.identity=id;a.evidence.target.bind=bind;a.clock_bind=bind_clock;a.evidence.target.architecture_supported=arch;a.evidence.target.lock=lock;a.evidence.target.unlock=unlock;a.evidence.target.reset_transport=reset;a.evidence.image=image;a.abi=abi;a.rm=rm;
 a.resolve_windows=[](const dc_gpu_identity&,const dc_target& target){++windows;VoltageScaffoldWindowsTarget value{target.slot,"PCI\\VEN_10DE&DEV_6789\\A","0007"};if(mode==13&&windows>1)value.driver_key="0008";return value;};
 a.registry_read=[](const VoltageScaffoldWindowsTarget&){++registries;if(mode==12)throw VoltageEquationEvidenceError("Registry unavailable",VoltagePolicyAvailability::transient);return PowerWeightRegistrySnapshot{{L"EnableGpuFirmware",4,{1,0,0,0}},{L"romOverride00",3,{1,2,3}}};};return a;}
void fresh(){images=infos=abis=windows=registries=locks=mode=resets=clock_binds=perf_binds=0;generation=7;adapter={};adapter.size=sizeof(adapter);std::strcpy(adapter.slot,"0000:7b:01.1");std::strcpy(adapter.uuid,"GPU-read-alt");adapter.nvapi_ok=adapter.nvapi_selected=1;adapter.nvapi_handle=42;adapter.nvapi_devid=0x6789;adapter.nvapi_subsys=0xaabbccdd;}

int tick_count=0,timing_mode=0;
dc_status DC_CALL p0_tick(dc_gpu*,dc_telemetry* tick) {
    ++tick_count;
    tick->present=DC_T_PSTATE|DC_T_MEM;
    tick->mem=(timing_mode==3&&tick_count>=2)?3725:3724;
    tick->pstate=(timing_mode==1||(timing_mode==2&&tick_count==2))?8:0;
    return DC_OK;
}
void run_timing_capture() {
    auto* gpu=reinterpret_cast<dc_gpu*>(1);
    for(timing_mode=0;timing_mode<=6;++timing_mode) {
        fresh();tick_count=0;
        generation=timing_mode==5?3:4;
        auto a=api();a.telemetry=p0_tick;
        int reads=0;
        a.timing_config=[&](dc_gpu*) {
            ++reads;
            PascalTimingSample words{0,0x2966c296,(timing_mode==4&&reads==2)?0x22003b4au:0x2200394au};
            words.set_word(0,42);
            if(timing_mode!=6||reads==1)words.set_word(2,255);
            words.set_word(4,timing_mode==6&&reads==2?1023:18);
            words.set_word(5,2);
            return words;
        };
        const auto state=make_power_weight_capture(a,adapter,true)(gpu,false);
        CHECK(state.image_availability==VoltagePolicyAvailability::available&&locks==0);
        CHECK(state.timing_sample.has_value()==(timing_mode==0||timing_mode==6));
        if(timing_mode==0) {
            CHECK(reads==2&&tick_count==4);
            const auto sample=*state.timing_sample;
            CHECK(sample.present==0x3f&&sample.memory_mhz==3724&&sample.config1==0x2966c296&&sample.config3==0x2200394a);
            verify_pascal_timing_sample(state,sample);
            verify_pascal_timing_sample(state,{sample.memory_mhz,sample.config1,sample.config3}); // Legacy receipt.
            for(int mismatch=-1;mismatch<6;++mismatch) {
                auto other=sample;
                if(mismatch<0)++other.memory_mhz;
                else other.set_word(mismatch,sample.word(mismatch)^1);
                bool refused=false;
                try {verify_pascal_timing_sample(state,other);} catch(const VoltageEquationEvidenceError&){refused=true;}
                CHECK(refused);
            }
        } else if(timing_mode==6) {
            CHECK(state.timing_sample->present==0x2b);
            auto previous=*state.timing_sample;
            previous.set_word(4,18);
            bool pending=false;
            try {verify_pascal_timing_sample(state,previous);}
            catch(const VoltageEquationEvidenceError& error){pending=error.kind==VoltagePolicyAvailability::transient;}
            CHECK(pending);
        } else if(timing_mode==5) {
            CHECK(state.timing_availability==VoltagePolicyAvailability::unsupported&&reads==0&&tick_count==0);
        } else {
            CHECK(state.timing_availability==VoltagePolicyAvailability::transient);
            CHECK(reads==(timing_mode==1?0:timing_mode==4?2:1));
        }
    }
}
void run_legacy_capture() {
    auto* gpu=reinterpret_cast<dc_gpu*>(1);
    for (const uint32_t architecture : {2u,3u}) {
        fresh();
        generation=architecture;
        mode=15;  // A transient ROM-target read recovers before any image or registry work.
        auto clock=make_power_weight_capture(api(),adapter,true);
        const auto recorded=clock(gpu,false);
        CHECK(recorded.identity.architecture==architecture&&recorded.target.handle==adapter.nvapi_handle&&
              recorded.image_availability==VoltagePolicyAvailability::available&&images==2&&
              clock_binds>2&&perf_binds==0&&abis==0&&infos==0&&windows==0&&registries==0&&locks==0);
        const auto verified=clock(gpu,true);
        CHECK(verified.image==recorded.image&&verified.windows&&verified.registry&&
              windows==2&&registries==1&&perf_binds==0&&infos==0&&locks==0);
        bool refused=false;
        try { make_power_weight_capture(api(),adapter)(gpu,true); }
        catch(const VoltageEquationEvidenceError& error) { refused=error.kind==VoltagePolicyAvailability::unsupported; }
        CHECK(refused&&perf_binds==1&&images==4&&registries==1&&infos==0&&locks==0);
    }
    fresh();
    generation=2;
    mode=14;
    bool refused=false;
    try { make_power_weight_capture(api(),adapter,true)(gpu,true); }
    catch(const VoltageEquationEvidenceError& error) { refused=error.kind==VoltagePolicyAvailability::invalid; }
    CHECK(refused&&images==1&&windows==0&&registries==0&&locks==0);

    for (uint32_t architecture : {2u,3u,4u,7u}) {
        fresh();
        generation=architecture;
        mode=16; // Even a permissive old PERF binder may only qualify Pascal clocks.
        auto old_core=api();
        old_core.clock_bind=nullptr;
        const auto legacy_client=make_power_weight_capture(old_core,adapter,true);
        refused=false;
        try {
            const auto read=legacy_client(gpu,true);
            CHECK(read.identity.architecture==4&&read.image_availability==VoltagePolicyAvailability::available&&
                  read.windows&&read.registry&&perf_binds>2&&images==2&&registries==1);
        } catch(const VoltageEquationEvidenceError& error) { refused=error.kind==VoltagePolicyAvailability::unsupported; }
        CHECK(refused==(architecture!=4)&&clock_binds==0&&infos==0&&locks==0);
        if(architecture!=4)CHECK(perf_binds==1&&images==0&&registries==0);
    }
}
void run_5520_capture() {
    constexpr int stable=0, transient=9, incomplete=10, unknown_command=8;
    for(const int scenario:{stable,transient,incomplete,unknown_command}) {
        fresh();
        mode=scenario;
        auto a=api();
        a.abi=[](dc_gpu*,dc_power_layout* layout,uint32_t* index,dc_error*) -> dc_status {
            ++abis;
            layout->sizes[0]=5520;
            layout->info_base=0x3c;
            layout->info_stride=0x84;
            layout->has_commands=1;
            layout->commands[0]=mode==unknown_command?0x2080a618:0x20802618;
            *index=0;
            return DC_OK;
        };
        a.rm=[](dc_gpu*,uint32_t command,const uint32_t* params,uint32_t params_count,int32_t has_mask,
                uint32_t mask,int32_t any_live,uint32_t* out,uint32_t capacity,uint32_t* count,dc_error* error) -> dc_status {
            CHECK(command==0x2080a618&&!params&&!params_count&&!has_mask&&!mask&&!any_live&&capacity==1380);
            if(++infos==1&&mode==transient){error->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}
            out[0x10bc/4]=1;
            out[0x10c4/4]=5000;
            *count=capacity-(mode==incomplete?1:0);
            return DC_OK;
        };
        auto read=make_power_weight_capture(a,adapter)(reinterpret_cast<dc_gpu*>(1),false);
        CHECK(read.image_availability==VoltagePolicyAvailability::available&&registries==0&&windows==0&&locks==0);
        if(scenario==unknown_command) {
            CHECK(read.info_availability==VoltagePolicyAvailability::unsupported&&infos==0&&resets==0);
        } else if(scenario==incomplete) {
            CHECK(read.info_availability==VoltagePolicyAvailability::invalid&&infos==1&&resets==0);
        } else {
            CHECK(read.info_availability==VoltagePolicyAvailability::available&&read.layout.info_command==0x20802618);
            CHECK(infos==(scenario==transient?3:2)&&resets==(scenario==transient?1:0));
            const auto report=decode_power_relationships(read.layout,read.info);
            CHECK(report.relationships.size()==1&&report.relationships[0].weight_raw==5000);
        }
    }
}
void run_rm26_info_capture(){
 auto* gpu=reinterpret_cast<dc_gpu*>(1);
 fresh();mode=kRm26Info;
 const auto stable=make_power_weight_capture(api(),adapter)(gpu,false);
 CHECK(stable.info_availability==VoltagePolicyAvailability::available&&abis==2&&infos==2&&resets==0&&locks==0);
 CHECK(stable.layout.info_command==0x20802618&&stable.layout.info_size==5520&&stable.info.size()==5520&&
       stable.layout.policy_base==0x3c&&stable.layout.policy_stride==0x84);

 fresh();mode=kChangingInfoLayout;
 const auto unstable=make_power_weight_capture(api(),adapter)(gpu,false);
 CHECK(unstable.info_availability==VoltagePolicyAvailability::transient&&unstable.info.empty()&&
       abis==4&&infos==4&&resets==1&&locks==0);
}
void run(){fresh();auto capture=make_power_weight_capture(api(),adapter);CHECK(images==0&&infos==0&&windows==0&&registries==0);auto* gpu=reinterpret_cast<dc_gpu*>(1);auto read=capture(gpu,false);
 CHECK(read.image_availability==VoltagePolicyAvailability::available&&read.info_availability==VoltagePolicyAvailability::available&&read.registry_availability==VoltagePolicyAvailability::unknown&&!read.windows&&!read.registry&&images==2&&infos==2&&windows==0&&registries==0&&locks==0);
 CHECK(read.identity.slot==adapter.slot&&read.identity.device_id==0x6789&&std::strcmp(read.adapter.uuid,adapter.uuid)==0&&read.target.handle==42);
 read=capture(gpu,true);CHECK(read.registry_availability==VoltagePolicyAvailability::available&&read.windows&&read.registry&&windows==2&&registries==1&&locks==0);
 for(const int test:{1,2,3,5,7,8,9,10,11,12}){fresh();mode=test;auto current=make_power_weight_capture(api(),adapter);read=current(gpu,test==12);CHECK(locks==0);
  if(test==1)CHECK(read.image_availability==VoltagePolicyAvailability::unsupported&&read.info_availability==VoltagePolicyAvailability::available&&images==1);
  if(test==2)CHECK(read.image_availability==VoltagePolicyAvailability::available&&images==3);
  if(test==3)CHECK(read.image_availability==VoltagePolicyAvailability::invalid&&images==1&&read.info_availability==VoltagePolicyAvailability::available);
  if(test==5)CHECK(read.image_availability==VoltagePolicyAvailability::transient&&images==6&&read.info_availability==VoltagePolicyAvailability::available);
  if(test==7||test==8)CHECK(read.info_availability==VoltagePolicyAvailability::unsupported&&read.image_availability==VoltagePolicyAvailability::available&&abis==1&&infos==0);
  if(test==9)CHECK(read.info_availability==VoltagePolicyAvailability::available&&infos==3&&resets==1);
  if(test==10)CHECK(read.info_availability==VoltagePolicyAvailability::invalid&&infos==1&&resets==0&&read.image_availability==VoltagePolicyAvailability::available);
  if(test==11)CHECK(read.info_availability==VoltagePolicyAvailability::transient&&infos==4&&resets==1);
  if(test==12)CHECK(read.registry_availability==VoltagePolicyAvailability::transient&&read.info_availability==VoltagePolicyAvailability::available&&!read.registry);
  if(test!=12)CHECK(windows==0&&registries==0);
 }
 for(const int test:{4,6,13}){fresh();mode=test;auto current=make_power_weight_capture(api(),adapter);bool refused=false;try{current(gpu,test==13);}catch(const VoltageEquationEvidenceError& error){refused=error.kind==VoltagePolicyAvailability::invalid;}CHECK(refused&&locks==0);if(test!=13)CHECK(infos==0&&images==1);}
 fresh();adapter.uuid[0]=0;auto no_uuid=make_power_weight_capture(api(),adapter);CHECK(no_uuid(gpu,false).info_availability==VoltagePolicyAvailability::available);
 fresh();auto missing_api=api();missing_api.evidence.image=nullptr;missing_api.abi=nullptr;auto missing=make_power_weight_capture(missing_api,adapter);read=missing(gpu,false);CHECK(read.image_availability==VoltagePolicyAvailability::unsupported&&read.info_availability==VoltagePolicyAvailability::unsupported&&images==0&&infos==0&&registries==0);
}
}
int main(){run();run_5520_capture();run_rm26_info_capture();run_legacy_capture();run_timing_capture();std::printf("Power weight capture: %u checks passed\n",checks);return 0;}
