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
    check(state.curve_draft.baseline().empty() && !state.curve_draft.dirty() && state.controls_unlocked,
        "new adapter/session clears curve edits while preserving unlock preference without replay");
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
    ImGui::ActivateItemByID(control_button(ControlId::fan,"Apply")); frame(state,1366,900,false);
    state.commands.clear();
    const auto apply_rect=body->NavRectRel[ImGuiNavLayer_Main];
    io.AddMousePosEvent(body->Pos.x+apply_rect.GetCenter().x,body->Pos.y+apply_rect.GetCenter().y);
    for(int i=0;i<4;++i) frame(state,1366,900,false);
    bool active_tooltip=false;
    for(auto* window:ImGui::GetCurrentContext()->Windows) if(window->Active && (window->Flags&ImGuiWindowFlags_Tooltip)) active_tooltip=true;
    check(!active_tooltip && state.commands.empty(),"hovering Apply with an empty detail opens no empty tooltip");
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
        && contains(text, "Timings (nvtune)") && !contains(text, "WORKSPACE"),
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
                && knobs->ScrollMax.x > 0 && knobs->ScrollMax.y == 0;
        }
    }
    check(columns_match && !active_sidebar, "Classic preserves six-column left/right groups with horizontal overflow instead of compressed controls");
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
    ViewState state; state.preview = false;
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
    ViewState state; state.preview = false;
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
    ViewState state; state.preview = false;
    auto live = snapshot(); state.live = live;
    live->data.xoc_known = live->data.rail_mode_known = live->data.rail_writable_known = true;
    live->data.rail_writable = true;
    const auto base_background = ImGui::GetStyle().Colors[ImGuiCol_WindowBg];
    for (auto presentation : {Presentation::focused, Presentation::classic}) {
        state.presentation = presentation;
        live->data.i2c.status = {};
        state.controls_unlocked = true;
        auto text = frame(state, 1920, 1400);
        const auto green = session_risk(live->data);
        check(contains(text, "GREEN / 0 pts") && workspace_has_background(green.tint.background),
            "known zero-risk unlocked workspace displays GREEN and renders its green background");
        state.controls_unlocked = false;
        text = frame(state, 1920, 1400);
        check(contains(text, "LOCKED") && !workspace_has_background(green.tint.background),
            "locking known zero-risk workspace restores a neutral background");
        live->data.i2c.status.recovery_pending = true;
        live->data.i2c.status.stale = true;
        live->data.telemetry_valid = false;
        live->data.telemetry_error = "Temporary read failure";
        text = frame(state, 1920, 1400);
        const auto red = session_risk(live->data);
        check(contains(text, "RED / 3 pts") && workspace_has_background(red.tint.background),
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
    ImGui::SetWindowFocus(window->Name); frame(state, 1920, 1400, false);
    press_key(state, ImGuiKey_W, false, false, 1920, 1400);
    check(state.curve_draft.draft()[0].delta_khz == 30000, "inline W stages one reported physical bin");
    press_key(state, ImGuiKey_W, true, false, 1920, 1400);
    check(state.curve_draft.draft()[0].delta_khz == 120000, "inline Shift+W stages three physical bins");
    press_key(state, ImGuiKey_D, false, false, 1920, 1400);
    check(state.selected_curve_point == live->data.curve[1].idx, "inline D selects the next point");
    press_key(state, ImGuiKey_A, false, false, 1920, 1400);
    press_key(state, ImGuiKey_S, true, false, 1920, 1400);
    check(state.selected_curve_point == live->data.curve[0].idx && state.curve_draft.draft()[0].delta_khz == 30000,
        "inline A and Shift+S select and edit locally");
    ImGui::SetWindowFocus("Druta workspace"); frame(state, 1920, 1400, false);
    press_key(state, ImGuiKey_W, false, false, 1920, 1400);
    check(state.curve_draft.draft()[0].delta_khz == 60000, "Classic window-wide W stages one bin while inline editor is in the background");

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
    if (presentation == Presentation::classic)
        check(state.curve_draft.draft()[0].delta_khz == 25314, "Classic retains window-wide WASD outside the editor");
    else check(!state.curve_draft.dirty(), "Focused background editor cannot consume WASD");
    ImGui::SetWindowFocus("V/F curve editor###druta_curve_editor");
    frame(state, 1366, 900, false);
    // Start the drag-history check from a clean editor; Classic's preceding
    // window-wide shortcut deliberately created its own separate Undo entry.
    ImGui::ActivateItemByID(workspace_button("Revert local curve"));
    frame(state, 1366, 900, false);
    check(!state.curve_draft.dirty() && !state.curve_draft.can_undo(), "drag gesture fixture starts with cleared local history");

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
    for(const char* label:{"Reset controls","Max it (fan + power + volts + curve)"}) {
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
    ImGui::ActivateItemByID(header_button("Max it (fan + power + volts + curve)")); frame(state,1920,1280);
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
    ImGui::ActivateItemByID(header_button("Max it (fan + power + volts + curve)")); frame(state,1920,1280);
    check(state.commands.empty(),"pending local curve edits block Hold and Max it rather than applying unseen drafts");
    state.curve_window_open=false; live->data.tuning.owned=true; live->data.tuning.recovery=true;
    live->data.connected=false; state.controls_unlocked=false;
    frame(state,1920,1280);
    ImGui::ActivateItemByID(header_button("Release hold (Ctrl+H)")); frame(state,1920,1280);
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::release_hold,
        "owned hold release remains reachable when disconnected, locked and an unapplied draft exists");
    state.commands.clear(); live->busy=true;
    ImGui::ActivateItemByID(header_button("Release hold (Ctrl+H)")); frame(state,1920,1280);
    check(state.commands.empty(),"busy owned hold cannot queue overlapping releases");
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
    ViewState state; state.preview=false; auto live=snapshot(); state.live=live;
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
    ImGui::ActivateItemByID(workspace_button("Apply curve")); frame(state,1920,1280);
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::apply_curve,
        "only explicit Apply queues the staged hard curve request");
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
        nav_id=ImHashStr("  Timings (nvtune)  ",0,root->GetID("legacy-tabs"));
        check(tabs && ImGui::TabBarFindTabByID(tabs,nav_id),"Timings route targets the real registered tab");
    }
    else {
        auto* nav=root;
        for(auto* candidate:ImGui::GetCurrentContext()->Windows)
            if(candidate->ParentWindow==root && candidate->ChildId==root->GetID("navigation")) nav=candidate;
        nav_id=nav->GetID("Timings");
    }
    ImGui::ActivateItemByID(nav_id); const auto text=frame(state,1024,1000);
    check(state.page==Page::timings && contains(text,"Memory timings") && state.commands.empty(),
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
    activate("Locate nvtune...");
    check(state.timing_helper_requested && state.commands.empty(),"Locate requests the native helper dialog without executing a helper in render");
    state.timing_helper_requested=false;
    auto timing=std::make_shared<TimingViewData>(); timing->helper_path=L"C:\\fixture-only\\nvtune.exe";
    live->data.timings=timing; frame(state,1024,1000);
    activate("Read timings");
    check(state.commands.size()==1 && state.commands[0].operation==Command::Operation::timings_read,
        "Read timings is an explicit read command and requires no tuning unlock");
    state.commands.clear(); timing->snapshot.ok=timing->snapshot.top_band=true;
    timing->snapshot.broadcast={{"REG", "DOCUMENTED", 16, 5}};
    timing->snapshot.fields={{"tRCD", "REG", "Documented field",0,3,15}};
    frame(state,1024,1000);
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
    live->fan_duty_stale_mask = live->fan_rpm_stale_mask = 1;
    const auto fan_text = frame(state, 1920, 1400);
    check(contains(fan_text, "1200 RPM (stale)"), "last-good fan readings are explicitly stale independently of count presence bit");
    state.page = Page::daily;
    const auto classic_fan = frame(state, 1920, 1400);
    check(contains(classic_fan, "35 % (stale)"), "Classic live fan reading marks retained duty stale independently of the configured request");
    live->fan_readings_stale = false;
    live->fan_duty_stale_mask = live->fan_rpm_stale_mask = 0;
    const auto fresh_classic_fan = frame(state, 1920, 1400);
    check(!contains(fresh_classic_fan, "35 % (stale)"), "Classic recovered fan duty clears its stale marker");
    state.page = Page::monitor;
    live->data.telemetry.fans[0].has_duty = 0;
    const auto partial_fan = frame(state, 1920, 1400);
    check(contains(partial_fan, "Fan 0 duty") && contains(partial_fan, "-- %") && contains(partial_fan, "1200 RPM"),
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
    offset_page_routing_and_gates();
    shunt_display_and_raw_power_apply();
    weighted_workspace_risk_tint();
    compact_curve_layout();
    draft_behavior();
    explicit_profile_intents_and_stock();
    curve_workspace(Presentation::focused);
    curve_workspace(Presentation::classic);
    inline_curve_input();
    popup_input(Presentation::focused);
    popup_input(Presentation::classic);
    curve_apply_acknowledgement(Presentation::focused);
    curve_apply_acknowledgement(Presentation::classic);
    i2c_panel_wiring(Presentation::focused);
    i2c_panel_wiring(Presentation::classic);
    curve_profile_menu();
    hard_de_flatten_ui();
    hold_shortcut();
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
