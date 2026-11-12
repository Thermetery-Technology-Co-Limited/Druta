/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "curve_planner.h"
#include <drutacore/drutacore.h>
#include <string>

namespace druta::ui {
struct TuningApi {
    decltype(&dc_gpu_lock) lock = nullptr;
    decltype(&dc_gpu_unlock) unlock = nullptr;
    decltype(&dc_gpu_identity_get) identity = nullptr;
    decltype(&dc_gpu_vf_lock_status) vf_status = nullptr;
    decltype(&dc_gpu_clk_lock_status) clock_status = nullptr;
    decltype(&dc_gpu_vf_lock_set) vf_set = nullptr;
    decltype(&dc_gpu_vf_lock_clear) vf_clear = nullptr;
    decltype(&dc_gpu_vf_lock_recovery_pending) recovery_pending = nullptr;
    decltype(&dc_gpu_vf_lock_recover) recover = nullptr;
    decltype(&dc_rails_hold_record_get) headroom_record = nullptr;
    decltype(&dc_rails_hold_apply) headroom_apply = nullptr;
    decltype(&dc_rails_hold_restore) headroom_restore = nullptr;
    decltype(&dc_rails_hold_live_ok) headroom_live = nullptr;
    decltype(&dc_rails_hold_overwritten) headroom_overwritten = nullptr;
};
struct TuningResult { bool ok = false; bool may_have_changed = false; std::string message; };
struct TuningState { bool owned = false; bool confirmed = false; bool headroom = false; bool recovery = false; bool tripped = false; uint32_t domain = 0; uint32_t voltage_uv = 0; bool maintenance_needed() const { return owned || headroom || recovery; } };
// One instance per opened handle. The caller must serialize calls and keep this object
// alive while state().owned, state().headroom or state().recovery is true.
class TuningActions {
public:
    TuningActions(TuningApi api, dc_gpu* gpu, const dc_gpu_identity& identity);
    CurvePlan prepare_max(const PlannerApi& api, CurvePlanRequest request) const;
    TuningResult hold(uint32_t domain, uint32_t voltage_uv, bool headroom, double margin_mv, bool xoc);
    TuningResult maintain(); // minimal read/restore work even when the UI is paused
    TuningResult release();
    TuningState state() const { return state_; }
private:
    bool identity_ok();
    bool record(dc_rails_hold_record& out);
    bool restore_headroom(std::string& why);
    TuningApi api_; dc_gpu* gpu_; dc_gpu_identity identity_; TuningState state_{};
};
}
