/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
// Headless UI contract test. Link advanced_view.cpp, risk_policy.cpp and the
// pinned ImGui library. No backend implementation, Session worker, DLL, GPU or
// external profile file is opened. All checks execute in Release builds.
#include "advanced_view.h"
#include "session.h"
#include "imgui.h"
#include "imgui_internal.h"
#include <cmath>
#include <cstdio>
#include <limits>
#include <stdexcept>
#include <string>

namespace {
using namespace druta::ui;
unsigned checks = 0, failures = 0;
constexpr const char* window_name = "Advanced view contract test";
void check(bool condition, const char* message) {
    ++checks;
    if (!condition) { ++failures; std::printf("FAIL: %s\n", message); }
}
bool near(double actual, double expected) { return std::abs(actual - expected) < 1e-9; }

ControlState control(double current, const char* unit) {
    ControlState state;
    state.availability = Availability::available;
    state.writable = state.stock_writable = true;
    state.current = current; state.low = -2000; state.high = 2000;
    state.stock = 0; state.step = 0.001; state.unit = unit;
    state.detail = "Adapter-reported request; physical response is not inferred";
    return state;
}

SessionSnapshot snapshot() {
    SessionSnapshot s;
    s.generation = 1;
    s.data.connected = true;
    s.data.xoc_known = true;
    s.data.rail_mode_known = s.data.rail_writable_known = true;
    s.data.rail_writable = true; s.data.rail_mode_enabled = true;
    s.data.advanced_controls = {
        {{AdvancedKind::clock_domain, 7}, "Video clock control with a long adapter-derived label", control(11.25, "MHz"), "clock-seven-layout"},
        {{AdvancedKind::clock_domain, 21}, "Independent secondary clock control", control(22.5, "MHz"), "clock-twenty-one-layout"},
        {{AdvancedKind::nvvdd, 0}, "NVVDD request offset", control(12.5, "mV"), "nvvdd-pairing"},
        {{AdvancedKind::msvdd, 4}, "MSVDD request at nonzero control", control(-8.125, "mV"), "msvdd-control-four"},
    };
    s.data.power_policies = {
        {9, "Adapter-specific current channel with a long policy label", true, true, control(126.125, "A"), "policy-nine-channel-A"},
        {25, "Separate board-derived power policy", false, false, control(87.5, "W"), "policy-twenty-five-channel-B"},
    };
    s.data.rail_controls = {
        {{0, 1}, control(1000.125, "mV"), 950.0, "rail-zero-field-one"},
        {{1, 3}, control(1100.125, "mV"), 900.0, "rail-one-field-three"},
    };
    s.data.undo_available = true;
    s.data.undo_label = "Undo selected advanced requests";
    return s;
}

struct Harness {
    SessionSnapshot s = snapshot();
    AdvancedViewState state;
    bool unlocked = false;
    std::vector<Command> commands;

    ImGuiWindow* body_window() const {
        auto* root = ImGui::FindWindowByName(window_name);
        if (root) for (auto* window : ImGui::GetCurrentContext()->Windows)
            if (window->ParentWindow == root && window->ChildId == root->GetID("advanced-body")) return window;
        return nullptr;
    }

    std::string frame(bool verbose = false, bool show_controls = true, float width = 1280, bool capture = true) {
        ImGui::GetIO().DisplaySize = {width, 1000};
        ImGui::NewFrame();
        ImGui::SetNextWindowPos({0, 0});
        ImGui::SetNextWindowSize({width, 1000});
        ImGui::Begin(window_name, nullptr, ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoResize);
        if (capture) ImGui::LogToBuffer();
        draw_mode_bar(s, unlocked, commands, false, {}, &state);
        ImGui::BeginChild("advanced-body", {0, 0});
        auto* body = ImGui::GetCurrentWindow();
        // Focused normally collapses large groups. Tests explicitly expand
        // the groups whose row interactions they exercise.
        body->StateStorage.SetInt(body->GetID("Current and power policies"), 1);
        body->StateStorage.SetInt(body->GetID("Rail limits"), 1);
        if (show_controls) draw_advanced_controls(s, unlocked, verbose, state, commands);
        else ImGui::TextUnformatted("Monitor / profile page: shared draft state is retained");
        ImGui::EndChild();
        const std::string text = capture ? ImGui::GetCurrentContext()->LogBuffer.c_str() : "";
        if (capture) ImGui::LogFinish();
        ImGui::End();
        ImGui::Render();
        check(ImGui::GetDrawData()->CmdListsCount > 0, "advanced view renders without a backend");
        return text;
    }

