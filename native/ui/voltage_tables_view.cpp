// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_tables_view.h"
#include "voltage_offsets_profile.h"
#include "classic_layout.h"
#include "classic_section.h"
#include <imgui.h>
#include <cfloat>
#include <algorithm>
#include <cctype>
#include <cstdio>
namespace druta::ui {
namespace {
std::string mv(int32_t value){const int64_t magnitude=value<0?-static_cast<int64_t>(value):value;
    char buffer[64];std::snprintf(buffer,sizeof(buffer),"%s%lld.%03lld",value<0?"-":"",static_cast<long long>(magnitude/1000),static_cast<long long>(magnitude%1000));return buffer;}
std::string policy_mv(int64_t value){const uint64_t magnitude=value<0?static_cast<uint64_t>(-(value+1))+1:static_cast<uint64_t>(value);char buffer[64];std::snprintf(buffer,sizeof(buffer),"%s%llu.%03llu",value<0?"-":"",static_cast<unsigned long long>(magnitude/1000),static_cast<unsigned long long>(magnitude%1000));return buffer;}
void seed(VoltageTablesDraft& draft,const VoltageTableRow& row){if(row.value){const auto value=mv(*row.value);std::snprintf(draft.text.data(),draft.text.size(),"%s",value.c_str());}draft.expected=row.value;draft.binding=row.binding;}
}
std::optional<int32_t> parse_voltage_table_mv(const std::string& text){
    size_t start=text.find_first_not_of(" \t\r\n"),end=text.find_last_not_of(" \t\r\n");if(start==std::string::npos||end-start>100)return {};
    size_t position=start;bool negative=false;if(text[position]=='+'||text[position]=='-'){negative=text[position]=='-';++position;}
    std::string digits;int fractional=0;bool decimal=false,any=false;
    for(;position<=end;++position){const char c=text[position];if(c>='0'&&c<='9'){digits.push_back(c);if(decimal)++fractional;any=true;}
        else if(c=='.'&&!decimal)decimal=true;else break;}
    if(!any)return {};int exponent=0;
    if(position<=end&&(text[position]=='e'||text[position]=='E')){++position;bool minus=false;if(position<=end&&(text[position]=='+'||text[position]=='-')){minus=text[position]=='-';++position;}
        bool has=false;for(;position<=end&&text[position]>='0'&&text[position]<='9';++position){has=true;if(exponent>1000)return {};exponent=exponent*10+(text[position]-'0');}
        if(!has)return {};if(minus)exponent=-exponent;
    }
    if(position<=end)return {};const auto nonzero=digits.find_first_not_of('0');if(nonzero==std::string::npos)return int32_t{0};digits.erase(0,nonzero);
    int shift=3-fractional+exponent;
    while(shift<0&&!digits.empty()&&digits.back()=='0'){digits.pop_back();++shift;}
    if(shift<0||shift>10||digits.size()+static_cast<size_t>(shift)>10)return {};
    uint64_t value=0;for(char c:digits)value=value*10+static_cast<uint64_t>(c-'0');while(shift-->0)value*=10;
    const uint64_t maximum=negative?uint64_t{2147483648}:uint64_t{2147483647};if(value>maximum)return {};
    const int64_t signed_value=negative?-static_cast<int64_t>(value):static_cast<int64_t>(value);return static_cast<int32_t>(signed_value);
}
void sync_voltage_tables_view(VoltageTablesViewState& state,const VoltageTablesData* data,uint64_t generation){
    if(!state.initialized||state.generation!=generation){state={};state.initialized=true;state.generation=generation;}
    if(!data)return;
    // Classic: the first revision published after an action carries that action's result,
    // which replaces the host's "queued" note for it.
    for(auto& last:state.last)if(last&&!last->outcome&&data->revision!=last->revision){last->outcome=data->message;state.status.clear();}
    for(const auto& row:data->rows){auto draft=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;});
        if(draft==state.drafts.end()){state.drafts.push_back({});draft=state.drafts.end()-1;draft->key=row.key;seed(*draft,row);}
        else if(!draft->dirty&&!row.stale)seed(*draft,row);
    }
}
bool stage_voltage_tables_document(VoltageTablesViewState& state,const ProfileJson& document,const VoltageTablesData& data){
    try{const bool offsets=document.value("format",ProfileJson())=="druta-native-voltage-offsets"||document.value("scope",ProfileJson())=="voltage_offsets";auto staged=offsets?stage_voltage_offsets_profile(document,data):stage_voltage_tables_profile(document,data);if(offsets)state.offsets_staged=std::move(staged);else state.staged=std::move(staged);state.status=offsets?"Offset profile staged; review and press Apply staged offsets":"Profile staged; review selected requests and press Apply staged profile";return true;}
    catch(const std::exception& error){state.status=error.what();return false;}
}
namespace {
// Decimal(uV) / 1000 as Python prints it: no trailing zeros.
std::string mv_text(int64_t value){std::string text=policy_mv(value);
    while(!text.empty()&&text.back()=='0')text.pop_back();if(!text.empty()&&text.back()=='.')text.pop_back();return text;}
void readback_box(const std::string& text,float height){
    ImGui::InputTextMultiline("##readback",const_cast<char*>(text.c_str()),text.size()+1,ImVec2(-FLT_MIN,classic_px(height)),ImGuiInputTextFlags_ReadOnly);}
constexpr const char* request_fields[]={"REL","ALT/OP","OV","VMIN","VCRIT low","VCRIT high"};
constexpr const char* vcrit_note="stored request; effective threshold unverified";
VoltageTablesDraft* draft_for(VoltageTablesViewState& state,const VoltageTableKey& key){
    auto found=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==key;});return found==state.drafts.end()?nullptr:&*found;}
