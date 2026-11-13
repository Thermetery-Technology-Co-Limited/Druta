// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_limits_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static std::vector<VoltageLimitsIntent> frame(VoltageLimitsViewState& state,const VoltageLimitsData* data,bool unlocked,bool busy=false,const char* activate=nullptr,int row=-1){
 ImGui::NewFrame();ImGui::SetNextWindowSize({1400,1500});ImGui::Begin("limits-test");ImGui::PushID("voltage-limits");if(row>=0)ImGui::PushID(row);if(activate){auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;}if(row>=0)ImGui::PopID();ImGui::PopID();auto out=draw_voltage_limits_controls(state,data,1,busy,unlocked);ImGui::End();ImGui::Render();return out;
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;io.DisplaySize={1600,1700};unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
 VoltageLimitsViewState state;VoltageLimitsData data;data.identity.size=sizeof(data.identity);std::strcpy(data.identity.uuid,"GPU-other");std::strcpy(data.identity.slot,"0000:07:00.0");
 VoltageLimitRow device;device.key={VoltageLimitKind::switch_delay,0,4};device.label="PWM device 4";device.binding="current-device-proof";device.value=10;device.initial_value=10;device.writable=true;device.availability=VoltagePolicyAvailability::available;device.voltage_domain=2;device.operation_type=3;data.rows={device};
 CHECK(frame(state,&data,false).empty());auto out=frame(state,&data,false,false,"Read voltage definitions");CHECK(out.size()==1&&out[0].families==1);out=frame(state,&data,false,false,"Read voltage device delays");CHECK(out.size()==1&&out[0].families==2);
 CHECK(frame(state,&data,true,true,"Read voltage device delays").empty());CHECK(frame(state,&data,false,false,"Apply selected",0).empty());
 state.drafts[0].dirty=true;state.drafts[0].value=25;data.rows[0].value=12;out=frame(state,&data,true,false,"Apply selected",0);CHECK(out.size()==1&&out[0].requests[0].value==25&&out[0].requests[0].expected_value==10);CHECK(state.drafts[0].value==25);
 out=frame(state,&data,true,false,"Use current",0);CHECK(out.empty()&&!state.drafts[0].dirty&&state.drafts[0].value==12);
 out=frame(state,&data,false,false,"Save selected voltage profile...");CHECK(out.size()==1&&out[0].selection.size()==1);out=frame(state,&data,false,false,"Load voltage profile...");CHECK(out.size()==1&&out[0].operation==VoltageLimitsIntent::Operation::load_profile);
 auto doc=capture_voltage_limits_profile(data,{device.key});doc["entries"][0]["value"]=35;CHECK(stage_voltage_limits_document(state,doc,data)&&state.staged.size()==1);CHECK(frame(state,&data,false,false,"Apply staged voltage profile").empty());out=frame(state,&data,true,false,"Apply staged voltage profile");CHECK(out.size()==1&&out[0].requests[0].value==35&&out[0].requests[0].expected_value==12);
 data.recovery_pending=true;data.undo_available=true;CHECK(frame(state,&data,true,false,"Apply selected",0).empty());CHECK(frame(state,&data,true,false,"Undo voltage limits").empty());out=frame(state,&data,true,false,"Recover selected voltage limit");CHECK(out.size()==1&&out[0].operation==VoltageLimitsIntent::Operation::recover);
 data.recovery_pending=false;out=frame(state,&data,true,false,"Restore captured initial",0);CHECK(out.size()==1&&out[0].selection[0]==device.key);
 data.rows[0].stale=true;CHECK(!stage_voltage_limits_document(state,doc,data)&&state.staged.empty());sync_voltage_limits_view(state,&data,2);CHECK(state.staged.empty()&&state.generation==2);
 ImGui::DestroyContext();std::printf("Voltage limits view: %u checks\n",checks);return 0;
}