    uint64_t key_with_value(double value) const {
        for (const auto& row : state.drafts) if (near(row.second.value, value)) return row.first;
        throw std::runtime_error("test could not locate a distinct initial request");
    }

    void activate(const char* label, bool verbose = false) {
        const bool body = std::string(label) == "Undo hardware write" || std::string(label) == "Revert advanced drafts";
        auto* window = body ? body_window() : ImGui::FindWindowByName(window_name);
        if (!window) throw std::runtime_error("missing test window");
        ImGui::ActivateItemByID(window->GetID(label));
        frame(verbose);
    }

    void activate_row(const char* table_name, uint64_t row_key, const char* label, bool verbose = false) {
        auto* window = body_window();
        if (!window) throw std::runtime_error("missing test window");
        const auto table_id = window->GetID(table_name);
        const int row_id = static_cast<int>(row_key);
        const auto seed = ImHashData(&row_id, sizeof(row_id), table_id);
        ImGui::ActivateItemByID(ImHashStr(label, 0, seed));
        frame(verbose);
    }
};

void mode_intents_and_risk(bool verbose) {
    Harness h; h.s.data.rail_mode_enabled=false;
    h.frame(verbose);
    h.activate("XOC", verbose);
    h.activate("Rail limits", verbose);
    check(h.commands.empty(), "locked controls cannot turn on XOC or rail mode");
    h.activate("Unlock controls", verbose);
    check(h.unlocked && h.commands.empty(), "unlock changes only local UI state");
    h.activate("XOC", verbose);
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::set_xoc
        && h.commands[0].enabled, "XOC click queues one explicit enable intent");
    check(!h.s.data.xoc_enabled, "mode intent does not optimistically change configured state");
    h.commands.clear();
    h.activate("Rail limits", verbose);
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::set_rail_mode
        && h.commands[0].enabled, "rail-mode click queues its own explicit intent");
    h.commands.clear();
    h.s.data.xoc_enabled = h.s.data.rail_mode_enabled = true; // A verified later snapshot.
    auto risk = session_risk(h.s.data);
    check(risk.band == RiskBand::red && !risk.incomplete, "confirmed XOC plus writable rail mode is RED");
    h.s.data.telemetry_valid = false;
    h.s.data.telemetry_error = "Transient sensor read failure";
    for (auto& row : h.s.data.advanced_controls) row.state.availability = Availability::transient;
    const auto text = h.frame(verbose);
    risk = session_risk(h.s.data);
    check(risk.band == RiskBand::red && text.find("RED") != std::string::npos,
        "transient telemetry does not erase established mode risk or its displayed RED band");
    h.unlocked = false;
    h.activate("XOC", verbose);
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::set_xoc
        && !h.commands[0].enabled, "XOC OFF remains reachable while controls are locked");
    h.commands.clear();
    h.activate("Rail limits", verbose);
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::set_rail_mode
        && !h.commands[0].enabled, "rail mode OFF remains reachable while controls are locked");
    h.commands.clear();
    check(session_risk(h.s.data).band == RiskBand::red, "unconfirmed OFF intent cannot remove the reported armed state");
    h.s.data.rail_writable_known = false;
    risk = session_risk(h.s.data);
    check(risk.incomplete && risk.band != RiskBand::green, "unknown direct-writer capability remains uncertainty rather than GREEN");
    h.s.data.xoc_known = false;
    h.unlocked = true;
    h.activate("XOC", verbose);
    check(h.commands.empty(), "never-established XOC state cannot be toggled speculatively");
}

