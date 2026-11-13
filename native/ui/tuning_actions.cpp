/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "tuning_actions.h"
#include <cmath>
#include <cstring>

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
TuningActions::TuningActions(TuningApi api, dc_gpu* gpu, const dc_gpu_identity& identity) : api_(api), gpu_(gpu), identity_(identity) {}
CurvePlan TuningActions::prepare_max(const PlannerApi& api, CurvePlanRequest request) const { request.kind = CurvePlanKind::ramp; return plan_curve(api, request); }
bool TuningActions::identity_ok() { dc_gpu_identity id{}; id.size = sizeof(id); return api_.identity && api_.identity(gpu_, &id) == DC_OK && same(identity_, id); }
bool TuningActions::record(dc_rails_hold_record& out) { out = {}; out.size = sizeof(out); return api_.headroom_record && api_.headroom_record(gpu_, &out) == DC_OK; }
bool TuningActions::restore_headroom(std::string& why) {
    if (!api_.headroom_record && !state_.headroom) return true;
    dc_rails_hold_record before{}; if (!record(before)) { why = "Headroom record unreadable"; return false; }
    if (!before.present) { state_.headroom = false; return true; }
    if (!state_.headroom || !api_.headroom_restore) { why = "Foreign or untracked headroom is present"; return false; }
    dc_rails_hold_outcome result{}; result.size = sizeof(result); dc_rails_setter_writes writes{}; writes.size = sizeof(writes);
    const auto status = api_.headroom_restore(gpu_, 0, &result, &writes);
    dc_rails_hold_record after{};
    if (status != DC_OK || result.ok != DC_RAILS_HOLD_OK || !record(after) || after.present) { why = result.message[0] ? result.message : "Headroom restoration unverified"; return false; }
    state_.headroom = false; return true;
}
TuningResult TuningActions::hold(uint32_t domain, uint32_t voltage_uv, bool headroom, double margin_mv, bool xoc) {
    TuningResult result{};
    if (!gpu_ || !api_.lock || !api_.unlock || !api_.vf_status || !api_.clock_status || !api_.vf_set || !api_.vf_clear || !api_.recovery_pending || !api_.recover || (headroom && !api_.headroom_record) || !voltage_uv || !std::isfinite(margin_mv) || margin_mv < 0) { result.message = "Hold interfaces or request unavailable"; return result; }
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
        if (!api_.headroom_apply || !api_.headroom_restore || !api_.headroom_live || !api_.headroom_overwritten) { result.message = "Hold confirmed; headroom interfaces unavailable"; return result; }
        // The core records the planned headroom before it writes; conservatively retain
        // ownership even if an exception prevents this caller reading the result.
        state_.headroom = true; dc_rails_hold_outcome raised{}; raised.size = sizeof(raised); dc_rails_setter_writes writes{}; writes.size = sizeof(writes);
        auto s = api_.headroom_apply(gpu_, voltage_uv / 1000.0, margin_mv, 0, 0, xoc ? 1 : 0, nullptr, nullptr, &raised, &writes);
        dc_rails_hold_record r{}; if (!record(r)) { result.message = "Hold confirmed; headroom result unreadable"; return result; }
        state_.headroom = r.present != 0;
        if (s != DC_OK || raised.ok != DC_RAILS_HOLD_OK) { result.message = raised.message[0] ? raised.message : "Hold confirmed; headroom not confirmed"; return result; }
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
