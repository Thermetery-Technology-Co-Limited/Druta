// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Headless: link ui_view.cpp, curve_draft.cpp and druta_ui_vendor,
// with the UI/core include paths.
// No Session implementation, GPU backend, native window, or graphics device is used.
#include "../ui_view.h"
#include "../session.h"
#include "imgui.h"
#include "imgui_internal.h" // Pinned ImGui text capture, not widget coordinates.
#include "implot.h"
#include "implot_internal.h" // Pinned plot bounds for layout qualification.
#include <cmath>
#include <cstdio>
#include <string>

namespace {
using namespace druta::ui;
int checks = 0;
int failures = 0;
void check(bool condition, const char* message) {
    ++checks;
    if (!condition) {
        ++failures;
        std::printf("FAIL: %s\n", message);
    }
}
bool near(double value, double expected) { return std::abs(value - expected) < 1e-9; }
bool contains(const std::string& value, const char* text) { return value.find(text) != std::string::npos; }

ImGuiID header_button(const char* name) {
    auto* window = ImGui::FindWindowByName("Druta workspace");
    check(window != nullptr, "workspace window exists for stable-ID activation");
    return window ? window->GetID(name) : 0;
}

ImGuiID control_button(ControlId control, const char* name) {
    auto* root = ImGui::FindWindowByName("Druta workspace");
    if (root) {
        const auto child_id = root->GetID("workspace");
        for (auto* window : ImGui::GetCurrentContext()->Windows) {
            if (window->ParentWindow == root && window->ChildId == child_id) {
                const int row = static_cast<int>(control);
                const auto table_id = window->GetID("daily_controls");
                const auto row_id = ImHashData(&row, sizeof(row), table_id);
                return ImHashStr(name, 0, row_id);
            }
        }
    }
    check(false, "control workspace exists for stable-ID activation");
    return 0;
}

ImGuiID workspace_button(const char* name) {
    auto* root = ImGui::FindWindowByName("Druta workspace");
    if (root) {
        const auto child_id = root->GetID("workspace");
        for (auto* window : ImGui::GetCurrentContext()->Windows)
            if (window->ParentWindow == root && window->ChildId == child_id) return window->GetID(name);
    }
    check(false, "curve workspace exists for stable-ID activation");
    return 0;
}

std::string frame(ViewState& state, float width = 1366, float height = 768, bool capture = true) {
    ImGui::GetIO().DisplaySize = ImVec2(width, height);
    ImGui::NewFrame();
    if (capture) ImGui::LogToBuffer();
    draw(state);
    const std::string text = capture ? ImGui::GetCurrentContext()->LogBuffer.c_str() : "";
    if (capture) ImGui::LogFinish();
    ImGui::Render();
    check(ImGui::GetDrawData()->CmdListsCount > 0, "frame generates drawing commands");
    return text;
}

std::shared_ptr<SessionSnapshot> snapshot() {
    auto live = std::make_shared<SessionSnapshot>();
    live->generation = 1;
    live->selected_slot = "0000:02:00.0";
    live->devices.push_back({live->selected_slot, "Alternate adapter", "adapter-uuid"});
    std::snprintf(live->data.identity.name, sizeof(live->data.identity.name), "Alternate adapter");
    live->status = "Partial telemetry; memory request remains available";
    live->data.connected = true;
    auto& memory = live->data.controls[static_cast<size_t>(ControlId::memory)];
    memory.availability = Availability::available;
    memory.writable = true;
    memory.low = -100;
    memory.high = 250;
    memory.stock = 0;
    memory.unit = "MHz true";
    live->data.curve.push_back({0, 750, 1400, 0});
    live->data.curve.push_back({1, 900, 1890, 0});
    live->curve_revision = 1;
    live->data.curve_freq_div = 1;
    live->log.push_back({1, "Refused request; captured Undo retained", false});
    return live;
}

void curve_workspace(Presentation presentation) {
    ViewState state;
    state.preview = false;
    state.page = Page::curve;
    state.presentation = presentation;
    auto live = snapshot();
    live->data.curve_freq_div = 2;
    live->data.curve_step_khz = 15000;
    live->data.curve_writable = true;
    live->data.curve_reset_writable = true;
    live->data.curve[0].delta_khz = 6000;
    live->data.undo_available = true;
    state.live = live;
    frame(state, 1920, 1400);
    check(state.curve_draft.baseline().size() == 2 && near(state.curve_delta_mhz, 3),
        "fresh curve initializes numeric point delta using the verified frequency divisor");
    state.curve_delta_mhz = 12.345;
    state.curve_entry_editing = true;
    ++live->curve_revision;
    frame(state, 1920, 1400);
    check(near(state.curve_delta_mhz, 12.345), "fresh full observation cannot replace an active curve numeric edit");
    state.curve_delta_mhz = 15.5;
    state.curve_entry_dirty = true;
    ImGui::ActivateItemByID(workspace_button("Stage point"));
    auto text = frame(state, 1920, 1400);
    check(state.curve_draft.dirty() && state.curve_draft.draft()[0].delta_khz == 31000,
        "Stage converts physical MHz to exact wire kHz through the curve model");
    check(near(state.curve_draft.draft()[0].freq_mhz, 1412.5) && state.commands.empty(),
        "staging predicts frequency locally and never emits a hardware command");
    ImGui::ActivateItemByID(workspace_button("Undo edit"));
    frame(state, 1920, 1400);
    check(!state.curve_draft.dirty() && state.commands.empty(), "Undo edit restores the local point without hardware Undo");
    state.curve_delta_mhz = 15.5;
    state.curve_entry_dirty = true;
    ImGui::ActivateItemByID(workspace_button("Stage point"));
    frame(state, 1920, 1400);
    state.presentation = presentation == Presentation::focused ? Presentation::classic : Presentation::focused;
    ++live->revision;
    // An ordinary telemetry publication is not a new full curve observation.
    live->data.curve[0].freq_mhz = 9999;
    frame(state, 1920, 1400);
    check(near(state.curve_draft.baseline()[0].freq_mhz, 1400)
        && state.curve_draft.draft()[0].delta_khz == 31000,
        "ordinary polls and presentation changes do not replace curve edit state");
    live->data.curve = state.curve_draft.draft();
    ++live->curve_revision;
    text = frame(state, 1920, 1400);
    check(!state.curve_draft.can_apply() && state.curve_draft.dirty()
        && contains(text, "Draft retained; Refresh / Revert"),
        "fresh changed hardware retains the pending draft and exposes a rebase conflict");
    live->curve_stale = true;
    state.controls_unlocked = true;
    frame(state, 1920, 1400);
    ImGui::ActivateItemByID(workspace_button("Apply curve"));
    frame(state, 1920, 1400);
    check(!state.curve_draft.can_apply() && state.commands.empty(), "stale curve disables Apply while retaining local changes");
    live->curve_stale = false;
    ++live->curve_revision;
    frame(state, 1920, 1400);
    ImGui::ActivateItemByID(workspace_button("Revert local curve"));
    frame(state, 1920, 1400);
    check(!state.curve_draft.dirty() && near(state.curve_delta_mhz, 15.5),
        "explicit local Revert rebases to the latest fresh hardware curve");
    state.curve_delta_mhz = 30;
    state.curve_entry_dirty = true;
    ImGui::ActivateItemByID(workspace_button("Stage point"));
    frame(state, 1920, 1400);
    state.controls_unlocked = false;
    ImGui::ActivateItemByID(workspace_button("Apply curve"));
    frame(state, 1920, 1400);
    check(state.commands.empty(), "curve hardware writes require tuning unlock");
    state.controls_unlocked = true;
    live->data.curve_reset_writable = false;
    ImGui::ActivateItemByID(workspace_button("Apply curve"));
    frame(state, 1920, 1400);
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::apply_curve
        && state.commands[0].expected_curve.size() == 2 && state.commands[0].expected_curve[0].delta_khz == 31000
        && state.commands[0].expected_curve_freq_div == 2 && state.commands[0].curve_deltas.size() == 1
        && state.commands[0].curve_deltas[0].delta_khz == 60000,
        "Apply carries captured baseline, divisor and changed requests independently of Reset capability");
    state.commands.clear();
    check(state.curve_draft.dirty(), "dispatch does not erase a draft before verified readback");
    live->data.curve_writable = false;
    live->data.curve_reset_writable = true;
    ImGui::ActivateItemByID(workspace_button("Reset curve to stock"));
    frame(state, 1920, 1400);
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::reset_curve
        && state.commands[0].expected_curve.size() == 2 && state.commands[0].expected_curve_freq_div == 2
        && state.commands[0].curve_deltas.empty(), "Reset carries fresh hardware state independently of Apply capability and dirty draft");
    state.commands.clear();
    ImGui::ActivateItemByID(workspace_button("Undo hardware write"));
    frame(state, 1920, 1400);
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::undo
        && state.curve_draft.dirty(), "hardware Undo is a distinct command and preserves pending local edits");
    state.commands.clear();
    ++live->generation;
    live->curve_revision = 0;
    live->data.curve.clear();
    live->data.curve_freq_div = 0;
    frame(state, 1920, 1400);
    check(state.curve_draft.baseline().empty() && !state.curve_draft.dirty() && !state.controls_unlocked,
        "new adapter/session clears curve edits and unlock without replay");
}

