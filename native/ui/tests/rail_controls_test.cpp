/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "rail_controls.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
using namespace druta::ui;
static unsigned checks = 0;
#define CHECK(expr) do { ++checks; if (!(expr)) { std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #expr); std::abort(); } } while (false)
static dc_gpu_identity device{};
static int32_t values[2][4] = {{100, -250, 300, 400}, {-500, 600, 700, 800}};
static int32_t initial_values[2][4] = {{10, -20, 30, 40}, {-50, 60, 70, 80}};
static int32_t types[2] = {77, 314};
static bool available[2] = {true, true}, fail_next = false, mismatch = false, invalid_field = false, throw_after_set = false;
static int write_calls = 0, reset_calls = 0, lock_depth = 0; static bool observed_xoc = false;
static int hold_rail = -1, invalid_index = 2;
static bool state_ok = true; static int state_calls = 0, limits_failures = 0;
static dc_status DC_CALL lock(dc_gpu*) { ++lock_depth; return DC_OK; }
static dc_status DC_CALL unlock(dc_gpu*) { --lock_depth; return DC_OK; }
static dc_status DC_CALL id(const dc_gpu*, dc_gpu_identity* out) { *out = device; return DC_OK; }
static dc_status DC_CALL get_fields(dc_gpu*, int32_t rail, uint32_t* count) { *count = available[rail] ? 4 : 0; return DC_OK; }
static dc_status DC_CALL get_limits(dc_gpu*, dc_rails_limit_set* out) {
    if (fail_next) { fail_next = false; return DC_E_FAILED; }
    if (limits_failures > 0) { --limits_failures; return DC_E_FAILED; }
    for (int r = 0; r < 2; ++r) if (available[r]) {
        out->present |= 1u << r; auto& row = out->rails[r]; row.type = types[r]; row.has_base = 1;
        for (int k = 0; k < 4; ++k) { row.fields[k] = values[r][k] / 1000.0; row.base[k] = 800 + r * 100 + k; }
        row.absolute_ok = state_ok && r == 0; row.boost_mv = 12.5;
    }
    if (invalid_field && available[1]) out->rails[1].fields[invalid_index] = std::numeric_limits<double>::quiet_NaN();
    return DC_OK;
}
static dc_status DC_CALL get_state(dc_gpu*, dc_rails_state_set* out) {
    ++state_calls; if (!state_ok) return DC_E_FAILED;
    out->present = 1u; out->rails[0].live = 912.5; out->rails[0].effective = 1081.25; out->rails[0].reliability = 1093.75; return DC_OK;
}
static dc_status DC_CALL get_hold(dc_gpu*, dc_rails_hold_record* out) { out->present = hold_rail >= 0; out->rail = hold_rail; return DC_OK; }
static dc_status DC_CALL get_initial(dc_gpu*, int32_t rail, int32_t* has, int32_t out[4]) {
    *has = available[rail]; for (int k = 0; k < 4; ++k) out[k] = initial_values[rail][k]; return DC_OK;
}
static uint32_t last_count = 0, last_order[4]{}; static bool mismatch_second = false;
static dc_status DC_CALL set_raw(dc_gpu*, int32_t rail, const dc_rails_raw_request* req, int32_t mode, int32_t xoc,
                                 dc_outcome* out, dc_rails_setter_writes* writes) {
    CHECK(lock_depth == 1 && mode == 1 && req->count >= 1 && req->count <= 2);
    ++write_calls; observed_xoc = !!xoc; writes->calls = 1; last_count = req->count;
    for (uint32_t i = 0; i < req->count; ++i) {
        const auto field = req->order[i]; last_order[i] = field;
        CHECK(req->passed[field] == 1);
        values[rail][field] = req->uv[field] + (mismatch || (mismatch_second && i == 1) ? 1 : 0);
    }
    if (throw_after_set) throw 42; out->ok = 1; return DC_OK;
}
static dc_status DC_CALL reset(dc_gpu*, int32_t has, int32_t rail, const dc_rails_reset_request* req,
                               dc_outcome* out, dc_rails_setter_writes* writes) {
    CHECK(lock_depth == 1 && has == 1 && req->has_fields == 1); ++reset_calls; writes->calls = 1;
    for (int k = 0; k < 4; ++k) if (req->field_mask & (1u << k)) values[rail][k] = initial_values[rail][k];
    out->ok = 1; return DC_OK;
}
int main() {
    device.size = sizeof(device); device.nvapi_ok = device.nvapi_selected = 1; device.nvapi_handle = 123;
    std::memcpy(device.slot, "0000:05:00.0", sizeof("0000:05:00.0")); std::memcpy(device.uuid, "GPU-alternate", sizeof("GPU-alternate"));
    RailApi api{}; api.lock = lock; api.unlock = unlock; api.identity = id; api.limits = get_limits;
    api.fields = get_fields; api.initial = get_initial; api.hold = get_hold; api.set_raw = set_raw; api.reset = reset;
    RailControls rails(api, reinterpret_cast<dc_gpu*>(1), device);
    // Without the optional state getter the listing still reads; no live reading is invented.
    auto shown = rails.readouts();
    CHECK(shown[0].listed && !shown[0].stale && !shown[0].state && shown[0].boost_mv == 12.5 && state_calls == 0);
    api.state = get_state;
    RailControls live_rails(api, reinterpret_cast<dc_gpu*>(1), device);
    // The control read and its write preflight never call the display-only state getter.
    auto rows = live_rails.read(); CHECK(state_calls == 0 && rows[0].fields[0].established);
    shown = live_rails.readouts(); CHECK(state_calls == 1);
    CHECK(shown[0].state && shown[0].state->live == 912.5 && shown[0].state->effective == 1081.25);
    // The core reports boost 0.0 for a rail missing from the state read (Python's "_boost_mv" default).
    CHECK(shown[0].boost_mv == 12.5 && !shown[1].state && shown[1].boost_mv == 12.5 && shown[1].listed);
    // A per-tick refresh reads only the state: listing and boost are the last full readout's.
    available[1] = false; shown = live_rails.readouts(false);
    CHECK(state_calls == 2 && shown[1].listed && !shown[0].stale);
    shown = live_rails.readouts(); CHECK(!shown[1].listed && shown[0].listed && !shown[1].boost_mv);
    available[1] = true;
    // A transient state failure keeps the last reading, marked stale, after a bounded retry.
    state_ok = false; const int before_fail = state_calls; shown = live_rails.readouts();
    CHECK(state_calls == before_fail + 2 && shown[0].stale && shown[0].state && shown[0].state->live == 912.5 && shown[0].listed);
    rows = live_rails.read(); CHECK(rows[0].fields[0].established && !rows[0].fields[0].stale);
    state_ok = true;
    // A failed limits read leaves the listing unknown instead of reporting rails absent.
    fail_next = true; shown = live_rails.readouts(); // first attempt fails, the retry succeeds
    CHECK(!shown[0].stale && shown[1].listed);
    available[1] = false; limits_failures = 2; shown = live_rails.readouts();
    CHECK(limits_failures == 0 && shown[1].listed && shown[1].stale && shown[0].stale);
    available[1] = true;
    device.nvapi_devid = 99; shown = live_rails.readouts();
    CHECK(shown[0].stale && shown[0].state); // wrong identity: retained, marked stale
    device.nvapi_devid = 0; shown = live_rails.readouts(); CHECK(!shown[0].stale);
    rows = rails.read();
    CHECK(rows[0].type == 77 && rows[1].type == 314);
    CHECK(rows[1].fields[0].requested_uv == -500 && rows[1].fields[0].initial_uv == -50);
    CHECK(rows[1].fields[0].estimated_base_mv == 900 && !rows[1].fields[0].stale);
    available[1] = false; rows = rails.read();
    CHECK(rows[0].fields[0].established && !rows[0].fields[0].stale);
    CHECK(rows[1].fields[0].established && rows[1].fields[0].stale && rows[1].fields[0].requested_uv == -500);
    available[1] = true; fail_next = true; rows = rails.read(); CHECK(!rows[0].fields[0].stale && !rows[1].fields[0].stale);
    invalid_field = true; rows = rails.read();
    CHECK(rows[1].fields[2].stale && rows[1].fields[2].requested_uv == 700 && !rows[1].fields[1].stale);
    invalid_field = false, throw_after_set = false;
    CHECK(!rails.apply(1, 2, 999, false, true).ok && write_calls == 0);
    hold_rail = 1; CHECK(!rails.apply(1, 2, 999, true, false).ok && !rails.stock(1, 2).ok && write_calls == 0 && reset_calls == 0);
    hold_rail = -1;
    auto applied = rails.apply(1, 2, -12345, true, true);
    CHECK(applied.ok && applied.may_have_changed && applied.undo && observed_xoc && values[1][2] == -12345);
    CHECK(values[1][0] == -500 && values[1][1] == 600 && values[1][3] == 800 && values[0][2] == 300);
    auto undo = *applied.undo; types[1] = 315;
    CHECK(!rails.stock(1, 2).ok && reset_calls == 0);
    CHECK(!rails.restore(undo, true, false).ok && write_calls == 1);
    types[1] = 314; CHECK(!rails.restore(undo, false, false).ok && write_calls == 1);
    CHECK(rails.restore(undo, true, false).ok && !observed_xoc && values[1][2] == 700);
    mismatch = true; auto failed = rails.apply(0, 1, 1000, true, false);
    CHECK(!failed.ok && failed.may_have_changed && failed.undo && values[0][1] == 1001);
    mismatch = false; CHECK(rails.restore(*failed.undo, true, false).ok && values[0][1] == -250);
    auto stock = rails.stock(0, 1);
    CHECK(stock.ok && stock.undo && reset_calls == 1 && values[0][1] == -20 && values[0][0] == 100);
    CHECK(rails.restore(*stock.undo, true, false).ok && values[0][1] == -250);
    std::optional<RailUndo> captured; int callbacks = 0;
    auto capture = [&](const RailUndo& u) { ++callbacks; captured = u; };
    auto noop = rails.apply(0, 0, values[0][0], true, false, capture);
    CHECK(noop.ok && callbacks == 0);
    int prior_calls = write_calls; bool threw = false;
    try { rails.apply(0, 0, 250, true, false, [](const RailUndo&) { throw 1; }); } catch (...) { threw = true; }
    CHECK(threw && write_calls == prior_calls && values[0][0] == 100 && lock_depth == 0);
    throw_after_set = true; threw = false;
    try { rails.apply(0, 0, 250, true, false, capture); } catch (...) { threw = true; }
    CHECK(threw && callbacks == 1 && captured && captured->rail == 0 && captured->field == 0 && captured->requested_uv == 100);
    CHECK(values[0][0] == 250 && lock_depth == 0);
    throw_after_set = false; CHECK(rails.restore(*captured, true, false).ok && values[0][0] == 100);
    callbacks = 0; auto stock_captured = rails.stock(0, 0, capture);
    CHECK(stock_captured.ok && callbacks == 1 && captured->requested_uv == 100 && values[0][0] == 10);
    CHECK(rails.restore(*captured, true, false).ok && values[0][0] == 100);
    // Linked reliability + alt-reliability: one setter call carrying both fields
    // in the caller's order, both read back, one Undo entry restoring both.
    int pair_calls = write_calls; callbacks = 0;
    auto pair = rails.apply_pair(0, 1, 1100, 0, 900, true, false, capture);
    CHECK(pair.ok && write_calls == pair_calls + 1 && last_count == 2 && last_order[0] == 1 && last_order[1] == 0);
    CHECK(values[0][0] == 900 && values[0][1] == 1100 && values[0][2] == 300 && values[0][3] == 400);
    CHECK(callbacks == 1 && captured->field == 1 && captured->requested_uv == -250 && captured->partner_field == 0u && captured->partner_requested_uv == 100);
    CHECK(pair.undo && pair.undo->partner_field == 0u);
    CHECK(rails.restore(*pair.undo, true, false).ok && write_calls == pair_calls + 2 && last_count == 2 && values[0][0] == 100 && values[0][1] == -250);
    // Readback must match on both fields; a partner mismatch is a failure, with Undo kept.
    mismatch_second = true; auto half = rails.apply_pair(0, 0, 950, 1, 950, true, false);
    CHECK(!half.ok && half.may_have_changed && half.undo && half.message.find("linked") != std::string::npos);
    mismatch_second = false; CHECK(rails.restore(*half.undo, true, false).ok && values[0][0] == 100 && values[0][1] == -250);
    // Refusals write nothing to either field.
    pair_calls = write_calls;
    CHECK(!rails.apply_pair(0, 0, 950, 1, 950, false, false).ok);                 // rail mode off
    CHECK(!rails.apply_pair(0, 1, 950, 1, 960, true, false).ok);                  // the same field twice
    CHECK(!rails.apply_pair(0, 0, 950, 4, 960, true, false).ok);                  // no such field
    hold_rail = 0; CHECK(!rails.apply_pair(0, 0, 950, 1, 950, true, false).ok); hold_rail = -1;
    invalid_index = 0; invalid_field = true; CHECK(!rails.apply_pair(1, 1, 650, 0, 650, true, false).ok); // partner unreadable
    invalid_field = false; invalid_index = 2;
    // Python VLIM_LINKED: only reliability (0) and alt-reliability (1) pair; every
    // other combination is refused although each field reads back fine.
    const std::uint32_t not_linked[][2] = {{0, 2}, {0, 3}, {1, 2}, {1, 3}, {2, 0}, {3, 1}, {2, 3}};
    for (const auto& p : not_linked) {
        auto refused = rails.apply_pair(0, p[0], 950, p[1], 960, true, false, capture);
        CHECK(!refused.ok && !refused.may_have_changed && !refused.undo);
    }
    CHECK(write_calls == pair_calls && values[0][0] == 100 && values[0][1] == -250 && values[0][2] == 300 && values[0][3] == 400 && values[1][1] == 600 && values[1][0] == -500);
    callbacks = 0; auto same = rails.apply_pair(0, 0, 100, 1, -250, true, false, capture);
    CHECK(same.ok && !same.undo && callbacks == 0 && write_calls == pair_calls);
    // Only one of the pair differs: both are still sent, as Python names both fields.
    auto one = rails.apply_pair(0, 0, 100, 1, -200, true, false);
    CHECK(one.ok && last_count == 2 && write_calls == pair_calls + 1 && values[0][1] == -200);
    CHECK(rails.restore(*one.undo, true, false).ok && values[0][1] == -250 && values[0][0] == 100);
    int previous_calls = write_calls; device.nvapi_devid = 1234;
    CHECK(!rails.apply(1, 0, 5, true, false).ok && write_calls == previous_calls);
    CHECK(lock_depth == 0);
    std::printf("rail_controls_test: %u checks passed\n", checks);
}
