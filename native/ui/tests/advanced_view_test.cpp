/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
// Headless UI contract test. Link advanced_view.cpp, risk_policy.cpp and the
// pinned ImGui library. No backend implementation, Session worker, DLL, GPU or
// external profile file is opened. All checks execute in Release builds.
#include "advanced_view.h"
#include "session.h"
#include "policy_workspace.h"
#include "imgui.h"
#include "imgui_internal.h"
#include <cmath>
#include <cstdio>
#include <limits>
#include <memory>
#include <vector>
#include "clock_monitor_model.h"
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

void rail_sliders_use_whole_number_grid(bool classic) {
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
    check(near(draft.value,std::round(draft.value)),
        "rail slider stages the closest whole-number request");
    const double retained=draft.value;
    for(auto& rail:h.s.data.rail_controls) rail.state.availability=Availability::transient;
    h.frame(classic,true,width);
    check(near(h.state.drafts[3000].value,retained) && h.state.drafts[3000].dirty && h.commands.empty(),
        "transient rail reads preserve the established grid and dirty draft without implicit commands");
}
// Classic Rails column: Python build_volt_limits_rows summary, Link and notes.
std::string daily_rails(Harness& h, const RailReadouts* readouts, bool classic = true) {
    ImGui::GetIO().DisplaySize = {1280, 1000};
    ImGui::NewFrame();
    ImGui::SetNextWindowPos({0, 0}); ImGui::SetNextWindowSize({1280, 1000});
    ImGui::Begin(window_name, nullptr, ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoResize);
    ImGui::LogToBuffer();
    ImGui::BeginChild("advanced-body", {0, 0});
    draw_daily_rail_controls(h.s, h.unlocked, classic, h.state, h.commands, false, readouts);
    ImGui::EndChild();
    const std::string text = ImGui::GetCurrentContext()->LogBuffer.c_str();
    ImGui::LogFinish(); ImGui::End(); ImGui::Render();
    return text;
}
bool contains(const std::string& text, const char* value) { return text.find(value) != std::string::npos; }
void classic_rail_summary_and_link() {
    Harness h; h.unlocked = true;
    h.s.data.advanced_controls.clear(); h.s.data.power_policies.clear(); h.s.data.rail_controls.clear();
    // Alternate adapter: each field has its own estimated base, not one board's constants.
    const double bases[4] = {1001.5, 1012.25, 990.0, 700.125};
    const double deltas[4] = {-10, 2.5, 30, 0.375};
    for (uint32_t field = 0; field < 4; ++field)
        h.s.data.rail_controls.push_back({{0, field}, control(deltas[field], "mV"), bases[field], "nvvdd-field-" + std::to_string(field)});
    RailReadouts readouts{};
    readouts[0].listed = true; readouts[0].boost_mv = 6.25;
    dc_rails_state_row live{}; live.live = 887.5; live.effective = 997.75; readouts[0].state = live;
    const auto summary_fits = [&](int first, int last) {
        auto* body = h.body_window();
        const auto* table = body ? ImGui::TableFindByID(body->GetID("daily-rail-controls")) : nullptr;
        if (!table) return false;
        for (int i = first; i <= last; ++i) {
            const auto& column = table->Columns[i];
            if (column.ContentMaxXUnfrozen > column.WorkMaxX + 1.0f) return false;
        }
        return true;
    };
    auto text = daily_rails(h, &readouts);
    check(contains(text, "NVVDD estimated limits") && contains(text, "rel 991.5 / alt 1014.75")
        && contains(text, "ov 1020 / vmin 700.5 mV"), "summary shows each field as its own base plus the stored request");
    check(contains(text, "estimated cap 997.75"), "cap is min(rel + reported boost, alt, ov)");
    check(contains(text, "live 887.5 mV") && contains(text, "eff 997.75"), "live and effective come from the rail state read");
    check(summary_fits(2, 4), "the estimated cap, live reading and effective limit fit their Classic table cells");
    check(contains(text, "Ceilings") && contains(text, "Link reliability + alt-reliability") && contains(text, "Reference")
        && contains(text, "first stable driver reading"), "writable rails show the Link and Reference rows");
    check(contains(text, "Driver reports NVVDD only"), "an NVVDD-only adapter shows the NVVDD note");
    check(!contains(text, "MSVDD estimated limits") && !contains(text, "MSVDD (read-only)"), "an absent rail gets no summary row");
    check(h.state.rail_link && h.commands.empty(), "Link defaults on and drawing writes nothing");

    // MSVDD listed without a usable reference: Python's read-only row, no cap, no NVVDD-only note.
    readouts[1].listed = true; dc_rails_state_row msvdd{}; msvdd.live = 0; msvdd.reliability = 850; msvdd.alt_reliability = 860;
    msvdd.overvoltage = 870; msvdd.vmin = 600; readouts[1].state = msvdd;
    text = daily_rails(h, &readouts);
    check(contains(text, "MSVDD (read-only)") && contains(text, "rel 850 / alt 860") && contains(text, "live --")
        && !contains(text, "Driver reports NVVDD only"), "a listed rail without reference shows driver absolutes read-only");

    // A failed read keeps the last reading, marked stale; the cap waits for a fresh read.
    readouts[0].stale = true; text = daily_rails(h, &readouts);
    check(contains(text, "NVVDD estimated limits") && !contains(text, "estimated cap") && contains(text, "live 887.5 mV (stale)")
        && contains(text, "eff 997.75 (stale)"), "a stale readout keeps live and effective marked stale and hides the cap");
    check(summary_fits(3, 4), "the live reading and effective limit fit their Classic table cells with stale markers");
    // Missing readouts show unknown, never an invented reading or cap.
    text = daily_rails(h, nullptr);
    check(contains(text, "rel 991.5 / alt 1014.75") && contains(text, "live --") && !contains(text, "estimated cap"),
        "without readouts the summary keeps the request fields and reads live as unknown");
    check(!contains(text, "Driver reports NVVDD only"),
        "without readouts which rails the driver lists is unknown, so the NVVDD-only note is not claimed");
    readouts[0].stale = false;
    // A listed rail missing from the state read: the core's boost default is 0.0, as Python's.
    readouts[0].boost_mv = 0; text = daily_rails(h, &readouts);
    check(contains(text, "estimated cap 991.5"), "a zero reported boost still gives Python's cap, min(rel, alt, ov)");
    readouts[0].boost_mv = 6.25;

    // Link on: Apply on reliability stages both reliability fields at one absolute voltage.
    daily_rails(h, &readouts);
    auto* body = h.body_window(); check(body != nullptr, "daily rails body exists"); if (!body) return;
    const auto apply = [&](int key) {
        const auto seed = ImHashData(&key, sizeof(key), body->GetID("daily-rail-controls"));
        ImGui::ActivateItemByID(ImHashStr("Apply", 0, seed)); daily_rails(h, &readouts);
    };
    h.state.drafts[3001].value = 1015; h.state.drafts[3001].dirty = true; // a stale alt edit
    h.state.drafts[3000].value = 1040.5; h.state.drafts[3000].dirty = true;
    apply(3000);
    check(h.commands.size() == 1 && h.commands[0].rail && h.commands[0].rail->field == 0 && near(h.commands[0].value, 39)
        && h.commands[0].expected_binding == "nvvdd-field-0" && h.commands[0].operation == Command::Operation::apply
        && h.commands[0].rail_partner && h.commands[0].rail_partner->id.rail == 0 && h.commands[0].rail_partner->id.field == 1
        && near(h.commands[0].rail_partner->value, 28.25) && h.commands[0].rail_partner->expected_binding == "nvvdd-field-1",
        "linked Apply stages one command naming alt-reliability at the same absolute voltage through its own base and binding");
    check(!h.state.drafts[3001].dirty, "the linked partner's draft follows its readback");
    h.commands.clear();
    h.state.drafts[3002].value = 1030; h.state.drafts[3002].dirty = true; apply(3002);
    check(h.commands.size() == 1 && h.commands[0].rail->field == 2 && !h.commands[0].rail_partner, "overvoltage is never linked");
    h.commands.clear();
    h.state.drafts[3003].value = 705; h.state.drafts[3003].dirty = true; apply(3003);
    check(h.commands.size() == 1 && h.commands[0].rail->field == 3 && !h.commands[0].rail_partner, "vmin is never linked");
    h.commands.clear();

    // Unlinked: each field moves independently.
    h.state.rail_link = false;
    h.state.drafts[3001].value = 1020.25; h.state.drafts[3001].dirty = true; apply(3001);
    check(h.commands.size() == 1 && h.commands[0].rail->field == 1 && near(h.commands[0].value, 8) && !h.commands[0].rail_partner,
        "unlinked Apply stages one field");
    h.commands.clear();
    ++h.s.generation; daily_rails(h, &readouts);
    check(!h.state.rail_link, "an adapter reopen keeps the Link choice as Python keeps it across rebuild");
    h.state.rail_link = true;

    const auto link_id = ImHashStr("Link reliability + alt-reliability", 0, body->GetID("daily-rail-controls"));
    h.state.log_notes.clear();
    ImGui::ActivateItemByID(link_id); daily_rails(h, &readouts);
    check(!h.state.rail_link && h.commands.empty(), "the Link checkbox toggles staging only and writes nothing");
    // Python's checkbox callback log line, lower-case, one per accepted change.
    check(h.state.log_notes.size() == 1 && h.state.log_notes[0]
        == "rail ceilings unlinked: each field moves independently; watch the cap and effective limit",
        "unticking Link queues Python's unlinked log line");
    ImGui::ActivateItemByID(link_id); daily_rails(h, &readouts);
    check(h.state.rail_link && h.state.log_notes.size() == 2 && h.state.log_notes[1]
        == "rail ceilings linked: both fields move together, which leaves the voltage boost slider with no range to work in",
        "ticking Link queues Python's linked log line");
    daily_rails(h, &readouts);
    check(h.state.log_notes.size() == 2, "drawing without a change queues no log line");
    h.state.log_notes.clear();
    // Rail limits mode off: Link is shown but disabled, and Apply stays gated.
    h.s.data.rail_mode_enabled = false; daily_rails(h, &readouts);
    ImGui::ActivateItemByID(link_id); daily_rails(h, &readouts);
    check(h.state.rail_link && h.commands.empty() && h.state.log_notes.empty(), "Link cannot change while Rail limits is off, and nothing is logged");
    h.s.data.rail_mode_enabled = true;
    // Not writable: no Ceilings or Reference rows, as Python returns before them.
    h.s.data.rail_writable = false; text = daily_rails(h, &readouts);
    check(contains(text, "NVVDD estimated limits") && !contains(text, "Ceilings") && !contains(text, "Reference"),
        "a non-writable block shows only the summary");
    // Without the Ceilings row Python has no vlim_link, so nothing is linked.
    h.state.drafts[3000].value = 1040.5; h.state.drafts[3000].dirty = true; apply(3000);
    check(h.commands.size() == 1 && h.commands[0].rail->field == 0 && !h.commands[0].rail_partner,
        "a block without the Link row never stages the partner field");
    h.commands.clear();
    h.s.data.rail_writable = true;
    // Link ticked but the pair cannot be completed: the Apply is refused, never sent as one field, and the row says why.
    const auto saved_controls = h.s.data.rail_controls;
    h.s.data.rail_controls.erase(h.s.data.rail_controls.begin() + 1); // alt-reliability missing from this read
    text = daily_rails(h, &readouts);
    check(contains(text, "NVVDD: reliability and alt-reliability are not both readable"), "an incomplete linked pair is explained");
    h.state.drafts[3000].value = 1040.5; h.state.drafts[3000].dirty = true; apply(3000);
    check(h.commands.empty(), "linked Apply with the partner missing stages nothing");
    h.s.data.rail_controls = saved_controls; h.s.data.rail_controls[1].estimated_base_mv.reset(); // partner without a reference
    h.state.drafts[3000].value = 1040.5; h.state.drafts[3000].dirty = true; apply(3000);
    check(h.commands.empty(), "linked Apply with the partner's base unknown stages nothing");
    h.state.rail_link = false; h.state.drafts[3000].dirty = true; apply(3000);
    check(h.commands.size() == 1 && h.commands[0].rail->field == 0 && !h.commands[0].rail_partner, "unlinked, the same field applies alone");
    h.commands.clear(); h.state.rail_link = true; h.s.data.rail_controls = saved_controls;
    text = daily_rails(h, &readouts);
    check(!contains(text, "are not both readable"), "a complete pair shows no Link warning");
    // Focused presentation is unchanged: no summary and no linking.
    text = daily_rails(h, &readouts, false);
    check(!contains(text, "estimated limits") && !contains(text, "Link reliability"), "Focused rails draw no Classic summary");
}
// Python domain_knob_label / DOMAIN_KNOBS: names and grades come from the
// pairing and the Monitor's classified rows, never from one adapter's table.
void classic_clock_domain_labels_and_order() {
    SessionSnapshot s = snapshot();
    // Adapter order differs from Python's knob order; control 7 is one Python does not list.
    s.data.advanced_controls = {
        {{AdvancedKind::clock_domain, 1}, "Crossbar", control(0, "MHz"), "c1"},
        {{AdvancedKind::clock_domain, 2}, "Additional memory offset", control(0, "MHz"), "c2"},
        {{AdvancedKind::clock_domain, 3}, "SYS", control(0, "MHz"), "c3"},
        {{AdvancedKind::nvvdd, 0}, "NVVDD offset", control(0, "mV"), "n0"},
        {{AdvancedKind::clock_domain, 5}, "Video", control(0, "MHz"), "c5"},
        {{AdvancedKind::clock_domain, 7}, "domain 7", control(0, "MHz"), "c7"},
        {{AdvancedKind::clock_domain, 9}, "LTC", control(0, "MHz"), "c9"},
    };
    std::vector<uint32_t> order;
    for (const auto* c : classic_clock_domain_order(s.data)) order.push_back(c->id.domain);
    check(order == std::vector<uint32_t>({1, 3, 5, 9, 7, 2}), "Classic domain rows follow Python's knob order with additional memory last");
    // An alternate pairing: names from the classified rows, one hedged, one unpaired.
    s.data.clock_domain_pairs = {{1, 11}, {3, 12}, {9, 13}, {2, 14}, {7, 15}};
    auto monitor = std::make_shared<ClockMonitorData>(); monitor->available = true;
    const auto add_row = [&](uint32_t domain, const char* name, const char* grade) {
        ClockDomainViewRow row; row.raw.domain = domain;
        std::snprintf(row.raw.name, sizeof(row.raw.name), "%s", name); std::snprintf(row.raw.grade, sizeof(row.raw.grade), "%s", grade);
        monitor->rows.push_back(row);
    };
    add_row(11, "XBARX", "confirmed"); add_row(12, "SYSX", "confirmed"); add_row(13, "L2X", "likely"); add_row(14, "MEMX", "confirmed");
    s.data.clock_monitor = monitor;
    const auto label = [&](uint32_t domain) {
        for (const auto& c : s.data.advanced_controls) if (c.id.kind == AdvancedKind::clock_domain && c.id.domain == domain) return clock_domain_label(s, c);
        throw std::runtime_error("missing control");
    };
    using Tone = ClockDomainLabel::Tone;
    check(label(1).text == "XBARX clock offset (MHz)" && label(1).tone == Tone::text, "a confirmed paired name is plain text");
    check(label(9).text == "L2X? clock offset (MHz)" && label(9).tone == Tone::warn, "a likely name carries '?' in WARN");
    check(label(5).text == "control domain 5 offset (MHz)" && label(5).tone == Tone::dim, "an unpaired control reads as a bare index in DIM");
    check(label(7).text == "domain 15 clock offset (MHz)" && label(7).tone == Tone::dim, "a paired domain with no classified row is unnamed and DIM");
    check(label(2).text == "Additional Memory Clock Offset (MHz)" && label(2).tone == Tone::text, "additional memory keeps its own label, coloured by its pairing");
    s.data.clock_domain_pairs.clear();
    check(label(1).text == "crossbar offset (MHz)" && label(1).tone == Tone::dim, "unpaired control 1 uses Python's crossbar fallback");
    s.data.clock_domain_requests_only = true;
    check(label(3).text == "SYS requested offset (MHz)" && label(3).tone == Tone::good, "Blackwell names the control block's own label in GOOD");
}
std::string classic_rails(Harness& h) {
    ImGui::GetIO().DisplaySize = {1600, 1000};
    ImGui::NewFrame();
    ImGui::SetNextWindowPos({0, 0}); ImGui::SetNextWindowSize({1600, 1000});
    ImGui::Begin(window_name, nullptr, ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoResize);
    ImGui::LogToBuffer();
    ImGui::BeginChild("advanced-body", {0, 0});
    draw_daily_rail_controls(h.s, h.unlocked, true, h.state, h.commands, true, nullptr);
    ImGui::EndChild();
    const std::string text = ImGui::GetCurrentContext()->LogBuffer.c_str();
    ImGui::LogFinish(); ImGui::End(); ImGui::Render();
    return text;
}
// Python build_volt_limits_rows and sync_risk_ui: labels, the MSVDD note, live
// rail-limit widgets that refuse Apply while Rail limits is off, and an Initial
// that stays available as the recovery action.
void classic_rail_labels_and_soft_gate() {
    Harness h; h.unlocked = true;
    h.s.data.power_policies.clear(); h.s.data.rail_controls.clear();
    h.s.data.advanced_controls = {
        {{AdvancedKind::nvvdd, 0}, "NVVDD offset", control(0, "mV"), "n0"},
        {{AdvancedKind::msvdd, 4}, "MSVDD offset", control(0, "mV"), "m4"},
    };
    for (uint32_t field = 0; field < 4; ++field) h.s.data.rail_controls.push_back({{0, field}, control(field, "mV"), 1000.0 + field, "nv-" + std::to_string(field)});
    auto text = classic_rails(h);
    check(contains(text, "NVVDD vmin (~mV)") && contains(text, "NVVDD offset (mV)") && contains(text, "MSVDD requested offset (mV)"),
        "Classic rail rows use Python's labels");
    check(!contains(text, "Driver-reported rail; physical effect"), "an NVVDD-only adapter shows no MSVDD note");
    h.s.data.rail_controls.push_back({{1, 0}, control(0, "mV"), 800.0, "ms-0"});
    text = classic_rails(h);
    const auto note = text.find("Driver-reported rail; physical effect"), row = text.find("MSVDD reliability (~mV)");
    check(note != std::string::npos && row != std::string::npos && note < row, "the MSVDD note sits ahead of the rail-1 rows");
    h.s.data.rail_controls.pop_back();
    // Rail limits off on a writable block: Apply flashes the gate and writes nothing; Initial still restores.
    h.s.data.rail_mode_enabled = false;
    for (auto& r : h.s.data.rail_controls) r.state.writable = false; // the adapter's writable flag includes the mode
    classic_rails(h);
    auto* body = h.body_window(); check(body != nullptr, "classic rails body exists"); if (!body) return;
    const auto activate = [&](int key, const char* label) {
        const auto seed = ImHashData(&key, sizeof(key), body->GetID("daily-rail-controls"));
        ImGui::ActivateItemByID(ImHashStr(label, 0, seed)); classic_rails(h);
    };
    h.state.drafts[3000].value = 1001; h.state.drafts[3000].dirty = true;
    activate(3000, "Apply");
    check(h.commands.empty() && h.state.rail_gate_flash_until > ImGui::GetTime(), "Apply with Rail limits off flashes the gate and writes nothing");
    activate(3001, "Initial");
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::stock && h.commands[0].rail && h.commands[0].rail->field == 1,
        "Initial stays available with Rail limits off");
    h.commands.clear();
    // Python flashes the gate only from Apply: clicking the live slider, the
    // input or Initial edits or restores without claiming a refusal.
    const auto click = [&](int key, const char* label) {
        const auto seed = ImHashData(&key, sizeof(key), body->GetID("daily-rail-controls"));
        ImGui::ActivateItemByID(ImHashStr(label, 0, seed)); classic_rails(h);
        const auto rect = body->NavRectRel[ImGuiNavLayer_Main]; ImGui::ClearActiveID(); classic_rails(h);
        h.commands.clear(); h.state.rail_gate_flash_until = 0; h.state.drafts[key].dirty = false;
        auto& io = ImGui::GetIO();
        io.AddMousePosEvent(body->Pos.x + rect.GetCenter().x, body->Pos.y + rect.GetCenter().y);
        io.AddMouseButtonEvent(0, true); classic_rails(h);
        io.AddMouseButtonEvent(0, false); classic_rails(h);
    };
    h.state.drafts[3002] = {};
    click(3002, "##slider");
    check(h.state.drafts[3002].dirty && h.commands.empty() && h.state.rail_gate_flash_until == 0,
        "a click on a live rail-limit slider edits the draft without flashing the gate");
    click(3002, "##value");
    check(h.commands.empty() && h.state.rail_gate_flash_until == 0, "a click on a live rail-limit input does not flash the gate");
    click(3002, "Initial");
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::stock && h.state.rail_gate_flash_until == 0,
        "Initial restores with Rail limits off without flashing the gate");
    click(3002, "Apply");
    check(h.commands.empty() && h.state.rail_gate_flash_until > ImGui::GetTime(), "a mouse click on Apply still flashes the gate");
    ImGui::GetIO().AddMousePosEvent(-FLT_MAX, -FLT_MAX); classic_rails(h);
    h.commands.clear();
    // XOC off: the MSVDD offset Apply stays refused, its Zero escape remains.
    h.s.data.xoc_enabled = false; h.state.rail_gate_flash_until = 0; h.state.xoc_gate_flash_until = 0;
    activate(1204, "Apply");
    check(h.commands.empty(), "MSVDD Apply stays XOC-gated");
    activate(1204, "Zero");
    check(h.commands.size() == 1 && h.commands[0].operation == Command::Operation::stock, "MSVDD Zero stays available without XOC");
}
size_t text_colour_vertices() {
    const ImU32 text = IM_COL32(230, 232, 236, 255);
    size_t n = 0; const auto* data = ImGui::GetDrawData();
    for (int i = 0; i < data->CmdListsCount; ++i) for (const auto& vertex : data->CmdLists[i]->VtxBuffer) n += vertex.col == text;
    return n;
}
// Python slider_row: a knob label is TEXT (230,232,236) unless its row passes a
// colour of its own; refresh_control draws a present live reading in TEXT. The
// theme's text stays white for widgets, and Focused rows keep it.
void classic_knob_text_colour() {
    Harness h; h.unlocked = true; h.s.data.power_policies.clear(); h.s.data.advanced_controls.clear();
    h.s.data.rail_controls = {{{0, 0}, control(0, "mV"), 1000.0, "nv-0"}};
    classic_rails(h); const auto nvvdd = text_colour_vertices();
    h.s.data.rail_controls = {{{1, 0}, control(0, "mV"), 800.0, "ms-0"}};
    classic_rails(h);
    check(nvvdd > text_colour_vertices(), "a Classic rail-limit label is drawn in TEXT; an MSVDD row keeps its WARN label");
    h.s.data.rail_controls.clear();
    h.s.data.advanced_controls = {{{AdvancedKind::nvvdd, 0}, "NVVDD offset", control(0, "mV"), "n0"}};
    classic_rails(h); const auto unread = text_colour_vertices();
    h.s.data.telemetry.present = DC_T_VCORE_MV; h.s.data.telemetry.vcore_mv = 912.5;
    classic_rails(h);
    check(text_colour_vertices() > unread, "a present live reading is drawn in TEXT");
    ImGui::GetIO().DisplaySize = {1600, 1000};
    ImGui::NewFrame();
    ImGui::SetNextWindowPos({0, 0}); ImGui::SetNextWindowSize({1600, 1000});
    ImGui::Begin(window_name, nullptr, ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoResize);
    draw_daily_rail_controls(h.s, h.unlocked, false, h.state, h.commands, true, nullptr);
    ImGui::End(); ImGui::Render();
    check(text_colour_vertices() == 0, "Focused rows keep the theme's text colour");
    check(h.commands.empty(), "colouring writes nothing");
}
void power_request_status_sentences() {
    auto status = power_request_status(900.0, 900.0, 972.07);
    check(status.text == "Configured request: 900 W. NVML-reported enforced limit: 900 W. Native effective limit: 972.07 W." && !status.warn,
        "agreeing limits read as Python's DIM sentence");
    status = power_request_status(450.5, 400.0, std::nullopt);
    check(status.text == "Configured request: 450.5 W. NVML-reported enforced limit: 400 W. Below configured request; downstream limits may apply."
        " Native effective limit unavailable; NVML readback does not verify it." && status.warn, "an enforced limit below the request warns");
    status = power_request_status(300.0, 320.0, 250.0);
    check(contains(status.text, "Differs from configured request.") && contains(status.text, "Native limit is below configured request") && status.warn,
        "a differing enforced limit and a lower native limit are both named");
    status = power_request_status(std::nullopt, std::nullopt, std::nullopt);
    check(status.text == "Configured power request unverified: readback unavailable. NVML enforced limit unavailable."
        " Native effective limit unavailable; NVML readback does not verify it." && status.warn, "unknown readbacks stay unknown and warn");
}
}

