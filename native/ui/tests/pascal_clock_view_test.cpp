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
            const char* click = nullptr, bool edit = true, bool apply = true) {
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
    draw_pascal_clock_sliders(state, table, generation, edit, apply, f.confirmed);
    f.text = ImGui::GetCurrentContext()->LogBuffer.c_str();
    ImGui::LogFinish();
    ImGui::End();
    ImGui::Render();
    return f;
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
                    {PascalClockDomain::xbar, -1, 98, 1, 100}};
    auto first = frame(state, &table, 10);
    check(state.selected_pstate == 0 && first.text.find("SYS / Video") != std::string::npos,
          "Select P0 by default and display shared SYS / Video");
    check(first.text.find("all states") != std::string::npos && first.confirmed.empty(), "Show global scope");

    check(pascal_clock_display_value(0, 3823) == 1911.5 &&
          pascal_clock_raw_value(table.fields[1], 1800.5) == 3601 &&
          pascal_clock_raw_value(table.fields[1], 1800.20) == 3600, "Half-MHz precision");
    check(pascal_clock_display_value(-1, 98) == 98 &&
          pascal_clock_raw_value(table.fields[3], 97.2) == 97, "Whole-percent precision");

    state.stage(table.fields[1], 3601);
    state.selected_pstate = 8;
    state.stage(table.fields[0], 279);
    state.stage(table.fields[3], 97);
    auto early = frame(state, &table, 10, "Apply and refresh selected GPU");
    check(early.confirmed.empty() && state.drafts.size() == 3, "Stage changes across states before review");
    auto preview = frame(state, &table, 10, "Review clock changes");
    check(preview.confirmed.empty() && state.reviewing, "Review does not apply");
    check(preview.text.find("P0  GPC maximum   1911.5 to 1800.5 MHz") != std::string::npos &&
          preview.text.find("P8  GPC maximum   140.0 to 139.5 MHz") != std::string::npos &&
          preview.text.find("All states  XBAR ratio   98% to 97%") != std::string::npos,
          "Review includes all states, units and both values");
    auto locked = frame(state, &table, 10, "Apply and refresh selected GPU", true, false);
    check(locked.confirmed.empty() && !state.reviewing && state.drafts.size() == 3, "Locked Apply retains drafts");
    frame(state, &table, 10, "Review clock changes");
    state.stage(table.fields[1], 3603);
    check(!state.reviewing && state.drafts.size() == 3, "Updating a draft requires new review");
    frame(state, &table, 10, "Review clock changes");
    auto applied = frame(state, &table, 10, "Apply and refresh selected GPU");
    check(applied.confirmed.size() == 3 && applied.confirmed[0].value == 3603 &&
          applied.confirmed[2].pstate == -1 && state.drafts.empty(), "Confirm the reviewed batch once");

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
    ImGui::DestroyContext();
    std::puts("Pascal clock slider view passed");
}
