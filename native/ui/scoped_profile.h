// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "backend.h"
#include "profile_io.h"
namespace druta::ui {
struct ScopedKey {
    enum class Kind { rail, policy, board };
    Kind kind=Kind::rail;
    uint32_t rail=0,field=0,policy=0;
    static ScopedKey rail_field(uint32_t rail,uint32_t field);
    static ScopedKey policy_request(uint32_t policy);
    static ScopedKey board_power();
};
bool operator==(const ScopedKey& a,const ScopedKey& b);
struct ScopedTarget {std::string uuid,slot;};
struct ScopedCurrent {
    ScopedKey key;
    // Existing native canonical binding: identity/geometry/reference metadata,
    // never opaque state to replay. Exclude mutable requested values.
    std::string binding,unit;
    int64_t value=0,low=0,high=0;
    bool fresh=false,writable=false;
    std::optional<int64_t> pin_value;
    // Reported identity metadata used for display annotations, never replayed.
    std::optional<uint32_t> channel,policy_type;
};
struct ScopedSnapshot {ScopedTarget target;std::vector<ScopedCurrent> entries;};
struct ScopedProfileRequest {
    std::vector<ScopedKey> selection;
    ProfileJson document;
};
struct ScopedStep {
    ScopedKey key;Command command;
    int64_t before=0,value=0;
    std::string unit,binding;
    bool changed=false,execute_required=false,writable=false;
};
struct ScopedPlan {
    ScopedTarget target;
    // Canonical order: board, policies by id, rails by rail/field.
    // Includes unchanged selected entries for final whole-selection verification.
    std::vector<ScopedStep> steps;
    bool coupled_power=false;
    std::string summary;
};
// Pure operations: no files, hardware, automatic application, or registry state.
// Capture needs fresh restorable values; the write gate may remain off while
// saving. Plan requires every selected entry fresh and currently writable.
ProfileJson capture_scoped_profile(const ScopedSnapshot& current,const std::vector<ScopedKey>& selection);
// require_writable=false is preview only; worker application must rebuild the
// default strict plan immediately before any write, never execute preview commands.
ScopedPlan plan_scoped_profile(const ProfileJson& document,const ScopedSnapshot& current,bool require_writable=true);
// Worker must revalidate the entire document immediately before any write,
// use existing typed writers with grouped Undo, then verify all selected
// values/pins, including entries a coupled write may have changed. Failure
// stops the transaction and retains meaningful Undo; this model never rolls
// back or replays unselected requests. Empty string means exact readback.
std::string scoped_profile_readback_error(const ScopedPlan& plan,const ScopedSnapshot& current);
}