// Released 1.9.1 power_policy_row reasons and refresh_power_policies' live cell.
void power_policy_reasons_and_live() {
    PowerPolicy p; p.id = 3; p.state.availability = Availability::available; p.state.unit = "A";
    p.state.current = 30; p.state.low = 10; p.state.high = 40; p.state.writable = true; p.state.detail = "Configured policy request";
    check(power_policy_readonly_reason(p).empty(), "a writable ordinary policy keeps its slider");
    p.core_current = true;
    check(power_policy_readonly_reason(p) == "set with its current-limit slider on the Control tab", "the core current limit points to the Control tab");
    p.core_current = false; p.state.writable = false; p.state.detail = "Board request is controlled separately";
    check(power_policy_readonly_reason(p) == "set with Power limit (W) on the Control tab", "the board policy points to Power limit");
    p.state.detail = "Policy record is inconsistent"; p.state.low = 40;
    check(power_policy_readonly_reason(p) == "no range to write (40 A .. 40 A)", "an empty range is named with both ends");
    p.state.low = 10;
    check(power_policy_readonly_reason(p) == "read-only: Policy record is inconsistent", "any other read-only row gives its reason");
    p.state.writable = true;
    check(!power_policy_live(p, "").known && power_policy_live(p, "").text == "--", "no reading in this read shows --, never the request");
    p.observed_amps = 24.6; p.limit_amps = 39;
    check(power_policy_live(p, "").text == "24.6 A\n\xE2\x89\xA4" "39 A", "a plain current row shows reading over effective limit");
    const auto wire = power_policy_live(p, "12VHPWR/12V-2x6");
    check(wire.text == "24.6 A (4.10/wire)\n\xE2\x89\xA4" "39 A (6.50/wire)" && wire.band == 0, "a connector row shows per-wire figures and their band");
    p.observed_amps = 6.6;
    const auto slot = power_policy_live(p, "PCIE");
    check(slot.text == "6.6 A\n\xE2\x89\xA4" "39 A" && slot.band == 2, "the slot is judged as a whole against its own bands");
    p.state.availability = Availability::transient;
    check(!power_policy_live(p, "").known, "a failed read does not present the last reading as live");
    PowerPolicy watts; watts.state.unit = "W"; watts.state.availability = Availability::available; watts.observed_amps = 1;
    check(!power_policy_live(watts, "").known && power_policy_live(watts, "").text == "--", "a power row shows no current reading as its live value");
    watts.observed_watts = 312.25; watts.limit_watts = 450;
    const auto power_row = power_policy_live(watts, "12VHPWR/12V-2x6");
    check(power_row.known && power_row.text == "312.25 W\n\xE2\x89\xA4" "450 W" && power_row.band == -1,
        "a power row shows its reading over its effective limit in plain text, never per wire");
    watts.limit_watts.reset();
    check(!power_policy_live(watts, "").known, "a power row without its effective limit stays unknown");
    watts.limit_watts = 450; watts.state.availability = Availability::transient;
    check(!power_policy_live(watts, "").known, "a failed read does not present the last power reading as live");
    check(power_policy_live(watts, "").text == "unavailable", "a row the latest read did not return reads Python's 'unavailable'");
    // Python refresh_power_policies' held mark: the card still holds this session's pin.
    PowerPolicy held; held.id = 5; held.state.availability = Availability::available; held.state.unit = "A";
    held.state.current = 30; held.observed_amps = 24.6; held.limit_amps = 39; held.pinned = 30;
    const auto plain_held = power_policy_live(held, "");
    check(plain_held.held && plain_held.text == "24.6 A\n\xE2\x89\xA4" "39 A *" && plain_held.band == -1, "an unscaled held row ends its limit with ' *'");
    const auto wire_held = power_policy_live(held, "12VHPWR/12V-2x6");
    check(wire_held.held && wire_held.text == "24.6 A (4.10/wire)\n\xE2\x89\xA4" "39 A (6.50/wire) *" && wire_held.band == 0, "a per-wire held row keeps its band and ends with ' *'");
    held.observed_amps = 6.6;
    const auto slot_held = power_policy_live(held, "PCIE");
    check(slot_held.held && slot_held.text == "6.6 A\n\xE2\x89\xA4" "39 A *" && slot_held.band == 2, "a slot row judged as a whole is marked too");
    held.pinned = 28;
    check(!power_policy_live(held, "").held && power_policy_live(held, "").text.find('*') == std::string::npos, "a pin the card no longer holds is not marked");
    held.pinned.reset();
    check(!power_policy_live(held, "").held, "an unpinned request is not marked");
    watts.state.availability = Availability::available; watts.state.current = 450; watts.pinned = 450;
    const auto watts_held = power_policy_live(watts, "");
    check(watts_held.held && watts_held.text == "312.25 W\n\xE2\x89\xA4" "450 W *", "a held power row is marked");
    // build_power_policies: "Power policies unavailable: {error}".
    SessionSnapshot none; none.data.connected = false;
    check(power_policies_unavailable_text(none) == "Power policies unavailable: no GPU session is connected.", "no session is named");
    SessionSnapshot failed; failed.data.connected = true;
    auto& power = failed.data.controls[static_cast<size_t>(ControlId::power)];
    power.availability = Availability::transient; power.detail = "Power policy interfaces or lock unavailable";
    check(power_policies_unavailable_text(failed) == "Power policies unavailable: Power policy interfaces or lock unavailable", "a failed read gives its own reason");
    power.availability = Availability::available; power.detail = "Configured request";
    check(power_policies_unavailable_text(failed) == "Power policies unavailable: the driver's power-policy table was not read.", "an unread table says so");
}
// Vertices drawn in `colour` by the Power policies section alone.
size_t policy_section_vertices(Harness& h, ImU32 colour) {
    ImGui::GetIO().DisplaySize = {1600, 1000};
    ImGui::NewFrame();
    ImGui::SetNextWindowPos({0, 0}); ImGui::SetNextWindowSize({1600, 1000});
    ImGui::Begin(window_name, nullptr, ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoResize);
    draw_power_policy_controls(h.s, h.unlocked, h.state, h.commands, nullptr);
    ImGui::End(); ImGui::Render();
    size_t count = 0; const auto* data = ImGui::GetDrawData();
    for (int i = 0; i < data->CmdListsCount; ++i) for (const auto& vertex : data->CmdLists[i]->VtxBuffer) if (vertex.col == colour) ++count;
    return count;
}
// Python refresh_power_policies colours every live reading TEXT (or its band),
// a read-only row's as a writable row's: the board W row is not drawn DIM.
void power_policy_readonly_reading_colour() {
    Harness h;
    PowerPolicy board; board.id = 0; board.label = "Policy 0"; board.state = control(450, "W");
    board.state.writable = board.state.stock_writable = false; board.state.detail = "Board request is controlled separately";
    h.s.data.power_policies = {board};
    const ImU32 text = IM_COL32(230, 232, 236, 255);
    check(policy_section_vertices(h, text) == 0, "a read-only row without a reading draws nothing in TEXT");
    h.s.data.power_policies[0].observed_watts = 301; h.s.data.power_policies[0].limit_watts = 480;
    check(policy_section_vertices(h, text) > 0, "a read-only row's known reading is drawn in TEXT, not DIM");
    // Python: an unscaled held row is drawn GOOD instead of TEXT.
    const ImU32 good = IM_COL32(70, 209, 122, 255);
    check(policy_section_vertices(h, good) == 0, "a row without a held pin draws nothing in GOOD");
    h.s.data.power_policies[0].pinned = 450;
    check(policy_section_vertices(h, good) > 0, "a held unscaled row's reading is drawn GOOD");
    PowerPolicy writable; writable.id = 6; writable.label = "Policy 6"; writable.state = control(30, "A");
    writable.state.low = 0; writable.state.high = 60; writable.observed_amps = 20; writable.limit_amps = 30;
    h.s.data.power_policies = {writable};
    check(policy_section_vertices(h, good) == 0, "a writable row that is not held is not GOOD");
    h.s.data.power_policies[0].pinned = 30;
    check(policy_section_vertices(h, good) > 0, "a held writable row's reading is drawn GOOD");
}
// Python refresh_power_policies binds the band of the staged value to a connector
// current row's value box: 72 A over a 12VHPWR's six wires is 12 A a wire, red.
void power_policy_staged_band() {
    const ImU32 red = IM_COL32(255, 120, 120, 255), crimson = IM_COL32(220, 20, 60, 255);
    const auto vertices = [](double request, const char* unit, const char* note, ImU32 colour) {
        Harness h; h.s.selected_slot = "0000:01:00.0"; std::snprintf(h.s.data.identity.uuid, sizeof(h.s.data.identity.uuid), "GPU-staged-band");
        PowerPolicy p; p.id = 4; p.label = "Policy 4"; p.state = control(request, unit); p.state.low = 0; p.state.high = 120;
        p.binding = "policy-four"; p.channel = 1; p.policy_type = 2;
        h.s.data.power_policies = {p};
        PolicyAnnotations notes;
        ScopedCurrent row; row.key = ScopedKey::policy_request(p.id); row.binding = p.binding; row.channel = p.channel; row.policy_type = p.policy_type; row.fresh = true;
        if (note) notes.set({h.s.data.identity.uuid, h.s.selected_slot}, row, note);
        ImGui::GetIO().DisplaySize = {1600, 1000};
        ImGui::NewFrame();
        ImGui::SetNextWindowPos({0, 0}); ImGui::SetNextWindowSize({1600, 1000});
        ImGui::Begin(window_name, nullptr, ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoResize);
        draw_power_policy_controls(h.s, h.unlocked, h.state, h.commands, &notes);
        ImGui::End(); ImGui::Render();
        size_t count = 0; const auto* data = ImGui::GetDrawData();
        for (int i = 0; i < data->CmdListsCount; ++i) for (const auto& vertex : data->CmdLists[i]->VtxBuffer) if (vertex.col == colour) ++count;
        return count;
    };
    check(vertices(72, "A", "12VHPWR/12V-2x6", red) > 0, "a staged 12 A a wire draws the value box red");
    check(vertices(24, "A", "12VHPWR/12V-2x6", red) == 0 && vertices(24, "A", "12VHPWR/12V-2x6", crimson) == 0, "a staged 4 A a wire stays in the plain band");
    check(vertices(90, "A", "12VHPWR/12V-2x6", crimson) > 0, "a staged 15 A a wire draws the value box crimson");
    check(vertices(6.6, "A", "PCIE", red) > 0, "the slot judges the staged value as a whole");
    check(vertices(72, "A", nullptr, red) == 0, "a current row without a connector note keeps the default colour");
    check(vertices(72, "W", "12VHPWR/12V-2x6", red) == 0, "a power row is never banded");
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
        rail_sliders_use_whole_number_grid(false);
        rail_sliders_use_whole_number_grid(true);
        classic_rail_summary_and_link();
        classic_clock_domain_labels_and_order();
        classic_rail_labels_and_soft_gate();
        classic_knob_text_colour();
        power_request_status_sentences();
        power_policy_reasons_and_live();
        power_policy_readonly_reading_colour();
        power_policy_staged_band();
    } catch (const std::exception& error) { check(false, error.what()); }
    ImGui::DestroyContext();
    std::printf("%u advanced view checks, %u failures\n", checks, failures);
    return failures ? 1 : 0;
}