void compact_layout() {
    ViewState state;
    state.preview = false;
    auto live = snapshot();
    state.live = live;
    for (auto& control : live->data.controls) control.current = 1.25;
    frame(state, 1024, 768, false);
    frame(state, 1024, 768, false);
    auto* root = ImGui::FindWindowByName("Druta workspace");
    bool visible = false;
    if (root) {
        const auto child_id = root->GetID("workspace");
        for (auto* window : ImGui::GetCurrentContext()->Windows) {
            if (window->ParentWindow == root && window->ChildId == child_id) {
                const auto* table = ImGui::TableFindByID(window->GetID("daily_controls"));
                visible = table && table->OuterRect.Min.y >= window->ClipRect.Min.y
                    && table->OuterRect.Max.y <= window->ClipRect.Max.y;
            }
        }
    }
    check(visible, "Focused Daily fits all five control rows at 1024x768 with global profiles and operation log");
}

void compact_curve_layout() {
    ViewState state;
    state.preview = false;
    state.page = Page::curve;
    auto live = snapshot();
    live->data.curve_writable = live->data.curve_reset_writable = true;
    live->data.curve_step_khz = 15000;
    state.live = live;
    apply_theme(1.5f);
    ImGui::GetStyle().FontScaleDpi = 1.5f;
    frame(state, 1280, 800, false);
    frame(state, 1280, 800, false);
    auto* root = ImGui::FindWindowByName("Druta workspace");
    bool plot_visible = false, panel_visible = false;
    if (root) {
        const auto child_id = root->GetID("workspace");
        for (auto* window : ImGui::GetCurrentContext()->Windows) {
            if (window->ParentWindow == root && window->ChildId == child_id) {
                const auto* plot = ImPlot::GetCurrentContext()->Plots.GetByKey(window->GetID("##live_curve"));
                plot_visible = plot && plot->FrameRect.Min.y >= window->ClipRect.Min.y
                    && plot->FrameRect.Max.y <= window->ClipRect.Max.y;
                panel_visible = window->ScrollMax.y <= 1;
            }
        }
    }
    check(plot_visible, "Curve plot remains visible at 1280x800 / 150 percent scale");
    check(panel_visible, "Curve point and hardware action panel fits alongside plot at 1280x800 / 150 percent");
    apply_theme(1);
}

