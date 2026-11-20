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
#include <algorithm>
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
size_t occurrences(const std::string& value, const char* text) {
    size_t count = 0, start = 0;
    while ((start = value.find(text, start)) != std::string::npos) { ++count; ++start; }
    return count;
}

ImGuiID offset_button(AdvancedId id, const char* name) {
    auto* root = ImGui::FindWindowByName("Druta workspace");
    if (root) for (auto* window : ImGui::GetCurrentContext()->Windows)
        if (window->ParentWindow == root && window->ChildId == root->GetID("workspace")) {
            const int key = static_cast<int>(1000 + static_cast<uint64_t>(id.kind) * 100 + id.domain);
            const char* table = id.kind == AdvancedKind::clock_domain ? "advanced-offsets" : "daily-rail-controls";
            return ImHashStr(name, 0, ImHashData(&key, sizeof(key), window->GetID(table)));
        }
    check(false, "offset workspace exists for stable-ID activation");
    return 0;
}

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

// Classic shows 1.9.1's wording for the same editor actions.
const char* curve_label(const ViewState& state, const char* name) {
    if (state.presentation != Presentation::classic) return name;
    const std::string focused = name;
    if (focused == "Apply curve") return "Apply V/F curve to GPU";
    if (focused == "Revert local curve") return "Revert edits";
    if (focused == "MHz") return "##MHz";
    return name;
}

ImGuiID workspace_button(const char* name) {
    auto* window = ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    check(window != nullptr, "curve popup exists for stable-ID activation");
    if (!window) return 0;
    // Classic keeps native-only editor tools in one collapsed header; a click
    // there starts by opening it. The Focused editor has no such header.
    const std::string label = name;
    if (label == "Undo edit" || label == "Redo edit" || label == "Undo hardware write" || label == "Hold selected point"
        || label == "Profiles..." || label == "Limited de-flatten \xE2\x89\xA4 cap")
        window->StateStorage.SetInt(window->GetID("More V/F tools"), 1);
    return window->GetID(name);
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
    // The fixture edits point 0 with a selection already made; Classic's
    // middle-point default for a fresh session is covered by inline_curve_input.
    state.curve_selection_pending = false;
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
    ImGui::ActivateItemByID(workspace_button(curve_label(state, "Apply curve")));
    frame(state, 1920, 1400);
    check(!state.curve_draft.can_apply() && state.commands.empty(), "stale curve disables Apply while retaining local changes");
    live->curve_stale = false;
    ++live->curve_revision;
    frame(state, 1920, 1400);
    ImGui::ActivateItemByID(workspace_button(curve_label(state, "Revert local curve")));
    frame(state, 1920, 1400);
    check(!state.curve_draft.dirty() && near(state.curve_frequency_mhz, 1430),
        "explicit local Revert rebases to the latest fresh hardware curve");
    state.curve_frequency_mhz = 1445;
    state.curve_entry_dirty = true;
    ImGui::ActivateItemByID(workspace_button("Set MHz"));
    frame(state, 1920, 1400);
    state.controls_unlocked = false;
    ImGui::ActivateItemByID(workspace_button(curve_label(state, "Apply curve")));
    frame(state, 1920, 1400);
    check(state.commands.empty(), "curve hardware writes require tuning unlock");
    state.controls_unlocked = true;
    live->data.curve_reset_writable = false;
    ImGui::ActivateItemByID(workspace_button(curve_label(state, "Apply curve")));
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
    check(state.curve_draft.baseline().empty() && !state.curve_draft.dirty() && state.controls_unlocked,
        "new adapter/session clears curve edits while preserving unlock preference without replay");
}

void compact_layout() {
    ViewState state;
    state.presentation = Presentation::focused;
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
    ViewState state;
    state.presentation = Presentation::focused; state.preview = false;
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
    ImGui::ActivateItemByID(control_button(ControlId::fan,"Apply")); frame(state,1366,900,false);
    state.commands.clear();
    const auto apply_rect=body->NavRectRel[ImGuiNavLayer_Main];
    io.AddMousePosEvent(body->Pos.x+apply_rect.GetCenter().x,body->Pos.y+apply_rect.GetCenter().y);
    for(int i=0;i<4;++i) frame(state,1366,900,false);
    bool active_tooltip=false;
    for(auto* window:ImGui::GetCurrentContext()->Windows) if(window->Active && (window->Flags&ImGuiWindowFlags_Tooltip)) active_tooltip=true;
    check(!active_tooltip && state.commands.empty(),"hovering Apply with an empty detail opens no empty tooltip");
}

// Python refresh_control: the Limits footer names the native effective limit the
// Classic poll read, and reads "unavailable" when that read failed.
void classic_power_request_footer() {
    ViewState state; state.preview = false; state.presentation = Presentation::classic;
    auto live = snapshot(); state.live = live;
    auto& power = live->data.controls[static_cast<size_t>(ControlId::power)];
    power.availability = Availability::available; power.current = 900; power.low = 100; power.high = 1000; power.unit = "W";
    live->data.enforced_power_w = 900; live->data.native_power_limit_w = 972.07;
    for (int n = 0; n < 3; ++n) frame(state, 1528, 1261, false);
    auto text = frame(state, 1528, 1261);
    check(contains(text, "Configured request: 900 W. NVML-reported enforced limit: 900 W. Native effective limit: 972.07 W."),
        "the Classic Limits footer shows the native effective limit it read");
    live->data.native_power_limit_w.reset();
    text = frame(state, 1528, 1261);
    check(contains(text, "Native effective limit unavailable; NVML readback does not verify it."),
        "a failed native read shows the clause as unavailable");
}

size_t colour_vertices(ImU32 colour) {
    size_t n = 0;
    for (auto* window : ImGui::GetCurrentContext()->Windows)
        if (window->Active) for (const auto& vertex : window->DrawList->VtxBuffer) n += vertex.col == colour;
    return n;
}

// A Classic knob row's button: Python's Control page keeps every row in the
// two-column table's knob tables (classic-clock-offsets, classic-limits).
ImGuiID classic_control_button(const char* table, ControlId control, const char* name) {
    auto* root = ImGui::FindWindowByName("Druta workspace");
    if (root) for (auto* body : ImGui::GetCurrentContext()->Windows) {
        if (body->ParentWindow != root || body->ChildId != root->GetID("workspace")) continue;
        for (auto* knobs : ImGui::GetCurrentContext()->Windows) {
            if (knobs->ParentWindow != body || knobs->ChildId != body->GetID("classic-knob-columns")) continue;
            const int row = static_cast<int>(control);
            const auto table_id = ImHashStr(table, 0, knobs->GetID("classic-control-columns"));
            return ImHashStr(name, 0, ImHashData(&row, sizeof(row), ImHashStr("control-row", 0, table_id)));
        }
    }
    check(false, "Classic knob columns exist for stable-ID activation");
    return 0;
}

// 1.9.1 apply_pl: a board-power request whose configured readback is
// unavailable, or differs from the request, appends its warning to the Limits
// footer in WARN. Fixture values are arbitrary, not one board's.
void classic_power_request_feedback() {
    ViewState state; state.preview = false; state.presentation = Presentation::classic;
    auto live = snapshot(); state.live = live; live->data.telemetry_valid = true;
    auto& power = live->data.controls[static_cast<size_t>(ControlId::power)];
    power.availability = Availability::available; power.writable = power.stock_writable = true;
    power.current = 900; power.low = 100; power.high = 1000; power.stock = 700; power.unit = "W";
    live->data.enforced_power_w = 900; live->data.native_power_limit_w = 972.07;
    for (int n = 0; n < 3; ++n) frame(state, 1528, 1261, false);
    const ImU32 warn = IM_COL32(255, 203, 71, 255);
    auto text = frame(state, 1528, 1261);
    const auto idle_warn = colour_vertices(warn);
    auto& draft = state.drafts[static_cast<size_t>(ControlId::power)];
    uint64_t log_id = 1;
    // Queue a request, then deliver its result and the session's read after it.
    // The session reports the result's own fields; the message text is not read.
    const auto request = [&](double watts, const char* result, bool ok, bool dispatched, std::optional<double> readback) {
        draft.value = watts; draft.dirty = true;
        ImGui::ActivateItemByID(classic_control_button("classic-limits", ControlId::power, "Apply")); frame(state, 1528, 1261, false);
        const bool queued = state.commands.size() == 1 && state.commands[0].operation == Command::Operation::apply
            && state.commands[0].control == ControlId::power && near(state.commands[0].value, watts);
        state.commands.clear();
        live->busy = true; frame(state, 1528, 1261, false);
        live->log.push_back({++log_id, result, ok, Command::Operation::apply}); live->last_action_log_id = log_id; ++live->completed_actions;
        live->last_action_operation = Command::Operation::apply; live->last_action_ok = ok; live->busy = false;
        live->last_action_requested = watts; live->last_action_readback = readback; live->last_action_may_have_changed = dispatched;
        return queued;
    };
    const char* unverified = "Current configured power request is unverified: readback unavailable. "
        "Slider shows last verified 900 W, not a confirmed current setting.";
    // A message that merely names a mismatch is not one: the readback matched.
    check(request(950, "Fixture wording; readback differs from request", false, true, 950.0), "a matched readback queues");
    text = frame(state, 1528, 1261);
    check(!contains(text, "did not match configured readback") && !contains(text, "unverified: readback unavailable. Slider"),
        "a readback equal to the request adds no warning, whatever the message says");
    check(request(950, "Fixture write result", false, true, std::nullopt), "board-power Apply queues the request");
    power.availability = Availability::transient; // the read after it failed: last known request kept
    text = frame(state, 1528, 1261);
    check(contains(text, unverified) && near(draft.value, 900) && !draft.dirty && colour_vertices(warn) > idle_warn,
        "an unavailable readback appends 1.9.1's unverified sentence in WARN and returns the slider to the last verified request");
    power.availability = Availability::available; text = frame(state, 1528, 1261);
    check(!contains(text, "unverified: readback unavailable. Slider") && colour_vertices(warn) == idle_warn,
        "the next valid configured read clears the unverified warning");
    check(request(950, "Fixture write result", false, true, 850.0), "a second request queues");
    power.current = 850; text = frame(state, 1528, 1261);
    const char* mismatch = "Requested 950 W did not match configured readback 850 W; slider returned to readback.";
    check(contains(text, "Configured request: 850 W.") && contains(text, mismatch) && near(draft.value, 850) && colour_vertices(warn) > idle_warn,
        "a readback that differs appends 1.9.1's mismatch sentence in WARN and returns the slider to the readback");
    text = frame(state, 1528, 1261);
    check(contains(text, mismatch), "a mismatch stays through later valid reads");
    // A request the action refused before any write leaves no readback warning,
    // and the new request drops the old one.
    check(request(875, "Fixture refusal; board readback unavailable", false, false, std::nullopt), "a third request queues");
    text = frame(state, 1528, 1261);
    check(!contains(text, mismatch) && !contains(text, "unverified: readback unavailable. Slider"),
        "a refusal before dispatch adds no readback warning and a new request clears the mismatch");
    request(950, "Fixture write result", false, true, 850.0);
    text = frame(state, 1528, 1261);
    check(contains(text, mismatch), "fixture: the mismatch is back");
    ++live->generation; text = frame(state, 1528, 1261);
    check(!contains(text, mismatch), "an adapter change clears the mismatch");
    state.presentation = Presentation::focused; frame(state, 1528, 1261);
    check(state.commands.empty(), "readback feedback writes nothing");
}

void press_key(ViewState& state, ImGuiKey key, bool shift, bool control, float width, float height);
ImGuiWindow* inline_curve_window();

