/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
// Headless display contract: no Session, backend, DLL, GPU or file I/O.
#include "shunt_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
using namespace druta::ui;
namespace {
unsigned checks = 0;
#define CHECK(expr) do { ++checks; if (!(expr)) { std::fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #expr); std::abort(); } } while (false)
std::string frame(ShuntViewState& state, bool stale = false, bool activate_save = false,
    ImVec2 display = {1400, 1200}, float font_scale = 1.0f, const char* activate_helper = nullptr) {
    ImGui::GetIO().DisplaySize = display;
    ImGui::GetIO().FontGlobalScale = font_scale;
    ImGui::NewFrame();
    ImGui::SetNextWindowPos({0, 0}); ImGui::SetNextWindowSize({600, 300});
    ImGui::Begin("Power summary"); ImGui::LogToBuffer();
    draw_shunt_power_summary(state, 100.0, 200.0, std::nullopt, stale);
    if (activate_save) {
        auto* window = ImGui::FindWindowByName("Shunt mod");
        CHECK(window != nullptr);
        auto& context = *ImGui::GetCurrentContext();
        context.NavActivateId = context.NavActivateDownId = context.NavActivatePressedId = window->GetID("Save");
    }
    if (activate_helper) {
        auto* popup = ImGui::FindWindowByName("Shunt mod");
        CHECK(popup != nullptr);
        ImGuiWindow* body = nullptr;
        for (auto* window : ImGui::GetCurrentContext()->Windows)
            if (window->ParentWindow == popup && window->ChildId == popup->GetID("shunt-body")) body = window;
        CHECK(body != nullptr);
        body->Scroll.y = body->ScrollMax.y;
        auto& context = *ImGui::GetCurrentContext();
        context.NavActivateId = context.NavActivateDownId = context.NavActivatePressedId = body->GetID(activate_helper);
    }
    draw_shunt_window(state);
    const std::string result = ImGui::GetCurrentContext()->LogBuffer.c_str();
    ImGui::LogFinish(); ImGui::End(); ImGui::Render();
    return result;
}
}
int main() {
    ImGui::CreateContext(); auto& io = ImGui::GetIO();
    io.IniFilename = nullptr; io.DeltaTime = 1.0f / 60.0f;
    unsigned char* pixels; int width, height;
    io.Fonts->AddFontDefault(); io.Fonts->GetTexDataAsRGBA32(&pixels, &width, &height);
    ShuntViewState state;
    CHECK(!state.open && !state.save_requested && !state.dirty);
    state.open = true;
    state.parallel_resistors.assign(8, 5.0);
    frame(state, false, false, {1024, 768}, 1.5f);
    frame(state, false, true, {1024, 768}, 1.5f);
    auto* popup = ImGui::FindWindowByName("Shunt mod");
    CHECK(popup != nullptr && ImGui::GetCurrentContext()->NavId == popup->GetID("Save"));
    ImRect save_rect = popup->NavRectRel[ImGuiNavLayer_Main];
    save_rect.Translate(popup->Pos);
    CHECK(save_rect.GetWidth() > 0 && save_rect.GetHeight() > 0);
    CHECK(popup->ClipRect.Contains(save_rect));
    CHECK(popup->Pos.x >= 0 && popup->Pos.y >= 0 && popup->Pos.x + popup->Size.x <= 1024 && popup->Pos.y + popup->Size.y <= 768);
    bool body_scrolls = false;
    for (auto* window : ImGui::GetCurrentContext()->Windows)
        if (window->ParentWindow == popup && window->ChildId == popup->GetID("shunt-body")) body_scrolls = window->ScrollMax.y > 0;
    CHECK(body_scrolls && take_shunt_save_request(state));
    state.parallel_resistors = {5, 5};
    auto text = frame(state);
    CHECK(text.find("editable templates") != std::string::npos);
    CHECK(text.find("No mod: driver-reported power") != std::string::npos);
    CHECK(text.find("Enforced limit: unavailable") != std::string::npos);
    CHECK(!take_shunt_save_request(state));
    state.config.rails[0].extra["future"] = "keep";
    for (std::size_t i = 0; i < state.config.rails.size(); ++i) CHECK(edit_shunt_rail(state, i, 5.0, 2.5));
    CHECK(state.dirty && state.correction.exact && state.correction.factor == 2);
    text = frame(state, true);
    CHECK(text.find("Uniform correction") != std::string::npos);
    CHECK(text.find("corrected 200.0 W") != std::string::npos);
    CHECK(text.find("telemetry is stale") != std::string::npos);
    CHECK(!take_shunt_save_request(state));
    CHECK(edit_shunt_rail(state, 0, 5, 5));
    text = frame(state);
    CHECK(text.find("Mixed-rail ESTIMATE") != std::string::npos);
    CHECK(text.find("ESTIMATE ") != std::string::npos);
    CHECK(!state.correction.exact);
    CHECK(edit_shunt_rail(state, 0, 5, 0));
    CHECK(!state.correction.valid && !request_shunt_save(state));
    frame(state, false, true);
    CHECK(!take_shunt_save_request(state));
    text = frame(state);
    CHECK(text.find("Correction unavailable") != std::string::npos);
    CHECK(text.find("corrected 200.0 W") == std::string::npos);
    CHECK(edit_shunt_rail(state, 0, 5, std::numeric_limits<double>::infinity()));
    CHECK(!state.correction.valid && !request_shunt_save(state));
    reset_shunt_no_mod(state);
    CHECK(state.config.rails.size() == 3 && state.config.rails[0].extra["future"] == "keep");
    CHECK(state.correction.valid && !state.correction.active);
    CHECK(add_shunt_rail(state, "pin6"));
    CHECK(!add_shunt_rail(state, "invented") && state.config.rails.size() == 4);
    CHECK(remove_shunt_rail(state, 1));
    reset_shunt_no_mod(state);
    CHECK(state.config.rails.size() == 3 && state.config.rails[1].kind == "pin8" && state.config.rails[2].kind == "pin6");
    const auto factor = state.correction.factor;
    state.parallel_resistors = {5, 5, 5};
    text = frame(state);
    CHECK(text.find("Equivalent: 1.667 mOhm") != std::string::npos);
    CHECK(state.config.rails[0].mod == 5 && state.correction.factor == factor && !take_shunt_save_request(state));
    state.parallel_resistors[0] = 0;
    text = frame(state);
    CHECK(text.find("Enter positive, finite resistance values") != std::string::npos);
    CHECK(state.correction.valid);
    frame(state, false, true);
    CHECK(take_shunt_save_request(state)); CHECK(!take_shunt_save_request(state));
    state.config.load_error = "read failed";
    CHECK(!request_shunt_save(state));
    text = frame(state);
    CHECK(text.find("Save unavailable: read failed") != std::string::npos && !take_shunt_save_request(state));
    ShuntConfig unknown; unknown.rails = {{"future-connector", 5, 2.5}, {"pin8", 5, 5}};
    unknown.rails[0].extra["opaque"] = "preserve";
    set_shunt_config(state, unknown);
    CHECK(!state.dirty && !state.correction.valid);
    text = frame(state);
    CHECK(text.find("future-connector") != std::string::npos && !take_shunt_save_request(state));
    CHECK(!edit_shunt_rail(state, 0, 10, 1) && !remove_shunt_rail(state, 0));
    CHECK(edit_shunt_rail(state, 1, 5, 2.5));
    CHECK(!state.correction.valid && state.config.rails[0].mod == 2.5);
    text = frame(state);
    CHECK(text.find("Correction unavailable") != std::string::npos && !take_shunt_save_request(state));
    frame(state, false, true);
    CHECK(take_shunt_save_request(state) && !state.correction.valid);
    reset_shunt_no_mod(state);
    CHECK(state.config.rails[0].mod == 2.5 && state.config.rails[0].extra["opaque"] == "preserve");
    CHECK(state.config.rails[1].mod == 5 && request_shunt_save(state));
    CHECK(take_shunt_save_request(state));
    CHECK(edit_shunt_rail(state, 1, 5, 0) && !request_shunt_save(state));
    frame(state, false, true);
    CHECK(!take_shunt_save_request(state));
    CHECK(edit_shunt_rail(state, 1, std::numeric_limits<double>::max(), std::numeric_limits<double>::min()));
    CHECK(!request_shunt_save(state));
    CHECK(edit_shunt_rail(state, 1, 5, 5));
    state.config.load_error = "unknown document read failure";
    CHECK(!request_shunt_save(state));
    ShuntConfig mixed;
    mixed.rails = {{"slot", 4, 4}, {"pin6", 6, 6}, {"pin8", 8, 8}, {"future", 9, 7}};
    mixed.rails[0].extra["known-extra"] = "keep";
    mixed.rails[3].extra["opaque"] = "preserve";
    mixed.document["future"] = "document";
    set_shunt_config(state, mixed);
    state.parallel_resistors = {5, 8};
    std::string clipboard = "unchanged";
    io.SetClipboardTextFn = [](void* user, const char* value) { *static_cast<std::string*>(user) = value; };
    io.ClipboardUserData = &clipboard;
    frame(state);
    frame(state, false, false, {1400, 1200}, 1.0f, "Copy value");
    CHECK(clipboard == "3.07692308" && !state.dirty && !take_shunt_save_request(state));
    frame(state, false, false, {1400, 1200}, 1.0f, "Use for all rails");
    CHECK(state.dirty && !take_shunt_save_request(state));
    for (std::size_t i = 0; i < 3; ++i) {
        CHECK(std::abs(state.config.rails[i].mod - 40.0 / 13.0) < 1e-12);
        CHECK(state.config.rails[i].orig == mixed.rails[i].orig && state.config.rails[i].kind == mixed.rails[i].kind);
    }
    CHECK(state.config.rails[0].extra["known-extra"] == "keep");
    CHECK(state.config.rails[3].mod == 7 && state.config.rails[3].orig == 9 && state.config.rails[3].extra["opaque"] == "preserve");
    CHECK(state.config.document["future"] == "document");
    state.dirty = false;
    frame(state, false, false, {1400, 1200}, 1.0f, "Use for all rails");
    CHECK(!state.dirty && !take_shunt_save_request(state));
    state.parallel_resistors = {5, 0};
    clipboard = "unchanged";
    frame(state, false, false, {1400, 1200}, 1.0f, "Copy value");
    frame(state, false, false, {1400, 1200}, 1.0f, "Use for all rails");
    CHECK(clipboard == "unchanged" && !state.dirty && !take_shunt_save_request(state));
    CHECK(std::abs(state.config.rails[0].mod - 40.0 / 13.0) < 1e-12);
    ShuntConfig only_unknown; only_unknown.rails = {{"future", 5, 5}};
    set_shunt_config(state, only_unknown);
    state.parallel_resistors = {5, 8};
    frame(state, false, false, {1400, 1200}, 1.0f, "Use for all rails");
    CHECK(!state.dirty && state.config.rails[0].mod == 5 && !take_shunt_save_request(state));
    set_shunt_config(state, ShuntConfig{});
    CHECK(!request_shunt_save(state));
    ImGui::DestroyContext();
    std::printf("%u shunt view checks passed\n", checks);
}
