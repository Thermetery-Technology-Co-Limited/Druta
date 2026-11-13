/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "timings_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
int main(){
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.snapshot.ok=true;data.snapshot.top_band=true;
 TimingField a;a.name="FAW";a.reg="CONFIG0";a.lo=8;a.hi=15;a.maximum=255;
 TimingField b;b.name="TRAIN";b.reg="CONFIG0";b.lo=0;b.hi=7;b.maximum=255;b.structural=true;
 data.snapshot.fields={a,b};data.snapshot.broadcast={{"CONFIG0","DOCUMENTED",0x1000,0xABCD182D}};
 TimingsViewState state;sync_timings_view(state,&data,17);
 CHECK(can_capture_timing(&data,false)); // hold permission is independent of read-only workload availability
 CHECK(!can_capture_timing(&data,true)&&!can_capture_timing(nullptr,false));
 data.capture_workload_pending=true;CHECK(!can_capture_timing(&data,false));data.capture_workload_pending=false;
 data.capture_hold_pending=true;CHECK(!can_capture_timing(&data,false));data.capture_hold_pending=false;
 data.capture_running=true;CHECK(!can_capture_timing(&data,false));data.capture_running=false;
 CHECK(!can_preview_timing(state,&data,false,true));CHECK(select_timing_field(state,data,"FAW")&&state.value==24);
 CHECK(can_preview_timing(state,&data,false,true)&&!can_apply_timing(state,&data,false,true));
 state.value=25;TimingPlan plan;plan.ok=true;plan.field="FAW";plan.value=25;data.preview=plan;
 CHECK(can_apply_timing(state,&data,false,true));
 CHECK(!can_apply_timing(state,&data,true,true)&&!can_apply_timing(state,&data,false,false));
 state.value=26;CHECK(!can_apply_timing(state,&data,false,true));state.value=25;
 data.snapshot.top_band=false;CHECK(!can_preview_timing(state,&data,false,true));data.snapshot.top_band=true;
 CHECK(select_timing_field(state,data,"TRAIN")&&!can_preview_timing(state,&data,false,true));
 CHECK(select_timing_field(state,data,"FAW"));data.snapshot.fields[0].overlapping=true;CHECK(!can_preview_timing(state,&data,false,true));
 data.snapshot.fields[0].overlapping=false;state.value=256;CHECK(!can_preview_timing(state,&data,false,true));
 state.value=25;auto failed=data;failed.snapshot.ok=false;failed.snapshot.fields.clear();sync_timings_view(state,&failed,17);CHECK(state.field=="FAW" && state.value==25);
 sync_timings_view(state,&data,18);CHECK(state.field.empty()&&state.generation==18);
 data.snapshot.memory_clock_stable=true;data.snapshot.slot="0000:09:00.0";data.snapshot.codename="GP102";
 data.snapshot.memory_before_mhz=data.snapshot.memory_after_mhz=7000;data.snapshot.pstate_before=data.snapshot.pstate_after=0;
 data.snapshot.offset_before=data.snapshot.offset_after=0;data.identity.nvapi_ok=1;
 std::snprintf(data.identity.slot,sizeof(data.identity.slot),"%s","0000:09:00.0");
 std::snprintf(data.identity.uuid,sizeof(data.identity.uuid),"%s","GPU-timing");
 TimingField ras;ras.name="RAS";ras.reg="CONFIG0";ras.lo=16;ras.hi=23;ras.maximum=255;data.snapshot.fields.push_back(ras);
 data.helper_token="one";sync_timings_view(state,&data,18);
 CHECK(stage_timing_field(state,data,"FAW",25)&&stage_timing_field(state,data,"RAS",200));
 CHECK(state.staged.size()==2&&select_timing_field(state,data,"FAW")&&state.value==25);
 CHECK(!stage_timing_field(state,data,"TRAIN",5)&&state.staged.size()==2);
 const auto document=capture_timing_editor_profile(data.identity,data.snapshot,state.staged);
 CHECK(stage_timing_profile(document,data,state)&&state.staged.size()==2);
 CHECK(can_preview_timing_batch(state,&data,false));
 TimingBatchPlan batch;batch.ok=true;for(const auto& r:state.staged){TimingPlan f;f.ok=true;f.field=r.field;f.value=r.value;batch.fields.push_back(f);}data.batch_preview=batch;
 CHECK(can_apply_timing_batch(state,&data,false,true)&&!can_apply_timing_batch(state,&data,false,false));
 state.staged[0].value++;CHECK(!can_apply_timing_batch(state,&data,false,true));state.staged[0].value--;
 auto invalid=document;invalid["fields"][0]["value"]=true;CHECK(!stage_timing_profile(invalid,data,state)&&state.staged.size()==2);
 data.snapshot.ok=false;sync_timings_view(state,&data,18);CHECK(state.staged.size()==2&&!can_preview_timing_batch(state,&data,false));data.snapshot.ok=true;
 data.helper_token="two";sync_timings_view(state,&data,18);CHECK(state.staged.empty());
 ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DisplaySize=ImVec2(1024,768);io.DeltaTime=1.0f/60;
 unsigned char* pixels=nullptr;int w=0,h=0;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
 auto raw=data.snapshot;raw.ok=true;raw.top_band=false;raw.memory_before_mhz=raw.memory_after_mhz=5000;raw.pstate_before=raw.pstate_after=2;
 raw.broadcast={{"DIAGNOSTIC_ONLY","RAW",0x1040,0xDEADBEEF}};raw.fields[0].name="DIAGFIELD";raw.message="Below current top band";data.capture_diagnostic=raw;
 CHECK(select_timing_field(state,data,"FAW"));const auto selected=state.field;const auto value=state.value;const auto main_word=data.snapshot.broadcast.front().value;
 ImGui::NewFrame();ImGui::SetNextWindowSize(ImVec2(1000,740));ImGui::Begin("test");
 ImGui::GetStateStorage()->SetInt(ImGui::GetID("Inspect raw capture diagnostic"),1);
 ImGui::LogToBuffer();auto result=draw_timings(state,&data,18,false,true);const std::string log=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();
 CHECK(result.commands.empty()&&!result.choose_helper&&!result.save_profile&&!result.load_profile&&!result.cancel_capture);
 CHECK(log.find("DIAGNOSTIC_ONLY")!=std::string::npos&&log.find("0xDEADBEEF")!=std::string::npos);
 CHECK(log.find("5000 -> 5000 MHz")!=std::string::npos&&log.find("P2 to P2")!=std::string::npos);
 CHECK(log.find("does not authorize timing writes")!=std::string::npos);
 CHECK(state.field==selected&&state.value==value&&state.staged.empty()&&data.snapshot.broadcast.front().value==main_word&&data.snapshot.top_band);
 CHECK(!select_timing_field(state,data,"DIAGFIELD"));
 data.capture_diagnostic.reset();
 const auto press_disconnected=[&](const char* label,bool busy,bool unlocked){
     const auto* window=ImGui::FindWindowByName("test");CHECK(window);
     ImGui::ActivateItemByID(ImHashStr(label,0,window->ID));
     ImGui::NewFrame();ImGui::SetNextWindowSize(ImVec2(1000,740));ImGui::Begin("test");ImGui::LogToBuffer();
     auto output=draw_timings(state,&data,18,busy,unlocked,false);ImGui::LogFinish();ImGui::End();ImGui::Render();return output;
 };
 data.capture_workload_pending=true;
 auto cleanup=press_disconnected("Release timing capture resources",false,false);
 CHECK(cleanup.commands.size()==1&&cleanup.commands.front().operation==Command::Operation::timings_release_capture_hold);
 CHECK(press_disconnected("Release timing capture resources",true,true).commands.empty());
 data.capture_workload_pending=false;data.capture_hold_pending=true;
 cleanup=press_disconnected("Release timing capture resources",false,false);
 CHECK(cleanup.commands.size()==1&&cleanup.commands.front().operation==Command::Operation::timings_release_capture_hold);
 data.capture_hold_pending=false;state.value=25;
 CHECK(press_disconnected("Read timings",false,true).commands.empty());
 CHECK(press_disconnected("Capture at performance band",false,true).commands.empty());
 CHECK(press_disconnected("Apply timing",false,true).commands.empty());
 data.capture_running=true;auto cancel=press_disconnected("Cancel timing capture",true,false);
 CHECK(cancel.cancel_capture&&cancel.commands.empty());
 ImGui::DestroyContext();
 std::printf("timings_view_test: %u checks passed\n",checks);
}