void draft_behavior() {
    ViewState state;
    state.preview = false;
    auto live = snapshot();
    state.live = live;
    auto& memory = live->data.controls[static_cast<size_t>(ControlId::memory)];
    auto& draft = state.drafts[static_cast<size_t>(ControlId::memory)];
    frame(state);
    check(!draft.initialized, "missing configured memory request does not create a zero baseline");
    memory.current = 1.25;
    frame(state);
    check(draft.initialized && near(draft.value, 1.25) && !state.has_draft(),
        "first valid fractional request initializes draft without requiring unrelated sensors");
    check(live->data.controls[static_cast<size_t>(ControlId::core)].availability == Availability::unknown,
        "partial capabilities remain independent");
    memory.current = 2.5;
    frame(state);
    check(near(draft.value, 2.5), "unedited draft follows a fresh configured request");

    // Simulate the presentation's active editing state without relying on fixed screen coordinates.
    // Physical keyboard/slider input is a separate desktop qualification check.
    draft.editing = true;
    memory.current = 3.5;
    frame(state);
    check(near(draft.value, 2.5), "poll does not replace an active numeric edit");
    check(draft.baseline && near(*draft.baseline, 3.5), "active edit retains latest observed restore baseline");
    draft.value = 4.75;
    draft.dirty = true;
    memory.current = 5.25;
    frame(state);
    check(near(draft.value, 4.75) && state.has_draft(), "poll preserves a dirty fractional target");
    state.presentation = Presentation::classic;
    state.page = Page::advanced;
    frame(state);
    state.page = Page::daily;
    frame(state);
    check(near(draft.value, 4.75) && state.has_draft(), "presentation and navigation preserve draft");
    state.revert_draft();
    check(near(draft.value, 5.25) && !state.has_draft(), "local Revert uses latest configured request");
    check(state.commands.empty(), "local Revert and presentation changes dispatch no hardware command");

    memory.availability = Availability::transient;
    memory.detail = "Temporary request read failure; last valid capability retained";
    const auto text = frame(state);
    check(draft.initialized && near(draft.value, 5.25), "transient state retains established draft");
    check(contains(text, "Temporary request read failure"), "transient uncertainty is visible");
    state.controls_unlocked = true;
    draft.value = 10;
    draft.dirty = true;
    live->generation = 2;
    memory.current = -2;
    frame(state);
    check(near(draft.value, -2) && !draft.dirty, "new session discards old adapter target");
    check(!state.controls_unlocked, "new session resets tuning unlock");
    check(state.commands.empty() && !state.select_slot && !state.refresh_requested && !state.reconnect_requested
        && !state.save_profile_requested && !state.load_profile_requested,
        "rendering and readback never create requests implicitly");
}

