// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "device_restart.h"
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <setupapi.h>
#include <cfgmgr32.h>
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <cwctype>
#include <regex>
#include <stdexcept>
namespace druta::ui {
namespace {
struct Slot {unsigned bus,device,function;std::string canonical;};
Slot parse(const std::string& slot){std::smatch m;static const std::regex re("^([0-9a-fA-F]{4}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\\.([0-7])$");
 if(!std::regex_match(slot,m,re)||std::stoul(m[1],nullptr,16)!=0)throw std::runtime_error("Invalid PCI slot or unsupported nonzero segment");
 Slot s{static_cast<unsigned>(std::stoul(m[2],nullptr,16)),static_cast<unsigned>(std::stoul(m[3],nullptr,16)),static_cast<unsigned>(std::stoul(m[4])),{}};
 if(s.device>31)throw std::runtime_error("Invalid PCI device number");char text[16];std::snprintf(text,sizeof(text),"0000:%02x:%02x.%u",s.bus,s.device,s.function);s.canonical=text;return s;}
bool same_instance(const std::wstring& a,const std::wstring& b){return !a.empty()&&!b.empty()&&_wcsicmp(a.c_str(),b.c_str())==0;}
struct Module {HMODULE h=nullptr;explicit Module(const wchar_t* n):h(LoadLibraryW(n)){if(!h)throw std::runtime_error("Required Windows PnP library unavailable");}~Module(){if(h)FreeLibrary(h);}template<class T>T get(const char* name){auto p=GetProcAddress(h,name);if(!p)throw std::runtime_error("Required Windows PnP export unavailable");return reinterpret_cast<T>(p);}};
struct Setup {
 Module dll{L"setupapi.dll"};
 decltype(&SetupDiGetClassDevsW) get_class=dll.get<decltype(get_class)>("SetupDiGetClassDevsW");
 decltype(&SetupDiEnumDeviceInfo) enumerate=dll.get<decltype(enumerate)>("SetupDiEnumDeviceInfo");
 decltype(&SetupDiGetDeviceRegistryPropertyW) property=dll.get<decltype(property)>("SetupDiGetDeviceRegistryPropertyW");
 decltype(&SetupDiGetDeviceInstanceIdW) instance=dll.get<decltype(instance)>("SetupDiGetDeviceInstanceIdW");
 decltype(&SetupDiDestroyDeviceInfoList) destroy=dll.get<decltype(destroy)>("SetupDiDestroyDeviceInfoList");
 decltype(&SetupDiSetClassInstallParamsW) set_params=dll.get<decltype(set_params)>("SetupDiSetClassInstallParamsW");
 decltype(&SetupDiCallClassInstaller) call=dll.get<decltype(call)>("SetupDiCallClassInstaller");
 decltype(&SetupDiGetDeviceInstallParamsW) get_params=dll.get<decltype(get_params)>("SetupDiGetDeviceInstallParamsW");
};
const GUID display_guid={0x4d36e968,0xe325,0x11ce,{0xbf,0xc1,0x08,0x00,0x2b,0xe1,0x03,0x18}};
struct List {Setup& a;HDEVINFO h;explicit List(Setup& s):a(s),h(a.get_class(&display_guid,nullptr,nullptr,DIGCF_PRESENT)){if(h==INVALID_HANDLE_VALUE)throw std::runtime_error("Cannot enumerate present display devices");}~List(){a.destroy(h);}};
std::vector<BYTE> property(Setup& a,HDEVINFO h,SP_DEVINFO_DATA& d,DWORD prop,DWORD expected){DWORD type=0,needed=0;a.property(h,&d,prop,&type,nullptr,0,&needed);
 if(GetLastError()!=ERROR_INSUFFICIENT_BUFFER||!needed||needed>65536)throw std::runtime_error("Required device property unavailable");std::vector<BYTE> data(needed);
 if(!a.property(h,&d,prop,&type,data.data(),needed,&needed)||type!=expected||needed>data.size())throw std::runtime_error("Device property type or length invalid");data.resize(needed);return data;}
std::wstring string_property(Setup& a,HDEVINFO h,SP_DEVINFO_DATA& d,DWORD prop){auto b=property(a,h,d,prop,REG_SZ);if(b.size()<sizeof(wchar_t)||b.size()%sizeof(wchar_t))throw std::runtime_error("Malformed device string");
 auto p=reinterpret_cast<const wchar_t*>(b.data());size_t n=b.size()/sizeof(wchar_t);if(p[n-1]!=0)throw std::runtime_error("Unterminated device string");size_t length=0;while(length<n && p[length])++length;return std::wstring(p,length);}
DWORD number(Setup& a,HDEVINFO h,SP_DEVINFO_DATA& d,DWORD prop){auto b=property(a,h,d,prop,REG_DWORD);if(b.size()!=4)throw std::runtime_error("Malformed PCI address property");DWORD v;std::memcpy(&v,b.data(),4);return v;}
std::wstring instance(Setup& a,HDEVINFO h,SP_DEVINFO_DATA& d){DWORD n=0;a.instance(h,&d,nullptr,0,&n);if(GetLastError()!=ERROR_INSUFFICIENT_BUFFER||n<2||n>32768)throw std::runtime_error("Device instance unavailable");std::vector<wchar_t> b(n);if(!a.instance(h,&d,b.data(),n,&n)||b.back()!=0)throw std::runtime_error("Device instance unreadable");return b.data();}
bool nvidia_ids(const std::vector<BYTE>& b){if(b.size()<2*sizeof(wchar_t)||b.size()%sizeof(wchar_t))throw std::runtime_error("Malformed hardware ID list");
 auto p=reinterpret_cast<const wchar_t*>(b.data());size_t n=b.size()/sizeof(wchar_t);if(p[n-1]||p[n-2])throw std::runtime_error("Unterminated hardware ID list");
 static const std::wregex re(L"^PCI\\\\VEN_10DE&DEV_[0-9A-F]{4}(&[A-Z0-9_]+)*$",std::regex_constants::icase);
 for(size_t i=0;i<n&&p[i];){std::wstring s(p+i);if(std::regex_match(s,re))return true;i+=s.size()+1;}return false;}
struct Candidate {DeviceRestartTarget target;SP_DEVINFO_DATA data;};
std::vector<Candidate> candidates(Setup& a,List& list){std::vector<Candidate> out;
 for(DWORD i=0;;++i){SP_DEVINFO_DATA d{};d.cbSize=sizeof(d);if(!a.enumerate(list.h,i,&d)){if(GetLastError()==ERROR_NO_MORE_ITEMS)break;throw std::runtime_error("Display enumeration failed");}
  const auto enumerator=string_property(a,list.h,d,SPDRP_ENUMERATOR_NAME);if(_wcsicmp(enumerator.c_str(),L"PCI"))continue;
  std::vector<BYTE> ids;try{ids=property(a,list.h,d,SPDRP_HARDWAREID,REG_MULTI_SZ);}catch(...){throw std::runtime_error("PCI display hardware identity unavailable");}if(!nvidia_ids(ids))continue;
  auto bus=number(a,list.h,d,SPDRP_BUSNUMBER),address=number(a,list.h,d,SPDRP_ADDRESS);unsigned dev=address>>16,fun=address&0xffff;
  if(bus>255||dev>31||fun>7)throw std::runtime_error("NVIDIA PCI address is not understood");char slot[16];std::snprintf(slot,sizeof(slot),"0000:%02x:%02x.%u",unsigned(bus),dev,fun);
  Candidate c;c.data=d;c.target.slot=slot;c.target.instance_id=instance(a,list.h,d);try{c.target.name=string_property(a,list.h,d,SPDRP_FRIENDLYNAME);}catch(...){c.target.name=string_property(a,list.h,d,SPDRP_DEVICEDESC);}if(c.target.name.empty()||c.target.instance_id.empty())throw std::runtime_error("Incomplete NVIDIA display identity");out.push_back(std::move(c));
 }return out;}
std::wstring quote(const std::wstring& s){std::wstring out=L"\"";size_t slash=0;for(wchar_t c:s){if(c==L'\\'){++slash;continue;}if(c==L'"'){out.append(slash*2+1,L'\\');out+=c;}else{out.append(slash,L'\\');out+=c;}slash=0;}out.append(slash*2,L'\\');return out+L"\"";}
}
DeviceRestartTarget resolve_restart_target(const std::string& requested,const std::vector<DeviceRestartTarget>& rows){const auto slot=parse(requested);const DeviceRestartTarget* found=nullptr;
 for(const auto& row:rows){if(parse(row.slot).canonical!=slot.canonical)continue;if(row.instance_id.empty()||row.name.empty())throw std::runtime_error("Incomplete selected display identity");if(found)throw std::runtime_error("Multiple NVIDIA display devices claim selected slot");found=&row;}
 if(!found)throw std::runtime_error("No NVIDIA display device at selected slot");return *found;}
std::vector<DeviceRestartTarget> enumerate_restart_targets(){Setup api;List list(api);auto c=candidates(api,list);std::vector<DeviceRestartTarget> out;for(auto& v:c)out.push_back(std::move(v.target));return out;}
DeviceRestartResult restart_device(const DeviceRestartTarget& target,const DeviceRestartApi& api,uint32_t timeout_ms){DeviceRestartResult r;bool dispatched=false;
 try{if(!api.enumerate||!api.launch_child||!api.started||!timeout_ms||timeout_ms>120000)throw std::runtime_error("Restart callbacks or timeout unavailable");
  auto before=resolve_restart_target(target.slot,api.enumerate());if(!same_instance(before.instance_id,target.instance_id))throw std::runtime_error("Selected device changed before restart");
  dispatched=true;r=api.launch_child(before,timeout_ms);if(!r.ok)return r;
  auto after=resolve_restart_target(target.slot,api.enumerate());if(!same_instance(after.instance_id,target.instance_id)){r.ok=false;r.uncertain=true;r.message="Different GPU appeared after restart";return r;}
  std::string why;if(!api.started(after,why)){r.ok=false;r.uncertain=true;r.message=why.empty()?"Restarted device is not confirmed started":why;return r;}
  r.message="Selected GPU restarted and is started without a reported problem";return r;
 }catch(const std::exception& e){r.ok=false;r.uncertain=dispatched;r.message=e.what();return r;}}
int device_restart_child(const std::string& slot,const std::wstring& expected){try{
 BOOL wow=FALSE;auto kernel=GetModuleHandleW(L"kernel32.dll");using Wow=BOOL(WINAPI*)(HANDLE,PBOOL);auto query=reinterpret_cast<Wow>(GetProcAddress(kernel,"IsWow64Process"));if(query&&(!query(GetCurrentProcess(),&wow)||wow))return 1;
 Setup a;List list(a);auto all=candidates(a,list);std::vector<DeviceRestartTarget> targets;for(const auto& c:all)targets.push_back(c.target);const auto selected=resolve_restart_target(slot,targets);if(!same_instance(selected.instance_id,expected))return 1;
 auto it=std::find_if(all.begin(),all.end(),[&](const Candidate& c){return same_instance(c.target.instance_id,selected.instance_id);});if(it==all.end())return 1;
 SP_PROPCHANGE_PARAMS params{};params.ClassInstallHeader.cbSize=sizeof(SP_CLASSINSTALL_HEADER);params.ClassInstallHeader.InstallFunction=DIF_PROPERTYCHANGE;params.StateChange=DICS_PROPCHANGE;params.Scope=DICS_FLAG_CONFIGSPECIFIC;
 if(!a.set_params(list.h,&it->data,&params.ClassInstallHeader,sizeof(params))||!a.call(DIF_PROPERTYCHANGE,list.h,&it->data))return 1;
 SP_DEVINSTALL_PARAMS_W result{};result.cbSize=sizeof(result);if(!a.get_params(list.h,&it->data,&result))return 1;return (result.Flags&(DI_NEEDREBOOT|DI_NEEDRESTART))?3010:0;
 }catch(...){return 1;}}
DeviceRestartResult launch_device_restart_child(const std::wstring& exe,const DeviceRestartTarget& target,uint32_t timeout){DeviceRestartResult r;
 const bool absolute=exe.size()>=3 && ((exe[1]==L':' && (exe[2]==L'\\'||exe[2]==L'/')) || (exe[0]==L'\\' && exe[1]==L'\\'));
 if(!absolute||exe.find(L'"')!=std::wstring::npos||target.instance_id.empty()||!timeout||timeout>120000){r.message="Invalid restart helper arguments";return r;}
 std::wstring command=quote(exe)+L" --device-restart-child "+quote(std::wstring(target.slot.begin(),target.slot.end()))+L" "+quote(target.instance_id);std::vector<wchar_t> args(command.begin(),command.end());args.push_back(0);
 STARTUPINFOW si{};si.cb=sizeof(si);PROCESS_INFORMATION process{};if(!CreateProcessW(exe.c_str(),args.data(),nullptr,nullptr,FALSE,CREATE_NO_WINDOW,nullptr,nullptr,&si,&process)){r.message="Could not launch restart helper";return r;}
 const auto wait=WaitForSingleObject(process.hProcess,timeout);if(wait!=WAIT_OBJECT_0){TerminateProcess(process.hProcess,1);WaitForSingleObject(process.hProcess,5000);r.uncertain=true;r.message="Restart helper timed out or wait failed; device state is uncertain";}
 else{DWORD code=1;GetExitCodeProcess(process.hProcess,&code);r.ok=code==0;r.reboot_required=code==3010;r.uncertain=code!=0&&code!=3010;r.message=r.ok?"Restart helper completed":r.reboot_required?"Windows requires reboot to complete restart":"Restart helper failed; device state may have changed";}
 CloseHandle(process.hThread);CloseHandle(process.hProcess);return r;}
bool restart_target_started(const DeviceRestartTarget& target,std::string& reason){try{Module cfg(L"cfgmgr32.dll");auto locate=cfg.get<decltype(&CM_Locate_DevNodeW)>("CM_Locate_DevNodeW");auto status=cfg.get<decltype(&CM_Get_DevNode_Status)>("CM_Get_DevNode_Status");DEVINST node=0;ULONG flags=0,problem=0;
 auto name=target.instance_id;if(locate(&node,name.data(),0)!=CR_SUCCESS||status(&flags,&problem,node,0)!=CR_SUCCESS){reason="Windows device status unavailable";return false;}if(!(flags&DN_STARTED)||problem){reason="Device not started or reports a problem";return false;}return true;
 }catch(const std::exception& e){reason=e.what();return false;}}
}
