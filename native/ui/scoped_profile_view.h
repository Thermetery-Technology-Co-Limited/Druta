// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "scoped_profile.h"
#include "session.h"
namespace druta::ui {
struct ScopedProfileViewState {
    uint64_t generation=0;
    bool initialized=false,open=false,save_requested=false,load_requested=false;
    std::vector<ScopedKey> selection;
    std::optional<ProfileJson> loaded_document;
    std::optional<ScopedPlan> plan;
    std::string status;
    std::optional<bool> status_ok;
    bool status_from_operation=false;
    std::optional<Command::Operation> pending_operation;
    bool pending_accepted = false;
    uint64_t pending_action_counter=0,pending_log_id=0;
    std::shared_ptr<const ScopedSnapshot> observed_snapshot;
};
void sync_scoped_profile_view(ScopedProfileViewState& state,uint64_t generation);
// Pass the snapshot from immediately before dispatch; accepted Save dispatch
// calls this helper in the host. View-generated intents mark themselves.
void mark_scoped_profile_pending(ScopedProfileViewState& state,const SessionSnapshot& baseline,Command::Operation operation,bool accepted=false);
void observe_scoped_profile_action(ScopedProfileViewState& state,const SessionSnapshot& snapshot);
std::string scoped_profile_apply_blocker(const SessionSnapshot& snapshot);
bool stage_scoped_profile(const ProfileJson& document,const ScopedSnapshot& snapshot,ScopedProfileViewState& state);
bool can_save_scoped_profile(const ScopedProfileViewState& state,const SessionSnapshot& snapshot);
bool can_apply_scoped_profile(const ScopedProfileViewState& state,const SessionSnapshot& snapshot,bool unlocked);
void draw_scoped_profile_workspace(ScopedProfileViewState& state,const SessionSnapshot& snapshot,
    bool controls_unlocked,std::vector<Command>& commands);
}
