// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_variable_capture.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
namespace {
dc_gpu_identity device{};int mode=0,calls=0,resets=0;
dc_status DC_CALL identity(const dc_gpu*,dc_gpu_identity* out){*out=device;if(mode==8)return DC_E_ARGUMENT;if(mode==11)out->nvml_has_devid=1;if(mode==12)std::memset(out->pairing_error,'x',sizeof(out->pairing_error));return DC_OK;}
dc_status DC_CALL bind(dc_gpu*,int32_t,dc_target* t,dc_error*){t->size=sizeof(*t);std::strcpy(t->slot,device.slot);t->architecture=7;t->device_id=device.nvapi_devid;t->subsystem_id=device.nvapi_subsys;t->handle=device.nvapi_handle;if(mode==7&&calls)++t->handle;return DC_OK;}
int32_t DC_CALL arch(uint32_t a){return a==7;}
dc_status DC_CALL transport(dc_gpu*,dc_rm_transport* t,dc_error*){t->captured=1;t->header_count=17;t->header[0]=0x4e564441;t->header[1]=0x10002;t->header[2]=76;t->header[3]=0x4e562a2a;t->header[4]=0x0500002b;t->header[15]=8;if(mode==5)t->header[0]=0;if(mode==9)t->captured=2;if(mode==10)t->size=0;return DC_OK;}
dc_status DC_CALL reset(dc_gpu*,int32_t){++resets;return DC_OK;}
dc_status DC_CALL escape(dc_gpu*,uint8_t* raw,size_t size,const dc_escape_fields*,int32_t* nt,dc_error* e){++calls;auto* p=reinterpret_cast<uint32_t*>(raw);CHECK(p[14]==0x2080a0b1&&p[15]==0xc364&&size==68+0xc364);if(mode==1&&calls==1){e->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}if(mode==2){e->kind=DC_ERR_UNSUPPORTED;return DC_E_FAILED;}if(mode==13||mode==14){++p[14];e->kind=DC_ERR_TRANSIENT;return mode==13?DC_E_FAILED:DC_E_UNAVAILABLE;}if(mode>=15&&mode<=18){p[11]=(mode==15||mode==17)?2u:0xffffffffu;e->kind=DC_ERR_TRANSIENT;return mode<=16?DC_E_FAILED:DC_E_UNAVAILABLE;}p[11]=1;*nt=0;raw[68+42]=mode==3?uint8_t(calls):42;if(mode==4)++p[14];if(mode==6)raw[size]=1;return DC_OK;}
}
int main(){device.size=sizeof(device);std::strcpy(device.slot,"0000:09:01.0");std::strcpy(device.uuid,"GPU-other");device.nvapi_ok=device.nvapi_selected=1;device.nvapi_handle=42;device.nvapi_devid=123;device.nvapi_subsys=456;VoltagePolicyApi a{};a.identity=identity;a.bind=bind;a.architecture_supported=arch;a.transport=transport;a.escape=escape;a.reset_transport=reset;auto* gpu=reinterpret_cast<dc_gpu*>(1);
 auto good=capture_voltage_variables(a,gpu,device);CHECK(good.bytes.size()==0xc364&&good.bytes[42]==42&&good.target.handle==42&&calls==2);
 mode=1;calls=resets=0;good=capture_voltage_variables(a,gpu,device);CHECK(calls==3&&resets==1&&good.bytes[42]==42);
 for(int m:{2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18}){mode=m;calls=resets=0;bool rejected=false;try{capture_voltage_variables(a,gpu,device);}catch(const VoltageEquationEvidenceError& e){rejected=true;CHECK(e.kind==(m==2?VoltagePolicyAvailability::unsupported:m==3?VoltagePolicyAvailability::transient:VoltagePolicyAvailability::invalid));}CHECK(rejected);CHECK(resets==(m==3?1:0));}
 auto bad_opened=device;bad_opened.pairing_error[0]='x';mode=0;calls=resets=0;bool bad=false;try{capture_voltage_variables(a,gpu,bad_opened);}catch(const VoltageEquationEvidenceError& e){bad=e.kind==VoltagePolicyAvailability::invalid;}CHECK(bad&&calls==0&&resets==0);
 a.escape=nullptr;bool missing=false;try{capture_voltage_variables(a,gpu,device);}catch(const VoltageEquationEvidenceError& e){missing=e.kind==VoltagePolicyAvailability::unsupported;}CHECK(missing);std::puts("Variable INFO capture tests passed");}
