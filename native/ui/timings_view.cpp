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
    if (data && data->snapshot.ok && !state.field.empty() && !field(*data,state.field)) state.field.clear();
}
bool select_timing_field(TimingsViewState& state,const TimingViewData& data,const std::string& name) {
    const auto* f=field(data,name); if(!f || !data.snapshot.ok || f->lo>f->hi || f->hi>=32)return false;
    const auto* w=word(data,*f); if(!w)return false;
    state.field=name;state.value=(w->value>>f->lo)&f->maximum;return true;
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
TimingsViewOutput draw_timings(TimingsViewState& state,const TimingViewData* data,std::uint64_t generation,bool busy,bool unlocked) {
    sync_timings_view(state,data,generation); TimingsViewOutput out;
    ImGui::TextUnformatted("Memory timings");
    ImGui::TextWrapped("nvtune is a separate helper. Timing writes affect the current memory band; capture and a stable top band are required.");
    ImGui::BeginDisabled(busy); if(ImGui::Button("Locate nvtune..."))out.choose_helper=true;
    ImGui::SameLine(); ImGui::BeginDisabled(!data||data->helper_path.empty());
    if(ImGui::Button("Read timings")){Command c{};c.operation=Command::Operation::timings_read;out.commands.push_back(c);} ImGui::EndDisabled(); ImGui::EndDisabled();
    if(!data){ImGui::TextDisabled("No helper configured");return out;}
    if(!data->helper_path.empty()) {const auto path=utf8(data->helper_path); ImGui::TextWrapped("Helper: %s",path.c_str());}
    if(!data->message.empty()) ImGui::TextWrapped("%s",data->message.c_str());
    ImGui::BeginDisabled(busy||!unlocked||!data->undo_available);
    if(ImGui::Button("Undo timing write")){Command c{};c.operation=Command::Operation::timings_undo;out.commands.push_back(c);} ImGui::EndDisabled();
    if(!data->snapshot.ok){ImGui::TextDisabled("No current timing capture");return out;}
    ImGui::Text("Adapter %s | %s | memory %s | top band %s",data->snapshot.slot.c_str(),data->snapshot.codename.c_str(),
        data->snapshot.memory_clock_stable?"stable":"unconfirmed",data->snapshot.top_band?"yes":"not established");
    if(ImGui::BeginTable("timing_fields",4,ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY,ImVec2(0,220))) {
        ImGui::TableSetupColumn("Field");ImGui::TableSetupColumn("Cycles");ImGui::TableSetupColumn("Register");ImGui::TableSetupColumn("Details");ImGui::TableHeadersRow();
        for(const auto& f:data->snapshot.fields){
            ImGui::TableNextRow();ImGui::TableSetColumnIndex(0);
            if(ImGui::Selectable(f.name.c_str(),state.field==f.name,ImGuiSelectableFlags_SpanAllColumns)) select_timing_field(state,*data,f.name);
            ImGui::TableSetColumnIndex(1);const auto* w=word(*data,f);
            if(w && f.lo<=f.hi && f.hi<32)ImGui::Text("%u",(w->value>>f.lo)&f.maximum);else ImGui::TextDisabled("--");
            ImGui::TableSetColumnIndex(2);ImGui::TextUnformatted(f.reg.c_str());
            ImGui::TableSetColumnIndex(3);ImGui::TextUnformatted(f.description.c_str());
        } ImGui::EndTable();
    }
    if(!state.field.empty()) {
        ImGui::Text("Selected: %s",state.field.c_str()); ImGui::SetNextItemWidth(140);
        ImGui::InputScalar("Cycles",ImGuiDataType_U32,&state.value);
        ImGui::BeginDisabled(!can_preview_timing(state,data,busy,unlocked));
        if(ImGui::Button("Preview timing")){Command c{};c.operation=Command::Operation::timings_preview;c.timing_field=state.field;c.timing_value=state.value;out.commands.push_back(c);} ImGui::EndDisabled();
        ImGui::SameLine();ImGui::BeginDisabled(!can_apply_timing(state,data,busy,unlocked));
        if(ImGui::Button("Apply timing")){Command c{};c.operation=Command::Operation::timings_apply;c.timing_field=state.field;c.timing_value=state.value;out.commands.push_back(c);} ImGui::EndDisabled();
    }
    if(data->preview && data->preview->ok) ImGui::TextWrapped("Preview %s: %u -> %u cycles; selected bits only",data->preview->field.c_str(),data->preview->prior,data->preview->value);
    return out;
}
}