// 1.9.1 vf_read after Reset all to stock (whatever its result), a full profile
// load and a non-label Undo, never after a scoped load or a label-only Undo; a
// failed read logs its error. Every vf_read rebases the editor. Arbitrary fixture.
void classic_curve_read_rules() {
    ViewState state; state.preview = false; state.presentation = Presentation::classic; state.controls_unlocked = true;
    auto live = snapshot(); live->data.curve_writable = live->data.curve_reset_writable = true;
    live->data.telemetry_valid = true; live->completed_reads = 1; state.live = live;
    const auto bottom = [&]() {
        if (auto* root = ImGui::FindWindowByName("Druta workspace")) for (auto* body : ImGui::GetCurrentContext()->Windows)
            if (body->ParentWindow == root && body->ChildId == root->GetID("workspace")) ImGui::SetScrollY(body, body->ScrollMax.y);
    };
    const auto draw_text = [&]() { frame(state, 1528, 1261, false); bottom(); frame(state, 1528, 1261, false); return frame(state, 1528, 1261); };
    const auto logged = [&](const char* start) {
        size_t n = 0; for (const auto& line : state.classic_log_lines) n += line.text.rfind(start, 0) == 0; return n;
    };
    auto text = draw_text();
    check(logged("curve read: ") == 1, "the startup read is logged once");
    uint64_t log_id = 1;
    const auto command = [&](Command::Operation operation, bool ok) {
        live->log.push_back({++log_id, "fixture result", ok}); live->last_action_log_id = log_id;
        ++live->completed_actions; live->last_action_operation = operation; live->last_action_ok = ok;
        ++live->completed_reads; live->curve_stale = false; live->data.curve_error.clear(); ++live->curve_revision;
        return draw_text();
    };
    // Reset all to stock re-reads even when it fails, and the read rebases the
    // editor: the staged edit and its history go.
    check(state.curve_draft.stage_khz(1, -15000, "nudge idx 1 -15 MHz") && state.curve_draft.dirty(), "fixture stages an edit");
    command(Command::Operation::reset_controls, false);
    check(logged("curve read: ") == 2 && !state.curve_draft.dirty() && !state.curve_draft.can_undo(),
        "a failed Reset controls still re-reads, rebasing the editor and clearing its history");
    command(Command::Operation::load_profile, false);
    check(logged("curve read: ") == 3, "a full profile load re-reads whatever its result");
    command(Command::Operation::apply_catalog_profile, true);
    check(logged("curve read: ") == 3, "a catalog profile whose scope is not a full profile does not re-read");
    live->data.undo_metadata_only = true; draw_text();
    command(Command::Operation::undo, true);
    check(logged("curve read: ") == 3, "a label-only Undo does not re-read");
    live->data.undo_metadata_only = false; draw_text();
    command(Command::Operation::undo, true);
    check(logged("curve read: ") == 4, "an Undo of a hardware write re-reads");

    // A curve already stale that fails Read curve twice logs each failure, and
    // the next unrelated command's read is not taken for the request.
    live->curve_stale = true; live->data.curve_error = "V/F read failed (fixture)"; ++live->completed_reads;
    text = draw_text();
    check(contains(text, "Curve refresh failed; showing previous read: V/F read failed (fixture)") && logged("V/F read failed") == 0,
        "a stale curve shows 1.9.1's refresh-failed sentence over the previous read; a read nobody asked for logs nothing");
    auto* editor = inline_curve_window();
    for (int attempt = 1; attempt <= 2; ++attempt) {
        ImGui::ActivateItemByID(editor ? editor->GetID("Read curve") : 0); frame(state, 1528, 1261, false);
        check(state.curve_read_requested && state.refresh_requested, "Read curve over a stale curve asks for a read");
        state.refresh_requested = false;
        ++live->completed_reads; draw_text(); ++live->completed_reads; draw_text();
        check(!state.curve_read_requested && logged("V/F read failed (fixture)") == static_cast<size_t>(attempt)
            && state.classic_log_lines.back().ok == false, "a failed Read curve logs its error once, as [!!]");
    }
    live->data.controls[static_cast<size_t>(ControlId::memory)].current = 5;
    command(Command::Operation::apply, true);
    check(logged("curve read: ") == 4, "after two failed reads an unrelated command's read logs no curve read");

    // Reset curve to stock says what it discards; its read rebases the editor.
    check(state.curve_draft.stage_khz(1, -15000, "nudge idx 1 -15 MHz"), "fixture stages an edit before Reset curve");
    editor = inline_curve_window();
    ImGui::ActivateItemByID(editor ? editor->GetID("Reset curve to stock") : 0); frame(state, 1528, 1261, false);
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::reset_curve
        && state.classic_log_lines.back().text == "zeroing all 2 V/F deltas back to the factory curve, discarding 1 staged edit(s)"
        && !state.classic_log_lines.back().ok, "Reset curve queues the reset and logs what it discards, untagged");
    state.commands.clear();
    command(Command::Operation::reset_curve, true);
    check(logged("curve read: ") == 5 && !state.curve_draft.dirty() && !state.curve_draft.can_undo(),
        "the read after Reset curve leaves an empty draft and an empty undo history");

    // Editor history goes to the log untagged, as 1.9.1 vf_undo / vf_redo do;
    // the editor gains no status line of its own.
    if (auto* root = ImGui::FindWindowByName("Druta workspace")) ImGui::FocusWindow(root);
    check(state.curve_draft.stage_khz(1, -15000, "nudge idx 1 -15 MHz"), "fixture stages a labelled edit");
    press_key(state, ImGuiKey_Z, false, true, 1528, 1261);
    check(state.classic_log_lines.back().text == "undo: nudge idx 1 -15 MHz (0 more) - 0 edit(s) still staged"
        && !state.classic_log_lines.back().ok, "Ctrl+Z logs 1.9.1's undo line");
    press_key(state, ImGuiKey_Y, false, true, 1528, 1261);
    check(state.classic_log_lines.back().text == "redo: nudge idx 1 -15 MHz (0 more)", "Ctrl+Y logs 1.9.1's redo line");
    press_key(state, ImGuiKey_Y, false, true, 1528, 1261);
    check(state.classic_log_lines.back().text == "nothing to redo", "an empty redo history says so");
    press_key(state, ImGuiKey_Z, false, true, 1528, 1261);
    press_key(state, ImGuiKey_Z, false, true, 1528, 1261);
    check(state.classic_log_lines.back().text == "nothing to undo", "an empty undo history says so");
    text = draw_text();
    check(!contains(text, "Edit undone") && !contains(text, "Edit redone") && !contains(text, "Draft ready")
        && contains(text, "nothing to undo"), "editor feedback reaches the status line through the log, with no extra editor line");
    check(state.commands.empty(), "curve reads and editor history write nothing");
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
    // Actual tab bars defer new-tab layout, and the horizontal knob region
    // measures its automatic height. Inspect the settled production frame.
    for (int n = 0; n < 4; ++n) frame(state, 1501, 1125, false);
    auto text = frame(state, 1501, 1125);
    check(contains(text, "Thermetery Druta 1.9.1") && contains(text, "card") && contains(text, "driver") && contains(text, "vbios"),
        "Classic restores the original version/card metadata header");
    check(contains(text, "Clock offsets") && contains(text, "Limits") && contains(text, "Rails")
        && contains(text, "Timings  (needs nvtune)") && !contains(text, "WORKSPACE"),
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
        ImGuiWindow* knobs = nullptr;
        for (auto* window : ImGui::GetCurrentContext()->Windows)
            if (window->ParentWindow == body && window->ChildId == body->GetID("classic-knob-columns")) knobs = window;
        if (knobs) {
            const auto columns_id = knobs->GetID("classic-control-columns");
            const auto* columns = ImGui::TableFindByID(columns_id);
            const auto* offsets = ImGui::TableFindByID(ImHashStr("classic-clock-offsets", 0, columns_id));
            const auto* rails = ImGui::TableFindByID(ImHashStr("daily-rail-controls", 0, columns_id));
            columns_match = columns && columns->ColumnsCount == 2 && offsets && offsets->ColumnsCount == 6
                && rails && rails->ColumnsCount == 6 && offsets->OuterRect.Min.x < rails->OuterRect.Min.x
                && !knobs->ScrollbarX && !(knobs->Flags & ImGuiWindowFlags_HorizontalScrollbar)
                && knobs->ScrollMax.x > 0 && knobs->ScrollMax.y == 0;
        }
    }
    // Python build_control at 1501 px: both columns do not fit, so ImGui narrows the
    // right one and its cells clip; no horizontal scrollbar is drawn.
    check(columns_match && !active_sidebar, "Classic preserves six-column left/right groups, clipping the narrowed right column without a horizontal scrollbar");
    check(near(ImGui::GetStyle().FrameRounding, rounding), "Classic restores scoped style after drawing instead of mutating Focused theme");
    check(state.advanced_view.drafts.count(2027) && !state.advanced_view.drafts.count(2003),
        "Daily selects uniquely API-qualified core current, not a matching label or fixed policy ID");
    state.advanced_view.drafts[2027].value = 201.125; state.advanced_view.drafts[2027].dirty = true;
    state.drafts[static_cast<size_t>(ControlId::memory)].value = 2.5;
    state.drafts[static_cast<size_t>(ControlId::memory)].dirty = true;
    state.presentation = Presentation::focused;
    text = frame(state, 1366, 900);
    check(contains(text, "NVVDD current limit") && contains(text, "Board power") && !contains(text, "NVVDD reliability"),
        "Focused Daily retains board power and named NVVDD current while rail rows have their own page");
    auto* navroot=ImGui::FindWindowByName("Druta workspace"); ImGuiWindow* nav=nullptr;
    if(navroot) for(auto* child:ImGui::GetCurrentContext()->Windows)
        if(child->ParentWindow==navroot && child->ChildId==navroot->GetID("navigation")) nav=child;
    check(nav!=nullptr,"Focused navigation exists for dedicated control pages");
    if(nav) ImGui::ActivateItemByID(nav->GetID("Rails"));
    text=frame(state,1366,900);
    check(state.page==Page::rails && contains(text,"NVVDD reliability") && !contains(text,"I2C regulator"),"Focused Rails tab opens its own controls");
    if(nav) ImGui::ActivateItemByID(nav->GetID("I2C"));
    text=frame(state,1366,900);
    check(state.page==Page::i2c && contains(text,"I2C") && !contains(text,"NVVDD reliability"),"Focused I2C tab opens its own page");
    state.page=Page::daily;
    check(near(state.advanced_view.drafts[2027].value, 201.125)
        && near(state.drafts[static_cast<size_t>(ControlId::memory)].value, 2.5) && state.commands.empty(),
        "switching actual layouts retains ordinary and named-current drafts without writing");
}

void offset_page_routing_and_gates() {
    ViewState state;
    state.presentation = Presentation::focused; state.preview = false;
    auto live = snapshot(); state.live = live;
    live->data.xoc_known = true;
    ControlState control;
    control.availability = Availability::available;
    control.writable = control.stock_writable = true;
    control.current = 10; control.stock = 0; control.low = -100; control.high = 100; control.unit = "MHz";
    live->data.advanced_controls = {
        {{AdvancedKind::clock_domain, 7}, "Video offset", control, "video-binding"},
        {{AdvancedKind::clock_domain, 21}, "Secondary offset", control, "secondary-binding"},
        {{AdvancedKind::clock_domain, 8}, "ROP offset", control, "rop-binding"},
        {{AdvancedKind::clock_domain, 11}, "Processor offset", control, "processor-binding"},
        {{AdvancedKind::clock_domain, 2}, "Memory domain request", control, "additional-memory-binding"},
        {{AdvancedKind::nvvdd, 3}, "NVVDD request offset", control, "nvvdd-binding"},
        {{AdvancedKind::msvdd, 4}, "MSVDD request offset", control, "msvdd-binding"},
    };
    const char* labels[] = {"Video offset", "Secondary offset", "ROP offset", "Processor offset",
        "Additional memory offset", "NVVDD request offset", "MSVDD request offset"};
    const Page pages[] = {Page::daily,Page::daily,Page::daily,Page::daily,Page::advanced,Page::rails,Page::rails};
    std::string text;
    for (auto page : {Page::daily,Page::advanced,Page::rails}) {
        state.page = page; text = frame(state,1920,1400);
        for (size_t i = 0; i < std::size(labels); ++i)
            check(occurrences(text,labels[i]) == (page == pages[i] ? 1u : 0u),
                "each offset appears exactly once on its assigned Focused page");
    }
    for (const auto& c : live->data.advanced_controls) {
        auto& draft = state.advanced_view.drafts[1000 + static_cast<uint64_t>(c.id.kind) * 100 + c.id.domain];
        draft.value = 17.25; draft.dirty = true;
    }
    const AdvancedId additional_memory{AdvancedKind::clock_domain, 2};
    const AdvancedId msvdd{AdvancedKind::msvdd, 4};
    for (auto id : {additional_memory, msvdd}) {
        state.page = id.kind == AdvancedKind::clock_domain ? Page::advanced : Page::rails;
        frame(state,1920,1400);
        ImGui::ActivateItemByID(offset_button(id, "Apply")); frame(state, 1920, 1400);
        check(state.commands.empty(), "relocated XOC-gated Apply stays disabled while XOC is off");
    }
    for (auto page : {Page::advanced, Page::rails, Page::monitor, Page::daily}) {
        state.page = page; text = frame(state, 1920, 1400);
        for (size_t i = 0; i < std::size(labels); ++i)
            check(occurrences(text, labels[i]) == (page == pages[i] ? 1u : 0u),
                "offset rows retain their assigned page across navigation");
    }
    for (const auto& c : live->data.advanced_controls) {
        const auto& draft = state.advanced_view.drafts[1000 + static_cast<uint64_t>(c.id.kind) * 100 + c.id.domain];
        check(draft.initialized && draft.dirty && near(draft.value, 17.25) && draft.binding == c.binding,
            "navigation retains each fractional offset draft and adapter binding");
    }
    check(!live->data.xoc_enabled && state.commands.empty(), "page navigation cannot enable XOC or apply pending offsets");
    live->data.xoc_enabled = true;
    frame(state, 1920, 1400);
    for (auto id : {additional_memory, msvdd}) {
        state.page = id.kind == AdvancedKind::clock_domain ? Page::advanced : Page::rails;
        frame(state,1920,1400);
        ImGui::ActivateItemByID(offset_button(id, "Apply")); frame(state, 1920, 1400);
        check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::apply
            && state.commands[0].advanced && state.commands[0].advanced->kind == id.kind
            && state.commands[0].advanced->domain == id.domain && near(state.commands[0].value, 17.25),
            "relocated offset applies the retained request after XOC enables its gate");
        state.commands.clear();
    }
    state.controls_unlocked = false;
    ImGui::ActivateItemByID(offset_button(msvdd, "Apply")); frame(state, 1920, 1400);
    check(state.commands.empty(), "relocated offset Apply still respects the global tuning lock");
}

void shunt_display_and_raw_power_apply() {
    ViewState state;
    state.presentation = Presentation::focused; state.preview = false;
    auto live = snapshot(); state.live = live;
    auto& power = live->data.controls[static_cast<size_t>(ControlId::power)];
    power.availability = Availability::available; power.writable = true;
    power.current = 150; power.low = 100; power.high = 300; power.unit = "W";
    live->data.telemetry_valid = true;
    live->data.telemetry.present = DC_T_POWER_W;
    live->data.telemetry.power_w = 120;
    frame(state, 1920, 1400);
    ImGui::ActivateItemByID(control_button(ControlId::power, "Shunt mod..."));
    auto text = frame(state, 1920, 1400);
    check(state.shunt.open && state.commands.empty(), "Board power Shunt mod button opens its display editor without a hardware command");
    for (size_t i = 0; i < state.shunt.config.rails.size(); ++i)
        check(edit_shunt_rail(state.shunt, i, 5, 2.5), "editing each shunt rail updates display correction");
    state.shunt.open = false;
    auto& draft = state.drafts[static_cast<size_t>(ControlId::power)];
    draft.value = 175; draft.dirty = true;
    text = frame(state, 1920, 1400);
    check(state.shunt.correction.exact && near(state.shunt.correction.factor, 2)
        && contains(text, "240 W corrected (120 W raw)") && contains(text, "300 W corrected"),
        "Daily shows corrected measured watts and configured watts with the raw reading retained");
    state.page = Page::monitor; text = frame(state, 1920, 1400);
    check(contains(text, "BOARD POWER") && contains(text, "240.0 W corrected") && contains(text, "120.0 W raw"),
        "monitor power tile identifies corrected watts and retains the raw reading");
    for (auto* window : ImGui::GetCurrentContext()->Windows)
        if (window->ParentWindow && window->ChildId == window->ParentWindow->GetID("workspace"))
            window->StateStorage.SetInt(window->GetID("History"), 1);
    text = frame(state, 1920, 1400);
    check(contains(text, "CORRECTED"), "expanded Monitor history identifies corrected power");
    state.page = Page::daily; frame(state, 1920, 1400);
    check(state.commands.empty() && !state.shunt.save_requested && !state.save_profile_requested
        && !state.load_profile_requested && !state.refresh_requested && !state.reconnect_requested,
        "shunt display edits and navigation never request saving, hardware writes or reconnects");
    ImGui::ActivateItemByID(control_button(ControlId::power, "Apply")); frame(state, 1920, 1400);
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::apply
        && state.commands[0].control == ControlId::power && near(state.commands[0].value, 175),
        "Board power Apply dispatches the raw requested watts independently of display correction");
    state.commands.clear();
    edit_shunt_rail(state.shunt, 0, 5, 5);
    text = frame(state, 1920, 1400);
    check(!state.shunt.correction.exact && contains(text, "estimate (120 W raw)"),
        "mixed shunt ratios label the main power reading as an estimate and retain raw watts");
    check(state.commands.empty() && !state.shunt.save_requested, "mixed display correction cannot save or dispatch hardware actions");
}

bool workspace_has_background(RiskColor color) {
    auto* window = ImGui::FindWindowByName("Druta workspace");
    if (!window) return false;
    const auto packed = IM_COL32(color.r, color.g, color.b, 255);
    for (const auto& vertex : window->DrawList->VtxBuffer)
        if (vertex.col == packed) return true;
    return false;
}

void weighted_workspace_risk_tint() {
    ViewState state;
    state.presentation = Presentation::focused; state.preview = false;
    auto live = snapshot(); state.live = live;
    live->data.xoc_known = live->data.rail_mode_known = live->data.rail_writable_known = true;
    live->data.rail_writable = true;
    const auto base_background = ImGui::GetStyle().Colors[ImGuiCol_WindowBg];
    for (auto presentation : {Presentation::focused, Presentation::classic}) {
        state.presentation = presentation;
        live->data.i2c.status = {};
        state.controls_unlocked = true;
        const bool classic = presentation == Presentation::classic;
        auto text = frame(state, 1920, 1400);
        const auto green = session_risk(live->data);
        // Classic follows 1.9.1: no score text at zero, the banner only above it.
        check((classic ? !contains(text, "(risk ") && !contains(text, "GREEN / 0 pts") : contains(text, "GREEN / 0 pts"))
            && workspace_has_background(green.tint.background),
            "known zero-risk unlocked workspace displays GREEN and renders its green background");
        state.controls_unlocked = false;
        text = frame(state, 1920, 1400);
        check(contains(text, classic ? "READ-ONLY" : "LOCKED") && !workspace_has_background(green.tint.background),
            "locking known zero-risk workspace restores a neutral background");
        live->data.i2c.status.recovery_pending = true;
        live->data.i2c.status.stale = true;
        live->data.telemetry_valid = false;
        live->data.telemetry_error = "Temporary read failure";
        text = frame(state, 1920, 1400);
        const auto red = session_risk(live->data);
        check(contains(text, classic ? "RED (risk 3) - A write on this path reaches hardware with no firmware clamp on it, or two software envelopes "
                "are off at once.  I2C: direct board-controller writes bypass the GPU's voltage request limits." : "RED / 3 pts")
            && workspace_has_background(red.tint.background),
            "locked workspace retains RED background for recovery-pending I2C despite transient reads");
        check(state.commands.empty() && !state.reconnect_requested && !state.refresh_requested,
            "risk rendering and tuning lock changes never release or replay a hardware request");
    }
    const auto restored = ImGui::GetStyle().Colors[ImGuiCol_WindowBg];
    check(near(restored.x, base_background.x) && near(restored.y, base_background.y)
        && near(restored.z, base_background.z) && near(restored.w, base_background.w),
        "workspace risk tint remains scoped and restores the shared style");
}

