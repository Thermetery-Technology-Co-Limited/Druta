/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "curve_planner.h"
#include <drutacore/drutacore.h>
#include <memory>
#include <string>
#include <vector>

namespace druta::ui {
struct LiveData;
struct MaxItPlan {
    uint32_t fan_percent = 0, board_mw = 0;
    std::string reason, fan_range_note;
    bool ready() const { return fan_percent && board_mw && reason.empty(); }
};
MaxItPlan discover_max_it(const LiveData& live, const dc_fan_state& fans, const dc_fan_capabilities& fan_caps,
                          const dc_capabilities* static_caps, uint32_t nominal_power_mw, bool curve_planner_available);
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
enum class HeadroomApplyOutcome { unverified, skipped_before_writer, no_set_issued, already_sufficient, request_verified };
struct HeadroomApplyEvidence {
    HeadroomApplyOutcome outcome = HeadroomApplyOutcome::unverified;
    uint32_t target_uv = 0;
    double margin_mv = 0;
    std::string detail;
    std::vector<std::string> last_writer_observations;
};
struct TuningState {
    bool owned = false, confirmed = false, headroom = false, recovery = false, tripped = false;
    uint32_t domain = 0, voltage_uv = 0;
    // Last apply on this open handle. Release and Undo only change current ownership.
    std::shared_ptr<const HeadroomApplyEvidence> last_headroom_apply;
    bool maintenance_needed() const { return owned || headroom || recovery; }
};
// One instance per opened handle. The caller must serialize calls and keep this object
// alive while state().owned, state().headroom or state().recovery is true.
class TuningActions {
public:
    TuningActions(TuningApi api, dc_gpu* gpu, const dc_gpu_identity& identity);
    CurvePlan prepare_max(const PlannerApi& api, CurvePlanRequest request, bool step_measured) const;
    TuningResult hold(uint32_t domain, uint32_t voltage_uv, bool headroom, double margin_mv, bool xoc,
                      bool allow_skipped_headroom = false);
    TuningResult maintain(); // minimal read/restore work even when the UI is paused
    TuningResult release();
    // Python restore_raised_limits_now: takes this session's headroom raise off
    // on the user's request, with one bounded retry; a hold stays in force.
    TuningResult restore_raised();
    TuningState state() const { return state_; }
private:
    bool identity_ok();
    bool record(dc_rails_hold_record& out);
    bool restore_headroom(std::string& why, std::string* restored = nullptr);
    TuningApi api_; dc_gpu* gpu_; dc_gpu_identity identity_; TuningState state_{};
};
}