void weighted_mode_labels_and_lock(bool verbose) {
    Harness h;
    struct Case { bool xoc, rails, i2c; const char* label; unsigned score; };
    const Case cases[] = {
        {false, false, false, "GREEN / 0 pts", 0},
        {true, false, false, "YELLOW / 1 pt", 1},
        {false, true, false, "AMBER / 2 pts", 2},
        {true, true, false, "RED / 3 pts", 3},
        {false, false, true, "RED / 3 pts", 3},
        {true, false, true, "CRIMSON / 4 pts", 4},
        {false, true, true, "CRIMSON / 5 pts", 5},
        {true, true, true, "CRIMSON / 6 pts", 6},
    };
    for (const auto& c : cases) {
        h.s.data.xoc_enabled = c.xoc;
        h.s.data.rail_mode_enabled = c.rails;
        h.s.data.i2c.gate = c.i2c;
        h.s.data.i2c.status.capable = c.i2c;
        h.unlocked = true;
        auto text = h.frame(verbose);
        check(text.find(c.label) != std::string::npos && session_risk(h.s.data).score == c.score,
            "unlocked mode bar displays weighted COLOR / points for each feature combination");
        h.unlocked = false;
        text = h.frame(verbose);
        check(text.find(c.score ? c.label : "LOCKED") != std::string::npos,
            "locking shows neutral LOCKED only at zero and preserves active feature risk labels");
    }
    h.s.data.xoc_enabled = h.s.data.rail_mode_enabled = h.s.data.i2c.gate = false;
    h.s.data.i2c.status = {};
    h.s.data.i2c.status.recovery_pending = true;
    h.s.data.i2c.status.stale = true;
    h.s.data.telemetry_valid = false;
    h.s.data.telemetry_error = "Temporary telemetry read failure";
    auto text = h.frame(verbose);
    check(text.find("RED / 3 pts") != std::string::npos && session_risk(h.s.data).score == 3,
        "locked recovery-pending I2C ownership retains RED despite gate off, stale reads and missing capability");
    h.s.data.i2c.status.recovery_pending = false;
    h.s.data.i2c.status.owned = true;
    text = h.frame(verbose);
    check(text.find("RED / 3 pts") != std::string::npos,
        "owned I2C request keeps its risk visible while controls are locked");
    h.s.data.i2c.status = {};
    h.s.data.xoc_known = false;
    h.unlocked = true;
    text = h.frame(verbose);
    check(text.find("UNKNOWN") != std::string::npos && text.find("GREEN / 0 pts") == std::string::npos,
        "incomplete zero score displays UNKNOWN instead of implying established GREEN");
    check(h.commands.empty(), "rendering weighted mode and lock states dispatches no commands");
}