void compact_curve_layout() {
    ViewState state;
    state.presentation = Presentation::focused;
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

void press_key(ViewState& state, ImGuiKey key, bool shift = false, bool control = false, float width = 1366, float height = 900) {
    auto& io = ImGui::GetIO();
    io.DeltaTime = control ? 0.5f : 1.0f / 60.0f; // Honor legacy Undo's 0.4s repeat bound.
    io.AddKeyEvent(ImGuiMod_Shift, shift);
    io.AddKeyEvent(ImGuiMod_Ctrl, control);
    io.AddKeyEvent(key, true);
    frame(state, width, height, false);
    io.AddKeyEvent(key, false);
    io.AddKeyEvent(ImGuiMod_Shift, false);
    io.AddKeyEvent(ImGuiMod_Ctrl, false);
    frame(state, width, height, false);
    io.DeltaTime = 1.0f / 60.0f;
}

ImPlotPlot* popup_plot() {
    auto* window = ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    return window ? ImPlot::GetCurrentContext()->Plots.GetByKey(window->GetID("##live_curve")) : nullptr;
}

ImGuiWindow* inline_curve_window() {
    auto* root = ImGui::FindWindowByName("Druta workspace");
    if (!root) return nullptr;
    for (auto* body : ImGui::GetCurrentContext()->Windows) {
        if (body->ParentWindow != root || body->ChildId != root->GetID("workspace")) continue;
        for (auto* child : ImGui::GetCurrentContext()->Windows)
            if (child->ParentWindow == body && child->ChildId == body->GetID("inline-vf-editor")) return child;
    }
    return nullptr;
}

ImPlotPlot* inline_plot() {
    auto* window = inline_curve_window();
    return window ? ImPlot::GetCurrentContext()->Plots.GetByKey(window->GetID("##live_curve")) : nullptr;
}

void inline_curve_input() {
    ViewState state; state.preview = false; state.presentation = Presentation::classic;
    auto live = snapshot();
    live->data.curve_freq_div = 2; live->data.curve_step_khz = 15000;
    live->data.curve_writable = true; state.live = live;
    frame(state, 1920, 1400, false); frame(state, 1920, 1400, false);
    auto* root = ImGui::FindWindowByName("Druta workspace");
    if (root) for (auto* body : ImGui::GetCurrentContext()->Windows)
        if (body->ParentWindow == root && body->ChildId == root->GetID("workspace"))
            ImGui::SetScrollY(body, body->ScrollMax.y);
    frame(state, 1920, 1400, false);
    auto* window = inline_curve_window();
    auto* plot = inline_plot();
    check(window && window->Active && plot && state.curve_inline_active && !state.curve_window_open,
        "Classic Daily displays the live curve editor inline by default");
    check(state.commands.empty(), "displaying the inline editor does not write to hardware");
    if (!window || !plot) return;
    // 1.9.1 starts on the middle point of the read curve.
    check(state.selected_curve_point == live->data.curve[1].idx, "Classic selects the middle point of a fresh curve");
    ImGui::SetWindowFocus(window->Name); frame(state, 1920, 1400, false);
    press_key(state, ImGuiKey_W, false, false, 1920, 1400);
    check(state.curve_draft.draft()[1].delta_khz == 30000, "inline W stages one reported physical bin");
    press_key(state, ImGuiKey_W, true, false, 1920, 1400);
    check(state.curve_draft.draft()[1].delta_khz == 120000, "inline Shift+W stages three physical bins");
    press_key(state, ImGuiKey_A, false, false, 1920, 1400);
    check(state.selected_curve_point == live->data.curve[0].idx, "inline A selects the previous point");
    press_key(state, ImGuiKey_D, false, false, 1920, 1400);
    press_key(state, ImGuiKey_S, true, false, 1920, 1400);
    check(state.selected_curve_point == live->data.curve[1].idx && state.curve_draft.draft()[1].delta_khz == 30000,
        "inline D and Shift+S select and edit locally");
    ImGui::SetWindowFocus("Druta workspace"); frame(state, 1920, 1400, false);
    press_key(state, ImGuiKey_W, false, false, 1920, 1400);
    check(state.curve_draft.draft()[1].delta_khz == 60000, "Classic window-wide W stages one bin while inline editor is in the background");

    // The edited dot now sits above the first fit; fit the view to grab it.
    state.curve_fit_requested = true; frame(state, 1920, 1400, false);
    plot = inline_plot();
    const auto before = state.curve_draft.draft()[1];
    const ImVec2 dot(plot->Axes[ImAxis_X1].PlotToPixels(before.volt_mv),
        plot->Axes[ImAxis_Y1].PlotToPixels(before.freq_mhz));
    auto& io = ImGui::GetIO();
    io.AddMousePosEvent(dot.x, dot.y); frame(state, 1920, 1400, false);
    io.AddMouseButtonEvent(ImGuiMouseButton_Left, true); frame(state, 1920, 1400, false);
    check(state.curve_drag_point && *state.curve_drag_point == before.idx, "inline mouse-down grabs the selected curve point");
    io.AddMousePosEvent(dot.x, dot.y - 70); frame(state, 1920, 1400, false);
    io.AddMousePosEvent(1950, 1450); io.AddMouseButtonEvent(ImGuiMouseButton_Left, false);
    frame(state, 1920, 1400, false);
    check(!state.curve_drag_point && state.curve_draft.draft()[1].delta_khz != before.delta_khz,
        "inline drag stages a local edit and ends on release outside the editor");
    const auto retained = state.curve_draft.draft()[1].delta_khz;
    state.curve_window_open = true; frame(state, 1920, 1400, false);
    auto* popup = ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    check(!state.curve_inline_active && popup && popup->Active && popup_plot()
        && state.curve_draft.draft()[1].delta_khz == retained,
        "opening the popup replaces the inline editor and preserves its draft");
    state.curve_window_open = false; frame(state, 1920, 1400, false);
    window = inline_curve_window();
    check(state.curve_inline_active && window && window->Active && state.curve_draft.draft()[1].delta_khz == retained
        && state.commands.empty(), "closing the popup returns to one inline editor without applying the draft");
}

// Released 1.9.1 V/F editor in Classic: plan banner, statistics line, Python
// rows and series colours. Focused keeps its own editor untouched.
void classic_curve_editor_parity() {
    ViewState state; state.preview = false; state.page = Page::curve; state.presentation = Presentation::classic;
    auto live = snapshot();
    live->data.curve_freq_div = 1; live->data.curve_step_khz = 15000; live->data.curve_writable = true;
    state.live = live; state.controls_unlocked = true; state.curve_cap_mv = 1000;
    frame(state, 1600, 1200, false);
    // Earlier fixtures reuse this editor window and may have opened its header.
    if (auto* window = ImGui::FindWindowByName("V/F curve editor###druta_curve_editor"))
        window->StateStorage.SetInt(window->GetID("More V/F tools"), 0);
    frame(state, 1600, 1200, false);
    auto text = frame(state, 1600, 1200);
    check(contains(text, "APPLY TO GPU") && contains(text, "nothing staged") && contains(text, "'Apply V/F curve to GPU' will write"),
        "Classic shows the idle plan banner before anything is staged");
    check(contains(text, "2 points   top") && contains(text, "idx 1 @ 900.00 mV = 1890 MHz")
        && contains(text, "peak 1890 MHz held by 1 point(s), lowest = idx 1 @ 900.00 mV") && contains(text, "flat runs below cap: 0"),
        "Classic statistics line is computed from the adapter's points and the cap");
    check(contains(text, "voltage cap (mV)") && contains(text, "ramp floor (mV)") && contains(text, "Re-phase to 15 MHz increments")
        && contains(text, "selected") && contains(text, "Revert edits") && contains(text, "Apply V/F curve to GPU")
        && !contains(text, "Curve shape tools") && !contains(text, "Undo edit"),
        "Classic rows use the released labels; native-only tools stay behind a collapsed header");
    auto* popup = ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    ImGuiWindow* banner = nullptr;
    if (popup) for (auto* child : ImGui::GetCurrentContext()->Windows)
        if (child->ParentWindow == popup && child->ChildId == popup->GetID("vf_plan")) banner = child;
    auto* plot = popup_plot();
    check(banner && plot && banner->Pos.y + banner->Size.y <= plot->FrameRect.Min.y && near(banner->WindowBorderSize, 2),
        "the bordered plan banner sits above the editing row and plot");
    auto legend_color = [](ImPlotPlot* p, const char* name) -> ImU32 {
        if (p) for (int i = 0; i < p->Items.GetLegendCount(); ++i)
            if (std::string(p->Items.GetLegendLabel(i)) == name) return p->Items.GetLegendItem(i)->Color;
        return 0;
    };
    check(legend_color(plot, "current") == IM_COL32(76, 114, 176, 255) && legend_color(plot, "edited") == IM_COL32(221, 132, 82, 255)
        && legend_color(plot, "selected") == IM_COL32(85, 168, 104, 255), "Classic plot series use the released Deep colours");
    // 1.9.1 lists all seven series in its lowercase names, the unarmed hard floor
    // and the absent hold included, and draws the guides in WARN/VIOLET/BAD/GOOD.
    std::string legend;
    if (plot) for (int i = 0; i < plot->Items.GetLegendCount(); ++i) legend += std::string(i ? "|" : "") + plot->Items.GetLegendLabel(i);
    check(legend.rfind("current|edited|selected|cap|ramp floor|hard floor|held", 0) == 0 && !state.hard_mod_ack,
        "Classic legend lists 1.9.1's seven series, empty ones included");
    check(legend_color(plot, "cap") == IM_COL32(255, 203, 71, 255) && legend_color(plot, "ramp floor") == IM_COL32(160, 108, 255, 255)
        && legend_color(plot, "hard floor") == IM_COL32(255, 92, 92, 255) && legend_color(plot, "held") == IM_COL32(70, 209, 122, 255),
        "Classic guide lines use 1.9.1's cap, ramp floor, hard floor and held colours");
    check(plot && std::string(plot->GetAxisLabel(plot->Axes[ImAxis_X1])) == "mV" && std::string(plot->GetAxisLabel(plot->Axes[ImAxis_Y1])) == "MHz",
        "Classic plot axes carry 1.9.1's bare unit labels");
    check(contains(text, "drag a dot to move it  \xE2\x80\xA2  drag anywhere else to pan  \xE2\x80\xA2  A/D select  \xE2\x80\xA2  W/S move \xC2\xB1" "15 MHz  "
        "\xE2\x80\xA2  hold Shift for \xC2\xB1" "45  \xE2\x80\xA2  Ctrl+H hold the selected point (again to release)  \xE2\x80\xA2  Ctrl+Z / Ctrl+Y undo and redo staged edits")
        && !contains(text, "Drag dots"), "Classic prints 1.9.1's key help sentence with this card's clock bin");
    ImGui::ActivateItemByID(workspace_button("Read curve")); frame(state, 1600, 1200, false);
    check(state.refresh_requested && state.commands.empty(), "Read curve requests a fresh read and writes nothing");
    check(state.selected_curve_point == 1 && contains(text, "idx 1   900.00 mV   1890 MHz   delta +0 MHz   |   edits pending: 0"),
        "Classic opens on the middle point and prints 1.9.1's selected-point line");
    check(contains(text, "[ok] curve read: peak 1890 MHz, parks idx 1 @ 900.00 mV, 0 flat run(s) below 1094 mV")
        && !contains(text, "Curve ready"), "the status line mirrors the curve-read log entry instead of the draft's routine state");
    state.curve_frequency_mhz = 1920; state.curve_entry_dirty = true;
    ImGui::ActivateItemByID(workspace_button("Set MHz"));
    frame(state, 1600, 1200, false); frame(state, 1600, 1200, false);
    text = frame(state, 1600, 1200);
    check(state.curve_draft.dirty() && state.commands.empty() && contains(text, "ONE CLICK WRITES THIS")
        && contains(text, "Writes 1 edited point(s)") && contains(text, "[staged curve, 1 edit(s) not yet written]")
        && contains(text, "edits pending: 1"),
        "a staged edit turns the banner and statistics into the pre-click plan without writing");
    state.curve_draft.revert(); state.curve_draft.stage_mhz(1, -30);
    text = frame(state, 1600, 1200);
    check(contains(text, "ONE CLICK LOWERS THE PEAK") && contains(text, "LOWERS the curve's peak from 1890 to 1860 MHz"),
        "a plan that lowers the table peak takes the red headline");
    ImGui::ActivateItemByID(workspace_button(curve_label(state, "Apply curve"))); frame(state, 1600, 1200, false);
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::apply_curve
        && state.commands[0].curve_deltas.size() == 1 && state.commands[0].curve_deltas[0].delta_khz == -30000,
        "the green Apply on the selected-point row sends the staged changes exactly");
    state.commands.clear(); state.presentation = Presentation::focused;
    text = frame(state, 1600, 1200);
    check(!contains(text, "APPLY TO GPU") && contains(text, "Apply would request 1 changed point(s)") && contains(text, "Curve shape tools"),
        "Focused keeps its own curve editor");
    plot = popup_plot();
    check(legend_color(plot, "Draft") == ImGui::ColorConvertFloat4ToU32(ImVec4(0.38f, 0.71f, 1.0f, 1.0f)), "Focused plot colours are unchanged");
    check(plot && std::string(plot->GetAxisLabel(plot->Axes[ImAxis_X1])) == "Voltage (mV)" && legend_color(plot, "cap") == 0
        && contains(text, "Drag dots | A/D select"), "Focused keeps its axis labels, legend and key help");
    state.curve_window_open = false; frame(state, 1600, 1200, false);
}