void explicit_profile_intents_and_stock() {
    ViewState state;
    state.preview = false;
    auto live = snapshot();
    state.live = live;
    frame(state, 1920, 1400);
    // Activate by the pinned widget ID, without assumptions about coordinates,
    // fonts or display scaling. This exercises actual ImGui Button behavior.
    ImGui::ActivateItemByID(header_button("Save profile..."));
    frame(state, 1920, 1400);
    check(state.save_profile_requested && state.commands.empty(), "Save emits only an explicit dialog intent and needs no tuning unlock");
    state.save_profile_requested = false;
    ImGui::ActivateItemByID(header_button("Load profile..."));
    frame(state, 1920, 1400);
    check(!state.load_profile_requested, "Load remains disabled while tuning controls are locked");
    state.controls_unlocked = true;
    ImGui::ActivateItemByID(header_button("Load profile..."));
    frame(state, 1920, 1400);
    check(state.load_profile_requested && state.commands.empty(), "Load emits an explicit dialog intent after unlock");
    state.load_profile_requested = false;
    live->busy = true;
    ImGui::ActivateItemByID(header_button("Save profile..."));
    frame(state, 1920, 1400);
    ImGui::ActivateItemByID(header_button("Load profile..."));
    frame(state, 1920, 1400);
    check(!state.save_profile_requested && !state.load_profile_requested, "busy session disables profile dialogs");
    live->busy = false;
    live->data.connected = false;
    ImGui::ActivateItemByID(header_button("Save profile..."));
    frame(state, 1920, 1400);
    ImGui::ActivateItemByID(header_button("Load profile..."));
    frame(state, 1920, 1400);
    check(!state.save_profile_requested && !state.load_profile_requested, "disconnected session disables profile dialogs");
    live->data.connected = true;
    live->opening = true;
    ImGui::ActivateItemByID(header_button("Save profile..."));
    frame(state, 1920, 1400);
    check(!state.save_profile_requested, "opening session disables Save");
    live->opening = false;

    auto& fan = live->data.controls[static_cast<size_t>(ControlId::fan)];
    fan.availability = Availability::transient;
    fan.detail = "Mixed or unreadable fan request";
    fan.writable = false;
    fan.current.reset();
    fan.stock.reset();
    fan.stock_writable = true;
    frame(state, 1920, 1400);
    ImGui::ActivateItemByID(control_button(ControlId::fan, "Auto"));
    frame(state, 1920, 1400);
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::stock
        && state.commands[0].control == ControlId::fan,
        "Fan Auto remains available independently of scalar/capture/manual-write state");
    state.commands.clear();
    fan.stock_writable = false;
    ImGui::ActivateItemByID(control_button(ControlId::fan, "Auto"));
    frame(state, 1920, 1400);
    check(state.commands.empty(), "Fan Auto requires its own verified capability");

    auto& power = live->data.controls[static_cast<size_t>(ControlId::power)];
    power.availability = Availability::transient;
    power.writable = false;
    power.current.reset();
    power.stock = 300;
    power.stock_writable = true;
    ImGui::ActivateItemByID(control_button(ControlId::power, "Stock"));
    frame(state, 1920, 1400);
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::stock
        && state.commands[0].control == ControlId::power,
        "Power Stock remains available when full prior policy capture is unavailable");
    state.commands.clear();
    ImGui::ActivateItemByID(control_button(ControlId::power, "Apply"));
    frame(state, 1920, 1400);
    check(state.commands.empty(), "Power Stock capability does not enable ordinary Apply");
}

