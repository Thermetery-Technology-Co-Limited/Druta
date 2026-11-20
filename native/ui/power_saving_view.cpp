// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_saving_view.h"
#include "classic_layout.h"
#include "classic_section.h"
#include <imgui.h>
#include <algorithm>
#include <cfloat>
#include <string>
#include <cstdio>
namespace druta::ui {
std::optional<uint32_t> parse_power_saving_us(const std::string& text){const auto begin=text.find_first_not_of(" \t\r\n"),end=text.find_last_not_of(" \t\r\n");if(begin==std::string::npos)return {};uint64_t value=0;for(size_t n=begin;n<=end;++n){if(text[n]<'0'||text[n]>'9')return {};value=value*10+static_cast<uint32_t>(text[n]-'0');if(value>UINT32_MAX)return {};}return value?std::optional<uint32_t>(static_cast<uint32_t>(value)):std::nullopt;}
void sync_power_saving_view(PowerSavingViewState& state,const PowerSavingData* data,uint64_t generation){if(!state.initialized||state.generation!=generation){state={};state.initialized=true;state.generation=generation;}if(!data)return;for(const auto& row:data->rows){auto it=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;});if(it==state.drafts.end()){state.drafts.push_back({});it=state.drafts.end()-1;it->key=row.key;}if(!it->dirty&&!row.stale){it->binding=row.binding;it->expected=row.value;if(row.value)std::snprintf(it->text.data(),it->text.size(),"%u",*row.value);}}}
bool stage_power_saving_document(PowerSavingViewState& state,const ProfileJson& document,const PowerSavingData& data){try{state.staged=stage_power_saving_profile(document,data);state.status="Profile staged; review and explicitly Apply";return true;}catch(const std::exception& e){state.status=e.what();return false;}}
namespace {
enum class Scope {all,family,shared};
// One undo stack, one recovery and every owned hold span all families, so classic
// draws those actions once in a shared subsection, never under one family heading.
std::vector<PowerSavingIntent> draw_power_saving(PowerSavingViewState& state,const PowerSavingData* data,uint64_t generation,bool busy,bool unlocked,Scope scope,std::optional<PowerSavingFamily> only){sync_power_saving_view(state,data,generation);std::vector<PowerSavingIntent> out;ImGui::PushID("power-saving");if(scope==Scope::family)ImGui::PushID(100+static_cast<int>(*only));else if(scope==Scope::shared)ImGui::PushID(200);
    const bool all=scope==Scope::all,in_family=scope==Scope::family,shared=scope==Scope::shared;
    const auto shown=[&](PowerSavingFamily f){return !in_family||*only==f;};
    if(shared)ImGui::TextWrapped("These act on this session's requests in every power-saving family.");
    if(!shared){ImGui::BeginDisabled(busy);if(ImGui::Button("Read power saving"))out.push_back({PowerSavingIntent::Operation::read,in_family?1u<<static_cast<uint32_t>(*only):31u,{},{}});ImGui::EndDisabled();}
    const char* headings[]={"GR ELPG","Individual power gating","Idle thresholds (us)","Adaptive power","DIFR"};
    if(!shared)for(int family=0;family<5;++family){if(!shown(PowerSavingFamily(family)))continue;ImGui::PushID(family);if((in_family||ImGui::CollapsingHeader(headings[family],ImGuiTreeNodeFlags_DefaultOpen))&&data){for(const auto& row:data->rows){if(static_cast<int>(row.key.family)!=family)continue;auto draft=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;});if(draft==state.drafts.end())continue;ImGui::PushID(static_cast<int>(row.key.index));ImGui::TextWrapped("%s%s",row.label.c_str(),row.stale?" [stale]":"");
        if(row.key.family==PowerSavingFamily::idle_threshold){if(!row.binding.empty())ImGui::Text("Raw %u / %u; applied %u / %u us",row.raw_first,row.raw_second,row.scaled_first,row.scaled_second);else ImGui::TextDisabled("Threshold readings unavailable");ImGui::SetNextItemWidth(140);if(ImGui::InputText("Request us",draft->text.data(),draft->text.size()))draft->dirty=true;const auto value=parse_power_saving_us(draft->text.data());ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!row.writable||row.stale||!value||!data||data->recovery_pending||(value&&*value>row.maximum_us));if(ImGui::Button("Apply")){out.push_back({PowerSavingIntent::Operation::apply,31,{{row.key,*value,draft->binding,draft->expected}}, {}});draft->dirty=false;}ImGui::EndDisabled();}
        else {if(row.aggregate_enabled)ImGui::Text("Aggregate: %s",*row.aggregate_enabled?"enabled":"disabled");if(row.key.family==PowerSavingFamily::adaptive)ImGui::Text("This app: %s",row.value?(*row.value?"Allow":"Disable"):"unknown");else ImGui::Text("This app: %s",row.uncertain?"uncertain":row.owned?"holds disable":"released");const bool adaptive=row.key.family==PowerSavingFamily::adaptive;ImGui::BeginDisabled(busy||!unlocked||(!adaptive&&(!row.writable||row.stale||data->recovery_pending))||(adaptive&&!row.supported));if(ImGui::Button(adaptive?"Disable":"Hold disable"))out.push_back({PowerSavingIntent::Operation::apply,31,{{row.key,adaptive?0u:1u,row.binding,row.value}}, {}});ImGui::EndDisabled();ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||(adaptive?!row.supported:(!row.owned||row.uncertain)));if(ImGui::Button(adaptive?"Allow":"Release"))out.push_back({PowerSavingIntent::Operation::apply,31,{{row.key,adaptive?1u:0u,row.binding,row.value}}, {},!adaptive});ImGui::EndDisabled();}
        ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!row.initial||row.stale||!row.writable||data->recovery_pending);if(ImGui::Button("Restore initial"))out.push_back({PowerSavingIntent::Operation::restore_initial,31,{}, {row.key}});ImGui::EndDisabled();ImGui::SameLine();ImGui::Checkbox("Profile",&draft->include);if(!row.message.empty())ImGui::TextWrapped("%s",row.message.c_str());ImGui::PopID();}}ImGui::PopID();}
    if(in_family&&data&&data->recovery_pending)ImGui::TextWrapped("A power-saving request needs recovery. Recover it under Undo, recovery and profiles at the end of this section.");
    if(!in_family){
        ImGui::BeginDisabled(busy||!unlocked||!data||!data->owned_requests);if(ImGui::Button("Release owned power-saving requests"))out.push_back({PowerSavingIntent::Operation::release_owned,31,{},{}});ImGui::EndDisabled();
        ImGui::BeginDisabled(busy||!unlocked||!data||!data->undo_available);if(ImGui::Button("Undo power-saving request"))out.push_back({PowerSavingIntent::Operation::undo,31,{},{}});ImGui::EndDisabled();ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!data||!data->recovery_pending);if(ImGui::Button("Recover power-saving request"))out.push_back({PowerSavingIntent::Operation::recover,31,{},{}});ImGui::EndDisabled();
    }
    if(!shared){std::vector<PowerSavingKey> selection;for(const auto& d:state.drafts)if(d.include&&shown(d.key.family))selection.push_back(d.key);ImGui::BeginDisabled(busy||!data||selection.empty());if(ImGui::Button("Save power-saving profile"))out.push_back({PowerSavingIntent::Operation::save_profile,31,{},selection});ImGui::EndDisabled();}
    if(all)ImGui::SameLine();
    if(!in_family){ImGui::BeginDisabled(busy);if(ImGui::Button("Load power-saving profile"))out.push_back({PowerSavingIntent::Operation::load_profile,31,{},{}});ImGui::EndDisabled();}
    if(!shared&&std::any_of(state.staged.begin(),state.staged.end(),[&](const auto& r){return shown(r.key.family);})){ImGui::TextDisabled("Staged requests apply individually; Undo restores the previous edit");for(size_t n=0;n<state.staged.size();++n){const auto& r=state.staged[n];if(!shown(r.key.family))continue;ImGui::PushID(static_cast<int>(n));ImGui::Text("Staged family %u / controller %u: %u",static_cast<uint32_t>(r.key.family),r.key.index,r.value);ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!data||(data->recovery_pending&&r.key.family!=PowerSavingFamily::adaptive));if(ImGui::Button("Apply staged request"))out.push_back({PowerSavingIntent::Operation::apply,31,{r},{}});ImGui::EndDisabled();ImGui::PopID();}if(all&&ImGui::Button("Discard staged power-saving profile"))state.staged.clear();}
    if(shared&&!state.staged.empty()){ImGui::Text("%u staged requests; apply each under its family.",unsigned(state.staged.size()));if(ImGui::Button("Discard staged power-saving profile"))state.staged.clear();}
    if(data&&!data->message.empty())ImGui::TextWrapped("%s",data->message.c_str());if(!state.status.empty())ImGui::TextWrapped("%s",state.status.c_str());    // Classic's shared subsection is native-only already, so its notes are plain text there,
    // not another collapsed header; a saved selection spanning families is folded beneath.
    constexpr const char* details="Requests last only this session. Initial means the first qualified request, never factory defaults. Aggregate enabled state is not request ownership or physical residency. Adaptive Allow does not restore idle thresholds. Counted operations with uncertain outcomes cannot be blindly repeated. Profiles contain explicit values and load as drafts; nothing is reapplied at startup or after driver reset.";
    if(shared){ImGui::TextWrapped("%s",details);
        if(ImGui::TreeNode("Save a selection of requests")){
            std::vector<PowerSavingKey> selection;
            if(data)for(const auto& row:data->rows){auto draft=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;});if(draft==state.drafts.end())continue;
                ImGui::PushID(static_cast<int>(row.key.family));ImGui::PushID(static_cast<int>(row.key.index));ImGui::Checkbox(row.label.c_str(),&draft->include);ImGui::PopID();ImGui::PopID();
                if(draft->include)selection.push_back(row.key);}
            ImGui::BeginDisabled(busy||!data||selection.empty());if(ImGui::Button("Save power-saving profile"))out.push_back({PowerSavingIntent::Operation::save_profile,31,{},selection});ImGui::EndDisabled();
            ImGui::TreePop();}
        // Native Restore initial for each controller; the released family panels have none.
        if(ImGui::TreeNode("Restore initial requests")){
            if(data)for(const auto& row:data->rows){if(std::none_of(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;}))continue;
                ImGui::PushID(static_cast<int>(row.key.family));ImGui::PushID(static_cast<int>(row.key.index));
                ImGui::AlignTextToFramePadding();ImGui::TextUnformatted(row.label.c_str());ImGui::SameLine();
                ImGui::BeginDisabled(busy||!unlocked||!row.initial||row.stale||!row.writable||data->recovery_pending);
                if(ImGui::Button("Restore initial"))out.push_back({PowerSavingIntent::Operation::restore_initial,31,{},{row.key}});ImGui::EndDisabled();
                ImGui::PopID();ImGui::PopID();}
            ImGui::TreePop();}}
    else if(all&&ImGui::CollapsingHeader("Power-saving details"))ImGui::TextWrapped("%s",details);if(!all)ImGui::PopID();ImGui::PopID();
    // A family's status line names only the operations its own buttons sent; after
    // these, a failure is reported by its reason alone.
    for(const auto& intent:out){using O=PowerSavingIntent::Operation;
        if(intent.operation==O::release_owned||intent.operation==O::undo||intent.operation==O::recover)state.last.clear();
        else if(intent.operation==O::restore_initial)for(const auto& key:intent.selection)state.last.erase(std::remove_if(state.last.begin(),state.last.end(),[&](const auto& a){return a.key==key;}),state.last.end());}
    return out;}
}
namespace {
void readback_box(const std::string& text,float height){
    ImGui::InputTextMultiline("##readback",const_cast<char*>(text.c_str()),text.size()+1,ImVec2(-FLT_MIN,classic_px(height)),ImGuiInputTextFlags_ReadOnly);}
// "Individual power gating: NVDEC" -> "NVDEC": the subsection already names the family.
std::string controller_name(const std::string& label){const auto at=label.find(": ");return at==std::string::npos?label:label.substr(at+2);}
bool read_ok(const PowerSavingRow& row){return !row.binding.empty()&&row.availability==VoltagePolicyAvailability::available&&!row.stale;}
const PowerSavingLastAction* last_action(const PowerSavingViewState& state,const PowerSavingKey& key){
    const auto it=std::find_if(state.last.begin(),state.last.end(),[&](const auto& a){return a.key==key;});return it==state.last.end()?nullptr:&*it;}
// Each action also drops the host's note about the previous one.
void remember(PowerSavingViewState& state,const PowerSavingData* data,const PowerSavingKey& key,PowerSavingLastAction::Kind kind,uint32_t value=0,std::optional<bool> before={}){
    PowerSavingLastAction action{key,kind,value,before,data?data->revision:0,{},{}};
    const auto it=std::find_if(state.last.begin(),state.last.end(),[&](const auto& a){return a.key==key;});
    if(it==state.last.end())state.last.push_back(action);else{action.prior=it->kind;*it=action;}
    state.status.clear();}
// The first revision published after an action carries that action's result, which the
// status line reports in place of the host's "queued" note.
void settle(PowerSavingViewState& state,const PowerSavingData* data){
    if(data)for(auto& a:state.last)if(!a.outcome&&data->revision!=a.revision){a.outcome=data->message;state.status.clear();}}
}
// The panels' _format lines (gr_gating_live_panel.py, individual_power_gating_panel.py,
// gr_idle_threshold_panel.py, adaptive_power_panel.py, difr_live_panel.py), from the
// fields this service reads. The box stays empty until a read returns this row, as
// Python's does; a last-good stale note is native and comes after the released lines.
std::string power_saving_readback(const PowerSavingRow& row){
    const auto name=controller_name(row.label);
    const auto unavailable=[&](const char* fallback){return "Unavailable: "+(row.message.empty()?std::string(fallback):row.message);};
    const char* support=row.supported?"available":"unavailable";
    const std::string held=std::string("This instance's disable request: ")+(row.owned?"held":"not held");
    const bool attempted=!row.binding.empty()||!row.message.empty()||row.availability!=VoltagePolicyAvailability::unknown;
    if(!attempted)return {};
    std::string text;
    switch(row.key.family){
    case PowerSavingFamily::idle_threshold:{
        // Python's box shows "Unavailable: <status>" only for a snapshot the read returned
        // (an unsupported layout or controller); a failed read leaves it as it was.
        if(row.binding.empty()){if(row.availability!=VoltagePolicyAvailability::unsupported)return {};text=unavailable("No verified threshold readback.");break;}
        // An adaptive request marks the thresholds stale without a failed read (invalidate_related).
        if(row.stale&&row.availability==VoltagePolicyAvailability::available)return "Readback may have changed; refresh required.";
        if(!row.supported){text=std::string("Unavailable: ")+(row.control_present?"unsupported":"unavailable");break;}
        const auto known=[](uint32_t v){return v?std::to_string(v):std::string("unknown");};
        text=name+" driver readback: "+std::to_string(row.scaled_first)+" \xC2\xB5s; scaled pair: ["+std::to_string(row.scaled_first)+", "+std::to_string(row.scaled_second)
            +"]; raw pair: ["+std::to_string(row.raw_first)+", "+std::to_string(row.raw_second)+"]; scale: "+known(row.scale)
            +"; allowed: 1\xE2\x80\x93"+known(row.maximum_us)+" \xC2\xB5s";break;}
    case PowerSavingFamily::adaptive:
        if(row.binding.empty()){if(row.availability!=VoltagePolicyAvailability::unsupported)return {};text=unavailable("No verified readback.");break;}
        text=name+" aggregate adaptive-power policy: "+(row.aggregate_enabled?(*row.aggregate_enabled?"enabled":"disabled"):"unknown")+".";break;
    case PowerSavingFamily::gr_elpg:{
        if(row.binding.empty())return {};
        // Python's uncertain line ends "; use Release". Release stays disabled here for an
        // uncertain request (the recovery note below the box says where it is resolved).
        text=std::string("Request readback: ")+support+"\n"+(row.uncertain?"This instance's request outcome is uncertain.":held);
        const auto observed=[&](uint32_t n){return std::find(row.observed.begin(),row.observed.end(),n)!=row.observed.end();};
        if(!row.observed.empty()){std::string list;for(auto n:row.observed)list+=(list.empty()?"":", ")+std::to_string(n);
            text+="\nObserved request changes on LPWR controllers: "+list+" (partial readback)";}
        for(const auto& c:row.controllers){const auto head="\nLPWR controller "+std::to_string(c.index);
            if(!c.available){text+=head+": support readback unavailable";continue;}
            if(!c.supported){text+=head+": unsupported";continue;}
            text+=head+", latest enabled predicate: "+(c.enabled?"enabled":"disabled");
            if(row.owned&&!row.uncertain)text+=observed(c.index)?"; request transition observed":"; no new request transition observed";}
        break;}
    case PowerSavingFamily::individual:{
        // An unsupported controller still reports its (absent) support, as Python's read does.
        if(row.binding.empty()&&row.availability!=VoltagePolicyAvailability::unsupported)return {};
        text=name+" request support: "+support+"\n"+(row.uncertain?"This instance's request outcome is uncertain; refresh status. Release may already have been attempted.":held);
        if(row.supported){char masks[96];std::snprintf(masks,sizeof(masks),"Reason masks: 0x48=0x%08X, 0x49=0x%08X",row.reason_a,row.reason_b);
            text+=std::string("\nReason-zero inhibit readback: ")+((row.reason_a&1)?"present":"not present")+" (may include other owners)\n"+masks;}
        // Native: why this read found no request support (the status line says only "Status refreshed.").
        if(row.binding.empty()&&!row.message.empty())text+="\n"+row.message;
        break;}
    case PowerSavingFamily::difr:
        if(row.binding.empty())return {};
        text=std::string("DIFR request support: ")+(row.request_support?(*row.request_support?"available":"unavailable"):support);
        text+=std::string("\nDisable request control: ")+(row.control_present.value_or(false)?"present":"unavailable");
        // Python reads the enabled predicate even without request support; this read takes it
        // only for a supported controller, so the line says unknown otherwise.
        text+=std::string("\nDriver-enabled readback (not residency): ")+(row.aggregate_enabled?(*row.aggregate_enabled?"yes":"no"):"unknown");
        text+=row.uncertain?"\nThis Druta instance's request outcome is uncertain.\nDo not send another Hold or Release request."
            :"\nThis Druta instance's disable request: "+std::string(row.owned?"held":"not held");
        if(!row.uncertain&&!row.owned&&((row.reason_a|row.reason_b)&1))text+="\nA disable request already exists; this instance cannot release it.";
        break;
    }
    if(row.stale)text+="\nLast successful read; stale.";
    return text;
}
std::string power_saving_status(const PowerSavingViewState& state,const PowerSavingData* data,const PowerSavingRow* row,PowerSavingFamily family){
    using Kind=PowerSavingLastAction::Kind;using A=VoltagePolicyAvailability;
    const bool difr=family==PowerSavingFamily::difr,idle=family==PowerSavingFamily::idle_threshold,adaptive=family==PowerSavingFamily::adaptive;
    const std::string prompt=difr?"Read DIFR status before requesting a change.":"Refresh status before requesting a change.";
    if(!row)return prompt;
    // gr_idle_threshold_panel.py _show names the controller in every status it sets.
    const auto name=controller_name(row->label);
    const auto named=[&](const std::string& text){return idle?name+": "+text:text;};
    const auto* last=last_action(state,row->key);
    // Until a later readback is published, the line still describes the previous one.
    const bool published=last&&data&&data->revision!=last->revision;
    const std::optional<Kind> op=last?(published?std::optional<Kind>(last->kind):last->prior):std::nullopt;
    // The panels' _failed: "<family> <operation> failed: <reason>. <action>". GR ELPG
    // suggests Release only while this view's Release is enabled for the row.
    const auto failed=[&](Kind kind,const std::string& reason){
        const bool refresh=kind==Kind::read;
        const std::string operation=refresh?(difr?"read":"refresh"):kind==Kind::hold?"hold":kind==Kind::release?"release":adaptive?"request":"apply";
        switch(family){
        case PowerSavingFamily::gr_elpg:return "Engine-level power gating (ELPG) "+operation+" failed: "+reason+". "
            +(refresh||!row->owned||row->uncertain?"Refresh status.":"Use Release or refresh status.");
        case PowerSavingFamily::individual:return name+" "+operation+" failed: "+reason+". "
            +(refresh?"Refresh status.":"Refresh status; release may be needed or already attempted.");
        case PowerSavingFamily::idle_threshold:return name+" idle threshold "+operation+" failed: "+reason+". Refresh status before applying.";
        case PowerSavingFamily::adaptive:return "Adaptive power "+operation+" failed: "+reason+". Refresh status before requesting a change.";
        case PowerSavingFamily::difr:break;}
        return "DIFR "+operation+" failed: "+reason+". Read current status before taking another action.";};
    if(row->availability!=A::available&&!row->message.empty()){
        // An unsupported layout is a snapshot the idle, adaptive and individual reads return
        // (gr_idle_threshold_live.py, adaptive_power_live.py, individual_power_gating_live.py),
        // not a failure: idle and adaptive show its status, individual "Status refreshed.".
        if((idle||adaptive)&&row->availability==A::unsupported)return named(row->message);
        if(family==PowerSavingFamily::individual&&row->availability==A::unsupported&&!row->stale)return "Status refreshed.";
        // Without an operation this view sent (Undo, Recover or Restore initial), the reason alone.
        return op?failed(*op,row->message):named(row->message);}
    // A request whose outcome is uncertain: its failure, in the service's own account.
    if(published&&last->kind!=Kind::read&&row->uncertain&&last->outcome&&!last->outcome->empty())return failed(last->kind,*last->outcome);
    // An adaptive request marks the thresholds stale (gr_idle_threshold_panel.py invalidate_related).
    if(idle&&row->stale&&row->availability==A::available)return "Refresh status before applying.";
    if(!read_ok(*row))return prompt;
    const std::string refreshed=family==PowerSavingFamily::gr_elpg?"Engine-level power-gating (ELPG) status refreshed.":difr?"DIFR status read."
        :!idle?"Status refreshed.":!row->supported?named(row->control_present?"unsupported":"unavailable")
        :!row->scale?named("Current scale could not be determined; Apply is unavailable."):named("Status refreshed.");
    if(!last||last->kind==Kind::read||!published)return refreshed;
    switch(last->kind){
    case Kind::hold:if(!row->owned)break;return difr?"Counted disable request held by this Druta instance.":"Disable request held by this instance.";
    case Kind::release:if(row->owned)break;
        return difr?"This Druta instance's disable request was released.":family==PowerSavingFamily::individual?"This instance's request was released.":"This instance's disable request was released.";
    case Kind::set:
        if(idle)return named("Requested "+std::to_string(last->value)+" \xC2\xB5s; driver readback "+std::to_string(row->scaled_first)+" \xC2\xB5s.");
        if(adaptive){const char* now=row->aggregate_enabled?(*row->aggregate_enabled?"enabled":"disabled"):"unknown";
            const bool transition=row->aggregate_enabled&&last->aggregate_before!=row->aggregate_enabled&&*row->aggregate_enabled==(last->value!=0);
            return std::string("Request accepted; aggregate policy is ")+now+"."+(transition?"":" Other reasons may affect the aggregate state; the request effect is not independently verified.");}
        break;
    case Kind::read:break;
    }
    return refreshed;
}
namespace {
constexpr const char* saved_profile_note="A saved profile describes only this instance's request. Load it manually to apply; "
    "requests are not replayed after a driver restart.";
// Released 1.9.1 Live power-saving panels (gr_gating_live_panel.py,
// individual_power_gating_panel.py, gr_idle_threshold_panel.py, adaptive_power_panel.py,
// difr_live_panel.py): explainer first, then per controller a button row, a status line
// and a framed readback. Every button sends the request the native row buttons sent.
std::vector<PowerSavingIntent> draw_power_saving_family(PowerSavingViewState& state,const PowerSavingData* data,bool busy,bool unlocked,PowerSavingFamily family){
    std::vector<PowerSavingIntent> out;ImGui::PushID("power-saving");ImGui::PushID(100+static_cast<int>(family));
    const float wrap=classic_px(900);
    const bool idle=family==PowerSavingFamily::idle_threshold,adaptive=family==PowerSavingFamily::adaptive,difr=family==PowerSavingFamily::difr;
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+wrap);
    switch(family){
    case PowerSavingFamily::gr_elpg:classic_text("Hold requests that the driver disable engine-level power gating (ELPG) for this session. "
        "The driver selects associated controllers, which can include graphics and media engines. Individual engines cannot be selected. "
        "Driver readback is a partial check, not physical residency. The request is not automatically reapplied after a driver restart.");break;
    case PowerSavingFamily::individual:classic_text("Each control holds an individual power-gating inhibit for this session. "
        "The request targets the selected controller; readback confirms a driver request, not physical power gating, memory power, or residency. "
        "Requests are not reapplied after a driver restart.");break;
    case PowerSavingFamily::idle_threshold:classic_text("Adjust the idle delay for the selected controller before power gating for this session. "
        "Actual latency, power and residency are not measured. The driver request is not reapplied after a driver restart.");
        classic_text("Some controller paths also change a related adaptive-power policy flag. "
        "Returning the threshold to its earlier value does not clear that flag.");break;
    case PowerSavingFamily::adaptive:classic_text("Allow or disable the selected controller's shared adaptive-power policy for this session. "
        "Other reasons may keep it disabled. Readback is the aggregate driver policy state, not a measurement of physical gating, residency or power. "
        "The driver may also change idle thresholds; Allow does not restore their previous numbers. Requests are not reapplied after a driver restart.");break;
    case PowerSavingFamily::difr:classic_text("Hold sends one counted DIFR disable request for this Druta instance. "
        "The driver-enabled readback is separate and does not report residency. "
        // 1.9.1 says the request is not released automatically; here closing Druta or
        // changing GPU releases it (CoreClient prepare_close), so say that instead.
        "Druta releases this request when it closes or changes GPU. "
        "Factory-disabled entries remain read-only.");break;
    }
    if(!idle&&!adaptive)classic_text(saved_profile_note);
    ImGui::PopTextWrapPos();
    std::vector<const PowerSavingRow*> rows;if(data)for(const auto& row:data->rows)if(row.key.family==family&&std::any_of(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;}))rows.push_back(&row);
    const uint32_t read_mask=1u<<static_cast<uint32_t>(family);
    using Kind=PowerSavingLastAction::Kind;settle(state,data);
    // A family read re-reads every row of the family, so each row's status describes it.
    const auto read=[&](const char* label){ImGui::BeginDisabled(busy);if(ImGui::Button(label)){out.push_back({PowerSavingIntent::Operation::read,read_mask,{},{}});
        for(const auto* r:rows)remember(state,data,r->key,Kind::read);}ImGui::EndDisabled();};
    const auto save=[&](const PowerSavingRow* row){ImGui::BeginDisabled(busy||!row);if(ImGui::Button("Save request profile"))out.push_back({PowerSavingIntent::Operation::save_profile,31,{},{row->key}});ImGui::EndDisabled();};
    const auto status=[&](const PowerSavingRow* row){ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+wrap);
        classic_text(power_saving_status(state,data,row,family).c_str());ImGui::PopTextWrapPos();};
    const auto hold_release=[&](const PowerSavingRow* row,const char* hold,const char* release){
        ImGui::BeginDisabled(busy||!unlocked||!row||!row->writable||row->stale||data->recovery_pending);
        if(ImGui::Button(hold)){out.push_back({PowerSavingIntent::Operation::apply,31,{{row->key,1u,row->binding,row->value}},{}});remember(state,data,row->key,Kind::hold);}ImGui::EndDisabled();
        ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!row||!row->owned||row->uncertain);
        if(ImGui::Button(release)){out.push_back({PowerSavingIntent::Operation::apply,31,{{row->key,0u,row->binding,row->value}},{},true});remember(state,data,row->key,Kind::release);}ImGui::EndDisabled();};
    if(idle||adaptive){
        auto& choice=state.choice[static_cast<size_t>(family)];
        const auto chosen=std::find_if(rows.begin(),rows.end(),[&](const auto* r){return choice&&r->key.index==*choice;});
        const PowerSavingRow* row=chosen!=rows.end()?*chosen:rows.empty()?nullptr:rows.front();
        ImGui::SetNextItemWidth(classic_px(180));ImGui::BeginDisabled(rows.empty());
        if(ImGui::BeginCombo("##controller",row?controller_name(row->label).c_str():"")){for(const auto* r:rows){ImGui::PushID(static_cast<int>(r->key.index));
                if(ImGui::Selectable(controller_name(r->label).c_str(),r==row))choice=r->key.index;ImGui::PopID();}ImGui::EndCombo();}
        ImGui::EndDisabled();
        if(idle){auto draft=row?std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row->key;}):state.drafts.end();
            static std::array<char,32> empty{};const bool has=draft!=state.drafts.end();
            ImGui::SameLine();ImGui::SetNextItemWidth(classic_px(180));ImGui::BeginDisabled(!has);
            if(ImGui::InputText("Idle threshold (\xC2\xB5s)",has?draft->text.data():empty.data(),has?draft->text.size():empty.size())&&has)draft->dirty=true;ImGui::EndDisabled();
            const auto value=has?parse_power_saving_us(draft->text.data()):std::nullopt;
            ImGui::SameLine();read("Refresh");
            ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!row||!row->writable||row->stale||!value||data->recovery_pending||(value&&*value>row->maximum_us));
            if(ImGui::Button("Apply")){out.push_back({PowerSavingIntent::Operation::apply,31,{{row->key,*value,draft->binding,draft->expected}},{}});draft->dirty=false;remember(state,data,row->key,Kind::set,*value);}ImGui::EndDisabled();}
        else{ImGui::SameLine();read("Refresh");
            ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!row||!row->supported);
            if(ImGui::Button("Allow")){out.push_back({PowerSavingIntent::Operation::apply,31,{{row->key,1u,row->binding,row->value}},{},false});remember(state,data,row->key,Kind::set,1u,row->aggregate_enabled);}ImGui::EndDisabled();
            ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!row||!row->supported);
            if(ImGui::Button("Disable")){out.push_back({PowerSavingIntent::Operation::apply,31,{{row->key,0u,row->binding,row->value}},{}});remember(state,data,row->key,Kind::set,0u,row->aggregate_enabled);}ImGui::EndDisabled();}
        // Restore initial and the one-controller profile are native extras: they sit
        // under Undo, recovery and profiles at the end of this section.
        status(row);readback_box(row?power_saving_readback(*row):std::string(),idle?70.0f:65.0f);
    }else{
        // GR ELPG and DIFR hold one request; individual gating lists every controller.
        const auto controls=[&](const PowerSavingRow* row){
            read(difr?"Read":"Refresh");ImGui::SameLine();save(row);ImGui::SameLine();
            hold_release(row,difr?"Hold disable request":"Hold disabled",difr?"Release this request":"Release");
            status(row);readback_box(row?power_saving_readback(*row):std::string(),family==PowerSavingFamily::individual?65.0f:difr?90.0f:100.0f);};
        if(family==PowerSavingFamily::individual){for(const auto* row:rows){ImGui::PushID(static_cast<int>(row->key.index));
                ImGui::TextUnformatted(controller_name(row->label).c_str());controls(row);ImGui::PopID();}
            if(rows.empty()){ImGui::PushID(-1);controls(nullptr);ImGui::PopID();}}
        else controls(rows.empty()?nullptr:rows.front());
    }
    if(data&&data->recovery_pending)ImGui::TextWrapped("A power-saving request needs recovery. Recover it under Undo, recovery and profiles at the end of this section.");
    if(!unlocked)ImGui::TextDisabled("Unlock controls to request, release or restore. Reading is available while locked.");
    // Python's single status line already carries a failure's reason; the service's own
    // account of the last operation is shown under Undo, recovery and profiles.
    if(!state.status.empty())ImGui::TextWrapped("%s",state.status.c_str());
    if(std::any_of(state.staged.begin(),state.staged.end(),[&](const auto& r){return r.key.family==family;})){
        ImGui::TextDisabled("Staged requests apply individually; Undo restores the previous edit");
        for(size_t n=0;n<state.staged.size();++n){const auto& r=state.staged[n];if(r.key.family!=family)continue;ImGui::PushID(static_cast<int>(n));
            const auto row=std::find_if(rows.begin(),rows.end(),[&](const auto* x){return x->key==r.key;});
            const std::string name=row!=rows.end()?controller_name((*row)->label):"Controller "+std::to_string(r.key.index);
            if(idle)ImGui::Text("Staged %s: %u us",name.c_str(),r.value);
            else if(adaptive)ImGui::Text("Staged %s: %s",name.c_str(),r.value?"Allow":"Disable");
            else ImGui::Text("Staged %s: %s",name.c_str(),r.value?"hold disabled":"release");
            ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!data||(data->recovery_pending&&!adaptive));
            if(ImGui::Button("Apply staged request")){out.push_back({PowerSavingIntent::Operation::apply,31,{r},{}});
                const auto current=row!=rows.end()?(*row)->aggregate_enabled:std::nullopt;
                remember(state,data,r.key,idle||adaptive?Kind::set:r.value?Kind::hold:Kind::release,r.value,current);}
            ImGui::EndDisabled();ImGui::PopID();}
    }
    ImGui::PopID();ImGui::PopID();return out;
}
}
std::vector<PowerSavingIntent> draw_power_saving_controls(PowerSavingViewState& state,const PowerSavingData* data,uint64_t generation,bool busy,bool unlocked,bool classic,std::optional<PowerSavingFamily> only){(void)classic;
    if(only){sync_power_saving_view(state,data,generation);return draw_power_saving_family(state,data,busy,unlocked,*only);}
    return draw_power_saving(state,data,generation,busy,unlocked,Scope::all,only);}
std::vector<PowerSavingIntent> draw_power_saving_shared_controls(PowerSavingViewState& state,const PowerSavingData* data,uint64_t generation,bool busy,bool unlocked){return draw_power_saving(state,data,generation,busy,unlocked,Scope::shared,{});}
}
