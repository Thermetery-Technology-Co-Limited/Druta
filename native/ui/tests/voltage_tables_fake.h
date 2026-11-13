// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_tables_service.h"
#include <algorithm>
#include <cstring>
#include <cstdio>
namespace voltage_tables_test {
using namespace druta::ui;
inline uint32_t word(const std::vector<uint8_t>& raw,size_t at){uint32_t value=0;std::memcpy(&value,raw.data()+at,4);return value;}
inline void put(std::vector<uint8_t>& raw,size_t at,uint32_t value){std::memcpy(raw.data()+at,&value,4);}
struct Fake {
    dc_gpu_identity id{};
    uint32_t arch=7,active=3,public_present=3;
    std::vector<uint8_t> info=std::vector<uint8_t>(0x98C),native=std::vector<uint8_t>(0x40C);
    std::array<std::vector<uint8_t>,2> public_rows;
    bool admission=true,exports=true,set_error=false,foreign_after_set=false,identity_after_set=false,unknown_after_set=false,mutate_input=false;
    int sets=0,reads=0,depth=0,fail_reads=0,post_set_failures=0,refreshes=0;
    Fake(){id.size=sizeof(id);std::snprintf(id.slot,sizeof(id.slot),"0000:af:02.0");std::snprintf(id.uuid,sizeof(id.uuid),"GPU-voltage-tables-alternate");
        id.nvapi_ok=id.nvapi_selected=1;id.nvapi_handle=0x556677;id.nvapi_devid=0x789A;id.nvapi_subsys=0xBADCABCD;
        put(info,4,active);put(native,4,active);native[8]=17;
        for(uint32_t rail=0;rail<2;++rail){public_rows[rail].resize(0xAC8);auto& raw=public_rows[rail];put(raw,0,0x20AC8);put(raw,4,1u<<rail);raw[8]=17;raw[0x3C]=static_cast<uint8_t>(0x70+rail);
            const uint32_t control=rail==0?0:2;put(raw,0x48+rail*0x54,control);info[12+rail*76]=static_cast<uint8_t>(rail==0?2:4);info[12+rail*76+0x33]=static_cast<uint8_t>(rail==0?1:3);
            native[12+rail*32]=static_cast<uint8_t>(rail==0?2:4);
            for(uint32_t field=0;field<6;++field){const int32_t value=static_cast<int32_t>((rail+1)*1000+field*111);put(raw,0x48+rail*0x54+4+field*4,static_cast<uint32_t>(value));put(native,12+rail*32+4+field*4,static_cast<uint32_t>(value));}
        }
    }
    dc_gpu* gpu(){return reinterpret_cast<dc_gpu*>(this);}
    int32_t value(uint32_t rail,uint32_t field)const{const auto raw=word(public_rows[rail],0x48+rail*0x54+4+field*4);int32_t value;std::memcpy(&value,&raw,4);return value;}
    void set(uint32_t rail,uint32_t field,int32_t value){put(public_rows[rail],0x48+rail*0x54+4+field*4,static_cast<uint32_t>(value));put(native,12+rail*32+4+field*4,static_cast<uint32_t>(value));}
};
inline Fake& f(dc_gpu* gpu){return *reinterpret_cast<Fake*>(gpu);}
inline dc_status DC_CALL identity(const dc_gpu* gpu,dc_gpu_identity* out){*out=reinterpret_cast<const Fake*>(gpu)->id;return DC_OK;}
inline dc_status DC_CALL lock(dc_gpu* gpu){++f(gpu).depth;return DC_OK;}
inline dc_status DC_CALL unlock(dc_gpu* gpu){--f(gpu).depth;return DC_OK;}
inline dc_status DC_CALL bind(dc_gpu* gpu,int32_t,dc_target* out,dc_error*){auto& x=f(gpu);out->size=sizeof(*out);std::snprintf(out->slot,sizeof(out->slot),"%s",x.id.slot);
    out->architecture=x.arch;out->handle=x.id.nvapi_handle;out->device_id=x.id.nvapi_devid;out->subsystem_id=x.id.nvapi_subsys;return DC_OK;}
inline int32_t DC_CALL architecture(uint32_t arch){return arch>=4&&arch<=10;}
inline dc_status DC_CALL transport(dc_gpu*,dc_rm_transport* out,dc_error*){out->captured=1;out->header_count=17;out->header[0]=0x4E564441;out->header[1]=0x10002;out->header[2]=72;out->header[3]=0x4E562A2A;out->header[4]=0x0500002B;out->header[15]=4;return DC_OK;}
inline dc_status DC_CALL escape(dc_gpu* gpu,uint8_t* bytes,size_t,const dc_escape_fields*,int32_t* nt,dc_error*){
    auto& x=f(gpu);auto* packet=reinterpret_cast<uint32_t*>(bytes);++x.reads;*nt=0;
    if(x.fail_reads-->0){*nt=-1;return DC_OK;}packet[5]=packet[11]=1;
    if(packet[14]==0x2080A030){packet[17]=x.admission?1u:0u;return DC_OK;}
    if(packet[14]==0x2080B201){auto info=x.info;put(info,4,x.active);std::memcpy(packet+17,info.data(),info.size());return DC_OK;}
    if(packet[14]==0x2080B213){const auto mask=packet[18];auto native=x.native;put(native,4,mask);std::memcpy(packet+17,native.data(),native.size());return DC_OK;}
    packet[16]=0x1F;return DC_OK;
}
inline dc_status DC_CALL blocks(dc_gpu* gpu,int32_t function,uint32_t version,dc_rails_block_set* out){auto& x=f(gpu);if(function!=DC_RAILS_CONTROL||version!=0x20AC8)return DC_E_ARGUMENT;
    out->present=x.public_present;for(uint32_t rail=0;rail<2;++rail)if(out->present&(1u<<rail))std::memcpy(out->blocks[rail],x.public_rows[rail].data(),0xAC8);return DC_OK;}
inline dc_status DC_CALL state(dc_gpu* gpu,dc_rails_state_set* out){auto& x=f(gpu);out->present=3;
    for(uint32_t rail=0;rail<2;++rail){auto& row=out->rails[rail];row.type=x.info[12+rail*76+0x33];row.reliability=1100;row.alt_reliability=1000;row.overvoltage=1200;row.effective=1050;row.vmin=600;row.boost_mv=5;}return DC_OK;}
inline dc_status DC_CALL deltas(dc_gpu* gpu,dc_rails_delta_set* out){auto& x=f(gpu);out->present=3;for(uint32_t rail=0;rail<2;++rail){auto& row=out->rails[rail];row.type=static_cast<int32_t>(word(x.public_rows[rail],0x48+rail*0x54));
    row.reliability=x.value(rail,0)/1000.0;row.alt_reliability=x.value(rail,1)/1000.0;row.overvoltage=x.value(rail,2)/1000.0;row.vmin=x.value(rail,3)/1000.0;}return DC_OK;}
inline dc_status DC_CALL refresh(dc_gpu* gpu){++f(gpu).refreshes;return DC_OK;}
inline dc_status DC_CALL reset(dc_gpu*,int32_t){return DC_OK;}
inline dc_status DC_CALL boost(dc_gpu* gpu,int32_t* value){*value=f(gpu).native[8];return DC_OK;}
inline int32_t DC_CALL present(const dc_gpu* gpu,uint32_t){return reinterpret_cast<const Fake*>(gpu)->exports?1:0;}
inline dc_status setter(dc_gpu* gpu,uint8_t* bytes,size_t size,int32_t& status,dc_error&){auto& x=f(gpu);++x.sets;if(size!=0xAC8)return DC_E_ARGUMENT;
    std::vector<uint8_t> request(bytes,bytes+size);const auto mask=word(request,4);
    for(uint32_t rail=0;rail<2;++rail)if(mask&(1u<<rail))for(uint32_t field=0;field<6;++field){const auto value=word(request,0x48+rail*0x54+4+field*4);int32_t signed_value;std::memcpy(&signed_value,&value,4);x.set(rail,field,signed_value);}
    if(x.foreign_after_set)x.set(0,0,987654);
    if(x.unknown_after_set){x.public_rows[1][0x3C]=0x12;x.unknown_after_set=false;}
    if(x.identity_after_set)std::snprintf(x.id.uuid,sizeof(x.id.uuid),"GPU-replaced");
    if(x.mutate_input)bytes[0x30]^=1;
    x.fail_reads=x.post_set_failures;x.post_set_failures=0;status=x.set_error?-1:0;return DC_OK;
}
inline VoltageTablesApi api(){VoltageTablesApi a;a.transport={identity,bind,architecture,transport,escape,reset,lock,unlock};
    a.rails_blocks=blocks;a.rails_state=state;a.rails_deltas=deltas;a.rails_refresh=refresh;a.boost=boost;a.interface_present=present;a.public_set=setter;return a;}
inline VoltageTableRequest request(const VoltageTablesService& service,uint32_t rail,uint32_t field,int32_t value){const auto& rows=service.state().rows;
    const auto row=std::find_if(rows.begin(),rows.end(),[&](const auto& r){return r.key.rail==rail&&r.key.field==field;});return {row->key,value,row->binding,row->value};}
}
