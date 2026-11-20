// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "device_recovery.h"
#include "profile_io.h"
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <algorithm>
#include <stdexcept>
#include <vector>
#include <iterator>
namespace druta::ui {
namespace {
bool absolute(const std::wstring& p){return p.size()>=3&&((p[1]==L':'&&(p[2]==L'\\'||p[2]==L'/'))||(p[0]==L'\\'&&p[1]==L'\\'));}
std::wstring full(const std::wstring& p){if(!absolute(p)||p.find(L'"')!=std::wstring::npos)return {};DWORD n=GetFullPathNameW(p.c_str(),0,nullptr,nullptr);if(!n||n>32767)return {};std::vector<wchar_t> b(n+1);DWORD got=GetFullPathNameW(p.c_str(),static_cast<DWORD>(b.size()),b.data(),nullptr);return got&&got<b.size()?std::wstring(b.data(),got):L"";}
std::wstring quote(const std::wstring& s){std::wstring out=L"\"";size_t slash=0;for(auto c:s){if(c==L'\\'){++slash;continue;}if(c==L'"'){out.append(slash*2+1,L'\\');out+=c;}else{out.append(slash,L'\\');out+=c;}slash=0;}out.append(slash*2,L'\\');return out+L"\"";}
std::wstring wide_ascii(const std::string& s){for(unsigned char c:s)if(c<0x20||c>0x7e)throw std::runtime_error("Target text is not printable ASCII");return std::wstring(s.begin(),s.end());}
std::string utf8(const std::wstring& s){if(s.empty())return {};int n=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,s.data(),static_cast<int>(s.size()),nullptr,0,nullptr,nullptr);if(!n)throw std::runtime_error("Invalid device instance text");std::string out(n,'\0');if(!WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,s.data(),static_cast<int>(s.size()),out.data(),n,nullptr,nullptr))throw std::runtime_error("Invalid device instance text");return out;}
bool spawn(const std::wstring& exe,std::wstring command,std::string& why){std::vector<wchar_t> args(command.begin(),command.end());args.push_back(0);STARTUPINFOW si{};si.cb=sizeof(si);PROCESS_INFORMATION pi{};
 if(!CreateProcessW(exe.c_str(),args.data(),nullptr,nullptr,FALSE,CREATE_NO_WINDOW,nullptr,nullptr,&si,&pi)){why="Could not launch recovery process";return false;}CloseHandle(pi.hThread);CloseHandle(pi.hProcess);return true;}
}
std::wstring device_recovery_directory(){DWORD n=GetEnvironmentVariableW(L"LOCALAPPDATA",nullptr,0);if(!n||n>32767)return {};std::vector<wchar_t> value(n);if(!GetEnvironmentVariableW(L"LOCALAPPDATA",value.data(),n))return {};const auto base=full(value.data());if(base.empty())return {};return base+L"\\Druta\\device-recovery";}
DeviceRecoveryReceipt read_device_recovery_receipt(const std::wstring& path,const std::wstring& base,const std::string& slot,const std::string& uuid){
 DeviceRecoveryRequest request;request.parent_pid=1;request.target={slot,L"receipt-target",L"receipt"};request.uuid=uuid;request.executable=L"C:\\receipt.exe";request.core=L"C:\\receipt.dll";request.receipt=path;std::string why;
 if(!valid_device_recovery_request(request,base,why))throw std::runtime_error(why);
 const auto doc=read_profile(path,65536);if(!doc.is_object()||!doc.contains("format")||doc["format"]!="druta-device-recovery"||!doc.contains("version")||(!doc["version"].is_number_integer()||doc["version"]!=1)||!doc.contains("slot")||doc["slot"]!=slot||!doc.contains("uuid")||doc["uuid"]!=uuid||
 !doc.contains("instance_id")||!doc["instance_id"].is_string()||!doc.contains("ok")||!doc["ok"].is_boolean()||!doc.contains("reboot_required")||!doc["reboot_required"].is_boolean()||!doc.contains("uncertain")||!doc["uncertain"].is_boolean()||!doc.contains("message")||!doc["message"].is_string())throw std::runtime_error("Recovery receipt is malformed or belongs to another GPU");
 const auto instance=doc["instance_id"].get<std::string>();if(instance.empty()||instance.size()>16384)throw std::runtime_error("Recovery receipt instance identity is invalid");
 int n=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,instance.data(),static_cast<int>(instance.size()),nullptr,0);if(!n)throw std::runtime_error("Recovery receipt instance identity is invalid");std::wstring wide(n,L'\0');MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,instance.data(),static_cast<int>(instance.size()),wide.data(),n);
 DeviceRecoveryReceipt receipt;receipt.slot=slot;receipt.uuid=uuid;receipt.instance_id=std::move(wide);receipt.ok=doc["ok"].get<bool>();receipt.reboot_required=doc["reboot_required"].get<bool>();receipt.uncertain=doc["uncertain"].get<bool>();receipt.message=doc["message"].get<std::string>();return receipt;
}
bool valid_device_recovery_request(const DeviceRecoveryRequest& r,const std::wstring& directory,std::string& why){
 try{if(!r.parent_pid||r.target.instance_id.empty()||r.target.instance_id.size()>4096||r.uuid.size()>512)throw std::runtime_error("Incomplete recovery target");wide_ascii(r.uuid);
  auto target=r.target;if(target.name.empty())target.name=L"selected device";resolve_restart_target(target.slot,{target});const auto exe=full(r.executable),core=full(r.core),base=full(directory),file=full(r.receipt);
  if(exe.empty()||core.empty()||base.empty()||file.empty())throw std::runtime_error("Recovery paths must be absolute");
  const auto separator=file.find_last_of(L"\\/");if(separator==std::wstring::npos)throw std::runtime_error("Invalid recovery receipt path");
  auto parent=file.substr(0,separator);while(parent.size()>3&&(parent.back()==L'\\'||parent.back()==L'/'))parent.pop_back();auto root=base;while(root.size()>3&&(root.back()==L'\\'||root.back()==L'/'))root.pop_back();
  const auto name=file.substr(separator+1);if(_wcsicmp(parent.c_str(),root.c_str())||name.size()<6||name.size()>128||_wcsicmp(name.c_str()+name.size()-5,L".json"))throw std::runtime_error("Receipt must be a direct JSON child of the recovery directory");
  const auto attrs=GetFileAttributesW(root.c_str());if(attrs==INVALID_FILE_ATTRIBUTES || !(attrs&FILE_ATTRIBUTE_DIRECTORY) || (attrs&FILE_ATTRIBUTE_REPARSE_POINT))throw std::runtime_error("Recovery directory unavailable or redirected");
  const auto file_attrs=GetFileAttributesW(file.c_str());if(file_attrs!=INVALID_FILE_ATTRIBUTES && (file_attrs&(FILE_ATTRIBUTE_DIRECTORY|FILE_ATTRIBUTE_REPARSE_POINT)))throw std::runtime_error("Recovery receipt is not a regular file");
  return true;
 }catch(const std::exception& e){why=e.what();return false;}}
