// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_catalog_view.h"
#include <imgui.h>
#include <algorithm>
#include <stdexcept>
#include <string_view>
namespace druta::ui {
namespace {
void invalidate(ProfileCatalogViewState& state){state.staged.reset();state.preflight_ok=false;state.status="Selection changed; compose and stage to review";}
}
void sync_profile_catalog_generation(ProfileCatalogViewState& state,uint64_t generation){
 if(!state.initialized||state.generation!=generation){state.generation=generation;state.initialized=true;state.staged.reset();state.preflight_ok=false;
  for(auto& item:state.items)item.included=false;state.status.clear();}
}
void add_profile_catalog_item(ProfileCatalogViewState& state,const std::string& id,const std::string& name,const ProfileJson& document){
 auto found=std::find_if(state.items.begin(),state.items.end(),[&](const auto& item){return item.id==id;});
 ProfileCatalogItem item;item.id=id;item.name=name;item.document=document;item.description=inspect_profile(document);item.selected_targets=item.description.targets;
 if(found==state.items.end())state.items.push_back(std::move(item));else *found=std::move(item);invalidate(state);
}
bool stage_profile_catalog_selection(ProfileCatalogViewState& state){
 try{std::vector<ProfileComponent> components;for(const auto& item:state.items)if(item.included){
   if(!item.description.valid)throw std::runtime_error(item.name+": "+item.description.error);
   components.push_back({item.name,select_profile_targets(item.document,item.selected_targets)});
  }
  state.staged=compose_profiles(components);state.preflight_ok=false;
  state.status.clear();return true;
 }catch(const std::exception& e){state.staged.reset();state.preflight_ok=false;state.status=e.what();return false;}
}
void record_profile_library(ProfileCatalogViewState& state,std::vector<ProfileLibraryEntry> entries,const std::string& status){
 for(auto& entry:entries)
     if(entry.document&&entry.error.empty()&&!entry.description.valid&&entry.description.error.empty())
         entry.description=inspect_profile(*entry.document);
 state.library=std::move(entries);state.library_loading=false;state.library_status=status;
}
bool stage_profile_library_item(ProfileCatalogViewState& state,const std::string& id){
 const auto found=std::find_if(state.library.begin(),state.library.end(),[&](const auto& entry){return entry.id==id;});
 if(found==state.library.end()||!found->document||!found->error.empty()){state.status="Saved profile is unreadable; nothing staged";return false;}
 const auto description=inspect_profile(*found->document);if(!description.valid){state.status=description.error;return false;}
 add_profile_catalog_item(state,found->id,found->name,*found->document);for(auto& item:state.items)item.included=item.id==id;
 return stage_profile_catalog_selection(state);
}
bool stage_newest_profile_history(ProfileCatalogViewState& state){
 state.staged.reset();state.preflight_ok=false;for(auto& item:state.items)item.included=false;
 const auto* newest=newest_profile_autosave(state.library);if(!newest){state.status="No Undo snapshots found.";return false;}
 const auto name=newest->name;if(!newest->error.empty()||!newest->document){state.status="Newest Undo snapshot '"+name+"' is unreadable: "+(newest->error.empty()?std::string("no profile document"):newest->error);return false;}
 const auto description=inspect_profile(*newest->document);if(!description.valid){state.status="Newest Undo snapshot '"+name+"' cannot be staged: "+description.error;return false;}
 if(!stage_profile_library_item(state,newest->id)){state.status="Newest Undo snapshot '"+name+"' cannot be staged: "+state.status;return false;}
 return true;
}
void record_profile_catalog_preflight(ProfileCatalogViewState& state,uint64_t generation,const ProfileJson& document,bool ok,const std::string& message){
 if(state.generation!=generation||!state.staged||*state.staged!=document)return;
 state.preflight_ok=ok;state.status=message;
}
ProfileCatalogViewOutput draw_profile_catalog_workspace(ProfileCatalogViewState& state,uint64_t generation,bool busy,bool unlocked){
    sync_profile_catalog_generation(state, generation);
    ProfileCatalogViewOutput out;
    if (!state.open) {
        state.was_open = false;
        return out;
    }

    const auto refresh_library = [&] {
        state.library_loading = true;
        out.intents.push_back({ProfileCatalogIntent::Operation::refresh_library});
    };
    if (!state.was_open) {
        state.was_open = true;
        if (!state.library_loading) refresh_library();
    }

    const auto* viewport = ImGui::GetMainViewport();
    const float dpi = ImGui::GetStyle().FontScaleDpi;
    ImGui::SetNextWindowPos(viewport->GetWorkCenter(), ImGuiCond_FirstUseEver, {0.5f, 0.5f});
    ImGui::SetNextWindowSize({std::min(760.0f * dpi, viewport->WorkSize.x * 0.94f),
                             std::min(640.0f * dpi, viewport->WorkSize.y * 0.9f)}, ImGuiCond_FirstUseEver);
    if (!ImGui::Begin("Profile catalog###druta_profile_catalog", &state.open)) {
        ImGui::End();
        return out;
    }

    const auto& style = ImGui::GetStyle();
    const auto same_line_if_fits = [&](const char* label) {
        const float right_edge = ImGui::GetCursorScreenPos().x + ImGui::GetContentRegionAvail().x;
        const float width = ImGui::CalcTextSize(label, nullptr, true).x + style.FramePadding.x * 2;
        if (ImGui::GetItemRectMax().x + style.ItemSpacing.x + width <= right_edge) ImGui::SameLine();
    };
    const auto dimmed = [&](const std::string& text) {
        ImGui::PushStyleColor(ImGuiCol_Text, style.Colors[ImGuiCol_TextDisabled]);
        ImGui::TextWrapped("%s", text.c_str());
        ImGui::PopStyleColor();
    };
    const auto library_title = [](const ProfileLibraryEntry& entry) {
        if (!entry.autosave) return entry.name;
        // Native filenames end in a FILETIME, process ID and serial. Keep
        // the actual filename available in the tooltip, alongside the date.
        constexpr std::string_view prefix = "autosave-native-";
        constexpr size_t suffix_length = 35;
        if (entry.library_owned && entry.name.size() > prefix.size() + suffix_length) {
            auto action = entry.name.substr(prefix.size(), entry.name.size() - prefix.size() - suffix_length);
            if (action == "vf-curve") return std::string("Before V/F curve edit");
            if (action == "max-it") return std::string("Before Max it");
            std::replace(action.begin(), action.end(), '-', ' ');
            return std::string("Before ") + action;
        }
        return std::string("Undo snapshot");
    };
    const auto visible_entry = [&](const ProfileLibraryEntry& entry) {
        return state.library_filter == ProfileLibraryFilter::all ||
            (state.library_filter == ProfileLibraryFilter::autosaves) == entry.autosave;
    };

    ImGui::PushID("profile-catalog");
    ImGui::SeparatorText("Saved profiles");
    if (ImGui::Button("Choose profile files..."))
        out.intents.push_back({ProfileCatalogIntent::Operation::choose_files});
    same_line_if_fits("Refresh###Refresh saved profiles");
    ImGui::BeginDisabled(state.library_loading);
    if (ImGui::Button("Refresh###Refresh saved profiles")) refresh_library();
    ImGui::EndDisabled();
    same_line_if_fits("Stage latest Undo###Undo from history...");
    ImGui::BeginDisabled(busy || state.library_loading);
    if (ImGui::Button("Stage latest Undo###Undo from history...")) {
        state.staged.reset();
        state.preflight_ok = false;
        for (auto& item : state.items) item.included = false;
        state.status = "Reading Undo history...";
        out.intents.push_back({ProfileCatalogIntent::Operation::stage_newest_history});
    }
    ImGui::EndDisabled();

    if (ImGui::RadioButton("Named", state.library_filter == ProfileLibraryFilter::named))
        state.library_filter = ProfileLibraryFilter::named;
    ImGui::SameLine();
    if (ImGui::RadioButton("Undo history", state.library_filter == ProfileLibraryFilter::autosaves))
        state.library_filter = ProfileLibraryFilter::autosaves;
    ImGui::SameLine();
    if (ImGui::RadioButton("All", state.library_filter == ProfileLibraryFilter::all))
        state.library_filter = ProfileLibraryFilter::all;

    if (state.library_loading) ImGui::TextDisabled("Reading saved profiles...");
    else if (!state.library_status.empty()) ImGui::TextWrapped("%s", state.library_status.c_str());

    const auto visible_count = std::count_if(state.library.begin(), state.library.end(), visible_entry);
    const float line = ImGui::GetTextLineHeightWithSpacing();
    const float list_max = std::clamp(ImGui::GetContentRegionAvail().y * 0.48f, 4 * line, 18 * line);
    const float list_height = std::min(list_max,
        std::max(3 * line, static_cast<float>(visible_count) * (2 * line + style.CellPadding.y * 2) + style.WindowPadding.y * 2));
    if (ImGui::BeginChild("saved-profile-library", ImVec2(0, list_height), ImGuiChildFlags_Borders)) {
        if (!visible_count && !state.library_loading) {
            const char* message = state.library_filter == ProfileLibraryFilter::named ? "No named profiles." :
                state.library_filter == ProfileLibraryFilter::autosaves ? "No Undo snapshots." : "No saved profiles.";
            ImGui::TextDisabled("%s", message);
        }
        if (visible_count && ImGui::BeginTable("saved-profile-rows", 2,
                ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_RowBg)) {
            ImGui::TableSetupColumn("Profile", ImGuiTableColumnFlags_WidthStretch);
            ImGui::TableSetupColumn("Action", ImGuiTableColumnFlags_WidthFixed);
            for (const auto& entry : state.library) {
                if (!visible_entry(entry)) continue;
                const auto& description = entry.description;
                const bool valid = entry.document && entry.error.empty() && description.valid;
                ImGui::PushID(entry.id.c_str());
                ImGui::TableNextRow();
                ImGui::TableNextColumn();
                const auto title = library_title(entry);
                ImGui::TextWrapped("%s", title.c_str());
                if (entry.autosave && ImGui::IsItemHovered())
                    ImGui::SetTooltip("%s", entry.name.c_str());
                std::string detail;
                if (valid) {
                    detail = profile_scope_name(description.scope);
                    detail += "  |  " + std::to_string(description.targets.size());
                    detail += description.targets.size() == 1 ? " target" : " targets";
                } else {
                    detail = !entry.error.empty() ? entry.error :
                        !entry.document ? "Unreadable profile" : description.error;
                }
                if (!entry.saved_at.empty()) detail += "  |  " + entry.saved_at;
                if (!entry.source.empty()) detail += "  |  " + entry.source;
                dimmed(detail);

                ImGui::TableNextColumn();
                ImGui::BeginDisabled(!valid);
                if (ImGui::Button("Stage###Stage profile")) stage_profile_library_item(state, entry.id);
                ImGui::EndDisabled();
                ImGui::PopID();
            }
            ImGui::EndTable();
        }
    }
    ImGui::EndChild();

    ImGui::SeparatorText("Review");
    if (state.items.empty() && !state.staged)
        ImGui::TextDisabled("Stage a saved profile or choose profile files.");
    for (auto& item : state.items) {
        ImGui::PushID(item.id.c_str());
        ImGui::BeginDisabled(!item.description.valid);
        if (ImGui::Checkbox("##include", &item.included)) invalidate(state);
        ImGui::EndDisabled();
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled) && item.description.valid)
            ImGui::SetTooltip("Include in the composed profile");
        ImGui::SameLine();
        const auto entry = std::find_if(state.library.begin(), state.library.end(),
            [&](const auto& saved) { return saved.id == item.id; });
        const auto title = entry != state.library.end() ? library_title(*entry) : item.name;
        if (item.description.valid) {
            if (ImGui::TreeNodeEx("profile-targets", ImGuiTreeNodeFlags_SpanAvailWidth,
                    "%s  (%zu of %zu targets)", title.c_str(), item.selected_targets.size(), item.description.targets.size())) {
                if (item.description.uuid) dimmed("GPU " + *item.description.uuid);
                if (item.description.slot) dimmed("Slot " + *item.description.slot);
                for (const auto& target : item.description.targets) {
                    const auto found = std::find(item.selected_targets.begin(), item.selected_targets.end(), target);
                    bool selected = found != item.selected_targets.end();
                    if (ImGui::Checkbox(target.c_str(), &selected)) {
                        if (selected) item.selected_targets.push_back(target);
                        else item.selected_targets.erase(found);
                        invalidate(state);
                    }
                }
                ImGui::TreePop();
            }
        } else {
            ImGui::TextWrapped("%s", title.c_str());
            dimmed(item.description.error);
        }
        ImGui::PopID();
    }
    if (!state.items.empty()) {
        const bool has_selection = std::any_of(state.items.begin(), state.items.end(), [](const auto& item) {
            return item.included && item.description.valid && !item.selected_targets.empty();
        });
        ImGui::BeginDisabled(!has_selection);
        if (ImGui::Button("Compose and stage selection")) stage_profile_catalog_selection(state);
        ImGui::EndDisabled();
        same_line_if_fits("Clear review###Clear list");
        if (ImGui::Button("Clear review###Clear list")) {
            state.items.clear();
            invalidate(state);
            state.status.clear();
        }
    }
    if (state.staged) {
        const auto staged_info = inspect_profile(*state.staged);
        ImGui::Text("Staged for review  |  %zu %s", staged_info.targets.size(),
            staged_info.targets.size() == 1 ? "target" : "targets");
        ImGui::BeginDisabled(busy);
        if (ImGui::Button("Preflight staged profile"))
            out.intents.push_back({ProfileCatalogIntent::Operation::preflight, ProfileScope::combined, *state.staged});
        ImGui::EndDisabled();
        same_line_if_fits("Apply staged profile");
        const bool metadata_only = staged_info.valid && staged_info.targets == std::vector<std::string>{"annotation:power"};
        ImGui::BeginDisabled(busy || (!unlocked && !metadata_only) || !state.preflight_ok);
        if (ImGui::Button("Apply staged profile")) {
            out.intents.push_back({ProfileCatalogIntent::Operation::apply, ProfileScope::combined, *state.staged});
            state.preflight_ok = false;
            state.status = "Applying profile...";
        }
        ImGui::EndDisabled();
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled) && !busy) {
            if (!unlocked && !metadata_only) ImGui::SetTooltip("Unlock controls to apply this profile.");
            else if (!state.preflight_ok) ImGui::SetTooltip("Run preflight before applying this profile.");
        }
        same_line_if_fits("Save composed profile...");
        if (ImGui::Button("Save composed profile..."))
            out.intents.push_back({ProfileCatalogIntent::Operation::save_composed, ProfileScope::combined, *state.staged});
    }
    if (!state.status.empty()) ImGui::TextWrapped("%s", state.status.c_str());

    if (ImGui::CollapsingHeader("Capture a profile")) {
        constexpr ProfileScope scopes[] = {ProfileScope::full_tune, ProfileScope::daily, ProfileScope::curve,
            ProfileScope::scoped, ProfileScope::timing, ProfileScope::icx, ProfileScope::fan,
            ProfileScope::voltage_policy, ProfileScope::power_integral, ProfileScope::voltage_tables,
            ProfileScope::voltage_limits, ProfileScope::power_saving, ProfileScope::clock_gating};
        const float input_width = std::min(230.0f * dpi, ImGui::GetContentRegionAvail().x);
        ImGui::SetNextItemWidth(input_width);
        if (ImGui::BeginCombo("Capture scope", profile_scope_name(state.capture_scope))) {
            for (auto scope : scopes) {
                if (ImGui::Selectable(profile_scope_name(scope), scope == state.capture_scope)) state.capture_scope = scope;
            }
            ImGui::EndCombo();
        }
        same_line_if_fits("Save configured scope...");
        ImGui::BeginDisabled(busy);
        if (ImGui::Button("Save configured scope..."))
            out.intents.push_back({ProfileCatalogIntent::Operation::save_capture, state.capture_scope});
        ImGui::EndDisabled();

        ImGui::SetNextItemWidth(input_width);
        ImGui::InputTextWithHint("##Profile name", "Profile name", state.save_name.data(), state.save_name.size());
        same_line_if_fits("Save named profile");
        const std::string name(state.save_name.data());
        ImGui::BeginDisabled(busy || name.find_first_not_of(" \t\r\n") == std::string::npos);
        if (ImGui::Button("Save named profile"))
            out.intents.push_back({ProfileCatalogIntent::Operation::save_named, state.capture_scope, {}, name});
        ImGui::EndDisabled();
    }

    ImGui::PopID();
    ImGui::End();
    return out;
}
}