constexpr const char* offset_roles[]={"REL","ALT","OV","VMIN"};
// voltage_offsets_panel.py _label: "Rail 0 REL".
std::string offset_label(const VoltageTableKey& key){return "Rail "+std::to_string(key.rail)+" "+(key.field<4?offset_roles[key.field]:"unknown");}
// voltage_bios_runtime.py _request_name: NVVDD for rail 0 and MSVDD for rail 1 when the
// rail's identity is the one those names belong to, otherwise "Rail N".
std::string request_rail_label(const VoltageTablesData* data,uint32_t rail){
    std::string name="Rail "+std::to_string(rail);
    if(data)for(const auto& r:data->rows)if(r.key.family==VoltageTableFamily::requests&&r.key.rail==rail){
        if(rail<2&&r.policy_type==(rail==0?1u:3u))name=rail==0?"NVVDD":"MSVDD";break;}
    return "Rail "+std::to_string(rail)+" ("+name+")";}
}
// voltage_offsets_panel.py refresh: each offset, the unavailable ones, then the reported policy.
// A failed Read clears the box (refresh's except branch).
std::string voltage_offsets_inventory(const VoltageTablesData* data){
    if(!data||!data->offsets_read_error.empty())return {};
    std::string lines;const auto add=[&](const std::string& text){lines+=(lines.empty()?"":"\n")+text;};
    std::vector<const VoltageTableRow*> rows;for(const auto& r:data->rows)if(r.key.family==VoltageTableFamily::offsets)rows.push_back(&r);
    for(const auto* r:rows)if(r->value)add(offset_label(r->key)+" offset: "+mv_text(*r->value)+" mV"+(r->stale?" (stale)":""));
    for(const auto* r:rows)if(!r->value)add(offset_label(r->key)+": unavailable");
    // Every present rail's reported policy from the same read, effective included.
    constexpr const char* reported_names[]={"REL","ALT","OV","VMIN","effective"};
    for(uint32_t rail=0;rail<2;++rail)if(const auto& policy=data->offset_policy_uv[rail])
        for(size_t i=0;i<5;++i)add("Rail "+std::to_string(rail)+" reported "+reported_names[i]+": "+mv_text((*policy)[i])+" mV");
    return lines;
}
// voltage_requests_panel.py _show: each request, then the preserved boost request. A failed
// Read that kept nothing clears the box (refresh, _clear); a retained one keeps it.
std::string voltage_requests_inventory(const VoltageTablesData* data){
    if(!data||(!data->requests_read_error.empty()&&!data->requests_read_retained))return {};
    std::string lines;const auto add=[&](const std::string& text){lines+=(lines.empty()?"":"\n")+text;};
    bool any=false;
    for(const auto& r:data->rows)if(r.key.family==VoltageTableFamily::requests){any=true;
        add("Rail "+std::to_string(r.key.rail)+" "+(r.key.field<6?request_fields[r.key.field]:"unknown")+": "+(r.value?mv_text(*r.value)+" mV":std::string("unavailable"))
            +(r.key.field>=4?std::string(" (")+vcrit_note+")":"")+(r.stale?" (stale)":""));}
    if(!any)return lines;
    // Each present rail's reported policy, then the boost, then the active rails the
    // read could not pair (Python prints the list as [i, ...]).
    constexpr const char* reported_names[]={"REL","ALT/OP","OV","VMIN","effective"};
    for(uint32_t rail=0;rail<2;++rail)if(const auto& policy=data->request_policy_uv[rail])
        for(size_t i=0;i<5;++i)add("Rail "+std::to_string(rail)+" reported "+reported_names[i]+" policy: "+mv_text((*policy)[i])+" mV");
    add("Preserved voltage boost request: "+(data->boost_percent?std::to_string(*data->boost_percent)+"%":std::string("unknown")));
    if(!data->missing_rails.empty()){std::string list;for(const auto rail:data->missing_rails)list+=(list.empty()?"":", ")+std::to_string(rail);
        add("Active rails unavailable for complete preservation: ["+list+"]; applying disabled");}
    return lines;
}
namespace {
// The published result of a family's last request (not a Read), shown as its status line.
// A single field's Apply reads as voltage_requests_panel.py / voltage_offsets_panel.py apply:
// it held when the field reads back available at the requested value; an offset that held
// leaves the read line ("Offsets read ..."), as the panel's refresh does. Restore initial,
// Undo, Recover and a staged profile are native: the service's own account.
std::optional<std::string> request_outcome(const VoltageTablesViewState& state,const VoltageTablesData* data,size_t family){
    const auto& last=state.last[family];
    if(!last||last->read||!last->outcome)return {};
    if(!last->key)return last->outcome->empty()?std::nullopt:last->outcome;
    const VoltageTableRow* row=nullptr;if(data)for(const auto& r:data->rows)if(r.key==*last->key)row=&r;
    const bool held=row&&row->value&&*row->value==last->value&&!row->stale&&row->availability==VoltagePolicyAvailability::available;
    if(held){if(family==1)return {};
        return last->unchanged?std::string("No change requested; stored request already equals this value. No SET sent.")
            :std::string("Stored request applied and exact full request readback verified.")+(last->key->field>=4?std::string(" VCRIT: ")+vcrit_note+".":"");}
    if(last->outcome->empty())return {};
    return (family==1?"Could not apply offset: ":"Could not apply request: ")+*last->outcome;}
// voltage_offsets_panel.py / voltage_requests_panel.py _clear: a failed Read that kept nothing
// leaves no selectable entry; the rows the service kept stay for the shared actions.
bool offsets_cleared(const VoltageTablesData* data){return data&&!data->offsets_read_error.empty();}
bool requests_cleared(const VoltageTablesData* data){return data&&!data->requests_read_error.empty()&&!data->requests_read_retained;}
// Classic: what each family's controls last sent. A profile save or load is the host's.
void remember(VoltageTablesViewState& state,const VoltageTablesData* data,const std::vector<VoltageTablesIntent>& out){
    using O=VoltageTablesIntent::Operation;
    for(const auto& intent:out){if(intent.operation==O::save_profile||intent.operation==O::load_profile||(intent.families!=1&&intent.families!=2))continue;
        VoltageTablesLastAction action{intent.operation==O::read,data?data->revision:0,{},{},0,false};
        if(intent.operation==O::apply&&intent.requests.size()==1){const auto& r=intent.requests[0];action.key=r.key;action.value=r.value;
            if(data)for(const auto& row:data->rows)if(row.key==r.key)action.unchanged=row.value&&*row.value==r.value;}
        state.last[intent.families==1?0:1]=std::move(action);
        // The host's note for the previous action no longer applies.
        state.status.clear();}
}
}
// voltage_requests_panel.py refresh: a failed Read says so ("Could not read requests: ..."),
// otherwise _gate_message, the gate read's own failure included. After a request from
// these controls, its result takes the line, as the panel's apply does.
std::string voltage_requests_status(const VoltageTablesViewState& state,const VoltageTablesData* data){
    if(auto outcome=request_outcome(state,data,0))return *outcome;
    const bool read_failed=data&&!data->requests_read_error.empty();
    const bool unread=!data||(!read_failed&&data->requests_message.empty()&&std::none_of(data->rows.begin(),data->rows.end(),
        [](const auto& r){return r.key.family==VoltageTableFamily::requests;}));
    if(unread)return "Read requests for the selected adapter.";
    if(read_failed)return "Could not read requests: "+data->requests_read_error
        +(data->requests_read_retained?". Previous valid display retained; applying always reads fresh requests.":"");
    if(data->gate_enabled==false)return "Voltage-request gate disabled: requests are suppressed. Enable Allow voltage requests separately.";
    if(!data->gate_enabled)return "Request gate read unavailable; applying disabled"+(data->gate_error.empty()?std::string("."):": "+data->gate_error);
    if(!data->requests_complete)return "Requests read; complete preservation is unavailable, so applying is disabled.";
    return "Requests read. Voltage-request gate enabled.";
}
// voltage_offsets_panel.py: the prompt, "Offsets read for the selected adapter." after a
// Read, or "Could not read offsets: ..." after a failed one; a request's result after it.
std::string voltage_offsets_status(const VoltageTablesViewState& state,const VoltageTablesData* data){
    if(auto outcome=request_outcome(state,data,1))return *outcome;
    if(offsets_cleared(data))return "Could not read offsets: "+data->offsets_read_error;
    if(data&&(data->offsets_recognized||std::any_of(data->rows.begin(),data->rows.end(),[](const auto& r){return r.key.family==VoltageTableFamily::offsets;})))
        return "Offsets read for the selected adapter.";
    return "Read offsets for the selected adapter.";
}
namespace {
// The status line, with the service's own account of the read in its tooltip.
void status_line(const std::string& text,const std::string& account){
    classic_text(text);if(!account.empty()&&account!=text&&ImGui::IsItemHovered())ImGui::SetTooltip("%s",account.c_str());}
// Released 1.9.1 voltage_offsets_panel.py: explainer, Read, one offset chosen in a
// selector with a single mV input and Apply now, Save / Load, status and inventory.
// Enabling (installing the definitions with a GPU restart) is not in this build. Restore
// initial, Undo and Recover are under Undo, recovery and profiles.
void draw_offsets_part(VoltageTablesViewState& state,const VoltageTablesData* data,bool busy,bool unlocked,std::vector<VoltageTablesIntent>& out){
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+classic_px(900));
    classic_text("Independent offsets for REL, ALT, OV and VMIN policies. The offset definitions must already be "
        "installed; this build does not install them or restart the GPU. Live changes do not restart the GPU. "
        "Voltage boost can recompute REL and effective limits. Reported values are not measured rail voltage. "
        "These settings are separate from legacy voltage requests.");
    ImGui::BeginDisabled(busy);if(ImGui::Button("Read offsets"))out.push_back({VoltageTablesIntent::Operation::read,2,{},{}});ImGui::EndDisabled();
    std::vector<const VoltageTableRow*> rows;if(data&&!offsets_cleared(data))for(const auto& row:data->rows)if(row.key.family==VoltageTableFamily::offsets&&draft_for(state,row.key))rows.push_back(&row);
    const auto chosen=std::find_if(rows.begin(),rows.end(),[&](const auto* r){return state.offsets_choice&&r->key==*state.offsets_choice;});
    const VoltageTableRow* row=chosen!=rows.end()?*chosen:rows.empty()?nullptr:rows.front();
    ImGui::SetNextItemWidth(classic_px(250));ImGui::BeginDisabled(rows.empty());
    if(ImGui::BeginCombo("##offset-select",row?offset_label(row->key).c_str():"")){for(const auto* r:rows){ImGui::PushID(static_cast<int>(r->key.rail*16+r->key.field));
            if(ImGui::Selectable(offset_label(r->key).c_str(),r==row))state.offsets_choice=r->key;ImGui::PopID();}ImGui::EndCombo();}
    ImGui::EndDisabled();
    auto* draft=row?draft_for(state,row->key):nullptr;static std::array<char,64> empty{};
    ImGui::SameLine();ImGui::SetNextItemWidth(classic_px(130));ImGui::BeginDisabled(busy||!draft);
    if(ImGui::InputText("Offset (mV)",draft?draft->text.data():empty.data(),draft?draft->text.size():empty.size())&&draft)draft->dirty=true;ImGui::EndDisabled();
    // The selected offset's own account (why it is not writable) is the input's tooltip.
    if(row&&!row->message.empty()&&ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))ImGui::SetTooltip("%s",row->message.c_str());
    const auto parsed=draft?parse_voltage_table_mv(draft->text.data()):std::nullopt;
    const bool ready=row&&!busy&&unlocked&&row->writable&&!row->stale&&!data->offset_recovery_pending;
    ImGui::SameLine();ImGui::BeginDisabled(!ready||!parsed);
    if(ImGui::Button("Apply now")){out.push_back({VoltageTablesIntent::Operation::apply,2,{{row->key,*parsed,draft->binding,draft->expected}},{}});draft->dirty=false;}
    ImGui::EndDisabled();
    // Save offset profile saves every listed offset; loading stages it for review here.
    std::vector<VoltageTableKey> keys;for(const auto* r:rows)keys.push_back(r->key);
    ImGui::BeginDisabled(busy||!data||keys.empty());if(ImGui::Button("Save offset profile"))out.push_back({VoltageTablesIntent::Operation::save_profile,2,{},keys});ImGui::EndDisabled();
    ImGui::SameLine();ImGui::BeginDisabled(busy);if(ImGui::Button("Load offset profile..."))out.push_back({VoltageTablesIntent::Operation::load_profile,2,{},{}});ImGui::EndDisabled();
    if(!state.offsets_staged.empty()){
        ImGui::Text("Staged offsets: %zu selected fields",state.offsets_staged.size());
        for(const auto& r:state.offsets_staged)ImGui::Text("Rail %u %s: %s mV",r.key.rail,r.key.field<4?std::array<const char*,4>{"REL","ALT","OV","VMIN"}[r.key.field]:"unknown",mv_text(r.value).c_str());
        const bool staged_ready=!busy&&unlocked&&data&&data->offsets_recognized&&!data->offset_recovery_pending&&std::all_of(state.offsets_staged.begin(),state.offsets_staged.end(),[&](const auto& r){return std::any_of(data->rows.begin(),data->rows.end(),[&](const auto& x){return x.key==r.key&&x.writable&&!x.stale;});});
        ImGui::BeginDisabled(!staged_ready);if(ImGui::Button("Apply staged offsets"))out.push_back({VoltageTablesIntent::Operation::apply,2,state.offsets_staged,{}});ImGui::EndDisabled();ImGui::SameLine();if(ImGui::Button("Discard staged offsets"))state.offsets_staged.clear();
    }
    status_line(voltage_offsets_status(state,data),data?data->offsets_message:std::string());
    if(data&&data->offset_recovery_pending)ImGui::TextUnformatted("An offset request needs recovery. Recover it under Undo, recovery and profiles at the end of this section.");
    if(!state.status.empty())ImGui::TextUnformatted(state.status.c_str());
    ImGui::PopTextWrapPos();
    readback_box(voltage_offsets_inventory(data),160);
}
// Released 1.9.1 voltage_requests_panel.py: explainer, Read / Save / rail selector,
// one "<field> offset (mV)" input and "Apply <field>" per request word of the
// selected rail, the gate status line, the note and the inventory. Restore initial,
// loading a profile, Undo and Recover are under Undo, recovery and profiles.
void draw_requests_part(VoltageTablesViewState& state,const VoltageTablesData* data,bool busy,bool unlocked,std::vector<VoltageTablesIntent>& out){
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+classic_px(900));
    classic_text("Signed offsets in mV for the driver's stored legacy rail requests. Readback confirms requests, "
        "not measured voltage. The request gate is read separately; applying here never enables it.");
    ImGui::PopTextWrapPos();
    ImGui::BeginDisabled(busy);if(ImGui::Button("Read requests"))out.push_back({VoltageTablesIntent::Operation::read,1,{},{}});ImGui::EndDisabled();
    std::vector<VoltageTableKey> keys;std::vector<uint32_t> rails;
    if(data&&!requests_cleared(data))for(const auto& row:data->rows)if(row.key.family==VoltageTableFamily::requests&&draft_for(state,row.key)){keys.push_back(row.key);
        if(std::find(rails.begin(),rails.end(),row.key.rail)==rails.end())rails.push_back(row.key.rail);}
    ImGui::SameLine();ImGui::BeginDisabled(busy||!data||keys.empty());if(ImGui::Button("Save requests profile"))out.push_back({VoltageTablesIntent::Operation::save_profile,1,{},keys});ImGui::EndDisabled();
    const std::optional<uint32_t> rail=std::find(rails.begin(),rails.end(),state.requests_rail.value_or(UINT32_MAX))!=rails.end()?state.requests_rail:rails.empty()?std::nullopt:std::optional<uint32_t>(rails.front());
    const std::string preview=rail?request_rail_label(data,*rail):std::string();
    ImGui::SameLine();ImGui::SetNextItemWidth(classic_px(260));ImGui::BeginDisabled(rails.empty());
    if(ImGui::BeginCombo("##rail-select",preview.c_str())){for(uint32_t r:rails){ImGui::PushID(static_cast<int>(r));
            if(ImGui::Selectable(request_rail_label(data,r).c_str(),rail==r))state.requests_rail=r;ImGui::PopID();}ImGui::EndCombo();}
    ImGui::EndDisabled();
    static std::array<char,64> empty{};
    for(uint32_t field=0;field<6;++field){ImGui::PushID(static_cast<int>(field));
        const VoltageTableRow* row=nullptr;if(data&&rail)for(const auto& r:data->rows)if(r.key.family==VoltageTableFamily::requests&&r.key.rail==*rail&&r.key.field==field)row=&r;
        auto* draft=row?draft_for(state,row->key):nullptr;
        char label[48];std::snprintf(label,sizeof(label),"%s offset (mV)",request_fields[field]);
        ImGui::SetNextItemWidth(classic_px(130));ImGui::BeginDisabled(busy||!draft);
        if(ImGui::InputText(label,draft?draft->text.data():empty.data(),draft?draft->text.size():empty.size())&&draft)draft->dirty=true;ImGui::EndDisabled();
        if(row&&!row->message.empty()&&ImGui::IsItemHovered())ImGui::SetTooltip("%s",row->message.c_str());
        const auto parsed=draft?parse_voltage_table_mv(draft->text.data()):std::nullopt;
        const bool ready=row&&!busy&&unlocked&&row->writable&&!row->stale&&!data->recovery_pending;
        std::snprintf(label,sizeof(label),"Apply %s",request_fields[field]);
        ImGui::SameLine();ImGui::BeginDisabled(!ready||!parsed);
        if(ImGui::Button(label)){out.push_back({VoltageTablesIntent::Operation::apply,1,{{row->key,*parsed,draft->binding,draft->expected}},{}});draft->dirty=false;}
        ImGui::EndDisabled();
        if(field>=4){ImGui::SameLine();classic_text(vcrit_note);}
        ImGui::PopID();}
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+classic_px(900));
    status_line(voltage_requests_status(state,data),data?data->requests_message:std::string());
    classic_text("Apply saves a field-only undo point. Load saved requests manually from Profiles; "
        "they are never applied at startup or after a driver reset.");
    if(data&&data->recovery_pending)ImGui::TextUnformatted("A voltage request needs recovery. Recover it under Undo, recovery and profiles at the end of this section.");
    if(!state.status.empty())ImGui::TextUnformatted(state.status.c_str());
    ImGui::PopTextWrapPos();
    readback_box(voltage_requests_inventory(data),140);
}
}
std::vector<VoltageTablesIntent> draw_voltage_tables_controls(VoltageTablesViewState& state,const VoltageTablesData* data,uint64_t generation,bool busy,bool unlocked,bool classic,VoltageTablesPart part){
    (void)classic;sync_voltage_tables_view(state,data,generation);std::vector<VoltageTablesIntent> out;
    if(part!=VoltageTablesPart::all){ImGui::PushID("voltage-tables");ImGui::PushID(static_cast<int>(part));
        if(part==VoltageTablesPart::offsets)draw_offsets_part(state,data,busy,unlocked,out);else draw_requests_part(state,data,busy,unlocked,out);
        ImGui::PopID();ImGui::PopID();remember(state,data,out);return out;}
    ImGui::PushID("voltage-tables");if(part!=VoltageTablesPart::all)ImGui::PushID(static_cast<int>(part));
    if(part!=VoltageTablesPart::offsets){
    ImGui::BeginDisabled(busy);if(ImGui::Button("Read voltage requests"))out.push_back({VoltageTablesIntent::Operation::read,1,{},{}});ImGui::EndDisabled();
    if(data){ImGui::SameLine();ImGui::TextDisabled("Admission: %s | inventory: %s",data->gate_enabled?(*data->gate_enabled?"On":"Off"):"unreadable",data->requests_complete?"complete":"partial / unreadable");}
    if(data&&ImGui::BeginTable("voltage-request-fields",5,ImGuiTableFlags_SizingStretchProp|ImGuiTableFlags_BordersInnerH)){
        ImGui::TableSetupColumn("Field",ImGuiTableColumnFlags_WidthStretch,1.3f);ImGui::TableSetupColumn("Readback (mV)",ImGuiTableColumnFlags_WidthStretch,1.0f);
        ImGui::TableSetupColumn("Stage (mV)",ImGuiTableColumnFlags_WidthStretch,1.0f);ImGui::TableSetupColumn("Action",ImGuiTableColumnFlags_WidthStretch,1.5f);ImGui::TableSetupColumn("Profile",ImGuiTableColumnFlags_WidthFixed,65);ImGui::TableHeadersRow();
        for(const auto& row:data->rows){if(row.key.family!=VoltageTableFamily::requests)continue;
            auto draft=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;});if(draft==state.drafts.end())continue;
            ImGui::PushID(static_cast<int>(row.key.rail));ImGui::PushID(static_cast<int>(row.key.field));ImGui::TableNextRow();ImGui::TableSetColumnIndex(0);ImGui::TextWrapped("%s",row.label.c_str());
            if(row.key.field>=4)ImGui::TextDisabled("Threshold unverified");
            ImGui::TableSetColumnIndex(1);if(row.value)ImGui::Text("%s%s",mv(*row.value).c_str(),row.stale?" *":"");else ImGui::TextDisabled("unavailable");
            if(!row.message.empty()&&ImGui::IsItemHovered())ImGui::SetTooltip("%s",row.message.c_str());
            ImGui::TableSetColumnIndex(2);ImGui::SetNextItemWidth(-1);ImGui::BeginDisabled(busy);
            if(ImGui::InputText("##mv",draft->text.data(),draft->text.size()))draft->dirty=true;ImGui::EndDisabled();
            const auto parsed=parse_voltage_table_mv(draft->text.data());const bool ready=!busy&&unlocked&&row.writable&&!row.stale&&!data->recovery_pending;
            ImGui::TableSetColumnIndex(3);ImGui::BeginDisabled(!ready||!parsed);
            if(ImGui::Button("Apply")){out.push_back({VoltageTablesIntent::Operation::apply,1,{{row.key,*parsed,draft->binding,draft->expected}}, {}});draft->dirty=false;}
            ImGui::EndDisabled();ImGui::SameLine();ImGui::BeginDisabled(!ready||!row.initial);
            if(ImGui::Button("Restore initial"))out.push_back({VoltageTablesIntent::Operation::restore_initial,1,{}, {row.key}});ImGui::EndDisabled();
            ImGui::TableSetColumnIndex(4);ImGui::Checkbox("Include",&draft->include);ImGui::PopID();ImGui::PopID();
        }ImGui::EndTable();
    }
    std::vector<VoltageTableKey> selected;for(const auto& draft:state.drafts)if(draft.include&&draft.key.family==VoltageTableFamily::requests)selected.push_back(draft.key);
    ImGui::BeginDisabled(busy||!data||selected.empty());if(ImGui::Button("Save selected requests profile"))out.push_back({VoltageTablesIntent::Operation::save_profile,1,{},selected});ImGui::EndDisabled();
    ImGui::SameLine();ImGui::BeginDisabled(busy);if(ImGui::Button("Load requests profile"))out.push_back({VoltageTablesIntent::Operation::load_profile,1,{},{}});ImGui::EndDisabled();
    if(!state.staged.empty()){
        ImGui::Text("Staged profile: %zu selected fields",state.staged.size());
        if(ImGui::CollapsingHeader("Staged voltage requests"))for(const auto& request:state.staged)ImGui::Text("Rail %u %s: %s mV",request.key.rail,request.key.field<6?std::array<const char*,6>{"REL","ALT/OP","OV","VMIN","VCRIT low","VCRIT high"}[request.key.field]:"unknown",mv(static_cast<int32_t>(request.value)).c_str());
        ImGui::BeginDisabled(busy||!unlocked||!data||data->recovery_pending||!data->requests_complete||data->gate_enabled!=true);
        if(ImGui::Button("Apply staged profile"))out.push_back({VoltageTablesIntent::Operation::apply,1,state.staged,{}});ImGui::EndDisabled();
        ImGui::SameLine();if(ImGui::Button("Discard staged profile"))state.staged.clear();
    }
    ImGui::BeginDisabled(busy||!data||!data->undo_available||data->recovery_pending);
    if(ImGui::Button("Undo voltage requests"))out.push_back({VoltageTablesIntent::Operation::undo,1,{},{}});ImGui::EndDisabled();
    ImGui::SameLine();ImGui::BeginDisabled(busy||!data||!data->recovery_pending);
    if(ImGui::Button("Recover selected voltage requests"))out.push_back({VoltageTablesIntent::Operation::recover,1,{},{}});ImGui::EndDisabled();
    if(data&&!data->message.empty())ImGui::TextWrapped("%s",data->message.c_str());if(!state.status.empty())ImGui::TextWrapped("%s",state.status.c_str());
    }
    if(part==VoltageTablesPart::offsets&&!state.status.empty())ImGui::TextWrapped("%s",state.status.c_str());
    if(part==VoltageTablesPart::all?ImGui::CollapsingHeader("Policy scaffold offsets"):part==VoltageTablesPart::offsets){
        ImGui::PushID("installed-offsets");
        ImGui::BeginDisabled(busy);if(ImGui::Button("Read installed offsets"))out.push_back({VoltageTablesIntent::Operation::read,2,{},{}});ImGui::EndDisabled();
        if(data){ImGui::TextWrapped("%s",data->offsets_message.c_str());
            if(ImGui::BeginTable("offset-fields",5,ImGuiTableFlags_SizingStretchProp|ImGuiTableFlags_BordersInnerH)){
                ImGui::TableSetupColumn("Policy");ImGui::TableSetupColumn("Offset / reported (mV)");ImGui::TableSetupColumn("Stage (mV)");ImGui::TableSetupColumn("Action");ImGui::TableSetupColumn("Profile");ImGui::TableHeadersRow();
                for(const auto& row:data->rows)if(row.key.family==VoltageTableFamily::offsets){auto draft=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;});if(draft==state.drafts.end())continue;
                    ImGui::PushID(static_cast<int>(row.key.rail));ImGui::PushID(static_cast<int>(row.key.field));ImGui::TableNextRow();ImGui::TableSetColumnIndex(0);ImGui::TextWrapped("%s",row.label.c_str());
                    ImGui::TableSetColumnIndex(1);ImGui::Text("%s%s",row.value?mv(*row.value).c_str():"unavailable",row.stale?" (stale)":"");if(!row.message.empty()&&ImGui::IsItemHovered())ImGui::SetTooltip("%s",row.message.c_str());if(row.reported)ImGui::TextDisabled("Policy %s",policy_mv(*row.reported).c_str());
                    ImGui::TableSetColumnIndex(2);ImGui::SetNextItemWidth(-1);ImGui::BeginDisabled(busy);if(ImGui::InputText("##mv",draft->text.data(),draft->text.size()))draft->dirty=true;ImGui::EndDisabled();
                    const auto parsed=parse_voltage_table_mv(draft->text.data());const bool ready=!busy&&unlocked&&row.writable&&!row.stale&&!data->offset_recovery_pending;
                    ImGui::TableSetColumnIndex(3);ImGui::BeginDisabled(!ready||!parsed);if(ImGui::Button("Apply")){out.push_back({VoltageTablesIntent::Operation::apply,2,{{row.key,*parsed,draft->binding,draft->expected}}, {}});draft->dirty=false;}ImGui::EndDisabled();ImGui::SameLine();ImGui::BeginDisabled(!ready||!row.initial);if(ImGui::Button("Restore initial"))out.push_back({VoltageTablesIntent::Operation::restore_initial,2,{}, {row.key}});ImGui::EndDisabled();
                    ImGui::TableSetColumnIndex(4);ImGui::Checkbox("Include",&draft->include);ImGui::PopID();ImGui::PopID();
                }ImGui::EndTable();
            }
        }
        std::vector<VoltageTableKey> offsets_selected;for(const auto& d:state.drafts)if(d.include&&d.key.family==VoltageTableFamily::offsets)offsets_selected.push_back(d.key);
        ImGui::BeginDisabled(busy||!data||offsets_selected.empty());if(ImGui::Button("Save selected offsets profile"))out.push_back({VoltageTablesIntent::Operation::save_profile,2,{},offsets_selected});ImGui::EndDisabled();ImGui::SameLine();ImGui::BeginDisabled(busy);if(ImGui::Button("Load offsets profile"))out.push_back({VoltageTablesIntent::Operation::load_profile,2,{},{}});ImGui::EndDisabled();
        if(!state.offsets_staged.empty()){
            ImGui::Text("Staged offsets: %zu selected fields",state.offsets_staged.size());if(ImGui::CollapsingHeader("Review staged offsets"))for(const auto& r:state.offsets_staged)ImGui::Text("Rail %u %s: %s mV",r.key.rail,r.key.field<4?std::array<const char*,4>{"REL","ALT","OV","VMIN"}[r.key.field]:"unknown",mv(static_cast<int32_t>(r.value)).c_str());
            const bool ready=!busy&&unlocked&&data&&data->offsets_recognized&&!data->offset_recovery_pending&&std::all_of(state.offsets_staged.begin(),state.offsets_staged.end(),[&](const auto& r){return std::any_of(data->rows.begin(),data->rows.end(),[&](const auto& row){return row.key==r.key&&row.writable&&!row.stale;});});
            ImGui::BeginDisabled(!ready);if(ImGui::Button("Apply staged offsets"))out.push_back({VoltageTablesIntent::Operation::apply,2,state.offsets_staged,{}});ImGui::EndDisabled();ImGui::SameLine();if(ImGui::Button("Discard staged offsets"))state.offsets_staged.clear();
        }
        ImGui::BeginDisabled(busy||!data||!data->offset_undo_available||data->offset_recovery_pending);if(ImGui::Button("Undo installed offsets"))out.push_back({VoltageTablesIntent::Operation::undo,2,{},{}});ImGui::EndDisabled();ImGui::SameLine();ImGui::BeginDisabled(busy||!data||!data->offset_recovery_pending);if(ImGui::Button("Recover installed offsets"))out.push_back({VoltageTablesIntent::Operation::recover,2,{},{}});ImGui::EndDisabled();
        ImGui::TextDisabled("Signed definition requests; reported policy is not measured voltage.");ImGui::PopID();
    }
    if(part!=VoltageTablesPart::offsets&&ImGui::CollapsingHeader("Voltage table details"))ImGui::TextWrapped("Signed whole-microvolt software requests, shown in mV. Readback is not measured voltage; VCRIT effective thresholds remain unverified. Every Apply preserves boost, the other five fields and every other active rail from a fresh complete capture. Admission is separate and is never enabled here. Initial means this session's first qualified read. Profiles load as drafts and require explicit Apply; no startup or post-reset replay occurs.");
    if(part!=VoltageTablesPart::all)ImGui::PopID();
    ImGui::PopID();return out;
}
std::vector<VoltageTablesIntent> draw_voltage_tables_shared_controls(VoltageTablesViewState& state,const VoltageTablesData* data,uint64_t generation,bool busy,bool unlocked){
    sync_voltage_tables_view(state,data,generation);std::vector<VoltageTablesIntent> out;
    ImGui::PushID("voltage-tables");ImGui::PushID(200);
    ImGui::TextWrapped("Offsets and rail requests keep separate Undo and recovery. Each acts on this session's last request in its own family.");
    // Restore initial for each field, folded away; "initial" is this session's first qualified read.
    const auto restore=[&](const char* label,VoltageTableFamily family,uint32_t families,bool recovering){
        if(!ImGui::TreeNode(label))return;
        if(data)for(size_t i=0;i<data->rows.size();++i){const auto& row=data->rows[i];if(row.key.family!=family||!draft_for(state,row.key))continue;
            ImGui::PushID(static_cast<int>(i));ImGui::AlignTextToFramePadding();
            const std::string name=family==VoltageTableFamily::offsets?offset_label(row.key):row.label;
            ImGui::Text("%s: initial %s",name.c_str(),row.initial?(mv_text(*row.initial)+" mV").c_str():"unknown");ImGui::SameLine();
            ImGui::BeginDisabled(busy||!unlocked||!row.writable||row.stale||recovering||!row.initial);
            if(ImGui::Button("Restore initial"))out.push_back({VoltageTablesIntent::Operation::restore_initial,families,{},{row.key}});ImGui::EndDisabled();
            ImGui::PopID();}
        ImGui::TreePop();};
    // Classic: each subsection's Save writes every listed field, as 1.9.1 does; a
    // chosen subset (the Focused table's Include) stays available here, folded away.
    const auto tree=[&](const char* label,VoltageTableFamily family,uint32_t families,const char* save){
        if(!ImGui::TreeNode(label))return;
        std::vector<VoltageTableKey> selection;
        if(data)for(size_t i=0;i<data->rows.size();++i){const auto& row=data->rows[i];if(row.key.family!=family)continue;
            auto* draft=draft_for(state,row.key);if(!draft)continue;
            ImGui::PushID(static_cast<int>(i));ImGui::Checkbox(family==VoltageTableFamily::offsets?offset_label(row.key).c_str():row.label.c_str(),&draft->include);ImGui::PopID();
            if(draft->include)selection.push_back(row.key);}
        ImGui::BeginDisabled(busy||!data||selection.empty());
        if(ImGui::Button(save))out.push_back({VoltageTablesIntent::Operation::save_profile,families,{},selection});ImGui::EndDisabled();
        ImGui::TreePop();};
    // Voltage policy offsets: Undo / Recover, then the folded Restore and subset save.
    ImGui::AlignTextToFramePadding();ImGui::TextUnformatted("Voltage policy offsets");
    ImGui::BeginDisabled(busy||!data||!data->offset_undo_available||data->offset_recovery_pending);if(ImGui::Button("Undo installed offsets"))out.push_back({VoltageTablesIntent::Operation::undo,2,{},{}});ImGui::EndDisabled();
    ImGui::SameLine();ImGui::BeginDisabled(busy||!data||!data->offset_recovery_pending);if(ImGui::Button("Recover installed offsets"))out.push_back({VoltageTablesIntent::Operation::recover,2,{},{}});ImGui::EndDisabled();
    restore("Restore initial offsets",VoltageTableFamily::offsets,2,data&&data->offset_recovery_pending);
    tree("Save a selection of offsets",VoltageTableFamily::offsets,2,"Save selected offsets profile");
    // Live voltage rail requests: loading a saved profile (staged here for review and an
    // explicit Apply), Undo / Recover, then the folded Restore and subset save.
    ImGui::AlignTextToFramePadding();ImGui::TextUnformatted("Live voltage rail requests");
    ImGui::BeginDisabled(busy);if(ImGui::Button("Load requests profile"))out.push_back({VoltageTablesIntent::Operation::load_profile,1,{},{}});ImGui::EndDisabled();
    ImGui::SameLine();ImGui::BeginDisabled(busy||!data||!data->undo_available||data->recovery_pending);
    if(ImGui::Button("Undo voltage requests"))out.push_back({VoltageTablesIntent::Operation::undo,1,{},{}});ImGui::EndDisabled();
    ImGui::SameLine();ImGui::BeginDisabled(busy||!data||!data->recovery_pending);
    if(ImGui::Button("Recover selected voltage requests"))out.push_back({VoltageTablesIntent::Operation::recover,1,{},{}});ImGui::EndDisabled();
    if(!state.staged.empty()){
        ImGui::Text("Staged profile: %zu selected fields",state.staged.size());
        for(const auto& request:state.staged)ImGui::Text("Rail %u %s: %s mV",request.key.rail,request.key.field<6?request_fields[request.key.field]:"unknown",mv_text(request.value).c_str());
        ImGui::BeginDisabled(busy||!unlocked||!data||data->recovery_pending||!data->requests_complete||data->gate_enabled!=true);
        if(ImGui::Button("Apply staged profile"))out.push_back({VoltageTablesIntent::Operation::apply,1,state.staged,{}});ImGui::EndDisabled();
        ImGui::SameLine();if(ImGui::Button("Discard staged profile"))state.staged.clear();
    }
    restore("Restore initial requests",VoltageTableFamily::requests,1,data&&data->recovery_pending);
    tree("Save a selection of rail requests",VoltageTableFamily::requests,1,"Save selected requests profile");
    if(data&&!data->message.empty())ImGui::TextWrapped("%s",data->message.c_str());
    if(!state.status.empty())ImGui::TextWrapped("%s",state.status.c_str());
    ImGui::PopID();ImGui::PopID();remember(state,data,out);return out;
}
}
