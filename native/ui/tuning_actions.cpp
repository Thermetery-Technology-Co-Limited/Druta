/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "tuning_actions.h"
#include "backend.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <iterator>

namespace druta::ui {
namespace {
struct Unlock { TuningApi& api; dc_gpu* gpu; ~Unlock() { api.unlock(gpu); } };
bool same(const dc_gpu_identity& a, const dc_gpu_identity& b) {
    return a.slot[0] && (b.nvapi_ok || b.nvml_ok) && !a.pairing_error[0] && !b.pairing_error[0] &&
        std::strcmp(a.slot, b.slot) == 0 && std::strcmp(a.uuid, b.uuid) == 0 &&
        a.nvapi_selected == b.nvapi_selected && a.nvml_selected == b.nvml_selected &&
        a.nvapi_handle == b.nvapi_handle && a.nvapi_devid == b.nvapi_devid &&
        a.nvapi_subsys == b.nvapi_subsys && a.nvml_has_devid == b.nvml_has_devid &&
        a.nvml_devid == b.nvml_devid && a.nvml_subsys == b.nvml_subsys;
}
}
MaxItPlan discover_max_it(const LiveData& live, const dc_fan_state& fans, const dc_fan_capabilities& fan_caps,
                          const dc_capabilities* static_caps, uint32_t nominal_power_mw, bool curve_planner_available) {
    MaxItPlan plan;
    if (nominal_power_mw != UINT32_MAX) plan.board_mw = nominal_power_mw;
    const auto& fan = live.controls[static_cast<size_t>(ControlId::fan)];
    const auto& power = live.controls[static_cast<size_t>(ControlId::power)];
    const auto& boost = live.controls[static_cast<size_t>(ControlId::boost)];
    if (fan.availability != Availability::available || !fan.writable || !fans.count ||
        fans.count > std::size(fans.fans) || !fan_caps.manual) {
        plan.reason = "Manual fan control and a readable starting fan policy are required.";
        return plan;
    }
    auto low = fan_caps.min, high = fan_caps.max;
    for (uint32_t i = 0; i < fans.count; ++i) {
        const auto& row = fans.fans[i];
        low = std::max(low, row.min); high = std::min(high, row.max);
        if ((row.manual != 0 && row.manual != 1) || row.level > 100 ||
            (row.manual && (row.level < row.min || row.level > row.max)) ||
            (!row.manual && (!fan_caps.automatic || row.policy != row.default_policy))) {
            plan.reason = "The current fan policy cannot be restored after Max it.";
            return plan;
        }
    }
    if (low > high || !high || high > 100) {
        plan.reason = "The fans have no common, valid manual duty range.";
        return plan;
    }
    plan.fan_percent = high;
    const auto native_source = [](const char (&source)[16]) {
        return std::strncmp(source, "nvapi_cooler", sizeof(source)) == 0 ||
               std::strncmp(source, "nvapi_client", sizeof(source)) == 0;
    };
    const uint32_t range_flags = DC_C_FAN_MIN | DC_C_FAN_MAX;
    if (native_source(fans.source) || (native_source(fan_caps.source) && fan_caps.max == high))
        plan.fan_range_note = "The cooler reports per-fan duty limits.";
    else if (static_caps && (static_caps->present & range_flags) == range_flags &&
             static_caps->fan_min <= high && static_caps->fan_max == high)
        plan.fan_range_note = "The adapter reports the manual duty range.";
    else
        plan.fan_range_note = "The hardware duty range is unconfirmed. The fan request may use the backend default.";
    if (!plan.board_mw) plan.reason = "The normal board power maximum could not be read.";
    else if (power.availability != Availability::available || !power.writable || !power.low || !power.high ||
             !power.current || !(*power.low <= plan.board_mw / 1000.0 && plan.board_mw / 1000.0 <= *power.high &&
                                 *power.low <= *power.current && *power.current <= *power.high))
        plan.reason = "The board power target or its starting request cannot be written and restored.";
    else if (boost.availability != Availability::available || !boost.writable || !boost.current ||
             !(*boost.current >= 0 && *boost.current <= 100))
        plan.reason = "Voltage boost needs a readable starting request and a setter.";
    else if (!curve_planner_available || !live.curve_writable || live.curve.empty() || !live.curve_model_max_rise_khz)
        plan.reason = "Max it needs a complete writable V/F curve, clock grid and ramp planner.";
    else if (!live.hold_available)
        plan.reason = "The interface to hold and release a V/F point is unavailable.";
    return plan;
}
TuningActions::TuningActions(TuningApi api, dc_gpu* gpu, const dc_gpu_identity& identity) : api_(api), gpu_(gpu), identity_(identity) {}
CurvePlan TuningActions::prepare_max(const PlannerApi& api, CurvePlanRequest request, bool step_measured) const {
    request.kind = CurvePlanKind::ramp;
    // The released action used the planner's fallback when NVML did not measure
    // the grid. Keep that ramp policy while retaining the inferred grid elsewhere.
    request.ramp_use_default_step = !step_measured;
    return plan_curve(api, request);
}
bool TuningActions::identity_ok() { dc_gpu_identity id{}; id.size = sizeof(id); return api_.identity && api_.identity(gpu_, &id) == DC_OK && same(identity_, id); }
bool TuningActions::record(dc_rails_hold_record& out) { out = {}; out.size = sizeof(out); return api_.headroom_record && api_.headroom_record(gpu_, &out) == DC_OK; }
bool TuningActions::restore_headroom(std::string& why, std::string* restored) {
    if (!api_.headroom_record && !state_.headroom) return true;
    dc_rails_hold_record before{}; if (!record(before)) { why = "Headroom record unreadable"; return false; }
    if (!before.present) { state_.headroom = false; return true; }
    if (!state_.headroom || !api_.headroom_restore) { why = "Foreign or untracked headroom is present"; return false; }
    dc_rails_hold_outcome result{}; result.size = sizeof(result); dc_rails_setter_writes writes{}; writes.size = sizeof(writes);
    const auto status = api_.headroom_restore(gpu_, 0, &result, &writes);
    dc_rails_hold_record after{};
    if (status != DC_OK || result.ok != DC_RAILS_HOLD_OK || !record(after) || after.present) { why = result.message[0] ? result.message : "Headroom restoration unverified"; return false; }
    if (restored) *restored = result.message[0] ? result.message : "headroom restored";
    state_.headroom = false; return true;
}
TuningResult TuningActions::restore_raised() {
    TuningResult result{};
    if (!state_.headroom) { result.ok = true; result.message = "no raised limits to restore"; return result; }
    if (!gpu_ || !api_.lock || !api_.unlock || !api_.headroom_record || !api_.headroom_restore) { result.message = "Headroom restore interfaces unavailable"; return result; }
    if (api_.lock(gpu_) != DC_OK) { result.message = "GPU lock unavailable"; return result; } Unlock unlock{api_, gpu_};
    if (!identity_ok()) { result.message = "GPU identity unreadable or changed"; return result; }
    std::string restored;
    // A failed restore keeps the record, the flag and the hold; it is tried once more at once.
    if (!restore_headroom(result.message, &restored)) {
        result.may_have_changed = true; result.message.clear();
        if (!restore_headroom(result.message, &restored)) return result;
    }
    result.may_have_changed |= !restored.empty();
    result.ok = true; result.message = restored.empty() ? "no raised limits to restore" : restored; return result;
}
TuningResult TuningActions::hold(uint32_t domain, uint32_t voltage_uv, bool headroom, double margin_mv, bool xoc,
                                 bool allow_skipped_headroom) {
    TuningResult result{};
    if (!gpu_ || !api_.lock || !api_.unlock || !api_.vf_status || !api_.clock_status || !api_.vf_set || !api_.vf_clear || !api_.recovery_pending || !api_.recover || (headroom && !allow_skipped_headroom && !api_.headroom_record) || !voltage_uv || !std::isfinite(margin_mv) || margin_mv < 0) { result.message = "Hold interfaces or request unavailable"; return result; }
    if (api_.lock(gpu_) != DC_OK) { result.message = "GPU lock unavailable"; return result; } Unlock unlock{api_, gpu_};
    if (!identity_ok()) { result.message = "GPU identity unreadable or changed"; return result; }
    dc_rails_hold_record prior{}; dc_vf_lock_status vf{}; dc_clk_lock_status clk{};
    if (state_.owned || state_.headroom || state_.recovery || api_.recovery_pending(gpu_) || (api_.headroom_record && (!record(prior) || prior.present)) || api_.vf_status(gpu_, 0, 0, &vf) != DC_OK || vf.error[0] || vf.locked || api_.clock_status(gpu_, &clk) != DC_OK || clk.error[0] || clk.locked) { result.message = "Existing or unreadable lock, headroom, or recovery; no hold issued"; return result; }
    // Record the possible ownership before dispatch, including exceptions after SET.
    state_.owned = true; state_.confirmed = false; state_.domain = domain; state_.voltage_uv = voltage_uv; state_.tripped = false;
    dc_outcome out{}; result.may_have_changed = true;
    const auto status = api_.vf_set(gpu_, voltage_uv, 1, domain, &out);
    state_.recovery = api_.recovery_pending(gpu_) != 0;
    dc_vf_lock_status after{}; const bool read = identity_ok() && api_.vf_status(gpu_, 1, domain, &after) == DC_OK && !after.error[0];
    state_.confirmed = read && after.locked && after.lock.domain == domain && after.lock.lock_mode == 3 && after.lock.volt_uv == voltage_uv;
    if (!state_.confirmed || status != DC_OK || !out.ok || state_.recovery) { if (read && !after.locked && !state_.recovery) state_.owned = false; result.message = "Hold not confirmed; release or recovery may be required"; return result; }
    if (headroom) {
        // The core records the planned headroom before it writes; conservatively retain
        // ownership even if an exception prevents this caller reading the result.
        auto evidence = std::make_shared<HeadroomApplyEvidence>();
        evidence->target_uv = voltage_uv;
        evidence->margin_mv = margin_mv;
        state_.last_headroom_apply = evidence;
        if (!api_.headroom_record || !api_.headroom_apply || !api_.headroom_restore || !api_.headroom_live || !api_.headroom_overwritten) {
            evidence->outcome = HeadroomApplyOutcome::skipped_before_writer;
            evidence->detail = "Headroom interfaces unavailable";
            result.ok = allow_skipped_headroom;
            result.message = allow_skipped_headroom ? "V/F hold confirmed. Optional headroom skipped: " + evidence->detail
                                                    : "Hold confirmed; headroom interfaces unavailable";
            return result;
        }
        state_.headroom = true; dc_rails_hold_outcome raised{}; raised.size = sizeof(raised); dc_rails_setter_writes writes{}; writes.size = sizeof(writes);
        auto s = api_.headroom_apply(gpu_, voltage_uv / 1000.0, margin_mv, 0, 0, xoc ? 1 : 0, nullptr, nullptr, &raised, &writes);
        if (s == DC_OK) {
            const auto* end = static_cast<const char*>(std::memchr(raised.message, 0, sizeof(raised.message)));
            if (end) evidence->detail.assign(raised.message, static_cast<size_t>(end - raised.message));
        }
        if (writes.attributes == 1 && writes.result.size == sizeof(writes.result)) {
            // The core exports bounded packet summaries, with no payloads or handles.
            for (unsigned i = 0; i < writes.result.observed_count && i < std::size(writes.result.observed); ++i) {
                const auto& entry = writes.result.observed[i];
                const auto* end = static_cast<const char*>(std::memchr(entry, 0, sizeof(entry)));
                if (end) evidence->last_writer_observations.emplace_back(entry, static_cast<size_t>(end - entry));
            }
        }
        dc_rails_hold_record r{}; if (!record(r)) { result.message = "Hold confirmed; headroom result unreadable"; return result; }
        state_.headroom = r.present != 0;
        if (s == DC_OK && !raised.key_error && !raised.aborted) {
            if (raised.ok == DC_RAILS_HOLD_OK && r.present == 1)
                evidence->outcome = HeadroomApplyOutcome::request_verified;
            else if (r.present == 0 && writes.calls == 0) {
                if (raised.ok == DC_RAILS_HOLD_OK) evidence->outcome = HeadroomApplyOutcome::already_sufficient;
                if (raised.ok == DC_RAILS_HOLD_NONE) evidence->outcome = HeadroomApplyOutcome::skipped_before_writer;
            }
        }
        if (s != DC_OK || raised.ok != DC_RAILS_HOLD_OK) {
            result.message=raised.message[0] ? raised.message : "Hold confirmed; headroom not confirmed";
            const bool undispatched=s==DC_OK && raised.ok==DC_RAILS_HOLD_FAILED && !raised.aborted && !raised.key_error &&
                raised.reserved==DC_RAILS_HOLD_WRITER_ISSUED_NO_SET && r.present==0 &&
                writes.calls==1 && writes.attributes==1 && writes.last_attributes==1 &&
                writes.result.size==sizeof(writes.result) && writes.result.done==0 && writes.result.has_status==0;
            if (undispatched) evidence->outcome = HeadroomApplyOutcome::no_set_issued;
            if (allow_skipped_headroom && (undispatched || evidence->outcome == HeadroomApplyOutcome::skipped_before_writer)) {
                // The rail refusal is optional only if this exact primary hold
                // is still ours after the failed attempt and requires no recovery.
                state_.recovery=api_.recovery_pending(gpu_)!=0;
                dc_vf_lock_status current{};
                const bool verified=identity_ok() && api_.vf_status(gpu_,1,domain,&current)==DC_OK && !current.error[0];
                state_.confirmed=verified && current.locked && current.lock.domain==domain &&
                    current.lock.lock_mode==3 && current.lock.volt_uv==voltage_uv;
                if (verified && !state_.confirmed && !state_.recovery) state_.owned=false;
                if (state_.confirmed && !state_.recovery) {
                    result.ok=true;
                    result.message="V/F hold confirmed. Optional headroom skipped: "+result.message;
                }
            }
            return result;
        }
    }
    result.ok = true; result.message = "V/F hold confirmed"; return result;
}
TuningResult TuningActions::release() {
    TuningResult result{}; if (!gpu_ || !api_.lock || !api_.unlock || !api_.vf_status || !api_.vf_clear || !api_.recovery_pending || !api_.recover) { result.message = "Release interfaces unavailable"; return result; }
    if (api_.lock(gpu_) != DC_OK) { result.message = "GPU lock unavailable"; return result; } Unlock unlock{api_, gpu_};
    if (!identity_ok()) { result.message = "GPU identity unreadable or changed"; return result; }
    if (!restore_headroom(result.message)) return result;
    if (api_.recovery_pending(gpu_)) { if (!state_.owned && !state_.recovery) { result.message = "Foreign V/F recovery left untouched"; return result; } state_.recovery = true; dc_outcome recovered{}; result.may_have_changed = true; if (api_.recover(gpu_, &recovered) != DC_OK || !recovered.ok || api_.recovery_pending(gpu_)) { result.message = "V/F recovery remains pending"; return result; } state_.recovery = false; }
    if (!state_.owned) { result.ok = true; result.message = "No owned hold"; return result; }
    dc_vf_lock_status vf{}; if (api_.vf_status(gpu_, 1, state_.domain, &vf) != DC_OK || vf.error[0]) { state_.confirmed = false; result.message = "Hold status unreadable; ownership retained"; return result; }
    if (!vf.locked || vf.lock.domain != state_.domain || vf.lock.lock_mode != 3 || vf.lock.volt_uv != state_.voltage_uv) { state_.owned = state_.confirmed = false; result.ok = true; result.message = "Owned hold no longer present; foreign request left untouched"; return result; }
    dc_outcome out{}; result.may_have_changed = true;
    if (api_.vf_clear(gpu_, 1, state_.domain, 1, state_.voltage_uv, &out) != DC_OK || !out.ok) { state_.confirmed = false; result.message = "Hold release failed; ownership retained"; return result; }
    dc_vf_lock_status after{}; if (!identity_ok() || api_.vf_status(gpu_, 1, state_.domain, &after) != DC_OK || after.error[0] || (after.locked && after.lock.volt_uv == state_.voltage_uv)) { state_.confirmed = false; result.message = "Hold release unverified; ownership retained"; return result; }
    state_.owned = state_.confirmed = false; result.ok = true; result.message = "Owned hold released"; return result;
}
TuningResult TuningActions::maintain() {
    TuningResult result{}; if (!gpu_ || !api_.lock || !api_.unlock || !api_.vf_status || (state_.headroom && !api_.headroom_record)) { result.message = "Maintenance interfaces unavailable"; return result; }
    if (api_.lock(gpu_) != DC_OK) { result.message = "GPU lock unavailable"; return result; } Unlock unlock{api_, gpu_};
    if (!identity_ok()) { result.message = "GPU identity unreadable or changed"; return result; }
    dc_rails_hold_record r{}; if (api_.headroom_record && !record(r)) { result.message = "Headroom record unreadable"; return result; }
    if (!r.present) { state_.headroom = false; if (state_.owned) { dc_vf_lock_status vf{}; if (api_.vf_status(gpu_, 1, state_.domain, &vf) != DC_OK || vf.error[0]) { state_.confirmed = false; result.message = "Hold status transiently unreadable"; return result; } state_.confirmed = vf.locked && vf.lock.domain == state_.domain && vf.lock.lock_mode == 3 && vf.lock.volt_uv == state_.voltage_uv; if (!state_.confirmed && !state_.recovery) state_.owned = false; } result.ok = true; result.message = "No headroom maintenance required"; return result; }
    if (!state_.headroom) { result.message = "Foreign headroom left untouched"; return result; }
    dc_vf_lock_status vf{}; if (api_.vf_status(gpu_, 1, state_.domain, &vf) != DC_OK || vf.error[0]) { state_.confirmed = false; result.message = "Hold status transiently unreadable"; return result; }
    const bool own = state_.owned && vf.locked && vf.lock.domain == state_.domain && vf.lock.lock_mode == 3 && vf.lock.volt_uv == state_.voltage_uv;
    state_.confirmed = own;
    if (own && api_.headroom_live) { dc_outcome live{}; if (api_.headroom_live(gpu_, 0, nullptr, 0, 0, &live) != DC_OK) { result.message = "Live headroom check unreadable"; return result; } if (!live.ok) state_.tripped = true; }
    if (!own || state_.tripped) { result.may_have_changed = true; if (!restore_headroom(result.message)) return result; if (!own) state_.owned = false; result.ok = true; result.message = state_.tripped ? "Headroom removed after live limit trip" : "Headroom removed after hold changed"; return result; }
    if (api_.headroom_overwritten) { int32_t overwritten = 0; if (api_.headroom_overwritten(gpu_, 0, nullptr, &overwritten) != DC_OK || overwritten == 2) { result.message = "Headroom fields unreadable"; return result; } if (overwritten) { result.may_have_changed = true; if (!restore_headroom(result.message)) return result; result.message = "Headroom was changed externally and has been reconciled; explicit replan required"; result.ok = true; return result; } }
    result.ok = true; result.message = "Owned hold and headroom confirmed"; return result;
}
}