// Released 1.9.1 Control page log and editor budget: the backend line and each
// vf_read's curve read are logged among the command results; the plot and banner
// share one budget; the log follows the editor. Fixture values are arbitrary.
void classic_curve_log_and_budget() {
    ViewState state; state.preview = false; state.presentation = Presentation::classic;
    auto live = snapshot(); auto& id = live->data.identity;
    id.size = sizeof(id); id.nvapi_ok = 1; id.nvml_ok = 0;
    std::snprintf(id.nvml_error, sizeof(id.nvml_error), "fixture library missing");
    std::snprintf(id.slot, sizeof(id.slot), "0000:02:00.0");
    live->data.curve_freq_div = 2; live->data.curve_writable = true; state.live = live;
    // Only drawn items are logged: keep the page scrolled to its end.
    auto bottom = [&]() {
        if (auto* root = ImGui::FindWindowByName("Druta workspace")) for (auto* body : ImGui::GetCurrentContext()->Windows)
            if (body->ParentWindow == root && body->ChildId == root->GetID("workspace")) ImGui::SetScrollY(body, body->ScrollMax.y);
    };
    frame(state, 1528, 1261, false); frame(state, 1528, 1261, false); bottom();
    frame(state, 1528, 1261, false); bottom();
    auto text = frame(state, 1528, 1261);
    // The log box is the last text on the page before the native draft/undo row,
    // so the last occurrence of each line is its place in the box.
    const auto oldest = text.rfind("[!!] Refused request");
    const auto backend = text.rfind("backend: NVAPI: ok   |   NVML: FAIL (fixture library missing)   |   slot 0000:02:00.0");
    const auto read = text.rfind("[ok] curve read: peak 1890 MHz, parks idx 1 @ 900.00 mV, 0 flat run(s) below 1094 mV");
    check(oldest != std::string::npos && backend != std::string::npos && read != std::string::npos,
        "Classic log holds the backend status and curve-read lines beside command results");
    check(oldest != std::string::npos && read < backend && backend < oldest,
        "the log box lists newest first: curve read, backend, then the earlier command result");
    check(!contains(text, "No tuning actions in this session.") && text.rfind("log  (newest line first)") < text.rfind("Revert local drafts"),
        "the log follows the editor and the native draft/undo row comes after it");
    // 1.9.1 calls vf_read only at startup, on a GPU switch, from Read curve and
    // after its successful curve-writing actions. The session's curve read after
    // any other command logs nothing and keeps a typed hard target, and the V/F
    // status line keeps that command's own result.
    auto command = [&](uint64_t log_id, const char* message, Command::Operation operation, bool ok) {
        live->log.push_back({log_id, message, ok}); live->completed_actions = log_id - 1;
        live->last_action_operation = operation; live->last_action_ok = ok; ++live->curve_revision;
        frame(state, 1528, 1261, false); bottom(); return frame(state, 1528, 1261);
    };
    const auto curve_reads = [](const std::string& t) {
        const auto box = t.rfind("log  (newest line first)");
        return box == std::string::npos ? size_t(0) : occurrences(t.substr(box), "[ok] curve read");
    };
    // Status mirror: the newest line drawn above the log box.
    const auto status_shows = [](const std::string& t, const char* line) {
        const auto box = t.rfind("log  (newest line first)"), at = t.rfind(line, box);
        return box != std::string::npos && at != std::string::npos && t.find(line, box) != std::string::npos;
    };
    state.hard_target_mhz = 1500;
    text = command(2, "Fan control returned to automatic", Command::Operation::stock, true);
    check(curve_reads(text) == 1 && near(state.hard_target_mhz, 1500) && status_shows(text, "[ok] Fan control returned to automatic"),
        "an unrelated command's curve read logs nothing, keeps the typed hard target and leaves its result on the status line");
    text = command(3, "Curve write refused by fixture", Command::Operation::apply_curve, false);
    check(curve_reads(text) == 1 && near(state.hard_target_mhz, 1500) && status_shows(text, "[!!] Curve write refused by fixture")
        && !status_shows(text, "[ok] Fan control returned to automatic"),
        "a refused V/F Apply is not a vf_read: its red result stays on the status line");
    text = command(4, "Applied curve fixture", Command::Operation::apply_curve, true);
    const auto box = text.rfind("log  (newest line first)");
    const auto newest = text.find("[ok] curve read", box), applied = text.find("[ok] Applied curve fixture", box);
    const auto backend_line = box == std::string::npos ? box : text.find("backend: NVAPI", box);
    check(curve_reads(text) == 2 && newest < applied && applied != std::string::npos && near(state.hard_target_mhz, 1890)
        && status_shows(text, "[ok] curve read: peak 1890 MHz"),
        "a successful V/F Apply re-reads as 1.9.1 does: logged newest and the hard target reseeded from the peak");
    check(backend_line != std::string::npos && text.find("backend: NVAPI", backend_line + 1) == std::string::npos,
        "the backend line is written once per adapter");
    ImGui::ActivateItemByID(inline_curve_window() ? inline_curve_window()->GetID("Read curve") : 0);
    frame(state, 1528, 1261, false);
    check(state.refresh_requested && state.curve_read_requested && state.commands.empty(), "inline Read curve asks for a fresh read and writes nothing");
    state.refresh_requested = false; ++live->curve_revision;
    frame(state, 1528, 1261, false); bottom(); text = frame(state, 1528, 1261);
    check(curve_reads(text) == 3 && !state.curve_read_requested, "the read Read curve asked for is logged once");
    ++live->curve_revision;
    frame(state, 1528, 1261, false); bottom(); text = frame(state, 1528, 1261);
    check(curve_reads(text) == 3, "a later detailed read nobody asked for logs nothing");

    // Budget: what the banner gains, the plot loses, so Apply and the plot stay together.
    auto* inline_window = inline_curve_window();
    auto banner_height = [&]() {
        if (inline_window) for (auto* child : ImGui::GetCurrentContext()->Windows)
            if (child->ParentWindow == inline_window && child->ChildId == inline_window->GetID("vf_plan")) return child->Size.y;
        return 0.0f;
    };
    auto* plot = inline_plot();
    check(inline_window && plot && inline_window->ScrollMax.y <= 1, "the inline editor is as tall as its contents and never scrolls inside");
    if (!inline_window || !plot) return;
    // Scrolled to the end, as tab_control-01: the editor rows, banner, plot and
    // the log below them are on screen together at 1528x1261.
    check(inline_window->ParentWindow && inline_window->Pos.y >= inline_window->ParentWindow->InnerClipRect.Min.y,
        "at 1528x1261 the whole V/F editor and the log fit one scrolled view");
    const float idle = banner_height() + plot->FrameRect.GetHeight();
    state.curve_draft.stage_mhz(1, -30);
    for (int n = 0; n < 3; ++n) { frame(state, 1528, 1261, false); bottom(); }
    plot = inline_plot();
    const float staged = banner_height() + plot->FrameRect.GetHeight();
    check(banner_height() > 0 && std::abs(staged - idle) <= 2, "a taller staged-plan banner takes its height from the plot");
    check(plot->FrameRect.GetHeight() < 1261 * 0.34f + 104, "the inline plot uses 1.9.1's budget, not the remaining window");
    // 1.9.1 takes its shares of the page under the menu bar, tab selector and
    // unlock strip (curve_share_height), not of the raw 1261 px client height:
    // that is about 80 px less, 27 px of plot budget and 10 px of log.
    check(idle <= std::floor(1261 * 0.34f) + 104 - 20, "the plot budget is a share of the page height, not the client height");
    // 1.9.1's own tree at 1528x1261 (share 1181 under a 24 px menu bar) records a
    // 386 px plot under a 119 px banner and a 153 px log. The share uses 1.9.1's
    // rule, text height + s(8) + s(28) + s(28), not the native 22 px menu row,
    // so both match exactly (the native frame rows gave 1185: 506 and 154).
    check(std::abs(idle - 505) <= 0.5f, "the banner and plot share 1.9.1's budget of the page under the pinned chrome");
    float log_box = 0;
    ImGuiWindow* log_window = nullptr;
    for (auto* window : ImGui::GetCurrentContext()->Windows)
        if (std::string(window->Name).find("##classic-log") != std::string::npos && window->Active) { log_box = window->Size.y; log_window = window; }
    check(log_box >= 90 && log_box <= std::floor(1261 * 0.13f) - 8, "the log box is 13% of the page height, not the client height");
    check(std::abs(log_box - 153) <= 0.5f, "the log box is 1.9.1's 13% of the page under the pinned chrome");
    // Python ui_sections.section ends every level-0 section with an 8 px spacer:
    // on tab_control-01 the I2C and V/F headers sit 38 px apart and the log box
    // starts 47 px under the plot. Here: two 22 px headers, the spacer and four
    // 4 px gaps between the knob columns and the editor; the spacer, separator,
    // caption row and their gaps between the editor and the log box.
    ImGuiWindow* knobs = nullptr;
    if (auto* body = inline_window->ParentWindow) for (auto* window : ImGui::GetCurrentContext()->Windows)
        if (window->ParentWindow == body && window->ChildId == body->GetID("classic-knob-columns")) knobs = window;
    check(knobs && std::abs(inline_window->Pos.y - (knobs->Pos.y + knobs->Size.y) - 68) <= 1,
        "the I2C regulator section is followed by 1.9.1's 8 px section spacer");
    check(log_window && std::abs(log_window->Pos.y - (inline_window->Pos.y + inline_window->Size.y) - 47) <= 1,
        "the V/F editor section is followed by 1.9.1's 8 px section spacer before the log");
    check(state.commands.empty(), "logging and layout write nothing");
}

// 1.9.1 reads the curve once its adapter is open, at startup and after a GPU
// switch. The session publishes the opening adapter before its first read, so
// that read still counts. Fixture values are arbitrary.
void classic_curve_read_on_open() {
    ViewState state; state.preview = false; state.presentation = Presentation::classic;
    auto live = snapshot(); const auto curve = live->data.curve;
    live->opening = true; live->data.connected = false; live->data.curve.clear(); live->curve_revision = 0;
    state.live = live;
    const auto reads = [&]() {
        size_t n = 0;
        for (const auto& line : state.classic_log_lines) n += line.text.rfind("curve read:", 0) == 0 ? 1 : 0;
        return n;
    };
    frame(state, 1528, 1261, false);
    check(reads() == 0, "an opening adapter logs no curve read before its curve is read");
    live->opening = false; live->data.connected = true; live->data.curve = curve; live->curve_revision = 1;
    frame(state, 1528, 1261, false);
    check(reads() == 1 && near(state.hard_target_mhz, 1890), "the first curve read after startup is logged and seeds the hard target");
    state.hard_target_mhz = 1500;
    live->completed_actions = 1; live->last_action_operation = Command::Operation::apply; live->last_action_ok = true; ++live->curve_revision;
    frame(state, 1528, 1261, false);
    check(reads() == 1 && near(state.hard_target_mhz, 1500), "a later unrelated command's curve read is not logged");
    // A GPU switch: the session clears the adapter's data under a new generation.
    ++live->generation; live->opening = true; live->data.connected = false; live->data.curve.clear();
    live->last_action_operation.reset(); live->last_action_ok = false;
    frame(state, 1528, 1261, false);
    check(reads() == 1, "a switching adapter logs nothing until its curve is read");
    live->opening = false; live->data.connected = true; live->data.curve = curve; ++live->curve_revision;
    frame(state, 1528, 1261, false);
    check(reads() == 2 && near(state.hard_target_mhz, 1890), "the switched adapter's first curve read is logged and reseeds the hard target");
    check(state.commands.empty(), "curve reads write nothing");
}

// The Classic log box shows 1.9.1's newest 40 lines; the native-only Copy log
// still copies every retained entry. Fixture entries are arbitrary.
std::string classic_copied_log;
void classic_copy_log_keeps_all_entries() {
    ViewState state; state.preview = false; state.presentation = Presentation::classic;
    auto live = snapshot();
    for (std::uint64_t n = 0; n < 60; ++n) live->log.push_back({500 + n, "copy fixture " + std::to_string(n) + ";", true});
    state.live = live;
    ImGuiWindow* body = nullptr;
    auto bottom = [&]() {
        if (auto* root = ImGui::FindWindowByName("Druta workspace")) for (auto* window : ImGui::GetCurrentContext()->Windows)
            if (window->ParentWindow == root && window->ChildId == root->GetID("workspace")) { body = window; ImGui::SetScrollY(window, window->ScrollMax.y); }
    };
    for (int n = 0; n < 3; ++n) { frame(state, 1528, 1261, false); bottom(); }
    const auto shown = frame(state, 1528, 1261);
    check(contains(shown, "[ok] copy fixture 59;") && !contains(shown, "[ok] copy fixture 19;"),
        "the Classic log box shows the newest 40 entries");
    // 1.9.1 has no button beside the caption: Copy log is in the box's right-click menu.
    check(!contains(shown, "Copy log") && contains(shown, "log  (newest line first)"), "the Classic log caption carries no button");
    ImGuiWindow* log_box = nullptr;
    for (auto* window : ImGui::GetCurrentContext()->Windows)
        if (std::string(window->Name).find("##classic-log") != std::string::npos && window->Active) log_box = window;
    auto& io = ImGui::GetIO();
    if (log_box) io.AddMousePosEvent(log_box->Pos.x + log_box->Size.x * 0.5f, log_box->Pos.y + log_box->Size.y * 0.5f);
    frame(state, 1528, 1261, false);
    io.AddMouseButtonEvent(ImGuiMouseButton_Right, true); frame(state, 1528, 1261, false);
    io.AddMouseButtonEvent(ImGuiMouseButton_Right, false); frame(state, 1528, 1261, false);
    const auto& popups = ImGui::GetCurrentContext()->OpenPopupStack;
    ImGuiWindow* menu = popups.empty() ? nullptr : popups.back().Window;
    check(log_box && menu, "a right-click on the Classic log box opens its menu");
    auto& platform = ImGui::GetPlatformIO();
    const auto previous = platform.Platform_SetClipboardTextFn;
    classic_copied_log.clear();
    platform.Platform_SetClipboardTextFn = [](ImGuiContext*, const char* text) { classic_copied_log = text ? text : ""; };
    if (menu) ImGui::ActivateItemByID(menu->GetID("Copy log"));
    frame(state, 1528, 1261, false); frame(state, 1528, 1261, false);
    platform.Platform_SetClipboardTextFn = previous;
    io.AddMousePosEvent(-FLT_MAX, -FLT_MAX); frame(state, 1528, 1261, false);
    const auto newest = classic_copied_log.find("[ok] copy fixture 59;"), oldest = classic_copied_log.find("[ok] copy fixture 0;");
    check(body && newest != std::string::npos && oldest != std::string::npos && newest < oldest
        && classic_copied_log.find("[ok] copy fixture 19;") != std::string::npos,
        "Copy log copies every retained entry, newest first, not only the 40 shown");
    check(state.commands.empty(), "copying the log writes nothing");
}

// 1.9.1 vf_apply logs its plan as the write's receipt before the write; Reset all
// and Max it log one line per step with the result last; a curve reset reads as
// one; the V/F status line mirrors one line. Messages and values are fixtures.
void classic_write_receipts() {
    ViewState state; state.preview = false; state.presentation = Presentation::classic; state.controls_unlocked = true;
    auto live = snapshot(); live->data.curve_writable = live->data.curve_reset_writable = true; live->data.telemetry_valid = true;
    state.live = live;
    const auto bottom = [&]() {
        if (auto* root = ImGui::FindWindowByName("Druta workspace")) for (auto* body : ImGui::GetCurrentContext()->Windows)
            if (body->ParentWindow == root && body->ChildId == root->GetID("workspace")) ImGui::SetScrollY(body, body->ScrollMax.y);
    };
    const auto draw_text = [&]() { frame(state, 1528, 1261, false); bottom(); frame(state, 1528, 1261, false); return frame(state, 1528, 1261); };
    draw_text(); state.curve_cap_mv = 1000;
    const auto apply = [&]() {
        draw_text(); auto* editor = inline_curve_window();
        ImGui::ActivateItemByID(editor ? editor->GetID("Apply V/F curve to GPU") : 0); frame(state, 1528, 1261, false);
        const bool sent = state.commands.size() == 1 && state.commands[0].operation == Command::Operation::apply_curve;
        state.commands.clear(); return sent;
    };
    state.curve_draft.stage_mhz(1, -30);
    check(apply() && state.classic_log_lines.back().text == "WARNING: this LOWERS the curve's peak from 1890 to 1860 MHz. writing 1 edited point(s): "
        "top \xE2\x89\xA4" "1000 mV becomes 1860 MHz, peak 1860 MHz held by 1 point(s), the card parks at idx 1 @ 900.00 mV"
        && state.classic_log_lines.back().ok == false, "a peak-lowering Apply logs 1.9.1's receipt as [!!] before the write");
    state.curve_draft.revert(); state.curve_draft.stage_mhz(1, 30);
    check(apply() && state.classic_log_lines.back().text == "writing 1 edited point(s): top \xE2\x89\xA4" "1000 mV becomes 1920 MHz, "
        "peak 1920 MHz held by 1 point(s), the card parks at idx 1 @ 900.00 mV" && !state.classic_log_lines.back().ok,
        "a raising Apply logs its receipt untagged");
    state.curve_draft.revert();
    const auto receipts = state.classic_log_lines.size();
    apply();
    check(state.classic_log_lines.size() == receipts + 1 && state.classic_log_lines.back().text == "no edits to apply",
        "Apply with nothing staged logs no receipt");

    uint64_t id = 1;
    const auto result = [&](const char* message, Command::Operation operation, bool ok) {
        live->log.push_back({++id, message, ok, operation}); live->last_action_log_id = id; ++live->completed_actions;
        live->last_action_operation = operation; live->last_action_ok = ok;
        return draw_text();
    };
    // The box is the last text on the page; the status line is the newest line above it.
    // ImGui's log capture ends lines with IM_NEWLINE ("\r\n" here): compare on "\n".
    const auto box_of = [](const std::string& t) {
        const auto at = t.rfind("log  (newest line first)");
        auto box = at == std::string::npos ? std::string() : t.substr(at);
        box.erase(std::remove(box.begin(), box.end(), '\r'), box.end()); return box;
    };
    const auto status_of = [](const std::string& t) { const auto at = t.rfind("log  (newest line first)"); return at == std::string::npos ? t : t.substr(0, at); };
    auto text = result("Reset controls completed\ncore offset: fixture offset zeroed\nfan: fixture fan to auto", Command::Operation::reset_controls, true);
    auto box = box_of(text);
    const auto summary = box.find("[ok] reset to stock complete"), fan = box.find("\nfan: fixture fan to auto"), core = box.find("\ncore offset: fixture offset zeroed");
    check(summary != std::string::npos && fan != std::string::npos && core != std::string::npos && summary < fan && fan < core
        && !contains(text, "Reset controls completed"), "a completed reset logs each step on its own line, then 1.9.1's summary");
    check(contains(status_of(text), "[ok] reset to stock complete") && !contains(status_of(text), "fixture offset zeroed"),
        "the status line mirrors the summary alone");
    text = result("Reset incomplete\ncore offset: fixture refusal\nfan: fixture fan to auto", Command::Operation::reset_controls, false);
    box = box_of(text);
    check(contains(box, "[!!] reset incomplete\nfan: fixture fan to auto\ncore offset: fixture refusal") && !contains(text, "Reset incomplete"),
        "an incomplete reset without step results ends in [!!]; its step lines stay untagged");
    text = result("Max it applied: fixture fan step\nfixture power step\nfixture hold step", Command::Operation::max_it, true);
    box = box_of(text);
    check(contains(box, "[ok] Max it applied\nmax: fixture hold step\nmax: fixture power step\nmax: fixture fan step")
        && contains(status_of(text), "[ok] Max it applied") && !contains(status_of(text), "fixture hold step"),
        "Max it logs one 'max:' line per step, newest first, under its result");
    // With the steps the session copies from the core's result: 1.9.1 tags each
    // step and logs a Reset step's message without its "name: "; lines that are
    // not a step stay untagged.
    const auto stepped = [&](const char* message, Command::Operation operation, bool ok, std::vector<ActionStep> steps) {
        live->log.push_back({++id, message, ok, operation, false, std::move(steps)}); live->last_action_log_id = id; ++live->completed_actions;
        live->last_action_operation = operation; live->last_action_ok = ok;
        return draw_text();
    };
    text = stepped("Reset controls completed\ncore offset: fixture offset zeroed\nfan: fixture fan to auto\nUndo unavailable for uncaptured controls: fixture rail",
        Command::Operation::reset_controls, true, {{"core offset", true, "fixture offset zeroed"}, {"fan", true, "fixture fan to auto"}});
    box = box_of(text);
    check(contains(box, "[ok] reset to stock complete\nUndo unavailable for uncaptured controls: fixture rail\n[ok] fixture fan to auto\n[ok] fixture offset zeroed\n")
        && !contains(box, "[ok] fan: ") && contains(status_of(text), "[ok] reset to stock complete"),
        "every reset step reports ok: each is [ok] without its name, then 1.9.1's summary");
    text = stepped("Reset incomplete\ncore offset: fixture refusal\nfan: fixture fan to auto\nfixture core error", Command::Operation::reset_controls, false,
        {{"core offset", false, "fixture refusal"}, {"fan", true, "fixture fan to auto"}});
    box = box_of(text);
    check(contains(box, "[!!] reset incomplete: 1 step(s) failed\nfixture core error\n[ok] fixture fan to auto\n[!!] fixture refusal\n")
        && contains(status_of(text), "[!!] reset incomplete: 1 step(s) failed"), "a failed reset step is [!!] and 1.9.1's summary counts it");
    text = stepped("Max it incomplete; captured Undo retained: fixture fan step\nfixture power refusal; fixture history notice", Command::Operation::max_it, false,
        {{"fan", true, "fixture fan step"}, {"board power", false, "fixture power refusal"}});
    box = box_of(text);
    check(contains(box, "[!!] Max it incomplete; captured Undo retained\n[!!] max: fixture power refusal; fixture history notice\n[ok] max: fixture fan step\n"),
        "each Max it step carries its own result, a trailing history notice included");
    text = result("V/F curve applied", Command::Operation::reset_curve, true);
    check(contains(box_of(text), "[ok] V/F curve reset to stock") && !contains(text, "V/F curve applied"),
        "a curve reset the core left unnamed reads as a reset");
    text = result("V/F curve applied", Command::Operation::apply_curve, true);
    check(contains(box_of(text), "[ok] V/F curve applied\n[ok] V/F curve reset to stock"), "an applied curve keeps its own wording");
    text = result("fixture first line\nfixture second line", Command::Operation::apply, true);
    check(contains(box_of(text), "[ok] fixture first line\nfixture second line") && contains(status_of(text), "[ok] fixture second line")
        && !contains(status_of(text), "fixture first line"), "another multi-line result stays whole in the box; the status line keeps its last line");
    state.presentation = Presentation::focused; text = frame(state, 1528, 1261);
    check(contains(text, "Reset controls completed") && contains(text, "Max it applied: fixture fan step") && !contains(text, "reset to stock complete"),
        "Focused keeps the session's own result text");
    check(contains(text, "core offset: fixture refusal") && contains(text, "fixture power refusal; fixture history notice")
        && !contains(text, "step(s) failed") && !contains(text, "max: fixture"), "Focused keeps one entry per result when steps are reported");
    check(state.commands.empty(), "logging the results writes nothing");
}

