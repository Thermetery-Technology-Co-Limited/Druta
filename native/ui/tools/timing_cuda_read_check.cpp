// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Explicit read-only CUDA load probe; optional helper mode captures timings.
// Neither mode submits tuning requests or acquires a timing hold.
#include "core_client.h"
#include "timing_cuda_process.h"
#include "timing_model.h"
#include <windows.h>
#include <cstdio>
#include <cwchar>
#include <stdexcept>
using namespace druta::ui;
namespace {
std::string ascii(const wchar_t* value){std::string out;for(;*value;++value){if(*value<0||*value>127)return {};out.push_back(static_cast<char>(*value));}return out;}
bool bound(const LiveData& data,const std::string& slot,const std::string& uuid){return data.connected&&data.identity.nvapi_ok&&data.identity.nvapi_selected&&!data.identity.pairing_error[0]&&data.identity.slot==slot&&data.identity.uuid==uuid;}
}
int wmain(int argc,wchar_t** argv){
 if(argc!=4&&argc!=5){std::puts("Usage: timing-cuda-read-check CORE_DLL PCI_SLOT EXPECTED_UUID [NVTUNE]");return 2;}
 const auto slot=ascii(argv[2]),uuid=ascii(argv[3]);auto backend=create_core_backend(argv[1]);std::string error;
 if(slot.empty()||uuid.empty()||!backend->open(slot,error)){std::printf("Open failed: %s\n",error.c_str());return 2;}
 ProfileJson report={{"slot",slot},{"uuid",uuid},{"hardware_set_requested",false},{"started",false},{"samples",ProfileJson::array()},{"ok",false}};const auto began=GetTickCount64();std::unique_ptr<TimingCudaProcess> process;bool started=false,settled=false;
 try{
  if(!bound(backend->read(false),slot,uuid))throw std::runtime_error("Expected paired adapter identity mismatch");
  if(argc==5){
   report.erase("started");report.erase("samples");report["mode"]="helper_capture";Command select;select.operation=Command::Operation::timings_select_helper;select.timing_helper_path=argv[4];const auto selected=backend->execute(select);report["helper_selection"]={{"ok",selected.ok},{"may_have_changed",selected.may_have_changed},{"message",selected.message}};
   if(!selected.ok||selected.may_have_changed)throw std::runtime_error("Timing helper selection failed: "+selected.message);
   Command capture;capture.operation=Command::Operation::timings_capture_p0;capture.timing_generation=1;capture.timing_allow_hold=false;const auto result=backend->execute(capture);const auto data=backend->read(false);if(!bound(data,slot,uuid))throw std::runtime_error("Adapter identity changed during timing capture");
   report["capture"]={{"ok",result.ok},{"may_have_changed",result.may_have_changed},{"message",result.message}};if(!data.timings)throw std::runtime_error("Timing capture result was not published");const auto& t=*data.timings;
   const char* method=t.capture_method==TimingCaptureMethod::direct?"direct":t.capture_method==TimingCaptureMethod::existing_hold?"existing_hold":t.capture_method==TimingCaptureMethod::temporary_hold?"temporary_hold":t.capture_method==TimingCaptureMethod::cuda?"cuda":"none";report["capture"]["method"]=method;
   const auto snapshot_json=[](const TimingSnapshot& snap){ProfileJson out={{"ok",snap.ok},{"top_band",snap.top_band},{"memory_clock_stable",snap.memory_clock_stable},{"message",snap.message},{"memory_before_mhz",nullptr},{"memory_after_mhz",nullptr},{"pstate_before",nullptr},{"pstate_after",nullptr}};if(snap.memory_before_mhz)out["memory_before_mhz"]=*snap.memory_before_mhz;if(snap.memory_after_mhz)out["memory_after_mhz"]=*snap.memory_after_mhz;if(snap.pstate_before)out["pstate_before"]=*snap.pstate_before;if(snap.pstate_after)out["pstate_after"]=*snap.pstate_after;return out;};
   report["snapshot"]=snapshot_json(t.snapshot);report["diagnostic"]=t.capture_diagnostic?snapshot_json(*t.capture_diagnostic):ProfileJson();report["capture_hold_pending"]=t.capture_hold_pending;report["capture_workload_pending"]=t.capture_workload_pending;report["auxiliary_cleanup_pending"]=backend->auxiliary_cleanup_needed();
   const bool diagnostic=t.capture_diagnostic&&t.capture_diagnostic->ok;const bool qualified=result.ok&&t.snapshot.ok&&t.snapshot.top_band;report["qualified"]=qualified;report["diagnostic_only"]=!qualified&&diagnostic;report["ok"]=qualified&&!result.may_have_changed&&!t.capture_hold_pending&&!t.capture_workload_pending&&!backend->auxiliary_cleanup_needed();if(!qualified)report["error"]="Timing capture was not qualified; diagnostic data does not establish success";
   const auto closed=backend->prepare_close();report["close"]={{"ok",closed.ok},{"may_have_changed",closed.may_have_changed},{"message",closed.message},{"auxiliary_cleanup_pending",backend->auxiliary_cleanup_needed()}};backend->close();report["elapsed_ms"]=GetTickCount64()-began;report["ok"]=report["ok"].get<bool>()&&closed.ok&&!closed.may_have_changed&&!report["close"]["auxiliary_cleanup_pending"].get<bool>();std::puts(report.dump(2).c_str());return report["ok"].get<bool>()?0:1;
  }
  wchar_t exe[32768]{};const auto n=GetModuleFileNameW(nullptr,exe,32768);if(!n||n>=32768)throw std::runtime_error("Could not determine probe executable path");std::wstring worker(exe,n);const auto slash=worker.find_last_of(L"\\/");if(slash==std::wstring::npos)throw std::runtime_error("Probe executable has no parent directory");worker.resize(slash+1);worker+=L"druta-ui-timing-cuda.exe";
  process=make_timing_cuda_process(worker);if(!process||!process->start(slot,15000,error))throw std::runtime_error("CUDA worker start failed: "+error);started=true;report["started"]=true;
  const auto startup=GetTickCount64();bool copying=false;while(GetTickCount64()-startup<5000){const auto status=process->poll();if(status.copying){copying=true;break;}if(status.finished||status.cleanup_pending)throw std::runtime_error("CUDA worker ended during startup: "+status.message);Sleep(50);}if(!copying)throw std::runtime_error("CUDA worker startup timed out");
  const auto sampling=GetTickCount64();uint32_t last=0;unsigned stable=0;while(GetTickCount64()-sampling<5000){const auto before=process->poll();if(!before.copying||before.finished||before.cleanup_pending)throw std::runtime_error("CUDA workload stopped before telemetry: "+before.message);
   const auto data=backend->read(false);if(!bound(data,slot,uuid))throw std::runtime_error("Adapter identity changed during CUDA workload");
   const auto after=process->poll();if(!after.copying||after.finished||after.cleanup_pending)throw std::runtime_error("CUDA workload stopped during telemetry: "+after.message);
   ProfileJson sample={{"memory_mhz",nullptr},{"pstate",nullptr}};if(data.telemetry_valid&&(data.telemetry.present&DC_T_MEM)&&data.telemetry.mem){const uint32_t value=data.telemetry.mem;sample["memory_mhz"]=value;stable=value==last?stable+1:1;last=value;}else{last=0;stable=0;}
   if(data.telemetry_valid&&(data.telemetry.present&DC_T_PSTATE))sample["pstate"]=data.telemetry.pstate;report["samples"].push_back(sample);if(stable>=3){settled=true;break;}Sleep(250);
  }
  if(!settled)throw std::runtime_error("Three stable valid memory readings were not observed");
 }catch(const std::exception& e){report["error"]=e.what();}
 TimingCudaProcessStatus cleanup;if(process)cleanup=process->stop(200,1000);report["cleanup"]={{"finished",cleanup.finished},{"ok",cleanup.ok},{"pending",process?process->pending():false},{"cleanup_pending",cleanup.cleanup_pending},{"message",cleanup.message}};
 const auto closed=backend->prepare_close();report["close"]={{"ok",closed.ok},{"may_have_changed",closed.may_have_changed},{"message",closed.message}};backend->close();report["elapsed_ms"]=GetTickCount64()-began;report["ok"]=started&&settled&&cleanup.finished&&cleanup.ok&&!cleanup.cleanup_pending&&process&&!process->pending()&&closed.ok&&!closed.may_have_changed&&!report.contains("error");std::puts(report.dump(2).c_str());return report["ok"].get<bool>()?0:1;
}