void exact_row_commands(bool verbose) {
    Harness h;
    h.frame(verbose);
    const auto clock = h.key_with_value(11.25);
    const auto secondary = h.key_with_value(22.5);
    const auto voltage = h.key_with_value(12.5);
    const auto memory_voltage = h.key_with_value(-8.125);
    const auto current = h.key_with_value(126.125);
    const auto policy = h.key_with_value(87.5);
    const auto first_rail = h.key_with_value(1950.125);
    const auto selected_rail = h.key_with_value(2000.125);
    h.state.drafts[clock].value = 42.375; h.state.drafts[clock].dirty = true;
    h.activate_row("advanced-offsets", clock, "Apply", verbose);
    check(h.commands.empty(), "an edited row still requires unlock before Apply");
    h.unlocked = true;
    h.activate_row("advanced-offsets", clock, "Apply", verbose);
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::apply
        && h.commands[0].advanced && h.commands[0].advanced->kind == AdvancedKind::clock_domain
        && h.commands[0].advanced->domain == 7 && near(h.commands[0].value, 42.375)
        && !h.commands[0].rail && !h.commands[0].power_policy
        && h.commands[0].expected_binding == "clock-seven-layout",
        "clock Apply selects the intended nonzero domain and exact pending request");
    h.commands.clear();
    h.activate_row("advanced-offsets", secondary, "Stock", verbose);
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::stock
        && h.commands[0].advanced && h.commands[0].advanced->domain == 21,
        "another clock row's Stock cannot reuse the first row selector");
    h.commands.clear();
    h.state.drafts[voltage].value = 1.125; h.state.drafts[voltage].dirty = true;
    h.activate_row("advanced-offsets", voltage, "Apply", verbose);
    check(h.commands.size() == 1 && h.commands[0].advanced
        && h.commands[0].advanced->kind == AdvancedKind::nvvdd && h.commands[0].advanced->domain == 0
        && near(h.commands[0].value, 1.125), "NVVDD request keeps its own controller kind and precision");
    h.commands.clear();
    h.s.data.advanced_controls[3].state.writable = false;
    h.activate_row("advanced-offsets", memory_voltage, "Apply", verbose);
    check(h.commands.empty(), "unqualified MSVDD Apply is disabled even after unlock");
    h.s.data.xoc_enabled = true;
    h.s.data.advanced_controls[3].state.writable = true; // Capability confirmed by a later read.
    h.activate_row("advanced-offsets", memory_voltage, "Apply", verbose);
    check(h.commands.size() == 1 && h.commands[0].advanced
        && h.commands[0].advanced->kind == AdvancedKind::msvdd && h.commands[0].advanced->domain == 4
        && near(h.commands[0].value, -8.125), "MSVDD Apply uses the confirmed nonzero control");
    h.commands.clear();
    h.state.drafts[current].value = 129.375; h.state.drafts[current].dirty = true;
    h.activate_row("power-policies", current, "Apply", verbose);
    check(h.commands.size() == 1 && h.commands[0].power_policy && *h.commands[0].power_policy == 9
        && !h.commands[0].advanced && !h.commands[0].rail && near(h.commands[0].value, 129.375)
        && h.commands[0].expected_binding == "policy-nine-channel-A",
        "current-policy Apply carries the selected policy and exact UI unit request");
    h.commands.clear();
    h.activate_row("power-policies", policy, "Default", verbose);
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::stock
        && h.commands[0].power_policy && *h.commands[0].power_policy == 25,
        "policy Default is scoped to the clicked policy ID");
    h.commands.clear();
    h.state.drafts[selected_rail].value = 1250.5; h.state.drafts[selected_rail].dirty = true;
    h.activate_row("rail-limits", selected_rail, "Apply", verbose);
    check(h.commands.size() == 1 && h.commands[0].rail && h.commands[0].rail->rail == 1
        && h.commands[0].rail->field == 3 && !h.commands[0].advanced && !h.commands[0].power_policy
        && near(h.commands[0].value, 350.5) && h.commands[0].expected_binding == "rail-one-field-three",
        "rail Apply preserves selected field/binding and converts displayed base plus delta back to the stored request");
    h.commands.clear();
    auto& unavailable = h.s.data.rail_controls[0].state;
    unavailable.current.reset(); unavailable.writable = false; unavailable.stock_writable = true;
    unavailable.availability = Availability::transient;
    h.activate_row("rail-limits", first_rail, "Apply", verbose);
    check(h.commands.empty(), "missing selected rail readback cannot authorize ordinary Apply");
    h.activate_row("rail-limits", first_rail, "Initial", verbose);
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::stock
        && h.commands[0].rail && h.commands[0].rail->rail == 0 && h.commands[0].rail->field == 1,
        "captured Initial remains separate from unavailable Apply/readback");
    h.commands.clear();
    h.state.drafts[clock].value = std::numeric_limits<double>::infinity();
    h.state.drafts[clock].dirty = true;
    h.activate_row("advanced-offsets", clock, "Apply", verbose);
    check(h.commands.empty(), "non-finite numeric draft is not dispatched");
    h.activate_row("advanced-offsets", clock, "Stock", verbose);
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::stock,
        "Stock does not depend on the validity of an unrelated pending numeric draft");
    h.commands.clear();
    h.activate("Undo hardware write", verbose);
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::undo,
        "hardware Undo is an explicit intent rather than local draft restoration");
}

