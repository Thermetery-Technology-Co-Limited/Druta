// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_saving_view.h"
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <algorithm>
#include <string>
using namespace druta::ui;
static int checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
namespace {
std::vector<PowerSavingIntent> frame(PowerSavingViewState& state,PowerSavingData& data,bool busy=false,bool unlocked=true,bool classic=false,uint64_t generation=2){ImGui::GetIO().DisplaySize={1700,1600};ImGui::NewFrame();ImGui::SetNextWindowSize({1600,1500});ImGui::Begin("Power-saving test");auto out=draw_power_saving_controls(state,&data,generation,busy,unlocked,classic);ImGui::End();ImGui::Render();return out;}
std::vector<PowerSavingIntent> press(PowerSavingViewState& state,PowerSavingData& data,const char* label,int family=-1,int index=-1,bool busy=false,bool unlocked=true){auto* w=ImGui::FindWindowByName("Power-saving test");auto id=ImHashStr("power-saving",0,w->ID);if(family>=0){id=ImHashData(&family,sizeof(family),id);id=ImHashData(&index,sizeof(index),id);}ImGui::ActivateItemByID(ImHashStr(label,0,id));return frame(state,data,busy,unlocked);}
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.Fonts->AddFontDefault();io.Fonts->Build();
    CHECK(parse_power_saving_us(" 4294967295 ")==UINT32_MAX);CHECK(!parse_power_saving_us("4294967296")&&!parse_power_saving_us("0")&&!parse_power_saving_us("1.5")&&!parse_power_saving_us("-1"));
    PowerSavingData data;data.identity.size=sizeof(data.identity);std::strcpy(data.identity.uuid,"GPU-power-view");std::strcpy(data.identity.slot,"0000:01:02.0");PowerSavingRow row;row.key={PowerSavingFamily::idle_threshold,0};row.label="GR threshold";row.binding="current-route";row.capability="idle-layout";row.supported=row.writable=true;row.availability=VoltagePolicyAvailability::available;row.value=row.initial=2000;row.maximum_us=10000;row.raw_first=2000;row.raw_second=3000;row.scaled_first=2000;row.scaled_second=3000;row.scale=1000;data.rows.push_back(row);
    PowerSavingRow individual;individual.key={PowerSavingFamily::individual,0};individual.label="GR gating";individual.binding="other-route";individual.capability="individual-layout";individual.supported=individual.writable=true;individual.availability=VoltagePolicyAvailability::available;individual.value=individual.initial=0;data.rows.push_back(individual);
    PowerSavingViewState state;CHECK(frame(state,data).empty()&&state.drafts.size()==2);std::strcpy(state.drafts[0].text.data(),"4567");state.drafts[0].dirty=true;auto out=press(state,data,"Apply",2,0);CHECK(out.size()==1&&out[0].requests.size()==1&&!out[0].lifecycle_release&&out[0].requests[0].value==4567&&out[0].requests[0].expected==2000&&out[0].requests[0].binding=="current-route");
    CHECK(press(state,data,"Hold disable",1,0,true).empty());CHECK(press(state,data,"Hold disable",1,0,false,false).empty());out=press(state,data,"Hold disable",1,0);CHECK(out.size()==1&&out[0].requests[0].value==1&&!out[0].lifecycle_release);
    data.rows[1].owned=true;data.rows[1].value=1;data.rows[1].stale=true;data.rows[1].writable=false;out=press(state,data,"Release",1,0);CHECK(out.size()==1&&out[0].requests[0].value==0&&out[0].lifecycle_release);data.rows[1].uncertain=true;CHECK(press(state,data,"Release",1,0).empty());
    data.owned_requests=true;out=press(state,data,"Release owned power-saving requests");CHECK(out.size()==1&&out[0].operation==PowerSavingIntent::Operation::release_owned);CHECK(press(state,data,"Release owned power-saving requests",-1,-1,true).empty());
    data.recovery_pending=true;out=press(state,data,"Recover power-saving request");CHECK(out.size()==1&&out[0].operation==PowerSavingIntent::Operation::recover);data.recovery_pending=false;
    data.rows[1].stale=data.rows[1].uncertain=false;data.rows[1].value=1;data.rows[1].writable=true;const auto profile=capture_power_saving_profile(data,{{PowerSavingFamily::individual,0},{PowerSavingFamily::idle_threshold,0}});CHECK(stage_power_saving_document(state,profile,data)&&state.staged.size()==2);CHECK(frame(state,data,false,true,true).empty());
    frame(state,data);{auto* window=ImGui::FindWindowByName("Power-saving test");auto id=ImHashStr("power-saving",0,window->ID);const int index=0;id=ImHashData(&index,sizeof(index),id);ImGui::ActivateItemByID(ImHashStr("Apply staged request",0,id));out=frame(state,data);CHECK(out.size()==1&&out[0].operation==PowerSavingIntent::Operation::apply&&!out[0].lifecycle_release);}
    PowerSavingRow adaptive;adaptive.key={PowerSavingFamily::adaptive,0};adaptive.label="Adaptive";adaptive.binding="adaptive";adaptive.supported=adaptive.writable=true;adaptive.value=0;data.rows.push_back(adaptive);frame(state,data);out=press(state,data,"Allow",3,0);CHECK(out.size()==1&&out[0].requests[0].value==1&&!out[0].lifecycle_release);data.rows.pop_back();
    std::strcpy(state.drafts[0].text.data(),"7654");state.drafts[0].dirty=true;data.rows[0].value=2345;frame(state,data);CHECK(!std::strcmp(state.drafts[0].text.data(),"7654")&&state.drafts[0].expected==2000);frame(state,data,false,true,false,3);CHECK(state.staged.empty()&&!state.drafts[0].dirty&&state.drafts[0].expected==2345);
    {   // Classic draws one family per released 1.9.1 subsection; its actions stay scoped to that family.
        PowerSavingViewState part;std::string text;
        const auto draw_part=[&](PowerSavingFamily family,bool busy=false,bool unlocked=true){ImGui::GetIO().DisplaySize={1700,1600};ImGui::NewFrame();ImGui::SetNextWindowSize({1600,1500});ImGui::Begin("Power-saving part");ImGui::LogToBuffer();
            auto result=draw_power_saving_controls(part,&data,2,busy,unlocked,true,family);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
        const auto part_id=[&](const char* label,PowerSavingFamily family=PowerSavingFamily::individual,int index=0){auto* w=ImGui::FindWindowByName("Power-saving part");const int scope=100+static_cast<int>(family);
            auto id=ImHashData(&scope,sizeof(scope),ImHashStr("power-saving",0,w->ID));if(family==PowerSavingFamily::individual)id=ImHashData(&index,sizeof(index),id);return ImHashStr(label,0,id);};
        const auto has=[&](const char* s){return text.find(s)!=std::string::npos;};
        CHECK(draw_part(PowerSavingFamily::individual).empty());
        // individual_power_gating_panel.py: explainer and profile note first, then per controller
        // its name, Refresh / Save request profile / Hold disabled / Release and a readback.
        CHECK(has("GR gating")&&!has("GR threshold")&&!has("Idle thresholds (us)")&&!has("GR ELPG")&&!has("Profile"));
        CHECK(has("Each control holds an individual power-gating inhibit")&&has("A saved profile describes only this instance's request")&&has("Refresh")
            &&has("Save request profile")&&has("Hold disabled")&&has("Release")&&!has("This app:"));
        // The readback box carries individual_power_gating_panel.py's _format lines.
        CHECK(has("GR gating request support: available")&&has("This instance's disable request: held")
            &&has("Reason-zero inhibit readback: not present (may include other owners)")&&has("Reason masks: 0x48=0x00000000, 0x49=0x00000000"));
        CHECK(text.find("Each control holds")<text.find("GR gating")&&text.find("GR gating")<text.find("Hold disabled"));
        ImGui::ActivateItemByID(part_id("Refresh"));out=draw_part(PowerSavingFamily::individual);
        CHECK(out.size()==1&&out[0].operation==PowerSavingIntent::Operation::read&&out[0].families==(1u<<static_cast<uint32_t>(PowerSavingFamily::individual)));
        ImGui::ActivateItemByID(part_id("Save request profile"));out=draw_part(PowerSavingFamily::individual);
        CHECK(out.size()==1&&out[0].operation==PowerSavingIntent::Operation::save_profile&&out[0].selection.size()==1&&out[0].selection[0].family==PowerSavingFamily::individual);
        data.rows[1].owned=false;ImGui::ActivateItemByID(part_id("Hold disabled"));out=draw_part(PowerSavingFamily::individual);
        CHECK(out.size()==1&&out[0].requests.size()==1&&out[0].requests[0].value==1&&out[0].requests[0].binding=="other-route"&&!out[0].lifecycle_release);
        data.rows[1].owned=true;ImGui::ActivateItemByID(part_id("Release"));out=draw_part(PowerSavingFamily::individual);
        CHECK(out.size()==1&&out[0].requests[0].value==0&&out[0].lifecycle_release);
        {   // Hold disabled and Release are inert while locked or busy; Hold also while a recovery is
            // pending or the row is stale or not writable, Release while not owned or uncertain.
            auto& gating=data.rows[1];gating.owned=false;
            const auto inert=[&](const char* label,bool busy=false,bool unlocked=true){ImGui::ActivateItemByID(part_id(label));return draw_part(PowerSavingFamily::individual,busy,unlocked).empty();};
            CHECK(inert("Hold disabled",false,false)&&inert("Hold disabled",true,true));
            data.recovery_pending=true;CHECK(inert("Hold disabled"));data.recovery_pending=false;
            gating.stale=true;CHECK(inert("Hold disabled"));gating.stale=false;
            gating.writable=false;CHECK(inert("Hold disabled"));gating.writable=true;
            gating.owned=true;CHECK(inert("Release",false,false)&&inert("Release",true,true));
            gating.uncertain=true;CHECK(inert("Release"));gating.uncertain=false;
            gating.owned=false;CHECK(inert("Release"));gating.owned=true;
        }
        // gr_idle_threshold_panel.py: controller selector, "Idle threshold (us)", Refresh, Apply.
        draw_part(PowerSavingFamily::idle_threshold);
        CHECK(has("Idle threshold (\xC2\xB5s)")&&has("Adjust the idle delay")&&has("GR threshold driver readback: 2000 \xC2\xB5s; scaled pair: [2000, 3000]; raw pair: [2000, 3000]; scale: 1000; allowed: 1\xE2\x80\x93" "10000 \xC2\xB5s")&&!has("Request us"));
        auto& idle_draft=*std::find_if(part.drafts.begin(),part.drafts.end(),[](const auto& d){return d.key.family==PowerSavingFamily::idle_threshold;});
        std::strcpy(idle_draft.text.data(),"4321");idle_draft.dirty=true;
        ImGui::ActivateItemByID(part_id("Apply",PowerSavingFamily::idle_threshold));out=draw_part(PowerSavingFamily::idle_threshold);
        CHECK(out.size()==1&&out[0].requests[0].value==4321&&out[0].requests[0].binding=="current-route"&&!idle_draft.dirty);
        {   // Apply is inert, and keeps the edit, while locked, busy, recovering, stale or not writable.
            std::strcpy(idle_draft.text.data(),"4322");idle_draft.dirty=true;
            const auto inert=[&](bool busy=false,bool unlocked=true){ImGui::ActivateItemByID(part_id("Apply",PowerSavingFamily::idle_threshold));
                return draw_part(PowerSavingFamily::idle_threshold,busy,unlocked).empty()&&idle_draft.dirty;};
            CHECK(inert(false,false)&&inert(true,true));
            data.recovery_pending=true;CHECK(inert());data.recovery_pending=false;
            data.rows[0].stale=true;CHECK(inert());data.rows[0].stale=false;
            data.rows[0].writable=false;CHECK(inert());data.rows[0].writable=true;
        }
        std::strcpy(idle_draft.text.data(),"20000");idle_draft.dirty=true;  // above maximum_us
        ImGui::ActivateItemByID(part_id("Apply",PowerSavingFamily::idle_threshold));CHECK(draw_part(PowerSavingFamily::idle_threshold).empty());
        idle_draft.dirty=false;
        // difr_live_panel.py: explainer, Read / Hold disable request / Release this request and its _format lines.
        // 1.9.1 says the request is not released automatically; native releases it on close or GPU change.
        PowerSavingRow difr;difr.key={PowerSavingFamily::difr,0};difr.label="DIFR";difr.binding="difr";difr.supported=difr.writable=true;difr.aggregate_enabled=true;difr.reason_b=1;
        difr.availability=VoltagePolicyAvailability::available;difr.request_support=true;difr.control_present=true;data.rows.push_back(difr);
        draw_part(PowerSavingFamily::difr);
        CHECK(has("Druta releases this request when it closes or changes GPU.")&&!has("it is not released automatically")&&has("Hold disable request")&&has("Release this request")
            &&has("DIFR request support: available")&&has("Driver-enabled readback (not residency): yes")&&has("This Druta instance's disable request: not held")
            &&has("A disable request already exists; this instance cannot release it."));
        // difr_live_panel.py _format's second line and the status after a read.
        CHECK(power_saving_readback(data.rows.back()).find("DIFR request support: available\nDisable request control: present\nDriver-enabled readback")!=std::string::npos&&has("DIFR status read.")&&!has("Read DIFR status before"));
        data.rows.back().control_present=false;draw_part(PowerSavingFamily::difr);CHECK(has("Disable request control: unavailable"));
        {   // DIFR's hold is inert while locked, busy or recovering; its release while locked or busy.
            const auto press_difr=[&](const char* label,bool busy=false,bool unlocked=true){ImGui::ActivateItemByID(part_id(label,PowerSavingFamily::difr));return draw_part(PowerSavingFamily::difr,busy,unlocked);};
            CHECK(press_difr("Hold disable request",false,false).empty()&&press_difr("Hold disable request",true,true).empty());
            data.recovery_pending=true;CHECK(press_difr("Hold disable request").empty());data.recovery_pending=false;
            data.rows.back().owned=true;CHECK(press_difr("Release this request",false,false).empty()&&press_difr("Release this request",true,true).empty());
            data.rows.back().owned=false;out=press_difr("Hold disable request");
            CHECK(out.size()==1&&out[0].requests.size()==1&&out[0].requests[0].value==1&&out[0].requests[0].binding=="difr"&&!out[0].lifecycle_release);
        }
        data.rows.pop_back();
        {   // gr_gating_live_panel.py _format: support, ownership, observed controllers, then one line per LPWR controller.
            PowerSavingRow gr;gr.key={PowerSavingFamily::gr_elpg,0};gr.label="Graphics (GR) ELPG";gr.binding="gr";gr.availability=VoltagePolicyAvailability::available;
            gr.supported=gr.writable=gr.owned=true;gr.controllers={{0,true,true,true},{1,true,true,false},{2,true,false,false},{3,false,false,false}};gr.observed={0};data.rows.push_back(gr);
            draw_part(PowerSavingFamily::gr_elpg);
            CHECK(power_saving_readback(data.rows.back())=="Request readback: available\nThis instance's disable request: held\nObserved request changes on LPWR controllers: 0 (partial readback)\n"
                "LPWR controller 0, latest enabled predicate: enabled; request transition observed\n"
                "LPWR controller 1, latest enabled predicate: disabled; no new request transition observed\n"
                "LPWR controller 2: unsupported\nLPWR controller 3: support readback unavailable");
            CHECK(has("LPWR controller 3: support readback unavailable")&&has("Engine-level power-gating (ELPG) status refreshed."));
            // Not yet read on this adapter: the prompt and an empty box, as in Python.
            data.rows.back().binding.clear();data.rows.back().availability=VoltagePolicyAvailability::unknown;draw_part(PowerSavingFamily::gr_elpg);
            CHECK(has("Refresh status before requesting a change.")&&!has("Request readback:")&&!has("LPWR controller"));
            data.rows.pop_back();
        }
        {   // The status line follows the panels: refreshed after a read, the request's own wording
            // once a later readback confirms it, and the row's failure otherwise.
            auto& gating=data.rows[1];gating.owned=false;gating.message.clear();
            ImGui::ActivateItemByID(part_id("Refresh"));draw_part(PowerSavingFamily::individual);data.revision=10;
            draw_part(PowerSavingFamily::individual);CHECK(has("Status refreshed.")&&!has("Refresh status before requesting a change."));
            ImGui::ActivateItemByID(part_id("Hold disabled"));out=draw_part(PowerSavingFamily::individual);CHECK(out.size()==1);
            gating.owned=true;draw_part(PowerSavingFamily::individual);CHECK(has("Status refreshed.")&&!has("held by this instance"));  // not yet published
            data.revision=11;draw_part(PowerSavingFamily::individual);CHECK(has("Disable request held by this instance."));
            ImGui::ActivateItemByID(part_id("Release"));out=draw_part(PowerSavingFamily::individual);CHECK(out.size()==1&&out[0].lifecycle_release);
            gating.owned=false;data.revision=12;draw_part(PowerSavingFamily::individual);CHECK(has("This instance's request was released."));
            ImGui::ActivateItemByID(part_id("Refresh"));draw_part(PowerSavingFamily::individual);data.revision=13;draw_part(PowerSavingFamily::individual);CHECK(has("Status refreshed."));
            // individual_power_gating_panel.py _failed: "<controller> <operation> failed: <reason>. <action>".
            gating.message="Power-saving RM request rejected: 31";gating.availability=VoltagePolicyAvailability::transient;gating.stale=true;draw_part(PowerSavingFamily::individual);
            CHECK(has("GR gating refresh failed: Power-saving RM request rejected: 31. Refresh status.")&&!has("Status refreshed."));
            gating.message.clear();gating.availability=VoltagePolicyAvailability::available;gating.stale=false;
            // An unread controller keeps the prompt and an empty readback.
            PowerSavingRow unread;unread.key={PowerSavingFamily::individual,4};unread.label="Individual power gating: Memory subsystem (MS)";data.rows.push_back(unread);
            draw_part(PowerSavingFamily::individual);CHECK(has("Refresh status before requesting a change.")&&!has("Memory subsystem (MS) request support"));
            data.rows.pop_back();
            // gr_idle_threshold_panel.py names the controller: after a read, and after Apply once the readback is published.
            auto& idle_row=data.rows[0];idle_row.availability=VoltagePolicyAvailability::available;
            draw_part(PowerSavingFamily::idle_threshold);CHECK(has("GR threshold: Requested 4321 \xC2\xB5s; driver readback 2000 \xC2\xB5s."));  // the earlier Apply
            ImGui::ActivateItemByID(part_id("Refresh",PowerSavingFamily::idle_threshold));draw_part(PowerSavingFamily::idle_threshold);data.revision=14;
            draw_part(PowerSavingFamily::idle_threshold);CHECK(has("GR threshold: Status refreshed."));
            auto& draft=*std::find_if(part.drafts.begin(),part.drafts.end(),[](const auto& d){return d.key.family==PowerSavingFamily::idle_threshold;});
            std::strcpy(draft.text.data(),"2500");draft.dirty=true;
            ImGui::ActivateItemByID(part_id("Apply",PowerSavingFamily::idle_threshold));out=draw_part(PowerSavingFamily::idle_threshold);CHECK(out.size()==1&&out[0].requests[0].value==2500);
            idle_row.scaled_first=2501;data.revision=15;draw_part(PowerSavingFamily::idle_threshold);
            CHECK(has("GR threshold: Requested 2500 \xC2\xB5s; driver readback 2501 \xC2\xB5s."));
            // Restore initial and the one-controller save are native extras kept under the shared subsection.
            CHECK(!has("Restore initial")&&!has("Save request profile"));
            idle_row.scaled_first=2000;
            // gr_idle_threshold_panel.py read: an ambiguous scale, an unsupported controller, the
            // thresholds an adaptive request invalidated, and a failed read.
            ImGui::ActivateItemByID(part_id("Refresh",PowerSavingFamily::idle_threshold));draw_part(PowerSavingFamily::idle_threshold);data.revision=20;
            idle_row.scale=0;idle_row.message="Idle scale is ambiguous; inspection only";draw_part(PowerSavingFamily::idle_threshold);
            CHECK(has("GR threshold: Current scale could not be determined; Apply is unavailable.")&&!has("Idle scale is ambiguous"));
            idle_row.scale=1000;idle_row.message.clear();idle_row.supported=false;idle_row.control_present=false;draw_part(PowerSavingFamily::idle_threshold);
            CHECK(has("GR threshold: unsupported")&&power_saving_readback(idle_row)=="Unavailable: unsupported");
            idle_row.control_present.reset();CHECK(power_saving_readback(idle_row)=="Unavailable: unavailable");
            idle_row.supported=true;idle_row.stale=true;idle_row.message="Adaptive request may change idle thresholds; Read again";draw_part(PowerSavingFamily::idle_threshold);
            CHECK(has("Refresh status before applying.")&&power_saving_readback(idle_row)=="Readback may have changed; refresh required.");
            idle_row.availability=VoltagePolicyAvailability::transient;idle_row.message="Bounded read exhausted";draw_part(PowerSavingFamily::idle_threshold);
            CHECK(has("GR threshold idle threshold refresh failed: Bounded read exhausted. Refresh status before applying."));
            {auto unread=idle_row;unread.binding.clear();unread.stale=false;CHECK(power_saving_readback(unread).empty());  // a failed first read leaves the box empty
                unread.availability=VoltagePolicyAvailability::unsupported;unread.message="Native idle/adaptive dispatch layout unavailable";
                CHECK(power_saving_readback(unread)=="Unavailable: Native idle/adaptive dispatch layout unavailable");}
            idle_row.availability=VoltagePolicyAvailability::available;idle_row.stale=false;idle_row.message.clear();
            // A failed hold: the earlier action names the failure until the hold's own result is published.
            auto& held_row=data.rows[1];  // the rows were resized above; take the row again
            held_row.owned=false;ImGui::ActivateItemByID(part_id("Hold disabled"));out=draw_part(PowerSavingFamily::individual);CHECK(out.size()==1);
            held_row.availability=VoltagePolicyAvailability::transient;held_row.stale=true;held_row.message="Owned individual witness disappeared";draw_part(PowerSavingFamily::individual);
            CHECK(has("GR gating refresh failed: Owned individual witness disappeared. Refresh status."));
            data.revision=21;draw_part(PowerSavingFamily::individual);
            CHECK(has("GR gating hold failed: Owned individual witness disappeared. Refresh status; release may be needed or already attempted."));
            // A dispatched hold whose outcome is uncertain reports the service's account of it; a later read reports the read.
            held_row.availability=VoltagePolicyAvailability::available;held_row.stale=false;held_row.message.clear();
            ImGui::ActivateItemByID(part_id("Hold disabled"));out=draw_part(PowerSavingFamily::individual);CHECK(out.size()==1);
            held_row.uncertain=true;held_row.message="Uncertain request; explicit recovery only";data.message="Power-saving escape completion failed";data.revision=22;
            draw_part(PowerSavingFamily::individual);data.message.clear();draw_part(PowerSavingFamily::individual);
            CHECK(has("GR gating hold failed: Power-saving escape completion failed. Refresh status; release may be needed or already attempted."));
            ImGui::ActivateItemByID(part_id("Refresh"));draw_part(PowerSavingFamily::individual);data.revision=23;draw_part(PowerSavingFamily::individual);
            CHECK(has("Status refreshed.")&&!has("hold failed")&&!has("Uncertain request"));
            held_row.uncertain=false;held_row.message.clear();
        }
        {   // Python's single status line: the service's account of the last operation is not printed
            // under it, and the host's queued note goes once the action's result is published.
            data.message="Power-saving inspection completed; each controller reports independent status";
            ImGui::ActivateItemByID(part_id("Refresh"));CHECK(draw_part(PowerSavingFamily::individual).size()==1);
            part.status="Operation queued; see operation log.";draw_part(PowerSavingFamily::individual);
            CHECK(has("Operation queued")&&!has("Power-saving inspection completed"));
            ++data.revision;draw_part(PowerSavingFamily::individual);
            CHECK(has("Status refreshed.")&&!has("Operation queued")&&!has("Power-saving inspection completed")&&part.status.empty());
            // A new action drops the note about the previous one.
            part.status="Operation queued; see operation log.";ImGui::ActivateItemByID(part_id("Refresh"));
            CHECK(draw_part(PowerSavingFamily::individual).size()==1&&part.status.empty());
            ++data.revision;draw_part(PowerSavingFamily::individual);
            // Focused keeps the service's account and the host's note.
            PowerSavingViewState focused;sync_power_saving_view(focused,&data,2);focused.status="Operation queued; see operation log.";
            ImGui::GetIO().DisplaySize={1700,1600};ImGui::NewFrame();ImGui::SetNextWindowSize({1600,1500});ImGui::Begin("Power-saving focused");ImGui::LogToBuffer();
            draw_power_saving_controls(focused,&data,2,false,true,false);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();
            CHECK(has("Power-saving inspection completed")&&has("Operation queued"));
            data.message.clear();
        }
        {   // The other panels' _failed wording, from the family's last operation.
            using Kind=PowerSavingLastAction::Kind;using A=VoltagePolicyAvailability;
            PowerSavingData d;d.revision=2;PowerSavingViewState st;
            const auto failing=[&](PowerSavingFamily family,const char* label,Kind kind,bool owned=false){PowerSavingRow r;r.key={family,0};r.label=label;r.binding="b";r.stale=true;
                r.availability=A::transient;r.message="Bounded read exhausted";r.owned=owned;st.last={{r.key,kind,0,{},1,{},{}}};return power_saving_status(st,&d,&r,family);};
            CHECK(failing(PowerSavingFamily::adaptive,"Adaptive power: NVDEC",Kind::read)=="Adaptive power refresh failed: Bounded read exhausted. Refresh status before requesting a change.");
            CHECK(failing(PowerSavingFamily::adaptive,"Adaptive power: NVDEC",Kind::set)=="Adaptive power request failed: Bounded read exhausted. Refresh status before requesting a change.");
            CHECK(failing(PowerSavingFamily::idle_threshold,"Idle threshold: NVDEC",Kind::set)=="NVDEC idle threshold apply failed: Bounded read exhausted. Refresh status before applying.");
            CHECK(failing(PowerSavingFamily::difr,"DIFR",Kind::read)=="DIFR read failed: Bounded read exhausted. Read current status before taking another action.");
            CHECK(failing(PowerSavingFamily::difr,"DIFR",Kind::release)=="DIFR release failed: Bounded read exhausted. Read current status before taking another action.");
            CHECK(failing(PowerSavingFamily::gr_elpg,"Graphics (GR) ELPG",Kind::read)=="Engine-level power gating (ELPG) refresh failed: Bounded read exhausted. Refresh status.");
            // GR ELPG suggests Release only while this view's Release is enabled for the row.
            CHECK(failing(PowerSavingFamily::gr_elpg,"Graphics (GR) ELPG",Kind::release,true)=="Engine-level power gating (ELPG) release failed: Bounded read exhausted. Use Release or refresh status.");
            CHECK(failing(PowerSavingFamily::gr_elpg,"Graphics (GR) ELPG",Kind::hold)=="Engine-level power gating (ELPG) hold failed: Bounded read exhausted. Refresh status.");
            // An unsupported idle or adaptive layout is a returned status, not a failure; without an operation of
            // this view's own (Undo, Recover, Restore initial), a failure shows its reason alone.
            PowerSavingRow r;r.key={PowerSavingFamily::adaptive,0};r.availability=A::unsupported;r.message="Native idle/adaptive dispatch layout unavailable";
            CHECK(power_saving_status(st,&d,&r,PowerSavingFamily::adaptive)=="Native idle/adaptive dispatch layout unavailable");
            {   // individual_power_gating_live.py returns an unsupported layout as no support: "Status refreshed.".
                PowerSavingRow ms;ms.key={PowerSavingFamily::individual,4};ms.label="Individual power gating: Memory subsystem (MS)";
                ms.availability=A::unsupported;ms.message="MS gating dispatch class is not qualified";st.last={{ms.key,Kind::read,0,{},1,{},{}}};
                CHECK(power_saving_status(st,&d,&ms,PowerSavingFamily::individual)=="Status refreshed.");
                CHECK(power_saving_readback(ms)=="Memory subsystem (MS) request support: unavailable\nThis instance's disable request: not held\nMS gating dispatch class is not qualified");
                ms.availability=A::transient;ms.message="Bounded read exhausted";  // a failed read is still a failure
                CHECK(power_saving_status(st,&d,&ms,PowerSavingFamily::individual)=="Memory subsystem (MS) refresh failed: Bounded read exhausted. Refresh status.");}
            st.last.clear();r.key={PowerSavingFamily::gr_elpg,0};r.availability=A::transient;
            CHECK(power_saving_status(st,&d,&r,PowerSavingFamily::gr_elpg)=="Native idle/adaptive dispatch layout unavailable");
            // gr_gating_live_panel.py: an uncertain outcome is stated; Release is not offered for it here.
            r.availability=A::available;r.binding="gr";r.supported=r.uncertain=true;
            CHECK(power_saving_readback(r)=="Request readback: available\nThis instance's request outcome is uncertain.");
            // difr_live_panel.py always prints the driver-enabled line; unread here, it says unknown.
            PowerSavingRow difr;difr.key={PowerSavingFamily::difr,0};difr.binding="difr";difr.availability=A::available;difr.request_support=false;difr.control_present=true;
            CHECK(power_saving_readback(difr)=="DIFR request support: unavailable\nDisable request control: present\nDriver-enabled readback (not residency): unknown\nThis Druta instance's disable request: not held");
        }
        {   // A request staged under its family is inert while locked, busy or recovering.
            part.staged={{{PowerSavingFamily::individual,0},1u,"other-route",std::nullopt}};
            const auto press_staged=[&](bool busy=false,bool unlocked=true){ImGui::ActivateItemByID(part_id("Apply staged request",PowerSavingFamily::individual,0));
                return draw_part(PowerSavingFamily::individual,busy,unlocked);};
            CHECK(press_staged(false,false).empty()&&press_staged(true,true).empty());
            data.recovery_pending=true;CHECK(press_staged().empty());data.recovery_pending=false;
            out=press_staged();CHECK(out.size()==1&&out[0].requests.size()==1&&out[0].requests[0].key.family==PowerSavingFamily::individual&&out[0].requests[0].value==1);
            part.staged.clear();
        }
        // Release owned, Undo, Recover and Load act on every family, so a family heading never carries them.
        data.owned_requests=data.undo_available=data.recovery_pending=true;draw_part(PowerSavingFamily::individual);
        for(const char* label:{"Release owned power-saving requests","Undo power-saving request","Recover power-saving request","Load power-saving profile","Discard staged power-saving profile"})CHECK(text.find(label)==std::string::npos);
        CHECK(text.find("Recover it under Undo, recovery and profiles")!=std::string::npos);
        const auto draw_shared=[&](const char* activate){ImGui::GetIO().DisplaySize={1700,1600};ImGui::NewFrame();ImGui::SetNextWindowSize({1600,1500});ImGui::Begin("Power-saving shared");
            if(activate){ImGui::PushID("power-saving");ImGui::PushID(200);auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;ImGui::PopID();ImGui::PopID();}
            ImGui::LogToBuffer();auto result=draw_power_saving_shared_controls(part,&data,2,false,true);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
        draw_shared(nullptr);
        // No family's own controls; its rows appear only as names in the folded saved selection
        // (logging expands tree nodes, so the folded entries are in this text).
        CHECK(text.find("Release owned power-saving requests")!=std::string::npos&&text.find("Read power saving")==std::string::npos&&text.find("Hold disabled")==std::string::npos
            &&text.find("Save request profile")==std::string::npos&&text.find("Save a selection of requests")<text.find("GR gating"));
        // The shared notes are plain text, not a further collapsed header.
        CHECK(has("Initial means the first qualified request")&&!has("Power-saving details"));
        CHECK(!part.last.empty());out=draw_shared("Release owned power-saving requests");
        // Each family's status line then reports a failure by its reason, not as one of its own operations.
        CHECK(out.size()==1&&out[0].operation==PowerSavingIntent::Operation::release_owned&&part.last.empty());
        // A profile spanning several controllers or families is saved from the folded selection.
        const auto draw_tree=[&](const char* activate){ImGui::GetIO().DisplaySize={1700,1600};ImGui::NewFrame();ImGui::SetNextWindowSize({1600,1500});ImGui::Begin("Power-saving shared");
            ImGui::PushID("power-saving");ImGui::PushID(200);const auto tree=ImGui::GetID("Save a selection of requests");ImGui::GetStateStorage()->SetInt(tree,1);
            if(activate){ImGui::PushOverrideID(tree);auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;ImGui::PopID();}
            ImGui::PopID();ImGui::PopID();
            ImGui::LogToBuffer();auto result=draw_power_saving_shared_controls(part,&data,2,false,true);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
        for(auto& d:part.drafts)d.include=false;
        CHECK(draw_tree("Save power-saving profile").empty()&&has("GR gating")&&has("GR threshold"));
        for(auto& d:part.drafts)d.include=true;out=draw_tree("Save power-saving profile");
        CHECK(out.size()==1&&out[0].operation==PowerSavingIntent::Operation::save_profile&&out[0].selection.size()==2
            &&out[0].selection[0].family==PowerSavingFamily::idle_threshold&&out[0].selection[1].family==PowerSavingFamily::individual);
        data.owned_requests=data.undo_available=data.recovery_pending=false;
        draw_shared(nullptr);CHECK(has("Restore initial requests")&&has("Restore initial")&&text.find("Save a selection of requests")<text.find("Restore initial requests"));
        {   // Restore initial, relocated from the family rows, sends the same request from the shared subsection.
            ImGui::GetIO().DisplaySize={1700,1600};ImGui::NewFrame();ImGui::SetNextWindowSize({1600,1500});ImGui::Begin("Power-saving shared");
            ImGui::PushID("power-saving");ImGui::PushID(200);const auto tree=ImGui::GetID("Restore initial requests");ImGui::GetStateStorage()->SetInt(tree,1);
            ImGui::PushOverrideID(tree);ImGui::PushID(static_cast<int>(PowerSavingFamily::individual));ImGui::PushID(0);
            auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetID("Restore initial");c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;
            ImGui::PopID();ImGui::PopID();ImGui::PopID();ImGui::PopID();ImGui::PopID();
            out=draw_power_saving_shared_controls(part,&data,2,false,true);ImGui::End();ImGui::Render();
            CHECK(out.size()==1&&out[0].operation==PowerSavingIntent::Operation::restore_initial&&out[0].selection.size()==1
                &&out[0].selection[0].family==PowerSavingFamily::individual&&out[0].selection[0].index==0);
        }
    }
    ImGui::DestroyContext();std::printf("Power-saving view: %d checks\n",checks);return 0;}
