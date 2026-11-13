// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_tables_view.h"
#include <imgui_internal.h>
#include <cstdio>
#include <cstring>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool value,const char* why){if(!value){++failures;std::printf("FAIL: %s\n",why);}}
std::vector<VoltageTablesIntent> frame(VoltageTablesViewState& state,VoltageTablesData& data,bool busy=false,bool unlocked=true,bool classic=false){
    ImGui::GetIO().DisplaySize={1500,1200};ImGui::NewFrame();ImGui::SetNextWindowSize({1400,1100});ImGui::Begin("Tables view test");
    auto out=draw_voltage_tables_controls(state,&data,9,busy,unlocked,classic);ImGui::End();ImGui::Render();return out;
}
std::vector<VoltageTablesIntent> press(VoltageTablesViewState& state,VoltageTablesData& data,const char* label,bool busy=false,bool unlocked=true,int rail=-1,int field=-1){
    auto* window=ImGui::FindWindowByName("Tables view test");ImGuiID id=ImHashStr("voltage-tables",0,window->ID);
    if(rail>=0){id=ImHashStr("voltage-request-fields",0,id);id=ImHashData(&rail,sizeof(rail),id);id=ImHashData(&field,sizeof(field),id);}
    ImGui::ActivateItemByID(ImHashStr(label,0,id));return frame(state,data,busy,unlocked);
}
void run(){
    check(parse_voltage_table_mv("-2147483.648")==INT32_MIN&&parse_voltage_table_mv("2147483.647")==INT32_MAX,"exact signed int32 mV endpoints");
    check(parse_voltage_table_mv("1e-3")==1&&parse_voltage_table_mv(" -0.001 ")==-1&&parse_voltage_table_mv("1.2340000000")==1234,"exact decimal/exponent whole-microvolt conversion");
    check(!parse_voltage_table_mv("2147483.648")&&!parse_voltage_table_mv("0.0001")&&!parse_voltage_table_mv("nan")&&!parse_voltage_table_mv("1.234000000000000000001"),"no fractional-uV, nonfinite, overflow or rounded-away decimal accepted");
    VoltageTablesData data;data.identity.size=sizeof(data.identity);std::strcpy(data.identity.uuid,"GPU-view-alt");std::strcpy(data.identity.slot,"0000:af:02.0");data.requests_complete=true;data.gate_enabled=true;
    VoltageTableRow row;row.key={VoltageTableFamily::requests,0,0};row.label="Rail 0 REL";row.binding="fresh-info-binding";row.value=row.initial=1000;row.policy_type=1;row.control_type=0;row.writable=true;row.availability=VoltagePolicyAvailability::available;data.rows={row};
    VoltageTablesViewState state;check(frame(state,data).empty()&&state.drafts.size()==1&&!std::strcmp(state.drafts[0].text.data(),"1.000"),"both table values and units seed read-only drafts without dispatch");
    std::strcpy(state.drafts[0].text.data(),"-1.234");state.drafts[0].dirty=true;
    auto out=press(state,data,"Apply",false,true,0,0);
    check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::apply&&out[0].requests.size()==1&&out[0].requests[0].value==-1234
        &&out[0].requests[0].expected==1000&&out[0].requests[0].binding==row.binding,"Apply emits only selected exact signed uV plus original expected/binding");
    frame(state,data);std::strcpy(state.drafts[0].text.data(),"2.000");state.drafts[0].dirty=true;data.rows[0].value=1500;frame(state,data);
    check(!std::strcmp(state.drafts[0].text.data(),"2.000")&&state.drafts[0].expected==1000,"refresh preserves dirty draft and conflict baseline");
    check(press(state,data,"Apply",false,false,0,0).empty(),"new edits require explicit unlock");
    data.rows[0].stale=true;check(press(state,data,"Apply",false,true,0,0).empty(),"stale last-good requests remain visible without dispatch");
    data.rows[0].stale=false;data.recovery_pending=true;check(press(state,data,"Apply",false,true,0,0).empty(),"uncertain request blocks new edits");
    out=press(state,data,"Recover selected voltage requests",false,false);check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::recover,"owned recovery remains reachable while controls locked");
    data.recovery_pending=false;data.undo_available=true;out=press(state,data,"Undo voltage requests",false,false);
    check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::undo,"Undo is distinct and available while edit gate locked");
    check(press(state,data,"Read voltage requests",true).empty(),"busy worker cannot queue overlapping read");
    state.drafts[0].include=true;out=press(state,data,"Save selected requests profile");check(out.size()==1&&out[0].selection.size()==1&&out[0].operation==VoltageTablesIntent::Operation::save_profile,"Save emits explicit selected-field host intent without applying");
    auto profile=capture_voltage_tables_profile(data,{row.key});profile["fields"][0]["value_uv"]=2000;
    check(stage_voltage_tables_document(state,profile,data)&&state.staged.size()==1,"manual load stages typed requests without a worker intent");
    out=press(state,data,"Apply staged profile");check(out.size()==1&&out[0].requests[0].value==2000,"staged profile requires separate explicit Apply");
    const auto staged=state.staged;profile["target"]["uuid"]="GPU-other";check(!stage_voltage_tables_document(state,profile,data)&&state.staged.size()==staged.size(),"invalid loaded profile preserves previous staged draft");
    sync_voltage_tables_view(state,&data,10);check(!state.drafts[0].dirty&&state.staged.empty(),"adapter generation change clears old-target drafts and staged profiles");
    frame(state,data,false,true,true);check(!state.drafts.empty(),"Classic uses same typed service draft state");
}
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;
    unsigned char* pixels;int width,height;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);run();ImGui::DestroyContext();
    std::printf("Voltage tables view: %d failures\n",failures);return failures?1:0;}