void draft_lifetime_and_layout() {
    Harness h;
    h.frame(false, true, 1024);
    const auto clock = h.key_with_value(11.25);
    const auto secondary = h.key_with_value(22.5);
    const auto policy = h.key_with_value(126.125);
    const auto rail = h.key_with_value(2000.125);
    h.state.drafts[clock].value = 45.125; h.state.drafts[clock].dirty = true;
    h.state.drafts[policy].value = 130.375; h.state.drafts[policy].dirty = true;
    h.state.drafts[rail].value = 1200.625; h.state.drafts[rail].dirty = true;
    h.state.drafts[secondary].editing = true;
    ++h.s.revision;
    h.s.data.advanced_controls[0].state.current = 50;
    h.s.data.advanced_controls[1].state.current = 24.5;
    h.s.data.power_policies[0].state.current = 135;
    h.s.data.rail_controls[1].state.current = 1220;
    h.frame(false, true, 1024);
    check(near(h.state.drafts[clock].value, 45.125) && near(h.state.drafts[policy].value, 130.375)
        && near(h.state.drafts[rail].value, 1200.625), "poll or manual-profile readback cannot replace dirty targets across control families");
    check(near(h.state.drafts[secondary].value, 22.5), "poll preserves an unfinished active numeric edit");
    h.frame(true, false, 1024); // Navigation away from the Advanced renderer.
    h.frame(true, true, 1366);
    check(near(h.state.drafts[clock].value, 45.125) && near(h.state.drafts[policy].value, 130.375)
        && near(h.state.drafts[rail].value, 1200.625), "page/presentation/width changes preserve all pending advanced drafts");
    check(near(h.state.drafts[secondary].value, 24.5), "an unedited inactive field follows later confirmed readback");
    h.s.data.advanced_controls[0].state.current = 45.125;
    h.frame(false, true, 1024);
    check(!h.state.drafts[clock].dirty, "matching configured readback resolves the pending draft");
    check(h.commands.empty(), "polling, navigation and draft synchronization never generate writes");
    const auto text = h.frame(true, true, 1024);
    check(text.find("Absolute limits are estimates") != std::string::npos && text.find("Initial") != std::string::npos
        && text.find("factory") == std::string::npos, "rail presentation distinguishes estimates and captured Initial from factory state");
    auto* window = h.body_window();
    bool layout_valid = window != nullptr;
    if (window) for (const char* name : {"advanced-offsets", "power-policies", "rail-limits"}) {
        const auto* table = ImGui::TableFindByID(window->GetID(name));
        layout_valid = layout_valid && table && table->ColumnsCount == 6
            && table->Columns[1].WidthGiven > 0 && table->Columns[4].WidthGiven >= 50;
    }
    check(layout_valid, "1024-pixel layout with long labels retains positive request and usable action columns");
    ++h.s.generation;
    h.s.data.advanced_controls[0].id.domain = 31;
    h.s.data.advanced_controls[0].state.current = 35.5;
    h.frame(false, true, 1024);
    check(h.state.generation == h.s.generation && h.state.drafts.find(clock) == h.state.drafts.end(),
        "new adapter generation discards old target identities");
    const auto replacement = h.key_with_value(35.5);
    check(!h.state.drafts[replacement].dirty && !h.state.drafts[rail].dirty
        && near(h.state.drafts[rail].value, 2120), "new generation seeds fresh requests rather than replaying old drafts");
    h.state.drafts[replacement].value = 60; h.state.drafts[replacement].dirty = true;
    h.activate("Revert advanced drafts");
    h.frame();
    check(near(h.state.drafts[replacement].value, 35.5) && !h.state.drafts[replacement].dirty
        && h.commands.empty(), "local Revert restores latest observed values without a hardware command");
}

