/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_view.h"
#include "ui_view.h"
#include "session.h"
#include "i2c_catalog.h"
#include "classic_layout.h"
#include <imgui.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
namespace druta::ui {
namespace {
bool ready(const ViewState& state) { return state.live && state.live->data.connected && !state.live->busy && !state.live->opening; }
bool idle(const ViewState& state) { return state.live && !state.live->busy && !state.live->opening; }
void flash_gate_attempt(ViewState& state,bool gated) {
    if (gated && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled) && ImGui::IsMouseClicked(ImGuiMouseButton_Left))
        state.i2c_gate_flash_until=ImGui::GetTime()+0.8;
}
Command command(const ViewState& state, Command::Operation op, double value = 0) {
    Command c; c.operation=op; c.value=value;
    if (state.live) {
        const auto& data=state.live->data.i2c;
        c.i2c_route=data.selected; c.i2c_family=data.family;
        c.i2c_selection_revision=data.selection_revision;
    }
    return c;
}
void queue(ViewState& state, Command::Operation op, double value = 0) { state.commands.push_back(command(state,op,value)); }
bool same_route(const I2cRoute& a,const I2cRoute& b) { return a.port==b.port && a.addr7==b.addr7; }
void observe(ViewState& state,const I2cViewData& data) {
    if (!state.i2c_draft_initialized || state.i2c_draft_generation!=state.live->generation ||
        state.i2c_draft_selection_revision!=data.selection_revision) {
        state.i2c_field_drafts.clear(); state.i2c_field_cache.clear();
        state.i2c_draft_generation=state.live->generation;
        state.i2c_draft_selection_revision=data.selection_revision;
        state.i2c_draft_initialized=true; state.i2c_target_mv=1100; state.i2c_nct_mapping=0;
    }
    // A partial or failed explicit read must not erase established rows or edits.
    for (auto& old : state.i2c_field_cache) old.stale=true;
    for (const auto& field : data.controller.fields) {
        auto found=std::find_if(state.i2c_field_cache.begin(),state.i2c_field_cache.end(),
            [&](const auto& old){return old.key==field.key;});
        if (found==state.i2c_field_cache.end()) state.i2c_field_cache.push_back(field);
        else *found=field;
    }
}
ControlDraft& field_draft(ViewState& state,const I2cField& field) {
    auto found=std::find_if(state.i2c_field_drafts.begin(),state.i2c_field_drafts.end(),
        [&](const auto& entry){return entry.first==field.key;});
    if (found==state.i2c_field_drafts.end()) {
        state.i2c_field_drafts.emplace_back(field.key,ControlDraft{});
        found=state.i2c_field_drafts.end()-1;
    }
    auto& draft=found->second;
    if (!draft.initialized) {
        draft.value=field.value.value_or(field.key=="nvvdd"?950:field.key=="fbvdd"?1300:field.minimum.value_or(0));
        draft.baseline=field.value; draft.initialized=true;
    } else if (!draft.dirty && !draft.editing && !field.stale && field.value) {
        draft.value=*field.value; draft.baseline=field.value;
    }
    return draft;
}
void snap(double& value,const I2cField& field) {
    if (!std::isfinite(value)) return;
    if (field.step && *field.step>0) value=std::round(value / *field.step) * *field.step;
    if (field.minimum && field.maximum) value=std::clamp(value,*field.minimum,*field.maximum);
}
double nct_gain(const std::string& key) { return key=="out2"?6.6:1.0; }
I2cField display_field(const I2cField& native,bool mapped) {
    auto field=native;
    if (!mapped) return field;
    const double gain=nct_gain(field.key);
    field.label=field.key=="out3"?"GPU offset (OUT3)":field.key=="out1"?"Memory offset (OUT1)":"PEX-PLL offset (OUT2)";
    field.unit="mV";
    if (native.value) field.value=-*native.value*gain;
    field.minimum=native.maximum?std::optional<double>(-*native.maximum*gain):std::nullopt;
    field.maximum=native.minimum?std::optional<double>(-*native.minimum*gain):std::nullopt;
    if (native.step) field.step=*native.step*gain;
    field.detail="Configured board offset from nominal DAC current, not a measured rail voltage";
    return field;
}
void draw_nct_mapping(ViewState& state) {
    const char* modes[]={"Raw outputs (µA)","GPU / memory / PEX-PLL (mV)"};
    bool fresh=true;
    for (const char* key : {"out1","out2","out3"}) {
        const auto found=std::find_if(state.i2c_field_cache.begin(),state.i2c_field_cache.end(),
            [&](const auto& field){return field.key==key;});
        fresh=fresh && found!=state.i2c_field_cache.end() && !found->stale && found->value && found->step && found->minimum && found->maximum;
    }
    ImGui::BeginDisabled(!ready(state) || !fresh); ImGui::SetNextItemWidth(360);
    ImGui::Combo("Output mapping",&state.i2c_nct_mapping,modes,2);
    ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("Use the mV mapping only for verified board wiring:\nOUT3 GPU, OUT1 memory, OUT2 PEX/PLL.\n-10 µA gives +10 mV GPU/memory or +66 mV PEX/PLL.");
    ImGui::TextWrapped("Select mV only after confirming this board's wiring and gains with a meter. This display choice applies to this connection.");
    if (!fresh) ImGui::TextDisabled("Read settings before changing output units.");
    if (state.i2c_nct_mapping)
        ImGui::TextWrapped("Positive mV raises voltage; negative mV lowers it. GPU/memory: 10 mV per 10 µA; PEX/PLL: 66 mV per 10 µA, with reversed sign. Configured offsets, not live rail voltages.");
    else ImGui::TextWrapped("Positive is SOURCE current and negative is SINK current, in nominal µA. Register readback; physical voltage requires a meter.");
}
void classic_family_help(const I2cViewData& data) {
    // Presentation from released fa6ee83's controller-specific frontend.
    // Ranges and write eligibility continue to come from this connection.
    ImGui::SeparatorText(data.controller.name.empty()?i2c_family_name(data.family):data.controller.name.c_str());
    switch (data.family) {
    case I2cFamily::nct3933:
        ImGui::TextWrapped("Three output offsets. OUT3, OUT1, OUT2 are shown in that order. Read settings first; Apply changes only its selected output and Zero sets that output to zero. Register current is not a measured rail voltage.");
        break;
    case I2cFamily::icx:
        ImGui::TextWrapped("iCX readback is the controller's voltage request, not a voltage measurement. Move a slider or select a setting before Apply. NVVDD Auto releases only its manual request; FBVDD is independent. Auto can show a staged manual value in its input.");
        ImGui::TextWrapped("Close Classified before applying. Frequency, overcurrent protection and loadline settings are independent of voltage requests. Use a meter to verify physical voltage. Loading or restoring an iCX preset stages a preview; Apply iCX preset is explicit.");
        break;
    case I2cFamily::up9512r:
        ImGui::TextWrapped("One common offset request updates all five load-current states. Mixed programmed offsets remain distinct from the staged common request; inspect the load-state readback below. Verify the selected controller before Apply.");
        break;
    case I2cFamily::ncp4206:
        ImGui::TextWrapped("Absolute VID command. Auto returns control to GPU VID; the target request is separate from controller VOUT and physical voltage. Verify this connection before applying a manual target.");
        break;
    case I2cFamily::mp29816:
        ImGui::TextWrapped("Offset requests require an understood controller scale and a verified response. Readback is the stored offset; VOUT is reported separately. Unavailable scale or verification keeps writes disabled.");
        break;
    case I2cFamily::mp2888:
        ImGui::TextWrapped(data.control_profile=="mp2888a-pmbus"
            ? "Manual PMBus voltage commands are nominal requests, not measured voltage. Verify this recipe at the owned low-clock clamp before Apply. Restore returns this session's captured controller state before releasing its clock."
            : "GPU VID offset recipe. Verify this selected method before Apply. A candidate fingerprint does not identify the electrical rail; controller VOUT is separate from the requested offset.");
        break;
    default: break;
    }
    ImGui::TextDisabled("Slider / input stage only; readback is separate. Apply and reset affect the selected field. * = last confirmed read.");
}
void draw_fields(ViewState& state,const I2cViewData& data) {
    const bool classic = state.presentation == Presentation::classic;
    if (classic) {
        ImGui::SetNextWindowContentSize({classic_px(classic_column_width), 0});
        ImGui::BeginChild("classic-i2c-fields", {0, 0}, ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize,
            ImGuiWindowFlags_HorizontalScrollbar);
    }
    const bool began = classic ? begin_classic_knob_table("i2c-knobs")
        : ImGui::BeginTable("i2c-knobs",5,ImGuiTableFlags_SizingStretchProp);
    if (!began) { if (classic) ImGui::EndChild(); return; }
    if (!classic) {
    ImGui::TableSetupColumn("Control",ImGuiTableColumnFlags_WidthStretch,1.45f);
    ImGui::TableSetupColumn("Stage",ImGuiTableColumnFlags_WidthStretch,2.0f);
    ImGui::TableSetupColumn("Value",ImGuiTableColumnFlags_WidthFixed,85);
    ImGui::TableSetupColumn("Readback",ImGuiTableColumnFlags_WidthStretch,1.0f);
    ImGui::TableSetupColumn("Action",ImGuiTableColumnFlags_WidthFixed,150);
    }
    std::vector<const I2cField*> rows;
    for (const auto& field : state.i2c_field_cache) rows.push_back(&field);
    if (data.family==I2cFamily::nct3933)
        std::stable_sort(rows.begin(),rows.end(),[](const auto* a,const auto* b) {
            const auto order=[](const std::string& key){return key=="out3"?0:key=="out1"?1:2;};
            return order(a->key)<order(b->key);
        });
    if (classic && data.family==I2cFamily::icx)
        std::stable_sort(rows.begin(),rows.end(),[](const auto* a,const auto* b) {
            const auto order=[](const std::string& key) {
                const char* keys[]{"nvvdd","fbvdd","frequency_nvvdd","frequency_fbvdd","ocp_nvvdd","ocp_fbvdd","loadline"};
                for (int i=0;i<7;++i) if (key==keys[i]) return i;
                return 7;
            };
            return order(a->key)<order(b->key);
        });
    for (const auto* native : rows) {
        const bool mapped=data.family==I2cFamily::nct3933 && state.i2c_nct_mapping==1;
        auto field=display_field(*native,mapped);
        if (classic && data.family==I2cFamily::icx) {
            if (field.key=="nvvdd") field.label="NVVDD voltage request";
            else if (field.key=="fbvdd") field.label="FBVDD voltage request";
            else if (field.key=="ocp_nvvdd") field.label="NVVDD overcurrent protection";
            else if (field.key=="ocp_fbvdd") field.label="FBVDD overcurrent protection";
            else if (field.key=="loadline") field.label="NVVDD loadline";
        }
        ImGui::PushID(field.key.c_str());
        auto& native_draft=field_draft(state,*native);
        auto draft=native_draft;
        if (mapped) draft.value=-draft.value*nct_gain(field.key);
        const bool gated=ready(state) && field.writable && !data.gate;
        const bool edit=ready(state) && field.writable && state.controls_unlocked && data.gate;
        const bool apply=edit && !data.controller.recovery &&
            (!data.controller.needs_verification || data.controller.verified);
        ImGui::TableNextRow(); ImGui::TableSetColumnIndex(0);
        ImGui::AlignTextToFramePadding(); ImGui::TextWrapped("%s%s%s%s",field.label.c_str(),field.unit.empty()?"":" (",field.unit.c_str(),field.unit.empty()?"":")");
        if (data.family==I2cFamily::nct3933 && field.step) ImGui::TextDisabled("Step %.0f %s",*field.step,field.unit.c_str());
        // Keep labels and controls compact like the Classic knob rows.
        ImGui::TableSetColumnIndex(1); ImGui::SetNextItemWidth(-1);
        auto choices=field.choices;
        if (data.family==I2cFamily::icx && choices.empty()) {
            if (field.key=="frequency_nvvdd" || field.key=="frequency_fbvdd")
                for (int n=300;n<=1000;n+=100) choices.emplace_back(std::to_string(n)+" kHz",n);
            else if (field.key=="loadline")
                for (int n=0;n<=15;++n) choices.emplace_back("Level"+std::to_string(n),n);
            else if (classic && (field.key=="ocp_nvvdd" || field.key=="ocp_fbvdd"))
                choices={{"Enable",1},{"Disable",0}};
        }
        ImGui::BeginDisabled(!edit);
        if (!choices.empty()) {
            const auto selected=std::find_if(choices.begin(),choices.end(),[&](const auto& choice){return choice.second==draft.value;});
            if (ImGui::BeginCombo("##choice",selected==choices.end()?"Select":selected->first.c_str())) {
                for (const auto& choice : choices) if (ImGui::Selectable(choice.first.c_str(),choice.second==draft.value)) {
                    draft.value=choice.second; draft.dirty=true;
                }
                ImGui::EndCombo();
            }
            flash_gate_attempt(state,gated);
            draft.editing=ImGui::IsItemActive();
        } else if (field.minimum && field.maximum && *field.minimum<*field.maximum) {
            if (ImGui::SliderScalar("##slider",ImGuiDataType_Double,&draft.value,&*field.minimum,&*field.maximum,classic?" ":"%.2f")) {
                snap(draft.value,field); draft.dirty=true;
            }
            flash_gate_attempt(state,gated);
            draft.editing=ImGui::IsItemActive();
        } else ImGui::TextDisabled("Range unavailable");
        ImGui::TableSetColumnIndex(2);
        if (choices.empty()) {
            ImGui::SetNextItemWidth(-1);
            if (ImGui::InputDouble("##number",&draft.value,0,0,"%.2f")) draft.dirty=true;
            if (ImGui::IsItemDeactivatedAfterEdit()) snap(draft.value,field);
            flash_gate_attempt(state,gated);
            draft.editing=draft.editing || ImGui::IsItemActive();
        }
        ImGui::EndDisabled();
        ImGui::TableSetColumnIndex(3); ImGui::AlignTextToFramePadding();
        if (classic) push_classic_mono();
        if (field.value && classic) {
            const auto choice=std::find_if(choices.begin(),choices.end(),[&](const auto& value){return value.second==*field.value;});
            if (choice!=choices.end()) ImGui::TextWrapped("%s%s",choice->first.c_str(),field.stale?" *":"");
            else ImGui::TextWrapped("%.2f %s%s",*field.value,field.unit.c_str(),field.stale?" *":"");
        }
        else if (field.value) ImGui::Text("%.2f %s%s",*field.value,field.unit.c_str(),field.stale?" *":"");
        else ImGui::TextDisabled("%s",field.detail=="Auto"?"Auto":"unavailable");
        if (classic) ImGui::PopFont();
        if (!field.detail.empty() && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) ImGui::SetTooltip("%s",field.detail.c_str());
        ImGui::TableSetColumnIndex(4); ImGui::BeginDisabled(!apply || !std::isfinite(draft.value) || (data.family==I2cFamily::icx && !draft.dirty));
        if (ImGui::Button("Apply", ImVec2(classic?-1.0f:0.0f,0))) {
            const double value=mapped?-draft.value/nct_gain(field.key):draft.value;
            auto c=command(state,Command::Operation::i2c_apply,value); c.i2c_field=field.key; state.commands.push_back(c);
        }
        flash_gate_attempt(state,gated);
        ImGui::EndDisabled();
        if (classic) ImGui::TableSetColumnIndex(5);
        if (field.resettable) {
            if (!classic) ImGui::SameLine(); ImGui::BeginDisabled(!apply);
            if (ImGui::Button(field.reset_label.c_str(), ImVec2(classic?-1.0f:0.0f,0))) { auto c=command(state,Command::Operation::i2c_reset); c.i2c_field=field.key; state.commands.push_back(c); }
            flash_gate_attempt(state,gated);
            ImGui::EndDisabled();
        }
        native_draft=draft;
        if (mapped) native_draft.value=-draft.value/nct_gain(field.key);
        ImGui::PopID();
    }
    ImGui::EndTable();
    if (classic) ImGui::EndChild();
}
void draw_manual(ViewState& state,const I2cViewData& data,const I2cControlProfile* profile) {
    const auto& status=data.status;
    if (!status.min_mv || !status.max_mv) return;
    const double low=*status.min_mv,high=*status.max_mv;
    const bool gated=ready(state) && !data.gate;
    ImGui::BeginDisabled(!ready(state) || !state.controls_unlocked || !data.gate);
    if (profile && !profile->presets_mv.empty()) {
        ImGui::SetNextItemWidth(240);
        if (ImGui::BeginCombo("Nominal request preset","Stage a nominal request")) {
            for (double value : profile->presets_mv) {
                if (!std::isfinite(value) || value<low || value>high) continue;
                char label[80]{}; std::snprintf(label,sizeof(label),"%.2f mV nominal",value);
                if (ImGui::Selectable(label,state.i2c_target_mv==value)) state.i2c_target_mv=value;
            }
            ImGui::EndCombo();
        }
        flash_gate_attempt(state,gated);
    }
    if (state.presentation==Presentation::classic) {
        ImGui::EndDisabled();
        ImGui::SetNextWindowContentSize({classic_px(classic_column_width),0});
        ImGui::BeginChild("classic-i2c-manual", {0,0}, ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize,
            ImGuiWindowFlags_HorizontalScrollbar);
        if (begin_classic_knob_table("manual-command")) {
            ImGui::TableNextRow(); ImGui::TableNextColumn(); ImGui::TextWrapped("Manual voltage command (mV)");
            ImGui::TableNextColumn();
            ImGui::BeginDisabled(!ready(state) || !state.controls_unlocked || !data.gate);
            ImGui::SetNextItemWidth(-1);
            if (ImGui::SliderScalar("##slider",ImGuiDataType_Double,&state.i2c_target_mv,&low,&high," ") &&
                profile && std::isfinite(profile->step_mv) && profile->step_mv>0)
                state.i2c_target_mv=std::clamp(std::round(state.i2c_target_mv/profile->step_mv)*profile->step_mv,low,high);
            flash_gate_attempt(state,gated);
            ImGui::TableNextColumn(); ImGui::SetNextItemWidth(-1);
            ImGui::InputDouble("##manual-mv",&state.i2c_target_mv,0,0,"%.2f");
            flash_gate_attempt(state,gated); ImGui::EndDisabled();
            ImGui::TableNextColumn(); push_classic_mono();
            if (status.requested_mv) ImGui::TextWrapped("%.2f mV%s",*status.requested_mv,status.stale?" *":"");
            else ImGui::TextDisabled("unavailable");
            ImGui::PopFont();
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Stored controller command, not a physical voltage measurement.");
            ImGui::TableNextColumn();
            ImGui::BeginDisabled(!ready(state) || !state.controls_unlocked || !data.gate || !data.verified || !status.writable);
            if (ImGui::Button("Apply",{-1,0})) queue(state,Command::Operation::i2c_apply,state.i2c_target_mv);
            flash_gate_attempt(state,gated); ImGui::EndDisabled();
            ImGui::TableNextColumn();
            ImGui::BeginDisabled(!idle(state) || (!status.owned && !status.recovery_pending));
            if (ImGui::Button("Restore",{-1,0})) queue(state,Command::Operation::i2c_release);
            ImGui::EndDisabled();
            ImGui::EndTable();
        }
        ImGui::EndChild(); return;
    }
    ImGui::SetNextItemWidth(320);
    if (ImGui::SliderScalar("Manual command (mV)",ImGuiDataType_Double,&state.i2c_target_mv,&low,&high,"%.2f") &&
        profile && std::isfinite(profile->step_mv) && profile->step_mv>0)
        state.i2c_target_mv=std::clamp(std::round(state.i2c_target_mv/profile->step_mv)*profile->step_mv,low,high);
    flash_gate_attempt(state,gated);
    ImGui::SameLine(); ImGui::SetNextItemWidth(110); ImGui::InputDouble("##manual-mv",&state.i2c_target_mv,0,0,"%.2f");
    flash_gate_attempt(state,gated); ImGui::EndDisabled();
    ImGui::SameLine(); ImGui::BeginDisabled(!ready(state) || !state.controls_unlocked || !data.gate || !data.verified || !status.writable);
    if (ImGui::Button("Apply")) queue(state,Command::Operation::i2c_apply,state.i2c_target_mv);
    flash_gate_attempt(state,gated);
    ImGui::EndDisabled();
    if (status.requested_mv) ImGui::Text("Command %.2f mV",*status.requested_mv);
}
}
void draw_i2c_gate(ViewState& state) {
    bool enabled=state.live && state.live->data.i2c.gate;
    const bool flashing=state.i2c_gate_flash_until>ImGui::GetTime();
    if (flashing) {
        const float pulse=0.45f+0.35f*float(std::sin(ImGui::GetTime()*25));
        ImGui::PushStyleColor(ImGuiCol_FrameBg,ImVec4(1.0f,0.48f,0.05f,pulse));
        ImGui::PushStyleColor(ImGuiCol_FrameBgHovered,ImVec4(1.0f,0.58f,0.12f,0.9f));
    }
    ImGui::BeginDisabled(!ready(state) || (!state.controls_unlocked && !enabled));
    if (ImGui::Checkbox("I2C rail",&enabled)) { auto c=command(state,Command::Operation::i2c_gate); c.enabled=enabled; state.commands.push_back(c); }
    ImGui::EndDisabled();
    if (flashing) ImGui::PopStyleColor(2);
}
void draw_i2c_panel(ViewState& state) {
    ImGui::PushID("i2c-controller");
    if (!state.live) { ImGui::TextDisabled("I2C transport unavailable"); ImGui::PopID(); return; }
    const auto& data=state.live->data.i2c;
    observe(state,data);
    const auto& status=data.status;
    const bool owned=status.owned || status.recovery_pending || data.clock_owned || data.controller.owned || data.controller.recovery;
    const bool can=ready(state) && data.available;
    if (!data.available) ImGui::TextDisabled("I2C transport unavailable");
    ImGui::BeginDisabled(!can || owned);
    state.i2c_scan_scope=std::clamp(state.i2c_scan_scope,0,int(i2c_family_names.size())-1);
    ImGui::SetNextItemWidth(225);
    ImGui::Combo("IC to scan",&state.i2c_scan_scope,i2c_family_names.data(),int(i2c_family_names.size()));
    ImGui::SameLine();
    if (ImGui::Button("Connect / Scan")) { auto c=command(state,Command::Operation::i2c_scan); c.i2c_family=static_cast<I2cFamily>(state.i2c_scan_scope); state.commands.push_back(c); }
    ImGui::SameLine();
    if (ImGui::Button("Full scan")) { auto c=command(state,Command::Operation::i2c_scan); c.i2c_family=static_cast<I2cFamily>(state.i2c_scan_scope); c.i2c_full_scan=true; state.commands.push_back(c); }
    char selected_label[160]{};
    if (data.selected) std::snprintf(selected_label,sizeof(selected_label),"%s / port %u, 0x%02X",i2c_family_name(data.family),data.selected->port,data.selected->addr7);
    ImGui::SetNextItemWidth(-1);
    if (ImGui::BeginCombo("##controller-candidate",data.selected?selected_label:"Select controller")) {
        for (size_t i=0;i<data.candidates.size();++i) {
            const auto& candidate=data.candidates[i]; const auto family=static_cast<I2cFamily>(candidate.family);
            char label[192]{}; std::snprintf(label,sizeof(label),"%zu: %s - port %u, 0x%02X",i+1,i2c_family_name(family),candidate.route.port,candidate.route.addr7);
            if (ImGui::Selectable(label,data.selected && data.family==family && same_route(*data.selected,candidate.route))) {
                auto c=command(state,Command::Operation::i2c_select); c.i2c_route=candidate.route; c.i2c_family=family; state.commands.push_back(c);
            }
        }
        ImGui::EndCombo();
    }
    if (ImGui::CollapsingHeader("Advanced routing")) {
        ImGui::SetNextItemWidth(80); ImGui::InputInt("Port",&state.i2c_port,0);
        ImGui::SameLine(); ImGui::SetNextItemWidth(80);
        ImGui::InputScalar("Address (hex)",ImGuiDataType_S32,&state.i2c_address,nullptr,nullptr,"%02X",ImGuiInputTextFlags_CharsHexadecimal);
        ImGui::SameLine();
        ImGui::BeginDisabled(state.i2c_scan_scope==0);
        if (ImGui::Button("Probe route") && state.i2c_port>=0 && state.i2c_port<=7 && state.i2c_address>=8 && state.i2c_address<=0x77) {
            auto c=command(state,Command::Operation::i2c_select); c.i2c_family=static_cast<I2cFamily>(state.i2c_scan_scope);
            c.i2c_route=I2cRoute{static_cast<uint8_t>(state.i2c_port),static_cast<uint8_t>(state.i2c_address)}; state.commands.push_back(c);
        }
        ImGui::EndDisabled();
    }
    ImGui::EndDisabled();
    if (data.selected) {
        if (state.presentation==Presentation::classic) classic_family_help(data);
        const auto profile_it=std::find_if(data.control_profiles.begin(),data.control_profiles.end(),[&](const auto& p){return p.id==data.control_profile;});
        const auto* profile=profile_it==data.control_profiles.end()?nullptr:&*profile_it;
        if (data.family==I2cFamily::mp2888) {
            ImGui::BeginDisabled(!can); ImGui::SetNextItemWidth(300);
            if (ImGui::BeginCombo("Control recipe",profile?profile->label.c_str():"Select recipe")) {
                for (const auto& choice : data.control_profiles) {
                    ImGui::BeginDisabled(!choice.available);
                    if (ImGui::Selectable(choice.label.c_str(),choice.id==data.control_profile)) {
                        auto c=command(state,Command::Operation::i2c_recipe); c.i2c_recipe=choice.id; state.commands.push_back(c);
                    }
                    ImGui::EndDisabled();
                }
                ImGui::EndCombo();
            }
            ImGui::EndDisabled();
        }
        const bool icx=data.family==I2cFamily::icx;
        ImGui::BeginDisabled(!can);
        if (ImGui::Button(icx || data.family==I2cFamily::nct3933?"Read settings":"Read controller")) queue(state,Command::Operation::i2c_read);
        ImGui::EndDisabled(); ImGui::SameLine();
        const bool verification_readonly=icx || !data.controller.needs_verification;
        const bool manual_ready=data.control_profile!="mp2888a-pmbus" || (status.capable && profile && profile->available);
        ImGui::BeginDisabled(!can || (!verification_readonly && (!state.controls_unlocked || !data.gate || owned || !manual_ready)));
        if (ImGui::Button(icx?"Check iCX identity":data.verified?"Verify again":"Verify response")) {
            auto c=command(state,Command::Operation::i2c_verify); c.enabled=false; state.commands.push_back(c);
        }
        flash_gate_attempt(state,can && !verification_readonly && !data.gate);
        ImGui::EndDisabled();
        if (data.needs_clock_clamp) ImGui::TextDisabled("Requests hold the graphics clock at the driver's minimum.");
        if (data.family==I2cFamily::nct3933) {
            draw_nct_mapping(state);
            ImGui::BeginDisabled(!can || !state.controls_unlocked || !data.gate || data.controller.recovery);
            if (ImGui::Button("Zero all outputs")) queue(state,Command::Operation::i2c_zero_nct_outputs);
            flash_gate_attempt(state,can && !data.gate);
            ImGui::EndDisabled();
        }
        if (icx) ImGui::TextDisabled("Controller requests, not physical voltage. NVVDD Auto and FBVDD are independent.");
        draw_fields(state,data);
        if (data.family==I2cFamily::mp2888 && data.control_profile=="mp2888a-pmbus") draw_manual(state,data,profile);
        if (status.controller_vout_mv) ImGui::Text("VOUT %.0f mV%s",*status.controller_vout_mv,status.stale?" (last read)":" (controller)");
        for (const auto& reading : data.controller.readings) ImGui::Text("%s: %s",reading.first.c_str(),reading.second.c_str());
        if (icx) {
            ImGui::BeginDisabled(!can);
            if (ImGui::Button("Save current iCX preset")) state.i2c_save_profile_requested=true;
            ImGui::SameLine(); if (ImGui::Button("Load iCX preset")) state.i2c_load_profile_requested=true;
            ImGui::SameLine(); ImGui::BeginDisabled(!data.captured_preset);
            if (ImGui::Button("Restore captured iCX settings")) queue(state,Command::Operation::i2c_stage_captured);
            ImGui::EndDisabled(); ImGui::EndDisabled();
            ImGui::TextDisabled("Restore stages captured settings from this connection; these are not factory defaults.");
            ImGui::TextWrapped("%s",data.preset_preview.empty()?"No iCX preset staged.":data.preset_preview.c_str());
            ImGui::BeginDisabled(!can || !state.controls_unlocked || !data.gate || !data.pending_preset || !data.controller.verified || data.controller.recovery);
            if (ImGui::Button("Apply iCX preset")) queue(state,Command::Operation::i2c_apply_profile);
            flash_gate_attempt(state,can && data.pending_preset && !data.gate);
            ImGui::EndDisabled();
        }
        if (!data.controller.message.empty()) ImGui::TextWrapped("%s",data.controller.message.c_str());
    }
    // Recovery remains reachable when the connection or user unlock was lost.
    ImGui::BeginDisabled(!idle(state) || !owned);
    if (ImGui::Button("Release / restore captured state")) queue(state,Command::Operation::i2c_release);
    ImGui::EndDisabled();
    if (data.clock_owned) ImGui::Text("Owned graphics clamp: %u MHz",data.clock_mhz);
    if (!data.message.empty()) ImGui::TextWrapped("%s",data.message.c_str());
    ImGui::PopID();
}
}
