// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_saving_service.h"
#include <algorithm>
#include <cstdio>
#include <cstring>
namespace power_saving_test {
using namespace druta::ui;
struct Fake {
    dc_gpu_identity identity{};uint32_t native_arch=0x1B0,implementation=3;
    std::array<uint32_t,22> reasons{},other{};std::array<bool,22> supported{};
    std::array<bool,8> adaptive{};std::array<uint32_t,16> raw{},second{};
    uint32_t factor=1000,handle=0;bool profiler_request=false,difr_supported=true;
    bool fail_set=false,mutate_preserved=false,no_dispatch=false,foreign_read=false,malformed=false,skip_raw=false;
    int sets=0,allocations=0,frees=0,gets=0,fail_gets=0,reset_count=0,lock_depth=0;
    Fake(){identity.size=sizeof(identity);std::strcpy(identity.slot,"0000:ab:03.0");std::strcpy(identity.uuid,"GPU-independent-power-saving");identity.nvapi_ok=identity.nvapi_selected=1;identity.nvapi_handle=0x9988;identity.nvapi_devid=0x4567;identity.nvapi_subsys=0x89ABCDEF;supported.fill(true);raw.fill(2000);second.fill(3000);for(auto& n:other)n=0x80;adaptive.fill(true);}
    dc_gpu* gpu(){return reinterpret_cast<dc_gpu*>(this);}
};
inline Fake& f(dc_gpu* gpu){return *reinterpret_cast<Fake*>(gpu);}
inline dc_status DC_CALL identity(const dc_gpu* gpu,dc_gpu_identity* out){const auto& x=*reinterpret_cast<const Fake*>(gpu);*out=x.identity;if(x.foreign_read)++out->nvapi_subsys;return DC_OK;}
inline dc_status DC_CALL lock(dc_gpu* gpu){++f(gpu).lock_depth;return DC_OK;}
inline dc_status DC_CALL unlock(dc_gpu* gpu){--f(gpu).lock_depth;return DC_OK;}
inline dc_status DC_CALL bind(dc_gpu* gpu,int32_t,dc_target* out,dc_error*){auto& x=f(gpu);out->size=sizeof(*out);std::strcpy(out->slot,x.identity.slot);out->architecture=7;out->handle=x.identity.nvapi_handle;out->device_id=x.identity.nvapi_devid;out->subsystem_id=x.identity.nvapi_subsys;return DC_OK;}
inline int32_t DC_CALL architecture(uint32_t arch){return arch>=4&&arch<=10;}
inline dc_status DC_CALL transport(dc_gpu*,dc_rm_transport* out,dc_error*){out->size=sizeof(*out);out->captured=1;out->header_count=17;out->header[0]=0x4E564441;out->header[1]=0x10002;out->header[2]=72;out->header[3]=0x4E562A2A;out->header[4]=0x0500002B;out->header[12]=0x123;out->header[13]=0x456;out->header[15]=4;return DC_OK;}
inline dc_status DC_CALL reset(dc_gpu* gpu,int32_t){++f(gpu).reset_count;return DC_OK;}
inline uint16_t half(const uint8_t* raw,size_t n){uint16_t v;std::memcpy(&v,raw+n,2);return v;}
inline void half(uint8_t* raw,size_t n,uint16_t v){std::memcpy(raw+n,&v,2);}
inline dc_status DC_CALL escape(dc_gpu* gpu,uint8_t* bytes,size_t,const dc_escape_fields*,int32_t* nt,dc_error* e){auto& x=f(gpu);auto* p=reinterpret_cast<uint32_t*>(bytes);*nt=0;
    const bool allocate=p[4]==0x0500002A,free=p[4]==0x0500000C;const uint32_t command=p[14];const bool set=allocate||free||command==0xB0CC0301||command==0xB0CC0302||command==0x2080E802||(command==0x20802801&&p[17]!=3);
    if(set&&x.no_dispatch){e->kind=DC_ERR_UNSUPPORTED;return DC_E_UNAVAILABLE;}if(!set){++x.gets;if(x.fail_gets-->0){*nt=-1;return DC_OK;}}
    p[5]=p[11]=1;if(allocate){++x.allocations;x.handle=p[14];p[19]=0;return DC_OK;}if(free){++x.frees;if(p[14]!=x.handle){p[15]=1;return DC_OK;}x.handle=0;p[15]=0;return DC_OK;}p[16]=0;
    if(command==0x20801701){p[17]=x.native_arch;p[18]=x.implementation;p[19]=1;p[20]=0;}
    else if(command==0x2080A801||command==0x2080E802){auto* payload=bytes+68;const auto group=half(payload,4),index=half(payload,6),attribute=half(payload,8);uint32_t value=0;
        if(command==0x2080E802){++x.sets;value=p[20];if(group==4)x.adaptive[index]=value!=0;else if(attribute==0x3A)x.raw[index]=static_cast<uint32_t>(std::max<uint64_t>(1,uint64_t(value)*x.factor/1000));else if(attribute==0x37){if(value)x.reasons[index]&=~1u;else x.reasons[index]|=1;}if(x.mutate_preserved)x.second[index]++;half(payload,10,1);}
        else {if(group==4)value=attribute==9?1u:(x.adaptive[index]?1u:0u);else if(attribute==0x39)value=x.supported[index]?1u:0u;else if(attribute==0x37)value=1;else if(attribute==0x48)value=x.reasons[index];else if(attribute==0x49)value=x.other[index];else if(attribute==0x3A)value=static_cast<uint32_t>(uint64_t(x.raw[index])*1000/x.factor);else if(attribute==0x3B)value=static_cast<uint32_t>(uint64_t(x.second[index])*1000/x.factor);else if(attribute==0x4C)value=x.raw[index];half(payload,10,1);p[20]=value;}}
    else if(command==0x20801706){uintptr_t pointer=0;std::memcpy(&pointer,p+19,sizeof(pointer));auto* entries=reinterpret_cast<uint32_t*>(pointer);entries[2]=x.raw[entries[1]];if(!x.skip_raw)entries[5]=x.second[entries[4]];}
    else if(command==0x20802801){if(p[17]==3)p[17]=x.difr_supported?1u:2u;else {++x.sets;if(p[17]==1)x.other[9]|=1;else x.other[9]&=~1u;}}
    else if(command==0xB0CC0301){++x.sets;x.profiler_request=true;for(size_t n=0;n<22;++n)if(x.supported[n])x.reasons[n]|=1;p[17]=p[19]=0;}
    else if(command==0xB0CC0302){++x.sets;x.profiler_request=false;for(size_t n=0;n<22;++n)if(x.supported[n])x.reasons[n]&=~1u;}
    else p[16]=0x1F;
    if(x.malformed&&!set)p[6]^=1;if(set&&x.fail_set)*nt=-1;return DC_OK;
}
inline PowerSavingApi api(){PowerSavingApi a;a.transport={identity,bind,architecture,transport,escape,reset,lock,unlock};a.profiler_handle=[](){return 0x60778899u;};return a;}
inline const PowerSavingRow& row(const PowerSavingService& service,PowerSavingFamily family,uint32_t index=0){const auto& rows=service.state().rows;return *std::find_if(rows.begin(),rows.end(),[&](const auto& r){return r.key==PowerSavingKey{family,index};});}
inline PowerSavingRequest request(const PowerSavingService& service,PowerSavingFamily family,uint32_t index,uint32_t value){const auto& r=row(service,family,index);return {r.key,value,r.binding,r.value};}
}