void disconnected_busy_opening() {
    for (int block = 0; block < 3; ++block) {
        Harness h;
        h.unlocked = true;
        h.s.data.xoc_enabled = h.s.data.rail_mode_enabled = true;
        h.frame();
        const auto key = h.key_with_value(11.25);
        h.s.data.connected = block != 0;
        h.s.busy = block == 1;
        h.s.opening = block == 2;
        h.activate("XOC"); h.activate("Rail limits");
        h.activate_row("advanced-offsets", key, "Apply");
        h.activate_row("advanced-offsets", key, "Stock");
        h.activate("Undo hardware write");
        check(h.commands.empty(), "disconnected, busy or opening session rejects mode/write/Undo intents");
    }
}

void rail_conversion_and_binding_replacement(bool verbose) {
    Harness h;
    h.unlocked = true;
    h.s.data.advanced_controls.clear();
    h.s.data.power_policies.clear();
    h.s.data.rail_controls = {{{1, 3}, control(10, "mV"), 1000.0, "rail-controller-A"}};
    h.frame(verbose);
    const auto key = h.key_with_value(1010);
    h.state.drafts[key].value = 1020;
    h.state.drafts[key].dirty = true;
    h.s.data.rail_controls[0].state.current = 12;
    ++h.s.revision;
    h.frame(verbose);
    check(near(h.state.drafts[key].value, 1020) && h.state.drafts[key].dirty,
        "same rail binding retains a dirty absolute draft through fresh request reads");
    h.activate_row("rail-limits", key, "Apply", verbose);
    check(h.commands.size() == 1 && h.commands[0].rail && h.commands[0].rail->rail == 1
        && h.commands[0].rail->field == 3 && near(h.commands[0].value, 20)
        && h.commands[0].expected_binding == "rail-controller-A",
        "base 1000 plus requested absolute 1020 produces exactly delta 20 with its captured binding");
    h.commands.clear();
    auto& replacement = h.s.data.rail_controls[0];
    replacement.binding = "rail-controller-B";
    replacement.estimated_base_mv = 1050;
    replacement.state.current = 15;
    ++h.s.revision;
    h.frame(verbose);
    check(near(h.state.drafts[key].value, 1065) && !h.state.drafts[key].dirty,
        "same rail/field with a new understood binding discards the old controller's draft");
    h.state.drafts[key].value = 1070;
    h.state.drafts[key].dirty = true;
    h.activate_row("rail-limits", key, "Apply", verbose);
    check(h.commands.size() == 1 && near(h.commands[0].value, 20)
        && h.commands[0].expected_binding == "rail-controller-B",
        "replacement controller uses its new base and binding rather than the old target");
    h.commands.clear();
    replacement.binding = "rail-controller-C";
    replacement.state.current.reset();
    replacement.state.availability = Availability::transient;
    ++h.s.revision;
    h.frame(verbose);
    h.activate_row("rail-limits", key, "Apply", verbose);
    check(!h.state.drafts[key].initialized && h.commands.empty(),
        "a replacement binding without current request cannot reuse an initialized old draft");
}

void transient_empty_binding(Availability availability) {
    Harness h;
    h.unlocked = true;
    h.s.data.advanced_controls.clear(); h.s.data.power_policies.clear();
    h.s.data.rail_controls = {{{1, 3}, control(10, "mV"), 1000.0, "established-rail-binding"}};
    h.frame();
    const auto key = h.key_with_value(1010);
    h.state.drafts[key].value = 1020; h.state.drafts[key].dirty = true;
    auto& row = h.s.data.rail_controls[0];
    row.binding.clear(); row.state.current.reset(); row.state.availability = availability;
    ++h.s.revision;
    h.frame();
    check(h.state.drafts[key].initialized && h.state.drafts[key].dirty
        && near(h.state.drafts[key].value, 1020) && h.state.drafts[key].binding == "established-rail-binding",
        "transient/unknown empty binding preserves the established controller and pending target");
    h.activate_row("rail-limits", key, "Apply");
    check(h.commands.size() == 1 && near(h.commands[0].value, 20)
        && h.commands[0].expected_binding == "established-rail-binding",
        "explicit retry retains the established binding guard instead of dispatching an unbound old target");
}

