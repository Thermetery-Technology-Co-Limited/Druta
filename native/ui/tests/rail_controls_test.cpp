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
static int hold_rail = -1;
static dc_status DC_CALL lock(dc_gpu*) { ++lock_depth; return DC_OK; }
static dc_status DC_CALL unlock(dc_gpu*) { --lock_depth; return DC_OK; }
static dc_status DC_CALL id(const dc_gpu*, dc_gpu_identity* out) { *out = device; return DC_OK; }
static dc_status DC_CALL get_fields(dc_gpu*, int32_t rail, uint32_t* count) { *count = available[rail] ? 4 : 0; return DC_OK; }
static dc_status DC_CALL get_limits(dc_gpu*, dc_rails_limit_set* out) {
    if (fail_next) { fail_next = false; return DC_E_FAILED; }
    for (int r = 0; r < 2; ++r) if (available[r]) {
        out->present |= 1u << r; auto& row = out->rails[r]; row.type = types[r]; row.has_base = 1;
        for (int k = 0; k < 4; ++k) { row.fields[k] = values[r][k] / 1000.0; row.base[k] = 800 + r * 100 + k; }
    }
    if (invalid_field && available[1]) out->rails[1].fields[2] = std::numeric_limits<double>::quiet_NaN();
    return DC_OK;
}
static dc_status DC_CALL get_hold(dc_gpu*, dc_rails_hold_record* out) { out->present = hold_rail >= 0; out->rail = hold_rail; return DC_OK; }
static dc_status DC_CALL get_initial(dc_gpu*, int32_t rail, int32_t* has, int32_t out[4]) {
    *has = available[rail]; for (int k = 0; k < 4; ++k) out[k] = initial_values[rail][k]; return DC_OK;
}
static dc_status DC_CALL set_raw(dc_gpu*, int32_t rail, const dc_rails_raw_request* req, int32_t mode, int32_t xoc,
                                 dc_outcome* out, dc_rails_setter_writes* writes) {
    CHECK(lock_depth == 1 && mode == 1 && req->count == 1);
    ++write_calls; observed_xoc = !!xoc; writes->calls = 1; const auto field = req->order[0];
    CHECK(req->passed[field] == 1); values[rail][field] = req->uv[field] + (mismatch ? 1 : 0); if (throw_after_set) throw 42; out->ok = 1; return DC_OK;
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
    auto rows = rails.read();
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
    int previous_calls = write_calls; device.nvapi_devid = 1234;
    CHECK(!rails.apply(1, 0, 5, true, false).ok && write_calls == previous_calls);
    CHECK(lock_depth == 0);
    std::printf("rail_controls_test: %u checks passed\n", checks);
}