// 1.9.1 copy_rail_limit_diagnostics confirms the copy with one [ok] log line.
void classic_copy_info_logs() {
    ViewState state; state.preview = false; state.presentation = Presentation::classic;
    auto live = snapshot(); state.live = live;
    frame(state, 1528, 1261, false); frame(state, 1528, 1261, false);
    auto& platform = ImGui::GetPlatformIO();
    const auto previous = platform.Platform_SetClipboardTextFn;
    classic_copied_log.clear();
    platform.Platform_SetClipboardTextFn = [](ImGuiContext*, const char* text) { classic_copied_log = text ? text : ""; };
    auto* root = ImGui::FindWindowByName("Druta workspace");
    ImGui::ActivateItemByID(root ? root->GetID("Copy info") : 0); frame(state, 1528, 1261, false);
    platform.Platform_SetClipboardTextFn = previous;
    check(!classic_copied_log.empty() && !state.classic_log_lines.empty()
        && state.classic_log_lines.back().text == "rail detection details copied to clipboard" && state.classic_log_lines.back().ok == true,
        "Copy info copies the rail details and logs 1.9.1's confirmation");
    check(state.commands.empty(), "copying writes nothing");
}

// 1.9.1 build_vf tooltips, with this card's clock grid and the editor's own
// constants. The fixture grid is arbitrary.
void classic_vf_tooltips() {
    ViewState state; state.preview = false; state.page = Page::curve; state.presentation = Presentation::classic;
    auto live = snapshot(); live->data.curve_step_khz = 12657; live->data.curve_writable = true;
    state.live = live; state.controls_unlocked = true;
    frame(state, 1600, 1200, false); frame(state, 1600, 1200, false);
    auto* editor = ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    check(editor != nullptr, "tooltip fixture has a curve editor");
    if (!editor) return;
    // Read curve's rect places the cap input to its left and Re-phase to its right.
    ImGui::ActivateItemByID(editor->GetID("Read curve")); frame(state, 1600, 1200, false);
    const auto read = ImGui::WindowRectRelToAbs(editor, editor->NavRectRel[ImGuiNavLayer_Main]);
    auto& io = ImGui::GetIO();
    const auto hover = [&](float x) { io.AddMousePosEvent(x, read.GetCenter().y); frame(state, 1600, 1200, false); return frame(state, 1600, 1200); };
    // The cap input's text field: left of its step buttons.
    auto text = hover(read.Min.x - 100);
    check(contains(text, "Voltage ceiling for the deflattener (clamped 600-1300 mV).") && contains(text, "VF points are 6.25 mV apart")
        && contains(text, "snaps it DOWN onto that same 6.25 mV grid"), "the cap input carries 1.9.1's tooltip");
    text = hover(read.Max.x + 60);
    check(contains(text, "Puts every point back on ONE 12.66 MHz phase - this") && contains(text, "floor((base+delta)/step)*step")
        && contains(text, "can only drop, by at most 12.66 MHz.") && contains(text, "Rephase is NOT a reset function."),
        "Re-phase carries 1.9.1's tooltip with this card's grid");
    io.AddMousePosEvent(-FLT_MAX, -FLT_MAX); frame(state, 1600, 1200, false);
    state.commands.clear(); state.refresh_requested = state.curve_read_requested = false;
    state.curve_window_open = false; frame(state, 1600, 1200, false);
}

// Vertices drawn in `colour` by `window` inside `rect`.
size_t colour_vertices_in(const ImGuiWindow* window, ImU32 colour, const ImRect& rect) {
    size_t n = 0;
    if (window) for (const auto& vertex : window->DrawList->VtxBuffer) n += vertex.col == colour && rect.Contains(vertex.pos);
    return n;
}

// Python draws the read-out, the knob labels and a present live reading in its
// TEXT colour (230,232,236); widgets and headers keep the theme's white.
void classic_text_colour() {
    ViewState state; state.preview = false; state.presentation = Presentation::classic;
    auto live = snapshot(); live->data.telemetry_valid = true;
    auto& t = live->data.telemetry; t.present = DC_T_CORE | DC_T_PSTATE; t.core = 1500; t.pstate = 0;
    auto& core = live->data.controls[static_cast<size_t>(ControlId::core)];
    core.availability = Availability::available; core.current = 0; core.low = -100; core.high = 100; core.writable = true; core.unit = "MHz";
    state.live = live;
    for (int n = 0; n < 3; ++n) frame(state, 1528, 1261, false);
    const ImU32 text = IM_COL32(230, 232, 236, 255), white = IM_COL32(255, 255, 255, 255);
    ImGuiWindow* body = nullptr; ImGuiWindow* knobs = nullptr;
    if (auto* root = ImGui::FindWindowByName("Druta workspace")) for (auto* window : ImGui::GetCurrentContext()->Windows)
        if (window->ParentWindow == root && window->ChildId == root->GetID("workspace")) body = window;
    if (body) for (auto* window : ImGui::GetCurrentContext()->Windows)
        if (window->ParentWindow == body && window->ChildId == body->GetID("classic-knob-columns")) knobs = window;
    check(body && knobs, "the Control page body and knob columns exist");
    if (!body || !knobs) return;
    // The two-line read-out sits above the knob columns.
    const ImRect readout(body->Pos, ImVec2(body->Pos.x + body->Size.x, knobs->Pos.y - ImGui::GetFrameHeight()));
    check(colour_vertices_in(body, text, readout) > 0 && colour_vertices_in(body, white, readout) == 0,
        "the read-out is drawn in TEXT, not white");
    // The core row is the Clock offsets table's first row: its label (column 0)
    // and its live reading (column 3).
    const auto* table = ImGui::TableFindByID(ImHashStr("classic-clock-offsets", 0, knobs->GetID("classic-control-columns")));
    check(table != nullptr, "the Clock offsets knob table exists");
    if (!table) return;
    const float top = table->OuterRect.Min.y, bottom = top + ImGui::GetFrameHeight() + 2;
    const ImRect label(ImVec2(table->Columns[0].MinX, top), ImVec2(table->Columns[0].MaxX, bottom));
    const ImRect reading(ImVec2(table->Columns[3].MinX, top), ImVec2(table->Columns[3].MaxX, bottom));
    check(colour_vertices_in(knobs, text, label) > 0 && colour_vertices_in(knobs, white, label) == 0,
        "a knob label is drawn in TEXT, not white");
    check(colour_vertices_in(knobs, text, reading) > 0 && colour_vertices_in(knobs, white, reading) == 0,
        "a present live reading is drawn in TEXT, not white");
    check(state.commands.empty(), "colouring writes nothing");
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
    if (presentation == Presentation::classic) {
        // 1.9.1 opens on the middle point of the read order; walk back to the first.
        check(state.selected_curve_point == 11, "Classic selects the middle actual point of a sparse curve");
        press_key(state, ImGuiKey_A, true);
        press_key(state, ImGuiKey_A);
    }
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
    ImGui::ActivateItemByID(workspace_button(curve_label(state, "Revert local curve")));
    frame(state, 1366, 900, false);
    if (presentation == Presentation::classic)
        // 1.9.1 vf_revert keeps the editor history and records no step for a clean draft.
        check(!state.curve_draft.dirty() && state.curve_draft.undo_label() == "nudge idx 9 -37 MHz",
            "Classic Revert of a clean draft keeps the key history and adds no step");
    else check(!state.curve_draft.dirty() && !state.curve_draft.can_undo(), "local Revert clears staged key history");

    auto* popup = ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    check(popup != nullptr, "large editor window exists");
    if (!popup) return;
    ImGui::ActivateItemByID(popup->GetID(curve_label(state, "MHz")));
    ImGui::GetCurrentContext()->NavNextActivateFlags = ImGuiActivateFlags_PreferInput;
    frame(state, 1366, 900, false);
    check(state.curve_entry_editing, "numeric field accepts real ImGui navigation activation");
    press_key(state, ImGuiKey_W);
    check(!state.curve_draft.dirty(), "W typed while numeric input is active does not change a curve point");
    ImGui::SetWindowFocus("Druta workspace");
    frame(state, 1366, 900, false);
    press_key(state, ImGuiKey_W);
    if (presentation == Presentation::classic)
        check(state.curve_draft.draft()[0].delta_khz == 25314, "Classic retains window-wide WASD outside the editor");
    else check(!state.curve_draft.dirty(), "Focused background editor cannot consume WASD");
    ImGui::SetWindowFocus("V/F curve editor###druta_curve_editor");
    frame(state, 1366, 900, false);
    // Start the drag-history check from a clean editor; Classic's preceding
    // window-wide shortcut deliberately created its own separate Undo entry.
    ImGui::ActivateItemByID(workspace_button(curve_label(state, "Revert local curve")));
    frame(state, 1366, 900, false);
    if (presentation == Presentation::classic) {
        // 1.9.1 vf_revert is itself one undo step: Ctrl+Z brings the edit back.
        check(!state.curve_draft.dirty() && state.curve_draft.undo_label() == "revert edits", "Classic Revert records one undo step");
        press_key(state, ImGuiKey_Z, false, true);
        check(state.curve_draft.draft()[0].delta_khz == 25314 && !state.classic_log_lines.empty()
            && state.classic_log_lines.back().text.rfind("undo: revert edits (", 0) == 0
            && contains(state.classic_log_lines.back().text, ") - 1 edit(s) still staged") && !state.classic_log_lines.back().ok,
            "Ctrl+Z after Revert restores the reverted edit and logs 1.9.1's untagged undo line");
        // Only a curve read clears the editor history (1.9.1 vf_read). Over a
        // staged edit the first Read curve press only warns.
        ImGui::ActivateItemByID(workspace_button("Read curve")); frame(state, 1366, 900, false);
        check(!state.curve_read_requested && state.curve_draft.dirty() && state.classic_log_lines.back().text
            == "1 unapplied edit(s) would be discarded - press Read curve again to confirm, or Apply first"
            && state.classic_log_lines.back().ok == false, "Read curve over a staged edit first warns and reads nothing");
        ImGui::ActivateItemByID(workspace_button("Read curve")); frame(state, 1366, 900, false);
        check(state.curve_read_requested && state.refresh_requested, "the second Read curve press asks for the read");
        state.refresh_requested = false;
        const auto logged_before = state.classic_log_lines.size();
        ++live->completed_reads; ++live->curve_revision; frame(state, 1366, 900, false);
        bool read_logged = false;
        for (size_t i = logged_before; i < state.classic_log_lines.size(); ++i)
            read_logged = read_logged || state.classic_log_lines[i].text.rfind("curve read: ", 0) == 0;
        check(!state.curve_read_requested && read_logged, "the read that answers Read curve is logged");
    }
    check(!state.curve_draft.dirty() && !state.curve_draft.can_undo(), "drag gesture fixture starts with cleared local history");
    // Classic's plan banner resizes to its text, so the plot settles a frame
    // after a revert; aim at the dot where it now stays.
    frame(state, 1366, 900, false); frame(state, 1366, 900, false);

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
    // Let Classic's text-sized plan banner settle after the redo before aiming.
    frame(state, 1366, 900, false); frame(state, 1366, 900, false);
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
    live->data.i2c.control_profile = "mp2888a-pmbus";
    live->data.i2c.control_profiles = {{"mp2888a-pmbus", "Adapter qualified manual PMBus",
        "Nominal requests only; not physical voltage", true, 6.25, {400, 875, 1125, 1700}}};
    state.i2c_port = 3; state.i2c_address = 0x42; state.i2c_target_mv = 1125;
    constexpr const char* panel_name = "I2C UI wiring contract";
    auto panel_frame = [&] {
        ImGui::GetIO().DisplaySize = {1200, 900}; ImGui::NewFrame();
        ImGui::SetNextWindowPos({0, 0}); ImGui::SetNextWindowSize({1200, 900});
        ImGui::Begin(panel_name, nullptr, ImGuiWindowFlags_NoSavedSettings);
        draw_mode_bar(*live, state.controls_unlocked, state.commands, presentation == Presentation::classic,
            [&] { draw_i2c_gate(state); });
        auto* body = ImGui::GetCurrentWindow();
        body->StateStorage.SetInt(ImHashStr("Advanced routing", 0, body->GetID("i2c-controller")), 1);
        draw_i2c_panel(state);
        ImGui::End(); ImGui::Render();
        check(ImGui::GetDrawData()->CmdListsCount > 0, "I2C panel renders from snapshot without a backend");
    };
    auto apply_window = [&]() -> ImGuiWindow* {
        auto* window = ImGui::FindWindowByName(panel_name);
        if (state.presentation == Presentation::classic && window)
            for (auto* child : ImGui::GetCurrentContext()->Windows)
                if (child->ParentWindow == window && child->ChildId == ImHashStr("classic-i2c-manual",0,window->GetID("i2c-controller"))) return child;
        return window;
    };
    auto panel_id = [&](const char* label) {
        auto* window = ImGui::FindWindowByName(panel_name);
        if (state.presentation == Presentation::classic && std::string(label) == "Apply") {
            auto* manual = apply_window();
            return manual ? ImHashStr(label,0,manual->GetID("manual-command")) : ImGuiID{0};
        }
        return window ? ImHashStr(label, 0, window->GetID("i2c-controller")) : ImGuiID{0};
    };
    auto activate_panel = [&](const char* label) { ImGui::ActivateItemByID(panel_id(label)); panel_frame(); };
    auto exact_route = [&](const Command& command) {
        return command.i2c_route && command.i2c_route->port == 3 && command.i2c_route->addr7 == 0x42;
    };
    panel_frame(); panel_frame();
    check(state.commands.empty(), "showing the I2C panel does not probe or write automatically");
    activate_panel("Connect / Scan");
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_scan,
        "legacy Connect / Scan is an explicit discovery intent without a voltage request");
    state.commands.clear();
    live->data.i2c.candidates = {{I2cRoute{2, 0x31}, 710}, {I2cRoute{3, 0x42}, 825}};
    activate_panel("##controller-candidate");
    auto* choices = ImGui::FindWindowByName("##Combo_00");
    check(choices && choices->Active, "detected controller choices open as a friendly dropdown");
    if (choices) {
        ImGui::ActivateItemByID(choices->GetID("2: MPS MP2888A - port 3, 0x42 (unverified candidate)")); panel_frame();
        check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_select
            && state.commands[0].i2c_family==I2cFamily::mp2888 && exact_route(state.commands[0]), "friendly candidate selection sends the selected detected route without Apply");
        state.commands.clear();
    }
    activate_panel("Probe route");
    check(state.commands.empty(), "manual route requires an explicit controller family");
    state.i2c_scan_scope=static_cast<int>(I2cFamily::mp2888);
    activate_panel("Probe route");
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_select
        && exact_route(state.commands[0]), "Probe queues the exact edited route instead of a developer-board route");
    state.commands.clear();
    live->data.i2c.selected = I2cRoute{3, 0x42}; live->data.i2c.family=I2cFamily::mp2888;
    live->data.i2c.status.min_mv = 500; live->data.i2c.status.max_mv = 1600;
    live->data.i2c.status.capable = true; live->data.i2c.status.writable = true;
    state.controls_unlocked=true; live->data.i2c.gate=true;
    panel_frame();
    activate_panel("Nominal request preset");
    auto* presets = ImGui::FindWindowByName("##Combo_00");
    check(presets && presets->Active, "qualified nominal request presets are available without manual route entry");
    if (presets) {
        ImGui::ActivateItemByID(presets->GetID("875.00 mV nominal")); panel_frame();
        check(near(state.i2c_target_mv,875) && state.commands.empty(),
            "selecting a nominal preset stages its target without Verify, Apply or gate commands");
        state.presentation = presentation == Presentation::classic ? Presentation::focused : Presentation::classic;
        panel_frame();
        check(near(state.i2c_target_mv,875) && state.commands.empty(),
            "preset draft survives layout changes and another snapshot render without automatic SET");
        state.presentation=presentation; state.i2c_target_mv=1125;
    }
    live->data.i2c.gate=false;
    auto* window = ImGui::FindWindowByName(panel_name);
    live->data.xoc_known = live->data.rail_mode_known = true;
    live->data.xoc_enabled = live->data.rail_mode_enabled = true;
    state.controls_unlocked = true;
    live->data.i2c.status.owned = true;
    activate_panel("Read controller");
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_read,
        "Read controller emits only its own explicit intent even with both global modes enabled");
    check(live->data.xoc_enabled && live->data.rail_mode_enabled,
        "rendering an I2C read leaves the established XOC and Rail modes intact");
    check(panel_id("Read controller") != window->GetID("XOC")
        && panel_id("Read controller") != window->GetID("Rail limits"),
        "I2C Read and both global mode controls have independent widget identities");
    state.commands.clear(); live->busy = true;
    ImGui::ActivateItemByID(window->GetID("XOC")); panel_frame();
    check(state.commands.empty(), "an in-flight read temporarily disables global mode submission");
    live->busy = false;
    ImGui::ActivateItemByID(window->GetID("XOC")); panel_frame();
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::set_xoc
        && !state.commands[0].enabled, "XOC remains clickable after Read completes even while I2C owns a request");
    state.commands.clear();
    ImGui::ActivateItemByID(window->GetID("Rail limits")); panel_frame();
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::set_rail_mode
        && !state.commands[0].enabled, "Rail limits remains clickable after Read completes even while I2C owns a request");
    state.commands.clear(); live->data.i2c.status.owned = false; state.controls_unlocked = false;
    ImGui::ActivateItemByID(window->GetID("I2C rail")); panel_frame();
    check(state.commands.empty(), "locked I2C gate cannot arm a controller");
    state.controls_unlocked = true;
    ImGui::ActivateItemByID(window->GetID("I2C rail")); panel_frame();
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_gate
        && state.commands[0].enabled && exact_route(state.commands[0]), "I2C gate emits a route-bound explicit intent");
    state.commands.clear();
    activate_panel("Verify response");
    check(state.commands.empty(), "write verification remains disabled until the I2C gate is confirmed");
    check(!live->data.i2c.gate && !live->data.i2c.verified,
        "Verify click does not optimistically change the gate or verified response state");
    state.commands.clear();
    live->data.i2c.gate = true; live->data.i2c.status.capable = false;
    activate_panel("Verify response");
    check(state.commands.empty(), "Verify requires the reported controller capability");
    live->data.i2c.status.capable = true;
    live->data.i2c.control_profiles[0].available = false;
    activate_panel("Verify response");
    check(state.commands.empty(), "read-only transport retains inspection but cannot offer response Verify writes");
    live->data.i2c.control_profiles[0].available = true;
    activate_panel("Verify response");
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_verify
        && !state.commands[0].enabled && exact_route(state.commands[0]), "Verify queues exactly the selected route without implicitly arming the gate");
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
    ImGui::ActivateItemByID(window->GetID("I2C rail")); panel_frame();
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_gate
        && !state.commands[0].enabled && exact_route(state.commands[0]),
        "an already armed I2C gate can be explicitly disabled while controls are locked");
    state.commands.clear();
    activate_panel("Apply");
    check(state.commands.empty(), "manual Apply still requires unlock");
    state.controls_unlocked = true;
    activate_panel("Apply");
    check(state.commands.size() == 1 && state.commands[0].operation == Command::Operation::i2c_apply
        && exact_route(state.commands[0]) && near(state.commands[0].value, 1125),
        "manual Apply button is wired to the route and exact visible millivolt target");
    state.commands.clear();
    window = apply_window();
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