void rail_gate_blocks_and_flashes() {
    Harness h; h.unlocked=true;
    h.s.data.advanced_controls.clear(); h.s.data.power_policies.clear();
    h.s.data.rail_controls={{{0,0},control(10,"mV"),1000.0,"gated-rail"}};
    h.frame(false,true,900); auto* body=h.body_window();
    const int key=3000; const auto seed=ImHashData(&key,sizeof(key),body->GetID("rail-limits"));
    ImGui::ActivateItemByID(ImHashStr("##slider",0,seed)); h.frame(false,true,900);
    const auto rect=body->NavRectRel[ImGuiNavLayer_Main]; ImGui::ClearActiveID();
    h.s.data.rail_mode_enabled=false; h.frame(false,true,900);
    auto& io=ImGui::GetIO();
    io.AddMousePosEvent(body->Pos.x+rect.Min.x+rect.GetWidth()*0.7f,body->Pos.y+rect.GetCenter().y);
    io.AddMouseButtonEvent(0,true); h.frame(false,true,900);
    io.AddMouseButtonEvent(0,false); h.frame(false,true,900);
    check(h.commands.empty() && !h.state.drafts[3000].dirty && h.state.rail_gate_flash_until>ImGui::GetTime(),
        "disabled rail slider flashes its gate without editing or dispatching");
    h.s.data.rail_mode_enabled=true; h.frame(false,true,900);
    io.AddMouseButtonEvent(0,true); h.frame(false,true,900);
    io.AddMouseButtonEvent(0,false); h.frame(false,true,900);
    check(h.state.drafts[3000].dirty && h.commands.empty(),"enabled gate permits a local rail draft without dispatch");
}

void xoc_gate_blocks_and_flashes() {
    Harness h; h.unlocked=true; h.s.data.xoc_enabled=true;
    h.s.data.power_policies.clear(); h.s.data.rail_controls.clear();
    h.s.data.advanced_controls={{{AdvancedKind::msvdd,4},"MSVDD offset",control(10,"mV"),"msvdd"}};
    h.frame(false,true,900); auto* body=h.body_window();
    const int key=1204; const auto seed=ImHashData(&key,sizeof(key),body->GetID("advanced-offsets"));
    ImGui::ActivateItemByID(ImHashStr("##slider",0,seed)); h.frame(false,true,900);
    const auto rect=body->NavRectRel[ImGuiNavLayer_Main]; ImGui::ClearActiveID();
    h.s.data.xoc_enabled=false; h.frame(false,true,900);
    auto& io=ImGui::GetIO();
    io.AddMousePosEvent(body->Pos.x+rect.Min.x+rect.GetWidth()*0.7f,body->Pos.y+rect.GetCenter().y);
    io.AddMouseButtonEvent(0,true); h.frame(false,true,900);
    io.AddMouseButtonEvent(0,false); h.frame(false,true,900);
    check(h.commands.empty() && !h.state.drafts[key].dirty && h.state.xoc_gate_flash_until>ImGui::GetTime(),
        "XOC-gated MSVDD slider flashes XOC without editing or dispatching");
    h.activate_row("advanced-offsets",key,"Stock");
    check(h.commands.size()==1 && h.commands[0].operation==Command::Operation::stock,
        "XOC-off MSVDD Stock escape remains available");
}

