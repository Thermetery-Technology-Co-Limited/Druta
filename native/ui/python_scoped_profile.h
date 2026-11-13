// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "scoped_profile.h"
#include <map>
namespace druta::ui {
struct PythonRailProfile {ScopedTarget target;std::vector<std::pair<ScopedKey,int64_t>> requests;};
struct PythonPowerRow {
    uint32_t policy=0,type=0,channel=0,minimum=0,maximum=0,requested=0;
    std::optional<std::string> unit;
    bool board=false,named_current=false,writable=false,consistent=false;
};
struct PythonPowerProfile {
    ScopedTarget target;uint32_t board_mw=0;
    std::optional<std::vector<PythonPowerRow>> rows;
    std::map<uint32_t,uint32_t> pins;
    // A full-tune import binds its saved type/channel requests to fresh native
    // geometry. It has no historical board-policy row request; the separately
    // captured board_mw is authoritative, never fabricated as a policy value.
    bool current_layout_bound=false;
    std::optional<std::vector<uint32_t>> selected_policies;
    std::vector<uint32_t> derived_candidates;
    std::vector<uint32_t> explicit_currents;
};
// Structural validation only; never accesses an adapter or file. Driver/VBIOS
// metadata are informational and never compatibility gates.
PythonRailProfile parse_python_rail_profile(const ProfileJson& document);
PythonPowerProfile parse_python_power_profile(const ProfileJson& document);
// Bind only the selected saved rail fields to this fresh native capture. An
// empty selection includes all saved fields, never unrelated current rails.
ProfileJson import_python_rail_profile(const ProfileJson& document,const ScopedSnapshot& current,
    const std::vector<ScopedKey>& selection={},bool require_writable=false);
// Full power-action semantics include coupled rows and exact original pins.
// Host executes board then current-qualified rows using pin=false, verifies
// all requests, then reinstalls only verified saved pins. Never reinterpret
// unpinned rows as explicit pins through ScopedPlan.
void validate_python_power_live(const PythonPowerProfile& saved,const ScopedTarget& target,
    const std::optional<std::vector<PythonPowerRow>>& current,bool board_only_peer_preserving);
}
