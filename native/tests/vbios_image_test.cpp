// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Resolver-injected real C ABI; no driver DLL or GPU access.
#include <drutacore/vbios_image.h>
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <vector>
namespace {
unsigned checks=0,failures=0,calls=0,bus_id=2,device_id=3,pci_id=0x1e0210de,subsystem=0x44556677,length=512;
int mode=0;bool available=true,identity_available=true,identity_failure=false;void* selected=reinterpret_cast<void*>(0x1234);
#define CHECK(x) do{++checks;if(!(x)){++failures;std::fprintf(stderr,"line %d: %s\n",__LINE__,#x);}}while(false)
struct alignas(8) Descriptor {uint32_t version,size;uint64_t buffer;};
int DC_CALL init(){return 0;}
int DC_CALL enumerate(void** handles,unsigned* count){handles[0]=selected;*count=1;return 0;}
int DC_CALL pci(void* handle,unsigned* id,unsigned* sub,unsigned* revision,unsigned* external){CHECK(handle==selected);*id=pci_id;*sub=subsystem;*revision=*external=0;return identity_failure?-5:0;}
int DC_CALL bus(void*,unsigned* out){*out=bus_id;return identity_failure?-5:0;}
int DC_CALL device(void*,unsigned* out){*out=device_id;return 0;}
int DC_CALL image(void* handle,void* block){++calls;auto* descriptor=static_cast<Descriptor*>(block);CHECK(handle==selected&&descriptor->version==0x20010&&descriptor->size==0xfa000&&descriptor->buffer!=0);
 auto* data=reinterpret_cast<uint8_t*>(static_cast<uintptr_t>(descriptor->buffer));for(uint32_t i=0;i<std::min(length,DC_VBIOS_IMAGE_CAPACITY);++i)data[i]=static_cast<uint8_t>(i*17u+3u);descriptor->size=length;
 switch(mode){case 1:descriptor->version^=1;break;case 2:descriptor->buffer=0;break;case 3:descriptor->size=31;break;case 4:descriptor->size=DC_VBIOS_IMAGE_CAPACITY+1;break;
  case 5:static_cast<uint8_t*>(block)[-1]^=1;break;case 6:static_cast<uint8_t*>(block)[16]^=1;break;case 7:data[-1]^=1;break;case 8:data[DC_VBIOS_IMAGE_CAPACITY]^=1;break;
  case 9:return -17;case 10:++bus_id;break;case 11:subsystem^=1;break;case 12:identity_failure=true;break;case 13:identity_available=false;break;default:break;}
 return 0;
}
void* DC_CALL query(unsigned id){switch(id){case 0x0150e828:return reinterpret_cast<void*>(init);case 0xe5ac921f:return reinterpret_cast<void*>(enumerate);case 0x2ddfb66e:return identity_available?reinterpret_cast<void*>(pci):nullptr;
 case 0x1be0b8e5:return identity_available?reinterpret_cast<void*>(bus):nullptr;case 0x2a0a350f:return reinterpret_cast<void*>(device);case 0xfc13ee11:return available?reinterpret_cast<void*>(image):nullptr;default:return nullptr;}}
void* DC_CALL resolve(void*,const char* library,const char* name){if(std::strcmp(library,"nvapi"))return nullptr;if(!*name)return reinterpret_cast<void*>(1);return std::strcmp(name,"nvapi_QueryInterface")==0?reinterpret_cast<void*>(query):nullptr;}
dc_gpu* opened(const char* slot){dc_gpu* gpu=nullptr;CHECK(dc_gpu_open(slot,&gpu)==DC_OK&&gpu);return gpu;}
}
int main(){dc_set_export_resolver(resolve,nullptr);auto* gpu=opened("0000:02:03.0");std::vector<uint8_t> bytes(DC_VBIOS_IMAGE_CAPACITY+8,0xcd);uint32_t size=0xaabbccdd;dc_error error{};error.size=sizeof(error);
 CHECK(dc_gpu_vbios_image_get(nullptr,bytes.data(),DC_VBIOS_IMAGE_CAPACITY,&size,&error)==DC_E_ARGUMENT&&calls==0);
 CHECK(dc_gpu_vbios_image_get(gpu,bytes.data(),31,&size,&error)==DC_E_BUFFER&&calls==0&&size==0xaabbccdd);
 error.size=0;CHECK(dc_gpu_vbios_image_get(gpu,bytes.data(),DC_VBIOS_IMAGE_CAPACITY,&size,&error)==DC_E_ARGUMENT&&calls==0);error.size=sizeof(error);
 if constexpr(sizeof(void*)!=8){CHECK(dc_gpu_vbios_image_get(gpu,bytes.data(),DC_VBIOS_IMAGE_CAPACITY,&size,&error)==DC_E_UNAVAILABLE&&error.kind==DC_ERR_UNSUPPORTED&&calls==0);dc_gpu_close(gpu);dc_set_export_resolver(nullptr,nullptr);return failures?1:0;}
 CHECK(dc_gpu_vbios_image_get(gpu,bytes.data(),DC_VBIOS_IMAGE_CAPACITY,&size,&error)==DC_OK&&size==length&&calls==1&&error.kind==DC_ERR_NONE);
 CHECK(bytes[0]==3&&bytes[length-1]==static_cast<uint8_t>((length-1)*17u+3u)&&bytes[length]==0xcd);
 for(int failure_mode=1;failure_mode<=13;++failure_mode){mode=failure_mode;const auto old_bus=bus_id,old_subsystem=subsystem;bytes.assign(bytes.size(),0xcd);const auto before=bytes;size=0xaabbccdd;const auto before_calls=calls;
  CHECK(dc_gpu_vbios_image_get(gpu,bytes.data(),DC_VBIOS_IMAGE_CAPACITY,&size,&error)==DC_E_FAILED&&bytes==before&&size==0xaabbccdd&&calls==before_calls+1);
  CHECK(error.kind==(mode==9||mode==12?DC_ERR_TRANSIENT:mode>=10?DC_ERR_IDENTITY:DC_ERR_MALFORMED));bus_id=old_bus;subsystem=old_subsystem;identity_failure=false;identity_available=true;
 }
 mode=0;available=false;const auto before_calls=calls;CHECK(dc_gpu_vbios_image_get(gpu,bytes.data(),DC_VBIOS_IMAGE_CAPACITY,&size,&error)==DC_E_UNAVAILABLE&&error.kind==DC_ERR_UNSUPPORTED&&calls==before_calls&&size==0xaabbccdd);available=true;
 const auto before=bytes;CHECK(dc_gpu_vbios_image_get(gpu,bytes.data(),32,&size,&error)==DC_E_BUFFER&&size==0xaabbccdd&&bytes==before);
 for(uint32_t count:{32u,DC_VBIOS_IMAGE_CAPACITY}){length=count;size=0;bytes.assign(bytes.size(),0xcd);CHECK(dc_gpu_vbios_image_get(gpu,bytes.data(),DC_VBIOS_IMAGE_CAPACITY,&size,&error)==DC_OK&&size==count&&bytes[count]==0xcd);}
 dc_gpu_close(gpu);bus_id=9;device_id=1;pci_id=0x2c0210de;subsystem=0x89abcdef;selected=reinterpret_cast<void*>(0x9876);length=1024;gpu=opened("0000:09:01.0");
 CHECK(dc_gpu_vbios_image_get(gpu,bytes.data(),DC_VBIOS_IMAGE_CAPACITY,&size,&error)==DC_OK&&size==1024); // No architecture export or board identity gate.
 ++subsystem;const auto saved=bytes;size=123;const auto old_calls=calls;CHECK(dc_gpu_vbios_image_get(gpu,bytes.data(),DC_VBIOS_IMAGE_CAPACITY,&size,&error)==DC_E_FAILED&&error.kind==DC_ERR_IDENTITY&&bytes==saved&&size==123&&calls==old_calls);
 dc_gpu_close(gpu);dc_set_export_resolver(nullptr,nullptr);std::printf("VBIOS image ABI: %u checks, %u failures\n",checks,failures);return failures?1:0;
}
