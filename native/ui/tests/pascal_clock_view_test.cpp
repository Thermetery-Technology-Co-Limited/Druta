// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "pascal_clock_view.h"
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
#include <string>

using namespace druta::ui;

namespace {
void check(bool value, const char* message) {
    if (!value) {
        std::fprintf(stderr, "%s\n", message);
        std::abort();
    }
}

struct Frame {
    std::string text;
    std::vector<PascalClockEdit> confirmed;
};

Frame frame(PascalClockViewState& state, const PascalClockTable* table, uint64_t generation,
            const char* click = nullptr, bool edit = true, bool apply = true,
            const PascalClockReference& reference = {}) {
    constexpr const char* window = "Pascal clocks test";
    if (click) {
        const auto* w = ImGui::FindWindowByName(window);
        check(w != nullptr, "Missing test window");
        const auto pane = ImHashStr("PascalClockSliders", 0, w->ID);
        ImGui::ActivateItemByID(ImHashStr(click, 0, pane));
    }
    Frame f;
    ImGui::NewFrame();
    ImGui::SetNextWindowSize({1100, 740});
    ImGui::Begin(window);
    ImGui::LogToBuffer();
    draw_pascal_clock_sliders(state, table, generation, edit, apply, f.confirmed, reference);
    f.text = ImGui::GetCurrentContext()->LogBuffer.c_str();
    ImGui::LogFinish();
    ImGui::End();
    ImGui::Render();
    return f;
}

void check_clock_column_widths(const PascalClockTable& clocks, float width, const ImGuiStyle& style) {
    const auto old_style = ImGui::GetStyle();
    ImGui::GetStyle() = style;
    ImGui::NewFrame();
    ImGui::SetNextWindowSize({width, 690});
    ImGui::Begin("Clock column sizing test");
    ImGui::LogToBuffer();
    const auto panel = ImGui::GetID("PascalClockSliders");
    PascalClockViewState state;
    std::vector<PascalClockEdit> confirmed;
    draw_pascal_clock_sliders(state, &clocks, 1, true, true, confirmed, {0, 1800.0});
    const std::string text = ImGui::GetCurrentContext()->LogBuffer.c_str();
    ImGui::LogFinish();
    check(text.find("1764.0 MHz") != std::string::npos, "The narrow layout still shows the estimated ratio frequency");
    const float chrome = 2 * (ImGui::GetFrameHeight() + style.FramePadding.x + style.ItemInnerSpacing.x);
    const auto check_table = [&](const char* id, const char* digits) {
        const auto* table = ImGui::TableFindByID(ImHashStr(id, 0, panel));
        check(table != nullptr, "Clock table is rendered for the selected style");
        const auto& input = table->Columns[2];
        check(input.WorkMaxX - input.WorkMinX - chrome >= ImGui::CalcTextSize(digits).x,
              "The complete numeric value fits beside its two step buttons");
        if (table->Columns[1].WidthGiven < 3 * ImGui::GetFrameHeight())
            std::fprintf(stderr, "%s slider %.0f px in %.0f px (%g DPI, %g px frame)\n", id,
                         table->Columns[1].WidthGiven, table->WorkRect.GetWidth(), style.FontScaleDpi, ImGui::GetFrameHeight());
        check(table->Columns[1].WidthGiven >= 3 * ImGui::GetFrameHeight(),
              "The clock slider keeps usable space when the numeric input is widened");
    };
    check_table("state_ceilings", "1911.5");
    check_table("shared_ratios", "105");
    ImGui::End();
    ImGui::Render();
    ImGui::GetStyle() = old_style;
}
}

