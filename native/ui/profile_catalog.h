// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_io.h"
#include <optional>
#include <vector>
namespace druta::ui {
enum class ProfileScope {daily,curve,scoped,timing,icx,python_tuning,python_rails,python_power,fan,legacy_timing,combined,unsupported};
struct ProfileDescription {
    ProfileScope scope=ProfileScope::unsupported;bool valid=false;
    std::string summary,error;
    std::optional<std::string> uuid,slot;
    // Canonical semantic write targets, not display labels or raw snapshots.
    std::vector<std::string> targets;
};
struct ProfileComponent {std::string name;ProfileJson document;};
const char* profile_scope_name(ProfileScope scope);
ProfileDescription inspect_profile(const ProfileJson& document);
// Throws for malformed/unsupported components, incompatible identities,
// duplicate targets (even identical values), or nested collections.
std::vector<ProfileComponent> flatten_profile_collection(const ProfileJson& document);
ProfileJson compose_profiles(const std::vector<ProfileComponent>& components);
bool profile_targets_overlap(const std::string& a,const std::string& b);
// Explicit subset only. Atomic curve/iCX/power snapshots cannot be partly
// selected; choose their single semantic target. Does not add absent scopes.
ProfileJson select_profile_targets(const ProfileJson& document,const std::vector<std::string>& selection);
}