void explicit_hold_and_global_actions(Presentation presentation) {
    ViewState state; state.preview=false; state.presentation=presentation;
    auto live=snapshot(); state.live=live;
    frame(state,1920,1280); state.controls_unlocked=true;
    // Classic carries 1.9.1's label, two spaces before the bracket.
    const char* max_it=presentation==Presentation::classic ? "Max it  (fan + power + volts + curve)" : "Max it (fan + power + volts + curve)";
    for(const char* label:{"Reset controls",max_it}) {
        ImGui::ActivateItemByID(header_button(label)); frame(state,1920,1280);
    }
    check(state.commands.empty(),"unavailable Reset and Max it buttons cannot emit placeholder actions");
    state.curve_window_open=true; frame(state,1920,1280);
    ImGui::ActivateItemByID(workspace_button("Hold selected point")); frame(state,1920,1280);
    check(state.commands.empty(),"unavailable Hold cannot emit a placeholder lock command");
    state.curve_window_open=false;
    live->data.reset_controls_available=live->data.max_it_available=live->data.hold_available=true;
    state.curve_cap_mv=1050; state.curve_floor_mv=925; state.hold_headroom=false; state.headroom_margin_mv=37.5;
    ImGui::ActivateItemByID(header_button("Reset controls")); frame(state,1920,1280);
    if (presentation==Presentation::classic) {
        check(state.commands.empty() && state.classic_reset_deadline>ImGui::GetTime(),
            "Classic Reset first press visibly arms without queuing a hardware operation");
        ImGui::ActivateItemByID(header_button("Reset controls")); frame(state,1920,1280);
    }
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::reset_controls,
        "Reset controls queues its explicit operation only after capability and unlock are established");
    state.commands.clear();
    ImGui::ActivateItemByID(header_button(max_it)); frame(state,1920,1280);
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::max_it
        && state.commands[0].expected_curve.size()==live->data.curve.size()
        && state.commands[0].expected_curve_freq_div==live->data.curve_freq_div
        && near(state.commands[0].curve_cap_mv,1050) && near(state.commands[0].curve_floor_mv,925)
        && !state.commands[0].hold_headroom && near(state.commands[0].headroom_margin_mv,37.5),
        "Max it carries the visible planner settings and complete captured curve guard");
    state.commands.clear(); state.curve_window_open=true; state.selected_curve_point=1;
    frame(state,1920,1280);
    ImGui::ActivateItemByID(workspace_button("Hold selected point")); frame(state,1920,1280);
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::hold_curve_point
        && state.commands[0].hold_point_index==1 && !state.commands[0].hold_headroom
        && near(state.commands[0].headroom_margin_mv,37.5)
        && state.commands[0].expected_curve_freq_div==live->data.curve_freq_div,
        "Hold selects the visible point and exact headroom settings through the command queue");
    state.commands.clear(); check(state.curve_draft.stage_mhz(0,15),"hold fixture creates a real local curve edit");
    ImGui::ActivateItemByID(workspace_button("Hold selected point")); frame(state,1920,1280);
    ImGui::ActivateItemByID(header_button(max_it)); frame(state,1920,1280);
    check(state.commands.empty(),"pending local curve edits block Hold and Max it rather than applying unseen drafts");
    state.curve_window_open=false; live->data.tuning.owned=true; live->data.tuning.recovery=true;
    live->data.connected=false; state.controls_unlocked=false;
    const auto gate=frame(state,1920,1280);
    // 1.9.1's gate row has no hold button or line: Classic keeps the release in
    // the Clocks menu beside Ctrl+H and Reset, and states the hold under the read-out.
    const bool classic=presentation==Presentation::classic;
    if (classic) check(!contains(gate,"Release hold (Ctrl+H)") && !contains(gate,"Owned V/F hold") && contains(gate,"V/F HOLD RECOVERY REQUIRED"),
        "the Classic gate row and V/F editor carry no hold button or hold line; the read-out's hold line does");
    ImGuiWindow* clocks=nullptr;
    if (classic) if (auto* workspace=ImGui::FindWindowByName("Druta workspace")) {
        ImGui::ActivateItemByID(ImHashStr("Clocks",0,workspace->GetID("##MenuBar"))); frame(state,1920,1280);
        const auto& stack=ImGui::GetCurrentContext()->OpenPopupStack;
        if (!stack.empty()) clocks=stack.back().Window;
    }
    const auto release=[&]{
        ImGui::ActivateItemByID(classic ? (clocks ? clocks->GetID("Release hold (Ctrl+H)") : 0) : header_button("Release hold (Ctrl+H)"));
        frame(state,1920,1280);
    };
    release();
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::release_hold,
        "owned hold release remains reachable when disconnected, locked and an unapplied draft exists");
    state.commands.clear(); live->busy=true;
    release();
    check(state.commands.empty(),"busy owned hold cannot queue overlapping releases");
    if (classic) { ImGui::ClosePopupsExceptModals(); frame(state,1920,1280); }
}

void hold_shortcut() {
    ViewState state; state.preview=false; state.presentation=Presentation::focused;
    auto live=snapshot(); state.live=live; live->data.hold_available=true;
    state.curve_window_open=true; state.curve_window_focus_requested=true; state.controls_unlocked=true;
    state.hold_headroom=false; state.headroom_margin_mv=37.5;
    frame(state,1920,1280); state.selected_curve_point=1;
    auto press = [&] { ImGui::FocusWindow(ImGui::FindWindowByName("V/F curve editor###druta_curve_editor"));
        press_key(state,ImGuiKey_H,false,true,1920,1280); };
    press();
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::hold_curve_point
        && state.commands[0].hold_point_index==1 && state.commands[0].expected_curve.size()==live->data.curve.size()
        && state.commands[0].expected_curve_freq_div==live->data.curve_freq_div && !state.commands[0].hold_headroom
        && near(state.commands[0].headroom_margin_mv,37.5), "Ctrl+H queues the same selected-point hold and curve guard as the button");
    press(); check(state.commands.size()==1,"a queued hold prevents duplicate shortcut commands");
    state.commands.clear(); live->data.tuning.owned=true; live->data.tuning.recovery=true;
    live->data.connected=false; state.controls_unlocked=false;
    press(); check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::release_hold,
        "Ctrl+H releases owned recovery even while disconnected and locked");
    press(); check(state.commands.size()==1,"a queued release is not duplicated");
    state.commands.clear(); live->busy=true; press(); check(state.commands.empty(),"busy blocks owned hold release");
    live->busy=false; live->data.tuning={}; live->data.connected=true; state.controls_unlocked=true;
    live->curve_stale=true; press(); check(state.commands.empty(),"stale curve blocks shortcut hold");
    live->curve_stale=false; ++live->curve_revision; live->busy=true; press(); check(state.commands.empty(),"busy blocks shortcut hold");
    live->busy=false; state.controls_unlocked=false; press(); check(state.commands.empty(),"locked controls block shortcut hold");
    state.controls_unlocked=true; live->data.connected=false; press(); check(state.commands.empty(),"disconnected adapter blocks shortcut hold");
    live->data.connected=true; ++live->curve_revision; frame(state,1920,1280); state.selected_curve_point=999; press(); check(state.commands.empty(),"invalid selected point blocks shortcut hold");
    state.selected_curve_point=1; live->data.telemetry.present|=DC_T_VF_LOCKED; live->data.telemetry.vf_locked_count=1;
    press(); check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::hold_curve_point,
        "unowned lock telemetry never causes a release; fresh backend preflight decides whether hold is safe");
    state.commands.clear(); live->data.telemetry.vf_locked_count=0;
    ImGui::ActivateItemByID(workspace_button("MHz")); frame(state,1920,1280);
    press(); check(state.commands.empty(),"active text entry blocks shortcut hold");
    ImGui::ClearActiveID(); state.curve_entry_dirty=true;
    press(); check(state.commands.empty(),"uncommitted numeric entry blocks shortcut hold");
    state.curve_entry_dirty=false; check(state.curve_draft.stage_mhz(0,15),"shortcut fixture stages a curve edit");
    press(); check(state.commands.empty(),"dirty curve blocks shortcut hold");
    check(state.curve_draft.revert(),"fresh curve observation restores clean shortcut fixture"); state.curve_window_open=false;
    for (const auto page : {Page::advanced, Page::monitor}) {
        state.page=page; frame(state,1920,1280);
        ImGui::FocusWindow(ImGui::FindWindowByName("Druta workspace"));
        press_key(state,ImGuiKey_H,false,true,1920,1280);
        check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::hold_curve_point
            && state.commands[0].hold_point_index==1,"Ctrl+H holds retained point on another page with editor closed");
        state.commands.clear();
    }
    state.curve_window_open=true; state.curve_window_focus_requested=true; frame(state,1920,1280);
    auto* editor=ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    check(editor != nullptr,"editor exists for collapsed shortcut test");
    if (editor) { editor->Collapsed=true; ImGui::FocusWindow(editor); }
    press_key(state,ImGuiKey_H,false,true,1920,1280);
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::hold_curve_point,
        "Ctrl+H holds retained point with editor collapsed");
    if (editor) editor->Collapsed=false;
}

