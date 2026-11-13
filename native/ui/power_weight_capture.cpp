// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weight_capture.h"
#include "power_weight_registry_host.h"
#include <algorithm>
#include <cstring>
#include <cstdio>
#include <memory>
#include <utility>
namespace druta::ui {
namespace {
using A=VoltagePolicyAvailability;using Bytes=std::vector<uint8_t>;
struct IdentityError:VoltageEquationEvidenceError {IdentityError(const std::string& message,A kind=A::invalid):VoltageEquationEvidenceError(message,kind){}};
[[noreturn]]void fail(const std::string& message,A kind=A::invalid){throw VoltageEquationEvidenceError(message,kind);}
void require(bool condition,const char* message){if(!condition)fail(message);}
bool terminated(const char* text,size_t size){return std::memchr(text,0,size)!=nullptr;}
A error_kind(const dc_error& error){return error.kind==DC_ERR_TRANSIENT||error.kind==DC_ERR_UNSTABLE?A::transient:error.kind==DC_ERR_UNSUPPORTED?A::unsupported:A::invalid;}
[[noreturn]]void api_fail(const char* message,const dc_error& error){if(error.kind==DC_ERR_IDENTITY)throw IdentityError(message);fail(message,error_kind(error));}
bool target_equal(const dc_target& a,const dc_target& b){return a.size==sizeof(a)&&b.size==sizeof(b)&&terminated(a.slot,sizeof(a.slot))&&terminated(b.slot,sizeof(b.slot))&&a.slot[0]&&a.handle&&a.handle==b.handle&&std::strcmp(a.slot,b.slot)==0&&a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id;}
bool windows_equal(const VoltageScaffoldWindowsTarget& a,const VoltageScaffoldWindowsTarget& b){return a.slot==b.slot&&a.instance_id==b.instance_id&&a.driver_key==b.driver_key;}
struct Provider {
 PowerWeightCaptureApi api;dc_gpu_identity opened;
 Provider(PowerWeightCaptureApi value,const dc_gpu_identity& identity):api(std::move(value)),opened(identity){}
 dc_target bind(dc_gpu* gpu,dc_gpu_identity* adapter=nullptr){auto& a=api.evidence.target;
  if(!a.identity||!a.bind||!a.architecture_supported)throw IdentityError("Relationship paired-target interfaces unavailable",A::unsupported);
  dc_gpu_identity current{};current.size=sizeof(current);const auto status=a.identity(gpu,&current);if(status!=DC_OK)throw IdentityError("Relationship identity read failed",status==DC_E_ARGUMENT?A::invalid:A::transient);
  const bool valid=opened.size==sizeof(opened)&&current.size==sizeof(current)&&terminated(opened.slot,sizeof(opened.slot))&&terminated(current.slot,sizeof(current.slot))&&terminated(opened.uuid,sizeof(opened.uuid))&&terminated(current.uuid,sizeof(current.uuid))&&terminated(opened.pairing_error,sizeof(opened.pairing_error))&&terminated(current.pairing_error,sizeof(current.pairing_error));
  if(!valid||!opened.nvapi_ok||!opened.nvapi_selected||!current.nvapi_ok||!current.nvapi_selected||!current.slot[0]||current.pairing_error[0]||opened.pairing_error[0]||std::strcmp(current.slot,opened.slot)||std::strcmp(current.uuid,opened.uuid)||current.nvapi_handle!=opened.nvapi_handle||current.nvapi_devid!=opened.nvapi_devid||current.nvapi_subsys!=opened.nvapi_subsys||current.nvml_selected!=opened.nvml_selected||current.nvml_has_devid!=opened.nvml_has_devid||current.nvml_devid!=opened.nvml_devid||current.nvml_subsys!=opened.nvml_subsys)throw IdentityError("Relationship adapter identity/pairing changed");
  dc_target target{};target.size=sizeof(target);dc_error error{};error.size=sizeof(error);if(a.bind(gpu,1,&target,&error)!=DC_OK)throw IdentityError("Relationship PERF target unavailable",error_kind(error));
  if(!target_equal(target,target)||std::strcmp(target.slot,current.slot)||target.handle!=current.nvapi_handle||target.device_id!=current.nvapi_devid||target.subsystem_id!=current.nvapi_subsys)throw IdentityError("Relationship PERF binding differs from current paired adapter");if(!a.architecture_supported(target.architecture))throw IdentityError("Relationship architecture unavailable",A::unsupported);if(adapter)*adapter=current;return target;
 }
 void bound(dc_gpu* gpu,const dc_target& expected){if(!target_equal(bind(gpu),expected))throw IdentityError("Relationship target changed during capture");}
 Bytes image_once(dc_gpu* gpu,const dc_target& target){bound(gpu,target);Bytes result(DC_VBIOS_IMAGE_CAPACITY);uint32_t size=0;dc_error error{};error.size=sizeof(error);const auto status=api.evidence.image(gpu,result.data(),DC_VBIOS_IMAGE_CAPACITY,&size,&error);bound(gpu,target);if(status!=DC_OK)api_fail("Current relationship ROM capture failed",error);require(size>=32&&size<=DC_VBIOS_IMAGE_CAPACITY,"Current relationship ROM length malformed");result.resize(size);return result;}
 Bytes image(dc_gpu* gpu,const dc_target& target){if(!api.evidence.image)fail("Current ROM getter unavailable",A::unsupported);for(unsigned attempt=0;attempt<3;++attempt){try{auto first=image_once(gpu,target),second=image_once(gpu,target);if(first==second)return second;if(attempt==2)fail("Current ROM changed between read pairs",A::transient);}catch(const IdentityError&){throw;}catch(const VoltageEquationEvidenceError& error){if(error.kind!=A::transient||attempt==2)throw;}}fail("ROM stability unavailable",A::transient);}
 struct Info {PowerRelationshipLayout layout;Bytes bytes;};
 Info info_once(dc_gpu* gpu,const dc_target& target){bound(gpu,target);dc_power_layout native{};native.size=sizeof(native);uint32_t index=0;dc_error error{};error.size=sizeof(error);const auto status=api.abi(gpu,&native,&index,&error);bound(gpu,target);if(status!=DC_OK)api_fail("Power policy INFO layout unavailable",error);
  require(native.size==sizeof(native)&&(native.has_commands==0||native.has_commands==1),"Power policy layout descriptor malformed");const auto command=native.has_commands?native.commands[0]:0x2080a618u;
  if(command!=0x2080a618||native.sizes[0]!=20512||native.info_base!=0xcc||native.info_stride!=0x104){char detail[192];std::snprintf(detail,sizeof(detail),"Relationship INFO command/geometry unqualified: command 0x%08X, size %u, base 0x%X, stride 0x%X",command,native.sizes[0],native.info_base,native.info_stride);fail(detail,A::unsupported);}
  std::vector<uint32_t> words(20512/4);uint32_t count=0;error={};error.size=sizeof(error);const auto read=api.rm(gpu,command,nullptr,0,0,0,0,words.data(),static_cast<uint32_t>(words.size()),&count,&error);bound(gpu,target);if(read!=DC_OK)api_fail("Relationship INFO read failed",error);require(count==words.size(),"Relationship INFO returned incomplete words");
  Info result;result.layout={command,native.sizes[0],native.info_base,native.info_stride};result.bytes.resize(20512);std::memcpy(result.bytes.data(),words.data(),20512);const auto decoded=decode_power_relationships(result.layout,result.bytes);require(decoded.available,"Relationship INFO decode unavailable");return result;
 }
 Info info(dc_gpu* gpu,const dc_target& target){if(!api.abi||!api.rm)fail("Relationship INFO interfaces unavailable",A::unsupported);for(unsigned attempt=0;attempt<2;++attempt){try{auto first=info_once(gpu,target),second=info_once(gpu,target);if(first.bytes==second.bytes)return second;fail("Relationship INFO changed between matching reads",A::transient);}catch(const IdentityError&){throw;}catch(const VoltageEquationEvidenceError& error){if(error.kind!=A::transient||attempt)throw;if(api.evidence.target.reset_transport)api.evidence.target.reset_transport(gpu,DC_TRANSPORT_POWER);}}fail("Relationship INFO stability unavailable",A::transient);}
 VoltageScaffoldWindowsTarget windows(dc_gpu* gpu,const dc_target& target){bound(gpu,target);auto result=api.resolve_windows?api.resolve_windows(opened,target):resolve_voltage_scaffold_windows_target(opened,target);bound(gpu,target);
  if(result.slot!=target.slot)throw IdentityError("Windows relationship target names another adapter");require(!result.instance_id.empty()&&!result.driver_key.empty()&&result.instance_id.find('\0')==std::string::npos&&result.driver_key.find('\0')==std::string::npos,"Windows relationship target malformed");return result;}
 template<class F>void component(A& availability,std::string& message,F&& work){try{work();availability=A::available;}catch(const IdentityError&){throw;}catch(const VoltageEquationEvidenceError& error){availability=error.kind;message=error.what();}catch(const std::exception& error){availability=A::invalid;message=error.what();}}
 PowerWeightCapture read(dc_gpu* gpu,bool registry_requested){auto& a=api.evidence.target;if(!a.lock||!a.unlock)fail("Relationship capture lock interface unavailable",A::unsupported);if(a.lock(gpu)!=DC_OK)fail("Relationship capture lock unavailable",A::transient);struct Unlock{VoltagePolicyApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}}unlock{a,gpu};
  PowerWeightCapture result;result.target=bind(gpu,&result.adapter);result.identity={result.target.slot,result.target.architecture,result.target.device_id,result.target.subsystem_id};
  component(result.image_availability,result.image_message,[&]{result.image=image(gpu,result.target);result.image_message="Matching current ROM reads captured";});
  component(result.info_availability,result.info_message,[&]{const auto captured=info(gpu,result.target);result.info=captured.bytes;result.layout=captured.layout;result.info_message="Matching configured relationship INFO captured";});
  if(registry_requested)component(result.registry_availability,result.registry_message,[&]{const auto first=windows(gpu,result.target);bound(gpu,result.target);auto registry=api.registry_read?api.registry_read(first):read_power_weight_registry(first);bound(gpu,result.target);const auto last=windows(gpu,result.target);if(!windows_equal(first,last))throw IdentityError("Windows instance/driver key changed during registry read");result.windows=last;result.registry=std::move(registry);result.registry_message="Current managed registry snapshot captured read-only";});
  else result.registry_message="Registry capture not requested";
  const auto final=bind(gpu,&result.adapter);if(!target_equal(final,result.target))throw IdentityError("Relationship final adapter binding changed");return result;
 }
};
}
std::function<PowerWeightCapture(dc_gpu*,bool)> make_power_weight_capture(PowerWeightCaptureApi api,const dc_gpu_identity& identity){auto provider=std::make_shared<Provider>(std::move(api),identity);return [provider](dc_gpu* gpu,bool registry){return provider->read(gpu,registry);};}
}
