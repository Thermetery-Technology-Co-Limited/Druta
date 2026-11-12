// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Headless: link ui_view.cpp, curve_draft.cpp and druta_ui_vendor,
// with the UI/core include paths.
// No Session implementation, GPU backend, native window, or graphics device is used.
#include "../ui_view.h"
#include "../session.h"
#include "../i2c_view.h"
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
                const auto row_seed = ImHashStr("control-row", 0, table_id);
                const auto row_id = ImHashData(&row, sizeof(row), row_seed);
                return ImHashStr(name, 0, row_id);
            }
        }
    }
    check(false, "control workspace exists for stable-ID activation");
    return 0;
}

ImGuiID workspace_button(const char* name) {
    auto* window = ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    check(window != nullptr, "curve popup exists for stable-ID activation");
    return window ? window->GetID(name) : 0;
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
    live->data.curve_step_khz = 15000;
    live->data.curve_max_abs_delta_khz = 1000000;
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
    state.curve_frequency_mhz = 1412.345;
    state.curve_entry_editing = true;
    ++live->curve_revision;
    frame(state, 1920, 1400);
    check(near(state.curve_frequency_mhz, 1412.345), "fresh full observation cannot replace an active curve numeric edit");
    state.curve_frequency_mhz = 1430;
    state.curve_entry_dirty = true;
    ImGui::ActivateItemByID(workspace_button("Set MHz"));
    auto text = frame(state, 1920, 1400);
    check(state.curve_draft.dirty() && state.curve_draft.draft()[0].delta_khz == 66000,
        "Set MHz snaps physical frequency on the captured phase and verified divisor");
    check(near(state.curve_draft.draft()[0].freq_mhz, 1430) && state.commands.empty(),
        "staging predicts frequency locally and never emits a hardware command");
    ImGui::ActivateItemByID(workspace_button("Undo edit"));
    frame(state, 1920, 1400);
    check(!state.curve_draft.dirty() && state.commands.empty(), "Undo edit restores the local point without hardware Undo");
    state.curve_frequency_mhz = 1430;
    state.curve_entry_dirty = true;
    ImGui::ActivateItemByID(workspace_button("Set MHz"));
    frame(state, 1920, 1400);
    state.presentation = presentation == Presentation::focused ? Presentation::classic : Presentation::focused;
    ++live->revision;
    // An ordinary telemetry publication is not a new full curve observation.
    live->data.curve[0].freq_mhz = 9999;
    frame(state, 1920, 1400);
    check(near(state.curve_draft.baseline()[0].freq_mhz, 1400)
        && state.curve_draft.draft()[0].delta_khz == 66000,
        "ordinary polls and presentation changes do not replace curve edit state");
    live->data.curve = state.curve_draft.draft();
    ++live->curve_revision;
    text = frame(state, 1920, 1400);
    check(!state.curve_draft.can_apply() && state.curve_draft.dirty()
        && contains(text, "Hardware curve changed"),
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
    check(!state.curve_draft.dirty() && near(state.curve_frequency_mhz, 1430),
        "explicit local Revert rebases to the latest fresh hardware curve");
    state.curve_frequency_mhz = 1445;
    state.curve_entry_dirty = true;
    ImGui::ActivateItemByID(workspace_button("Set MHz"));
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
        && state.commands[0].expected_curve.size() == 2 && state.commands[0].expected_curve[0].delta_khz == 66000
        && state.commands[0].expected_curve_freq_div == 2 && state.commands[0].curve_deltas.size() == 1
        && state.commands[0].curve_deltas[0].delta_khz == 96000,
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

void daily_header_hover_conflict() {
    ViewState state; state.preview = false;
    auto live = snapshot(); state.live = live;
    for (auto& control : live->data.controls) {
        control.current = 0; control.low = -100; control.high = 100;
        control.writable = control.stock_writable = true;
    }
    frame(state, 1366, 900, false); frame(state, 1366, 900, false);
    auto* root = ImGui::FindWindowByName("Druta workspace");
    ImGuiWindow* body = nullptr;
    if (root) for (auto* window : ImGui::GetCurrentContext()->Windows)
        if (window->ParentWindow == root && window->ChildId == root->GetID("workspace")) body = window;
    check(body != nullptr, "Daily body exists for real Apply-header hover regression");
    if (!body) return;
    const auto table_id = body->GetID("daily_controls");
    const auto* table = ImGui::TableFindByID(table_id);
    check(table && table->ColumnsCount == 5, "Focused Daily exposes the real five-column table");
    if (!table) return;
    const int apply_column = 3;
    const auto header_id = ImHashStr("Apply", 0, ImHashData(&apply_column, sizeof(apply_column), table_id));
    check(header_id != control_button(ControlId::fan, "Apply"), "Apply header column 3 and fan control 3 have distinct ID namespaces");
    const float x = (table->Columns[3].MinX + table->Columns[3].MaxX) * 0.5f;
    const float y = table->OuterRect.Min.y + 12;
    auto& io = ImGui::GetIO();
    check(io.ConfigDebugHighlightIdConflicts, "ImGui conflicting-ID diagnostics stay enabled");
    io.AddMousePosEvent(x, y);
    for (int i = 0; i < 4; ++i) frame(state, 1366, 900, false);
    check(ImGui::GetCurrentContext()->DebugDrawIdConflictsId == 0
        && ImGui::GetCurrentContext()->HoveredIdPreviousFrameItemCount <= 1,
        "hovering actual Apply header does not report two visible items with a conflicting ID");
    io.AddMouseButtonEvent(ImGuiMouseButton_Left, true); frame(state, 1366, 900, false);
    io.AddMouseButtonEvent(ImGuiMouseButton_Left, false); frame(state, 1366, 900, false);
    check(state.commands.empty(), "clicking a table header cannot dispatch a fan Apply command");
}

void original_classic_layout_and_daily_companions() {
    ViewState state; state.preview = false; state.presentation = Presentation::classic;
    auto live = snapshot(); state.live = live;
    live->data.xoc_known = live->data.rail_mode_known = live->data.rail_writable_known = true;
    live->data.rail_writable = true;
    for (auto& control : live->data.controls) {
        control.current = 0; control.low = -100; control.high = 100;
        control.writable = control.stock_writable = true;
    }
    PowerPolicy named;
    named.id = 27; named.named_current = true; named.core_current = true;
    named.label = "Core current (NVVDD)"; named.binding = "current-channel-binding";
    named.state.current = 200; named.state.low = 0; named.state.high = 300;
    named.state.unit = "A"; named.state.writable = named.state.stock_writable = true;
    named.state.availability = Availability::available;
    auto misleading = named; misleading.id = 3; misleading.core_current = false;
    misleading.label = "Core current"; misleading.state.current = 30;
    live->data.power_policies = {misleading, named};
    RailControl rail;
    rail.id = {0, 0}; rail.binding = "rail-binding"; rail.estimated_base_mv = 1000;
    rail.state.current = 0; rail.state.low = -100; rail.state.high = 100; rail.state.unit = "mV";
    rail.state.availability = Availability::available;
    live->data.rail_controls.push_back(rail);
    const float rounding = ImGui::GetStyle().FrameRounding;
    auto text = frame(state, 1501, 1125);
    check(contains(text, "Thermetery Druta 1.9.1") && contains(text, "card") && contains(text, "driver") && contains(text, "vbios"),
        "Classic restores the original version/card metadata header");
    check(contains(text, "Clock offsets") && contains(text, "Limits") && contains(text, "Rails")
        && contains(text, "Timings (needs nvtune)") && !contains(text, "WORKSPACE"),
        "Classic has original Control grouping and Timings tab rather than the modern workspace sidebar");
    size_t unlock_count = 0, start = 0;
    while ((start = text.find("Unlock controls", start)) != std::string::npos) { ++unlock_count; ++start; }
    check(unlock_count == 1 && !contains(text, "Unlock tuning controls"), "the main Classic interface has exactly one original unlock strip");
    auto* root = ImGui::FindWindowByName("Druta workspace"); ImGuiWindow* body = nullptr;
    bool active_sidebar = false;
    if (root) for (auto* window : ImGui::GetCurrentContext()->Windows) if (window->ParentWindow == root) {
        if (window->ChildId == root->GetID("workspace")) body = window;
        if (window->ChildId == root->GetID("navigation") && window->Active) active_sidebar = true;
    }
    bool columns_match = false;
    if (body) {
        const auto columns_id = body->GetID("classic-control-columns");
        const auto* columns = ImGui::TableFindByID(columns_id);
        const auto* offsets = ImGui::TableFindByID(ImHashStr("classic-clock-offsets", 0, columns_id));
        const auto* rails = ImGui::TableFindByID(ImHashStr("daily-rail-controls", 0, columns_id));
        columns_match = columns && columns->ColumnsCount == 2 && offsets && offsets->ColumnsCount == 6
            && rails && rails->ColumnsCount == 6 && offsets->OuterRect.Min.x < rails->OuterRect.Min.x;
    }
    check(columns_match && !active_sidebar, "Classic uses clipped left/right groups and original six-column slider tables");
    check(near(ImGui::GetStyle().FrameRounding, rounding), "Classic restores scoped style after drawing instead of mutating Focused theme");
    check(state.advanced_view.drafts.count(2027) && !state.advanced_view.drafts.count(2003),
        "Daily selects uniquely API-qualified core current, not a matching label or fixed policy ID");
    state.advanced_view.drafts[2027].value = 201.125; state.advanced_view.drafts[2027].dirty = true;
    state.drafts[static_cast<size_t>(ControlId::memory)].value = 2.5;
    state.drafts[static_cast<size_t>(ControlId::memory)].dirty = true;
    state.presentation = Presentation::focused;
    text = frame(state, 1366, 900);
    check(contains(text, "NVVDD current limit") && contains(text, "Rail limits") && contains(text, "Board power"),
        "Focused Daily retains board power, named NVVDD current and rail-limit controls");
    check(near(state.advanced_view.drafts[2027].value, 201.125)
        && near(state.drafts[static_cast<size_t>(ControlId::memory)].value, 2.5) && state.commands.empty(),
        "switching actual layouts retains ordinary and named-current drafts without writing");
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
    auto* window = ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    bool plot_visible = false, panel_visible = false;
    if (window) {
        const auto* plot = ImPlot::GetCurrentContext()->Plots.GetByKey(window->GetID("##live_curve"));
        plot_visible = plot && plot->FrameRect.Min.y >= window->ClipRect.Min.y
            && plot->FrameRect.Max.y <= window->ClipRect.Max.y && plot->FrameRect.GetHeight() >= 300;
        panel_visible = window->ScrollMax.y <= 1;
    }
    check(plot_visible, "Curve plot remains visible at 1280x800 / 150 percent scale");
    check(panel_visible, "Curve point and hardware action panel fits alongside plot at 1280x800 / 150 percent");
    apply_theme(1);
}

void press_key(ViewState& state, ImGuiKey key, bool shift = false, bool control = false) {
    auto& io = ImGui::GetIO();
    io.DeltaTime = control ? 0.5f : 1.0f / 60.0f; // Honor legacy Undo's 0.4s repeat bound.
    io.AddKeyEvent(ImGuiMod_Shift, shift);
    io.AddKeyEvent(ImGuiMod_Ctrl, control);
    io.AddKeyEvent(key, true);
    frame(state, 1366, 900, false);
    io.AddKeyEvent(key, false);
    io.AddKeyEvent(ImGuiMod_Shift, false);
    io.AddKeyEvent(ImGuiMod_Ctrl, false);
    frame(state, 1366, 900, false);
    io.DeltaTime = 1.0f / 60.0f;
}

ImPlotPlot* popup_plot() {
    auto* window = ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    return window ? ImPlot::GetCurrentContext()->Plots.GetByKey(window->GetID("##live_curve")) : nullptr;
}

void popup_input(Presentation presentation) {
    ViewState state;
    state.preview = false;
    state.page = Page::curve;
    state.presentation = presentation;
    auto live = snapshot();
    live->data.curve_freq_div = 2;
    live->data.curve_step_khz = 12657;
    live->data.curve.clear();
    constexpr uint32_t ids[] = {9, 2, 20, 4, 11, 42, 7, 99};
    for (size_t i = 0; i < std::size(ids); ++i)
        live->data.curve.push_back({ids[i], 750.0 + 25.0 * i, 1500.0 + 30.0 * i, static_cast<int32_t>(i * 1000)});
    state.live = live;
    frame(state, 1366, 900, false);
    frame(state, 1366, 900, false);
    check(state.curve_window_open && state.page == Page::daily, "--page curve opens the popup while preserving Daily as the main page");
    check(state.selected_curve_point == 9, "selection uses an actual sparse point identity");
    press_key(state, ImGuiKey_W);
    check(state.curve_draft.draft()[0].delta_khz == 25314, "W moves one reported physical bin with divisor two");
    press_key(state, ImGuiKey_W, true);
    check(state.curve_draft.draft()[0].delta_khz == 101256, "Shift+W moves three physical bins");
    press_key(state, ImGuiKey_S);
    check(state.curve_draft.draft()[0].delta_khz == 75942, "S moves down one physical bin");
    press_key(state, ImGuiKey_S, true);
    check(state.curve_draft.draft()[0].delta_khz == 0, "Shift+S moves down three bins");
    press_key(state, ImGuiKey_D);
    check(state.selected_curve_point == 2, "D selects the next actual point, not numeric index plus one");
    press_key(state, ImGuiKey_D, true);
    check(state.selected_curve_point == 11, "Shift+D selects three points forward");
    press_key(state, ImGuiKey_A, true);
    check(state.selected_curve_point == 2, "Shift+A selects three points backward");
    press_key(state, ImGuiKey_A);
    press_key(state, ImGuiKey_A);
    check(state.selected_curve_point == 9, "A clamps at the first point");
    press_key(state, ImGuiKey_F);
    check(state.selected_curve_point == 9 && state.curve_draft.draft()[0].delta_khz == 0, "F has no invented editing binding");
    ImGui::ActivateItemByID(workspace_button("Revert local curve"));
    frame(state, 1366, 900, false);
    check(!state.curve_draft.dirty() && !state.curve_draft.can_undo(), "local Revert clears staged key history");

    auto* popup = ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    check(popup != nullptr, "large editor window exists");
    if (!popup) return;
    ImGui::ActivateItemByID(popup->GetID("MHz"));
    ImGui::GetCurrentContext()->NavNextActivateFlags = ImGuiActivateFlags_PreferInput;
    frame(state, 1366, 900, false);
    check(state.curve_entry_editing, "numeric field accepts real ImGui navigation activation");
    press_key(state, ImGuiKey_W);
    check(!state.curve_draft.dirty(), "W typed while numeric input is active does not change a curve point");
    ImGui::SetWindowFocus("Druta workspace");
    frame(state, 1366, 900, false);
    press_key(state, ImGuiKey_W);
    check(!state.curve_draft.dirty(), "background editor cannot consume WASD");
    ImGui::SetWindowFocus("V/F curve editor###druta_curve_editor");
    frame(state, 1366, 900, false);

    auto* plot = popup_plot();
    check(plot != nullptr, "popup has a real interactive plot");
    if (!plot) return;
    const auto original = state.curve_draft.draft();
    const auto x_range = plot->Axes[ImAxis_X1].Range;
    const auto y_range = plot->Axes[ImAxis_Y1].Range;
    const auto& picked = original[3];
    const ImVec2 dot(plot->Axes[ImAxis_X1].PlotToPixels(picked.volt_mv), plot->Axes[ImAxis_Y1].PlotToPixels(picked.freq_mhz));
    auto& io = ImGui::GetIO();
    io.AddMousePosEvent(dot.x, dot.y);
    frame(state, 1366, 900, false);
    io.AddMouseButtonEvent(ImGuiMouseButton_Left, true);
    frame(state, 1366, 900, false);
    check(state.curve_drag_point && *state.curve_drag_point == picked.idx && state.selected_curve_point == picked.idx,
        "mouse-down on a large draft dot selects and grabs that exact point");
    for (float amount : {30.0f, 65.0f, 100.0f}) {
        io.AddMousePosEvent(dot.x + 20, dot.y - amount);
        frame(state, 1366, 900, false);
    }
    check(state.curve_draft.dirty() && state.curve_draft.draft()[3].delta_khz != picked.delta_khz,
        "real mouse motion stages a vertically snapped frequency change");
    bool voltages_fixed = true;
    for (size_t i = 0; i < original.size(); ++i)
        voltages_fixed = voltages_fixed && state.curve_draft.draft()[i].volt_mv == original[i].volt_mv;
    check(voltages_fixed, "horizontal mouse motion cannot change point voltages");
    plot = popup_plot();
    check(near(plot->Axes[ImAxis_X1].Range.Min, x_range.Min) && near(plot->Axes[ImAxis_X1].Range.Max, x_range.Max)
        && near(plot->Axes[ImAxis_Y1].Range.Min, y_range.Min) && near(plot->Axes[ImAxis_Y1].Range.Max, y_range.Max),
        "grabbing a dot does not pan the view at the same time");
    io.AddMousePosEvent(1500, 1000); // Release outside the popup/window.
    io.AddMouseButtonEvent(ImGuiMouseButton_Left, false);
    frame(state, 1366, 900, false);
    check(!state.curve_drag_point && ImPlot::GetInputMap().Pan == ImGuiMouseButton_Left, "outside release ends the gesture and restores left pan");
    const auto moved_delta = state.curve_draft.draft()[3].delta_khz;
    press_key(state, ImGuiKey_Z, false, true);
    check(!state.curve_draft.dirty() && !state.curve_draft.can_undo() && state.curve_draft.can_redo(),
        "one Ctrl+Z undoes the entire multi-frame drag gesture");
    press_key(state, ImGuiKey_Y, false, true);
    check(state.curve_draft.draft()[3].delta_khz == moved_delta, "Ctrl+Y redoes the whole gesture");
    press_key(state, ImGuiKey_Z, false, true);
    press_key(state, ImGuiKey_Z, true, true);
    check(state.curve_draft.draft()[3].delta_khz == moved_delta, "Ctrl+Shift+Z is also local Redo");
    check(state.commands.empty(), "mouse dragging and all legacy keys only edit the draft");

    const auto before_miss = state.curve_draft.draft();
    plot = popup_plot();
    const ImVec2 empty(plot->PlotRect.GetCenter().x, plot->PlotRect.Min.y + 30);
    io.AddMousePosEvent(empty.x, empty.y); frame(state, 1366, 900, false);
    io.AddMouseButtonEvent(ImGuiMouseButton_Left, true); frame(state, 1366, 900, false);
    check(!state.curve_drag_point, "empty sky does not grab a point merely sharing its voltage column");
    const auto before_pan = popup_plot()->Axes[ImAxis_Y1].Range;
    io.AddMousePosEvent(empty.x + 40, empty.y + 40); frame(state, 1366, 900, false);
    io.AddMouseButtonEvent(ImGuiMouseButton_Left, false); frame(state, 1366, 900, false);
    bool unchanged = true;
    for (size_t i = 0; i < before_miss.size(); ++i)
        unchanged = unchanged && before_miss[i].delta_khz == state.curve_draft.draft()[i].delta_khz;
    check(unchanged && !near(before_pan.Min, popup_plot()->Axes[ImAxis_Y1].Range.Min), "empty left-drag pans without editing any point");
    const double zoom_before = popup_plot()->Axes[ImAxis_Y1].Range.Size();
    io.AddMouseWheelEvent(0, 1); frame(state, 1366, 900, false);
    check(popup_plot()->Axes[ImAxis_Y1].Range.Size() < zoom_before, "wheel zoom remains available in the editor");
    ImGui::ActivateItemByID(workspace_button("Fit view")); frame(state, 1366, 900, false);
    frame(state, 1366, 900, false);
    const auto retained = state.curve_draft.draft()[3].delta_khz;
    state.curve_window_open = false; frame(state, 1366, 900, false);
    check(state.curve_draft.draft()[3].delta_khz == retained && state.commands.empty(), "closing the editor retains its draft without applying it");
    state.page = Page::curve; frame(state, 1366, 900, false);
    check(state.curve_window_open && state.curve_draft.draft()[3].delta_khz == retained && state.commands.empty(), "reopening restores the same local draft without a hardware command");
}

void curve_apply_acknowledgement(Presentation presentation) {
    for (int scenario = 0; scenario < 5; ++scenario) {
        ViewState state;
        state.preview = false;
        state.presentation = presentation;
        auto live = snapshot();
        live->data.curve_freq_div = 2;
        state.live = live;
        frame(state, 1366, 900, false);
        check(state.curve_draft.stage_mhz(0, 15), "test stages a representable physical request");
        const auto submitted = state.curve_draft.draft();
        if (scenario == 2) {
            check(state.curve_draft.stage_mhz(0, 30), "a new local edit can follow the submitted curve");
        }
        live->data.curve = submitted;
        ++live->curve_revision;
        live->completed_actions = 1;
        live->last_action_operation = scenario == 3 ? Command::Operation::apply : Command::Operation::apply_curve;
        live->last_action_ok = scenario != 1;
        if (scenario == 4) live->curve_stale = true;
        frame(state, 1366, 900, false);
        if (scenario == 0) {
            check(!state.curve_draft.dirty() && !state.curve_draft.can_undo()
                && state.curve_draft.baseline()[0].delta_khz == 30000
                && state.curve_draft.status() == "Applied curve verified",
                "successful curve Apply with exact fresh readback accepts the baseline and clears edit history");
            check(state.curve_draft.stage_mhz(0, 22.5), "a new draft can be staged after acknowledgement");
            live->data.curve = state.curve_draft.draft();
            ++live->curve_revision; // Same completed action count; no new successful Apply.
            frame(state, 1366, 900, false);
            check(state.curve_draft.dirty(), "historical successful-action metadata cannot acknowledge a later independent draft");
        } else if (scenario == 1) {
            check(state.curve_draft.dirty() && state.curve_draft.can_undo()
                && state.curve_draft.baseline()[0].delta_khz == 0
                && state.curve_draft.draft()[0].delta_khz == 30000,
                "failed Apply retains the draft even when fresh readback happens to match the request");
        } else if (scenario == 2) {
            check(state.curve_draft.dirty() && state.curve_draft.draft()[0].delta_khz == 60000
                && state.curve_draft.baseline()[0].delta_khz == 0,
                "successful earlier Apply cannot erase a local draft edited while that request was pending");
        } else if (scenario == 3) {
            check(state.curve_draft.dirty() && state.curve_draft.baseline()[0].delta_khz == 0,
                "an ordinary control action cannot acknowledge a curve edit merely because the readback matches");
        } else {
            check(state.curve_draft.dirty(), "successful action waits for a fresh curve before acknowledging edits");
            live->curve_stale = false; live->busy = true;
            ++live->curve_revision;
            frame(state, 1366, 900, false);
            check(state.curve_draft.dirty(), "busy snapshot cannot prematurely acknowledge an Apply");
            live->busy = false;
            frame(state, 1366, 900, false);
            check(!state.curve_draft.dirty() && state.curve_draft.baseline()[0].delta_khz == 30000,
                "deferred successful Apply is acknowledged when exact readback is fresh and work finishes");
        }
        check(state.commands.empty(), "Apply-result presentation never replays a hardware command");
    }
}

void i2c_panel_wiring(Presentation presentation) {
    ViewState state; state.preview = false; state.presentation = presentation;
    auto live = snapshot(); state.live = live;
    live->data.i2c.available = true;
    state.i2c_port = 3; state.i2c_address = 0x42; state.i2c_target_mv = 1125;
    constexpr const char* panel_name = "I2C UI wiring contract";
    auto panel_frame = [&] {
        ImGui::GetIO().DisplaySize = {1200, 900}; ImGui::NewFrame();
        ImGui::SetNextWindowPos({0, 0}); ImGui::SetNextWindowSize({1200, 900});
        ImGui::Begin(panel_name, nullptr, ImGuiWindowFlags_NoSavedSettings);
        draw_i2c_gate(state); draw_i2c_panel(state);
        ImGui::End(); ImGui::Render();
        check(ImGui::GetDrawData()->CmdListsCount > 0, "I2C panel renders from snapshot without a backend");
    };
    auto panel_id = [&](const char* label) {
        auto* window = ImGui::FindWindowByName(panel_name);
        return window ? ImHashStr(label, 0, window->GetID("i2c-controller")) : ImGuiID{0};
    };
    auto activate_panel = [&](const char* label) { ImGui::ActivateItemByID(panel_id(label)); panel_frame(); };
    auto exact_route = [&](const Command& command) {
        return command.i2c_route && command.i2c_route->port == 3 && command.i2c_route->addr7 == 0x42;
    };
    panel_frame(); panel_frame();
    check(state.commands.empty(), "showing the I2C panel does not probe or write automatically");
    activate_panel("Probe route");
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_select
        && exact_route(state.commands[0]), "Probe queues the exact edited route instead of a developer-board route");
    state.commands.clear();
    live->data.i2c.selected = I2cRoute{3, 0x42};
    live->data.i2c.status.min_mv = 500; live->data.i2c.status.max_mv = 1600;
    live->data.i2c.status.capable = true; live->data.i2c.status.writable = true;
    panel_frame();
    auto* window = ImGui::FindWindowByName(panel_name);
    ImGui::ActivateItemByID(window->GetID("I2C rail")); panel_frame();
    check(state.commands.empty(), "locked I2C gate cannot arm a controller");
    state.controls_unlocked = true;
    ImGui::ActivateItemByID(window->GetID("I2C rail")); panel_frame();
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_gate
        && state.commands[0].enabled && exact_route(state.commands[0]), "I2C gate emits a route-bound explicit intent");
    state.commands.clear();
    activate_panel("Verify response");
    check(state.commands.empty(), "an unconfirmed gate intent does not immediately authorize Verify");
    live->data.i2c.gate = true; live->data.i2c.status.capable = false;
    activate_panel("Verify response");
    check(state.commands.empty(), "Verify requires the reported controller capability");
    live->data.i2c.status.capable = true;
    activate_panel("Verify response");
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_verify
        && exact_route(state.commands[0]), "Verify queues exactly the selected route without calling hardware in the renderer");
    state.commands.clear();
    activate_panel("Apply");
    check(state.commands.empty(), "manual Apply is blocked until response verification is confirmed");
    live->data.i2c.verified = true; live->data.i2c.status.writable = false;
    activate_panel("Apply");
    check(state.commands.empty(), "verified but non-writable controller cannot Apply");
    live->data.i2c.status.writable = true; live->data.i2c.gate = false;
    activate_panel("Apply");
    check(state.commands.empty(), "manual Apply still requires the I2C gate");
    live->data.i2c.gate = true; state.controls_unlocked = false;
    activate_panel("Apply");
    check(state.commands.empty(), "manual Apply still requires unlock");
    state.controls_unlocked = true;
    activate_panel("Apply");
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_apply
        && exact_route(state.commands[0]) && near(state.commands[0].value, 1125),
        "manual Apply button is wired to the route and exact visible millivolt target");
    state.commands.clear();
    window = ImGui::FindWindowByName(panel_name);
    const auto apply_id = panel_id("Apply");
    check(window->NavLastIds[ImGuiNavLayer_Main] == apply_id, "actual Apply button receives navigation focus");
    const auto hover = window->NavRectRel[ImGuiNavLayer_Main].GetCenter();
    ImGui::GetIO().AddMousePosEvent(window->Pos.x + hover.x, window->Pos.y + hover.y);
    panel_frame(); panel_frame();
    check(state.commands.empty(), "hovering the actual Apply button never submits another request");
    live->data.i2c.status.owned = true;
    live->data.connected = false; state.controls_unlocked = false; live->data.i2c.gate = false;
    state.i2c_port = 7; state.i2c_address = 0x77; // Route hints are not the owned selection.
    activate_panel("Probe route");
    check(state.commands.empty(), "owned/disconnected controller cannot be redirected by route hints");
    activate_panel("Release / restore captured state");
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_release
        && exact_route(state.commands[0]), "owned Release remains available while disconnected, locked and gate-off");
    state.commands.clear();
    live->busy = true;
    activate_panel("Release / restore captured state");
    check(state.commands.empty(), "in-flight work is not overlapped by a second release intent");
    live->busy = false; live->data.i2c.status.owned = false; live->data.i2c.status.recovery_pending = true;
    activate_panel("Release / restore captured state");
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_release,
        "uncertain restoration retains an explicit Release recovery action");
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
    check(contains(text, "last read"), "transient uncertainty remains visible without a repeated diagnostics paragraph");
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
    check(contains(text, "Power limit") && contains(text, "--"), "absent board reading is unavailable rather than zero");
    check(live->curve_stale && state.curve_draft.status() == "Curve is not fresh", "retained curve is not treated as a fresh editor observation");
    // Stress the incoming snapshot sizes without allowing plotting arrays to overflow.
    for (size_t i = 0; i < 800; ++i) live->history.push_back({static_cast<double>(i + 3), 1890, 43, 200});
    live->data.curve.resize(DC_VFP_POINTS + 1, dc_vf_point{2, 950, 2000, 0});
    const auto bounded = frame(state, 1920, 1400);
    check(state.commands.empty(), "oversized incoming curve cannot dispatch a command from Daily");
    (void)bounded;
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
    daily_header_hover_conflict();
    original_classic_layout_and_daily_companions();
    compact_curve_layout();
    draft_behavior();
    explicit_profile_intents_and_stock();
    curve_workspace(Presentation::focused);
    curve_workspace(Presentation::classic);
    popup_input(Presentation::focused);
    popup_input(Presentation::classic);
    curve_apply_acknowledgement(Presentation::focused);
    curve_apply_acknowledgement(Presentation::classic);
    i2c_panel_wiring(Presentation::focused);
    i2c_panel_wiring(Presentation::classic);
    rendering_and_uncertainty();
    ImPlot::DestroyContext();
    ImGui::DestroyContext();
    std::printf("%d UI view checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
