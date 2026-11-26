/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "tuning_actions.h"
#include <cstdio>
#include <cstring>
#include <vector>
using namespace druta::ui;
namespace druta::ui { CurvePlanRequest observed_request; CurvePlan plan_curve(const PlannerApi&, const CurvePlanRequest& r) { observed_request = r; CurvePlan p{}; p.ok = r.kind == CurvePlanKind::ramp; return p; } }
namespace {
int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; std::printf("failed line %d: %s\n", __LINE__, #x); } } while (0)
dc_gpu_identity id{}; bool held = false, rail = false, pending = false, live = true, unread = false, restore_fail = false, foreign_clock = false; uint32_t uv = 0, domain = 0; std::vector<int> calls;
dc_status DC_CALL lock(dc_gpu*) { return DC_OK; } dc_status DC_CALL unlock(dc_gpu*) { return DC_OK; }
dc_status DC_CALL identity(const dc_gpu*, dc_gpu_identity* p) { *p = id; return DC_OK; }
dc_status DC_CALL status(dc_gpu*, int32_t, int64_t, dc_vf_lock_status* out) { if (unread) return DC_E_FAILED; *out = {}; out->locked = held; out->lock = {domain, 3, uv, 1}; return DC_OK; }
dc_status DC_CALL clk(dc_gpu*, dc_clk_lock_status* out) { *out = {}; out->locked = foreign_clock; return DC_OK; }
dc_status DC_CALL set(dc_gpu*, int64_t v, int32_t, int64_t d, dc_outcome* out) { calls.push_back(1); uv = static_cast<uint32_t>(v); domain = static_cast<uint32_t>(d); held = true; out->ok = 1; return DC_OK; }
dc_status DC_CALL clear(dc_gpu*, int32_t hd, int64_t d, int32_t he, int64_t v, dc_outcome* out) { calls.push_back(3); CHECK(hd && he && d == domain && v == uv && !rail); held = false; out->ok = 1; return DC_OK; }
int32_t DC_CALL recovery(const dc_gpu*) { return pending; }
dc_status DC_CALL recover(dc_gpu*, dc_outcome* out) { calls.push_back(4); pending = false; out->ok = 1; return DC_OK; }
dc_status DC_CALL record(dc_gpu*, dc_rails_hold_record* out) { out->present = rail; return DC_OK; }
dc_status DC_CALL apply(dc_gpu*, double v, double, uint32_t, int32_t, int32_t, dc_rails_before_write_fn, void*, dc_rails_hold_outcome* out, dc_rails_setter_writes*) { CHECK(held && v == uv / 1000.0); calls.push_back(2); rail = true; out->ok = DC_RAILS_HOLD_OK; return DC_OK; }
dc_status DC_CALL restore(dc_gpu*, int32_t, dc_rails_hold_outcome* out, dc_rails_setter_writes*) { calls.push_back(5); if (restore_fail) { out->ok = DC_RAILS_HOLD_FAILED; return DC_OK; } rail = false; out->ok = DC_RAILS_HOLD_OK; return DC_OK; }
dc_status DC_CALL live_ok(dc_gpu*, int32_t, const dc_rails_hold_record*, int32_t, double, dc_outcome* out) { out->ok = live; return DC_OK; }
dc_status DC_CALL overwritten(dc_gpu*, int32_t, const dc_rails_limit_set*, int32_t* out) { *out = 0; return DC_OK; }
enum class Refusal { no_set, old_core, dispatched, status_only, unknown_trace, retried,
                     undone, retained, foreign_hold, pending_recovery };
Refusal refusal;
dc_status DC_CALL refused_headroom(dc_gpu*, double, double, uint32_t, int32_t, int32_t, dc_rails_before_write_fn,
        void*, dc_rails_hold_outcome* out, dc_rails_setter_writes* writes) {
    out->ok=DC_RAILS_HOLD_FAILED;
    out->reserved=DC_RAILS_HOLD_WRITER_ISSUED_NO_SET;
    std::strcpy(out->message,"Fixture rail refusal");
    writes->calls=writes->attributes=writes->last_attributes=1;
    writes->result.size=sizeof(writes->result);
    writes->result.observed_count=1;
    std::strcpy(writes->result.observed[0],"rail packet size=972 layout=unknown");
    switch (refusal) {
    case Refusal::no_set: break;
    case Refusal::old_core: out->reserved=0; break;
    case Refusal::dispatched: writes->result.done=1; break;
    case Refusal::status_only: writes->result.has_status=1; break;
    case Refusal::unknown_trace: writes->result.size=0; break;
    case Refusal::retried: writes->calls=2; break;
    case Refusal::undone: out->ok=DC_RAILS_HOLD_NONE; writes->calls=2; break;
    case Refusal::retained: rail=true; break;
    case Refusal::foreign_hold: uv+=6250; break;
    case Refusal::pending_recovery: pending=true; break;
    }
    return DC_OK;
}
}
int main() {
    id.size = sizeof(id); id.nvapi_ok = 1; std::memcpy(id.slot, "0000:02:00.0", sizeof("0000:02:00.0")); std::memcpy(id.uuid, "GPU-test", sizeof("GPU-test"));
    TuningApi a{}; a.lock = lock; a.unlock = unlock; a.identity = identity; a.vf_status = status; a.clock_status = clk; a.vf_set = set; a.vf_clear = clear; a.recovery_pending = recovery; a.recover = recover; a.headroom_record = record; a.headroom_apply = apply; a.headroom_restore = restore; a.headroom_live = live_ok; a.headroom_overwritten = overwritten;
    auto* gpu = reinterpret_cast<dc_gpu*>(1); TuningActions t(a, gpu, id);
    CurvePlanRequest request; request.step_khz = 12656; request.max_rise_khz = 37968;
    CHECK(t.prepare_max({}, request, false).ok && observed_request.ramp_use_default_step && observed_request.step_khz == 12656 && *observed_request.max_rise_khz == 37968);
    CHECK(t.prepare_max({}, request, true).ok && !observed_request.ramp_use_default_step && observed_request.step_khz == 12656 && *observed_request.max_rise_khz == 37968);
    foreign_clock = true; CHECK(!t.hold(6, 900000, true, 25, false).ok && calls.empty()); foreign_clock = false;
    CHECK(!t.state().last_headroom_apply);
    CHECK(t.hold(6, 900000, true, 25, false).ok && held && rail && t.state().confirmed && t.state().maintenance_needed());
    const auto verified = t.state().last_headroom_apply;
    CHECK(verified && verified->outcome == HeadroomApplyOutcome::request_verified);
    unread = true; CHECK(!t.maintain().ok && rail && t.state().owned); unread = false;
    restore_fail = true; CHECK(!t.release().ok && held && rail && t.state().owned); restore_fail = false;
    CHECK(t.release().ok && !held && !rail && !t.state().maintenance_needed()); CHECK(calls[calls.size()-2] == 5 && calls.back() == 3);
    CHECK(t.state().last_headroom_apply == verified);
    CHECK(t.hold(6, 900000, true, 25, false).ok); live = false; CHECK(t.maintain().ok && held && !rail && t.state().tripped); live = true; CHECK(t.release().ok);
    CHECK(t.hold(6, 900000, true, 25, false).ok); uv = 925000; CHECK(t.maintain().ok && held && !rail && !t.state().owned); int before = static_cast<int>(calls.size()); CHECK(t.release().ok && static_cast<int>(calls.size()) == before && held); held = false;
    pending = true; before = static_cast<int>(calls.size()); CHECK(!t.release().ok && pending && static_cast<int>(calls.size()) == before); pending = false;
    rail = true; before = static_cast<int>(calls.size()); CHECK(!t.hold(6, 900000, true, 25, false).ok && static_cast<int>(calls.size()) == before); rail = false;
    CHECK(t.hold(6, 900000, true, 25, false).ok); id.nvapi_devid = 123; before = static_cast<int>(calls.size()); CHECK(!t.release().ok && static_cast<int>(calls.size()) == before && held && rail); id.nvapi_devid = 0;
    live = false; restore_fail = true; CHECK(!t.maintain().ok && held && rail && t.state().tripped); restore_fail = false; CHECK(t.maintain().ok && held && !rail); live = true; CHECK(t.release().ok);
    TuningApi no_rails = a; no_rails.headroom_record = nullptr; no_rails.headroom_apply = nullptr; no_rails.headroom_restore = nullptr; no_rails.headroom_live = nullptr; no_rails.headroom_overwritten = nullptr;
    TuningActions bare(no_rails, gpu, id); CHECK(!bare.hold(6, 900000, true, 25, false).ok); CHECK(bare.hold(6, 900000, false, 25, false).ok); CHECK(bare.maintain().ok && bare.release().ok);
    a.headroom_apply=refused_headroom;
    TuningActions max(a,gpu,id);
    CHECK(!max.state().last_headroom_apply);
    for (const auto sample:{Refusal::no_set,Refusal::old_core,Refusal::dispatched,Refusal::status_only,
            Refusal::unknown_trace,Refusal::retried,Refusal::undone,Refusal::retained,Refusal::foreign_hold,
            Refusal::pending_recovery}) {
        refusal=sample;
        const auto outcome=max.hold(6,900000,true,25,false,true);
        CHECK(outcome.may_have_changed && outcome.ok==(sample==Refusal::no_set));
        CHECK((outcome.message=="V/F hold confirmed. Optional headroom skipped: Fixture rail refusal")
              ==(sample==Refusal::no_set));
        CHECK(max.state().headroom==(sample==Refusal::retained));
        CHECK(max.state().recovery==(sample==Refusal::pending_recovery));
        const auto evidence = max.state().last_headroom_apply;
        CHECK(evidence && evidence->target_uv == 900000 && evidence->margin_mv == 25);
        CHECK(evidence && evidence->detail == "Fixture rail refusal");
        const bool no_set = sample == Refusal::no_set || sample == Refusal::foreign_hold || sample == Refusal::pending_recovery;
        CHECK(evidence && evidence->outcome == (no_set ? HeadroomApplyOutcome::no_set_issued : HeadroomApplyOutcome::unverified));
        CHECK(evidence && evidence->last_writer_observations.size() == (sample == Refusal::unknown_trace ? 0u : 1u));
        CHECK(max.release().ok);
        CHECK(max.state().last_headroom_apply == evidence);
        if (sample==Refusal::foreign_hold) { CHECK(held && uv==906250); held=false; }
        else CHECK(!held && !rail && !pending);
    }
    refusal=Refusal::no_set;
    CHECK(!max.hold(6,900000,true,25,false).ok && max.state().confirmed && held);
    CHECK(max.release().ok);
    a.headroom_apply = [](dc_gpu*, double, double, uint32_t, int32_t, int32_t xoc, dc_rails_before_write_fn,
                          void*, dc_rails_hold_outcome* out, dc_rails_setter_writes*) -> dc_status {
        out->ok = xoc ? DC_RAILS_HOLD_OK : DC_RAILS_HOLD_NONE;
        return DC_OK;
    };
    TuningActions no_writer(a,gpu,id);
    CHECK(!no_writer.hold(6,900000,true,25,false).ok && no_writer.state().last_headroom_apply->outcome == HeadroomApplyOutcome::skipped_before_writer);
    CHECK(no_writer.release().ok);
    CHECK(no_writer.hold(6,900000,true,25,false,true).ok && held && !rail &&
          no_writer.state().last_headroom_apply->outcome == HeadroomApplyOutcome::skipped_before_writer);
    CHECK(no_writer.release().ok && !held);
    CHECK(no_writer.hold(6,900000,true,25,true).ok && no_writer.state().last_headroom_apply->outcome == HeadroomApplyOutcome::already_sufficient);
    CHECK(no_writer.release().ok);
    a.headroom_record=nullptr; a.headroom_apply=nullptr;
    TuningActions partial(a,gpu,id);
    CHECK(!partial.hold(6,900000,true,25,false).ok && !held);
    CHECK(partial.hold(6,900000,true,25,false,true).ok && held && !rail &&
          partial.state().last_headroom_apply->outcome == HeadroomApplyOutcome::skipped_before_writer);
    CHECK(partial.release().ok && !held);
    std::printf("%d checks, %d failures\n", checks, failures); return failures ? 1 : 0;
}