void rail_sliders_keep_live_grid(bool classic) {
    Harness h; h.unlocked=true;
    h.s.data.advanced_controls.clear(); h.s.data.power_policies.clear(); h.s.data.rail_controls.clear();
    for(uint32_t field=0;field<4;++field) {
        auto c=control(10.125+field,"mV"); c.low=-349.875; c.high=200.125; c.step=0.001;
        h.s.data.rail_controls.push_back({{0,field},c,1000.0,"live-rail-grid"});
    }
    const float width=classic?1024.0f:650.0f;
    h.frame(classic,true,width);
    auto* body=h.body_window();
    check(body!=nullptr,"rail grid test body exists");
    if(!body) return;
    for(int field=0;field<4;++field) {
        const int key=3000+field;
        const auto seed=ImHashData(&key,sizeof(key),body->GetID("rail-limits"));
        const auto slider=ImHashStr("##slider",0,seed);
        ImGui::ActivateItemByID(slider); h.frame(classic,true,width);
        check(body->NavLastIds[ImGuiNavLayer_Main]==slider,
            "every live-bounded NVVDD field retains a real slider in compact and Classic layouts");
    }
    // Use the actual slider navigation rectangle, avoiding fixed screen coordinates.
    const int first_key=3000;
    const auto first_seed=ImHashData(&first_key,sizeof(first_key),body->GetID("rail-limits"));
    ImGui::ActivateItemByID(ImHashStr("##slider",0,first_seed)); h.frame(classic,true,width);
    const auto rect=body->NavRectRel[ImGuiNavLayer_Main];
    ImGui::ClearActiveID();
    auto& io=ImGui::GetIO();
    io.AddMousePosEvent(body->Pos.x+rect.Min.x+rect.GetWidth()*0.713f,body->Pos.y+rect.GetCenter().y);
    io.AddMouseButtonEvent(0,true); h.frame(classic,true,width);
    io.AddMouseButtonEvent(0,false); h.frame(classic,true,width);
    const auto& draft=h.state.drafts[3000];
    check(draft.dirty && draft.value>=650.125 && draft.value<=1200.125,
        "dragging a rail slider stages an in-range absolute estimate without applying");
    check(near((draft.value-650.125)/0.001,std::round((draft.value-650.125)/0.001)),
        "rail slider preserves the supplied microvolt command grid instead of forcing whole millivolts");
    const double retained=draft.value;
    for(auto& rail:h.s.data.rail_controls) rail.state.availability=Availability::transient;
    h.frame(classic,true,width);
    check(near(h.state.drafts[3000].value,retained) && h.state.drafts[3000].dirty && h.commands.empty(),
        "transient rail reads preserve the established grid and dirty draft without implicit commands");
}
}

int main() {
    ImGui::CreateContext();
    auto& io = ImGui::GetIO();
    io.IniFilename = nullptr; io.DeltaTime = 1.0f / 60.0f;
    ImFontConfig config; config.SizePixels = 18;
    io.Fonts->AddFontDefault(&config);
    unsigned char* pixels = nullptr; int width = 0, height = 0;
    io.Fonts->GetTexDataAsRGBA32(&pixels, &width, &height);
    try {
        mode_intents_and_risk(false);
        mode_intents_and_risk(true);
        weighted_mode_labels_and_lock(false);
        weighted_mode_labels_and_lock(true);
        exact_row_commands(false);
        exact_row_commands(true);
        draft_lifetime_and_layout();
        disconnected_busy_opening();
        rail_conversion_and_binding_replacement(false);
        rail_conversion_and_binding_replacement(true);
        transient_empty_binding(Availability::transient);
        transient_empty_binding(Availability::unknown);
        rail_gate_blocks_and_flashes();
        xoc_gate_blocks_and_flashes();
        rail_sliders_keep_live_grid(false);
        rail_sliders_keep_live_grid(true);
    } catch (const std::exception& error) { check(false, error.what()); }
    ImGui::DestroyContext();
    std::printf("%u advanced view checks, %u failures\n", checks, failures);
    return failures ? 1 : 0;
}
