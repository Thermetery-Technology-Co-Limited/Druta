// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_recovery_logon.h"
#define main weight_service_fixture_main
#include "power_weights_service_test.cpp"
#undef main
#include "pascal_clock_fixture.h"
#include <windows.h>
#include <sddl.h>
#include <filesystem>
#include <fstream>

namespace {
void check(bool ok,const char* why){if(!ok)throw std::runtime_error(why);}
PowerWeightReceipt fixture() {
 WeightFake fake; auto& c=fake.current;
 c.adapter.nvapi_devid=c.adapter.nvml_devid=c.target.device_id=c.identity.device_id=0x1bbc;
 c.target.architecture=c.identity.architecture=4;
 c.windows->instance_id="PCI\\VEN_10DE&DEV_1BBC&SUBSYS_12345678\\TEST";
 c.image=test::pascal_clock_fixture();
 c.registry=PowerWeightRegistrySnapshot{{L"EnableGpuFirmware",4,{0x11,0,0,0}}};
 return make_pascal_clock_receipt(c,{{PascalClockDomain::xbar,-1,95,105}});
}
struct Hive {
 HKEY root=nullptr,machine=nullptr,user=nullptr;
 std::filesystem::path dir;
 ~Hive(){
  if(machine){RegOverridePredefKey(HKEY_LOCAL_MACHINE,nullptr);RegCloseKey(machine);}
  if(user){RegOverridePredefKey(HKEY_CURRENT_USER,nullptr);RegCloseKey(user);}
  if(root)RegCloseKey(root);
  std::error_code ec;if(!dir.empty())std::filesystem::remove_all(dir,ec);
 }
 void setup(){
  wchar_t system[MAX_PATH]{},tmp[MAX_PATH]{};
  check(GetSystemDirectoryW(system,MAX_PATH)>0,"Cannot get system directory");
  // Use a private child on a trusted system volume so production ACL checks
  // also run. Its root stays owned by Administrators with a protected DACL.
  auto drive=std::filesystem::path(system).root_path();
  check(GetTempFileNameW(drive.c_str(),L"DRC",0,tmp)!=0,"Cannot reserve scratch path");
  check(DeleteFileW(tmp)!=FALSE,"Cannot prepare scratch path");
  PSECURITY_DESCRIPTOR sd=nullptr;
  check(ConvertStringSecurityDescriptorToSecurityDescriptorW(L"O:BAG:BAD:P(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)",
      SDDL_REVISION_1,&sd,nullptr)!=FALSE,"Cannot create fixture ACL");
  SECURITY_ATTRIBUTES sa{sizeof(sa),sd,FALSE};
  const bool created=CreateDirectoryW(tmp,&sa)!=FALSE;LocalFree(sd);
  check(created,"Cannot create protected scratch directory");dir=tmp;
  const auto exe=std::filesystem::path(system)/L"bcdedit.exe",file=dir/L"private.dat";
  auto command=L"\""+exe.wstring()+L"\" /createstore \""+file.wstring()+L"\"";
  PROCESS_INFORMATION pi{};STARTUPINFOW si{};si.cb=sizeof(si);
  check(CreateProcessW(exe.c_str(),command.data(),nullptr,nullptr,FALSE,CREATE_NO_WINDOW,nullptr,nullptr,&si,&pi)!=0,"Cannot make private hive");
  CloseHandle(pi.hThread);DWORD code=1;
  const auto wait=WaitForSingleObject(pi.hProcess,15000);GetExitCodeProcess(pi.hProcess,&code);CloseHandle(pi.hProcess);
  check(wait==WAIT_OBJECT_0&&code==0,"Private fixture hive creation failed");
  using Load=LSTATUS(WINAPI*)(LPCWSTR,PHKEY,REGSAM,DWORD,DWORD);
  const auto load=reinterpret_cast<Load>(GetProcAddress(GetModuleHandleW(L"advapi32.dll"),"RegLoadAppKeyW"));
  check(load&&load(file.c_str(),&root,KEY_ALL_ACCESS,0,0)==ERROR_SUCCESS,"Cannot mount private test hive");
  check(RegCreateKeyExW(root,L"Machine",0,nullptr,0,KEY_ALL_ACCESS,nullptr,&machine,nullptr)==ERROR_SUCCESS&&
        RegCreateKeyExW(root,L"User",0,nullptr,0,KEY_ALL_ACCESS,nullptr,&user,nullptr)==ERROR_SUCCESS,"Cannot initialize private registry roots");
  HKEY key=nullptr;check(RegCreateKeyExW(machine,L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion",0,nullptr,0,
                 KEY_ALL_ACCESS,nullptr,&key,nullptr)==ERROR_SUCCESS,"Cannot create private installer key");
  const auto path=dir.wstring();
  const auto ok=RegSetValueExW(key,L"ProgramFilesDir",0,REG_SZ,reinterpret_cast<const BYTE*>(path.c_str()),
                               static_cast<DWORD>((path.size()+1)*sizeof(wchar_t)))==ERROR_SUCCESS;
  RegCloseKey(key);check(ok,"Cannot initialize private Program Files");
  // Both production predefined keys are redirected before any API under test.
  check(RegOverridePredefKey(HKEY_LOCAL_MACHINE,machine)==ERROR_SUCCESS&&
        RegOverridePredefKey(HKEY_CURRENT_USER,user)==ERROR_SUCCESS,"Cannot isolate fixture registry");
 }
 void consume(const std::string& id,bool foreign=false){
  HKEY key=nullptr;
  check(RegOpenKeyExW(user,L"Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce",0,KEY_SET_VALUE,&key)==ERROR_SUCCESS,
        "Private RunOnce missing");
  const std::wstring value=L"*DrutaClockRecovery-"+std::wstring(id.begin(),id.end());
  const wchar_t text[]=L"foreign";
  const auto status=foreign?RegSetValueExW(key,value.c_str(),0,REG_SZ,reinterpret_cast<const BYTE*>(text),sizeof(text)):
                            RegDeleteValueW(key,value.c_str());
  RegCloseKey(key);check(status==ERROR_SUCCESS,"Cannot change private fixture command");
 }
};
void exercise_sign_in(const PowerWeightReceipt& receipt,const std::filesystem::path& helper) {
 clock_logon_test::SignIn session;
 auto image=helper.wstring();
 session.executable=[&] {return image;};
 const std::wstring id(receipt.id.begin(),receipt.id.end());
 std::wstring message;
 int approvals=0,recoveries=0,choice=IDYES;
 bool approve=false,recover_ok=false;
 session.prompt=[&](const std::wstring& text,unsigned){message=text;return choice;};
 session.elevate=[&](const std::wstring& file,const std::wstring& args){
  ++approvals;
  check(file==helper.wstring()&&args==L"--clock-recovery-logon "+id,"Unexpected elevation target");
  return approve;
 };
 const auto recover=[&](const PowerWeightReceipt& pin){
  ++recoveries;
  check(clock_logon_receipts_match(pin,receipt),"Unexpected cleanup scope");
  PowerWeightTransactionResult result;result.ok=recover_ok;result.message=recover_ok?"Done":"Binding changed";
  return result;
 };
 const auto retry=L"\""+helper.wstring()+L"\" --recover-clock-overrides "+id;
 const auto run=[&]{message.clear();return clock_logon_test::run(session,receipt.id,recover);};
 check(run()!=0&&message.find(retry)!=message.npos&&message.find(L"normally")!=message.npos&&
       approvals==0&&recoveries==0,"Normal sign-in attempted automatic cleanup or hid manual recovery");
 session.safe_mode=true;choice=IDNO;
 check(run()!=0&&approvals==0&&recoveries==0&&message.find(retry)!=message.npos,"Declining prompt hid retry or attempted elevation");
 choice=IDYES;
 check(run()!=0&&approvals==1&&recoveries==0&&message.find(retry)!=message.npos&&
       message.find(L"administrator approval")!=message.npos,"Refused UAC lost the manual retry");
 approve=true;
 check(run()==0&&approvals==2&&recoveries==0,"Shell approval executed cleanup in the unelevated parent");
 session.elevated=true;image+=L".untrusted";
 check(run()!=0&&approvals==2&&recoveries==0&&message.find(L"druta-ui.exe --recover-clock-overrides "+id)!=message.npos,
       "Unprotected binary ran a privileged callback or retried itself");
 image=helper.wstring();
 check(run()!=0&&recoveries==1&&message.find(L"Binding changed")!=message.npos&&message.find(retry)!=message.npos,
       "Failed cleanup omitted its cause or manual retry");
 recover_ok=true;
 check(run()==0&&recoveries==2&&message==L"Done","Verified Safe Mode sign-in failed to dispatch the pinned receipt");
}

}
int main(){
 try {
  const auto r=fixture();
  auto advanced=r;advanced.phase="writing";advanced.message="in flight";advanced.uncertain=true;advanced.refresh_requested=true;
  const auto plan=derive_weight_receipt(r);advanced.issued_names.push_back(plan.after.back().name);
  check(clock_logon_receipts_match(r,advanced),"Progress should preserve enrollment");
  auto bogus=advanced;bogus.issued_names={L"vbios"};
  check(!clock_logon_receipts_match(r,bogus),"Unowned registry name accepted");
  auto different=r;different.uuid+="elsewhere";
  check(!clock_logon_receipts_match(r,different),"Foreign receipt identity accepted");
  auto changed=r;changed.clocks.front().value+=1;
  check(!clock_logon_receipts_match(r,changed),"Changed override accepted");
  BYTE sid[SECURITY_MAX_SID_SIZE];DWORD bytes=sizeof(sid);BOOL elevated=FALSE;
  if(!CreateWellKnownSid(WinBuiltinAdministratorsSid,nullptr,sid,&bytes)||!CheckTokenMembership(nullptr,sid,&elevated)||!elevated){
   std::puts("Immutable receipt checks passed; isolated Windows installation fixture needs an elevated test process.");return 77;
  }
  if(!GetProcAddress(GetModuleHandleW(L"advapi32.dll"),"RegLoadAppKeyW")){
   std::puts("Immutable receipt checks passed; this Windows version lacks the private test hive API.");return 77;
  }
  clock_logon_test::check_system_directory_permissions(); // read-only, host installation ancestors
  Hive store;store.setup();
  std::string error;
  check(clock_logon_recovery_enrollments().empty(),"A fresh installation has protected receipts");
  check(disarm_clock_logon_recovery(r,error),error.c_str());
  auto armed=arm_clock_logon_recovery(r,error);check(armed,error.c_str());
  check(clock_logon_recovery_armed(advanced)&&clock_logon_recovery_enrolled(advanced),"Durable enrollment missing");
  check(!clock_logon_recovery_enrolled(different),"Altered protected identity accepted");
  check(arm_clock_logon_recovery(r,error),"Repeated arm failed");
  std::filesystem::path helper;
  for (const auto& entry : std::filesystem::recursive_directory_iterator(store.dir)) {
   if(entry.path().filename()==L"druta-clock-recovery.exe")helper=entry.path();
  }
  check(!helper.empty(),"Protected helper not staged");
  const auto marker=helper.parent_path()/(std::wstring(r.id.begin(),r.id.end())+L".json");
  const auto wrong=helper.parent_path()/(std::wstring(32,L'a')+L".json");
  check(marker!=wrong,"Unexpected test receipt ID collision");
  std::filesystem::copy_file(marker,wrong);
  bool refused=false;
  try {(void)clock_logon_recovery_enrollments();}catch(const std::exception&){refused=true;}
  check(refused,"Corrupt enrollment with a mismatched filename was silently skipped");
  std::filesystem::remove(wrong);
  {
   std::fstream bytes(helper,std::ios::in|std::ios::out|std::ios::binary);
   char first{};bytes.read(&first,1);
   bytes.seekp(0);bytes.put(static_cast<char>(first^1));bytes.flush();
   check(bool(bytes),"Cannot alter fixture copy");
   bytes.close();
   check(!clock_logon_recovery_armed(r)&&clock_logon_recovery_enrolled(r),
         "Modified executable advertised as armed, or lost manual recovery evidence");
   check(!arm_clock_logon_recovery(r,error),"An active enrollment silently replaced its pinned executable");
   bytes.open(helper,std::ios::in|std::ios::out|std::ios::binary);
   bytes.put(first);bytes.close();
  }
  check(clock_logon_recovery_armed(r),"Restored fixture image should match its enrollment");
  store.consume(r.id,true);
  check(!disarm_clock_logon_recovery(r,error)&&clock_logon_recovery_enrolled(r),"Foreign command removed");
  store.consume(r.id);
  check(!clock_logon_recovery_armed(r)&&clock_logon_recovery_enrolled(r),"Consumed RunOnce lost its receipt");
  const auto orphaned=clock_logon_recovery_enrollments();
  check(orphaned.size()==1&&clock_logon_receipts_match(orphaned.front(),advanced),"Consumed registration loses protected discovery");
  exercise_sign_in(r,helper);
  check(disarm_clock_logon_recovery(advanced,error)&&disarm_clock_logon_recovery(r,error)&&!clock_logon_recovery_enrolled(r),
        "Idempotent cleanup after logon failed");
  check(clock_logon_recovery_enrollments().empty(),"Removed protected receipt still discovered");
  std::puts("Logon recovery checks passed with a private registry hive and temporary protected files.");
  return 0;
 }catch(const std::exception& e){std::fprintf(stderr,"%s\n",e.what());return 1;}
}
