/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "timings_view.h"
#include <imgui.h>
#include <windows.h>
#include <algorithm>
#include <vector>
namespace druta::ui {
namespace {
std::wstring env(const wchar_t* key) {
    const DWORD needed=GetEnvironmentVariableW(key,nullptr,0); if (!needed) return {};
    std::vector<wchar_t> value(needed); const DWORD used=GetEnvironmentVariableW(key,value.data(),needed);
    return used && used<needed ? std::wstring(value.data(),used) : std::wstring{};
}
std::wstring executable(std::wstring path) {
    if (path.empty()) return {};
    const DWORD kind=GetFileAttributesW(path.c_str());
    if (kind!=INVALID_FILE_ATTRIBUTES && (kind&FILE_ATTRIBUTE_DIRECTORY)) {
        if (path.back()!=L'\\' && path.back()!=L'/') path+=L'\\'; path+=L"nvtune.exe";
    }
    const DWORD flags=GetFileAttributesW(path.c_str());
    if (flags==INVALID_FILE_ATTRIBUTES || (flags&FILE_ATTRIBUTE_DIRECTORY)) return {};
    const DWORD needed=GetFullPathNameW(path.c_str(),0,nullptr,nullptr); if (!needed) return {};
    std::vector<wchar_t> absolute(needed); const DWORD n=GetFullPathNameW(path.c_str(),needed,absolute.data(),nullptr);
    return n && n<needed ? std::wstring(absolute.data(),n) : std::wstring{};
}
std::string utf8(const std::wstring& value) {
    if(value.empty())return {};
    const int n=WideCharToMultiByte(CP_UTF8,0,value.data(),static_cast<int>(value.size()),nullptr,0,nullptr,nullptr);
    if(n<=0)return {};
    std::string result(static_cast<size_t>(n),'\0');
    if(!WideCharToMultiByte(CP_UTF8,0,value.data(),static_cast<int>(value.size()),&result[0],n,nullptr,nullptr))return {};
    return result;
}const TimingField* field(const TimingViewData& data,const std::string& name) {
    const auto& rows=data.snapshot.fields;
    auto it=std::find_if(rows.begin(),rows.end(),[&](const auto& f){return f.name==name;});
    return it==rows.end()?nullptr:&*it;
}
const TimingWord* word(const TimingViewData& data,const TimingField& f) {
    const auto& rows=data.snapshot.broadcast;
    auto it=std::find_if(rows.begin(),rows.end(),[&](const auto& w){return w.name==f.reg;});
    return it==rows.end()?nullptr:&*it;
}
bool editable(const TimingViewData& data,const TimingField& f) {
    const auto* w=word(data,f);
    return data.snapshot.ok && data.snapshot.top_band && w && !f.structural && !f.inferred && !f.overlapping &&
        (w->confidence.empty() || w->confidence=="DOCUMENTED");
}
}
TimingsHelperSuggestion suggest_timing_helper() {
    const auto current=env(L"DRUTA_NVTUNE"), legacy=env(L"TITANTUNE_NVTUNE");
    if (!current.empty() || !legacy.empty()) {
        auto path=executable(!current.empty()?current:legacy);
        return {path,path.empty()?"The explicit nvtune environment path is unavailable":"nvtune found from the environment"};
    }
    std::vector<wchar_t> self(32768); const DWORD n=GetModuleFileNameW(nullptr,self.data(),static_cast<DWORD>(self.size()));
    if (n && n<self.size()) {
        std::wstring candidate(self.data(),n); const auto slash=candidate.find_last_of(L"\\/");
        if (slash!=std::wstring::npos) { auto path=executable(candidate.substr(0,slash+1)+L"nvtune.exe"); if (!path.empty()) return {path,"nvtune found beside the application"}; }
    }
    std::vector<wchar_t> found(32768); const DWORD count=SearchPathW(nullptr,L"nvtune.exe",nullptr,static_cast<DWORD>(found.size()),found.data(),nullptr);
    if (count && count<found.size()) { auto path=executable(std::wstring(found.data(),count)); if (!path.empty()) return {path,"nvtune found on PATH"}; }
    return {{},"Choose an installed nvtune.exe to read memory timings"};
}
void sync_timings_view(TimingsViewState& state,const TimingViewData* data,std::uint64_t generation) {
    if (!state.initialized || state.generation!=generation) { state={};state.initialized=true;state.generation=generation; }
    if(data){
        if((!state.observed_helper.empty()&&state.observed_helper!=data->helper_path)||
            (!state.observed_token.empty()&&!data->helper_token.empty()&&state.observed_token!=data->helper_token)){
            state.staged.clear();state.field.clear();state.message="Timing helper changed; staged fields cleared.";
        }
        state.observed_helper=data->helper_path;state.observed_token=data->helper_token;
    }
    if (data && data->snapshot.ok && !state.field.empty() && !field(*data,state.field)) state.field.clear();
}
bool select_timing_field(TimingsViewState& state,const TimingViewData& data,const std::string& name) {
    const auto* f=field(data,name); if(!f || !data.snapshot.ok || f->lo>f->hi || f->hi>=32)return false;
    const auto* w=word(data,*f); if(!w)return false;
    state.field=name;state.value=(w->value>>f->lo)&f->maximum;
    const auto staged=std::find_if(state.staged.begin(),state.staged.end(),[&](const auto& r){return r.field==name;});
    if(staged!=state.staged.end())state.value=staged->value;return true;
}
bool stage_timing_field(TimingsViewState& state,const TimingViewData& data,const std::string& name,uint32_t value){
    const auto* f=field(data,name);if(!f||!editable(data,*f)||value>f->maximum){state.message="Field cannot be staged from this capture.";return false;}
    auto staged=state.staged;const auto found=std::find_if(staged.begin(),staged.end(),[&](const auto& r){return r.field==name;});
    if(found==staged.end())staged.push_back({name,value});else found->value=value;
    std::sort(staged.begin(),staged.end(),[](const auto& a,const auto& b){return a.field<b.field;});state.staged=std::move(staged);state.message="Staged locally; preview the whole selection before Apply.";return true;
}
bool stage_timing_profile(const ProfileJson& document,const TimingViewData& data,TimingsViewState& state){
    try{auto fields=validate_timing_profile(document,data.identity,data.snapshot);state.staged=std::move(fields);
        state.message="Timing profile staged. Preview and Apply remain explicit.";return true;
    }catch(const std::exception& error){state.message=error.what();return false;}
}
bool can_preview_timing_batch(const TimingsViewState& state,const TimingViewData* data,bool busy){
    if(!data||busy||state.staged.empty())return false;
    for(const auto& request:state.staged){const auto* f=field(*data,request.field);if(!f||!editable(*data,*f)||request.value>f->maximum)return false;}return true;
}
bool can_apply_timing_batch(const TimingsViewState& state,const TimingViewData* data,bool busy,bool unlocked){
    if(!unlocked||!can_preview_timing_batch(state,data,busy)||!data->batch_preview||!data->batch_preview->ok||data->batch_preview->no_change||data->batch_preview->fields.size()!=state.staged.size())return false;
    for(const auto& r:state.staged){const auto& fields=data->batch_preview->fields;
        if(std::none_of(fields.begin(),fields.end(),[&](const auto& f){return f.field==r.field&&f.value==r.value;}))return false;}
    return true;
}
bool can_preview_timing(const TimingsViewState& state,const TimingViewData* data,bool busy,bool unlocked) {
    if(!data||busy||!unlocked||state.field.empty())return false;
    const auto* f=field(*data,state.field);
    return f && editable(*data,*f) && state.value<=f->maximum;
}
bool can_apply_timing(const TimingsViewState& state,const TimingViewData* data,bool busy,bool unlocked) {
    return can_preview_timing(state,data,busy,unlocked) && data->preview && data->preview->ok &&
        !data->preview->no_change && data->preview->field==state.field && data->preview->value==state.value;
}
bool can_capture_timing(const TimingViewData* data,bool busy){return data&&!busy&&!data->helper_path.empty()&&!data->capture_hold_pending&&!data->capture_workload_pending&&!data->capture_running;}
TimingsViewOutput draw_timings(TimingsViewState& state,const TimingViewData* data,std::uint64_t generation,bool worker_busy,bool unlocked,bool connected) {
    const bool busy=worker_busy||!connected;
    sync_timings_view(state,data,generation); TimingsViewOutput out;
    ImGui::TextUnformatted("Memory timings");
    ImGui::TextWrapped("nvtune is a separate helper. Timing writes affect the current memory band; capture and a stable top band are required.");
    ImGui::BeginDisabled(busy); if(ImGui::Button("Locate nvtune..."))out.choose_helper=true;
    ImGui::SameLine(); ImGui::BeginDisabled(!data||data->helper_path.empty());
    if(ImGui::Button("Read timings")){Command c{};c.operation=Command::Operation::timings_read;out.commands.push_back(c);} ImGui::EndDisabled(); ImGui::EndDisabled();
    if(!data){ImGui::TextDisabled("No helper configured");return out;}
    ImGui::BeginDisabled(!can_capture_timing(data,busy));
    ImGui::BeginDisabled(!unlocked);
    ImGui::SetNextItemWidth(110);ImGui::InputDouble("P0 capture V/F cap (mV)",&state.capture_cap_mv,0,0,"%.2f");
    ImGui::EndDisabled();
    if(ImGui::Button("Capture at performance band")){Command c;c.operation=Command::Operation::timings_capture_p0;c.timing_generation=generation;c.curve_cap_mv=state.capture_cap_mv;c.timing_allow_hold=unlocked;out.commands.push_back(c);}
    ImGui::EndDisabled();
    ImGui::TextWrapped(unlocked?"Uses current P0 first, then a temporary hold or optional CUDA workload. Releases only this capture's hold.":"Uses current P0 or an optional CUDA workload. Tuning holds remain disabled.");
    if(data->capture_running&&ImGui::Button("Cancel timing capture"))out.cancel_capture=true;
    if(data->capture_method==TimingCaptureMethod::cuda)ImGui::TextDisabled("Capture method: CUDA workload; actual P-state and band determine qualification.");
    if(data->capture_diagnostic){const auto& diagnostic=*data->capture_diagnostic;
        ImGui::TextWrapped("Raw capture diagnostic (does not authorize timing writes): %s",diagnostic.message.c_str());
        if(diagnostic.pstate_before&&diagnostic.pstate_after)ImGui::Text("Observed P-state: P%u to P%u",*diagnostic.pstate_before,*diagnostic.pstate_after);
        if(ImGui::CollapsingHeader("Inspect raw capture diagnostic")){
            ImGui::Text("Captured adapter: %s | %s",diagnostic.slot.c_str(),diagnostic.codename.c_str());
            ImGui::Text("Memory stable: %s | current top band: %s",diagnostic.memory_clock_stable?"yes":"unconfirmed",diagnostic.top_band?"yes":"not established");
            const auto observation=[](const char* label,const auto& before,const auto& after,const char* units){
                if(before&&after)ImGui::Text("%s: %lld -> %lld %s",label,static_cast<long long>(*before),static_cast<long long>(*after),units);
                else ImGui::Text("%s: unavailable",label);
            };
            observation("Memory clock",diagnostic.memory_before_mhz,diagnostic.memory_after_mhz,"MHz");
            observation("Memory offset request",diagnostic.offset_before,diagnostic.offset_after,"raw driver units");
            ImGui::TextWrapped("Read-only captured values. This panel never replaces the qualified editor capture or stages fields.");
            if(ImGui::BeginTable("capture_diagnostic_words",4,ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY,ImVec2(0,160))){
                ImGui::TableSetupColumn("Register");ImGui::TableSetupColumn("Address");ImGui::TableSetupColumn("Captured raw value");ImGui::TableSetupColumn("Evidence");ImGui::TableHeadersRow();
                for(const auto& row:diagnostic.broadcast){ImGui::TableNextRow();ImGui::TableSetColumnIndex(0);ImGui::TextUnformatted(row.name.c_str());
                    ImGui::TableSetColumnIndex(1);ImGui::Text("0x%08X",row.offset);ImGui::TableSetColumnIndex(2);ImGui::Text("0x%08X",row.value);
                    ImGui::TableSetColumnIndex(3);ImGui::TextUnformatted(row.confidence.c_str());}
                ImGui::EndTable();
            }
        }
    }
    if(data->capture_hold_pending||data->capture_workload_pending){
        if(data->capture_workload_pending)ImGui::TextWrapped("CUDA workload exit is unconfirmed. Retry cleanup before another capture.");
        ImGui::BeginDisabled(worker_busy);
        if(ImGui::Button("Release timing capture resources")){Command c;c.operation=Command::Operation::timings_release_capture_hold;out.commands.push_back(c);}
        ImGui::EndDisabled();
    }
    ImGui::Checkbox("Auto-capture on stable P0 entry",&state.auto_capture);
    if(ImGui::IsItemHovered())ImGui::SetTooltip("Uses ordinary telemetry only; Pause and minimized refresh settings still apply. Does not acquire a hold.");
    if(!data->helper_path.empty()) {const auto path=utf8(data->helper_path); ImGui::TextWrapped("Helper: %s",path.c_str());}
    if(!data->message.empty()) ImGui::TextWrapped("%s",data->message.c_str());
    ImGui::BeginDisabled(busy||!unlocked||!data->undo_available);
    if(ImGui::Button("Undo timing write")){Command c{};c.operation=Command::Operation::timings_undo;out.commands.push_back(c);} ImGui::EndDisabled();
    if(!state.message.empty())ImGui::TextWrapped("%s",state.message.c_str());
    ImGui::BeginDisabled(busy||!unlocked||!data->initial_available);
    if(ImGui::Button("Restore captured initial")){Command c{};c.operation=Command::Operation::timings_restore_initial;out.commands.push_back(c);}ImGui::EndDisabled();
    ImGui::TextWrapped("Captured initial means this connection's first qualified capture, not factory defaults. Values originate from captured broadcast fields; units and partition agreement require independent evidence.");
    if(!data->snapshot.ok)ImGui::TextDisabled("No current timing capture. Read timings to retry; staged edits are retained.");
    ImGui::Text("Adapter %s | %s | memory %s | top band %s",data->snapshot.slot.c_str(),data->snapshot.codename.c_str(),
        data->snapshot.memory_clock_stable?"stable":"unconfirmed",data->snapshot.top_band?"yes":"not established");
    const auto diagnostics=timing_diagnostics(data->snapshot,data->snapshot.diagnostic_evidence);
    ImGui::TextWrapped("%s | %s",diagnostics.clock_status.c_str(),diagnostics.partition_status.c_str());
    if(data->snapshot.ok&&ImGui::BeginTable("timing_fields",4,ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY,ImVec2(0,220))) {
        ImGui::TableSetupColumn("Field");ImGui::TableSetupColumn("Captured value");ImGui::TableSetupColumn("Register");ImGui::TableSetupColumn("Details");ImGui::TableHeadersRow();
        for(const auto& f:data->snapshot.fields){
            ImGui::TableNextRow();ImGui::TableSetColumnIndex(0);
            if(ImGui::Selectable(f.name.c_str(),state.field==f.name,ImGuiSelectableFlags_SpanAllColumns)) select_timing_field(state,*data,f.name);
            ImGui::TableSetColumnIndex(1);
            const auto index=static_cast<size_t>(&f-data->snapshot.fields.data());
            const auto& d=diagnostics.rows[index];
            if(d.value) { if(d.nanoseconds) ImGui::Text("%u %s / %.3f ns",*d.value,d.unit.c_str(),*d.nanoseconds); else ImGui::Text("%u %s",*d.value,d.unit.c_str()); } else ImGui::TextDisabled("--");
            ImGui::TableSetColumnIndex(2);ImGui::TextUnformatted(f.reg.c_str());
            ImGui::TableSetColumnIndex(3);ImGui::TextUnformatted(f.description.c_str());
            if(!d.provenance.empty())ImGui::TextWrapped("Units: %s",d.provenance.c_str());
            for(const auto& p:d.partitions) { if(p.value) ImGui::Text("%s: %u%s",p.scope.c_str(),*p.value,p.differs?" (differs)":""); else ImGui::Text("%s: unavailable",p.scope.c_str()); }
        } ImGui::EndTable();
    }
    if(!state.field.empty()) {
        ImGui::Text("Selected: %s",state.field.c_str()); ImGui::SetNextItemWidth(140);
        ImGui::InputScalar("Field value",ImGuiDataType_U32,&state.value);
        const auto* selected=field(*data,state.field);
        ImGui::BeginDisabled(busy||!selected||!editable(*data,*selected)||state.value>selected->maximum);
        if(ImGui::Button("Stage field"))stage_timing_field(state,*data,state.field,state.value);ImGui::EndDisabled();
        ImGui::SameLine();
        ImGui::BeginDisabled(!can_preview_timing(state,data,busy,unlocked));
        if(ImGui::Button("Preview timing")){Command c{};c.operation=Command::Operation::timings_preview;c.timing_field=state.field;c.timing_value=state.value;out.commands.push_back(c);} ImGui::EndDisabled();
        ImGui::SameLine();ImGui::BeginDisabled(!can_apply_timing(state,data,busy,unlocked));
        if(ImGui::Button("Apply timing")){Command c{};c.operation=Command::Operation::timings_apply;c.timing_field=state.field;c.timing_value=state.value;out.commands.push_back(c);} ImGui::EndDisabled();
    }
    if(data->preview && data->preview->ok) ImGui::TextWrapped("Preview %s: %u -> %u; selected bits only",data->preview->field.c_str(),data->preview->prior,data->preview->value);
    ImGui::SeparatorText("Staged timing fields");
    if(state.staged.empty())ImGui::TextDisabled("No staged timing fields.");
    for(size_t i=0;i<state.staged.size();++i){const auto& r=state.staged[i];ImGui::PushID(static_cast<int>(i));
        const auto* f=field(*data,r.field);const auto* w=f?word(*data,*f):nullptr;
        if(f&&w&&f->lo<=f->hi&&f->hi<32)ImGui::Text("%s: %u -> %u",r.field.c_str(),(w->value>>f->lo)&f->maximum,r.value);
        else ImGui::Text("%s: -- -> %u (refresh required)",r.field.c_str(),r.value);
        ImGui::SameLine();if(ImGui::SmallButton("Remove")){state.staged.erase(state.staged.begin()+static_cast<std::ptrdiff_t>(i));ImGui::PopID();break;}ImGui::PopID();
    }
    if(ImGui::Button("Revert staged edits"))state.staged.clear();ImGui::SameLine();
    ImGui::BeginDisabled(!can_preview_timing_batch(state,data,busy));
    if(ImGui::Button("Preview all staged")){auto request=std::make_shared<TimingEditorRequest>();request->fields=state.staged;
        Command c{};c.operation=Command::Operation::timings_preview_batch;c.timing_editor=std::move(request);out.commands.push_back(std::move(c));}ImGui::EndDisabled();ImGui::SameLine();
    ImGui::BeginDisabled(!can_apply_timing_batch(state,data,busy,unlocked));
    if(ImGui::Button("Apply all staged")){auto request=std::make_shared<TimingEditorRequest>();request->fields=state.staged;
        Command c{};c.operation=Command::Operation::timings_apply_batch;c.timing_editor=std::move(request);out.commands.push_back(std::move(c));}ImGui::EndDisabled();
    ImGui::BeginDisabled(busy||!data->snapshot.ok||!data->snapshot.top_band);if(ImGui::Button("Save timing profile..."))out.save_profile=true;ImGui::EndDisabled();ImGui::SameLine();
    ImGui::BeginDisabled(busy);if(ImGui::Button("Load timing profile..."))out.load_profile=true;ImGui::EndDisabled();
    if(data->batch_preview&&!data->batch_preview->message.empty())ImGui::TextWrapped("%s",data->batch_preview->message.c_str());
    return out;
}
}
