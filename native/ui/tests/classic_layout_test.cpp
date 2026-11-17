// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Headless production-view geometry checks. No Session/backend/GPU is opened.
#include "ui_view.h"
#include "session.h"
#include "classic_layout.h"
#include "imgui_internal.h"
#include "implot.h"
#include <cmath>
#include <cstdio>

namespace {
using namespace druta::ui;
int failures = 0;
void check(bool ok, const char* message) {
    if (!ok) { ++failures; std::printf("FAIL: %s\n", message); }
}
ImGuiWindow* child(const char* id) {
    for (auto* w : ImGui::GetCurrentContext()->Windows)
        if (w->ParentWindow && w->ChildId == w->ParentWindow->GetID(id)) return w;
    return nullptr;
}
void frame(ViewState& state, float width, float dpi) {
    ImGui::GetIO().DisplaySize = {width, 1400};
    ImGui::GetStyle().FontScaleDpi = dpi;
    ImGui::NewFrame(); draw(state); ImGui::Render();
}
// Python build_control: no horizontal scrollbar at any width. Both columns
// keep the released widths when they fit; otherwise the right one is narrowed
// to the window and its cells clip.
void layout(float width, float dpi, bool both_fit) {
    auto s = std::make_shared<SessionSnapshot>();
    s->generation = 1; s->data.connected = true;
    for (auto& c : s->data.controls) {
        c.availability = Availability::available;
        c.current = 12; c.low = -100; c.high = 100;
        c.writable = c.stock_writable = true; c.unit = "MHz";
    }
    s->data.advanced_controls.push_back({{AdvancedKind::clock_domain, 7},
        "Additional memory clock control with an adapter-derived label that must wrap onto several lines",
        s->data.controls[0], "alternate-domain-seven"});
    s->data.rail_controls.push_back({{0, 0}, s->data.controls[0], 950, "alternate-rail-zero"});
    s->log.push_back({1, "Selected request readback retained", true});
    ViewState state; state.preview = false; state.presentation = Presentation::classic; state.live = s;
    for (int n = 0; n < 5; ++n) frame(state, width, dpi);
    auto* root = ImGui::FindWindowByName("Druta workspace");
    check(root && std::abs(root->WindowPadding.x - 8 * dpi) <= 0.6f && std::abs(root->WindowPadding.y - 8 * dpi) <= 0.6f,
        "Classic workspace uses stock ImGui window padding");
    if (root) {
        check(std::abs(root->MenuBarHeight - (16 * dpi + 2 * 3 * dpi)) <= 1.1f,
            "Classic menu bar uses stock frame padding");
        auto* header = ImGui::TableFindByID(root->GetID("classic-header"));
        check(header != nullptr && header->ColumnsCount == 2, "Classic header is a two-column identity / recovery table");
        if (header) {
            check(header->RowPosY2 - header->RowPosY1 >= 52 * dpi - 0.5f, "header row holds the 52 px panic button");
            check(header->Columns[1].MaxX >= header->OuterRect.Max.x - 1 && header->Columns[1].WidthGiven < 360 * dpi,
                "panic column sits at the right edge and is sized to its label");
            check(header->Columns[0].WidthGiven > header->Columns[1].WidthGiven, "identity column takes the remaining width");
            // Dear PyGui 2.3.1 rebuild of the 1.9.1 header: table at y 36, row 36-124
            // (title line 32, details and empty status lines 22 each, as DPG gives
            // every text a frame-height line), so the gate row starts at y 128.
            check(std::abs(header->OuterRect.Min.y - 36 * dpi) <= 1.1f, "header starts below the menu bar at the released offset");
            if (width >= 1900) check(std::abs((header->RowPosY2 - header->RowPosY1) - 88 * dpi) <= 1.6f,
                "inline header row matches the released title, details and status line heights");
        }
    }
    auto* knobs = child("classic-knob-columns");
    check(knobs != nullptr, "Classic columns have a content-sized region");
    if (!knobs) return;
    // ImGui still records the fixed columns' fit width as content (as it does for
    // Python's page child), but without the scrollbar flag none is drawn.
    check(!knobs->ScrollbarX && !(knobs->Flags & ImGuiWindowFlags_HorizontalScrollbar),
        "the knob columns never show a horizontal scrollbar");
    check(both_fit == (knobs->ScrollMax.x < 1), "only a window too narrow for both columns has clipped content");
    auto* columns = ImGui::TableFindByID(knobs->GetID("classic-control-columns"));
    check(columns && columns->ColumnsCount == 2, "Clocks/limits and Rails are one two-column table");
    if (columns) {
        check(std::abs(columns->Columns[0].WidthGiven - classic_column_width * dpi) <= 1.1f,
            "the left column keeps the released knob column width");
        check((std::abs(columns->Columns[1].WidthGiven - classic_column_width * dpi) <= 1.1f) == both_fit,
            "the right column is narrowed only when the window cannot hold both");
        check(columns->Columns[1].WorkMaxX <= knobs->InnerRect.Max.x + 1 && columns->OuterRect.Max.x <= knobs->InnerRect.Max.x + 1,
            "the right column's cells end inside the window");
    }
    check(knobs->ScrollMax.y < 1, "knob region fits its content without a second vertical scrollbar");
    const float settled_height = knobs->Size.y;
    frame(state, width, dpi);
    check(std::abs(knobs->Size.y - settled_height) < 1, "auto height settles without growing every frame");
    auto* curve = child("inline-vf-editor");
    check(curve && curve->Pos.y >= knobs->Pos.y + knobs->Size.y,
        "integrated curve stays below the content-sized knob region");
    int tables = 0; bool wrapped = false;
    auto& pool = ImGui::GetCurrentContext()->Tables;
    for (int n = 0; n < pool.GetBufSize(); ++n) {
        auto* t = pool.GetByIndex(n);
        if (t->OuterWindow != knobs || t->ColumnsCount != 6) continue;
        ++tables;
        check(t->Columns[5].WorkMaxX <= knobs->InnerRect.Max.x + 1 && t->OuterRect.Max.x <= knobs->InnerRect.Max.x + 1,
            "knob tables are narrowed to the window, never past it");
        // A right-column table in a narrowed column is narrowed further, as in Python.
        const bool left = columns && t->OuterRect.Min.x < columns->Columns[1].MinX;
        if (!left && !both_fit) continue;
        // Released 1.9.1 runs stock CellPadding (4,2) inside a 754 px column, so
        // ImGui keeps the first five widths and narrows only the last column to fit.
        for (int col = 0; col < 5; ++col) {
            check(std::abs(t->Columns[col].WidthGiven - classic_knob_widths[col] * dpi) <= 1.1f,
                "ordinary and advanced tables retain released column widths at this DPI");
        }
        check(t->Columns[5].WidthGiven > 0 && t->Columns[5].WidthGiven <= classic_knob_widths[5] * dpi,
            "last knob column is narrowed, not dropped, by stock cell padding");
        check(t->Columns[5].WorkMaxX <= t->OuterRect.Min.x + classic_column_width * dpi + 1.1f,
            "knob tables stay inside the released column width");
        check(std::abs(t->CellSpacingX1 - 4 * dpi) <= 0.6f, "Classic tables use stock ImGui cell padding");
        if (t->RowPosY2 - t->RowPosY1 > 2 * 16 * dpi) wrapped = true;
    }
    // Clock offsets (core, memory and the per-domain rows in one table), Limits
    // (power, current, boost, fan) and Rails, as Python builds them.
    check(tables >= 3, "ordinary, domain, limits and rail tables share geometry");
    check(wrapped, "long adapter-derived labels increase row height rather than clipping");
    check(state.commands.empty(), "layout changes dispatch no backend commands");
    check(state.advanced_view.drafts.size() >= 2, "partial fixture retains independent domain and rail drafts");
}
// Released 1.9.1 gate row: title, details and empty status lines, then the
// 28 px gate row at y 128 and the tab bar directly under it (no profile row,
// no rule); the rail badge describes the current adapter's partial rails.
void gate_row(float dpi) {
    auto s = std::make_shared<SessionSnapshot>();
    s->generation = 4; s->data.connected = true; s->data.reset_controls_available = s->data.max_it_available = true;
    ControlState rail; rail.availability = Availability::available; rail.current = 5; rail.stock = 0; rail.detail = "fixture";
    s->data.rail_controls.push_back({{1, 2}, rail, 640.5, "alternate-msvdd-overvoltage"});
    s->data.rail_writable_known = true; s->data.rail_writable = true;
    // An adapter whose mode states are read: risk score zero, so no banner.
    s->data.xoc_known = s->data.rail_mode_known = true;
    ViewState state; state.preview = false; state.presentation = Presentation::classic; state.live = s; state.controls_unlocked = true;
    for (int n = 0; n < 3; ++n) frame(state, 1900, dpi);
    auto* root = ImGui::FindWindowByName("Druta workspace");
    ImGuiTabBar* tabs = nullptr;
    auto& bars = ImGui::GetCurrentContext()->TabBars;
    for (int n = 0; n < bars.GetMapSize(); ++n) if (auto* bar = bars.TryGetMapData(n)) if (root && bar->ID == root->GetID("legacy-tabs")) tabs = bar;
    check(tabs != nullptr, "Classic tab bar is drawn in the workspace");
    // Header 36 + 88, 4 px spacing, 28 px gate row, 4 px spacing.
    if (tabs) check(std::abs(tabs->BarRect.Min.y - 160 * dpi) <= 1.6f, "tabs sit directly under the 28 px gate row at the released offset");
    // 1.9.1 risk_banner sits above the gate row only while the score is above
    // zero: one text item on a frame-height line, wrapped at s(sum(KNOB_COLS)) =
    // 720 px, pushing the row and tabs down by its wrapped height. XOC alone
    // gives 1.9.1's AMBER and XOC sentences, several lines at that width.
    s->data.xoc_enabled = true;
    for (int n = 0; n < 2; ++n) frame(state, 1900, dpi);
    {
        const char* banner = "YELLOW (risk 1) - A software envelope has been removed. Requests still go through the driver, so the GPU's own "
            "reliability ceiling is still underneath you.  XOC: expanded voltage and clock ranges; current limits use the API ceilings. "
            "Supported GPUs also expose live extended power requests; the 1999 W software request cap does not raise firmware limits. "
            "Native policy limits can wrap at high requests.";
        ImGui::NewFrame(); ImGui::PushFont(nullptr, 16.0f);
        const float wrapped = ImGui::CalcTextSize(banner, nullptr, false, 720 * dpi).y;
        ImGui::PopFont(); ImGui::EndFrame();
        const float line = std::max(22 * dpi, wrapped + 3 * dpi) + 4 * dpi;
        check(wrapped >= 3 * 16 * dpi - 0.5f, "the banner text wraps onto several lines at 720 px");
        if (tabs) check(std::abs(tabs->BarRect.Min.y - (160 * dpi + line)) <= 1.6f, "a risk banner wrapped at 720 px sits above the gate row");
    }
    s->data.xoc_enabled = false;
    for (int n = 0; n < 2; ++n) frame(state, 1900, dpi);
    if (tabs) check(std::abs(tabs->BarRect.Min.y - 160 * dpi) <= 1.6f, "the banner goes with the score");
    auto badge = rail_limit_badge(*s);
    check(badge.state == RailLimitBadge::State::ready && badge.text == "Rail limits: ready"
        && badge.detail == "Rail limits ready for MSVDD. NVVDD unavailable",
        "a second-rail-only adapter reports MSVDD ready and names the missing rail");
    const auto info = rail_limit_diagnostics(*s);
    check(info.find("\"MSVDD\"") != std::string::npos && info.find("640.5") != std::string::npos
        && info.find("NVVDD") != std::string::npos && info.find("\"available\": true") != std::string::npos,
        "Copy info carries the adapter's rail read and verdict");
    s->data.rail_writable = false;
    check(rail_limit_badge(*s).state == RailLimitBadge::State::unavailable, "a known non-writable rail block reads unavailable");
    s->data.rail_writable_known = false;
    badge = rail_limit_badge(*s);
    check(badge.state == RailLimitBadge::State::unavailable && badge.detail.find("Retry") != std::string::npos,
        "an unestablished rail read offers Retry instead of claiming support");
    s->opening = true;
    check(rail_limit_badge(*s).state == RailLimitBadge::State::checking, "an opening adapter is still being checked");
    s->opening = false; s->data.rail_controls.clear(); s->data.rail_writable_known = true; s->data.rail_writable = true;
    check(rail_limit_badge(*s).state == RailLimitBadge::State::unavailable, "no rail controls is never reported ready");
    check(state.commands.empty(), "gate row rendering dispatches nothing");
}
// Released 1.9.1 gate row spacers inside a horizontal group (8 px stock item
// spacing either side): 24 px after Unlock controls, 16 before Reset controls
// and 12 before Max it, with the 150 x 28 and 290 x 28 buttons. Measured by
// hit-testing the row, so the label widths of the test font do not matter.
void gate_row_spacing(float dpi) {
    auto s = std::make_shared<SessionSnapshot>();
    s->generation = 6; s->data.connected = true; s->data.reset_controls_available = s->data.max_it_available = true;
    s->data.xoc_known = s->data.rail_mode_known = s->data.rail_writable_known = true;
    ViewState state; state.preview = false; state.presentation = Presentation::classic; state.live = s; state.controls_unlocked = true;
    for (int n = 0; n < 3; ++n) frame(state, 1900, dpi);
    auto* root = ImGui::FindWindowByName("Druta workspace");
    if (!root) { check(false, "workspace exists for the gate row scan"); return; }
    struct Span { float first = -1, last = -1; };
    const char* labels[]{"Unlock controls", "XOC", "Rail limits", "Reset controls", "Max it  (fan + power + volts + curve)"};
    Span spans[5];
    auto& io = ImGui::GetIO();
    const float y = (128 + 10) * dpi;
    for (float x = 0; x < 1000 * dpi; x += 1) {
        io.AddMousePosEvent(x, y); frame(state, 1900, dpi);
        const ImGuiID hovered = ImGui::GetCurrentContext()->HoveredId;
        for (int i = 0; i < 5; ++i) if (hovered && hovered == root->GetID(labels[i])) {
            if (spans[i].first < 0) spans[i].first = x;
            spans[i].last = x;
        }
    }
    bool found = true;
    for (const auto& span : spans) found = found && span.first >= 0;
    check(found, "every gate row item is hit on the row");
    if (!found) { io.AddMousePosEvent(-FLT_MAX, -FLT_MAX); frame(state, 1900, dpi); return; }
    // 1.9.1's Unlock and Max it tooltips, shown while the item is hovered.
    const auto hover_text = [&](float x) {
        io.AddMousePosEvent(x, y); ImGui::GetIO().DisplaySize = {1900, 1400}; ImGui::GetStyle().FontScaleDpi = dpi;
        ImGui::NewFrame(); ImGui::LogToBuffer(0); draw(state);
        std::string text = ImGui::GetCurrentContext()->LogBuffer.c_str(); ImGui::LogFinish(); ImGui::Render(); return text;
    };
    auto text = hover_text((spans[0].first + spans[0].last) / 2);
    check(text.find("Writes are enabled while this is ticked; untick for read-only.") != std::string::npos
        && text.find("I2C changes can persist through a reboot: use Reset or power off.") != std::string::npos,
        "Unlock controls carries 1.9.1's tooltip");
    text = hover_text((spans[4].first + spans[4].last) / 2);
    check(text.find("The four things done by hand at the start of every") != std::string::npos
        && text.find("Undo last write puts them back.") != std::string::npos
        && text.find("UNDO RELEASES THE HOLD FIRST.") != std::string::npos,
        "Max it carries 1.9.1's tooltip, worded for native Undo and hold release");
    io.AddMousePosEvent(-FLT_MAX, -FLT_MAX); frame(state, 1900, dpi);
    const auto gap = [&](int a, int b) { return spans[b].first - (spans[a].last + 1); };
    const auto width = [&](int i) { return spans[i].last + 1 - spans[i].first; };
    check(std::abs(gap(0, 1) - 40 * dpi) <= 1.6f, "Unlock controls is followed by 1.9.1's 24 px spacer");
    check(std::abs(gap(2, 3) - 32 * dpi) <= 1.6f, "Reset controls follows a 16 px spacer at this DPI");
    check(std::abs(gap(3, 4) - 28 * dpi) <= 1.6f, "Max it follows a 12 px spacer");
    check(std::abs(width(3) - 150 * dpi) <= 1.6f && std::abs(width(4) - 290 * dpi) <= 1.6f, "Reset and Max it keep 1.9.1's widths");
    check(state.commands.empty(), "hovering the gate row dispatches nothing");
}
// Released 1.9.1 Druta.run default viewport (outer window) size and Dear
// PyGui's default viewport position.
void default_window_placement() {
    const auto a = classic_default_window_placement(1, 2560, 1440);
    check(a.width == 1544 && a.height == 1300 && a.x == 100 && a.y == 100, "a large 96 DPI screen gets the released 1544 x 1300 viewport at 100, 100");
    const auto b = classic_default_window_placement(1, 1366, 768);
    check(b.width == 1366 && b.height == 698 && b.x == 0 && b.y == 70,
        "a short screen keeps 70 px free below the height, and the width and position on screen");
    const auto c = classic_default_window_placement(1.5f, 3840, 2160);
    check(c.width == 2316 && c.height == 1950 && c.x == 100 && c.y == 100, "the size scales with the display; the position does not");
    const auto d = classic_default_window_placement(1, 1024, 600);
    check(d.width == 1024 && d.height == 600 && d.x == 0 && d.y == 0, "the 600 px floor never exceeds the screen");
    const auto e = classic_default_window_placement(1.25f, 0, 0);
    check(e.width == 1930 && e.height == 1625 && e.x == 100 && e.y == 100, "an unreadable screen size leaves the scaled default");
}
}
int main() {
    ImGui::CreateContext(); ImPlot::CreateContext();
    auto& io = ImGui::GetIO(); io.IniFilename = nullptr; io.DeltaTime = 1.0f / 60;
    ImFontConfig config; config.SizePixels = 18; io.Fonts->AddFontDefault(&config);
    unsigned char* pixels = nullptr; int width = 0, height = 0;
    io.Fonts->GetTexDataAsRGBA32(&pixels, &width, &height);
    apply_theme(1);
    // No host fonts: exercising fallback is intentional.
    layout(1900, 1, true); layout(1024, 1, false); layout(1900, 1.5f, false);
    gate_row(1); gate_row(1.5f); gate_row_spacing(1); gate_row_spacing(1.5f); default_window_placement();
    ImPlot::DestroyContext(); ImGui::DestroyContext();
    std::printf("Classic geometry: %d failures\n", failures);
    return failures ? 1 : 0;
}
