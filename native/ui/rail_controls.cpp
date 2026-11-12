/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "rail_controls.h"
#include <cmath>
#include <cstring>
#include <limits>

namespace druta::ui {
namespace {
bool same_identity(const dc_gpu_identity& a, const dc_gpu_identity& b) {
    return a.slot[0] && b.slot[0] && (b.nvapi_ok || b.nvml_ok) &&
        std::strcmp(a.slot, b.slot) == 0 && std::strcmp(a.uuid, b.uuid) == 0 &&
        a.nvapi_selected == b.nvapi_selected && a.nvml_selected == b.nvml_selected &&
        a.nvapi_handle == b.nvapi_handle && a.nvapi_devid == b.nvapi_devid &&
        a.nvapi_subsys == b.nvapi_subsys && a.nvml_has_devid == b.nvml_has_devid &&
        a.nvml_devid == b.nvml_devid && a.nvml_subsys == b.nvml_subsys &&
        !a.pairing_error[0] && !b.pairing_error[0];
}
bool identity(const RailApi& api, dc_gpu* gpu, const dc_gpu_identity& target) {
    if (!api.identity || !gpu) return false;
    for (int i = 0; i < 2; ++i) {
        dc_gpu_identity got{}; got.size = sizeof(got);
        if (api.identity(gpu, &got) == DC_OK && same_identity(target, got)) return true;
    }
    return false;
}
struct Lock {
    const RailApi& api; dc_gpu* gpu; bool held;
    Lock(const RailApi& a, dc_gpu* g) : api(a), gpu(g), held(a.lock && a.unlock && g && a.lock(g) == DC_OK) {}
    ~Lock() { if (held) api.unlock(gpu); }
};
bool to_uv(double mv, std::int32_t& out) {
    const double n = mv * 1000.0;
    if (!std::isfinite(n)) return false;
    const double rounded = std::round(n);
    if (rounded < INT32_MIN || rounded > INT32_MAX || rounded / 1000.0 != mv) return false;
    out = static_cast<std::int32_t>(rounded); return true;
}
bool limits(const RailApi& api, dc_gpu* gpu, int rail, dc_rails_limit_row& out) {
    if (!api.limits) return false;
    for (int i = 0; i < 2; ++i) {
        dc_rails_limit_set set{}; set.size = sizeof(set);
        if (api.limits(gpu, &set) == DC_OK && (set.present & (1u << rail))) {
            out = set.rails[rail]; return true;
        }
    }
    return false;
}
bool fields(const RailApi& api, dc_gpu* gpu, int rail) {
    if (!api.fields) return false;
    for (int i = 0; i < 2; ++i) {
        uint32_t count = 0;
        if (api.fields(gpu, rail, &count) == DC_OK && count == DC_RAILS_LIMIT_FIELDS) return true;
    }
    return false;
}
bool initial(const RailApi& api, dc_gpu* gpu, int rail, std::int32_t out[4]) {
    if (!api.initial) return false;
    for (int i = 0; i < 2; ++i) {
        int32_t has = 0;
        if (api.initial(gpu, rail, &has, out) == DC_OK && has == 1) return true;
    }
    return false;
}
RailResult failed(const char* why) { RailResult r; r.message = why; return r; }
}
RailControls::RailControls(RailApi api, dc_gpu* gpu, const dc_gpu_identity& target)
    : api_(api), gpu_(gpu), target_(target) {}
std::array<RailRow, DC_RAILS_COUNT> RailControls::read() {
    for (auto& rail : last_) for (auto& field : rail.fields) if (field.established) field.stale = true;
    Lock guard(api_, gpu_);
    if (!guard.held || !identity(api_, gpu_, target_)) return last_;
    for (int rail = 0; rail < static_cast<int>(DC_RAILS_COUNT); ++rail) {
        dc_rails_limit_row row{};
        if (!fields(api_, gpu_, rail) || !limits(api_, gpu_, rail, row) || !row.has_base) continue;
        auto& dest = last_[rail];
        if (dest.type && *dest.type != row.type) dest = {};
        dest.type = row.type;
        std::int32_t first[4]{};
        bool has_initial = initial(api_, gpu_, rail, first);
        if (has_initial && !initial_type_[rail]) initial_type_[rail] = row.type;
        has_initial = has_initial && initial_type_[rail] && *initial_type_[rail] == row.type;
        for (size_t k = 0; k < DC_RAILS_LIMIT_FIELDS; ++k) {
            std::int32_t value = 0;
            if (!to_uv(row.fields[k], value) || !std::isfinite(row.base[k])) continue;
            auto& f = dest.fields[k]; f.established = true; f.stale = false; f.requested_uv = value;
            f.estimated_base_mv = row.base[k];
            if (has_initial) f.initial_uv = first[k];
        }
    }
    return last_;
}
RailResult RailControls::apply(int rail, std::uint32_t field, std::int32_t raw_uv, bool mode, bool xoc,
                                const std::function<void(const RailUndo&)>& before_write) {
    return write(rail, field, raw_uv, mode, xoc, false, {}, before_write);
}
RailResult RailControls::stock(int rail, std::uint32_t field, const std::function<void(const RailUndo&)>& before_write) {
    return write(rail, field, 0, false, false, true, {}, before_write);
}
RailResult RailControls::restore(const RailUndo& undo, bool mode, bool xoc) {
    return write(undo.rail, undo.field, undo.requested_uv, mode, xoc, false, undo.type, {});
}
RailResult RailControls::write(int rail, std::uint32_t field, std::int32_t value, bool mode, bool xoc,
                               bool stock_action, const std::optional<std::int32_t>& expected_type,
                               const std::function<void(const RailUndo&)>& before_write) {
    if (rail < 0 || rail >= static_cast<int>(DC_RAILS_COUNT) || field >= DC_RAILS_LIMIT_FIELDS)
        return failed("Invalid rail or field");
    if (!stock_action && !mode) return failed("Rail limits mode is off; no write issued");
    if ((!stock_action && !api_.set_raw) || (stock_action && !api_.reset)) return failed("Rail write interface unavailable");
    Lock guard(api_, gpu_);
    if (!guard.held || !identity(api_, gpu_, target_)) return failed("GPU identity or lock unavailable");
    if (!api_.hold) return failed("Hold-headroom state unavailable; no write issued");
    dc_rails_hold_record hold{}; hold.size = sizeof(hold);
    if (api_.hold(gpu_, &hold) != DC_OK || (hold.present && (hold.rail < 0 || hold.rail >= static_cast<int>(DC_RAILS_COUNT))))
        return failed("Hold-headroom state unavailable; no write issued");
    if (hold.present && hold.rail == rail)
        return failed("A hold owns this rail; release it before changing its limits");
    dc_rails_limit_row before{}; std::int32_t previous = 0;
    if (!fields(api_, gpu_, rail) || !limits(api_, gpu_, rail, before) || !before.has_base ||
        !std::isfinite(before.base[field]) || !to_uv(before.fields[field], previous))
        return failed("Selected rail field or current-card reference unavailable");
    if (expected_type && *expected_type != before.type) return failed("Rail type changed; Undo retained");
    if (stock_action) {
        std::int32_t first[4]{};
        if (!initial_type_[rail] || *initial_type_[rail] != before.type || !initial(api_, gpu_, rail, first))
            return failed("Captured initial request or its rail identity unavailable");
        value = first[field];
    }
    if (previous == value) { RailResult r; r.ok = true; r.message = "Selected request already matches"; return r; }
    RailResult result; result.undo = RailUndo{rail, field, before.type, previous};
    if (before_write) before_write(*result.undo); // a throwing capture prevents the setter
    dc_outcome outcome{}; dc_rails_setter_writes writes{}; writes.size = sizeof(writes);
    dc_status status;
    if (stock_action) {
        dc_rails_reset_request request{}; request.size = sizeof(request); request.has_fields = 1; request.field_mask = 1u << field;
        status = api_.reset(gpu_, 1, rail, &request, &outcome, &writes);
    } else {
        dc_rails_raw_request request{}; request.size = sizeof(request); request.count = 1;
        request.order[0] = field; request.passed[field] = 1; request.uv[field] = value;
        status = api_.set_raw(gpu_, rail, &request, 1, xoc ? 1 : 0, &outcome, &writes);
    }
    result.may_have_changed = writes.calls != 0 || status != DC_OK;
    if (!result.may_have_changed) result.undo.reset();
    dc_rails_limit_row after{}; std::int32_t actual = 0;
    const bool verified = identity(api_, gpu_, target_) && limits(api_, gpu_, rail, after) &&
        after.type == before.type && to_uv(after.fields[field], actual) && actual == value;
    result.ok = status == DC_OK && outcome.ok && verified;
    result.message = outcome.message[0] ? outcome.message : result.ok ? "Selected request verified" : "Rail write failed";
    if (!verified) result.message += "; selected request readback differs or is unavailable";
    return result;
}
}
