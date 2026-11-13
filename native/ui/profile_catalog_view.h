// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_catalog.h"
#include "profile_library.h"
#include <array>
#include <cstdint>
namespace druta::ui {
struct ProfileCatalogItem {
    std::string id,name;ProfileJson document;ProfileDescription description;
    bool included=false;std::vector<std::string> selected_targets;
};
enum class ProfileLibraryFilter {all,named,autosaves};
struct ProfileCatalogViewState {
    bool open=false,initialized=false;uint64_t generation=0;
    std::vector<ProfileCatalogItem> items;
    ProfileScope capture_scope=ProfileScope::daily;
    std::vector<ProfileLibraryEntry> library;
    ProfileLibraryFilter library_filter=ProfileLibraryFilter::all;
    bool library_loading=false,was_open=false;
    std::string library_status;
    std::array<char,256> save_name{};
    std::optional<ProfileJson> staged;
    bool preflight_ok=false;
    std::string status;
};
struct ProfileCatalogIntent {
    enum class Operation {choose_files,save_capture,save_composed,preflight,apply,refresh_library,save_named};
    Operation operation=Operation::choose_files;
    ProfileScope scope=ProfileScope::daily;
    ProfileJson document;
    std::string name;
};
struct ProfileCatalogViewOutput {std::vector<ProfileCatalogIntent> intents;};
// Host supplies explicit file reads and worker results. Load and composition
// only stage; every Apply still repeats whole preflight in the worker.
void sync_profile_catalog_generation(ProfileCatalogViewState& state,uint64_t generation);
void add_profile_catalog_item(ProfileCatalogViewState& state,const std::string& id,const std::string& name,const ProfileJson& document);
bool stage_profile_catalog_selection(ProfileCatalogViewState& state);
void record_profile_library(ProfileCatalogViewState&,std::vector<ProfileLibraryEntry>,const std::string& status={});
bool stage_profile_library_item(ProfileCatalogViewState&,const std::string& id);
void record_profile_catalog_preflight(ProfileCatalogViewState& state,uint64_t generation,const ProfileJson& document,bool ok,const std::string& message);
ProfileCatalogViewOutput draw_profile_catalog_workspace(ProfileCatalogViewState& state,uint64_t generation,bool busy,bool unlocked);
}
