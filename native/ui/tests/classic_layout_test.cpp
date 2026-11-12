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
void layout(float width, float dpi, bool expect_scroll) {
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
    auto* knobs = child("classic-knob-columns");
    check(knobs != nullptr, "Classic columns have a dedicated horizontal overflow region");
    if (!knobs) return;
    check((knobs->ScrollMax.x > 1) == expect_scroll, "horizontal overflow follows available width");
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
        for (int col = 0; col < 6; ++col) {
            check(std::abs(t->Columns[col].WidthGiven - classic_knob_widths[col] * dpi) <= 1.1f,
                "ordinary and advanced tables retain released column widths at this DPI");
        }
        if (t->RowPosY2 - t->RowPosY1 > 2 * 16 * dpi) wrapped = true;
    }
    check(tables >= 5, "ordinary, domain, limits and rail tables share geometry");
    check(wrapped, "long adapter-derived labels increase row height rather than clipping");
    check(state.commands.empty(), "layout changes dispatch no backend commands");
    check(state.advanced_view.drafts.size() >= 2, "partial fixture retains independent domain and rail drafts");
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
    layout(1900, 1, false); layout(1024, 1, true); layout(1900, 1.5f, true);
    ImPlot::DestroyContext(); ImGui::DestroyContext();
    std::printf("Classic geometry: %d failures\n", failures);
    return failures ? 1 : 0;
}
