// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "frequency_lock.h"
namespace druta::ui {
enum class FrequencyLockAction { refresh,apply,lock_max,legacy_p0_max_fan,release };
struct FrequencyLockIntent {FrequencyLockAction action=FrequencyLockAction::refresh;int64_t minimum_mhz=0,maximum_mhz=0;};
struct FrequencyLockViewContext {
    uint64_t generation=0;
    std::string slot,uuid;
    bool connected=false,busy=false,unlocked=false,fan_manual_available=false;
    std::optional<double> live_core_mhz;
    // Set only when the coordinator proves the visible min=max request is
    // this session's exact owned I2C clamp. Apply still performs handover.
    bool other_owned_clock=false;
};
struct FrequencyLockViewState {
    bool open=false,initialized=false,dirty=false;
    uint64_t generation=0;
    std::string slot,uuid;
    int64_t minimum_mhz=0,maximum_mhz=0;
    std::optional<FrequencyRange> observed_owned;
};
// Emits worker intents only. The host captures fan Undo and coordinates owned
// I2C/VF/headroom handover; this view never invokes a driver or spawns a worker.
std::vector<FrequencyLockIntent> draw_frequency_lock(FrequencyLockViewState& state,
    const FrequencyLockData& data,const FrequencyLockViewContext& context);
// Released 1.9.1 Clocks menu GPU CLOCK LOCK block, drawn inline in an open
// menu. Shares the range with the window; the same capability, range and
// ownership gates apply, plus 1.9.1's unlock gate on every widget. step_mhz is
// the card's clock bin in whole MHz (0: unknown, step 1); grid_khz is the exact
// clock grid the V/F editor uses (0: unknown), named in the Lock max tooltip.
std::vector<FrequencyLockIntent> draw_frequency_lock_menu(FrequencyLockViewState& state,
    const FrequencyLockData& data,const FrequencyLockViewContext& context,int64_t step_mhz,bool vf_applicable,uint32_t grid_khz=0);
// 1.9.1's note under the inline lock, shown with a V/F curve. Python's last
// clause names a memory clock measured on one card and is left out.
inline constexpr char frequency_lock_menu_note[]="the result is one line in the Control tab log.\n"
    "Ctrl+H on the curve editor is a DIFFERENT lock\n(V/F point, by voltage); taking one releases the\nother.";
// 1.9.1's Lock max tooltip: Python's floor((base+delta)/15)*15 uses the
// adapter's grid_khz, or reads "/step)*step" while the grid is unknown.
std::string frequency_lock_max_tooltip(const std::optional<uint32_t>& maximum_mhz,uint32_t grid_khz);
// Lock P0 and max fan's own gate: a connected, idle, unlocked session, the
// adapter's freshly established legacy-P0 capability and manual fan control.
bool legacy_p0_capable(const FrequencyLockData& data,const FrequencyLockViewContext& context);
// Released 1.9.1 gate row: where the adapter reports legacy-P0 support, Lock P0
// and max fan and Release P0 take Max it's place. legacy_available keeps the
// last established answer through a transient probe failure (legacy_known then
// clears and only disables the action), so the row does not flip.
inline bool legacy_p0_gate_shown(const FrequencyLockData& data){return data.legacy_available;}
// Draws the two gate-row buttons (Classic sizes and 1.9.1's yellow theme) and
// returns their intents. Release P0 acts only on this session's own P0 request.
std::vector<FrequencyLockIntent> draw_legacy_p0_gate(const FrequencyLockData& data,const FrequencyLockViewContext& context);
}
