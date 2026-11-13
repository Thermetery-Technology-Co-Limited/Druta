// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Injectable NvAPI only: no hardware access.
#include <drutacore/drutacore.h>
#include <array>
#include <cstdio>
#include <cstring>
namespace {
int failures=0,calls=0;bool export_available=true,mutate=false,overflow=false;void* selected=reinterpret_cast<void*>(0x1234);
std::array<uint8_t,DC_RAILS_REQUEST_PACKET_BYTES> expected{};
void check(bool v,const char* s){if(!v){++failures;std::printf("FAIL: %s\n",s);}}
int DC_CALL init(){return 0;}int DC_CALL enumerate(void** out,unsigned* n){out[0]=selected;*n=1;return 0;}
int DC_CALL pci(void*,unsigned* id,unsigned* sub,unsigned* rev,unsigned* ext){*id=0x567810de;*sub=0x12345678;*rev=*ext=0;return 0;}
int DC_CALL bus(void*,unsigned* out){*out=2;return 0;}int DC_CALL device(void*,unsigned* out){*out=3;return 0;}
int DC_CALL set(void* gpu,void* bytes){++calls;check(gpu==selected && std::memcmp(bytes,expected.data(),expected.size())==0,"complete exact packet and paired handle");if(mutate)static_cast<uint8_t*>(bytes)[100]^=1;if(overflow)static_cast<uint8_t*>(bytes)[DC_RAILS_REQUEST_PACKET_BYTES]^=1;return -5;}
void* DC_CALL query(unsigned id){switch(id){case 0x0150E828:return reinterpret_cast<void*>(init);case 0xE5AC921F:return reinterpret_cast<void*>(enumerate);
 case 0x2DDFB66E:return reinterpret_cast<void*>(pci);case 0x1BE0B8E5:return reinterpret_cast<void*>(bus);case 0x2A0A350F:return reinterpret_cast<void*>(device);
 case 0x87C55C8A:return export_available?reinterpret_cast<void*>(set):nullptr;default:return nullptr;}}
void* DC_CALL resolve(void*,const char* library,const char* name){if(std::strcmp(library,"nvapi"))return nullptr;if(!*name)return reinterpret_cast<void*>(1);return std::strcmp(name,"nvapi_QueryInterface")?nullptr:reinterpret_cast<void*>(query);}
}
int main(){dc_set_export_resolver(resolve,nullptr);dc_gpu* gpu=nullptr;check(dc_gpu_open("0000:02:03.0",&gpu)==DC_OK && gpu,"open fake paired GPU");
 for(size_t i=0;i<expected.size();++i)expected[i]=static_cast<uint8_t>(i*17);uint32_t version=DC_RAILS_CONTROL_VERSION;std::memcpy(expected.data(),&version,sizeof(version));
 auto packet=expected;int32_t status=123;dc_error error{};error.size=sizeof(error);
 check(dc_rails_request_packet_set(gpu,packet.data(),packet.size()-1,&status,&error)==DC_E_ARGUMENT && calls==0,"wrong size refuses before dispatch");
 packet[0]^=1;check(dc_rails_request_packet_set(gpu,packet.data(),packet.size(),&status,&error)==DC_E_ARGUMENT && calls==0,"wrong version refuses before dispatch");packet=expected;
 export_available=false;check(dc_rails_request_packet_set(gpu,packet.data(),packet.size(),&status,&error)==DC_E_UNAVAILABLE && calls==0 && error.kind==DC_ERR_UNSUPPORTED,"missing export is guaranteed not dispatched");
 export_available=true;check(dc_rails_request_packet_set(gpu,packet.data(),packet.size(),&status,&error)==DC_OK && calls==1 && status==-5 && packet==expected,"driver status preserved and input not modified");
 mutate=true;check(dc_rails_request_packet_set(gpu,packet.data(),packet.size(),&status,&error)==DC_E_FAILED && calls==2 && error.kind==DC_ERR_READBACK && packet==expected,"mutated dispatched packet fails uncertain without retry");
 mutate=false;overflow=true;check(dc_rails_request_packet_set(gpu,packet.data(),packet.size(),&status,&error)==DC_E_FAILED && calls==3 && error.kind==DC_ERR_READBACK && packet==expected,"guard detects bounded post-packet write without retry");
 dc_gpu_close(gpu);dc_set_export_resolver(nullptr,nullptr);std::printf("Rail request packet: %d failures\n",failures);return failures?1:0;}
