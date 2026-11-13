// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/vbios_image.h"
#include "backend/client_power_controller.h"
#include "core/slot.h"
#include <cstddef>
#include <cstring>
namespace druta::backend {
namespace {
struct alignas(8) Descriptor {uint32_t version=0,size=0;uint64_t buffer=0;};
static_assert(sizeof(Descriptor)==16&&offsetof(Descriptor,buffer)==8,"VBIOS V2 wire descriptor geometry");
using Getter=int(DRUTA_CDECL*)(void*,void*);
using Pci=int(DRUTA_CDECL*)(void*,unsigned*,unsigned*,unsigned*,unsigned*);
using Bus=int(DRUTA_CDECL*)(void*,unsigned*);
RmError failure(RmErrorKind kind,const std::string& message){RmError out;out.kind=kind;out.fixed=kind==RmErrorKind::unsupported||kind==RmErrorKind::malformed;out.message=message;return out;}
bool same(const PciSlot& a,const PciSlot& b){return a.domain==b.domain&&a.bus==b.bus&&a.device==b.device&&a.function==b.function;}
struct Identity {void* handle=nullptr;PciSlot slot;uint32_t device=0,subsystem=0;};
bool binding(Gpu& gpu,Identity& out,RmError& error){const auto& api=gpu.nvapi;
 const auto reject=[&](RmErrorKind kind,const char* message){error=failure(kind,message);return false;};
 if(!gpu.pairing_error.empty())return reject(RmErrorKind::identity,"VBIOS target pairing failed");
 if(!api.ok||!api.gpu||!api.selected||api.gpu!=api.selected->handle)return reject(RmErrorKind::identity,"VBIOS requires the selected paired NVAPI handle");
 const auto selected=parse_slot(api.selected->slot),actual=parse_slot(gpu.slot());
 if(!selected||!actual||!same(*selected,*actual))return reject(RmErrorKind::identity,"VBIOS handle does not identify the exact selected PCI slot");
 if(gpu.nvml.selected){const auto& other=*gpu.nvml.selected;const auto slot=parse_slot(other.slot);
  if(!slot||!same(*slot,*actual)||(other.devid&&*other.devid!=api.selected->devid)||(other.subsys&&*other.subsys!=api.selected->subsys))return reject(RmErrorKind::identity,"VBIOS NVAPI and NVML identities disagree");}
 const auto pci=api.get<Pci>(nvapi_id::GetPCIIdentifiers);const auto bus=api.get<Bus>(nvapi_id::GetBusId);const auto device=api.get<Bus>(nvapi_id::GetBusSlotId);
 if(!pci||!bus||!device)return reject(RmErrorKind::identity,"VBIOS PCI identity interfaces unavailable");
 unsigned id=0,subsystem=0,revision=0,external=0,bus_id=0,device_id=0;
 if(pci(api.gpu,&id,&subsystem,&revision,&external)!=0||bus(api.gpu,&bus_id)!=0||device(api.gpu,&device_id)!=0)return reject(RmErrorKind::transient,"VBIOS current PCI identity read failed");
 if((id&0xffffu)!=0x10deu||(id>>16)!=api.selected->devid||subsystem!=api.selected->subsys||bus_id!=actual->bus||device_id!=actual->device||actual->function!=0)return reject(RmErrorKind::identity,"VBIOS current PCI identity differs from the opened paired target");
 out={api.gpu,*actual,id>>16,subsystem};return true;
}
}
VbiosImageResult read_vbios_image(Gpu& gpu){VbiosImageResult out;
 if constexpr(sizeof(void*)!=8){out.error=failure(RmErrorKind::unsupported,"VBIOS getter requires the understood 64-bit V2 descriptor");return out;}
 Identity before;if(!binding(gpu,before,out.error))return out;
 const auto getter=gpu.nvapi.get<Getter>(kVbiosImageExport);if(!getter){out.error=failure(RmErrorKind::unsupported,"VBIOS image getter export unavailable");return out;}
 GuardedBuffer image(kVbiosImageCapacity),packet(sizeof(Descriptor));Descriptor request;request.version=kVbiosImageVersion;request.size=kVbiosImageCapacity;request.buffer=static_cast<uint64_t>(reinterpret_cast<uintptr_t>(image.data()));std::memcpy(packet.data(),&request,sizeof(request));
 const int status=getter(before.handle,packet.data());Descriptor returned;std::memcpy(&returned,packet.data(),sizeof(returned));
 if(!packet.guards_intact()||!image.guards_intact()){out.error=failure(RmErrorKind::malformed,"VBIOS getter exceeded its guarded descriptor or image allocation");return out;}
 Identity after;if(!binding(gpu,after,out.error))return out;
 if(before.handle!=after.handle||!same(before.slot,after.slot)||before.device!=after.device||before.subsystem!=after.subsystem){out.error=failure(RmErrorKind::identity,"VBIOS paired target changed during image capture");return out;}
 if(returned.version!=request.version||returned.buffer!=request.buffer||returned.size<32||returned.size>kVbiosImageCapacity){out.error=failure(RmErrorKind::malformed,"VBIOS getter changed descriptor version, buffer pointer or bounded image length");return out;}
 if(status!=0){out.error=failure(RmErrorKind::transient,"VBIOS getter returned NVAPI status "+std::to_string(status));return out;}
 out.image.assign(image.data(),image.data()+returned.size);return out;
}
}
