// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weight_host.h"
#include "core_client.h"
#include "clock_recovery_boot_bcd.h"
#include "clock_recovery_logon.h"
#include "device_restart.h"
#include "profile_io.h"
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <algorithm>
#include <cwchar>
#include <regex>
#include <stdexcept>
namespace druta::ui {
namespace {
std::wstring full(const std::wstring& p){if(p.size()<3||p[1]!=L':'||(p[2]!=L'\\'&&p[2]!=L'/')||p.find(L'"')!=std::wstring::npos)return {};DWORD n=GetFullPathNameW(p.c_str(),0,nullptr,nullptr);if(!n||n>32767)return {};std::wstring out(n,L'\0');const auto k=GetFullPathNameW(p.c_str(),n,out.data(),nullptr);if(!k||k>=n)return {};out.resize(k);return out;}
std::wstring own(){std::wstring b(32768,L'\0');const auto n=GetModuleFileNameW(nullptr,b.data(),static_cast<DWORD>(b.size()));if(!n||n>=b.size())throw std::runtime_error("Cannot resolve executable");b.resize(n);return full(b);}
std::wstring quote(const std::wstring& s){std::wstring out=L"\"";size_t slashes=0;for(auto c:s){if(c==L'\\'){++slashes;continue;}if(c==L'"'){out.append(2*slashes+1,L'\\');out+=c;}else{out.append(slashes,L'\\');out+=c;}slashes=0;}out.append(2*slashes,L'\\');return out+L"\"";}
std::wstring wide(const std::string& s){for(unsigned char c:s)if(c<32||c>126)throw std::runtime_error("Invalid helper text");return {s.begin(),s.end()};}
std::string narrow(const wchar_t* s){std::string out;for(;*s;++s){if(*s<32||*s>126)throw std::runtime_error("Invalid helper argument");out.push_back(static_cast<char>(*s));}return out;}
std::wstring operation(PowerWeightOperation op){switch(op){case PowerWeightOperation::apply:return L"apply";case PowerWeightOperation::remove:return L"remove";case PowerWeightOperation::verify:return L"verify";}throw std::runtime_error("Unknown operation");}
PowerWeightOperation parse_op(const wchar_t* s){if(!wcscmp(s,L"apply"))return PowerWeightOperation::apply;if(!wcscmp(s,L"remove"))return PowerWeightOperation::remove;if(!wcscmp(s,L"verify"))return PowerWeightOperation::verify;throw std::runtime_error("Unknown helper operation");}
bool spawn(const std::wstring& exe,std::wstring cmd,PROCESS_INFORMATION& pi){std::vector<wchar_t> args(cmd.begin(),cmd.end());args.push_back(0);STARTUPINFOW si{};si.cb=sizeof(si);return CreateProcessW(exe.c_str(),args.data(),nullptr,nullptr,FALSE,CREATE_NO_WINDOW,nullptr,nullptr,&si,&pi)!=0;}
bool wait_parent(DWORD pid){if(!pid||pid==GetCurrentProcessId())return false;HANDLE h=OpenProcess(SYNCHRONIZE,FALSE,pid);if(!h)return GetLastError()==ERROR_INVALID_PARAMETER;const auto result=WaitForSingleObject(h,45000);CloseHandle(h);return result==WAIT_OBJECT_0;}
bool target_same(const PowerWeightReceipt& r,const PowerWeightCapture& c){return c.identity.slot==r.identity.slot&&c.identity.architecture==r.identity.architecture&&c.identity.device_id==r.identity.device_id&&c.identity.subsystem_id==r.identity.subsystem_id&&std::string(c.adapter.uuid)==r.uuid;}
PowerWeightReceipt active(const std::string& id){const auto dir=power_weight_receipt_directory();if(dir.empty())throw std::runtime_error("Weight receipt directory unavailable");auto r=read_weight_receipt(dir,id);auto found=discover_weight_receipt(dir,r.identity.slot,r.uuid);if(!found||found->id!=id)throw std::runtime_error("Weight receipt is not active");return r;}
std::wstring result_path(const std::string& id){const auto directory=full(power_weight_receipt_directory());if(directory.empty())throw std::runtime_error("Weight receipt directory unavailable");for(unsigned i=0;i<128;++i){auto path=directory+L"\\verify-"+wide(id)+L"-"+std::to_wstring(GetCurrentProcessId())+L"-"+std::to_wstring(GetTickCount64())+L"-"+std::to_wstring(i)+L".json";HANDLE h=CreateFileW(path.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);if(h!=INVALID_HANDLE_VALUE){CloseHandle(h);return path;}if(GetLastError()!=ERROR_FILE_EXISTS)break;}throw std::runtime_error("Cannot reserve verification result");}
bool valid_result(const std::wstring& path,const std::string& id){const auto root=full(power_weight_receipt_directory()),p=full(path);if(root.empty()||p.empty())return false;const auto at=p.find_last_of(L"\\/");return at!=std::wstring::npos&&_wcsicmp(root.c_str(),p.substr(0,at).c_str())==0&&p.substr(at+1).rfind(L"verify-"+wide(id)+L"-",0)==0&&p.size()>5&&_wcsicmp(p.c_str()+p.size()-5,L".json")==0;}
PowerWeightBoundaryResult verify_child(const std::wstring& exe,const std::wstring& core,const PowerWeightReceipt& receipt,bool original,bool staged=false){PowerWeightBoundaryResult result;std::wstring path;try{path=result_path(receipt.id);PROCESS_INFORMATION pi{};const auto cmd=quote(exe)+L" --weight-verify-child "+wide(receipt.id)+(original?L" original ":staged?L" staged ":L" expected ")+quote(path)+L" "+quote(core);if(!spawn(exe,cmd,pi))throw std::runtime_error("Cannot launch fresh verification process");CloseHandle(pi.hThread);const auto status=WaitForSingleObject(pi.hProcess,30000);if(status==WAIT_TIMEOUT){TerminateProcess(pi.hProcess,1);const auto stopped=WaitForSingleObject(pi.hProcess,5000);CloseHandle(pi.hProcess);result=power_weight_child_timeout(stopped==WAIT_OBJECT_0);throw std::runtime_error("Fresh verification timed out");}DWORD exit_code=1;const bool exited=status==WAIT_OBJECT_0&&GetExitCodeProcess(pi.hProcess,&exit_code);CloseHandle(pi.hProcess);if(!exited||exit_code)throw std::runtime_error("Fresh verification process failed");const auto doc=read_profile(path,65536);if(!doc.is_object()||doc.value("format",std::string())!="druta-weight-verify"||doc.value("id",std::string())!=receipt.id||!doc.contains("original")||!doc["original"].is_boolean()||doc["original"].get<bool>()!=original||doc.value("staged",false)!=staged||!doc.contains("ok")||!doc["ok"].is_boolean()||!doc.contains("message")||!doc["message"].is_string())throw std::runtime_error("Fresh verification result malformed");result.ok=doc["ok"].get<bool>();result.message=doc["message"].get<std::string>();result.retryable=staged&&doc.value("retryable",false);result.image_matches_timing_pending=!staged&&!result.ok&&doc.value("image_matches_timing_pending",false);}catch(const std::exception& e){result.message=e.what();}if(!path.empty())DeleteFileW(path.c_str());return result;}
int verify_main(const std::string& id,bool original,bool staged,const std::wstring& result,const std::wstring& core) {
 if(!valid_result(result,id)||full(core).empty())return 1;
 bool ok=false,retryable=false,image_matches_timing_pending=false;std::string message;
 try {
  const auto receipt=active(id);
  if(staged&&(!receipt.is_pascal_clocks()||receipt.phase!="clock_initializing"))
   throw std::runtime_error("A staged clock proof requires the current initializing clock receipt");
  // Once the image loads, release the temporary registry records immediately.
  // Controller confirmation then runs with the records cleared, at the same P0 clock.
  const auto timing=staged?std::optional<PascalTimingSample>{}:receipt_timing_sample(receipt,
                      original?PascalTimingPhase::original:PascalTimingPhase::applied);
  const auto capture=capture_power_weights_from_core(full(core),receipt.identity.slot,receipt.uuid,true,
                                                     receipt.is_pascal_clocks(),timing.has_value());
  if(!target_same(receipt,capture))throw std::runtime_error("Fresh verification adapter mismatch");
  if(!capture.windows||capture.windows->slot!=receipt.windows.slot||
     capture.windows->instance_id!=receipt.windows.instance_id||capture.windows->driver_key!=receipt.windows.driver_key||
     capture.registry_availability!=VoltagePolicyAvailability::available||!capture.registry||
     !power_weight_verify_registry(receipt,*capture.registry,original,staged))
   throw std::runtime_error("Fresh verification Windows target or registry differs");
  const auto derived=derive_weight_receipt(receipt);
  if(receipt.is_pascal_clocks()) {
   ok=capture.image_availability==VoltagePolicyAvailability::available&&
      capture.image==(original?receipt.image:derived.clock_plan.image);
   retryable=staged&&!ok&&(capture.image_availability==VoltagePolicyAvailability::transient||
                 (capture.image_availability==VoltagePolicyAvailability::available&&
                  (capture.image==receipt.image||
                   (!derived.clock_source_image.empty()&&capture.image==derived.clock_source_image))));
   message=ok?"Complete running clock image verified":staged?"Driver has not loaded the expected staged clock image":"Complete running clock image differs";
   if(ok&&timing) {
    ok=false;
    try {verify_pascal_timing_sample(capture,*timing);}
    catch(const VoltageEquationEvidenceError& e) {
     image_matches_timing_pending=e.kind==VoltagePolicyAvailability::transient||
        e.kind==VoltagePolicyAvailability::unknown||e.kind==VoltagePolicyAvailability::unsupported;
     throw;
    }
    ok=true;message="Complete firmware image and reviewed P0 controller timing words verified";
   }
  } else {
   const auto checked=verify_power_relationship_weights(derived.plan,capture.identity,capture.layout,capture.info,&capture.image,original);
   ok=checked.ok;message=checked.message;
  }
 } catch(const VoltageEquationEvidenceError& error) {
  retryable=staged&&error.kind==VoltagePolicyAvailability::transient;
  message=error.what();
 } catch(const std::exception& error) { message=error.what(); }
 try {
  write_profile(result,ProfileJson{{"format","druta-weight-verify"},{"id",id},{"original",original},
                                   {"staged",staged},{"retryable",retryable},{"image_matches_timing_pending",image_matches_timing_pending},{"ok",ok},{"message",message}});
  return 0;
 } catch(...) { return 1; }
}
PowerWeightRegistryRecovery recover_registry(const PowerWeightReceipt& receipt){
 const auto devices=enumerate_restart_targets();const std::wstring instance(receipt.windows.instance_id.begin(),receipt.windows.instance_id.end());unsigned matches=0;for(const auto& device:devices)if(device.slot==receipt.identity.slot&&_wcsicmp(device.instance_id.c_str(),instance.c_str())==0)++matches;if(matches!=1)throw std::runtime_error("Saved PCI instance not uniquely present");
 HKEY key=nullptr;const auto path=L"SYSTEM\\CurrentControlSet\\Enum\\"+instance;if(RegOpenKeyExW(HKEY_LOCAL_MACHINE,path.c_str(),0,KEY_READ|KEY_WOW64_64KEY,&key)!=ERROR_SUCCESS)throw std::runtime_error("Cannot inspect saved PCI instance");wchar_t value[1024]{};DWORD type=0,size=sizeof(value);const auto status=RegQueryValueExW(key,L"Driver",nullptr,&type,reinterpret_cast<BYTE*>(value),&size);RegCloseKey(key);if(status!=ERROR_SUCCESS||type!=REG_SZ||size<sizeof(wchar_t)||size>sizeof(value)||value[size/sizeof(wchar_t)-1]!=0)throw std::runtime_error("Saved PCI driver binding unavailable");const std::wstring full_key=L"SYSTEM\\CurrentControlSet\\Control\\Class\\"+std::wstring(value);const std::wstring expected(receipt.windows.driver_key.begin(),receipt.windows.driver_key.end());if(_wcsicmp(full_key.c_str(),expected.c_str()))throw std::runtime_error("Saved PCI driver binding changed");PowerWeightRegistryRecovery out;out.windows=receipt.windows;out.registry=read_power_weight_registry(out.windows);return out;
}
// A wedged PnP stack can also block SetupAPI enumeration. Recover through
// the saved Enum instance and its exact display-class binding without PnP calls.
std::wstring enum_string(HKEY key, const wchar_t* name) {
 wchar_t value[1024]{};
 DWORD type=0, size=sizeof(value);
 const auto status=RegQueryValueExW(key,name,nullptr,&type,reinterpret_cast<BYTE*>(value),&size);
 if (status!=ERROR_SUCCESS || type!=REG_SZ || size<sizeof(wchar_t) || size>sizeof(value) ||
     size%sizeof(wchar_t) || value[size/sizeof(wchar_t)-1] || wcslen(value)+1!=size/sizeof(wchar_t))
  throw std::runtime_error("Saved PCI instance has no valid registry binding");
 return value;
}
void verify_clock_recovery_binding(const PowerWeightReceipt& r,bool writing,
                                   std::wstring* bound_service=nullptr) {
 if (!r.is_pascal_clocks() || !std::regex_match(r.windows.instance_id,
     std::regex(R"(^PCI\\VEN_10DE&DEV_[0-9A-F]{4}&SUBSYS_[0-9A-F]{8}(&[0-9A-Z_]+)*\\[0-9A-Z_&.-]+$)",std::regex::icase)))
  throw std::runtime_error("Saved clock recovery PCI instance is invalid");
 const auto instance=L"SYSTEM\\CurrentControlSet\\Enum\\"+wide(r.windows.instance_id);
 HKEY key=nullptr;
 if(RegOpenKeyExW(HKEY_LOCAL_MACHINE,instance.c_str(),0,KEY_QUERY_VALUE|KEY_WOW64_64KEY,&key)!=ERROR_SUCCESS)
  throw std::runtime_error("Saved PCI instance is unavailable. Clock recovery made no request to another GPU.");
 struct CloseKey {HKEY key;~CloseKey(){RegCloseKey(key);}} close{key};
 const auto path=L"SYSTEM\\CurrentControlSet\\Control\\Class\\"+enum_string(key,L"Driver");
 if(_wcsicmp(path.c_str(),wide(r.windows.driver_key).c_str()))
  throw std::runtime_error("Saved PCI instance maps to a different driver key");
 if(bound_service || writing) {
  const auto service=enum_string(key,L"Service");
  if(bound_service && bound_service->empty() && !writing) *bound_service=service;
  if(writing && bound_service && !bound_service->empty()) {
   if(_wcsicmp(service.c_str(),bound_service->c_str()))throw std::runtime_error("Driver service changed after capture");
  } else if(writing) {
   HKEY cls=nullptr;
   if(RegOpenKeyExW(HKEY_LOCAL_MACHINE,path.c_str(),0,KEY_QUERY_VALUE|KEY_WOW64_64KEY,&cls)!=ERROR_SUCCESS)
    throw std::runtime_error("Cannot verify the driver provider for clock recovery");
   CloseKey close_class{cls};
   const auto provider=enum_string(cls,L"ProviderName");
   if(!is_clock_rescue_driver(service,provider))
    throw std::runtime_error("Selected PCI device no longer uses an identifiable NVIDIA driver. Recovery preserved its registry.");
  }
 }
}
PowerWeightRegistryRecovery read_clock_recovery_binding(const PowerWeightReceipt& r) {
 verify_clock_recovery_binding(r,false);
 return {r.windows,read_power_weight_registry(r.windows)};
}
struct HelperLock {
 HANDLE handle=INVALID_HANDLE_VALUE;
 ~HelperLock(){if(handle!=INVALID_HANDLE_VALUE)CloseHandle(handle);}
};
HANDLE try_operation_lock(const std::string& id,const std::wstring& dir={}) {
 const auto path=power_weight_operation_lock_path(dir,id);
 return CreateFileW(path.c_str(),GENERIC_READ|GENERIC_WRITE,0,nullptr,OPEN_ALWAYS,
                    FILE_ATTRIBUTE_NORMAL|FILE_FLAG_DELETE_ON_CLOSE,nullptr);
}
PowerWeightRegistryWriteResult write_clock_records(const PowerWeightReceipt& r,
  const VoltageScaffoldWindowsTarget& target,const PowerWeightRegistrySnapshot& expected,
  const PowerWeightRegistrySnapshot& desired,const std::function<bool(const std::wstring&,std::string&)>& journal,
  std::wstring* bound_service=nullptr) {
 const auto checked_journal=[&](const std::wstring& name,std::string& error) {
  verify_clock_recovery_binding(r,true,bound_service);
  return journal(name,error);
 };
 auto written=write_power_weight_registry(target,expected,desired,checked_journal);
 if (written.ok && written.may_have_changed && desired.size()<expected.size()) {
  HKEY key=nullptr;
  const auto path=wide(target.driver_key);
  if(RegOpenKeyExW(HKEY_LOCAL_MACHINE,path.c_str(),0,KEY_QUERY_VALUE|KEY_WOW64_64KEY,&key)!=ERROR_SUCCESS)
   return {false,true,"Clock records restored but the driver key could not be opened for flushing"};
  const auto status=RegFlushKey(key);
  RegCloseKey(key);
  if(status!=ERROR_SUCCESS)return {false,true,"Clock records restored but Windows could not flush the registry"};
 }
 return written;
}
void acquire_clock_lock(HelperLock& lock,const std::string& id) {
 lock.handle=try_operation_lock(id);
 if(lock.handle==INVALID_HANDLE_VALUE)throw std::runtime_error("The clock receipt is busy or inaccessible");
}
// The caller owns the operation lock until cleanup, verification and optional boot disarm complete.
PowerWeightTransactionResult clean_clock_receipt_locked(const std::string& id,const PowerWeightReceipt* pinned=nullptr) {
 auto receipt=active(id);
 if(!receipt.is_pascal_clocks())throw std::runtime_error("The active receipt does not describe a clock override");
 if(pinned && !clock_logon_receipts_match(*pinned,receipt))
  throw std::runtime_error("The saved clock plan differs from the protected recovery copy");
 PowerWeightTransactionApi api;
 api.recovery_registry=[](const auto& r){return read_clock_recovery_binding(r);};
 api.write=[&](const auto& target,const auto& expected,const auto& desired,const auto& journal) {
  return write_clock_records(receipt,target,expected,desired,journal);
 };
 const auto dir=power_weight_receipt_directory();
 api.persist=[&](const auto& r,bool){save_weight_receipt(dir,r);};
 auto result=recover_staged_clocks(api,receipt);
 if(!result.ok) {
  receipt.message="Clock registry recovery pending. "+result.message;
  receipt.uncertain=true;
  api.persist(receipt,false);
 }
 return result;
}

bool ratio_change(const PowerWeightReceipt& receipt) {
 if(receipt.identity.architecture!=4 || receipt.phase!="prepared" || receipt.uncertain ||
    receipt.refresh_requested || !receipt.issued_names.empty() || receipt.restoring) return false;
 const auto current_ratio=[](const auto& edits, PascalClockDomain domain, int baseline) {
  for(const auto& entry:edits) if(entry.pstate==-1 && entry.domain==domain)return entry.value;
  return baseline;
 };
 // A revision can omit a ratio when it returns to the original clock table.
 for(const auto* edits:{&receipt.clocks,&receipt.previous_clocks}) {
  for(const auto& edit:*edits) {
   if(edit.pstate!=-1 || (edit.domain!=PascalClockDomain::xbar && edit.domain!=PascalClockDomain::sys))continue;
   if(current_ratio(receipt.clocks,edit.domain,edit.expected)!=
      current_ratio(receipt.previous_clocks,edit.domain,edit.expected))return true;
  }
 }
 return false;
}

void disarm_trial_boot(const PowerWeightReceipt& receipt) {
 // Remove the boot selection before unregistering its repair program.
 disarm_clock_recovery_boot(receipt.id);
 std::string error;
 if(!disarm_clock_logon_recovery(receipt,error))throw std::runtime_error(error);
}

void install_trial_boot(const PowerWeightReceipt& receipt) {
 if(!ratio_change(receipt))throw std::runtime_error("Select and review a Pascal XBAR or SYS ratio change first.");
 check_clock_recovery_boot_available(receipt.id);
 std::string error;
 if(!arm_clock_logon_recovery(receipt,error))throw std::runtime_error(error);
 try {
  arm_clock_recovery_boot(receipt.id);
  if(!clock_logon_recovery_armed(receipt) || !clock_recovery_boot_is_armed(receipt.id))
   throw std::runtime_error("The one-time recovery setup did not verify.");
 } catch(const std::exception& setup) {
  try {disarm_trial_boot(receipt);}
  catch(const std::exception& cleanup) {
   throw std::runtime_error(std::string(setup.what())+" Recovery setup cleanup: "+cleanup.what());
  }
  throw;
 }
}

bool clean_and_flush_clock_plan(const PowerWeightReceipt& receipt) {
 const auto registry=read_clock_recovery_binding(receipt);
 if(!power_weight_verify_registry(receipt,registry.registry,false,false))return false;
 const auto key_path=wide(receipt.windows.driver_key);
 HKEY key=nullptr;
 if(RegOpenKeyExW(HKEY_LOCAL_MACHINE,key_path.c_str(),0,KEY_QUERY_VALUE|KEY_WOW64_64KEY,&key)!=ERROR_SUCCESS)return false;
 const auto rc=RegFlushKey(key);
 RegCloseKey(key);
 return rc==ERROR_SUCCESS;
}

void retire_clean_boot_locked(const PowerWeightReceipt& receipt) {
 if(!clean_and_flush_clock_plan(receipt))
  throw std::runtime_error("Staged clock records are still present or cannot be verified. Keep recovery available.");
 disarm_trial_boot(receipt);
}

PowerWeightTransactionResult recover_clock(const PowerWeightReceipt& requested,bool from_logon) {
 HelperLock lock;acquire_clock_lock(lock,requested.id);
 // Include the explicit manual retry in the same protected plan binding as automatic logon.
 const auto pins=clock_logon_recovery_enrollments();
 const auto match=std::find_if(pins.begin(),pins.end(),[&](const auto& pin){return pin.id==requested.id;});
 const bool enrolled=match!=pins.end();
 if(from_logon&&(!enrolled||!clock_logon_receipts_match(*match,requested)))
  throw std::runtime_error("Clock logon recovery changed before cleanup; no settings were changed");
 const auto& receipt=enrolled?*match:requested;
 const auto current=discover_weight_receipt({},receipt.identity.slot,receipt.uuid);
 if(!current||current->id!=receipt.id) {
  // A reset to stock can retire the active pointer before its supervisor removes the boot choice.
  if(!enrolled)throw std::runtime_error("This clock recovery receipt is no longer active");
  retire_clean_boot_locked(receipt);
  PowerWeightTransactionResult result;
  result.ok=result.no_change=true;
  result.message="The clock records were already clear. Restart Windows normally.";
  return result;
 }
 auto result=clean_clock_receipt_locked(receipt.id,enrolled?&receipt:nullptr);
 if(result.ok&&enrolled) {
  retire_clean_boot_locked(receipt);
  if(unused_clock_preparation(*current))abandon_prepared_clock_receipt({},*current);
  result.message="Staged clock overrides are clear. Restart Windows normally.";
 }
 return result;
}

std::vector<PowerWeightReceipt> recovery_candidates(const std::string& slot={}) {
 auto records=active_clock_receipts({},slot);
 for(auto& pin:clock_logon_recovery_enrollments()) {
  const auto duplicate=std::find_if(records.begin(),records.end(),[&](const auto& r){return r.id==pin.id;});
  if(!slot.empty()&&slot!=pin.identity.slot) {
   if(duplicate!=records.end())records.erase(duplicate);
  } else if(duplicate!=records.end()) {
   if(!clock_logon_receipts_match(pin,*duplicate))*duplicate=std::move(pin);
  } else records.push_back(std::move(pin));
 }
 return records;
}

int manage_trial_boot() {
 constexpr auto title=L"Druta recovery boot setup";
 try {
  if(!clock_recovery_boot_supported())throw std::runtime_error("On XP x64 use manual Safe Mode clock recovery.");
  const auto receipts=recovery_candidates();
  bool offered=false;
  for(const auto& receipt:receipts) {
   const bool ready=ratio_change(receipt);
   if(!ready && !clock_logon_recovery_enrolled(receipt))continue;
   offered=true;
   const auto identity=L"GPU "+wide(receipt.identity.slot)+L"\nReceipt "+wide(receipt.id)+L"\n\n";
   const bool armed=clock_logon_recovery_enrolled(receipt);
   const auto prompt=identity+(armed ?
    L"Remove this recovery setup? Druta checks that the staging records are clear before removing its one-time boot. An unused clock edit will also be canceled." :
    L"Prepare a one-time Windows Safe Mode boot for this clock edit? Druta copies the current Windows loader and registers a recovery command for this account. This may trigger a BitLocker recovery-key prompt. Have the recovery key available if this drive is encrypted.\n\nMake sure you know this administrator account's password. Windows Hello PIN and biometrics are unavailable in Safe Mode. After a freeze, sign in to this account and approve cleanup before restarting.");
   const int choice=MessageBoxW(nullptr,prompt.c_str(),title,MB_YESNOCANCEL|MB_ICONWARNING|MB_DEFBUTTON2);
   if(choice==IDCANCEL)return 2;
   if(choice!=IDYES)continue;
   HelperLock lock;
   lock.handle=try_operation_lock(receipt.id);
   if(lock.handle==INVALID_HANDLE_VALUE)throw std::runtime_error("This clock operation is busy. Try again after the current operation finishes.");
   if(armed) {
    retire_clean_boot_locked(receipt);
    const auto current=discover_weight_receipt({},receipt.identity.slot,receipt.uuid);
    if(current&&current->id==receipt.id&&unused_clock_preparation(*current))abandon_prepared_clock_receipt({},*current);
   } else install_trial_boot(active(receipt.id));
   MessageBoxW(nullptr,armed?L"Recovery boot setup removed.":L"Recovery boot is armed. Return to the Druta warning to continue the reviewed clock edit.",title,MB_OK|MB_ICONINFORMATION);
   return 0;
  }
  if(!offered)MessageBoxW(nullptr,L"Review a Pascal XBAR or SYS ratio edit in Druta, select recovery boot, and choose Apply first. Leave the warning open while running this script.",title,MB_OK|MB_ICONINFORMATION);
  return offered?2:1;
 } catch(const std::exception& e) {
  MessageBoxW(nullptr,wide(e.what()).c_str(),title,MB_OK|MB_ICONERROR);
  return 1;
 }
}

void check_requested_boot(const PowerWeightReceipt& receipt) {
 if(!ratio_change(receipt))throw std::runtime_error("Recovery boot requires a prepared Pascal ratio change.");
 const auto application=own();
 const auto script=application.substr(0,application.find_last_of(L"\\/")+1)+L"Druta-Recovery-Boot.cmd";
 if(GetFileAttributesW(script.c_str())==INVALID_FILE_ATTRIBUTES)
  throw std::runtime_error("The recovery setup script is missing. Extract the complete Druta package.");
 const auto message=L"GPU "+wide(receipt.identity.slot)+L"\nReceipt "+wide(receipt.id)+L"\n\nReview and run this script as administrator under this Windows account:\n"+script+
     L"\n\nChoose OK after the script confirms recovery boot is armed. Cancel to leave the clocks as they are. Your normal Windows entry stays the default.";
 for(;;) {
  if(MessageBoxW(nullptr,message.c_str(),L"Druta clock recovery before Apply",MB_OKCANCEL|MB_ICONWARNING|MB_DEFBUTTON2)!=IDOK) {
   throw std::runtime_error("Clock Apply canceled before staging.");
  }
  if(clock_logon_recovery_armed(receipt) && clock_recovery_boot_is_armed(receipt.id))return;
  if(MessageBoxW(nullptr,L"Recovery boot is not armed for this edit. Retry after running the script, or cancel this Apply.",
                 L"Druta clock recovery",MB_RETRYCANCEL|MB_ICONWARNING)!=IDRETRY)
   throw std::runtime_error("Clock Apply canceled before staging.");
 }
}
int manual_clock_recovery(const std::string& selector) {
 try {
  if(selector.empty()){
   const auto records=recovery_candidates();
   std::wstring message;
   for(const auto& r:records)
    message+=wide(r.identity.slot)+L"  "+wide(r.phase)+L"  "+wide(r.id)+L"\n";
   if(message.empty())message=L"No active clock receipts for this Windows account.";
   else message+=L"\nRun druta-ui.exe --recover-clock-overrides PCI_SLOT as administrator under the original Windows account. For multiple records at one slot, use the receipt ID.";
   MessageBoxW(nullptr,message.c_str(),L"Druta clock recovery",MB_OK|MB_ICONINFORMATION);
   return 0;
  }
  std::string id=selector;
  if(selector.size()!=32){
   const auto records=recovery_candidates(selector);
   if(records.size()!=1)throw std::runtime_error(records.empty()?"No active clock receipt for this slot and Windows account":"More than one active clock receipt matches this slot. Use the specific receipt ID from the recovery list.");
   id=records.front().id;
  }
  const auto records=recovery_candidates();
  const auto entry=std::find_if(records.begin(),records.end(),[&](const auto& item){return item.id==id;});
  if(entry==records.end())throw std::runtime_error("Clock recovery receipt is unavailable for this Windows account");
  const auto result=recover_clock(*entry,false);
  MessageBoxW(nullptr,wide(result.message).c_str(),L"Druta clock recovery",MB_OK|(result.ok?MB_ICONINFORMATION:MB_ICONERROR));
  return result.ok?0:1;
 } catch(const std::exception& e) {
  MessageBoxW(nullptr,wide(e.what()).c_str(),L"Druta clock recovery",MB_OK|MB_ICONERROR);
  return 1;
 }
}
bool reopen_result(const std::wstring& core,const PowerWeightReceipt& r,const PowerWeightHostRequest& request) {
 PROCESS_INFORMATION pi{};
 const auto exe=own();
 auto cmd=quote(exe)+L" --gpu "+quote(wide(r.identity.slot))+L" --expect-uuid "+quote(wide(r.uuid))+
          L" --core "+quote(core)+L" --weight-result "+wide(r.id);
 cmd += request.daily && r.is_pascal_clocks() ? L" --page daily" : L" --page advanced";
 cmd += request.classic ? L" --classic" : L" --focused";
 if(!spawn(exe,cmd,pi))return false;
 CloseHandle(pi.hThread);CloseHandle(pi.hProcess);return true;
}
int helper(const PowerWeightHostRequest& request,bool supervised=false){bool parent_exited=supervised;HelperLock operation_lock;try{if(_wcsicmp(full(request.executable).c_str(),own().c_str())!=0||full(request.core).empty()||(!supervised&&!power_weight_host_gate(request.parent_pid,wait_parent,[&](){parent_exited=true;})))return 1;const auto dir=power_weight_receipt_directory();
 operation_lock.handle=try_operation_lock(request.receipt_id);
 if(operation_lock.handle==INVALID_HANDLE_VALUE) {
  // A second launcher can race the probe before either parent closes.
  // Give that user the read-only result without touching the owner's journal.
  parent_exited=false;
  if(!supervised)reopen_result(full(request.core),read_weight_receipt(dir,request.receipt_id),request);
  return 1;
 }
 auto receipt=active(request.receipt_id);
 if(request.arm_safe_mode && (!supervised || !ratio_change(receipt) ||
    !clock_logon_recovery_armed(receipt) || !clock_recovery_boot_is_armed(receipt.id)))
  throw std::runtime_error("Requested Safe Mode recovery is no longer armed. Clock staging was canceled.");
 dc_gpu_identity opened{};try{const auto initial=capture_power_weights_from_core(full(request.core),receipt.identity.slot,receipt.uuid,true,receipt.is_pascal_clocks());if(!target_same(receipt,initial))throw std::runtime_error("Weight helper adapter mismatch");opened=initial.adapter;}catch(const VoltageEquationEvidenceError& e){if(request.operation!=PowerWeightOperation::remove||(e.kind!=VoltagePolicyAvailability::transient&&e.kind!=VoltagePolicyAvailability::unsupported))throw;}
 std::wstring live_service;
 if(receipt.is_pascal_clocks() && opened.size==sizeof(opened))verify_clock_recovery_binding(receipt,false,&live_service);
 PowerWeightTransactionApi api;api.capture=[&](dc_gpu*,bool registry){return capture_power_weights_from_core(full(request.core),receipt.identity.slot,receipt.uuid,registry,receipt.is_pascal_clocks(),receipt_timing_sample(receipt,PascalTimingPhase::previous).has_value());};api.persist=[&](const auto& r,bool create){if(create){const auto existing=active(r.id);auto normalized=r;normalized.phase=existing.phase;normalized.message=existing.message;normalized.issued_names=existing.issued_names;normalized.refresh_requested=existing.refresh_requested;normalized.uncertain=existing.uncertain;if(encode_weight_receipt(normalized)!=encode_weight_receipt(existing))throw std::runtime_error("Prepared weight receipt differs");}save_weight_receipt(dir,r);};api.clear_active=[&](const auto& r){return clear_active_weight_receipt(dir,r);};api.write=[&](const auto& t,const auto& before,const auto& after,const auto& journal){if(receipt.is_pascal_clocks())return write_clock_records(receipt,t,before,after,journal,&live_service);return write_power_weight_registry(t,before,after,journal);};api.restart=[&](const auto& t){DeviceRestartTarget wanted{t.slot,std::wstring(t.instance_id.begin(),t.instance_id.end()),L"selected GPU"};DeviceRestartApi a;a.enumerate=enumerate_restart_targets;a.started=restart_target_started;a.launch_child=[&](const auto& device,uint32_t timeout){return receipt.is_pascal_clocks()?launch_clock_restart_child(own(),device,timeout):launch_device_restart_child(own(),device,timeout);};const auto v=restart_device(wanted,a);return power_weight_restart_result(v);};api.recovery_registry=[](const auto& r){return r.is_pascal_clocks()?read_clock_recovery_binding(r):recover_registry(r);};api.fresh_verify=[&](const auto& r,bool original){return verify_child(own(),full(request.core),r,original);};api.fresh_verify_staged=[&](const auto& r){return verify_child(own(),full(request.core),r,false,true);};api.wait_clock_ready=[](){Sleep(400);};PowerWeightTransaction transaction(api,reinterpret_cast<dc_gpu*>(1),opened);const auto result=transaction.run(request.operation,receipt,request.operation==PowerWeightOperation::verify&&receipt.restoring);
 if(supervised&&!result.ok) {
  auto journal=active(receipt.id);
  if(unused_clock_preparation(journal)&&journal.message!=result.message) {
   journal.message=result.message;
   save_weight_receipt(dir,journal);
  }
 }
 if(result.ok&&request.operation!=PowerWeightOperation::apply&&receipt.is_pascal_clocks()&&clock_logon_recovery_enrolled(receipt))
  retire_clean_boot_locked(receipt);
 if(!supervised)reopen_result(full(request.core),receipt,request);return result.ok?0:1;
 }catch(const std::exception& error){
  if(parent_exited)try {
   const auto dir=power_weight_receipt_directory();
   auto prior=read_weight_receipt(dir,request.receipt_id);
   auto current=discover_weight_receipt(dir,prior.identity.slot,prior.uuid);
   if(current&&current->id==prior.id) {
    current->message=error.what();
    try{save_weight_receipt(dir,*current);}catch(...){}
   }
   // Another window may have advanced the session while this helper waited
   // for its parent to exit. Reopen on the current record in that case.
   if(!supervised)reopen_result(full(request.core),current?*current:prior,request);
  }catch(...){}
  return 1;
 }}
// This process owns the worker HANDLE. The worker alone can stage records.
// Its verification/restart children do not write registry values.
constexpr DWORD clock_ticket=0x434C4F4B;
bool read_clock_ticket() {
 const HANDLE input=GetStdHandle(STD_INPUT_HANDLE);
 DWORD ticket=0,received=0;
 return input && GetFileType(input)==FILE_TYPE_PIPE &&
        ReadFile(input,&ticket,sizeof(ticket),&received,nullptr) && received==sizeof(ticket) && ticket==clock_ticket;
}
bool spawn_clock_worker(const std::wstring& exe,std::wstring command,PROCESS_INFORMATION& worker,bool& stopped) {
 stopped=true;
 SECURITY_ATTRIBUTES inheritable{sizeof(SECURITY_ATTRIBUTES),nullptr,TRUE};
 HANDLE input=nullptr,output=nullptr;
 if(!CreatePipe(&input,&output,&inheritable,0))return false;
 struct Pipes {HANDLE read,write;~Pipes(){CloseHandle(read);CloseHandle(write);}} pipes{input,output};
 if(!SetHandleInformation(output,HANDLE_FLAG_INHERIT,0))return false;
 STARTUPINFOW info{};info.cb=sizeof(info);info.dwFlags=STARTF_USESTDHANDLES;info.hStdInput=input;
 if(!CreateProcessW(exe.c_str(),command.data(),nullptr,nullptr,TRUE,CREATE_NO_WINDOW,nullptr,nullptr,&info,&worker))return false;
 stopped=false;
 DWORD sent=0;
 if(WriteFile(output,&clock_ticket,sizeof(clock_ticket),&sent,nullptr)&&sent==sizeof(clock_ticket))return true;
 TerminateProcess(worker.hProcess,1);stopped=WaitForSingleObject(worker.hProcess,5000)==WAIT_OBJECT_0;
 CloseHandle(worker.hThread);CloseHandle(worker.hProcess);return false;
}
int supervise_clock_apply(const PowerWeightHostRequest& request) {
 if (!wait_parent(request.parent_pid)) {
  cancel_unused_clock_preparation(active(request.receipt_id));
  return 1;
 }
 const auto receipt=active(request.receipt_id);
 if (!receipt.is_pascal_clocks() || request.operation!=PowerWeightOperation::apply) return 1;
 const auto exe=own();
 auto command=quote(exe)+L" --clock-apply-worker "+wide(receipt.id)+L" apply "+
              std::to_wstring(GetCurrentProcessId())+L" "+quote(request.core);
 if(request.arm_safe_mode)command+=L" --arm-safe-mode";
 const auto completion=run_clock_worker(exe,std::move(command),90000);
 // Never clean underneath a surviving writer. A full machine failure can
 // interrupt both processes; the same cleanup entry point works in Safe Mode.
 if(completion.stopped && (!completion.success||request.arm_safe_mode)) {
  try {
   with_clock_operation_lock(power_weight_receipt_directory(),receipt.id,[&] {
    if(!completion.success) {
     const auto current=discover_weight_receipt({},receipt.identity.slot,receipt.uuid);
     if(current&&current->id==receipt.id) {
      if(unused_clock_preparation(*current)) {
       if(request.arm_safe_mode||clock_logon_recovery_enrolled(*current))retire_clean_boot_locked(*current);
       abandon_prepared_clock_receipt({},*current);
       return;
      }
      const auto result=clean_clock_receipt_locked(receipt.id);
      if(!result.ok)throw std::runtime_error(result.message);
      // A failed trial that issued an effect keeps its recovery enrollment until explicit verification or cleanup.
      return;
     }
    }
    if(request.arm_safe_mode)retire_clean_boot_locked(receipt);
   });
  } catch(const std::exception& e) {
   const auto message=std::string("Clock recovery could not finish. ")+e.what();
   MessageBoxA(nullptr,message.c_str(),"Druta clock recovery",MB_OK|MB_ICONWARNING);
  }
 }
 reopen_result(request.core,receipt,request);
 return completion.success?0:1;
}

}
void cancel_unused_clock_preparation(const PowerWeightReceipt& receipt,const std::wstring& directory) {
 if(!receipt.is_pascal_clocks())return;
 with_clock_operation_lock(directory,receipt.id,[&] {
  const auto current=discover_weight_receipt(directory,receipt.identity.slot,receipt.uuid);
  if(!current||current->id!=receipt.id) {
   if(clock_logon_recovery_enrolled(receipt))retire_clean_boot_locked(receipt);
   return;
  }
  if(!unused_clock_preparation(*current))throw std::runtime_error("The clock trial has already started or changed");
  if(clock_logon_recovery_enrolled(*current))retire_clean_boot_locked(*current);
  abandon_prepared_clock_receipt(directory,*current);
 });
}
void with_clock_operation_lock(const std::wstring& dir,const std::string& id,const std::function<void()>& action) {
 HelperLock lock;
 lock.handle=try_operation_lock(id,dir);
 if(lock.handle==INVALID_HANDLE_VALUE)throw std::runtime_error("The clock receipt is busy or inaccessible");
 action();
}
bool clock_worker_admitted() { return read_clock_ticket(); }
ClockWorkerCompletion run_clock_worker(const std::wstring& exe,std::wstring command,uint32_t timeout_ms) {
 if(!timeout_ms)return {};
 PROCESS_INFORMATION worker{};
 bool stopped=false;
 if(!spawn_clock_worker(exe,std::move(command),worker,stopped))return {stopped,false};
 CloseHandle(worker.hThread);
 struct CloseProcess {HANDLE h;~CloseProcess(){CloseHandle(h);}} close{worker.hProcess};
 const auto process=worker.hProcess;
 const DWORD wait=WaitForSingleObject(process,timeout_ms);
 if(wait==WAIT_OBJECT_0) {
  DWORD code=1;
  return {true,GetExitCodeProcess(process,&code)!=0 && code==0};
 }
 // Only the supervisor-created worker HANDLE is passed here. Failure to
 // confirm exit leaves its exclusive operation lock authoritative.
 TerminateProcess(process,1);
 return {WaitForSingleObject(process,5000)==WAIT_OBJECT_0,false};
}
bool is_clock_rescue_driver(const std::wstring& service,const std::wstring& provider) {
 // Old receipts have no service name. Confirm the supplier of the current
 // display-class key before mutating it. A DDU-rebound BasicDisplay can reuse
 // the same four-digit key, even with the same PCI instance.
 return !service.empty() && _wcsicmp(service.c_str(),L"BasicDisplay") &&
        (!_wcsicmp(provider.c_str(),L"NVIDIA") || !_wcsicmp(provider.c_str(),L"NVIDIA Corporation"));
}
PowerWeightBoundaryResult power_weight_child_timeout(bool exit_confirmed){PowerWeightBoundaryResult out;out.timed_out=true;out.uncertain=true;out.cleanup_pending=!exit_confirmed;out.message="Fresh verification timed out";return out;}
PowerWeightBoundaryResult power_weight_restart_result(const DeviceRestartResult& value){PowerWeightBoundaryResult out;out.ok=value.ok;out.reboot_required=value.reboot_required;out.uncertain=value.uncertain;out.message=value.message;return out;}
bool power_weight_host_gate(uint32_t pid,const std::function<bool(uint32_t)>& wait,const std::function<void()>& admitted){if(!pid||!wait||!admitted||!wait(pid))return false;admitted();return true;}
bool power_weight_verify_result(const ProfileJson& doc,const std::string& id,bool original){return doc.is_object()&&doc.value("format",std::string())=="druta-weight-verify"&&doc.value("id",std::string())==id&&doc.contains("original")&&doc["original"].is_boolean()&&doc["original"].get<bool>()==original&&doc.contains("ok")&&doc["ok"].is_boolean()&&doc["ok"].get<bool>();}
bool launch_power_weight_host(const PowerWeightHostRequest& request, std::string& error) {
    std::optional<PowerWeightReceipt> preview;
    try {
        const auto exe = full(request.executable), core = full(request.core);
        if (exe.empty() || _wcsicmp(exe.c_str(), own().c_str()) || core.empty() ||
            !request.parent_pid || request.parent_pid != GetCurrentProcessId())
            throw std::runtime_error("Invalid weight helper target or paths");
        const auto receipt=active(request.receipt_id);
        if(request.operation==PowerWeightOperation::apply&&unused_clock_preparation(receipt))preview=receipt;
        if(request.arm_safe_mode) {
            if(request.operation!=PowerWeightOperation::apply)throw std::runtime_error("Recovery boot applies only to clock edits.");
            check_requested_boot(receipt);
        }
        {
            HelperLock probe;
            probe.handle = try_operation_lock(request.receipt_id);
            if (probe.handle == INVALID_HANDLE_VALUE)
                throw std::runtime_error("A helper is already using this recovery record, or its lock cannot be opened");
        }
        PROCESS_INFORMATION pi{};
        auto command = quote(exe) + L" --weight-helper " + wide(request.receipt_id) + L" " +
            operation(request.operation) + L" " + std::to_wstring(request.parent_pid) + L" " + quote(core);
        if (request.classic) command += L" --classic";
        if (request.daily) command += L" --daily";
        if (request.arm_safe_mode) command += L" --arm-safe-mode";
        if (!spawn(exe, command, pi)) throw std::runtime_error("Cannot start weight helper");
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        return true;
    } catch (const std::exception& e) {
        error = e.what();
        if(preview)try{cancel_unused_clock_preparation(*preview);}
        catch(const std::exception& cleanup){error+=". Check the prepared clock recovery: ";error+=cleanup.what();}
        return false;
    }
}

bool dispatch_power_weight_host(int argc, wchar_t** argv, int& exit_code) {
    if (argc < 2) return false;
    if(!wcscmp(argv[1],L"--manage-clock-recovery-boot")) {
        exit_code=argc==2?manage_trial_boot():2;
        return true;
    }
    if(!wcscmp(argv[1],L"--clock-recovery-logon")) {
        exit_code=2;
        if(argc==3)try {
            exit_code=run_clock_logon_recovery(narrow(argv[2]),[](const auto& pinned) {return recover_clock(pinned,true);});
        }catch(const std::exception&){exit_code=1;}
        return true;
    }
    if (!wcscmp(argv[1],L"--recover-clock-overrides")) {
        exit_code=2;
        if(argc==2)exit_code=manual_clock_recovery({});
        if(argc==3){
            try{
                auto slot=narrow(argv[2]);
                if(std::regex_match(slot,std::regex(R"(^(0000:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]|[0-9a-f]{32})$)",std::regex::icase))){
                    for(auto& c:slot)if(c>='A'&&c<='F')c=static_cast<char>(c+('a'-'A'));
                    exit_code=manual_clock_recovery(slot);
                }
            }catch(const std::exception&){exit_code=1;}
        }
        return true;
    }
    const bool supervised_worker = !wcscmp(argv[1],L"--clock-apply-worker");
    const bool run_helper = supervised_worker || !wcscmp(argv[1], L"--weight-helper");
    if (!run_helper && wcscmp(argv[1], L"--weight-verify-child")) return false;
    exit_code = 1;
    try {
        if (argc < 6 || argc > 9 || (!run_helper && argc != 6)) return true;
        bool classic = false, daily = false, arm_safe_mode = false;
        for (int i = 6; i < argc; ++i) {
            if (!classic && !wcscmp(argv[i], L"--classic")) classic = true;
            else if (!daily && !wcscmp(argv[i], L"--daily")) daily = true;
            else if (!arm_safe_mode && !wcscmp(argv[3], L"apply") &&
                     !wcscmp(argv[i], L"--arm-safe-mode")) arm_safe_mode = true;
            else return true;
        }
        const auto id = narrow(argv[2]);
        if (!std::regex_match(id, std::regex("^[0-9a-f]{32}$"))) return true;
        if (run_helper) {
            wchar_t* end = nullptr;
            const auto pid = wcstoul(argv[4], &end, 10);
            if (!end || *end || !pid || pid > 0xfffffffful) return true;
            PowerWeightHostRequest request{id, parse_op(argv[3]), static_cast<uint32_t>(pid), own(), argv[5], classic, daily, arm_safe_mode};
            if (request.operation==PowerWeightOperation::apply && active(id).is_pascal_clocks()) {
                if (!supervised_worker) exit_code=supervise_clock_apply(request);
                else if(clock_worker_admitted())exit_code=helper(request,true);
            } else if (!supervised_worker && !arm_safe_mode) exit_code=helper(request);
        } else {
            const bool original = !wcscmp(argv[3], L"original");
            const bool staged = !wcscmp(argv[3], L"staged");
            if (!original && !staged && wcscmp(argv[3], L"expected")) return true;
            exit_code = verify_main(id, original, staged, argv[4], argv[5]);
        }
    } catch (...) {
        exit_code = 1;
    }
    return true;
}
}
