// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_io.h"
#include <optional>
#include <cstdint>

namespace druta::ui {
struct StartupProfile {
    std::string id, name, uuid, slot;
    ProfileJson document;
};
struct ProfileDescription;
std::string startup_profile_reason(const ProfileDescription&,const std::string& uuid,const std::string& slot);
void validate_startup_profile(const StartupProfile&);
// The current user's task stores the approved document and registration together.
std::optional<StartupProfile> registered_startup_profile();
std::wstring startup_task_xml(const StartupProfile&, const std::wstring& executable,
                              const std::wstring& core,const std::wstring& user_sid);
void register_startup_profile(const StartupProfile&, const std::wstring& core);
void unregister_startup_profile();

class Session;
class PolicyAnnotations;
struct StartupResult { bool ok; std::string message; };
class StartupProfileLaunch {
    std::optional<StartupProfile> profile_;
    std::optional<std::uint64_t> actions_;
    std::uint64_t generation_ = 0;
public:
    void start(Session&, std::optional<StartupProfile> approved, bool hidden);
    bool pending() const { return profile_.has_value(); }
    std::optional<StartupResult> tick(Session&, bool hidden, const PolicyAnnotations&,
                                      std::uint64_t annotation_action_log, bool annotation_ok);
    static void resume(Session&);
};
struct StartupBoot { std::string id; bool clean = false; };
// Record an attempt before opening the GPU. The mutable boot journal never
// supplies a profile, a path to execute, or any tuning values.
void claim_profile_startup();
void finish_profile_startup();
std::string claim_startup_boot(ProfileJson& journal, const StartupBoot& boot);
}
