// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_tables_view.h"
#include <imgui_internal.h>
#include <cstdio>
#include <cstring>
#include <algorithm>
#include <string>
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
    // Classic's released 1.9.1 subsections: policy offsets and rail requests draw apart, offsets without a nested header.
    VoltageTableRow offset=row;offset.key={VoltageTableFamily::offsets,0,0};offset.label="Rail 0 REL offset";offset.binding="offset-binding";data.rows={row,offset};data.offsets_message="Installed offsets recognized";
    VoltageTablesViewState part;std::string text;
    const auto draw_part=[&](VoltageTablesPart which,bool busy=false,bool unlocked=true){ImGui::GetIO().DisplaySize={1500,1200};ImGui::NewFrame();ImGui::SetNextWindowSize({1400,1100});ImGui::Begin("Tables part");ImGui::LogToBuffer();
        auto result=draw_voltage_tables_controls(part,&data,9,busy,unlocked,true,which);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
    check(draw_part(VoltageTablesPart::offsets).empty(),"drawing the offsets part dispatches nothing");
    const auto has=[&](const char* s){return text.find(s)!=std::string::npos;};
    // voltage_offsets_panel.py: explainer, Read offsets, one selected offset with "Offset (mV)" and Apply now.
    check(has("Read offsets")&&has("Rail 0 REL offset")&&has("Offset (mV)")&&has("Apply now")&&has("Save offset profile")&&has("this build does not install them")
        &&!has("Policy scaffold offsets")&&!has("Read requests")&&!has("Save requests profile")&&!has("Include"),"offsets part shows only the installed offsets, in released wording");
    check(has("Rail 0 REL offset: 1 mV"),"offset inventory shows Python's mV text");
    draw_part(VoltageTablesPart::requests);
    check(has("Read requests")&&has("Save requests profile")&&has("Rail 0")&&has("REL offset (mV)")&&has("Apply REL")&&has("VCRIT high offset (mV)")
        &&has("Requests read. Voltage-request gate enabled.")&&has("Rail 0 REL: 1 mV")&&!has("Read offsets")&&!has("Policy scaffold offsets"),"requests part shows only the rail requests, one input per field");
    check(text.find("Signed offsets in mV")<text.find("Read requests"),"requests explainer precedes its controls");
    // voltage_requests_panel.py names the rail by its identity (voltage_bios_runtime.py _request_name).
    check(has("Rail 0 (NVVDD)"),"requests rail selector names the rail as Python does");
    {   // voltage_offsets_panel.py / voltage_requests_panel.py inventory wording.
        auto inventory=data;inventory.rows[1].reported=1250000;inventory.boost_percent=25;
        VoltageTableRow absent=offset;absent.key={VoltageTableFamily::offsets,1,3};absent.value.reset();absent.reported.reset();inventory.rows.push_back(absent);
        check(voltage_offsets_inventory(&inventory)=="Rail 0 REL offset: 1 mV\nRail 1 VMIN: unavailable","offset inventory: offsets, then unavailable offsets");
        // Every present rail's reported policy of the same read, effective included.
        inventory.offset_policy_uv[0]=std::array<int64_t,5>{1250000,1200000,1300500,700000,1100000};
        check(voltage_offsets_inventory(&inventory)=="Rail 0 REL offset: 1 mV\nRail 1 VMIN: unavailable\nRail 0 reported REL: 1250 mV\nRail 0 reported ALT: 1200 mV"
            "\nRail 0 reported OV: 1300.5 mV\nRail 0 reported VMIN: 700 mV\nRail 0 reported effective: 1100 mV","offset inventory then lists each present rail's reported policy as Python does");
        check(voltage_requests_inventory(&inventory)=="Rail 0 REL: 1 mV\nPreserved voltage boost request: 25%","request inventory always states the preserved boost");
        inventory.request_policy_uv[1]=std::array<int64_t,5>{1000000,1010000,1200000,650000,987500};
        check(voltage_requests_inventory(&inventory)=="Rail 0 REL: 1 mV\nRail 1 reported REL policy: 1000 mV\nRail 1 reported ALT/OP policy: 1010 mV"
            "\nRail 1 reported OV policy: 1200 mV\nRail 1 reported VMIN policy: 650 mV\nRail 1 reported effective policy: 987.5 mV\nPreserved voltage boost request: 25%",
            "request inventory lists each rail's reported policy before the boost");
        inventory.request_policy_uv[1].reset();inventory.boost_percent.reset();inventory.requests_complete=false;inventory.missing_rails={1};
        check(voltage_requests_inventory(&inventory)=="Rail 0 REL: 1 mV\nPreserved voltage boost request: unknown\nActive rails unavailable for complete preservation: [1]; applying disabled","an unread boost is unknown, never omitted, and the missing rails are named");
        inventory.missing_rails={0,1};
        check(voltage_requests_inventory(&inventory).find("Active rails unavailable for complete preservation: [0, 1]; applying disabled")!=std::string::npos,"several missing rails print as Python's list");
        check(voltage_offsets_inventory(nullptr).empty()&&voltage_requests_inventory(nullptr).empty(),"inventories are empty before a read");
        auto other=data;other.rows[0].policy_type=3;VoltageTablesViewState fresh;
        ImGui::GetIO().DisplaySize={1500,1200};ImGui::NewFrame();ImGui::SetNextWindowSize({1400,1100});ImGui::Begin("Tables rail");ImGui::LogToBuffer();
        draw_voltage_tables_controls(fresh,&other,9,false,true,true,VoltageTablesPart::requests);const std::string shown=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();
        check(shown.find("Rail 0 (Rail 0)")!=std::string::npos,"a rail whose identity is not NVVDD keeps Python's generic name");
    }
    data.offsets_recognized=true;draw_part(VoltageTablesPart::offsets);data.offsets_recognized=false;
    check(has("Rail 0 REL")&&!has("NVVDD REL")&&has("Offsets read for the selected adapter.")&&!has("Installed offsets recognized"),
        "offset selector and status follow voltage_offsets_panel.py; the service's read account is not printed under the line");
    auto* window=ImGui::FindWindowByName("Tables part");
    const auto part_id=[&](VoltageTablesPart which,const char* label,int field=-1){const int scope=static_cast<int>(which);
        auto id=ImHashData(&scope,sizeof(scope),ImHashStr("voltage-tables",0,window->ID));if(field>=0)id=ImHashData(&field,sizeof(field),id);return ImHashStr(label,0,id);};
    ImGui::ActivateItemByID(part_id(VoltageTablesPart::offsets,"Read offsets"));
    out=draw_part(VoltageTablesPart::offsets);check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::read&&out[0].families==2,"offsets part reads installed offsets");
    auto* offset_draft=&*std::find_if(part.drafts.begin(),part.drafts.end(),[&](const auto& d){return d.key==offset.key;});
    std::strcpy(offset_draft->text.data(),"-2.5");offset_draft->dirty=true;
    ImGui::ActivateItemByID(part_id(VoltageTablesPart::offsets,"Apply now"));out=draw_part(VoltageTablesPart::offsets);
    check(out.size()==1&&out[0].families==2&&out[0].requests.size()==1&&out[0].requests[0].key==offset.key&&out[0].requests[0].value==-2500&&out[0].requests[0].binding=="offset-binding","Apply now sends the selected offset as before");
    {   // Apply now is inert while locked, busy, recovering an offset, or on a stale or non-writable offset.
        const auto press_offset=[&](const char* label,bool busy=false,bool unlocked=true){ImGui::ActivateItemByID(part_id(VoltageTablesPart::offsets,label));return draw_part(VoltageTablesPart::offsets,busy,unlocked);};
        for(const char* label:{"Apply now"}){
            check(press_offset(label).size()==1,"an offset write is enabled while unlocked and idle");
            check(press_offset(label,false,false).empty()&&press_offset(label,true,true).empty(),"an offset write is inert while locked or busy");
            data.offset_recovery_pending=true;check(press_offset(label).empty(),"an offset write is inert while an offset recovery is pending");data.offset_recovery_pending=false;
            data.rows[1].stale=true;check(press_offset(label).empty(),"a stale offset cannot be written");data.rows[1].stale=false;
            data.rows[1].writable=false;check(press_offset(label).empty(),"a non-writable offset cannot be written");data.rows[1].writable=true;
        }
    }
    ImGui::ActivateItemByID(part_id(VoltageTablesPart::offsets,"Save offset profile"));out=draw_part(VoltageTablesPart::offsets);
    check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::save_profile&&out[0].selection.size()==1&&out[0].selection[0]==offset.key,"Save offset profile saves the listed offsets");
    auto* request_draft=&*std::find_if(part.drafts.begin(),part.drafts.end(),[&](const auto& d){return d.key==row.key;});
    std::strcpy(request_draft->text.data(),"3");request_draft->dirty=true;draw_part(VoltageTablesPart::requests);
    ImGui::ActivateItemByID(part_id(VoltageTablesPart::requests,"Apply REL",0));out=draw_part(VoltageTablesPart::requests);
    check(out.size()==1&&out[0].families==1&&out[0].requests.size()==1&&out[0].requests[0].key==row.key&&out[0].requests[0].value==3000&&out[0].requests[0].binding==row.binding,"Apply REL sends the field request as before");
    {   // Apply REL is inert while locked, busy, recovering, or on a stale or non-writable request.
        const auto press_request=[&](const char* label,bool busy=false,bool unlocked=true){ImGui::ActivateItemByID(part_id(VoltageTablesPart::requests,label,0));return draw_part(VoltageTablesPart::requests,busy,unlocked);};
        for(const char* label:{"Apply REL"}){
            check(press_request(label).size()==1,"a request write is enabled while unlocked and idle");
            check(press_request(label,false,false).empty()&&press_request(label,true,true).empty(),"a request write is inert while locked or busy");
            data.recovery_pending=true;check(press_request(label).empty(),"a request write is inert while a recovery is pending");data.recovery_pending=false;
            data.rows[0].stale=true;check(press_request(label).empty(),"a stale request cannot be written");data.rows[0].stale=false;
            data.rows[0].writable=false;check(press_request(label).empty(),"a non-writable request cannot be written");data.rows[0].writable=true;
        }
    }
    ImGui::ActivateItemByID(part_id(VoltageTablesPart::requests,"Apply ALT/OP",1));check(draw_part(VoltageTablesPart::requests).empty(),"a field the rail did not report cannot be applied");
    data.gate_enabled=false;draw_part(VoltageTablesPart::requests);check(has("Voltage-request gate disabled: requests are suppressed."),"gate status line follows Python");data.gate_enabled=true;
    const auto count=[&](const std::string& s){size_t n=0;for(auto at=text.find(s);at!=std::string::npos;at=text.find(s,at+1))++n;return n;};
    {   // voltage_requests_panel.py refresh / _gate_message: the read's and the gate's own failures.
        auto failed=data;failed.gate_enabled.reset();failed.requests_complete=false;
        failed.requests_read_error=failed.requests_message=failed.message="Independent rail request read failed";failed.requests_read_retained=true;
        std::swap(failed,data);draw_part(VoltageTablesPart::requests);
        check(has("Could not read requests: Independent rail request read failed. Previous valid display retained; applying always reads fresh requests.")
            &&!has("Request gate read unavailable")&&count("Independent rail request read failed")==1,"a failed transient Read reads as Python's sentence, once, instead of the gate line");
        // refresh: a retained transient failure keeps the previous display.
        check(has("Rail 0 REL: 1 mV")&&has("Rail 0 (NVVDD)"),"a retained failed Read keeps the inventory and the rail selector");
        data.requests_read_retained=false;draw_part(VoltageTablesPart::requests);
        check(has("Could not read requests: Independent rail request read failed")&&!has("Previous valid display retained"),"a failed Read that kept nothing says only why");
        // _clear: nothing is kept on display; the stale rows stay only for the shared actions.
        check(!has("Rail 0 REL: 1 mV")&&!has("Rail 0 (NVVDD)")&&voltage_requests_inventory(&data).empty(),"a failed Read that kept nothing clears the inventory and the rail selector");
        data.requests_read_error.clear();data.requests_message=data.message="Complete current request inventory captured";data.gate_error="Voltage policy GET rejected (RM 31)";
        draw_part(VoltageTablesPart::requests);
        check(has("Request gate read unavailable; applying disabled: Voltage policy GET rejected (RM 31)")&&count("Complete current request inventory captured")==0,"an unread gate names its own failure and the service's read summary is not printed");
        check(has("Rail 0 REL: 1 mV"),"a good read shows the inventory again");
        data.offsets_read_error=data.offsets_message="Installed offset definitions were not recognized";draw_part(VoltageTablesPart::offsets);
        check(has("Could not read offsets: Installed offset definitions were not recognized")&&count("Installed offset definitions were not recognized")==1
            &&!has("Offsets read for the selected adapter."),"a failed offsets Read reads as Python's sentence");
        // voltage_offsets_panel.py refresh: any failed Read clears the box and the selector.
        check(!has("Rail 0 REL")&&voltage_offsets_inventory(&data).empty(),"a failed offsets Read clears the inventory and the offset selector");
        std::swap(failed,data);
        VoltageTablesData unread;VoltageTablesViewState none;std::swap(unread,data);std::swap(none,part);draw_part(VoltageTablesPart::requests);
        check(has("Read requests for the selected adapter.")&&!has("Request gate read unavailable"),"before any read the requests status asks for one");
        std::swap(unread,data);std::swap(none,part);
    }
    std::string shared_text;
    // Classic "Undo, recovery and profiles": tree_label opens one folded tree (logging expands
    // every tree), activate presses a control in it (row: the field's data row) or at its top.
    const auto draw_shared=[&](const char* tree_label,const char* activate,int row_index=-1,bool busy=false,bool unlocked=true){
        ImGui::GetIO().DisplaySize={1500,1200};ImGui::NewFrame();ImGui::SetNextWindowSize({1400,1100});ImGui::Begin("Tables shared");
        ImGui::PushID("voltage-tables");ImGui::PushID(200);auto& c=*ImGui::GetCurrentContext();
        if(tree_label){const auto tree=ImGui::GetID(tree_label);ImGui::GetStateStorage()->SetInt(tree,1);
            if(activate){ImGui::PushOverrideID(tree);if(row_index>=0)ImGui::PushID(row_index);const auto id=ImGui::GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;if(row_index>=0)ImGui::PopID();ImGui::PopID();}}
        else if(activate){const auto id=ImGui::GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;}
        ImGui::PopID();ImGui::PopID();
        ImGui::LogToBuffer();auto result=draw_voltage_tables_shared_controls(part,&data,9,busy,unlocked);shared_text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
    const auto shared_has=[&](const char* s){return shared_text.find(s)!=std::string::npos;};
    {   // Classic keeps the Focused table's chosen-subset saves under Undo, recovery and profiles: one
        // folded tree per family, sending the Focused save intents (families 2 and 1).
        for(auto& d:part.drafts)d.include=false;
        check(draw_shared("Save a selection of rail requests","Save selected requests profile").empty()&&draw_shared("Save a selection of offsets","Save selected offsets profile").empty(),"nothing chosen saves nothing");
        check(shared_has("Rail 0 REL")&&shared_has("Save selected offsets profile")&&shared_has("Save selected requests profile"),"each family lists its fields to choose");
        for(auto& d:part.drafts)d.include=true;
        out=draw_shared("Save a selection of rail requests","Save selected requests profile");
        check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::save_profile&&out[0].families==1&&out[0].selection.size()==1&&out[0].selection[0]==row.key,"the requests subset saves only chosen requests");
        out=draw_shared("Save a selection of offsets","Save selected offsets profile");
        check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::save_profile&&out[0].families==2&&out[0].selection.size()==1&&out[0].selection[0]==offset.key,"the offsets subset saves only chosen offsets");
        check(draw_shared("Save a selection of offsets","Save selected offsets profile",-1,true).empty(),"a busy worker saves nothing");
        for(auto& d:part.drafts)d.include=false;
    }
    {   // The released subsections draw only Python's rows, status, note and box; Restore initial,
        // loading requests, Undo, Recover and the staged requests are native and live here.
        draw_part(VoltageTablesPart::requests);
        check(!has("Restore initial")&&!has("Load requests profile")&&!has("Undo voltage requests")&&!has("Recover selected voltage requests")&&!has("Staged profile"),
            "the requests subsection holds no native recovery or profile-loading control");
        check(has("Apply saves a field-only undo point. Load saved requests manually from Profiles; they are never applied at startup or after a driver reset."),
            "the requests note is voltage_requests_panel.py's");
        const auto status_at=text.find("Requests read. Voltage-request gate enabled."),note_at=text.find("Apply saves a field-only undo point."),box_at=text.find("Rail 0 REL: 1 mV");
        check(status_at!=std::string::npos&&status_at<note_at&&note_at<box_at,"status, note, then the inventory box, as in Python");
        draw_part(VoltageTablesPart::offsets);
        check(!has("Restore initial")&&!has("Undo installed offsets")&&!has("Recover installed offsets")&&has("Save offset profile")&&has("Load offset profile..."),
            "the offsets subsection keeps Save and Load, as Python does, and nothing else native");
        draw_shared(nullptr,nullptr);
        check(shared_has("Undo installed offsets")&&shared_has("Recover installed offsets")&&shared_has("Load requests profile")&&shared_has("Undo voltage requests")
            &&shared_has("Recover selected voltage requests")&&shared_has("Restore initial offsets")&&shared_has("Restore initial requests")&&shared_has("Rail 0 REL: initial 1 mV"),
            "every relocated control is reachable under Undo, recovery and profiles, with the captured initial values");
        // Restore initial, relocated from the rows, sends the request the rows sent (data row 0 is the request, 1 the offset).
        out=draw_shared("Restore initial requests","Restore initial",0);
        check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::restore_initial&&out[0].families==1&&out[0].selection.size()==1&&out[0].selection[0]==row.key,"a request's Restore initial is unchanged");
        out=draw_shared("Restore initial offsets","Restore initial",1);
        check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::restore_initial&&out[0].families==2&&out[0].selection.size()==1&&out[0].selection[0]==offset.key,"an offset's Restore initial is unchanged");
        struct Case{const char* tree;int index;bool* recovery;};
        const Case cases[]{{"Restore initial requests",0,&data.recovery_pending},{"Restore initial offsets",1,&data.offset_recovery_pending}};
        for(const auto& c:cases){auto& r=data.rows[static_cast<size_t>(c.index)];
            check(draw_shared(c.tree,"Restore initial",c.index,false,false).empty()&&draw_shared(c.tree,"Restore initial",c.index,true,true).empty(),"Restore initial is inert while locked or busy");
            *c.recovery=true;check(draw_shared(c.tree,"Restore initial",c.index).empty(),"Restore initial is inert while its family recovers");*c.recovery=false;
            r.stale=true;check(draw_shared(c.tree,"Restore initial",c.index).empty(),"a stale field cannot be restored");r.stale=false;
            r.writable=false;check(draw_shared(c.tree,"Restore initial",c.index).empty(),"a non-writable field cannot be restored");r.writable=true;
            const auto initial=r.initial;r.initial.reset();check(draw_shared(c.tree,"Restore initial",c.index).empty()&&shared_has("initial unknown"),"a field without a captured initial cannot be restored");r.initial=initial;
            check(draw_shared(c.tree,"Restore initial",c.index).size()==1,"each refusal above is the gate's");}
        // Undo and Recover keep their gates: available while the edit gate is locked.
        data.undo_available=true;out=draw_shared(nullptr,"Undo voltage requests",-1,false,false);
        check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::undo&&out[0].families==1,"request Undo is reachable while locked");data.undo_available=false;
        data.recovery_pending=true;out=draw_shared(nullptr,"Recover selected voltage requests",-1,false,false);
        check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::recover&&out[0].families==1,"request recovery is reachable while locked");
        draw_part(VoltageTablesPart::requests);check(has("Recover it under Undo, recovery and profiles"),"the subsection points to the relocated recovery");data.recovery_pending=false;
        data.offset_undo_available=true;out=draw_shared(nullptr,"Undo installed offsets");
        check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::undo&&out[0].families==2,"offset Undo is unchanged");data.offset_undo_available=false;
        check(draw_shared(nullptr,"Recover installed offsets").empty(),"offset recovery needs a pending recovery");
        data.offset_recovery_pending=true;out=draw_shared(nullptr,"Recover installed offsets");
        check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::recover&&out[0].families==2,"offset recovery is unchanged");data.offset_recovery_pending=false;
        out=draw_shared(nullptr,"Load requests profile");check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::load_profile&&out[0].families==1,"loading requests still stages through the host");
        VoltageTableRequest staged_request;staged_request.key=row.key;staged_request.value=1500;staged_request.binding=row.binding;part.staged={staged_request};
        check(draw_shared(nullptr,"Apply staged profile",-1,false,false).empty(),"a staged profile needs unlock");
        out=draw_shared(nullptr,"Apply staged profile");
        check(out.size()==1&&out[0].operation==VoltageTablesIntent::Operation::apply&&out[0].families==1&&out[0].requests.size()==1&&out[0].requests[0].value==1500,"the staged profile applies only on its explicit Apply");
        draw_shared(nullptr,"Discard staged profile");check(part.staged.empty(),"Discard drops the staged profile");
    }
    {   // Python's single status line: after a request from these controls its published result takes
        // the line, the host's queued note goes, and a later Read brings the read line back.
        part.last={};part.status.clear();
        auto* draft=&*std::find_if(part.drafts.begin(),part.drafts.end(),[&](const auto& d){return d.key==row.key;});
        std::strcpy(draft->text.data(),"4");draft->dirty=true;draw_part(VoltageTablesPart::requests);
        ImGui::ActivateItemByID(part_id(VoltageTablesPart::requests,"Apply REL",0));check(draw_part(VoltageTablesPart::requests).size()==1,"Apply REL sends its request");
        part.status="Operation queued; see the operation log.";data.message="Selected signed request fields read back exactly; Undo available";
        draw_part(VoltageTablesPart::requests);
        check(has("Requests read. Voltage-request gate enabled.")&&has("Operation queued")&&!has("Stored request applied"),"until its result is published the line still describes the read");
        data.rows[0].value=4000;++data.revision;draw_part(VoltageTablesPart::requests);
        check(has("Stored request applied and exact full request readback verified.")&&!has("Requests read. Voltage-request gate enabled.")&&!has("Operation queued")
            &&!has("Selected signed request fields"),"a field that reads back the requested value takes voltage_requests_panel.py's applied line, replacing the queued note");
        draw_part(VoltageTablesPart::offsets);check(!has("Stored request applied")&&has("Offsets read for the selected adapter."),"a result stays with its own family");
        // A field that does not read back the requested value failed: "Could not apply request: <reason>".
        std::strcpy(draft->text.data(),"5");draft->dirty=true;
        ImGui::ActivateItemByID(part_id(VoltageTablesPart::requests,"Apply REL",0));check(draw_part(VoltageTablesPart::requests).size()==1,"Apply REL sends again");
        data.message="Admission gate disabled or unavailable; no SET issued";++data.revision;draw_part(VoltageTablesPart::requests);
        check(has("Could not apply request: Admission gate disabled or unavailable; no SET issued")&&count("no SET issued")==1,"a failed Apply reads as the panel's failure, once");
        // The value the field already holds: the panel's no-change line.
        std::strcpy(draft->text.data(),"4");draft->dirty=true;
        ImGui::ActivateItemByID(part_id(VoltageTablesPart::requests,"Apply REL",0));check(draw_part(VoltageTablesPart::requests).size()==1,"Apply REL sends the held value");
        data.message="Selected requests already match; no SET issued";++data.revision;draw_part(VoltageTablesPart::requests);
        check(has("No change requested; stored request already equals this value. No SET sent."),"an Apply of the held value reads as no change");
        ImGui::ActivateItemByID(part_id(VoltageTablesPart::requests,"Read requests"));check(draw_part(VoltageTablesPart::requests).size()==1,"Read requests sends a read");
        data.message=data.requests_message="Complete current request inventory captured";++data.revision;draw_part(VoltageTablesPart::requests);
        check(has("Requests read. Voltage-request gate enabled.")&&!has("Complete current request inventory captured")&&!has("No change requested"),
            "a Read brings back the released line, without the service's read summary");
        // Offsets: a held Apply now leaves the panel's read line; a failed one says why.
        auto* offset_text=&*std::find_if(part.drafts.begin(),part.drafts.end(),[&](const auto& d){return d.key==offset.key;});
        std::strcpy(offset_text->text.data(),"-3");offset_text->dirty=true;
        ImGui::ActivateItemByID(part_id(VoltageTablesPart::offsets,"Apply now"));check(draw_part(VoltageTablesPart::offsets).size()==1,"Apply now sends");
        data.message=data.offsets_message="Offset SET rejected by the driver";++data.revision;draw_part(VoltageTablesPart::offsets);
        check(has("Could not apply offset: Offset SET rejected by the driver")&&!has("Offsets read for the selected adapter."),"a failed offset Apply reads as voltage_offsets_panel.py's failure");
        std::strcpy(offset_text->text.data(),"-3");offset_text->dirty=true;
        ImGui::ActivateItemByID(part_id(VoltageTablesPart::offsets,"Apply now"));out=draw_part(VoltageTablesPart::offsets);
        check(out.size()==1&&out[0].requests[0].value==-3000,"Apply now sends again");
        data.rows[1].value=-3000;data.message=data.offsets_message="Selected signed offsets applied and verified";++data.revision;draw_part(VoltageTablesPart::offsets);
        check(has("Offsets read for the selected adapter.")&&!has("Could not apply offset")&&!has("Selected signed offsets applied"),"an offset Apply that held reads as the panel's refresh");
        // A request sent from the shared subsection reports under its own family's subsection.
        data.offset_undo_available=true;check(draw_shared(nullptr,"Undo installed offsets").size()==1,"offset Undo sends");data.offset_undo_available=false;
        data.message=data.offsets_message="Original issued offsets restored and verified";++data.revision;
        draw_part(VoltageTablesPart::offsets);check(has("Original issued offsets restored and verified")&&!has("Offsets read for the selected adapter."),"the offset Undo's result is the offsets status line");
        draw_part(VoltageTablesPart::requests);check(!has("Original issued offsets restored")&&has("Requests read. Voltage-request gate enabled."),"and not the requests one");
        part.last={};
    }
}
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;
    unsigned char* pixels;int width,height;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);run();ImGui::DestroyContext();
    std::printf("Voltage tables view: %d failures\n",failures);return failures?1:0;}
