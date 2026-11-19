// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "app_actions.h"
#include "ui_view.h"
#include "session.h"
#include "classic_layout.h"
#include "imgui.h"
#include "imgui_internal.h"
#include "implot.h"
#include <algorithm>
#include <cmath>
namespace druta::ui {
double clamp_headroom_margin_mv(double value){
    if(!std::isfinite(value))return classic_headroom_default_margin_mv;
    return std::clamp(value,classic_headroom_min_margin_mv,classic_headroom_max_margin_mv);
}
void queue_raised_limits_restore(ViewState& state){
    if(std::none_of(state.commands.begin(),state.commands.end(),[](const Command& c){return c.operation==Command::Operation::restore_raised_limits;})){
        Command c;c.operation=Command::Operation::restore_raised_limits;state.commands.push_back(std::move(c));
    }
}
// The button's restore has its result: 1.9.1 logs the note as its own untagged
// line after "[ok] headroom: ...", when a raise was taken off and the hold is
// still in force. The next completed action after the click is that restore,
// or the session refused it.
static void note_button_restore_result(ViewState& state){
    auto& a=state.app_actions;
    const auto pending=a.headroom_button_restore;
    if(!pending)return;
    const auto live=state.live;
    if(!live||live->generation!=pending->generation){a.headroom_button_restore.reset();return;}
    if(live->completed_actions<=pending->completed_actions)return;
    a.headroom_button_restore.reset();
    if(pending->raised&&live->last_action_operation==Command::Operation::restore_raised_limits&&live->last_action_ok
        &&live->data.tuning.owned&&!live->data.tuning.headroom){
        state.classic_log_lines.push_back({live->last_action_log_id,classic_raised_restored_under_hold_note,std::nullopt});
        if(state.classic_log_lines.size()>200)state.classic_log_lines.erase(state.classic_log_lines.begin());
    }
}
void hold_headroom_changed(ViewState& state){
    auto& a=state.app_actions;
    const auto live=state.live;
    // 1.9.1 headroom_changed: turning the setting off puts the raised limits
    // back, ungated. Classic only: Focused keeps its own behaviour.
    if(!a.headroom_setting_seen)a.headroom_setting_seen=state.hold_headroom;
    else if(*a.headroom_setting_seen!=state.hold_headroom){
        a.headroom_setting_seen=state.hold_headroom;a.headroom_restore_owed.reset();
        if(!state.hold_headroom&&state.presentation==Presentation::classic&&!state.preview&&live)a.headroom_restore_owed=live->generation;
    }
    if(!a.headroom_restore_owed)return;
    if(state.hold_headroom||state.preview||!live||live->generation!=*a.headroom_restore_owed){a.headroom_restore_owed.reset();return;}
    // 1.9.1 has no busy state; the session would refuse the restore while
    // another command runs, so it goes once that command has ended.
    if(live->busy||live->opening)return;
    a.headroom_restore_owed.reset();
    if(live->data.tuning.headroom)queue_raised_limits_restore(state);
}
namespace {
void capture_report(ViewState& state) {
    if(state.live)state.app_actions.report=selected_device_report(*state.live);
}
void readonly_text(const char* id,std::string& text) {
    ImGui::InputTextMultiline(id,text.data(),text.size()+1,{-1,-1},ImGuiInputTextFlags_ReadOnly);
}
}
void draw_device_restart_button(ViewState& state){
    std::string why;const auto target=state.live?restart_device_target(*state.live,why,state.app_actions.administrator):std::optional<AppTarget>{};
    ImGui::BeginDisabled(!target||bool(state.select_slot)||state.app_actions.recovery_preparing);
    if(state.presentation==Presentation::classic){
        // Released 1.9.1 panic_pnp_reset: fills the header's recovery column, 52 px tall, own theme.
        const auto rgb=[](int r,int g,int b){return ImVec4(r/255.0f,g/255.0f,b/255.0f,1);};
        ImGui::PushStyleColor(ImGuiCol_Button,rgb(150,28,32));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,rgb(190,43,48));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,rgb(222,58,63));
        ImGui::PushStyleColor(ImGuiCol_Text,rgb(255,236,236));
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding,classic_px(4));
        if(ImGui::Button(classic_panic_label,ImVec2(-1,classic_px(52))))state.app_actions.restart_device=target;
        ImGui::PopStyleVar();ImGui::PopStyleColor(4);
    }else{
        ImGui::PushStyleColor(ImGuiCol_Button,ImVec4(0.55f,0.08f,0.08f,1));
        if(ImGui::Button("Panic / restart GPU"))state.app_actions.restart_device=target;
        ImGui::PopStyleColor();
    }
    ImGui::EndDisabled();
    if(ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)){ImGui::BeginTooltip();ImGui::PushTextWrapPos(420);
        ImGui::TextWrapped("%s",why.empty()?"Closes Druta, restarts only the selected GPU through Windows PnP, then opens a fresh session. No tuning settings are reapplied.":why.c_str());
        ImGui::PopTextWrapPos();ImGui::EndTooltip();}
}
namespace {
const ImVec4& classic_accent=classic_accent_text;
const ImVec4& classic_dim=classic_dim_text;
const ImVec4 classic_warn{1,203/255.0f,71/255.0f,1};
void classic_menu_hint(const char* text){
    if(ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)){ImGui::BeginTooltip();ImGui::PushTextWrapPos(classic_px(420));
        ImGui::TextUnformatted(text);ImGui::PopTextWrapPos();ImGui::EndTooltip();}
}
// The native refresh and pause controls share the File menu with Exit.
void classic_file_menu(ViewState& state){
    auto& actions=state.app_actions;
    if(!ImGui::BeginMenu("File"))return;
    ImGui::MenuItem("Always refresh",nullptr,&state.always_refresh);
    classic_menu_hint("Off by default. Keep rendering and telemetry running when minimized. Explicit Pause overrides this option.");
    if(ImGui::MenuItem("Pause"))state.pause_requested=true;
    classic_menu_hint(state.hotkey_available
        ? "Pause rendering and telemetry. Restore from the taskbar or Ctrl+Shift+F11."
        : "Pause rendering and telemetry. Restore from the taskbar; the shortcut is unavailable.");
    ImGui::Separator();
    if(ImGui::MenuItem("Exit"))actions.exit_requested=true;
    ImGui::EndMenu();
}
// Released 1.9.1 Profiles menu: Save, Load, then Undo last write with its note.
// The native catalog, history and scoped profile windows follow.
void classic_profiles_menu(ViewState& state){
    if(!ImGui::BeginMenu("Profiles"))return;
    const auto snapshot=state.live;
    const bool ready=!state.preview&&snapshot&&snapshot->data.connected&&!snapshot->busy&&!snapshot->opening;
    if(ImGui::MenuItem("Save profile...",nullptr,false,ready))state.save_profile_requested=true;
    classic_menu_hint("Capture current requests to a file you select. Saving does not apply tuning.");
    if(ImGui::MenuItem("Load profile...",nullptr,false,ready&&state.controls_unlocked))state.load_profile_requested=true;
    classic_menu_hint("Select a saved profile to apply explicitly in this session. Unlock tuning controls first.");
    ImGui::Separator();
    const bool metadata=snapshot&&snapshot->data.undo_metadata_only;
    const bool undo=!state.preview&&snapshot&&snapshot->data.undo_available&&(state.controls_unlocked||metadata)
        &&(snapshot->data.connected||metadata)&&!snapshot->busy&&!snapshot->opening;
    if(ImGui::MenuItem(metadata?"Undo label update":"Undo last write",nullptr,false,undo))
        state.commands.push_back({Command::Operation::undo,ControlId::memory,0});
    if(snapshot&&!snapshot->data.undo_label.empty())classic_menu_hint(snapshot->data.undo_label.c_str());
    // 1.9.1 adds "It is a write itself, so it takes one too." Native Undo takes
    // no snapshot of its own (it consumes the entry), so that line is not ported.
    ImGui::TextColored(classic_dim,"restores the snapshot taken automatically\nimmediately before the most recent write.");
    ImGui::Separator();
    // The catalog opens in any state, as the former Classic header button did;
    // the catalog itself gates each action on the session.
    if(ImGui::MenuItem("Profile catalog..."))state.profile_catalog_view.open=true;
    ImGui::BeginDisabled(state.preview||!snapshot);
    if(ImGui::MenuItem("Undo from history...")){
        state.profile_catalog_view.open=true;
        state.profile_catalog_intents.push_back({ProfileCatalogIntent::Operation::stage_newest_history,{},{},{}});
    }
    if(ImGui::MenuItem("Rail / power profiles..."))state.scoped_profile_view.open=true;
    ImGui::EndDisabled();
    ImGui::EndMenu();
}
// Released 1.9.1 Device menu order and wording. The native-only Save device
// report and timings page entries sit beside their 1.9.1 neighbours.
void classic_device_menu(ViewState& state){
    auto& actions=state.app_actions;
    const auto snapshot=state.live;
    if(ImGui::BeginMenu("Open a second window on")){
        if(!snapshot||snapshot->devices.empty())ImGui::TextColored(ImVec4(1,92/255.0f,92/255.0f,1),"no NVIDIA GPU enumerated");
        else for(const auto& device:snapshot->devices){
            // 1.9.1 opens a second window on any listed card. The selected card
            // goes with its verified session identity, another card with its
            // enumerated slot and UUID; the child refuses an adapter whose UUID differs.
            std::string why;const auto target=second_window_card_target(*snapshot,device.slot,why);
            ImGui::BeginDisabled(!target||bool(state.select_slot));
            if(ImGui::MenuItem((device.name+"   "+device.slot).c_str()))actions.second_window=target;
            ImGui::EndDisabled();
            if(!target&&!why.empty())classic_menu_hint(why.c_str());
        }
        ImGui::TextColored(classic_dim,"for watching both at once, or holding\na lock on one while tuning the other");
        ImGui::EndMenu();
    }
    ImGui::Separator();
    ImGui::BeginDisabled(!snapshot||bool(state.select_slot));
    if(ImGui::MenuItem("Device report...")){capture_report(state);actions.report_open=true;}
    // 1.9.1 copy_device_report confirms the copy in the log, not in a window.
    if(ImGui::MenuItem("Copy device report")){
        capture_report(state);ImGui::SetClipboardText(actions.report->text.c_str());
        state.classic_log_lines.push_back({snapshot->log.empty()?0:snapshot->log.back().id,"device report copied to clipboard",true});
        if(state.classic_log_lines.size()>200)state.classic_log_lines.erase(state.classic_log_lines.begin());
    }
    if(ImGui::MenuItem("Save device report...")){capture_report(state);actions.save_report_requested=true;}
    ImGui::EndDisabled();
    ImGui::BeginDisabled(!snapshot||snapshot->opening||snapshot->busy||bool(state.select_slot));
    if(ImGui::MenuItem("Refresh capabilities"))state.refresh_requested=true;
    ImGui::EndDisabled();
    std::string why;const auto restart=snapshot?restart_device_target(*snapshot,why,actions.administrator):std::optional<AppTarget>{};
    ImGui::BeginDisabled(!restart||bool(state.select_slot)||actions.recovery_preparing);
    if(ImGui::MenuItem("Restart GPU device (PnP)..."))actions.restart_device=restart;
    ImGui::EndDisabled();
    ImGui::Separator();
    ImGui::BeginDisabled(!snapshot||snapshot->busy||snapshot->opening);
    if(ImGui::MenuItem("Locate nvtune..."))state.timing_helper_requested=true;
    if(ImGui::MenuItem("Forget nvtune location"))state.timing_forget_requested=true;
    ImGui::EndDisabled();
    if(ImGui::MenuItem("Enable test signing..."))state.test_signing_requested=true;
    if(ImGui::MenuItem("Memory timings / helper setup")){
        state.page=Page::timings;
        if(state.classic_floating[3])state.classic_float_focus[3]=true;
    }
    ImGui::Separator();
    if(ImGui::MenuItem("Shunt mod (corrected power)..."))state.shunt.open=true;
}
// Released 1.9.1 Clocks menu: Limited de-flatten and the voltage-limit
// headroom setting inline, then the native lock and policy windows.
void classic_clocks_body(ViewState& state){
    const bool curve=!state.preview&&state.live&&!state.live->data.curve.empty();
    if(curve){
        ImGui::TextColored(classic_accent,"LIMITED DE-FLATTEN");
        ImGui::TextColored(classic_dim,"Makes only the cap point the unique top; leaves\nthe rest of the band alone. Niche - the V/F tab's\n'De-flatten' rebuilds the whole band and is the\none to reach for first.");
        ImGui::BeginDisabled(!state.limited_deflatten_ready);
        if(ImGui::Button("Limited de-flatten \xE2\x89\xA4 cap",ImVec2(classic_px(230),0))){
            state.limited_deflatten_requested=true;ImGui::CloseCurrentPopup();
        }
        ImGui::EndDisabled();
        classic_menu_hint("Stages a plan in the V/F editor; nothing is written until Apply.");
        ImGui::Separator();
        ImGui::TextColored(classic_accent,"VOLTAGE-LIMIT HEADROOM");
        // The core's generation answer: false hides the setting, unset is "try again".
        const auto architecture=state.live->data.hold_headroom_architecture;
        if(architecture&&!*architecture){
            // 1.9.1 goes on to name its log clock-gap check, which this build does not have.
            ImGui::TextColored(classic_dim,"Not available: Druta cannot change this GPU\ngeneration's voltage limits.");
            ImGui::Separator();
        }else{
            if(!architecture)ImGui::TextColored(classic_warn,"GPU architecture not read yet - headroom is\ntried again at the next hold.");
            if(ImGui::Checkbox("Keep headroom above a held point (strongly recommended)",&state.hold_headroom))hold_headroom_changed(state);
            ImGui::SetNextItemWidth(classic_px(130));
            // 1.9.1 min/max_clamped on entry: the field returns true when it is left or stepped.
            if(ImGui::InputDouble("margin mV",&state.headroom_margin_mv,classic_headroom_min_margin_mv,0,"%.2f"))
                state.headroom_margin_mv=clamp_headroom_margin_mv(state.headroom_margin_mv);
            ImGui::TextColored(classic_dim,"While Ctrl+H or Max it holds a point, the voltage\n"
                "limits are raised to the held voltage + margin, so\n"
                "the hold is not sitting on the ceiling. A point held\n"
                "ON the ceiling runs slower than the clock it shows.\n"
                "The held point's voltage stays the same; the rail\n"
                "may run up to the margin higher (one TITAN RTX ran\n"
                "its rail up to 18.75 mV higher, one RTX 3070 Ti the\n"
                "full 25 mV). The limits go back when the hold ends.\n"
                "A point ABOVE the ceiling gets no raise (the card\n"
                "would climb past the ceiling you set, by more than\n"
                "the margin) - you get a warning instead.\n"
                "Where the card reports a live rail voltage it is\n"
                "watched: above the raised ceiling (beyond this\n"
                "card's own reading offset), the raise is undone.\n"
                "25 mV is a fixed safe margin for every card, not\n"
                "tuned per card or generation. On one TITAN RTX and\n"
                "one RTX 3070 Ti it removed the loss.");
            // A Dear PyGui button leaves its menu open; the result goes to the log.
            // Disabled while a command runs or the GPU opens, where the session
            // would refuse it, as every native write control is.
            ImGui::BeginDisabled(state.live->busy||state.live->opening);
            if(ImGui::Button("Restore raised limits now",ImVec2(classic_px(230),0))){
                queue_raised_limits_restore(state);
                const auto& live=*state.live;
                state.app_actions.headroom_button_restore=AppActionsState::HeadroomButtonRestore{live.generation,live.completed_actions,live.data.tuning.headroom};
            }
            ImGui::EndDisabled();
            ImGui::Separator();
        }
    }
    if(!state.preview&&state.live){
        queue_frequency_lock_intents(state,draw_frequency_lock_menu(state.frequency_view,state.live->data.frequency,
            frequency_lock_context(state),std::max<int64_t>(1,std::llround(state.live->data.curve_step_khz/1000.0)),curve,state.live->data.curve_step_khz));
        // 1.9.1's Clocks > Release drops a V/F point hold too. The native Release
        // above names only the clock request, so the hold the gate row no longer
        // shows keeps its own release here, beside Ctrl+H and Reset.
        const auto& live=*state.live;
        if(live.data.tuning.maintenance_needed()){
            const bool queued=std::any_of(state.commands.begin(),state.commands.end(),[](const Command& c){
                return c.operation==Command::Operation::hold_curve_point||c.operation==Command::Operation::release_hold;});
            ImGui::BeginDisabled(live.busy||queued);
            if(ImGui::Button("Release hold (Ctrl+H)",ImVec2(classic_px(230),0))){
                Command c;c.operation=Command::Operation::release_hold;state.commands.push_back(std::move(c));
            }
            ImGui::EndDisabled();
            classic_menu_hint("Release this session's V/F point hold and take its headroom raise off.");
        }
        ImGui::Separator();
    }
}
}
void draw_app_actions_menu(ViewState& state) {
    // Each frame, whether or not the bar draws: the Clocks menu's headroom
    // restore and its note follow the setting and the session.
    note_button_restore_result(state);hold_headroom_changed(state);
    if(!ImGui::BeginMenuBar())return;
    auto& actions=state.app_actions;
    const bool classic=state.presentation==Presentation::classic;
    if(classic)classic_file_menu(state);
    else if(ImGui::BeginMenu("File")) {
        ImGui::BeginDisabled(state.preview || !state.live);
        if(ImGui::MenuItem("Profile catalog..."))state.profile_catalog_view.open=true;
        if(ImGui::MenuItem("Undo from history...")){
            state.profile_catalog_view.open=true;
            state.profile_catalog_intents.push_back({ProfileCatalogIntent::Operation::stage_newest_history,{},{},{}});
        }
        if(ImGui::MenuItem("Rail / power profiles..."))state.scoped_profile_view.open=true;
        ImGui::EndDisabled();
        ImGui::Separator();
        if(ImGui::MenuItem("Exit"))actions.exit_requested=true;
        ImGui::EndMenu();
    }
    if(classic){
        if(ImGui::BeginMenu("Device")){classic_device_menu(state);ImGui::EndMenu();}
    }
    else if(ImGui::BeginMenu("Device")) {
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
        if(ImGui::MenuItem("Forget nvtune location"))state.timing_forget_requested=true;
        ImGui::EndDisabled();
        if(ImGui::MenuItem("Test-signing setup..."))state.test_signing_requested=true;
        if(ImGui::MenuItem("Shunt mod (corrected power)..."))state.shunt.open=true;
        const auto restart=state.live?restart_device_target(*state.live,why,state.app_actions.administrator):std::optional<AppTarget>{};
        ImGui::BeginDisabled(!restart||bool(state.select_slot)||actions.recovery_preparing);if(ImGui::MenuItem("Restart selected GPU (PnP)"))actions.restart_device=restart;ImGui::EndDisabled();
        ImGui::EndMenu();
    }
    if(classic)classic_profiles_menu(state);
    if(ImGui::BeginMenu("Clocks")) {
        if(classic)classic_clocks_body(state);
        ImGui::BeginDisabled(state.preview || !state.live);
        if(ImGui::MenuItem("Frequency locks / legacy P0..."))state.frequency_view.open=true;
        if(ImGui::MenuItem("Voltage admission / V/F policies...")){state.page=Page::advanced;state.open_voltage_policy_requested=true;if(state.classic_floating[1])state.classic_float_focus[1]=true;}
        if(ImGui::MenuItem("Power feedback ratios...")){state.page=Page::advanced;state.open_power_integral_requested=true;if(state.classic_floating[1])state.classic_float_focus[1]=true;}
        if(ImGui::MenuItem("Power saving...")){state.page=Page::advanced;state.open_power_saving_requested=true;if(state.classic_floating[1])state.classic_float_focus[1]=true;}
        if(ImGui::MenuItem("Clock gating / PCIe...")){state.page=Page::advanced;state.open_clock_gating_requested=true;if(state.classic_floating[1])state.classic_float_focus[1]=true;}
        ImGui::EndDisabled();
        ImGui::EndMenu();
    }
    if (classic && ImGui::BeginMenu("Skin")) {
        ImGui::MenuItem("Verbose (Classic)", nullptr, true);
        if (ImGui::MenuItem("Focused")) state.presentation = Presentation::focused;
        ImGui::EndMenu();
    }
    if(ImGui::BeginMenu("Help")) {
        // 1.9.1 lists Keyboard shortcuts only where a V/F curve applies; Classic
        // uses the same curve test as its Clocks menu.
        const bool shortcuts=!classic||(!state.preview&&state.live&&!state.live->data.curve.empty());
        if(shortcuts&&ImGui::MenuItem("Keyboard shortcuts"))actions.shortcuts_open=true;
        if(ImGui::MenuItem(classic?"Licences":"Licences / bundled sources"))actions.licences_open=true;
        if(ImGui::MenuItem(classic?"About":"About Druta"))actions.about_open=true;
        ImGui::EndMenu();
    }
    ImGui::EndMenuBar();
    if(classic){
        // Dear PyGui's viewport menu bar is its own bordered window: a 1 px
        // border-colour line closes the bar's last row.
        auto* window=ImGui::GetCurrentWindow();const ImRect bar=window->MenuBarRect();
        window->DrawList->PushClipRect(window->Rect().Min,window->Rect().Max);
        window->DrawList->AddRectFilled({bar.Min.x,bar.Max.y-1},bar.Max,ImGui::GetColorU32(ImGuiCol_Border));
        window->DrawList->PopClipRect();
    }
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
            // Classic opens a second window on any enumerated card (1.9.1); Focused only on the selected one.
            ImGui::TextWrapped("Tuning is session-only. Saved settings require an explicit load. %s",state.presentation==Presentation::classic
                ?"A second window opens one explicit PCI slot with that card's expected GPU UUID and receives no startup tuning settings."
                :"A second window opens only the selected PCI slot with an expected GPU UUID and receives no startup tuning settings.");
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