int main() {
    ImGui::CreateContext();
    auto& io = ImGui::GetIO();
    io.IniFilename = nullptr;
    io.DisplaySize = {1280, 800};
    io.DeltaTime = 1.0f / 60;
    unsigned char* pixels;
    int width, height;
    io.Fonts->GetTexDataAsRGBA32(&pixels, &width, &height);

    PascalClockViewState state;
    PascalClockTable table;
    table.ok = true;
    table.fields = {{PascalClockDomain::gpc, 8, 280, 270, 400},
                    {PascalClockDomain::gpc, 0, 3823, 1900, 4200},
                    {PascalClockDomain::sys, 0, 3417, 1300, 3600},
                    {PascalClockDomain::xbar, -1, 98, 1, 105}};
    auto first = frame(state, &table, 10);
    check(state.selected_pstate == 0 && first.text.find("SYS / Video") != std::string::npos,
          "Select P0 by default and display shared SYS / Video");
    check(first.text.find("all states") != std::string::npos && first.confirmed.empty(), "Show global scope");

    check(pascal_clock_display_value(0, 3823) == 1911.5 &&
          pascal_clock_raw_value(table.fields[1], 1800.5) == 3601 &&
          pascal_clock_raw_value(table.fields[1], 1800.20) == 3600, "Half-MHz precision");
    check(pascal_clock_display_value(-1, 98) == 98 &&
          pascal_clock_raw_value(table.fields[3], 97.2) == 97 &&
          pascal_clock_raw_value(table.fields[3], 106) == 105, "Whole-percent precision and ratio bounds");

    state.stage(table.fields[3], 106);
    check(state.drafts.empty(), "Refuse a ratio above the table's upper bound");

    state.stage(table.fields[1], 3601);
    state.selected_pstate = 8;
    state.stage(table.fields[0], 279);
    state.stage(table.fields[3], 105);
    auto early = frame(state, &table, 10, "Apply and refresh selected GPU");
    check(early.confirmed.empty() && state.drafts.size() == 3, "Stage changes across states before review");
    auto preview = frame(state, &table, 10, "Review clock changes");
    check(preview.confirmed.empty() && state.reviewing, "Review does not apply");
    check(preview.text.find("P0  GPC maximum   1911.5 to 1800.5 MHz") != std::string::npos &&
          preview.text.find("P8  GPC maximum   140.0 to 139.5 MHz") != std::string::npos &&
          preview.text.find("All states  XBAR ratio   98% to 105%") != std::string::npos,
          "Review includes all states, units and both values");
    auto locked = frame(state, &table, 10, "Apply and refresh selected GPU", true, false);
    check(locked.confirmed.empty() && !state.reviewing && state.drafts.size() == 3, "Locked Apply retains drafts");
    frame(state, &table, 10, "Review clock changes");
    state.stage(table.fields[1], 3603);
    check(!state.reviewing && state.drafts.size() == 3, "Updating a draft requires new review");
    frame(state, &table, 10, "Review clock changes");
    auto applied = frame(state, &table, 10, "Apply and refresh selected GPU");
    check(applied.confirmed.size() == 3 && applied.confirmed[0].value == 3603 &&
          applied.confirmed[2].pstate == -1 && applied.confirmed[2].value == 105 && state.drafts.empty(),
          "Confirm the reviewed batch once");

    state.stage(table.fields[1], 3601);
    auto stale = table;
    stale.fields[1].current = 3820;
    frame(state, &table, 10, "Review clock changes");
    auto mismatch = frame(state, &stale, 10, "Apply and refresh selected GPU");
    check(mismatch.confirmed.empty() && !state.reviewing && state.drafts.size() == 1, "Refuse a stale preimage");
    frame(state, nullptr, 10);
    check(state.drafts.size() == 1, "A transient read keeps local drafts");
    frame(state, &stale, 10, "Discard drafts");
    check(state.drafts.empty(), "Discard stale drafts");

    state.stage(table.fields[1], 3601);
    state.stage(table.fields[1], table.fields[1].current);
    check(state.drafts.empty(), "Returning a slider to the read value removes its draft");
    state.stage(table.fields[1], 3601);
    frame(state, &table, 10, "Review clock changes");
    auto switched = frame(state, &table, 11, "Apply and refresh selected GPU");
    check(switched.confirmed.empty() && state.drafts.empty() && !state.reviewing && state.selected_pstate == 0,
          "A new adapter generation cannot apply previous drafts");
    PascalClockTable partial;
    partial.ok = true;
    partial.fields = {table.fields[0]};
    frame(state, &partial, 11);
    check(state.selected_pstate == 8, "Accept an adapter whose only shown state is P8");
    PascalClockViewState ratio_only_state;
    partial.fields = {table.fields[3]};
    auto ratio_only = frame(ratio_only_state, &partial, 12, nullptr, true, true, {8, 250.0});
    check(ratio_only.text.find("245.0 MHz") != std::string::npos &&
          ratio_only.text.find("Target estimates for P8") != std::string::npos,
          "A ratio-only layout estimates from the live state when no state selector is available");
    partial.fields = {{PascalClockDomain::gpc, 0, 3200, 3200, 3200}};
    auto fixed = frame(ratio_only_state, &partial, 12);
    check(!has_writable_pascal_clock(&partial, PascalClockDomain::gpc) && fixed.text.find("##slider") == std::string::npos &&
          fixed.text.find("Maximum") == std::string::npos, "Hide a fixed field instead of presenting a dead slider");
    ImGuiStyle focused;
    focused.FramePadding = {10, 7};
    focused.ItemInnerSpacing = {8, 6};
    check_clock_column_widths(table, 500, focused);
    ImGuiStyle classic;
    classic.ScaleAllSizes(1.5f);
    classic.FontScaleDpi = 1.5f;
    check_clock_column_widths(table, 500, classic);
    ImGui::DestroyContext();
    std::puts("Pascal clock slider view passed");
}