// The editor's Margin (mV) input feeds the hold write (Hold selected point,
// Ctrl+H, Max it): 1.9.1's clamp_margin band applies when an entry is made, in
// the Classic and the Focused editor alike.
void editor_headroom_margin_entry(Presentation presentation) {
    ViewState state; state.preview=false; state.presentation=presentation;
    auto live=snapshot(); state.live=live; live->data.hold_available=true;
    state.controls_unlocked=true; state.curve_window_open=true; state.hold_headroom=true; state.headroom_margin_mv=25;
    frame(state,1920,1280); state.selected_curve_point=1;
    // workspace_button opens Classic's "More V/F tools", where the input sits beside Hold.
    const auto margin=[&]() -> ImGuiID {
        workspace_button("Hold selected point");
        auto* editor=ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
        return editor ? editor->GetID("Margin (mV)") : 0;
    };
    auto& io=ImGui::GetIO();
    const auto type=[&](const char* text) {
        ImGui::ActivateItemByID(margin()); frame(state,1920,1280,false);
        io.AddInputCharactersUTF8(text); frame(state,1920,1280,false);
    };
    const auto enter=[&] {
        io.AddKeyEvent(ImGuiKey_Enter,true); frame(state,1920,1280,false);
        io.AddKeyEvent(ImGuiKey_Enter,false); frame(state,1920,1280,false);
    };
    type("500");
    check(near(state.headroom_margin_mv,25),"typing a margin stays free until the entry is made");
    enter(); check(near(state.headroom_margin_mv,100),"an editor margin above 1.9.1's band is clamped to MAX_MARGIN_MV on entry");
    type("3"); enter(); check(near(state.headroom_margin_mv,6.25),"an editor margin below 1.9.1's band is clamped to MIN_MARGIN_MV on entry");
    type("37.5"); enter(); check(near(state.headroom_margin_mv,37.5),"an editor margin inside the band is kept");
    type("250"); enter();
    check(state.commands.empty(),"entering a margin writes nothing by itself");
    ImGui::ActivateItemByID(workspace_button("Hold selected point")); frame(state,1920,1280);
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::hold_curve_point
        && state.commands[0].hold_headroom && near(state.commands[0].headroom_margin_mv,100),
        "Hold selected point carries the clamped editor margin to the hold write");
}

// The editor's Hold headroom is the setting the Clocks menu shows: in Classic,
// turning it off while a hold's raise is in force restores the raised limits,
// as 1.9.1's headroom_changed does. Focused keeps its own behaviour.
void editor_hold_headroom_untick(Presentation presentation) {
    ViewState state; state.preview=false; state.presentation=presentation;
    auto live=snapshot(); state.live=live; live->data.hold_available=true;
    live->data.tuning.owned=live->data.tuning.confirmed=live->data.tuning.headroom=true;
    state.controls_unlocked=true; state.curve_window_open=true; state.hold_headroom=true;
    frame(state,1920,1280); state.selected_curve_point=1;
    // workspace_button opens Classic's "More V/F tools", where the checkbox sits beside Hold.
    workspace_button("Hold selected point");
    auto* editor=ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    if (editor) { ImGui::ActivateItemByID(editor->GetID("Hold headroom")); frame(state,1920,1280,false); frame(state,1920,1280,false); }
    const auto restores=std::count_if(state.commands.begin(),state.commands.end(),
        [](const Command& c){ return c.operation==Command::Operation::restore_raised_limits; });
    check(editor && !state.hold_headroom,"the editor's Hold headroom turns the headroom setting off");
    if (presentation==Presentation::classic)
        check(restores==1 && state.commands.size()==1,"Classic: turning Hold headroom off in the editor restores the raised limits");
    else check(state.commands.empty(),"Focused: turning Hold headroom off in the editor queues nothing, as before");
}

static dc_status DC_CALL hard_ui_plan(const dc_vf_point* p, size_t count, const dc_vfp_num*, const dc_vfp_num*, const dc_vfp_num*, dc_vfp_hard* out) {
    if (count != 2) return DC_E_ARGUMENT;
    out->change_count=1; out->changes[0].pos=1; out->changes[0].idx=p[1].idx;
    out->changes[0].delta_khz.kind=DC_VFP_INT; out->changes[0].delta_khz.i=p[1].delta_khz+15000;
    return DC_OK;
}
static dc_status DC_CALL hard_ui_predict(const dc_vf_point*, size_t count, const dc_vfp_num* values, const dc_vfp_num*, int64_t* predicted, uint32_t* order, size_t capacity) {
    if (count != capacity) return DC_E_ARGUMENT;
    for (size_t i=0;i<count;++i) { predicted[i]=values[i].i; order[i]=static_cast<uint32_t>(i); }
    return DC_OK;
}
void curve_profile_menu() {
    ViewState state; state.preview=false; state.presentation=Presentation::focused;
    auto live=snapshot(); state.live=live; state.curve_window_open=true; state.curve_window_focus_requested=true;
    frame(state,1920,1280);
    auto open_menu=[&]() {
        ImGui::ActivateItemByID(workspace_button("Profiles...")); frame(state,1920,1280);
        auto& stack=ImGui::GetCurrentContext()->OpenPopupStack;
        return stack.empty()?static_cast<ImGuiWindow*>(nullptr):stack.back().Window;
    };
    auto choose=[&](const char* name) {
        auto* popup=open_menu(); check(popup!=nullptr,"curve profiles menu opens");
        if (popup) { ImGui::ActivateItemByID(popup->GetID(name)); frame(state,1920,1280); }
    };
    auto close_menu=[&]() {
        auto& stack=ImGui::GetCurrentContext()->OpenPopupStack;
        if (!stack.empty() && stack.back().Window) ImGui::FocusWindow(stack.back().Window);
        press_key(state,ImGuiKey_Escape,false,false,1920,1280);
        // Keep later fixtures isolated even if a disabled popup didn't consume Escape.
        if (!stack.empty()) ImGui::ClosePopupToLevel(0,true);
        frame(state,1920,1280);
    };
    const auto configured=live->data.curve;
    choose("Save configured curve...");
    check(state.save_curve_profile_requested && !state.load_curve_profile_requested && state.commands.empty(),
        "Save configured opens a dialog intent without a hardware command");
    state.save_curve_profile_requested=false;
    choose("Load curve to draft...");
    check(state.load_curve_profile_requested && !state.save_curve_profile_requested && state.commands.empty(),
        "Load opens a staging dialog intent without a hardware command");
    state.load_curve_profile_requested=false;
    check(state.curve_draft.stage_mhz(0,15),"profile menu fixture stages an unsaved curve edit");
    choose("Load curve to draft...");
    check(!state.load_curve_profile_requested && state.commands.empty(),"dirty curve blocks profile load");
    close_menu(); choose("Save configured curve...");
    check(state.save_curve_profile_requested && state.commands.empty() && live->data.curve[0].delta_khz==configured[0].delta_khz
        && state.curve_draft.dirty(),"Save remains available with a draft and leaves configured snapshot and draft unchanged");
    state.save_curve_profile_requested=false;
    check(state.curve_draft.revert(),"profile menu fixture reverts staged curve"); state.curve_entry_dirty=true;
    choose("Load curve to draft...");
    check(!state.load_curve_profile_requested && state.commands.empty(),"uncommitted numeric edit blocks profile load");
    close_menu(); state.curve_entry_dirty=false;
    auto* busy_popup=open_menu(); check(busy_popup!=nullptr,"profile menu opens before busy transition");
    live->busy=true;
    if (busy_popup) {
        ImGui::ActivateItemByID(busy_popup->GetID("Save configured curve...")); frame(state,1920,1280);
        ImGui::ActivateItemByID(busy_popup->GetID("Load curve to draft...")); frame(state,1920,1280);
    }
    check(!state.save_curve_profile_requested && !state.load_curve_profile_requested && state.commands.empty(),
        "busy session blocks profile actions in an already-open menu");
    close_menu(); live->busy=false;
    auto* stale_popup=open_menu(); check(stale_popup!=nullptr,"profile menu opens before stale transition");
    live->curve_stale=true;
    if (stale_popup) {
        ImGui::ActivateItemByID(stale_popup->GetID("Save configured curve...")); frame(state,1920,1280);
        ImGui::ActivateItemByID(stale_popup->GetID("Load curve to draft...")); frame(state,1920,1280);
    }
    check(!state.save_curve_profile_requested && !state.load_curve_profile_requested && state.commands.empty(),
        "stale curve blocks profile actions in an already-open menu");
    close_menu();
}

void hard_de_flatten_ui() {
    ViewState state;
    state.presentation = Presentation::focused; state.preview=false; auto live=snapshot(); state.live=live;
    live->data.curve_model_max_rise_khz=45000; live->data.curve_writable=true;
    state.planner_api.hard_deflatten=hard_ui_plan; state.planner_api.predict=hard_ui_predict;
    state.curve_window_open=true; state.controls_unlocked=true; state.hard_floor_mv=900; state.hard_target_mhz=1905;
    frame(state,1920,1280);
    auto* editor=ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    check(editor!=nullptr,"hard de-flatten fixture has a curve editor");
    if (editor) {
        editor->StateStorage.SetInt(editor->GetID("Curve shape tools"),1);
        editor->StateStorage.SetInt(editor->GetID("Hard de-flatten (needs an external voltage mod)"),1);
    }
    frame(state,1920,1280);
    ImGui::ActivateItemByID(workspace_button("Stage hard de-flatten")); frame(state,1920,1280);
    check(!state.curve_draft.dirty() && state.commands.empty(),"hard de-flatten requires explicit hardware acknowledgement");
    state.hard_mod_ack=true;
    ImGui::ActivateItemByID(workspace_button("Stage hard de-flatten"));
    auto text=frame(state,1920,1280);
    check(state.curve_draft.dirty() && state.commands.empty() && !state.hard_mod_ack,
        "acknowledged hard de-flatten stages locally and consumes acknowledgement without writing");
    check(contains(text,"Hard de-flatten remains in this draft") && contains(text,"changed point"),
        "staged hard plan shows persistent consequences before Apply");
    ImGui::ActivateItemByID(workspace_button(curve_label(state, "Apply curve"))); frame(state,1920,1280);
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::apply_curve,
        "only explicit Apply queues the staged hard curve request");
}

// Classic plan buttons: each stages its own planner into the draft the green
// Apply writes. Fakes record which planner ran; values are arbitrary.
int classic_calls[4]{}; // ramp, rephase, deflatten, hard
double classic_ramp_lo=0, classic_ramp_cap=0;
dc_status DC_CALL classic_ramp(const dc_vf_point* p, size_t count, const dc_vfp_num* lo, const dc_vfp_num* cap,
    const dc_vfp_num*, const dc_vfp_num*, dc_vfp_ramp* out) {
    if (count!=2) return DC_E_ARGUMENT;
    ++classic_calls[0]; classic_ramp_lo=lo->f; classic_ramp_cap=cap->f;
    out->change_count=1; out->changes[0].pos=0; out->changes[0].idx=p[0].idx;
    out->changes[0].delta_khz.kind=DC_VFP_INT; out->changes[0].delta_khz.i=p[0].delta_khz+15000;
    out->has_band=1; out->rungs=2; out->delivered=2; out->lo_mv=lo->f; out->cap_mv=cap->f; out->top_mhz=1890; out->floor_after_mhz=1415;
    return DC_OK;
}
dc_status DC_CALL classic_rephase(const dc_vfp_num*, size_t count, const dc_vfp_num*, dc_vfp_rephase* out) {
    if (count!=2) return DC_E_ARGUMENT;
    ++classic_calls[1]; out->change_count=1; out->changes[0].pos=1; out->changes[0].delta_khz=30000; return DC_OK;
}
dc_status DC_CALL classic_deflatten(const dc_vf_point* p, size_t count, const dc_vfp_num*, const dc_vfp_num*,
    const dc_vfp_num*, const dc_vfp_num*, dc_vfp_deflatten* out) {
    if (count!=2) return DC_E_ARGUMENT;
    ++classic_calls[2]; out->change_count=1; out->changes[0].pos=1; out->changes[0].idx=p[1].idx;
    out->changes[0].delta_khz.kind=DC_VFP_INT; out->changes[0].delta_khz.i=p[1].delta_khz+45000; return DC_OK;
}
dc_status DC_CALL classic_hard(const dc_vf_point* p, size_t count, const dc_vfp_num*, const dc_vfp_num*,
    const dc_vfp_num*, dc_vfp_hard* out) {
    ++classic_calls[3]; const auto status=hard_ui_plan(p,count,nullptr,nullptr,nullptr,out);
    out->has_band=1; out->n_flat=1; out->floor_mv=900; out->target_mhz=1905; out->park_idx=1; out->park_mv=900; out->park_mhz=1905;
    out->parks_at_floor=1; return status;
}
void classic_plan_buttons() {
    ViewState state; state.preview=false; state.page=Page::curve; state.presentation=Presentation::classic;
    auto live=snapshot(); live->data.curve_model_max_rise_khz=45000; live->data.curve_writable=true; state.live=live;
    state.planner_api={classic_deflatten,classic_ramp,classic_rephase,hard_ui_predict,classic_hard};
    state.controls_unlocked=true;
    frame(state,1600,1200,false);
    // The first read seeds the released presets bounded to this curve's voltages (750-900 mV)
    // and the hard target from its peak; the fixture then types its own bounds.
    check(near(state.curve_cap_mv,1093.75) && near(state.curve_floor_mv,900) && near(state.hard_floor_mv,800)
        && near(state.hard_target_mhz,1890),"Classic seeds 1.9.1's presets and bounds the floors to the adapter's curve");
    state.curve_cap_mv=1000; state.curve_floor_mv=750; state.hard_floor_mv=900; state.hard_target_mhz=1905;
    auto* editor=ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    check(editor!=nullptr,"Classic plan-button fixture has a curve editor");
    if (!editor) return;
    editor->StateStorage.SetInt(editor->GetID("More V/F tools"),0);
    editor->StateStorage.SetInt(editor->GetID("Hard de-flatten  (needs an external voltage mod - read this before using)"),0);
    frame(state,1600,1200,false);
    auto press=[&](const char* name) { ImGui::ActivateItemByID(workspace_button(name)); frame(state,1600,1200,false); return frame(state,1600,1200); };
    auto revert=[&]() { press("Revert edits"); };
    auto text=press("Apply V/F curve to GPU");
    // 1.9.1 vf_apply logs "no edits to apply" untagged; the status line under the
    // statistics mirrors it and the editor gains no line of its own.
    check(state.commands.empty() && !state.classic_log_lines.empty() && state.classic_log_lines.back().text=="no edits to apply"
        && !state.classic_log_lines.back().ok && contains(text,"no edits to apply") && !contains(text,"No edits to apply"),
        "the green Apply is live at idle and a click with nothing staged writes nothing");

    text=press("Re-phase to 15 MHz increments");
    check(classic_calls[1]==1 && classic_calls[0]==0 && state.curve_draft.draft()[1].delta_khz==30000 && state.commands.empty(),
        "Re-phase stages the re-phase planner only");
    check(contains(text,"ONE CLICK WRITES THIS") && !contains(text,"RAMP -") && state.curve_plan_note.empty(),
        "a re-phase adds no plan note, as in 1.9.1");
    revert();
    text=press("De-flatten");
    check(classic_calls[0]==1 && near(classic_ramp_lo,750) && near(classic_ramp_cap,1000)
        && state.curve_draft.draft()[0].delta_khz==15000 && state.commands.empty(),
        "De-flatten stages the ramp planner between the ramp floor and the cap");
    check(contains(text,"RAMP - 2 rungs") && contains(text,"The floor keeps its 1415 MHz at 750.00 mV") && !contains(text,"STILL STAGED UNDERNEATH"),
        "a staged ramp prints the released plan note inside the banner");
    revert();
    check(state.curve_plan_note.empty() && !state.curve_draft.dirty(),"Revert edits drops the plan and its note");
    text=press("Limited de-flatten \xE2\x89\xA4 cap");
    check(classic_calls[2]==1 && state.curve_draft.draft()[1].delta_khz==45000 && !contains(text,"RAMP -") && state.commands.empty(),
        "Limited de-flatten under More V/F tools stages the narrow de-flatten planner");
    revert();

    editor->StateStorage.SetInt(editor->GetID("Hard de-flatten  (needs an external voltage mod - read this before using)"),1);
    frame(state,1600,1200,false);
    text=press("Hard de-flatten");
    check(classic_calls[3]==0 && !state.curve_draft.dirty(),"Classic hard de-flatten needs the acknowledgement");
    const auto ack=text.find("I have a functional hardware voltage mod on this card"), warn=text.find("Without it the card");
    check(ack!=std::string::npos && warn!=std::string::npos && ack<warn && contains(text,"by the 45 MHz repair, and crashes the driver")
        && text.find("\nfloor (mV)",warn)!=std::string::npos && text.find("target (MHz)",warn)!=std::string::npos,
        "Classic hard block: acknowledgement first, then the red warning naming the model's repair, then floor/target");
    state.hard_mod_ack=true;
    text=press("Hard de-flatten");
    check(classic_calls[3]==1 && state.hard_plan_staged && state.curve_draft.draft()[1].delta_khz==15000 && state.commands.empty(),
        "Classic's own hard header stages the hard de-flatten locally");
    check(contains(text,"HARD DE-FLATTEN - WITHOUT THE VOLTAGE MOD THIS WILL CRASH")
        && contains(text,"HARD DE-FLATTEN - 1 point(s) at or above 900.00 mV flattened onto ONE frequency, 1905 MHz"),
        "a staged hard de-flatten takes the red headline and the released note");
    text=press("De-flatten");
    check(state.hard_plan_staged && contains(text,"HARD DE-FLATTEN - WITHOUT THE VOLTAGE MOD THIS WILL CRASH")
        && contains(text,"A HARD DE-FLATTEN IS STILL STAGED UNDERNEATH THIS") && contains(text,"RAMP - 2 rungs"),
        "a ramp over a hard plan keeps the red band and names the hard plan underneath");
    text=press("Apply V/F curve to GPU");
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::apply_curve
        && state.commands[0].curve_deltas.size()==2 && state.commands[0].curve_deltas[0].delta_khz==15000
        && state.commands[0].curve_deltas[1].delta_khz==15000,
        "Apply writes exactly the ramp and the hard plan staged underneath it");
    // 1.9.1 vf_apply: the planner's note goes into the receipt; a hard plan tags it [!!].
    const auto& receipt=state.classic_log_lines.back();
    check(receipt.text.rfind("writing 2 edited point(s): top ",0)==0 && !state.curve_plan_note.empty()
        && contains(receipt.text,(" mV. "+state.curve_plan_note).c_str()) && receipt.ok==false,
        "the receipt carries the staged plan's note and is [!!] over a hard plan");
    state.commands.clear();
    state.curve_window_open=false; frame(state,1600,1200,false);
}

