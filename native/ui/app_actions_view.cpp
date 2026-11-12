// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "app_actions.h"
#include "ui_view.h"
#include "session.h"
#include "classic_layout.h"
#include "imgui.h"
#include "implot.h"
namespace druta::ui {
namespace {
void capture_report(ViewState& state) {
    if(state.live)state.app_actions.report=selected_device_report(*state.live);
}
void readonly_text(const char* id,std::string& text) {
    ImGui::InputTextMultiline(id,text.data(),text.size()+1,{-1,-1},ImGuiInputTextFlags_ReadOnly);
}
}
void draw_app_actions_menu(ViewState& state) {
    if(!ImGui::BeginMenuBar())return;
    auto& actions=state.app_actions;
    if(ImGui::BeginMenu("File")) {
        ImGui::BeginDisabled(state.preview || !state.live);
        if(ImGui::MenuItem("Rail / power profiles..."))state.scoped_profile_view.open=true;
        ImGui::EndDisabled();
        ImGui::Separator();
        if(ImGui::MenuItem("Exit"))actions.exit_requested=true;
        ImGui::EndMenu();
    }
    if(ImGui::BeginMenu("Device")) {
        ImGui::BeginDisabled(!state.live || bool(state.select_slot));
        if(ImGui::MenuItem("Device report...")){capture_report(state);actions.report_open=true;}
        if(ImGui::MenuItem("Copy device report")){capture_report(state);ImGui::SetClipboardText(actions.report->text.c_str());actions.status="Selected-device report copied.";}
        if(ImGui::MenuItem("Save device report...")){capture_report(state);actions.save_report_requested=true;}
        ImGui::EndDisabled();
        ImGui::BeginDisabled(!state.live || state.live->opening || state.live->busy || bool(state.select_slot));
        if(ImGui::MenuItem("Refresh capabilities"))state.refresh_requested=true;
        ImGui::EndDisabled();
        std::string why;const auto target=state.live?second_window_target(*state.live,why):std::optional<AppTarget>{};
        ImGui::BeginDisabled(!target || bool(state.select_slot));
        if(ImGui::MenuItem("Open selected GPU in a second window"))actions.second_window=target;
        ImGui::EndDisabled();
        if(!target && !why.empty())ImGui::TextDisabled("%s",why.c_str());
        ImGui::Separator();
        if(ImGui::MenuItem("Memory timings / helper setup")) {
            state.page=Page::timings;
            if(state.classic_floating[3])state.classic_float_focus[3]=true;
        }
        ImGui::BeginDisabled(!state.live || state.live->busy || state.live->opening);
        if(ImGui::MenuItem("Locate nvtune..."))state.timing_helper_requested=true;
        ImGui::EndDisabled();
        if(ImGui::MenuItem("Shunt mod (corrected power)..."))state.shunt.open=true;
        ImGui::EndMenu();
    }
    if(ImGui::BeginMenu("Help")) {
        if(ImGui::MenuItem("Keyboard shortcuts"))actions.shortcuts_open=true;
        if(ImGui::MenuItem("Licences / bundled sources"))actions.licences_open=true;
        if(ImGui::MenuItem("About Druta"))actions.about_open=true;
        ImGui::EndMenu();
    }
    ImGui::EndMenuBar();
}
void draw_app_actions_windows(ViewState& state) {
    auto& a=state.app_actions;
    if(a.report_open) {
        ImGui::SetNextWindowSize({780,560},ImGuiCond_FirstUseEver);
        if(ImGui::Begin("Selected-device report###druta_device_report",&a.report_open,ImGuiWindowFlags_NoSavedSettings)) {
            if(!a.report)capture_report(state);
            if(a.report) {
                if(state.live && !app_target_matches(a.report->target,*state.live))
                    ImGui::TextWrapped("The selected adapter or session changed. This report remains the explicitly captured earlier snapshot; capture again to save the current adapter.");
                ImGui::BeginDisabled(!state.live || bool(state.select_slot));
                if(ImGui::Button("Capture selected snapshot"))capture_report(state);
                ImGui::EndDisabled();ImGui::SameLine();
                if(ImGui::Button("Copy report")){ImGui::SetClipboardText(a.report->text.c_str());a.status="Captured report copied.";}
                ImGui::SameLine();
                ImGui::BeginDisabled(!state.live || bool(state.select_slot) || !app_target_matches(a.report->target,*state.live));
                if(ImGui::Button("Save report..."))a.save_report_requested=true;
                ImGui::EndDisabled();
                ImGui::TextWrapped("%s",a.status.c_str());
                push_classic_mono();readonly_text("##device-report",a.report->text);ImGui::PopFont();
            } else ImGui::TextDisabled("No selected session snapshot is available.");
        }ImGui::End();
    }
    if(a.shortcuts_open) {
        ImGui::SetNextWindowSize({700,400},ImGuiCond_FirstUseEver);
        if(ImGui::Begin("Keyboard shortcuts###druta_shortcuts",&a.shortcuts_open,ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextWrapped("Classic edits the retained curve selection across this window, including other pages and a collapsed editor. Focused edits only inside its focused curve editor. Text entry and active widgets retain their own keys.");
            const double bin=state.live?state.live->data.curve_step_khz/1000.0:0;
            if(bin>0)ImGui::Text("W / S: +/- %.9g MHz; Shift: +/- %.9g MHz",bin,3*bin);
            else ImGui::TextDisabled("W / S: one card-reported bin (unavailable until a curve is read)");
            ImGui::TextUnformatted("A / D: previous / next actual point; Shift: three points");
            ImGui::TextWrapped("Ctrl+Z: undo staged edit; Ctrl+Y or Ctrl+Shift+Z: redo staged edit. These never write to the GPU. Undo hardware write is a separate explicit action.");
            ImGui::TextWrapped("Ctrl+H: hold selected configured point or release this session's owned hold. Eligibility, unlock, ownership and readback checks still apply.");
            ImGui::TextWrapped("Drag a large dot vertically to edit; drag empty plot space to pan; wheel to zoom; Fit view restores bounds. Classic tabs drag off the selector to float; Dock back or closing returns the page.");
            if(state.hotkey_available)ImGui::TextUnformatted("Ctrl+Shift+F11: restore the paused/minimized window");
            else ImGui::TextDisabled("Restore shortcut unavailable; use the taskbar.");
        }ImGui::End();
    }
    if(a.licences_open) {
        ImGui::SetNextWindowSize({850,610},ImGuiCond_FirstUseEver);
        if(ImGui::Begin("Licences and bundled sources###druta_licences",&a.licences_open,ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextWrapped("These texts are read from the files bundled beside this executable. Missing sources are reported explicitly.");
            if(ImGui::BeginTabBar("bundled-notices")) {
                for(auto& notice:a.notices)if(ImGui::BeginTabItem(notice.title.c_str())) {
                    ImGui::Text("Source: %s%s",notice.source.c_str(),notice.available?"":" (unavailable)");
                    if(ImGui::Button("Copy source text"))ImGui::SetClipboardText(notice.text.c_str());
                    readonly_text("##notice",notice.text);ImGui::EndTabItem();
                }
                ImGui::EndTabBar();
            }
            if(a.notices.empty())ImGui::TextDisabled("Bundled files have not been loaded by this host.");
        }ImGui::End();
    }
    if(a.about_open) {
        ImGui::SetNextWindowSize({650,360},ImGuiCond_FirstUseEver);
        if(ImGui::Begin("About Druta###druta_about",&a.about_open,ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextUnformatted("Thermetery Druta 1.9.1 — native frontend");
            ImGui::TextUnformatted("Copyright (C) 2026 Thermetery Technology Co Limited");
            ImGui::TextWrapped("GNU General Public License, version 3 or later. See Licences for the complete bundled COPYING text and third-party notices.");
            ImGui::Text("Dear ImGui %s / ImPlot %s / nlohmann JSON",IMGUI_VERSION,IMPLOT_VERSION);
            ImGui::TextWrapped("The native frontend uses the selected drutacore DLL through its C API. Reported capability and telemetry availability belong to the current adapter; incomplete evidence is shown explicitly.");
            ImGui::TextWrapped("Tuning is session-only. Saved settings require an explicit load. A second window opens only the selected PCI slot with an expected GPU UUID and receives no startup tuning settings.");
            if(ImGui::Button("View licences / bundled sources"))a.licences_open=true;
        }ImGui::End();
    }
    if(!a.status.empty() && !a.report_open) {
        bool open=true;ImGui::SetNextWindowSize({620,140},ImGuiCond_FirstUseEver);
        if(ImGui::Begin("Druta action result###druta_app_action_result",&open,ImGuiWindowFlags_NoSavedSettings))
            ImGui::TextWrapped("%s",a.status.c_str());
        ImGui::End();if(!open)a.status.clear();
    }
}
}
