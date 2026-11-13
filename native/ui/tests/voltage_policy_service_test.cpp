// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_policy_service.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
namespace {
dc_gpu_identity dev{};bool gate=false,foreign=false,unsupported_gate=false,race=false,drift=false;
int transient_get=0,blocked_get=0,writes=0,resets=0;bool block_after_write=false,malformed_set=false;
std::array<uint8_t,60> header{};
dc_status DC_CALL identity(const dc_gpu*,dc_gpu_identity* out){*out=dev;if(foreign)++out->nvapi_devid;return DC_OK;}
dc_status DC_CALL lock(dc_gpu*){return DC_OK;}dc_status DC_CALL unlock(dc_gpu*){return DC_OK;}
int32_t DC_CALL supported(uint32_t architecture){return architecture>=4&&architecture<=10;}
dc_status DC_CALL bind(dc_gpu*,int32_t complete,dc_target* out,dc_error*){CHECK(complete==1);out->size=sizeof(*out);std::strcpy(out->slot,dev.slot);out->architecture=7;out->handle=dev.nvapi_handle;out->device_id=dev.nvapi_devid;out->subsystem_id=dev.nvapi_subsys;return DC_OK;}
dc_status DC_CALL transport(dc_gpu*,dc_rm_transport* out,dc_error*){out->captured=1;out->header_count=17;out->header[0]=0x4E564441;out->header[1]=0x10002;out->header[2]=76;out->header[3]=0x4E562A2A;out->header[4]=0x0500002B;out->header[15]=8;return DC_OK;}
dc_status DC_CALL reset(dc_gpu*,int32_t kind){CHECK(kind==DC_TRANSPORT_POWER);++resets;return DC_OK;}
dc_status DC_CALL escape(dc_gpu*,uint8_t* raw,size_t size,const dc_escape_fields*,int32_t* nt,dc_error*){
 auto* p=reinterpret_cast<uint32_t*>(raw);CHECK(size==p[2]&&p[15]+68==size&&p[5]==0&&p[11]==0);*nt=0;
 const auto cmd=p[14];const bool setting=cmd==0x2080A031||cmd==0x2080D01C;
 if(!setting&&(transient_get>0||blocked_get>0)){if(transient_get>0)--transient_get;if(blocked_get>0)--blocked_get;*nt=-1;return DC_OK;}
 p[5]=1;p[11]=1;p[16]=0;
 if(cmd==0x2080A030){if(unsupported_gate)p[16]=0x1F;else p[17]=gate?1:0;}
 else if(cmd==0x2080A031){++writes;if(race){gate=true;race=false;}p[17]=gate?1:0;gate=p[18]!=0;if(malformed_set){p[11]=0;malformed_set=false;}}
 else if(cmd==0x2080901B){CHECK(p[18]==0);std::memcpy(p+17,header.data(),header.size());}
 else {CHECK(cmd==0x2080D01C);++writes;auto* bytes=reinterpret_cast<uint8_t*>(p+17);
  for(size_t i=0;i<header.size();++i)if(i!=10&&i!=11)CHECK(bytes[i]==header[i]);
  std::memcpy(header.data(),bytes,header.size());if(drift){header[0x20]^=0x17;drift=false;}
 }
 if(setting&&block_after_write){blocked_get=4;block_after_write=false;}
 return DC_OK;
}
void fresh(){dev={};dev.size=sizeof(dev);std::strcpy(dev.slot,"0000:09:00.0");std::strcpy(dev.uuid,"GPU-alternate");dev.nvapi_ok=dev.nvapi_selected=1;dev.nvapi_handle=42;dev.nvapi_devid=0x1234;dev.nvapi_subsys=0x98761234;
 gate=false;foreign=unsupported_gate=race=drift=block_after_write=malformed_set=false;transient_get=blocked_get=writes=resets=0;
 header.fill(0xA5);header[0]=3;for(size_t i=4;i<8;++i)header[i]=0;for(size_t i=8;i<14;++i)header[i]=1;header[0x38]=1;
}
VoltagePolicyApi api(){VoltagePolicyApi a;a.identity=identity;a.bind=bind;a.architecture_supported=supported;a.transport=transport;a.escape=escape;a.reset_transport=reset;a.lock=lock;a.unlock=unlock;return a;}
}
int main(){
 fresh();{VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);auto data=s.read();CHECK(data.rows[0].initial==false&&data.rows[1].current==true&&!data.message.empty());
 CHECK(s.apply({2,0,2}).ok&&header[10]==0&&header[11]==1&&s.state().undo_available);CHECK(s.undo().ok&&header[10]==1);
 CHECK(s.apply({6,0,6}).ok&&header[10]==0&&header[11]==0);CHECK(s.restore_initial(6).ok&&header[10]==1&&header[11]==1);
 auto rejected=s.apply({1,1,1});CHECK(!rejected.ok&&s.state().rows[0].writable); // stale display is not unsupported
 transient_get=2;data=s.read(1);CHECK(data.rows[0].current==false&&data.rows[0].writable&&data.rows[0].availability==VoltagePolicyAvailability::transient);
 CHECK(s.read(1).rows[0].availability==VoltagePolicyAvailability::available);
 unsupported_gate=true;data=s.read();CHECK(data.rows[0].availability==VoltagePolicyAvailability::unsupported&&data.rows[1].availability==VoltagePolicyAvailability::available);
 }
 fresh();{VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);s.read();race=true;auto r=s.apply({1,0,0});CHECK(!r.ok&&r.rollback_verified&&gate&&writes==2);CHECK(s.state().rows[0].current==true);}
 fresh();{VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);s.read();race=malformed_set=true;auto r=s.apply({1,0,0});CHECK(!r.ok&&!r.rollback_verified&&s.state().recovery_pending&&writes==1&&!gate);CHECK(!s.recover().ok&&writes==1);CHECK(s.state().recovery_prior_unknown&&!s.state().undo_available);CHECK(s.restore_initial(1).ok&&writes==2&&!gate&&!s.state().recovery_pending);}
 fresh();{VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);s.read();const auto old=header[0x20];drift=true;auto r=s.apply({2,0,2});CHECK(!r.ok&&r.rollback_verified&&header[10]==1&&header[0x20]==uint8_t(old^0x17));CHECK(writes==2);}
 fresh();{VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);s.read();block_after_write=true;auto r=s.apply({2,0,2});CHECK(!r.ok&&!r.rollback_verified&&s.state().recovery_pending&&header[10]==0);CHECK(s.state().rows[1].availability==VoltagePolicyAvailability::transient);blocked_get=0;CHECK(s.recover().ok&&header[10]==1&&!s.state().recovery_pending);}
 fresh();{VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);s.read();CHECK(s.apply({4,0,4}).ok);block_after_write=true;CHECK(!s.apply({2,0,2}).ok&&s.state().recovery_pending);blocked_get=0;CHECK(s.recover().ok&&s.state().undo_available&&s.state().undo_selection==4);CHECK(s.undo().ok&&header[10]==1&&header[11]==1);}
 fresh();{auto alternate=dev;VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),alternate);s.read();foreign=true;CHECK(!s.apply({1,1,0}).ok&&writes==0);}
 fresh();{std::strcpy(dev.uuid,"GPU-second-device");dev.nvapi_handle=99;dev.nvapi_devid=0x4567;VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);CHECK(s.read().rows[2].writable);CHECK(s.apply({1,1,0}).ok&&gate);}
 const auto final_writes=writes;CHECK(final_writes==1); // destruction never restores a session request
 std::printf("voltage policy service: %u checks\n",checks);return 0;
}