DeviceRecoveryResult run_device_recovery(const DeviceRecoveryRequest& r,const DeviceRecoveryApi& api){DeviceRecoveryResult result;DeviceRecoveryReceipt receipt{r.target.slot,r.uuid,"Waiting for previous process",r.target.instance_id,false,false,false};
 try{if(!api.write_receipt||!api.wait_parent||!api.restart||!api.relaunch)throw std::runtime_error("Recovery callbacks unavailable");std::string why;
  if(!api.write_receipt(r,receipt,why))throw std::runtime_error("Cannot prepare recovery receipt: "+why);
  if(!api.wait_parent(r.parent_pid,45000,why)){receipt.message="Previous process did not exit; GPU was not restarted: "+why;api.write_receipt(r,receipt,why);result.message=receipt.message;return result;}
  result.parent_exited=true;
  try{const auto restart=api.restart(r.target);receipt.ok=restart.ok;receipt.reboot_required=restart.reboot_required;receipt.uncertain=restart.uncertain;receipt.message=restart.message;}
  catch(const std::exception& e){receipt.message=std::string("GPU restart failed: ")+e.what();receipt.uncertain=true;}
  catch(...){receipt.message="GPU restart failed";receipt.uncertain=true;}
  if(!api.write_receipt(r,receipt,why)){result.message="Could not save recovery result: "+why;return result;}
  if(!api.relaunch(r,why)){result.message="Could not reopen application: "+why;return result;}
  result.completed=receipt.ok&&!receipt.reboot_required;result.relaunched=true;result.message=receipt.message;return result;
 }catch(const std::exception& e){result.message=e.what();return result;}}
std::wstring create_device_recovery_receipt(const std::wstring& directory){auto base=full(directory);if(base.empty())throw std::runtime_error("Recovery directory must be absolute");
 const auto parent_at=base.find_last_of(L"\\/");if(parent_at!=std::wstring::npos){const auto parent=base.substr(0,parent_at);if(!CreateDirectoryW(parent.c_str(),nullptr)&&GetLastError()!=ERROR_ALREADY_EXISTS)throw std::runtime_error("Could not create application data directory");}
 if(!CreateDirectoryW(base.c_str(),nullptr)&&GetLastError()!=ERROR_ALREADY_EXISTS)throw std::runtime_error("Could not create recovery directory");
 const auto attrs=GetFileAttributesW(base.c_str());if(attrs==INVALID_FILE_ATTRIBUTES||!(attrs&FILE_ATTRIBUTE_DIRECTORY)||(attrs&FILE_ATTRIBUTE_REPARSE_POINT))throw std::runtime_error("Recovery directory is unavailable or redirected");
 for(unsigned i=0;i<100;++i){auto path=base+L"\\recovery-"+std::to_wstring(GetCurrentProcessId())+L"-"+std::to_wstring(GetTickCount64())+L"-"+std::to_wstring(i)+L".json";
  HANDLE h=CreateFileW(path.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);if(h!=INVALID_HANDLE_VALUE){CloseHandle(h);return path;}if(GetLastError()!=ERROR_FILE_EXISTS)break;}
 throw std::runtime_error("Could not reserve recovery receipt");}
