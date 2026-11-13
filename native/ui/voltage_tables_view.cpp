// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_tables_view.h"
#include <imgui.h>
#include <algorithm>
#include <cctype>
#include <cstdio>
namespace druta::ui {
namespace {
std::string mv(int32_t value){const int64_t magnitude=value<0?-static_cast<int64_t>(value):value;
    char buffer[64];std::snprintf(buffer,sizeof(buffer),"%s%lld.%03lld",value<0?"-":"",static_cast<long long>(magnitude/1000),static_cast<long long>(magnitude%1000));return buffer;}
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
    for(const auto& row:data->rows){auto draft=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;});
        if(draft==state.drafts.end()){state.drafts.push_back({});draft=state.drafts.end()-1;draft->key=row.key;seed(*draft,row);}
        else if(!draft->dirty&&!row.stale)seed(*draft,row);
    }
}
bool stage_voltage_tables_document(VoltageTablesViewState& state,const ProfileJson& document,const VoltageTablesData& data){
    try{auto staged=stage_voltage_tables_profile(document,data);state.staged=std::move(staged);state.status="Profile staged; review selected requests and press Apply staged profile";return true;}
    catch(const std::exception& error){state.status=error.what();return false;}
}
std::vector<VoltageTablesIntent> draw_voltage_tables_controls(VoltageTablesViewState& state,const VoltageTablesData* data,uint64_t generation,bool busy,bool unlocked,bool classic){
    (void)classic;sync_voltage_tables_view(state,data,generation);std::vector<VoltageTablesIntent> out;
    ImGui::PushID("voltage-tables");
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
    std::vector<VoltageTableKey> selected;for(const auto& draft:state.drafts)if(draft.include)selected.push_back(draft.key);
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
    if(ImGui::CollapsingHeader("Policy scaffold offsets"))ImGui::TextWrapped("%s",data?data->offsets_message.c_str():"Requires native ROM graph and installed-scaffold receipt qualification; activation is not implemented.");
    if(ImGui::CollapsingHeader("Voltage table details"))ImGui::TextWrapped("Signed whole-microvolt software requests, shown in mV. Readback is not measured voltage; VCRIT effective thresholds remain unverified. Every Apply preserves boost, the other five fields and every other active rail from a fresh complete capture. Admission is separate and is never enabled here. Initial means this session's first qualified read. Profiles load as drafts and require explicit Apply; no startup or post-reset replay occurs.");
    ImGui::PopID();return out;
}
}
