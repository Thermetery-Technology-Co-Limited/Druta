// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weight_host.h"
#define main unused_weight_service_fixture_main
#include "power_weights_service_test.cpp"
#undef main
#include <cstdio>
#include <cstdlib>
#include <windows.h>
#include <cstring>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
ClockWorkerCompletion check_worker(const wchar_t* option,uint32_t timeout_ms) {
 wchar_t path[32768]{};
 CHECK(GetModuleFileNameW(nullptr,path,32768)>0);
 std::wstring cmd=L"\""+std::wstring(path)+L"\" "+option;
 return run_clock_worker(path,std::move(cmd),timeout_ms);
}
int main(int argc,char** argv){
 if(argc==2) {
  if(!clock_worker_admitted())return 1;
  if(std::strcmp(argv[1],"--stall")==0)Sleep(30000);
  return std::strcmp(argv[1],"--success")==0?0:1;
 }
 const auto stalled=check_worker(L"--stall",80),crashed=check_worker(L"--failure",5000),
            succeeded=check_worker(L"--success",5000);
 CHECK(stalled.stopped&&!stalled.success&&crashed.stopped&&!crashed.success&&succeeded.stopped&&succeeded.success);
 const auto missing=run_clock_worker(L"",L"",1000);
 CHECK(missing.stopped&&!missing.success);
 wchar_t scratch[MAX_PATH]{};
 CHECK(GetTempPathW(MAX_PATH,scratch)>0);
 char id[33]{};
 CHECK(std::snprintf(id,sizeof(id),"%032lx",static_cast<unsigned long>(GetCurrentProcessId()))==32);
 bool busy=false,disarmed=false;
 with_clock_operation_lock(scratch,id,[&] {
  try {with_clock_operation_lock(scratch,id,[&]{disarmed=true;});}
  catch(const std::exception&){busy=true;}
 });
 CHECK(busy&&!disarmed);
 with_clock_operation_lock(scratch,id,[&]{disarmed=true;});
 CHECK(disarmed);

 CHECK(is_clock_rescue_driver(L"nv4",L"NVIDIA")&&is_clock_rescue_driver(L"vendor-service",L"NVIDIA Corporation"));
 CHECK(!is_clock_rescue_driver(L"BasicDisplay",L"NVIDIA")&&!is_clock_rescue_driver(L"BasicDisplay",L"Microsoft"));
 CHECK(!is_clock_rescue_driver(L"nvlddmkm",L"Microsoft")&&!is_clock_rescue_driver(L"",L"NVIDIA"));
int status=99;wchar_t exe[]=L"druta.exe",normal[]=L"--gpu",helper[]=L"--weight-helper",child[]=L"--weight-verify-child",badid[]=L"../bad",action[]=L"apply",pid[]=L"1",core[]=L"C:\\core.dll";wchar_t* normal_args[]={exe,normal};CHECK(!dispatch_power_weight_host(2,normal_args,status)&&status==99);wchar_t* invalid[]={exe,helper,badid,action,pid,core};CHECK(dispatch_power_weight_host(6,invalid,status)&&status==1);wchar_t* shortargs[]={exe,child};status=99;CHECK(dispatch_power_weight_host(2,shortargs,status)&&status==1);PowerWeightHostRequest request;request.receipt_id="bad";request.parent_pid=0;request.executable=L"relative.exe";request.core=L"relative.dll";std::string why;CHECK(!launch_power_weight_host(request,why)&&!why.empty());int effects=0;CHECK(!power_weight_host_gate(7,[](uint32_t){return false;},[&]{++effects;})&&effects==0);CHECK(power_weight_host_gate(7,[](uint32_t){return true;},[&]{++effects;})&&effects==1);ProfileJson child_result={{"format","druta-weight-verify"},{"id","abc"},{"original",false},{"ok",true}};CHECK(power_weight_verify_result(child_result,"abc",false));CHECK(!power_weight_verify_result(child_result,"different",false)&&!power_weight_verify_result(child_result,"abc",true));child_result["ok"]=false;CHECK(!power_weight_verify_result(child_result,"abc",false));const auto pending=power_weight_child_timeout(false),stopped=power_weight_child_timeout(true);CHECK(!pending.ok&&pending.timed_out&&pending.uncertain&&pending.cleanup_pending&&!stopped.ok&&stopped.timed_out&&!stopped.cleanup_pending);DeviceRestartResult reboot;reboot.ok=true;reboot.reboot_required=true;const auto restart=power_weight_restart_result(reboot);CHECK(restart.ok&&restart.reboot_required);WeightFake fake;fake.current.registry=PowerWeightRegistrySnapshot{{L"EnableGpuFirmware",4,{17,0,0,0}}};auto receipt=make_weight_receipt(fake.current,{{0,1.25,4096}});const auto projected=derive_weight_receipt(receipt).after;receipt.issued_names.push_back(projected.back().name);CHECK(power_weight_verify_registry(receipt,projected,false));CHECK(!power_weight_verify_registry(receipt,receipt.before,false));auto original=receipt.before;original.push_back({L"unrelated2",4,{9}});CHECK(power_weight_verify_registry(receipt,original,true));auto conflict=projected;conflict.back().bytes.back()^=1;CHECK(!power_weight_verify_registry(receipt,conflict,true));CHECK(!power_weight_verify_registry(receipt,projected,true));std::puts("Weight host argument refusal passed");}
