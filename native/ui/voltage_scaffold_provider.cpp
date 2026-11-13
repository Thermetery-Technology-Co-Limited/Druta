// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_scaffold_provider.h"
#include "voltage_variable_capture.h"
#include <windows.h>
#include <algorithm>
#include <cstring>
#include <filesystem>
#include <memory>
namespace druta::ui {
namespace {
using Bytes=std::vector<uint8_t>;using Availability=VoltagePolicyAvailability;
[[noreturn]]void fail(const std::string& message,Availability kind=Availability::invalid){throw VoltageEquationEvidenceError(message,kind);}
void require(bool ok,const char* message){if(!ok)fail(message);}
bool bounded(const char* text,size_t size){return std::memchr(text,0,size)!=nullptr;}
Availability error_kind(const dc_error& error){return error.kind==DC_ERR_TRANSIENT||error.kind==DC_ERR_UNSTABLE?Availability::transient:error.kind==DC_ERR_UNSUPPORTED?Availability::unsupported:Availability::invalid;}
bool same_target(const dc_target& a,const dc_target& b){return a.size==sizeof(a)&&b.size==sizeof(b)&&bounded(a.slot,sizeof(a.slot))&&bounded(b.slot,sizeof(b.slot))&&a.slot[0]&&a.handle&&a.handle==b.handle&&std::strcmp(a.slot,b.slot)==0&&a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id;}
bool same_windows(const VoltageScaffoldWindowsTarget& a,const VoltageScaffoldWindowsTarget& b){return a.slot==b.slot&&a.instance_id==b.instance_id&&a.driver_key==b.driver_key;}
bool same_head(const InstalledVoltageScaffoldHead& a,const InstalledVoltageScaffoldHead& b){return a.rail==b.rail&&a.role==b.role&&a.index==b.index&&a.next==b.next&&a.variable==b.variable&&a.output==b.output&&a.selector_offset==b.selector_offset&&a.row_offset==b.row_offset;}
std::wstring environment(const wchar_t* name){const auto size=GetEnvironmentVariableW(name,nullptr,0);if(!size)return {};std::vector<wchar_t> buffer(size);const auto got=GetEnvironmentVariableW(name,buffer.data(),size);if(!got||got>=size)fail("Scaffold state environment changed during lookup",Availability::transient);return {buffer.data(),got};}
std::wstring default_directory(){auto base=environment(L"LOCALAPPDATA");if(base.empty())base=environment(L"USERPROFILE");if(base.empty())base=environment(L"HOMEDRIVE")+environment(L"HOMEPATH");if(base.empty())fail("Scaffold state home directory unavailable",Availability::unsupported);return (std::filesystem::path(base)/L"Druta").wstring();}
struct Provider {
 VoltageEvidenceApi api;dc_gpu_identity opened;VoltageScaffoldHostApi host;
 Provider(VoltageEvidenceApi value,const dc_gpu_identity& id,VoltageScaffoldHostApi callbacks):api(std::move(value)),opened(id),host(std::move(callbacks)){}
 dc_target bind(dc_gpu* gpu){
  if(!api.target.identity||!api.target.bind||!api.target.architecture_supported)fail("Scaffold paired-target interfaces unavailable",Availability::unsupported);
  dc_gpu_identity current{};current.size=sizeof(current);const auto status=api.target.identity(gpu,&current);if(status!=DC_OK)fail("Scaffold identity read failed",status==DC_E_ARGUMENT?Availability::invalid:Availability::transient);
  require(opened.size==sizeof(opened)&&current.size==sizeof(current)&&bounded(opened.slot,sizeof(opened.slot))&&bounded(current.slot,sizeof(current.slot))&&bounded(opened.uuid,sizeof(opened.uuid))&&bounded(current.uuid,sizeof(current.uuid))&&bounded(opened.pairing_error,sizeof(opened.pairing_error))&&bounded(current.pairing_error,sizeof(current.pairing_error)),"Scaffold identity strings malformed");
  require(current.nvapi_ok&&current.nvapi_selected&&current.slot[0]&&!current.pairing_error[0]&&!opened.pairing_error[0]&&std::strcmp(current.slot,opened.slot)==0&&std::strcmp(current.uuid,opened.uuid)==0&&current.nvapi_handle==opened.nvapi_handle&&current.nvapi_devid==opened.nvapi_devid&&current.nvapi_subsys==opened.nvapi_subsys&&current.nvml_selected==opened.nvml_selected&&current.nvml_has_devid==opened.nvml_has_devid&&current.nvml_devid==opened.nvml_devid&&current.nvml_subsys==opened.nvml_subsys,"Scaffold selected adapter identity changed");
  dc_target target{};target.size=sizeof(target);dc_error error{};error.size=sizeof(error);if(api.target.bind(gpu,1,&target,&error)!=DC_OK)fail("Scaffold PERF target unavailable",error_kind(error));
  require(same_target(target,target)&&std::strcmp(target.slot,current.slot)==0&&target.handle==current.nvapi_handle&&target.device_id==current.nvapi_devid&&target.subsystem_id==current.nvapi_subsys,"Scaffold PERF target differs from paired adapter");if(!api.target.architecture_supported(target.architecture))fail("Scaffold architecture unsupported",Availability::unsupported);return target;
 }
 void bound(dc_gpu* gpu,const dc_target& target){require(same_target(bind(gpu),target),"Scaffold target binding changed during read");}
 std::string hash(const Bytes& bytes){if(!api.sha256)fail("Scaffold SHA256 interface unavailable",Availability::unsupported);char output[65]{};if(api.sha256(bytes.data(),bytes.size(),output)!=DC_OK)fail("Scaffold SHA256 failed",Availability::unsupported);require(output[64]==0&&std::strlen(output)==64&&std::string(output).find_first_not_of("0123456789abcdef")==std::string::npos,"Scaffold SHA256 result malformed");return output;}
 Bytes image_once(dc_gpu* gpu,const dc_target& target){bound(gpu,target);Bytes bytes(DC_VBIOS_IMAGE_CAPACITY);uint32_t size=0;dc_error error{};error.size=sizeof(error);if(api.image(gpu,bytes.data(),DC_VBIOS_IMAGE_CAPACITY,&size,&error)!=DC_OK)fail("Installed scaffold current ROM read failed",error_kind(error));require(size>=32&&size<=DC_VBIOS_IMAGE_CAPACITY,"Scaffold ROM extent malformed");bytes.resize(size);bound(gpu,target);return bytes;}
 Bytes image(dc_gpu* gpu,const dc_target& target){for(unsigned attempt=0;attempt<3;++attempt){try{auto a=image_once(gpu,target),b=image_once(gpu,target);if(a==b)return b;if(attempt==2)fail("Scaffold current ROM differs between reads",Availability::transient);}catch(const VoltageEquationEvidenceError& error){if(error.kind!=Availability::transient||attempt==2)throw;}}fail("Scaffold current ROM stability unavailable",Availability::transient);}
 VoltageScaffoldWindowsTarget windows(dc_gpu* gpu,const dc_target& target){bound(gpu,target);auto result=host.resolve_windows?host.resolve_windows(opened,target):resolve_voltage_scaffold_windows_target(opened,target);
  require(result.slot==target.slot&&!result.instance_id.empty()&&!result.driver_key.empty()&&result.instance_id.size()<=4096&&result.driver_key.size()<=4096&&result.instance_id.find('\0')==std::string::npos&&result.driver_key.find('\0')==std::string::npos,"Scaffold Windows target malformed or mismatched");bound(gpu,target);return result;}
 VoltageScaffoldReceipt receipt(dc_gpu* gpu,const std::wstring& directory,const dc_target& target,const VoltageScaffoldWindowsTarget& windows_target){bound(gpu,target);
  if(!host.load_receipt){const auto path=voltage_scaffold_receipt_path(directory,target.slot);const auto attributes=GetFileAttributesW(path.c_str());if(attributes==INVALID_FILE_ATTRIBUTES){const auto error=GetLastError();fail("Installed scaffold receipt unavailable",error==ERROR_FILE_NOT_FOUND||error==ERROR_PATH_NOT_FOUND?Availability::unsupported:Availability::transient);}}
  auto result=host.load_receipt?host.load_receipt(directory,target,windows_target):read_voltage_scaffold_receipt(directory,target,windows_target);
  require(result.slot==target.slot&&same_windows(result.windows_target,windows_target)&&same_target(result.before.target,target),"Scaffold receipt target differs from current Windows/adapter identity");bound(gpu,target);return result;}
 std::string fingerprint(const VoltageScaffoldReceipt& receipt){
  require(!receipt.requests.empty()&&receipt.requests.size()<=8&&receipt.recorded_heads.size()==receipt.requests.size()&&receipt.historical_values.size()<=8,"Scaffold receipt inventory malformed");
  Bytes bytes;const auto integer=[&](uint64_t value){for(unsigned i=0;i<8;++i)bytes.push_back(uint8_t(value>>(8*i)));};
  const auto text=[&](const std::string& value){require(value.size()<=4096&&value.find('\0')==std::string::npos,"Scaffold receipt string malformed");integer(value.size());bytes.insert(bytes.end(),value.begin(),value.end());};
  const auto blob=[&](const Bytes& value,size_t maximum){require(value.size()<=maximum,"Scaffold receipt payload exceeds bounds");integer(value.size());bytes.insert(bytes.end(),value.begin(),value.end());};
  require(receipt.source_sha256.size()==64&&receipt.source_sha256.find_first_not_of("0123456789abcdef")==std::string::npos,"Scaffold receipt source fingerprint unavailable");
  text(receipt.source_sha256);text(receipt.slot);text(receipt.windows_target.slot);text(receipt.windows_target.instance_id);text(receipt.windows_target.driver_key);
  const auto& t=receipt.before.target;text(t.slot);integer(t.architecture);integer(t.device_id);integer(t.subsystem_id);integer(t.handle);
  blob(receipt.original_image,vbios_voltage_max_image_bytes);blob(receipt.before.info,0xe0408);blob(receipt.before.control,0x100408);
  integer(receipt.requests.size());for(const auto& request:receipt.requests){integer(request.first);integer(static_cast<uint32_t>(request.second));}
  for(const auto& head:receipt.recorded_heads){integer(head.rail);integer(static_cast<uint32_t>(head.role));integer(head.index);integer(head.next);integer(head.variable);integer(head.output);integer(head.selector_offset);integer(head.row_offset);}
  integer(receipt.historical_values.size());for(const auto& value:receipt.historical_values){text(value.first);integer(static_cast<uint64_t>(value.second));}
  return hash(bytes);
 }
 VoltageScaffoldRead read(dc_gpu* gpu){
  if(!api.target.lock||!api.target.unlock||!api.image)fail("Scaffold read interfaces unavailable",Availability::unsupported);
  if(api.target.lock(gpu)!=DC_OK)fail("Scaffold read lock unavailable",Availability::transient);
  struct Unlock {VoltagePolicyApi& api;dc_gpu* gpu;~Unlock(){api.unlock(gpu);}} unlock{api.target,gpu};
  try{const auto target=bind(gpu);const auto directory=host.state_directory?host.state_directory():default_directory();
   require(!directory.empty()&&directory.find(L'\0')==std::wstring::npos&&std::filesystem::path(directory).is_absolute(),"Scaffold state directory must be absolute");const auto selected_windows=windows(gpu,target);const auto saved=receipt(gpu,directory,target,selected_windows);const auto binding=fingerprint(saved);
   const auto current_image=image(gpu,target);bound(gpu,target);const auto variables=capture_voltage_variables(api.target,gpu,opened);require(same_target(variables.target,target),"Scaffold variables target differs");bound(gpu,target);
   VoltageEquationPackets loaded;if(host.capture_equations)loaded=host.capture_equations(gpu);else{VoltageLimitsApi raw;raw.transport=api.target;raw.interface_present=api.interface_present;VoltageLimitsService service(raw,gpu,opened);loaded=service.capture_equations();}
   require(same_target(loaded.target,target),"Scaffold raw equation target differs");bound(gpu,target);
   const auto plan=plan_installed_voltage_scaffold(saved.original_image,saved.before,variables.bytes,saved.requests);if(!plan.ok)fail("Installed scaffold plan unqualified: "+plan.message,Availability::unsupported);
   require(plan.heads.size()==saved.recorded_heads.size(),"Receipt head inventory differs from derived plan");for(size_t i=0;i<plan.heads.size();++i)require(same_head(plan.heads[i],saved.recorded_heads[i]),"Receipt head binding differs from independently derived plan");
   const auto recognized=recognize_installed_voltage_scaffold(plan,current_image,loaded);if(!recognized.ok)fail("Installed scaffold recognition failed: "+recognized.message);
   const auto final_directory=host.state_directory?host.state_directory():default_directory();require(final_directory==directory,"Scaffold state directory changed during read");const auto final_windows=windows(gpu,target);require(same_windows(selected_windows,final_windows),"Scaffold Windows target changed during read");
   const auto final_receipt=receipt(gpu,directory,target,final_windows);require(fingerprint(final_receipt)==binding,"Scaffold receipt changed during read");bound(gpu,target);
   VoltageScaffoldRead result;result.target=target;result.heads=recognized.heads;result.equations=std::make_shared<const VoltageEquationPackets>(std::move(loaded));result.receipt_binding=binding;result.image_sha256=hash(current_image);result.message="Installed signed offset requests inspected; no write/profile authority";return result;
  }catch(const VoltageEquationEvidenceError&){throw;}catch(const std::exception& error){fail(error.what());}
 }
};
}
std::function<VoltageScaffoldRead(dc_gpu*)> make_voltage_scaffold_provider(VoltageEvidenceApi api,const dc_gpu_identity& opened,VoltageScaffoldHostApi host){auto provider=std::make_shared<Provider>(std::move(api),opened,std::move(host));return [provider](dc_gpu* gpu){return provider->read(gpu);};}
}
