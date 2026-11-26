// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "app_actions.h"
#include "app_version.h"
#include "profile_startup.h"
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
void classic_profile_actions(ViewState& state){
    const auto snapshot=state.live;
    const bool ready=!state.preview&&snapshot&&snapshot->data.connected&&!snapshot->busy&&!snapshot->opening;
    if(ImGui::MenuItem("Save profile...",nullptr,false,ready))state.save_profile_requested=true;
    classic_menu_hint("Capture current requests to a file you select. Saving does not apply tuning.");
    if(ImGui::MenuItem("Load profile...",nullptr,false,ready&&state.controls_unlocked))state.load_profile_requested=true;
    classic_menu_hint("Select a saved profile to apply explicitly in this session. Unlock tuning controls first.");
    if (!state.startup_profile_id.empty()) {
        if (ImGui::MenuItem("Disable startup profile", nullptr, false, !state.startup_profile_request.has_value())) {
            state.startup_profile_request = false;
            state.startup_profile_status.clear();
        }
        classic_menu_hint("Stop applying the configured profile at sign-in");
    }
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
    if(ImGui::MenuItem("Profile catalog..."))state.profile_catalog_view.open=true;
    ImGui::BeginDisabled(state.preview||!snapshot);
    if(ImGui::MenuItem("Undo from history...")){
        state.profile_catalog_view.open=true;
        state.profile_catalog_intents.push_back({ProfileCatalogIntent::Operation::stage_newest_history,{},{},{}});
    }
    if(ImGui::MenuItem("Rail / power profiles..."))state.scoped_profile_view.open=true;
    ImGui::EndDisabled();
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
    if(ImGui::MenuItem("Refresh capabilities"))state.capability_refresh_requested=true;
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

const ProfileLibraryEntry* named_profile(const ViewState& state, const std::string& id) {
    if (id.empty()) return nullptr;
    const auto& library = state.profile_catalog_view.library;
    const auto entry = std::find_if(library.begin(), library.end(), [&](const auto& item) {
        return !item.autosave && item.id == id;
    });
    return entry == library.end() ? nullptr : &*entry;
}

const ProfileLibraryEntry* native_profile_with_stem(const ViewState& state, const std::string& stem) {
    const auto& library = state.profile_catalog_view.library;
    const auto entry = std::find_if(library.begin(), library.end(), [&](const auto& item) {
        return item.source == "Native" && ImStricmp(item.name.c_str(), stem.c_str()) == 0;
    });
    return entry == library.end() ? nullptr : &*entry;
}

ProfileScope capture_scope(const ProfileLibraryEntry& entry) {
    return entry.description.scope == ProfileScope::python_full_tune ? ProfileScope::full_tune : entry.description.scope;
}

bool same_capture_gpu(const ProfileLibraryEntry& entry, const SessionSnapshot& live) {
    return entry.document && entry.error.empty() && entry.description.valid &&
        entry.description.slot == live.selected_slot && entry.description.uuid == live.data.identity.uuid;
}

void suggest_save_defaults(ViewState& state) {
    auto& draft = state.toolbar_save_draft;
    const auto& selected = state.classic_profile_bar.selected;
    const auto* entry = draft.slot ? named_profile(state, *draft.expected_slot_id)
                                   : selected ? named_profile(state, selected->id) : nullptr;
    std::string name;
    if (!draft.scope_chosen) draft.scope = ProfileScope::daily;
    if (entry && same_capture_gpu(*entry, *state.live)) {
        const auto scope = capture_scope(*entry);
        if (scope == ProfileScope::daily || scope == ProfileScope::curve || scope == ProfileScope::full_tune) {
            if (!draft.scope_chosen) draft.scope = scope;
            if (draft.scope == scope) name = entry->name;
        }
    }
    if (draft.slot && name.empty() && !draft.name_edited) {
        const auto base = "Profile " + std::to_string(*draft.slot + 1);
        name = base;
        for (unsigned suffix = 2; native_profile_with_stem(state, profile_library_named_stem(name)); ++suffix)
            name = base + "-" + std::to_string(suffix);
    }
    if (!draft.name_edited) ImStrncpy(draft.name.data(), name.c_str(), draft.name.size());
}

void select_save_destination(ViewState& state, std::optional<std::size_t> slot) {
    auto& draft = state.toolbar_save_draft;
    draft.slot = slot;
    draft.expected_slot_id = slot ? std::optional<std::string>(state.quick_profiles.ids[*slot]) : std::nullopt;
    suggest_save_defaults(state);
}

void open_profile_save(ViewState& state, std::optional<std::size_t> slot) {
    auto& draft = state.toolbar_save_draft;
    draft = {};
    draft.generation = state.live->generation;
    draft.awaiting_library = state.profile_catalog_view.library_loading;
    if (state.quick_profiles.save_scope) {
        draft.scope = *state.quick_profiles.save_scope;
        draft.scope_chosen = true;
    }
    select_save_destination(state, slot ? slot : state.quick_profiles.save_slot);
    state.toolbar_save_status.clear();
    ImGui::OpenPopup("##classic-profile-save-dialog");
}

void draw_profile_save_popup(ViewState& state, bool ready) {
    if (!ImGui::IsPopupOpen("##classic-profile-save-dialog")) return;
    const float width = std::min(classic_px(320), ImGui::GetMainViewport()->WorkSize.x);
    ImGui::SetNextWindowSize({width, 0});
    if (!ImGui::BeginPopup("##classic-profile-save-dialog")) return;
    auto& draft = state.toolbar_save_draft;
    if (!state.live || state.live->generation != draft.generation || state.select_slot) {
        ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
        return;
    }
    if (draft.awaiting_library && !state.profile_catalog_view.library_loading) {
        draft.awaiting_library = false;
        suggest_save_defaults(state);
    }
    const auto slot_label = [&](std::size_t slot) {
        const auto& id = state.quick_profiles.ids[slot];
        const auto* entry = named_profile(state, id);
        return "Slot " + std::to_string(slot + 1) + "  " +
            (entry ? entry->name : id.empty() ? "Unassigned" : "Profile unavailable");
    };
    ImGui::TextUnformatted("Profile name");
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::IsWindowAppearing() && !draft.slot && !draft.name[0]) ImGui::SetKeyboardFocusHere();
    if (ImGui::InputText("##save-profile-name", draft.name.data(), draft.name.size()))
        draft.name_edited = true;
    ImGui::TextUnformatted("Capture");
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##save-profile-scope", profile_scope_name(draft.scope))) {
        for (auto scope : {ProfileScope::daily, ProfileScope::curve, ProfileScope::full_tune}) {
            if (ImGui::Selectable(profile_scope_name(scope), draft.scope == scope)) {
                draft.scope = scope;
                draft.scope_chosen = true;
                state.toolbar_save_preference_change.scope = scope;
                suggest_save_defaults(state);
            }
        }
        ImGui::EndCombo();
    }
    ImGui::TextUnformatted("Save to");
    ImGui::SetNextItemWidth(-FLT_MIN);
    const auto destination = draft.slot ? slot_label(*draft.slot) : std::string("Library");
    if (ImGui::BeginCombo("##save-profile-destination", destination.c_str())) {
        const auto choose = [&](std::optional<std::size_t> slot) {
            select_save_destination(state, slot);
            state.toolbar_save_preference_change.destination_changed = true;
            state.toolbar_save_preference_change.slot = slot;
        };
        if (ImGui::Selectable("Library", !draft.slot)) choose(std::nullopt);
        for (std::size_t i = 0; i < state.quick_profiles.ids.size(); ++i) {
            if (ImGui::Selectable(slot_label(i).c_str(), draft.slot == i)) choose(i);
        }
        ImGui::EndCombo();
    }

    const std::string name = draft.name.data();
    std::string stem, problem;
    const ProfileLibraryEntry* existing = nullptr;
    const bool has_name = name.find_first_not_of(" \t\r\n") != std::string::npos;
    if (has_name) {
        try { stem = profile_library_named_stem(name); }
        catch (const std::exception& e) { problem = e.what(); }
        if (!stem.empty()) existing = native_profile_with_stem(state, stem);
        if (existing && (existing->autosave || !same_capture_gpu(*existing, *state.live) || capture_scope(*existing) != draft.scope))
            problem = "This name is in use by a different profile. Choose another name.";
    }
    if (state.profile_catalog_view.library_loading) problem = "Reading saved profiles...";
    else if (draft.slot && draft.expected_slot_id != state.quick_profiles.ids[*draft.slot])
        problem = "The slot assignment changed. Choose the destination again.";
    if (!problem.empty()) ImGui::TextWrapped("%s", problem.c_str());
    else if (has_name && name != stem) ImGui::TextDisabled("%s.json", stem.c_str());

    ImGui::BeginDisabled(!ready || !has_name || !problem.empty());
    if (ImGui::Button(existing ? "Update profile" : "Save profile")) {
        ViewState::ToolbarSaveRequest request{draft.generation, name, draft.slot, {}, draft.scope, {}, draft.expected_slot_id};
        if (existing) {
            request.replace_id = existing->id;
            request.replace_document = existing->document;
        }
        state.toolbar_save_request = std::move(request);
        ImGui::CloseCurrentPopup();
    }
    ImGui::EndDisabled();
    ImGui::SameLine();
    if (ImGui::Button("Cancel")) ImGui::CloseCurrentPopup();
    ImGui::EndPopup();
}
}
void draw_classic_profile_bar(ViewState& state) {
    auto& catalog = state.profile_catalog_view;
    auto& bar = state.classic_profile_bar;
    using ApplyState = ViewState::ClassicProfileBar::ApplyState;
    const auto& live = state.live;
    if (bar.selected && (!live || bar.selected->generation != live->generation)) bar = {};
    const bool connected = !state.preview && live && live->data.connected;
    const bool ready = connected && !live->busy && !live->opening && !state.select_slot
        && bar.apply != ApplyState::running;
    const bool can_save = ready && !state.toolbar_save_pending && !state.toolbar_save_request &&
        !state.quick_profile_assignment;
    const auto refresh_library = [&] {
        if (catalog.library_loading) return;
        catalog.library_loading = true;
        state.profile_catalog_intents.push_back({ProfileCatalogIntent::Operation::refresh_library});
    };
    if (connected && !state.quick_library_requested) {
        state.quick_library_requested = true;
        refresh_library();
    }

    const auto& slots = state.quick_profiles.ids;
    const auto find_profile = [&](const std::string& id) -> const ProfileLibraryEntry* {
        return named_profile(state, id);
    };
    const auto selectable = [](const ProfileLibraryEntry* entry) {
        return entry && entry->document && entry->error.empty() && entry->description.valid;
    };
    const auto metadata_only = [](const ProfileLibraryEntry& entry) {
        return entry.description.targets.size() == 1 && entry.description.targets.front() == "annotation:power";
    };
    const auto select = [&](const ProfileLibraryEntry& entry) {
        bar.selected = ViewState::ClassicProfileBar::Selection{
            live->generation, entry.id, entry.name, *entry.document, metadata_only(entry)};
        bar.apply = ApplyState::idle;
        bar.status.clear();
    };
    const auto apply = [&] {
        const auto& selected = *bar.selected;
        state.profile_catalog_intents.push_back({
            ProfileCatalogIntent::Operation::apply, {}, selected.document, {}, selected.generation});
        bar.apply = ApplyState::running;
        bar.status.clear();
    };
    const auto profile_hint = [&](const ProfileLibraryEntry* entry) {
        if (!entry) return "The saved profile is unavailable";
        if (selectable(entry)) return entry->source.c_str();
        if (!entry->error.empty()) return entry->error.c_str();
        return !entry->description.error.empty() ? entry->description.error.c_str() : "Unreadable profile";
    };

    ImGui::AlignTextToFramePadding();
    ImGui::TextColored(classic_accent, "Profile");
    ImGui::SameLine();
    const auto& style = ImGui::GetStyle();
    const char* actions = "Profile actions";
    const char* startup = "Apply on startup";
    const float spacing = style.ItemSpacing.x;
    const float apply_width = ImGui::CalcTextSize("Apply").x + 2 * style.FramePadding.x;
    const char* save = "Save...";
    const float save_width = ImGui::CalcTextSize(save).x + 2 * style.FramePadding.x;
    const float actions_width = ImGui::CalcTextSize(actions).x + 2 * style.FramePadding.x;
    const float startup_width = ImGui::CalcTextSize(startup).x + ImGui::GetFrameHeight() + style.ItemInnerSpacing.x;
    const float slot_width = std::max(classic_px(28), ImGui::CalcTextSize("5").x + 2 * style.FramePadding.x);
    const float quick_space = static_cast<float>(slots.size()) * (slot_width + spacing);
    const float actions_space = apply_width + save_width + actions_width + 3 * spacing;
    const float available = ImGui::GetContentRegionAvail().x;
    const float minimum_combo = classic_px(160);
    float reserved = 0;
    for (float group : {quick_space, actions_space, startup_width + spacing}) {
        if (available - reserved < minimum_combo + group) break;
        reserved += group;
    }
    const auto same_line_if_fits = [&](float width) {
        const float line_end = ImGui::GetCursorScreenPos().x + ImGui::GetContentRegionAvail().x;
        if (ImGui::GetItemRectMax().x + spacing + width <= line_end) ImGui::SameLine();
    };
    const bool inline_quick = available >= minimum_combo + quick_space;
    ImGui::SetNextItemWidth(std::max(1.0f, std::min(classic_px(410), available - reserved)));
    ImGui::BeginDisabled(!ready);
    if (ImGui::BeginCombo("##classic-profile", bar.selected ? bar.selected->name.c_str() : "Select saved profile",
                          ImGuiComboFlags_HeightLarge)) {
        if (ImGui::IsWindowAppearing()) refresh_library();
        if (catalog.library_loading) ImGui::TextDisabled("Reading saved profiles...");
        else {
            ImGui::TextColored(classic_accent, "Quick slots");
            for (std::size_t i = 0; i < slots.size(); ++i) {
                const auto* entry = find_profile(slots[i]);
                const auto number = std::to_string(i + 1);
                ImGui::PushID(number.c_str());
                const auto label = number + "  " + (entry ? entry->name : slots[i].empty() ? "Unassigned" : "Profile unavailable");
                const char* assignment = slots[i].empty() ? "Assign" : "Change";
                const float assignment_width = ImGui::CalcTextSize(assignment).x + 2 * style.FramePadding.x;
                const float row_width = std::max(1.0f, ImGui::GetContentRegionAvail().x - assignment_width - spacing);
                ImGui::AlignTextToFramePadding();
                ImGui::BeginDisabled(!selectable(entry));
                if (ImGui::Selectable(label.c_str(), entry && bar.selected && bar.selected->id == entry->id, 0, {row_width, 0}))
                    select(*entry);
                ImGui::EndDisabled();
                classic_menu_hint(entry ? profile_hint(entry) : slots[i].empty()
                    ? "Assign a saved profile to this quick slot" : "The saved profile is unavailable");
                ImGui::SameLine();
                ImGui::BeginDisabled(state.quick_profile_assignment.has_value());
                if (ImGui::Button(assignment)) ImGui::OpenPopup("##assign-quick-slot");
                if (ImGui::BeginPopup("##assign-quick-slot")) {
                    bool found = false;
                    for (const auto& choice : catalog.library) {
                        if (choice.autosave || !selectable(&choice)) continue;
                        found = true;
                        const auto existing = std::find(slots.begin(), slots.end(), choice.id);
                        const bool swap = existing != slots.end() && existing != slots.begin() + i;
                        const auto name = swap
                            ? choice.name + "  (slot " + std::to_string(existing - slots.begin() + 1) + ")"
                            : choice.name;
                        ImGui::PushID(choice.id.c_str());
                        if (ImGui::MenuItem(name.c_str(), nullptr, choice.id == slots[i])) {
                            state.quick_profile_assignment.emplace(i, choice.id);
                            state.quick_profile_status.clear();
                        }
                        if (swap) classic_menu_hint("Swap this quick slot with the indicated slot");
                        ImGui::PopID();
                    }
                    if (!found) ImGui::TextDisabled("No selectable profiles");
                    ImGui::EndPopup();
                }
                ImGui::EndDisabled();
                ImGui::PopID();
            }
            ImGui::Separator();
            bool has_profiles = false;
            for (const auto& entry : catalog.library) {
                if (entry.autosave) continue;
                has_profiles = true;
                if (std::find(slots.begin(), slots.end(), entry.id) != slots.end()) continue;
                ImGui::PushID(entry.id.c_str());
                ImGui::BeginDisabled(!selectable(&entry));
                if (ImGui::Selectable(entry.name.c_str(), bar.selected && bar.selected->id == entry.id)) select(entry);
                ImGui::EndDisabled();
                classic_menu_hint(profile_hint(&entry));
                ImGui::PopID();
            }
            if (!has_profiles) ImGui::TextDisabled("No saved profiles");
        }
        ImGui::EndCombo();
    }
    ImGui::EndDisabled();
    for (std::size_t i = 0; i < slots.size(); ++i) {
        if (i || inline_quick) same_line_if_fits(slot_width);
        const auto* entry = find_profile(slots[i]);
        const bool usable = selectable(entry) && !catalog.library_loading;
        const bool unlocked = usable && (state.controls_unlocked || metadata_only(*entry));
        const bool empty_slot = slots[i].empty();
        const auto label = std::to_string(i + 1) + "##classic-profile-quick";
        ImGui::BeginDisabled(empty_slot ? (!can_save || catalog.library_loading) : (!ready || !unlocked));
        if (ImGui::Button(label.c_str(), {slot_width, 0})) {
            if (empty_slot) open_profile_save(state, i);
            else { select(*entry); apply(); }
        }
        ImGui::EndDisabled();
        if (empty_slot) {
            classic_menu_hint("Save current GPU settings to this slot");
        } else {
            if (can_save && !catalog.library_loading && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)
                && ImGui::IsMouseReleased(ImGuiMouseButton_Right)) open_profile_save(state, i);
            std::string detail = !usable ? profile_hint(entry) : unlocked ? "Apply " + entry->name
                : "Unlock controls to apply " + entry->name;
            detail += "\nRight-click to save current settings to this slot";
            classic_menu_hint(detail.c_str());
        }
    }

    same_line_if_fits(apply_width + save_width + spacing);
    const bool apply_allowed = bar.selected && (state.controls_unlocked || bar.selected->metadata_only);
    ImGui::BeginDisabled(!ready || !apply_allowed || bar.apply == ApplyState::running);
    if (ImGui::Button("Apply##classic-profile-apply")) apply();
    ImGui::EndDisabled();
    classic_menu_hint(!bar.selected ? "Select a saved profile" : !apply_allowed ? "Unlock controls to apply this profile"
        : "Check and apply the selected profile to this GPU");
    same_line_if_fits(save_width);
    ImGui::BeginDisabled(!can_save);
    if (ImGui::Button("Save...##classic-profile-save")) open_profile_save(state, std::nullopt);
    ImGui::EndDisabled();
    classic_menu_hint("Save current GPU settings to the library or a quick slot");
    draw_profile_save_popup(state, can_save);
    same_line_if_fits(actions_width);
    if (ImGui::Button(actions)) ImGui::OpenPopup("##classic-profile-actions");
    if (ImGui::BeginPopup("##classic-profile-actions")) {
        classic_profile_actions(state);
        ImGui::EndPopup();
    }

    same_line_if_fits(startup_width);
    const bool has_identity = live && live->data.identity.uuid[0] && !live->selected_slot.empty();
    const auto* startup_entry = bar.selected ? find_profile(bar.selected->id) : nullptr;
    const bool startup_selected = has_identity && bar.selected && !state.startup_profile_id.empty()
        && state.startup_profile_id == bar.selected->id && state.startup_profile_uuid == live->data.identity.uuid
        && state.startup_profile_slot == live->selected_slot;
    std::string startup_reason;
    if (!startup_selected) {
        if (!has_identity) startup_reason = "The selected GPU identity is unavailable";
        else if (!selectable(startup_entry)) startup_reason = "Select an available named profile";
        else if (!apply_allowed) startup_reason = "Unlock controls to enable startup for this profile";
        else startup_reason = startup_profile_reason(startup_entry->description, live->data.identity.uuid, live->selected_slot);
    }
    bool startup_enabled = state.startup_profile_request.value_or(startup_selected);
    ImGui::BeginDisabled(!ready || bar.apply == ApplyState::running || !startup_reason.empty()
        || state.startup_profile_request.has_value());
    if (ImGui::Checkbox(startup, &startup_enabled)) {
        state.startup_profile_request = startup_enabled;
        state.startup_profile_status.clear();
    }
    ImGui::EndDisabled();
    classic_menu_hint(startup_reason.empty()
        ? "Apply this saved profile automatically at sign-in on the selected GPU" : startup_reason.c_str());

    if (bar.apply != ApplyState::idle) {
        same_line_if_fits(classic_px(84));
        ImGui::AlignTextToFramePadding();
        if (bar.apply == ApplyState::running) ImGui::TextDisabled("Applying...");
        else if (bar.apply == ApplyState::applied) ImGui::TextColored(classic_accent, "Applied");
        else ImGui::TextColored(classic_warn, "Apply failed");
        classic_menu_hint(bar.status.c_str());
    }
    if (!state.quick_profile_status.empty()) ImGui::TextWrapped("%s", state.quick_profile_status.c_str());
    if (!state.toolbar_save_status.empty()) ImGui::TextWrapped("%s", state.toolbar_save_status.c_str());
    if (!state.startup_profile_status.empty()) ImGui::TextWrapped("%s", state.startup_profile_status.c_str());
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
        if(ImGui::MenuItem("Refresh capabilities"))state.capability_refresh_requested=true;
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
    if(ImGui::BeginMenu("Clocks")) {
        if(classic)classic_clocks_body(state);
        const auto* live = state.live.get();
        if (live && live->verifying_clock_offsets) {
            if (ImGui::MenuItem("Stop after current clock offset")) state.clock_checks_cancel_requested = true;
        } else {
            const bool ready = live && !state.preview && state.controls_unlocked && live->data.connected &&
                !live->busy && !live->opening && !live->paused && !state.select_slot;
            const bool available = ready && std::any_of(live->data.advanced_controls.begin(),
                live->data.advanced_controls.end(), [](const AdvancedControl& row) {
                    return row.clock_check == ClockCheck::candidate && row.can_verify;
                });
            if (ImGui::MenuItem("Verify all clock offsets at P0", nullptr, false, available)) {
                Command command; command.operation = Command::Operation::verify_all_clock_offsets;
                state.commands.push_back(std::move(command));
            }
            const bool has_known = live && std::any_of(live->data.advanced_controls.begin(),
                live->data.advanced_controls.end(), [](const AdvancedControl& row) {
                    return row.clock_check == ClockCheck::known_pattern;
                });
            if (has_known && ImGui::BeginMenu("Recheck a known offset at P0", ready)) {
                for (const auto& row : live->data.advanced_controls) {
                    if (row.clock_check != ClockCheck::known_pattern) continue;
                    const auto label = row.label + " [" + std::to_string(row.id.domain) + "]";
                    if (ImGui::MenuItem(label.c_str(), nullptr, false, row.can_verify && !row.binding.empty())) {
                        Command command; command.operation = Command::Operation::verify_clock_offset;
                        command.advanced = row.id;
                        command.expected_binding = row.binding;
                        state.commands.push_back(std::move(command));
                    }
                }
                ImGui::EndMenu();
            }
        }
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
            ImGui::TextUnformatted("Thermetery Druta " DRUTA_APP_VERSION " — native frontend");
            ImGui::TextUnformatted("Copyright (C) 2026 Thermetery Technology Co Limited");
            ImGui::TextWrapped("GNU General Public License, version 3 or later. See Licences for the complete bundled COPYING text and third-party notices.");
            ImGui::Text("Dear ImGui %s / ImPlot %s / nlohmann JSON",IMGUI_VERSION,IMPLOT_VERSION);
            ImGui::TextWrapped("The native frontend uses the selected drutacore DLL through its C API. Reported capability and telemetry availability belong to the current adapter; incomplete evidence is shown explicitly.");
            ImGui::TextWrapped("The Classic profile bar can apply a selected profile at Windows sign-in. "
                "A second window opens one explicit PCI slot with that card's expected GPU UUID and receives no startup tuning settings.");
            ImGui::TextWrapped("Selecting a different GPU during an owned V/F hold opens that GPU in a second window.");
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