std::wstring device_recovery_relaunch_command(const DeviceRecoveryRequest& req){const auto exe=full(req.executable),core=full(req.core),receipt=full(req.receipt);if(exe.empty()||core.empty()||receipt.empty())throw std::runtime_error("Invalid relaunch paths");
 std::wstring cmd=quote(exe)+L" --gpu "+quote(wide_ascii(req.target.slot));if(!req.uuid.empty())cmd+=L" --expect-uuid "+quote(wide_ascii(req.uuid));cmd+=L" --core "+quote(core)+L" --recovery-result "+quote(receipt);return cmd;}
bool launch_device_recovery_helper(const DeviceRecoveryRequest& r,std::string& why){try{const auto exe=full(r.executable),core=full(r.core),receipt=full(r.receipt);const auto base=device_recovery_directory();if(base.empty()||!valid_device_recovery_request(r,base,why))throw std::runtime_error(why.empty()?"Invalid recovery helper arguments":why);
 ProfileJson initial={{"format","druta-device-recovery"},{"version",1},{"slot",r.target.slot},{"uuid",r.uuid},{"instance_id",utf8(r.target.instance_id)},{"ok",false},{"reboot_required",false},{"uncertain",false},{"message","Waiting for previous process"}};if(initial.dump().size()>65536)throw std::runtime_error("Recovery receipt exceeds limit");write_profile(receipt,initial);
 const auto slot=wide_ascii(r.target.slot),uuid=wide_ascii(r.uuid);std::wstring cmd=quote(exe)+L" --device-recovery-helper --parent-pid "+std::to_wstring(r.parent_pid)+L" --gpu "+quote(slot)+L" --expected-instance "+quote(r.target.instance_id)+L" --core "+quote(core)+L" --recovery-result "+quote(receipt);
 if(!uuid.empty())cmd+=L" --expect-uuid "+quote(uuid);return spawn(exe,cmd,why);
 }catch(const std::exception& e){why=e.what();return false;}}
int device_recovery_helper(const DeviceRecoveryRequest& r,const std::wstring& directory){std::string why;wchar_t own_path[32768]{};const DWORD own_size=GetModuleFileNameW(nullptr,own_path,static_cast<DWORD>(std::size(own_path)));
 if(!own_size||own_size>=std::size(own_path)||_wcsicmp(full(r.executable).c_str(),full(std::wstring(own_path,own_size)).c_str()) || !valid_device_recovery_request(r,directory,why)||r.parent_pid==GetCurrentProcessId())return 1;
 DeviceRecoveryApi api;
 api.write_receipt=[](const DeviceRecoveryRequest& req,const DeviceRecoveryReceipt& value,std::string& error){try{ProfileJson doc={{"format","druta-device-recovery"},{"version",1},{"slot",value.slot},{"uuid",value.uuid},{"instance_id",utf8(value.instance_id)},{"ok",value.ok},{"reboot_required",value.reboot_required},{"uncertain",value.uncertain},{"message",value.message}};
  if(doc.dump().size()>65536)throw std::runtime_error("Recovery receipt exceeds limit");write_profile(req.receipt,doc);return true;}catch(const std::exception& e){error=e.what();return false;}};
 api.wait_parent=[](uint32_t pid,uint32_t timeout,std::string& error){HANDLE parent=OpenProcess(SYNCHRONIZE,FALSE,pid);if(!parent){if(GetLastError()==ERROR_INVALID_PARAMETER)return true;error="Cannot open previous process for synchronization";return false;}const auto status=WaitForSingleObject(parent,timeout);CloseHandle(parent);if(status==WAIT_OBJECT_0)return true;error=status==WAIT_TIMEOUT?"Previous process is still running":"Previous process wait failed";return false;};
 api.restart=[&](const DeviceRestartTarget& target){DeviceRestartApi restart;restart.enumerate=enumerate_restart_targets;restart.started=restart_target_started;restart.launch_child=[&](const auto& t,uint32_t timeout){return launch_device_restart_child(r.executable,t,timeout);};return restart_device(target,restart);};
 api.relaunch=[](const DeviceRecoveryRequest& req,std::string& error){try{const auto exe=full(req.executable);return spawn(exe,device_recovery_relaunch_command(req),error);}catch(const std::exception& e){error=e.what();return false;}};
 const auto result=run_device_recovery(r,api);return result.completed?0:1;}
}
