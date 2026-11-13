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
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
void put(std::vector<uint8_t>& b,size_t at,uint32_t value){std::memcpy(b.data()+at,&value,4);}
dc_status DC_CALL id(const dc_gpu*,dc_gpu_identity* out){*out=adapter;return DC_OK;}
dc_status DC_CALL bind(dc_gpu*,int32_t,dc_target* target,dc_error*){target->size=sizeof(*target);std::strcpy(target->slot,adapter.slot);target->architecture=7;target->handle=adapter.nvapi_handle;target->device_id=adapter.nvapi_devid;target->subsystem_id=adapter.nvapi_subsys;return DC_OK;}
int32_t DC_CALL arch(uint32_t a){return a==7;}
dc_status DC_CALL lock(dc_gpu*){++locks;return DC_OK;}dc_status DC_CALL unlock(dc_gpu*){--locks;return DC_OK;}
dc_status DC_CALL reset(dc_gpu*,int32_t kind){CHECK(kind==DC_TRANSPORT_POWER);++resets;return DC_OK;}
dc_status DC_CALL image(dc_gpu*,uint8_t* bytes,uint32_t capacity,uint32_t* count,dc_error* error){++images;CHECK(capacity==DC_VBIOS_IMAGE_CAPACITY);
 if(mode==1){error->kind=DC_ERR_UNSUPPORTED;return DC_E_UNAVAILABLE;}if(mode==2&&images==1){error->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}if(mode==3){error->kind=DC_ERR_MALFORMED;return DC_E_FAILED;}if(mode==4){error->kind=DC_ERR_IDENTITY;return DC_E_FAILED;}
 std::fill_n(bytes,32,uint8_t{0xa5});*count=32;if(mode==5)bytes[31]=uint8_t(images);if(mode==6)++adapter.nvapi_handle;return DC_OK;}
dc_status DC_CALL abi(dc_gpu*,dc_power_layout* layout,uint32_t* index,dc_error* error){++abis;if(mode==7){error->kind=DC_ERR_UNSUPPORTED;return DC_E_UNAVAILABLE;}layout->size=sizeof(*layout);layout->sizes[0]=20512;layout->has_commands=1;layout->commands[0]=0x2080a618;layout->info_base=0xcc;layout->info_stride=mode==8?0x100:0x104;*index=0;return DC_OK;}
dc_status DC_CALL rm(dc_gpu*,uint32_t command,const uint32_t* params,uint32_t params_count,int32_t has_mask,uint32_t mask,int32_t any_live,uint32_t* out,uint32_t capacity,uint32_t* count,dc_error* error){++infos;
 CHECK(command==0x2080a618&&!params&&!params_count&&!has_mask&&!mask&&!any_live&&capacity==20512/4);
 if(mode==9&&infos==1){error->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}if(mode==10){error->kind=DC_ERR_MALFORMED;return DC_E_FAILED;}
 std::vector<uint8_t> bytes(20512);put(bytes,4,1);put(bytes,0xcc,0x40);put(bytes,0xcc+8,1);put(bytes,0xcc+12,2);put(bytes,0xcc+16,3);put(bytes,0x41cc,1);bytes[0x41d0+7]=0x10;if(mode==11)bytes[0x5000]=uint8_t(infos);std::memcpy(out,bytes.data(),bytes.size());*count=capacity;return DC_OK;}
PowerWeightCaptureApi api(){PowerWeightCaptureApi a;a.evidence.target.identity=id;a.evidence.target.bind=bind;a.evidence.target.architecture_supported=arch;a.evidence.target.lock=lock;a.evidence.target.unlock=unlock;a.evidence.target.reset_transport=reset;a.evidence.image=image;a.abi=abi;a.rm=rm;
 a.resolve_windows=[](const dc_gpu_identity&,const dc_target& target){++windows;VoltageScaffoldWindowsTarget value{target.slot,"PCI\\VEN_10DE&DEV_6789\\A","0007"};if(mode==13&&windows>1)value.driver_key="0008";return value;};
 a.registry_read=[](const VoltageScaffoldWindowsTarget&){++registries;if(mode==12)throw VoltageEquationEvidenceError("Registry unavailable",VoltagePolicyAvailability::transient);return PowerWeightRegistrySnapshot{{L"EnableGpuFirmware",4,{1,0,0,0}},{L"romOverride00",3,{1,2,3}}};};return a;}
void fresh(){images=infos=abis=windows=registries=locks=mode=resets=0;adapter={};adapter.size=sizeof(adapter);std::strcpy(adapter.slot,"0000:7b:01.1");std::strcpy(adapter.uuid,"GPU-read-alt");adapter.nvapi_ok=adapter.nvapi_selected=1;adapter.nvapi_handle=42;adapter.nvapi_devid=0x6789;adapter.nvapi_subsys=0xaabbccdd;}
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
int main(){run();std::printf("Power weight capture: %u checks passed\n",checks);return 0;}
