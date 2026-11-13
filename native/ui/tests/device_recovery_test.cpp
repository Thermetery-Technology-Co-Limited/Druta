// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "device_recovery.h"
#include "profile_io.h"
#include <windows.h>
#include <cstdio>
#include <stdexcept>
using namespace druta::ui;
int failures=0;void check(bool b,const char* why){if(!b){++failures;std::printf("FAIL: %s\n",why);}}
int main(){DeviceRecoveryRequest req;req.parent_pid=45;req.target={"0000:09:00.0",L"PCI\\VEN_10DE&DEV_ABCD\\A",L"GPU"};req.uuid="GPU-alt";req.executable=L"C:\\Druta\\druta.exe";req.core=L"C:\\Druta\\drutacore.dll";req.receipt=L"C:\\Config\\receipt.json";
 const auto command=device_recovery_relaunch_command(req);check(command.find(L"--gpu")!=std::wstring::npos && command.find(L"--expect-uuid")!=std::wstring::npos && command.find(L"--recovery-result")!=std::wstring::npos && command.find(L"--profile")==std::wstring::npos && command.find(L"--apply")==std::wstring::npos,"fresh relaunch carries identity and receipt only");
 auto no_uuid=req;no_uuid.uuid.clear();check(device_recovery_relaunch_command(no_uuid).find(L"--expect-uuid")==std::wstring::npos,"UUID absent remains optional");
 int writes=0,waits=0,restarts=0,launches=0;bool writable=true,exited=true,relaunch=true,fail_restart=false;DeviceRecoveryReceipt last;
 DeviceRecoveryApi api;api.write_receipt=[&](const auto&,const auto& receipt,std::string&){++writes;last=receipt;return writable;};
 api.wait_parent=[&](uint32_t pid,uint32_t ms,std::string&){++waits;check(pid==45&&ms==45000,"bounded wait on requested parent");return exited;};
 api.restart=[&](const auto& target){++restarts;check(target.instance_id==req.target.instance_id,"same captured target");if(fail_restart)throw std::runtime_error("restart unavailable");DeviceRestartResult r;r.ok=true;r.message="restarted";return r;};
 api.relaunch=[&](const auto& r,std::string&){++launches;check(r.uuid==req.uuid&&r.target.slot==req.target.slot,"relaunch retains identity");return relaunch;};
 auto ok=run_device_recovery(req,api);check(ok.completed&&ok.parent_exited&&ok.relaunched&&writes==2&&waits==1&&restarts==1&&launches==1&&last.ok,"successful ordered recovery");
 writes=waits=restarts=launches=0;exited=false;auto timeout=run_device_recovery(req,api);check(!timeout.parent_exited&&writes==2&&waits==1&&!restarts&&!launches&&!last.ok,"live parent prevents restart and relaunch");
 exited=true;writes=waits=restarts=launches=0;writable=false;auto no_receipt=run_device_recovery(req,api);check(!no_receipt.parent_exited&&writes==1&&!waits&&!restarts&&!launches,"unwritable initial receipt prevents restart");
 writable=true;fail_restart=true;writes=waits=restarts=launches=0;auto failed=run_device_recovery(req,api);check(!failed.completed&&failed.parent_exited&&failed.relaunched&&last.uncertain&&writes==2&&launches==1,"restart exception persists failure and reopens after exit");
 wchar_t temp[MAX_PATH]{},folder[MAX_PATH]{};check(GetTempPathW(MAX_PATH,temp)&&GetTempFileNameW(temp,L"drc",0,folder),"temporary test path");DeleteFileW(folder);check(CreateDirectoryW(folder,nullptr)!=0,"create test receipt directory");
 std::wstring path=std::wstring(folder)+L"\\result.json";ProfileJson doc={{"format","druta-device-recovery"},{"version",1},{"slot",req.target.slot},{"uuid",req.uuid},{"instance_id","PCI\\VEN_10DE&DEV_ABCD\\A"},{"ok",true},{"reboot_required",false},{"uncertain",false},{"message","done"}};write_profile(path,doc);
 try{auto receipt=read_device_recovery_receipt(path,folder,req.target.slot,req.uuid);check(receipt.ok&&receipt.message=="done","matching receipt loads typed status");}catch(...){check(false,"matching receipt readable");}
 bool rejected=false;try{read_device_recovery_receipt(path,folder,req.target.slot,"GPU-different");}catch(...){rejected=true;}check(rejected,"wrong UUID receipt refused");
 rejected=false;try{read_device_recovery_receipt(path,folder,"0000:08:00.0",req.uuid);}catch(...){rejected=true;}check(rejected,"wrong slot receipt refused");
 doc["ok"]="yes";write_profile(path,doc);rejected=false;try{read_device_recovery_receipt(path,folder,req.target.slot,req.uuid);}catch(...){rejected=true;}check(rejected,"malformed receipt refused");DeleteFileW(path.c_str());RemoveDirectoryW(folder);
 std::printf("Device recovery: %d failures\n",failures);return failures?1:0;}