void timings_navigation_and_commands(Presentation presentation) {
    ViewState state; state.preview=false; state.presentation=presentation;
    auto live=snapshot(); state.live=live;
    frame(state,1024,1000);
    auto* root=ImGui::FindWindowByName("Druta workspace");
    ImGuiID nav_id=0;
    if(presentation==Presentation::classic) {
        // New tabs are registered on the first frame and become interactive
        // after the bar has laid them out. Activate the actual padded tab ID.
        frame(state,1024,1000);
        auto* tabs=ImGui::GetCurrentContext()->TabBars.GetByKey(root->GetID("legacy-tabs"));
        check(tabs && tabs->Tabs.Size==4,"Classic navigation is an actual four-page tab bar");
        nav_id=ImHashStr("  Timings  (needs nvtune)  ",0,root->GetID("legacy-tabs"));
        check(tabs && ImGui::TabBarFindTabByID(tabs,nav_id),"Timings route targets the real registered tab");
    }
    else {
        auto* nav=root;
        for(auto* candidate:ImGui::GetCurrentContext()->Windows)
            if(candidate->ParentWindow==root && candidate->ChildId==root->GetID("navigation")) nav=candidate;
        nav_id=nav->GetID("Timings");
    }
    const bool classic=presentation==Presentation::classic;
    ImGui::ActivateItemByID(nav_id); const auto text=frame(state,1024,1000);
    // Classic shows the released 1.9.1 setup screen while no helper is known.
    check(state.page==Page::timings && contains(text,classic?"NVTUNE NOT FOUND":"Memory timings") && state.commands.empty(),
        "both presentations expose a real Timings tab without dispatching a helper or tuning command");
    // Continue independently so a navigation regression does not hide the command tests.
    state.page=Page::timings; frame(state,1024,1000);
    auto body=[&]() -> ImGuiWindow* {
        for(auto* candidate:ImGui::GetCurrentContext()->Windows)
            if(candidate->ParentWindow==root && candidate->ChildId==root->GetID("workspace")) return candidate;
        return nullptr;
    };
    auto activate=[&](const char* label) {
        auto* window=body(); check(window!=nullptr,"timings body exists for actual widget activation");
        if(window) ImGui::ActivateItemByID(window->GetID(label)); frame(state,1024,1000);
    };
    activate(classic?"Locate nvtune.exe...":"Locate nvtune...");
    check(state.timing_helper_requested && state.commands.empty(),"Locate requests the native helper dialog without executing a helper in render");
    state.timing_helper_requested=false;
    auto timing=std::make_shared<TimingViewData>(); timing->helper_path=L"C:\\fixture-only\\nvtune.exe";
    live->data.timings=timing; frame(state,1024,1000);
    activate(classic?"Re-read timings":"Read timings");
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::timings_read,
        "Read timings is an explicit read command and requires no tuning unlock");
    state.commands.clear(); timing->snapshot.ok=timing->snapshot.top_band=true;
    timing->snapshot.broadcast={{"REG", "DOCUMENTED", 16, 5}};
    timing->snapshot.fields={{"tRCD", "REG", "Documented field",0,3,15}};
    frame(state,1024,1000);
    if(presentation==Presentation::classic) {
        // Classic edits inline (released 1.9.1 'new value' cell) into the staged
        // fields; writes still go through the explicit batch Preview / Apply.
        check(edit_timing_cell(state.timings_view,*timing,"tRCD",7) && state.commands.empty(),
            "a Classic inline timing edit only stages the field");
        state.controls_unlocked=true; activate("Preview all staged");
        check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::timings_preview_batch
            && state.commands[0].timing_editor && state.commands[0].timing_editor->fields.size()==1
            && state.commands[0].timing_editor->fields[0].field=="tRCD" && state.commands[0].timing_editor->fields[0].value==7,
            "Classic Preview carries the staged field and value");
        // Released 1.9.1 tw_button relabels Apply by its count: one ID for every label.
        state.commands.clear(); activate("###tw_apply");
        check(state.commands.empty(),"Classic timing Apply requires a canonical backend preview");
        TimingBatchPlan batch; batch.ok=true; TimingPlan staged; staged.ok=true; staged.field="tRCD"; staged.value=7; staged.prior=5;
        batch.fields.push_back(staged); timing->batch_preview=batch; activate("###tw_apply");
        check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::timings_apply_batch,
            "Classic Apply dispatches the backend-previewed staged fields");
        state.commands.clear(); state.timings_view.staged.clear(); activate("###tw_apply");
        check(state.commands.empty(),"cleared staged fields cannot replay a previous preview");
        timing->snapshot.ok=false; timing->undo_available=true; activate("Undo timing write");
        check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::timings_undo,
            "timing Undo remains visible after capture failure rather than losing a retained recovery action");
        // Native single-field Preview / Apply stays reachable under its collapsed header.
        state.commands.clear(); timing->snapshot.ok=true; timing->batch_preview.reset(); frame(state,1024,1000);
        if(auto* window=body()) window->StateStorage.SetInt(window->GetID("Single-field preview and apply"),1);
        // Classic's real selector: the header's 'Field' combo lists every captured
        // field, a structural one too; choosing tRCD selects it with its captured cycles.
        timing->snapshot.fields.push_back({"TRAIN", "REG", "Training [structural]",4,7,15,true});
        frame(state,1024,1000); activate("Field");
        auto* combo=ImGui::FindWindowByName("##Combo_00");
        check(combo && combo->Active && state.timings_view.field.empty(),"the Classic Field combo opens without selecting a field");
        const auto listed=frame(state,1024,1000); const auto header_at=listed.find("Single-field preview and apply");
        check(header_at!=std::string::npos && listed.find("tRCD",header_at)!=std::string::npos && listed.find("TRAIN",header_at)!=std::string::npos,
            "the open Field combo lists the captured fields, the structural one included");
        if(combo) ImGui::ActivateItemByID(ImHashStr("tRCD",0,combo->ID)); frame(state,1024,1000);
        check(state.timings_view.field=="tRCD" && state.timings_view.value==5 && state.timings_view.staged.empty() && state.commands.empty(),
            "choosing tRCD in the Field combo selects tRCD (not the structural field) and its captured value, writing nothing");
        state.timings_view.value=9; frame(state,1024,1000);
        activate("Preview timing");
        check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::timings_preview
            && state.commands[0].timing_field=="tRCD" && state.commands[0].timing_value==9,
            "Classic single-field Preview sends the selected field and value");
        state.commands.clear(); activate("Apply timing");
        check(state.commands.empty(),"Classic single-field Apply needs the backend preview of the same request");
        TimingPlan single; single.ok=true; single.field="tRCD"; single.value=9; single.prior=5; timing->preview=single;
        activate("Apply timing");
        check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::timings_apply
            && state.commands[0].timing_field=="tRCD" && state.commands[0].timing_value==9,
            "Classic single-field Apply sends the previewed field and value");
        state.commands.clear();
        return;
    }
    auto* window=body(); const auto table_id=window->GetID("timing_fields");
    ImGui::ActivateItemByID(ImHashStr("tRCD",0,table_id)); frame(state,1024,1000);
    check(state.timings_view.field=="tRCD" && state.timings_view.value==5 && state.commands.empty(),
        "clicking a captured timing field stages only that field and its captured cycles");
    state.controls_unlocked=true; state.timings_view.value=7;
    activate("Preview timing");
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::timings_preview
        && state.commands[0].timing_field=="tRCD" && state.commands[0].timing_value==7,
        "Preview timing carries the selected field and staged value");
    state.commands.clear(); activate("Apply timing");
    check(state.commands.empty(),"timing Apply requires a canonical backend preview");
    TimingPlan plan; plan.ok=true; plan.field="tRCD"; plan.value=7; plan.prior=5; timing->preview=plan;
    activate("Apply timing");
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::timings_apply
        && state.commands[0].timing_field=="tRCD" && state.commands[0].timing_value==7,
        "Apply timing dispatches only the matching backend-previewed field and value");
    state.commands.clear(); state.timings_view.field.clear(); activate("Apply timing");
    check(state.commands.empty(),"cleared timing selection cannot replay a previous preview");
    timing->snapshot.ok=false; timing->undo_available=true; activate("Undo timing write");
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::timings_undo,
        "timing Undo remains visible after capture failure rather than losing a retained recovery action");
}

void draft_behavior() {
    ViewState state;
    state.presentation = Presentation::focused;
    state.preview = false;
    auto live = snapshot();
    state.live = live;
    auto& memory = live->data.controls[static_cast<size_t>(ControlId::memory)];
    auto& draft = state.drafts[static_cast<size_t>(ControlId::memory)];
    check(state.controls_unlocked, "tuning controls start unlocked");
    frame(state);
    check(state.commands.empty(), "unlocked startup does not dispatch hardware writes");
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
    ImGui::ActivateItemByID(header_button("Unlock controls"));
    frame(state);
    check(!state.controls_unlocked, "user can explicitly lock controls");
    draft.value = 10;
    draft.dirty = true;
    live->generation = 2;
    memory.current = -2;
    frame(state);
    check(near(draft.value, -2) && !draft.dirty, "new session discards old adapter target");
    check(!state.controls_unlocked, "new session preserves an explicit tuning lock");
    memory.availability = Availability::available;
    state.presentation = Presentation::focused;
    draft.value = 10; draft.dirty = true;
    frame(state);
    ImGui::ActivateItemByID(control_button(ControlId::memory, "Apply"));
    frame(state);
    check(state.commands.empty(), "explicit lock continues to block Apply after a generation change");
    check(state.commands.empty() && !state.select_slot && !state.refresh_requested && !state.reconnect_requested
        && !state.save_profile_requested && !state.load_profile_requested,
        "rendering and readback never create requests implicitly");
}

void explicit_profile_intents_and_stock() {
    ViewState state;
    state.presentation = Presentation::focused;
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
    state.controls_unlocked = false;
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
    state.presentation = Presentation::focused;
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
        for (auto page : {Page::daily, Page::advanced, Page::monitor, Page::curve, Page::timings}) {
            state.page = page;
            frame(state, 1024, 768);
            frame(state, 1366, 768);
            const auto text = frame(state, 1920, 1080);
            check(!contains(text, "3070 Ti") && !contains(text, "Sample data"),
                "live pages contain no fixture adapter or telemetry disclaimer");
            check(contains(text, "Alternate adapter"), "live adapter identity remains visible");
            check(contains(text, "Refused request; captured Undo retained"),
                "failed action remains visible in both presentations and all pages");
            // Classic keeps them in 1.9.1's Profiles menu (app_actions test opens it).
            if (presentation == Presentation::classic) {
                check(contains(text, "Profiles") && !contains(text, "Save profile..."), "Classic has no header profile row");
                auto* workspace = ImGui::FindWindowByName("Druta workspace");
                if (workspace) ImGui::ActivateItemByID(ImHashStr("Profiles", 0, workspace->GetID("##MenuBar")));
                frame(state, 1920, 1080);
                const auto menu = frame(state, 1920, 1080);
                check(contains(menu, "Save profile...") && contains(menu, "Load profile...") && contains(menu, "Profile catalog..."),
                    "manual profile actions remain reachable from every page and presentation");
                ImGui::ClosePopupsExceptModals();
                frame(state, 1920, 1080);
            } else check(contains(text, "Save profile...") && contains(text, "Load profile..."),
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
    live->fan_duty_stale_mask = live->fan_rpm_stale_mask = 1;
    const auto fan_text = frame(state, 1920, 1400);
    // Classic (the loop above ends there): the State panel's summary line, as Python's, marked retained.
    check(contains(fan_text, "fan0: 35% 1200rpm  (stale)"), "last-good fan readings are explicitly stale independently of count presence bit");
    state.page = Page::daily;
    const auto classic_fan = frame(state, 1920, 1400);
    check(contains(classic_fan, "35% (stale)"), "Classic live fan reading marks retained duty stale independently of the configured request");
    live->fan_readings_stale = false;
    live->fan_duty_stale_mask = live->fan_rpm_stale_mask = 0;
    const auto fresh_classic_fan = frame(state, 1920, 1400);
    check(!contains(fresh_classic_fan, "35% (stale)") && contains(fresh_classic_fan, "35%"), "Classic recovered fan duty clears its stale marker");
    state.page = Page::monitor;
    live->data.telemetry.fans[0].has_duty = 0;
    const auto partial_fan = frame(state, 1920, 1400);
    check(contains(partial_fan, "fan0: --% 1200rpm") && !contains(partial_fan, "1200rpm  (stale)"),
        "fresh partial fan data preserves independent duty/RPM availability");
    live->data.connected = false;
    live->data.curve.clear();
    live->data.telemetry.present = 0;
    live->data.telemetry_valid = false;
    live->data.telemetry_error = "Read temporarily unavailable";
    live->paused = true;
    for (auto page : {Page::daily, Page::advanced, Page::monitor, Page::curve, Page::timings}) {
        state.page = page;
        frame(state, 1024, 768);
    }
    check(state.commands.empty(), "missing/stale/paused displays never dispatch a write");
    state.preview = true;
    for (auto page : {Page::daily, Page::advanced, Page::monitor, Page::curve, Page::timings}) {
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
    classic_power_request_footer();
    classic_power_request_feedback();
    offset_page_routing_and_gates();
    shunt_display_and_raw_power_apply();
    weighted_workspace_risk_tint();
    compact_curve_layout();
    draft_behavior();
    explicit_profile_intents_and_stock();
    curve_workspace(Presentation::focused);
    curve_workspace(Presentation::classic);
    inline_curve_input();
    classic_curve_editor_parity();
    classic_curve_log_and_budget();
    classic_curve_read_rules();
    classic_curve_read_on_open();
    classic_copy_log_keeps_all_entries();
    classic_write_receipts();
    classic_copy_info_logs();
    classic_vf_tooltips();
    classic_text_colour();
    popup_input(Presentation::focused);
    popup_input(Presentation::classic);
    curve_apply_acknowledgement(Presentation::focused);
    curve_apply_acknowledgement(Presentation::classic);
    i2c_panel_wiring(Presentation::focused);
    i2c_panel_wiring(Presentation::classic);
    curve_profile_menu();
    hard_de_flatten_ui();
    classic_plan_buttons();
    hold_shortcut();
    editor_headroom_margin_entry(Presentation::focused);
    editor_headroom_margin_entry(Presentation::classic);
    editor_hold_headroom_untick(Presentation::focused);
    editor_hold_headroom_untick(Presentation::classic);
    explicit_hold_and_global_actions(Presentation::focused);
    explicit_hold_and_global_actions(Presentation::classic);
    timings_navigation_and_commands(Presentation::focused);
    timings_navigation_and_commands(Presentation::classic);
    rendering_and_uncertainty();
    ImPlot::DestroyContext();
    ImGui::DestroyContext();
    std::printf("%d UI view checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