void rendering_and_uncertainty() {
    ViewState state;
    state.preview = false;
    auto live = snapshot();
    state.live = live;
    auto& memory = live->data.controls[static_cast<size_t>(ControlId::memory)];
    memory.current = 7.125;
    live->data.telemetry_valid = true;
    live->data.telemetry.present = DC_T_CORE | DC_T_TEMP_EDGE;
    live->data.telemetry.core = 1890;
    live->data.telemetry.temp_edge = 43;
    live->telemetry_stale_mask = DC_T_CORE;
    live->curve_stale = true;
    live->history.push_back({1, 1890, 43, std::nullopt});
    live->history.push_back({2, std::nullopt, 44, std::nullopt});
    // Smaller and larger layouts exercise clipping, child windows and plots in both presentations.
    for (auto presentation : {Presentation::focused, Presentation::classic}) {
        state.presentation = presentation;
        for (auto page : {Page::daily, Page::advanced, Page::monitor, Page::curve}) {
            state.page = page;
            frame(state, 1024, 768);
            frame(state, 1366, 768);
            const auto text = frame(state, 1920, 1080);
            check(!contains(text, "3070 Ti") && !contains(text, "Sample data"),
                "live pages contain no fixture adapter or telemetry disclaimer");
            check(contains(text, "Alternate adapter"), "live adapter identity remains visible");
            check(contains(text, "Refused request; captured Undo retained"),
                "failed action remains visible in both presentations and all pages");
            check(contains(text, "Save profile...") && contains(text, "Load profile..."),
                "manual profile actions remain reachable from every page and presentation");
        }
    }
    state.page = Page::daily;
    const auto text = frame(state, 1920, 1080);
    check(contains(text, "1890 MHz") && contains(text, "(stale)"), "last-good core reading is explicitly stale");
    check(contains(text, "Board") && contains(text, "--"), "absent board reading is unavailable rather than zero");
    check(contains(text, "Last observed curve (stale)"), "retained curve is not presented as a fresh read");
    // Stress the incoming snapshot sizes without allowing plotting arrays to overflow.
    for (size_t i = 0; i < 800; ++i) live->history.push_back({static_cast<double>(i + 3), 1890, 43, 200});
    live->data.curve.resize(DC_VFP_POINTS + 1, dc_vf_point{2, 950, 2000, 0});
    const auto bounded = frame(state, 1920, 1400);
    check(contains(bounded, "plot is truncated"), "oversized incoming curve is bounded and explicitly described");
    state.page = Page::monitor;
    live->data.telemetry.fan_count = 1;
    live->data.telemetry.fans[0].has_duty = 1;
    live->data.telemetry.fans[0].duty = 35;
    live->data.telemetry.fans[0].has_rpm = 1;
    live->data.telemetry.fans[0].rpm = 1200;
    live->fan_readings_stale = true;
    const auto fan_text = frame(state, 1920, 1400);
    check(contains(fan_text, "1200 RPM (stale)"), "last-good fan readings are explicitly stale independently of count presence bit");
    live->fan_readings_stale = false;
    live->data.telemetry.fans[0].has_duty = 0;
    const auto partial_fan = frame(state, 1920, 1400);
    check(contains(partial_fan, "duty --") && contains(partial_fan, "1200 RPM"), "fresh partial fan data preserves independent duty/RPM availability");
    live->data.connected = false;
    live->data.curve.clear();
    live->data.telemetry.present = 0;
    live->data.telemetry_valid = false;
    live->data.telemetry_error = "Read temporarily unavailable";
    live->paused = true;
    for (auto page : {Page::daily, Page::advanced, Page::monitor, Page::curve}) {
        state.page = page;
        frame(state, 1024, 768);
    }
    check(state.commands.empty(), "missing/stale/paused displays never dispatch a write");
    state.preview = true;
    for (auto page : {Page::daily, Page::advanced, Page::monitor, Page::curve}) {
        state.page = page;
        frame(state, 1024, 768);
    }
    check(state.commands.empty(), "fixture preview remains disconnected from commands");
}
} // namespace

int main() {
    ImGui::CreateContext();
    ImPlot::CreateContext();
    auto& io = ImGui::GetIO();
    io.IniFilename = nullptr;
    io.DeltaTime = 1.0f / 60.0f;
    ImFontConfig font_config;
    font_config.SizePixels = 18;
    io.Fonts->AddFontDefault(&font_config);
    unsigned char* pixels = nullptr;
    int width = 0, height = 0;
    io.Fonts->GetTexDataAsRGBA32(&pixels, &width, &height);
    apply_theme(1);
    compact_layout();
    compact_curve_layout();
    draft_behavior();
    explicit_profile_intents_and_stock();
    curve_workspace(Presentation::focused);
    curve_workspace(Presentation::classic);
    rendering_and_uncertainty();
    ImPlot::DestroyContext();
    ImGui::DestroyContext();
    std::printf("%d UI view checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
