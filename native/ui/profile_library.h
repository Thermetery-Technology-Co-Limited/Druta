// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_io.h"
#include <optional>
#include <vector>
namespace druta::ui {
struct ProfileLibraryPaths {
 // Empty fields resolve the current executable and LOCALAPPDATA (home fallback).
 std::wstring executable,local_app_data;
};
struct ProfileLibraryDirectory {std::wstring path;std::string source;bool writable=false;};
struct ProfileLibraryEntry {
 std::string id,name,source,saved_at,error;
 std::wstring path;
 bool autosave=false,library_owned=false;
 double timestamp=0;
 std::optional<ProfileJson> document;
};
// Resolving/discovering never creates directories or reads profiles.
std::vector<ProfileLibraryDirectory> discover_profile_directories(const ProfileLibraryPaths& paths={});
std::vector<ProfileLibraryEntry> list_profile_library(const std::vector<ProfileLibraryDirectory>&);
// Exact typed JSON is preserved. Only the native destination is written.
// Named saves replace the same sanitized name atomically; autosaves never collide.
std::wstring profile_library_named_path(const ProfileLibraryDirectory& native,const std::string& name);
std::wstring save_library_profile(const ProfileLibraryDirectory& native,const std::string& name,const ProfileJson&);
std::wstring save_library_autosave(const ProfileLibraryDirectory& native,const std::string& action,const ProfileJson&);
}
