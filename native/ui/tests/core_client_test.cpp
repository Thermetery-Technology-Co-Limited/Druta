/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "core_client.h"
#include "profile_io.h"
#include "scoped_profile.h"
#include "clock_monitor_model.h"
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <cstdio>
#define CHECK(x) do { ++checks; if (!(x)) { std::fprintf(stderr, "CHECK failed at line %d: %s\n", __LINE__, #x); ++failures; } } while (0)
int failures = 0, checks = 0, boost_writes = 0;
#include <cstring>
using namespace druta::ui;
namespace {
int current = 40, writes = 0, mode = 0, range_fail = 0;
int core_low = -1000, core_high = 2000, mem_low = -1000, mem_high = 2000, telemetry_fail = 0; uint32_t memory_p0 = 8000, memory_clock = 0; bool changed_id = false, no_device = false;
int curve_div = 1, voltage_shift = 0, base_shift = 0, layout_shift = 0, curve_read_fail = 0;
int clock_step_fail = 0; uint32_t test_clock_step = 1000; int32_t test_clock_measured = 1; bool nonlinear_curve = false;
int core_value = 30, boost_value = 10, delta = 123, delta2 = 456, curve_failure = 0, curve_writes = 0, fan_mode = 0, fan_level = 40; bool fan_auto = false, fan_refuse = false, fan_post_fail = false, curve_corrupt = false, curve_geometry_corrupt = false; int fan_read_fail = 0;
dc_gpu* handle = reinterpret_cast<dc_gpu*>(1);
uint32_t DC_CALL version() { return DC_ABI_VERSION; }
dc_status DC_CALL enumerate(dc_gpu_entry*, size_t, size_t* n) { *n = 0; return DC_OK; }
dc_status DC_CALL open_gpu(const char*, dc_gpu** out) { *out = handle; return DC_OK; }
void DC_CALL close_gpu(dc_gpu*) {}
dc_status DC_CALL identity(const dc_gpu*, dc_gpu_identity* out) {
    out->nvml_ok = no_device ? 0 : 1; std::memcpy(out->slot, "0000:02:00.0", sizeof("0000:02:00.0")); const char* uuid = changed_id ? "other" : "GPU-A"; std::memcpy(out->uuid, uuid, 6); return DC_OK;
}
dc_status DC_CALL range(dc_gpu*, uint32_t type, dc_offset_range* out) {
    if (range_fail) { --range_fail; return DC_E_UNAVAILABLE; }
    out->present = 1; out->low = type == 0 ? core_low : mem_low; out->high = type == 0 ? core_high : mem_high; out->current = type == 0 ? core_value : current; return DC_OK;
}
dc_status DC_CALL memory_telemetry(dc_gpu*, dc_telemetry* out) { if (telemetry_fail > 0) { --telemetry_fail; return DC_E_UNAVAILABLE; } out->present = DC_T_MEM_P0MAX | DC_T_MEM; out->mem_p0max = memory_p0; out->mem = memory_clock; return DC_OK; }
dc_status DC_CALL clock_telemetry(dc_gpu*, dc_telemetry* out) {
    out->present=DC_T_PRIV_CLOCKS|DC_T_CORE; out->core=1500; return DC_OK;
}
dc_status DC_CALL clock_classifier(const uint32_t*, size_t, int32_t, uint32_t, int32_t, uint32_t, int32_t, uint32_t, dc_clock_domains* out) {
    out->count=1; auto& row=out->rows[0]; row.domain=0; row.scale=1;
    std::memcpy(row.name,"GPC",4); std::memcpy(row.grade,"confirmed",10); std::memcpy(row.kind,"freq",5);
    row.prog_khz=1500000; row.meas_khz=1499000; return DC_OK;
}
dc_status DC_CALL scale(dc_gpu*, dc_mem_offset_scale* out) { out->scale = 2; std::memcpy(out->unit, "MHz eff", sizeof("MHz eff")); return DC_OK; }
uint32_t DC_CALL step(dc_gpu*) { return 2; }
dc_status DC_CALL setter(dc_gpu*, uint32_t type, double value, int32_t preserve, dc_outcome* out) {
    CHECK(preserve == 1); ++writes;
    if (type == 0) { core_value = static_cast<int>(value); delta = 999; out->ok = 1; return DC_OK; }
    if (mode == 1) { current = static_cast<int>(value * 2) - 2; out->ok = 1; }
    else if (mode == 2) { out->ok = 0; }
    else { current = static_cast<int>(value * 2); out->ok = 1; }
    if (mode == 3) range_fail = 2;
    return DC_OK;
}
dc_status DC_CALL clock_step(dc_gpu*, uint32_t* v, int32_t* m) { if (clock_step_fail > 0) { --clock_step_fail; return DC_E_UNAVAILABLE; } *v = test_clock_step; *m = test_clock_measured; return DC_OK; }
dc_status DC_CALL snapshot(dc_gpu*, dc_vf_delta_snapshot* o) { o->freq_div = curve_div; o->count = 2; o->deltas[0] = {3, delta}; o->deltas[1] = {4, delta2}; return DC_OK; }
dc_status DC_CALL deltas(dc_gpu*, const dc_vf_delta* d, size_t n, dc_outcome* o) { ++curve_writes; if (!curve_failure) { for (size_t i = 0; i < n; ++i) { if (d[i].idx == 3) delta = static_cast<int>(d[i].delta_khz); else if (d[i].idx == 4) delta2 = static_cast<int>(d[i].delta_khz); } if (curve_corrupt) ++delta2; if (curve_geometry_corrupt) ++voltage_shift; } o->ok = !curve_failure; return DC_OK; }
dc_status DC_CALL curve_layout(dc_gpu*, int32_t, dc_vf_layout* l) { l->n_entries = 7; l->gpu_count = 2; l->gpu_idx[0] = 3; l->gpu_idx[1] = 4; l->other_count = 1; l->other_idx[0] = 5 + layout_shift; l->freq_div = curve_div; return DC_OK; }
dc_status DC_CALL curve_read(dc_gpu*, dc_vf_curve* c) { if (curve_read_fail > 0) { --curve_read_fail; return DC_E_UNAVAILABLE; } c->count = 2; c->points[0] = {3, 900.0 + voltage_shift, (nonlinear_curve ? 2010.0 : 1500.0) + base_shift + delta / (1000.0 * curve_div), delta}; c->points[1] = {4, 950, (nonlinear_curve ? 2010.0 : 1600.0) + delta2 / (1000.0 * curve_div) + (nonlinear_curve ? delta / (1000.0 * curve_div) : 0), delta2}; return DC_OK; }
dc_status DC_CALL envelopes(dc_request_envelopes* e) { e->max_abs_delta_khz = 1000000; e->rail_offset_max_mv = 100; e->rail_offset_xoc_max_mv = 500; e->power_limit_xoc_max_mw = 1999000; return DC_OK; }
dc_status DC_CALL curve_reset(dc_gpu*, dc_outcome* o) { ++curve_writes; delta = delta2 = 0; o->ok = 1; return DC_OK; }
dc_status DC_CALL fan_get(dc_gpu*, dc_fan_state* s) { if (fan_mode == 2 || fan_read_fail > 0) { if (fan_read_fail > 0) --fan_read_fail; return DC_E_UNAVAILABLE; } std::memcpy(s->source, "nvml", 5); s->count = 2; for (int i = 0; i < 2; ++i) { s->fans[i].id = i; s->fans[i].level = fan_level + (fan_mode == 1 ? i : 0); s->fans[i].policy = fan_auto ? 0u : 1u; s->fans[i].default_policy = 0; s->fans[i].manual = !fan_auto; s->fans[i].min = 0; s->fans[i].max = 100; } return DC_OK; }
dc_status DC_CALL fan_caps(dc_gpu*, dc_fan_capabilities* c) { c->manual = 1; c->automatic = 1; c->max = 100; return DC_OK; }
dc_status DC_CALL fan_set(dc_gpu*, int32_t x, dc_outcome* o) { if (!fan_refuse) { fan_level = x; fan_mode = 0; fan_auto = false; } if (fan_post_fail) fan_read_fail = 2; o->ok = 1; return DC_OK; }
dc_status DC_CALL fan_reset(dc_gpu*, dc_outcome* o) { fan_level = 20; fan_auto = true; if (fan_mode == 2) fan_mode = 0; o->ok = 1; return DC_OK; }
dc_status DC_CALL fan_restore(dc_gpu*, const dc_fan_snapshot* s, dc_outcome* o) { CHECK(s->count == 2); fan_level = s->fans[0].level; fan_mode = s->fans[1].level != s->fans[0].level ? 1 : 0; fan_auto = !s->fans[0].manual; o->ok = 1; return DC_OK; }
dc_status DC_CALL boost_get(dc_gpu*, int32_t* v) { *v = boost_value; return DC_OK; }
dc_status DC_CALL boost_set(dc_gpu*, int64_t v, dc_outcome* o) { ++boost_writes; boost_value = static_cast<int>(v); o->ok = 1; return DC_OK; }
uint32_t board = 200000; int64_t peer = 100, pinned_value = 100; bool table_fail = false, board_mismatch = false, peer_fail = false, geometry_change = false, xoc_live = false; bool pin_present = true; int power_writes = 0, policy_writes = 0, policy_reads = 0, pin_writes = 0; bool policy_external_test = false, policy_change_board = false, policy_drop_pin = false;
bool derived_test = false, callback_matches = true, callback_answer = false, mutable_bounds = false, power_throw = false;
int mutate_power_at_policy_read = 0;
dc_status DC_CALL cpc_live(dc_gpu*, dc_cpc_observation* out, dc_error*) { CHECK(out->size == sizeof(*out) && out->metadata.size == sizeof(out->metadata) && out->arbitration.size == sizeof(out->arbitration)); out->metadata.role_info_size = 5; out->metadata.role_info[4] = 0x30; out->metadata.maximum_mw = 500000; out->requested_mw = 600000; out->metadata.native_info_size = 4; out->native_control_size = 4; uint32_t a = callback_matches ? 42u : 43u, b = 56; std::memcpy(out->metadata.native_info, &a, 4); std::memcpy(out->native_control, &b, 4); return DC_OK; }
dc_status DC_CALL lock_gpu(dc_gpu*) { return DC_OK; }
dc_status DC_CALL unlock_gpu(dc_gpu*) { return DC_OK; }
dc_status DC_CALL capabilities(dc_gpu*, dc_capabilities* c) { c->present = DC_C_PL_MIN | DC_C_PL_MAX | DC_C_PL_DEF; c->pl_min_mw = 100000; c->pl_max_mw = 500000; c->pl_def_mw = 200000; return DC_OK; }
dc_status DC_CALL power_read(dc_gpu*, dc_cl_statics*, int32_t* has, uint32_t* value, dc_error*) { *has = 1; *value = board; return DC_OK; }
dc_status DC_CALL bounds(dc_gpu*, dc_cl_statics*, int32_t xoc, int64_t* low, int64_t* high, dc_error*) { *low = 100000; *high = xoc && xoc_live ? 1999000 : 500000; if (board > *high) *high = board; return DC_OK; }
dc_status DC_CALL power_set(dc_gpu*, dc_cl_statics*, int64_t value, int32_t, int32_t* ok, char*, size_t, size_t* needed) { ++power_writes; board = static_cast<uint32_t>(value) + (board_mismatch ? 1000 : 0); peer = derived_test ? 700 : 200; if (power_throw) throw std::runtime_error("post-dispatch power failure"); *ok = 1; *needed = 1; return DC_OK; }
dc_status DC_CALL message(dc_gpu*, char* out, size_t, size_t* n) { out[0] = 0; *n = 1; return DC_OK; }
dc_status DC_CALL pins_get(dc_gpu*, uint32_t* ids, int64_t* vals, uint32_t, uint32_t* n) { *n = pin_present ? 1 : 0; if (*n) { ids[0] = 1; vals[0] = pinned_value; } return DC_OK; }
dc_status DC_CALL pins_set(dc_gpu*, const uint32_t* ids, const int64_t* values, uint32_t n) { ++pin_writes; if (n) { CHECK(n == 1 && ids[0] == 1); pinned_value = values[0]; } pin_present = n != 0; return DC_OK; }
dc_status DC_CALL policies(dc_gpu*, dc_cl_confirm_fn confirm, void* confirm_context, dc_cl_policy_inputs_fn input, void* context, dc_policy_row* rows, uint32_t, uint32_t* n, dc_error*) {
    ++policy_reads; if (mutate_power_at_policy_read==policy_reads) peer=101;
    CHECK(confirm != nullptr); if (table_fail) return DC_E_FAILED;
    if (derived_test) { uint32_t info = 42, control = 56; callback_answer = confirm(confirm_context, 1, 700, 500, &info, 1, &control, 1) != 0; }
    dc_cl_statics st{}; st.size = sizeof(st); int32_t has = 0; int64_t mw = 0; CHECK(input(context, &has, &mw, &st) == 0 && has && mw == board);
    *n = 2; rows[0].policy = 0; rows[0].board = 1; rows[0].unit = 0; rows[0].minimum = 100000; rows[0].maximum = 350000; rows[0].requested = board;
    rows[1].policy = 1; rows[1].unit = 1; rows[1].type = geometry_change ? 1 : 0; rows[1].minimum = 0; rows[1].maximum = geometry_change || (mutable_bounds && board != 200000) ? 501 : 500; rows[1].requested = peer; rows[1].writable = 1; rows[1].named_current = derived_test; rows[1].consistent = 1; return DC_OK;
}
dc_status DC_CALL policy_set(dc_gpu*, dc_cl_confirm_fn confirm, void*, dc_cl_policy_inputs_fn, void*, int64_t id, int32_t whole, int64_t value, int32_t pin, int32_t, int32_t* ok, char*, size_t, size_t* needed) {
    CHECK(confirm && id == 1 && whole && (policy_external_test || !pin_present)); ++policy_writes; *ok = !peer_fail; if (!peer_fail) { peer = value; if (pin) { pin_present = true; pinned_value = value; } if (policy_change_board) board += 1000; if (policy_drop_pin) pin_present = false; } *needed = 1; return DC_OK;
}
int domain_raw[32]{}, rail_raw[2]{}, advanced_writes = 0, xoc_flag = 0, advanced_fail_reads = 0, layout_version = 4; bool has_msvdd = false, advanced_refuse = false, flags_fail = false;
int32_t DC_CALL dom_ok(dc_gpu*) { return 1; }
dc_status DC_CALL dom_layout(dc_gpu*, dc_clkdom_layout* l) { l->version = layout_version; l->block_size = 100; l->header = 4; l->stride = 12; l->freq_khz = 4; l->has_nvvdd_uv = 1; return DC_OK; }
dc_status DC_CALL dom_list(dc_gpu*, uint32_t* out, size_t, size_t* n) { out[0] = 0; out[1] = 1; out[2] = 2; out[3] = 5; *n = 4; return DC_OK; }
dc_status DC_CALL dom_controls(dc_gpu*, uint32_t* out, size_t, size_t* n) { out[0] = 1; out[1] = 2; out[2] = 5; *n = 3; return DC_OK; }
int32_t DC_CALL dom_offered(dc_gpu*, int64_t) { return 1; }
dc_status DC_CALL dom_read(dc_gpu*, dc_clkdom_requests* out) { if (advanced_fail_reads > 0) { --advanced_fail_reads; return DC_E_UNAVAILABLE; } out->count = 4; for (int i = 0; i < 4; ++i) { out->rows[i].domain = i == 3 ? 5 : i; out->rows[i].freq_khz = domain_raw[out->rows[i].domain]; } return DC_OK; }
dc_status DC_CALL dom_set(dc_gpu*, int64_t id, double value, dc_outcome* out) { ++advanced_writes; if (!advanced_refuse) domain_raw[id] = static_cast<int>(value * 1000); out->ok = !advanced_refuse; return DC_OK; }
dc_status DC_CALL rail_cap(dc_gpu*, int64_t rail, int32_t selected, int64_t domain, dc_rail_offset_capability* out) { if (rail == 1 && !has_msvdd) return DC_OK; out->available = out->has_domain = out->has_value = 1; out->domain = selected ? domain : (rail ? 5 : 0); out->value_mv = rail_raw[rail] / 1000.0; return DC_OK; }
dc_status DC_CALL rail_read(dc_gpu*, int64_t, int64_t rail, double* value) { *value = rail_raw[rail] / 1000.0; return DC_OK; }
dc_status DC_CALL rail_set(dc_gpu*, double value, int64_t, int64_t rail, dc_outcome* out) { ++advanced_writes; if (!advanced_refuse) rail_raw[rail] = static_cast<int>(value * 1000); out->ok = !advanced_refuse; return DC_OK; }
dc_status DC_CALL rail_reset(dc_gpu*, int64_t, dc_outcome* out) { ++advanced_writes; rail_raw[1] = 0; out->ok = 1; return DC_OK; }
dc_status DC_CALL flags_set(dc_gpu*, int32_t a, int32_t b) { CHECK(a == b); xoc_flag = a; return DC_OK; }
dc_status DC_CALL flags_get(dc_gpu*, int32_t* a, int32_t* b) { if (flags_fail) return DC_E_UNAVAILABLE; *a = *b = xoc_flag; return DC_OK; }
int rail_type = 7, rail_limits_calls = 0, rail_type_flip_at = 0, rail_request_flip_at = 0; double rail_base = 900;
int raw_limits[2][4] = {{100, 200, 300, 400}, {500, 600, 700, 800}}; bool override_rail_initial=false; int rail_initial_override=0; bool rail_fields_fail = false, rail_throw = false; int rail_set_calls = 0, rail_failed_index = -1, rail_held = -1;
dc_status DC_CALL rail_fields(dc_gpu*, int32_t rail, uint32_t* count) { if (rail_fields_fail || rail == rail_failed_index) return DC_E_UNAVAILABLE; *count = 4; return DC_OK; }
dc_status DC_CALL rail_limits(dc_gpu*, dc_rails_limit_set* out) { ++rail_limits_calls; if (rail_type_flip_at && rail_limits_calls == rail_type_flip_at) ++rail_type; if (rail_request_flip_at && rail_limits_calls == rail_request_flip_at) raw_limits[1][2]=850; for (int i = 0; i < 2; ++i) { out->present |= 1u << i; out->rails[i].type = i + rail_type; out->rails[i].has_base = 1; for (int j = 0; j < 4; ++j) { out->rails[i].fields[j] = raw_limits[i][j] / 1000.0; out->rails[i].base[j] = rail_base; } } return DC_OK; }
dc_status DC_CALL rail_initial(dc_gpu*, int32_t rail, int32_t* has, int32_t values[4]) { *has = 1; for (int i = 0; i < 4; ++i) values[i] = override_rail_initial && rail==1 && i==2 ? rail_initial_override : (rail * 4 + i + 1) * 100; return DC_OK; }
dc_status DC_CALL rail_hold(dc_gpu*, dc_rails_hold_record* out) { out->present = rail_held >= 0; out->rail = rail_held; return DC_OK; }
dc_status DC_CALL rail_raw_set(dc_gpu*, int32_t rail, const dc_rails_raw_request* request, int32_t enabled, int32_t, dc_outcome* out, dc_rails_setter_writes* write_info) { CHECK(enabled && request->count == 1); ++rail_set_calls; auto field = request->order[0]; raw_limits[rail][field] = request->uv[field]; write_info->calls = 1; if (rail_throw) throw std::runtime_error("post-write failure"); out->ok = 1; return DC_OK; }
dc_status DC_CALL rail_initial_set(dc_gpu*, int32_t, int32_t rail, const dc_rails_reset_request* request, dc_outcome* out, dc_rails_setter_writes* write_info) { for (int i = 0; i < 4; ++i) if (request->field_mask & (1u << i)) raw_limits[rail][i] = (rail * 4 + i + 1) * 100; write_info->calls = 1; out->ok = 1; return DC_OK; }
uint32_t current_index = 7; bool current_read_fails = false, current_generic_label = false;
dc_status DC_CALL alternate_policies(dc_gpu*, dc_cl_confirm_fn confirm, void*, dc_cl_policy_inputs_fn, void*, dc_policy_row* rows, uint32_t, uint32_t* n, dc_error*) {
    CHECK(confirm); *n = 2; rows[0].policy = 0; rows[0].board = 1; rows[0].unit = 0; rows[0].minimum = 100000; rows[0].maximum = 500000; rows[0].requested = board;
    rows[1].policy = current_index; rows[1].channel = 17; rows[1].unit = 1; rows[1].minimum = 1000; rows[1].maximum = 500000; rows[1].default_value = 150000; rows[1].requested = 120000; rows[1].named_current = 1; rows[1].consistent = 1; return DC_OK;
}
dc_status DC_CALL core_current(dc_gpu*, dc_cl_confirm_fn confirm, void*, dc_current_row* rows, uint32_t, uint32_t* n) {
    CHECK(confirm); if (current_read_fails) return DC_E_FAILED; *n = 1; rows[0].policy = current_index; rows[0].channel = 17; rows[0].minimum_ma = 1000; rows[0].maximum_ma = 500000; rows[0].requested_ma = 120000;
    if (current_generic_label) std::memcpy(rows[0].label, "Other current", sizeof("Other current")); else std::memcpy(rows[0].label, "Core current", sizeof("Core current")); return DC_OK;
}
dc_status DC_CALL no_pins(dc_gpu*, uint32_t*, int64_t*, uint32_t, uint32_t* n) { *n = 0; return DC_OK; }
bool test_vf_held = false, test_foreign_clock = false, reset_step_failure = false, reset_api_failure = false; uint32_t test_vf_uv = 0, test_vf_domain = 0; int reset_calls = 0;
dc_status DC_CALL tuning_vf_status(dc_gpu*, int32_t, int64_t, dc_vf_lock_status* s) { *s = {}; s->locked = test_vf_held; s->lock = {test_vf_domain, 3, test_vf_uv, 1}; return DC_OK; }
dc_status DC_CALL tuning_clock_status(dc_gpu*, dc_clk_lock_status* s) { *s = {}; s->locked = test_foreign_clock; return DC_OK; }
dc_status DC_CALL tuning_vf_set(dc_gpu*, int64_t v, int32_t, int64_t d, dc_outcome* o) { test_vf_held = true; test_vf_uv = static_cast<uint32_t>(v); test_vf_domain = static_cast<uint32_t>(d); o->ok = 1; return DC_OK; }
dc_status DC_CALL tuning_vf_clear(dc_gpu*, int32_t, int64_t, int32_t, int64_t, dc_outcome* o) { test_vf_held = false; o->ok = 1; return DC_OK; }
int32_t DC_CALL tuning_recovery(const dc_gpu*) { return 0; }
dc_status DC_CALL tuning_recover(dc_gpu*, dc_outcome* o) { o->ok = 1; return DC_OK; }
dc_status DC_CALL tuning_record(dc_gpu*, dc_rails_hold_record* r) { r->present = 0; return DC_OK; }
dc_status DC_CALL model_rise(dc_gpu*, uint32_t* out) { *out = 300000; return DC_OK; }
dc_status DC_CALL test_ramp(const dc_vf_point*, size_t, const dc_vfp_num*, const dc_vfp_num*, const dc_vfp_num*, const dc_vfp_num*, dc_vfp_ramp* out) { out->change_count = 0; return DC_OK; }
dc_status DC_CALL test_predict(const dc_vf_point*, size_t n, const dc_vfp_num* values, const dc_vfp_num*, int64_t* actual, uint32_t* order, size_t capacity) { if (capacity < n) return DC_E_BUFFER; for (size_t i = 0; i < n; ++i) { order[i] = static_cast<uint32_t>(i); actual[i] = values[i].i; } return DC_OK; }
dc_status DC_CALL test_reset(dc_gpu*, dc_cl_confirm_fn, void*, dc_cl_policy_inputs_fn, void*, dc_reset_statics_fn, void*, int32_t, dc_reset_step* steps, uint32_t cap, uint32_t* count, dc_rails_setter_writes*, dc_error*) {
    ++reset_calls; CHECK(cap >= DC_RESET_STEPS_MAX); core_value = 0; delta = 0; delta2 = 0;
    if (reset_api_failure) { *count = 0; return DC_E_FAILED; }
    *count = 2; std::memcpy(steps[0].name, "core offset", sizeof("core offset")); steps[0].ok = 1;
    std::memcpy(steps[1].name, "fan", sizeof("fan")); steps[1].ok = reset_step_failure ? 0 : 1; return DC_OK;
}
int perf_group_reads=0; bool perf_partial_failure=false;
dc_status DC_CALL perf_read_group(dc_gpu*,const char* name,uint32_t attempts,dc_perf_group_result* result,dc_error* error) {
    ++perf_group_reads; CHECK(attempts==2 && result->size==sizeof(*result) && result->snapshot.size==sizeof(result->snapshot));
    if (perf_partial_failure && std::strcmp(name,"cf_policy")==0) {
        result->status=DC_PERF_STATUS_FAILED; error->kind=DC_ERR_TRANSIENT; std::strcpy(error->message,"temporary failure"); return DC_OK;
    }
    result->has_target=1; std::strcpy(result->slot,"0000:02:00.0"); result->architecture=7;
    result->status=DC_PERF_STATUS_EMPTY; result->snapshot.capacity=1; std::strcpy(result->snapshot.group,name); return DC_OK;
}
bool freq_locked=false, freq_reset_fails=false; uint32_t freq_min=0,freq_max=0; int freq_sets=0,freq_resets=0;
dc_status DC_CALL freq_arch(dc_gpu*,uint32_t* value) {*value=7;return DC_OK;}
dc_status DC_CALL freq_export(const char*,const char*,void** out) {*out=reinterpret_cast<void*>(1);return DC_OK;}
dc_status DC_CALL freq_caps(dc_gpu*,dc_capabilities* c) {c->present=DC_C_GFX_MIN|DC_C_GFX_MAX;c->gfx_min=300;c->gfx_max=900;return DC_OK;}
dc_status DC_CALL freq_table(dc_gpu*,dc_clock_row* rows,size_t capacity,size_t* count) {
    *count=1; if(!rows || capacity==0)return DC_OK;
    rows[0].mem_mhz=7000;rows[0].count=3;rows[0].graphics[0]=300;rows[0].graphics[1]=600;rows[0].graphics[2]=900;return DC_OK;
}
dc_status DC_CALL freq_status(dc_gpu*,dc_clk_lock_status* status) {
    *status={};status->locked=freq_locked;status->min_mhz=freq_min;status->max_mhz=freq_max;return DC_OK;
}
dc_status DC_CALL freq_set(dc_gpu*,int64_t low,int64_t high,dc_outcome* out) {
    ++freq_sets;freq_min=static_cast<uint32_t>(low);freq_max=static_cast<uint32_t>(high);freq_locked=true;out->ok=1;return DC_OK;
}
dc_status DC_CALL freq_last(dc_gpu*,uint32_t* low,uint32_t* high) {*low=freq_min;*high=freq_max;return DC_OK;}
dc_status DC_CALL freq_reset(dc_gpu*,int32_t,dc_outcome* out) {
    ++freq_resets;if(freq_reset_fails){out->ok=0;return DC_E_FAILED;}freq_locked=false;out->ok=1;return DC_OK;
}
CoreApi api() { CoreApi a{}; a.abi_version = version; a.enumerate = enumerate; a.open = open_gpu; a.close = close_gpu; a.identity = identity; a.range = range; a.scale = scale; a.step = step; a.set_offset = setter; a.clock_step = clock_step; a.snapshot = snapshot; a.apply_deltas = deltas; a.curve_layout = curve_layout; a.curve = curve_read; a.envelopes = envelopes; a.reset_curve = curve_reset; a.fan_state = fan_get; a.fan_caps = fan_caps; a.fan_set = fan_set; a.fan_reset = fan_reset; a.fan_restore = fan_restore; a.boost = boost_get; a.boost_set = boost_set; return a; }
Command command(Command::Operation op, double value = 0) { return {op, ControlId::memory, value}; }
#if defined(_MSC_VER)
#define TEST_NOINLINE __declspec(noinline)
#else
#define TEST_NOINLINE __attribute__((noinline))
#endif
TEST_NOINLINE void perf_frequency_integration_tests() {
    std::string error;
    // Diagnostics use only explicit refresh; ordinary read retains a prior
    // immutable publication, including a partial refresh with stale data.
    auto integration_api=api(); integration_api.perf.identity=identity; integration_api.perf.read_group=perf_read_group;
    integration_api.lock=lock_gpu;integration_api.unlock=unlock_gpu;
    integration_api.tuning.lock=lock_gpu;integration_api.tuning.unlock=unlock_gpu;integration_api.tuning.identity=identity;
    integration_api.tuning.vf_status=tuning_vf_status;integration_api.tuning.clock_status=tuning_clock_status;
    integration_api.tuning.vf_set=tuning_vf_set;integration_api.tuning.vf_clear=tuning_vf_clear;
    integration_api.tuning.recovery_pending=tuning_recovery;integration_api.tuning.recover=tuning_recover;
    integration_api.tuning.headroom_record=tuning_record;
    integration_api.frequency.lock=lock_gpu;integration_api.frequency.unlock=unlock_gpu;integration_api.frequency.identity=identity;
    integration_api.frequency.architecture=freq_arch;integration_api.frequency.export_address=freq_export;
    integration_api.frequency.capabilities=freq_caps;integration_api.frequency.lockable_clocks=freq_table;
    integration_api.frequency.clock_status=freq_status;integration_api.frequency.clocks_lock=freq_set;
    integration_api.frequency.last_lock=freq_last;integration_api.frequency.clocks_reset=freq_reset;
    auto integrated=create_core_backend_for_test(integration_api);CHECK(integrated->open("0000:02:00.0",error));
    integrated->read(false);integrated->read(true);CHECK(perf_group_reads==0);
    Command perf_refresh{};perf_refresh.operation=Command::Operation::refresh_perf_diagnostics;
    CHECK(integrated->execute(perf_refresh).ok && perf_group_reads==3);
    const auto prior_perf=integrated->read(false).perf_diagnostics;
    CHECK(prior_perf && prior_perf->groups[2].freshness==PerfFreshness::current);
    perf_partial_failure=true;CHECK(!integrated->execute(perf_refresh).ok && perf_group_reads==6);
    const auto partial_perf=integrated->read(false).perf_diagnostics;
    CHECK(partial_perf && partial_perf->groups[2].freshness==PerfFreshness::stale && partial_perf->groups[2].snapshot
        && prior_perf->groups[2].freshness==PerfFreshness::current && perf_group_reads==6);
    perf_partial_failure=false;
    Command frequency{};frequency.operation=Command::Operation::frequency_lock_apply;frequency.frequency_min_mhz=600;frequency.frequency_max_mhz=900;
    CHECK(integrated->execute(frequency).ok && freq_sets==1 && freq_locked);
    auto owned_frequency=integrated->frequency_state();
    CHECK(owned_frequency && owned_frequency->ownership.needs_cleanup() && owned_frequency->ownership.confirmed);
    const auto hold_input=integrated->read(true);
    freq_reset_fails=true; Command competing{};competing.operation=Command::Operation::hold_curve_point;
    competing.expected_curve=hold_input.curve; competing.expected_curve_freq_div=hold_input.curve_freq_div;
    competing.hold_point_index=3; competing.hold_headroom=false;
    const int prior_resets=freq_resets;test_vf_held=false;
    CHECK(!integrated->execute(competing).ok && freq_resets==prior_resets+1 && freq_locked && !test_vf_held);
    CHECK(!integrated->prepare_close().ok && integrated->frequency_state()->ownership.needs_cleanup());
    freq_reset_fails=false;CHECK(integrated->prepare_close().ok && !freq_locked && !integrated->frequency_state()->ownership.needs_cleanup());
    const int after_owned_release=freq_resets;integrated->close();CHECK(freq_resets==after_owned_release);
    // A foreign request never becomes this client's cleanup responsibility.
    freq_locked=true;freq_min=300;freq_max=600;
    auto foreign_client=create_core_backend_for_test(integration_api);CHECK(foreign_client->open("0000:02:00.0",error));
    foreign_client->read(true);CHECK(!foreign_client->frequency_state()->ownership.needs_cleanup());
    CHECK(foreign_client->prepare_close().ok);foreign_client->close();CHECK(freq_locked && freq_resets==after_owned_release);
    freq_locked=false;freq_min=freq_max=0;
}
TEST_NOINLINE void scoped_client_tests(const CoreApi& rail_api, const wchar_t* directory, const Command& undo) {
    std::string error;
    // Scoped profiles capture fresh selected requests and use one grouped Undo.
    wchar_t scoped_path[MAX_PATH]{}; CHECK(GetTempFileNameW(directory,L"scp",0,scoped_path)!=0);
    Command prepare_scoped{}; prepare_scoped.operation=Command::Operation::prepare_scoped_profile;
    Command save_scoped{}; save_scoped.operation=Command::Operation::save_scoped_profile; save_scoped.profile_path=scoped_path;
    Command apply_scoped{}; apply_scoped.operation=Command::Operation::apply_scoped_profile;
    auto selection=[](std::vector<ScopedKey> keys) {
        auto request=std::make_shared<ScopedProfileRequest>(); request->selection=std::move(keys); return request;
    };
    auto document=[](ProfileJson json) {
        auto request=std::make_shared<ScopedProfileRequest>(); request->document=std::move(json); return request;
    };
    auto scoped_client=create_core_backend_for_test(rail_api); CHECK(scoped_client->open("0000:02:00.0",error));
    board=200000; peer=100; pinned_value=100; pin_present=true; raw_limits[1][2]=700;
    const int scoped_rail_writes=rail_set_calls, scoped_power_writes=power_writes, scoped_policy_writes=policy_writes, scoped_pin_writes=pin_writes;
    table_fail=true; CHECK(scoped_client->execute(prepare_scoped).ok);
    auto catalog=scoped_client->read(false).scoped_profiles; CHECK(catalog!=nullptr);
    save_scoped.scoped_profile=selection({ScopedKey::rail_field(1,2)});
    raw_limits[1][2]=800; // changed after catalog: Save must re-read the configured request.
    CHECK(scoped_client->execute(save_scoped).ok); auto scoped_rail=read_profile(scoped_path);
    CHECK(scoped_rail["entries"].size()==1 && scoped_rail["entries"][0]["value"]==800);
    CHECK(rail_set_calls==scoped_rail_writes && power_writes==scoped_power_writes && policy_writes==scoped_policy_writes && pin_writes==scoped_pin_writes);
    scoped_rail["entries"][0]["value"]=900; apply_scoped.scoped_profile=document(scoped_rail);
    CHECK(!scoped_client->execute(apply_scoped).ok && rail_set_calls==scoped_rail_writes); // Rail mode remains off.
    table_fail=false; raw_limits[1][2]=700;
    CHECK(scoped_client->execute(prepare_scoped).ok); catalog=scoped_client->read(false).scoped_profiles;
    CHECK(catalog!=nullptr);
    save_scoped.scoped_profile=selection({ScopedKey::board_power(),ScopedKey::policy_request(1)});
    board=250000; peer=250; pinned_value=250;
    CHECK(scoped_client->execute(save_scoped).ok); auto scoped_power=read_profile(scoped_path);
    CHECK(scoped_power["entries"].size()==2 && power_writes==scoped_power_writes && policy_writes==scoped_policy_writes && pin_writes==scoped_pin_writes);
    board=200000; peer=100; pinned_value=100; pin_present=true;
    auto reject_scoped=[&](ProfileJson value) {
        const int p=power_writes, q=policy_writes, r=rail_set_calls, pins=pin_writes;
        apply_scoped.scoped_profile=document(std::move(value));
        CHECK(!scoped_client->execute(apply_scoped).ok && power_writes==p && policy_writes==q && rail_set_calls==r && pin_writes==pins);
    };
    auto invalid_scoped=scoped_power; invalid_scoped["target"]["uuid"]="GPU-other"; reject_scoped(invalid_scoped);
    invalid_scoped=scoped_power; invalid_scoped["entries"][1]["binding"]="changed"; reject_scoped(invalid_scoped);
    invalid_scoped=scoped_power; invalid_scoped["entries"][1]["value"]=true; reject_scoped(invalid_scoped);
    // An external rail update after aggregate capture must not be overwritten
    // or become a stale grouped Undo baseline.
    Command scoped_rail_gate{}; scoped_rail_gate.operation=Command::Operation::set_rail_mode; scoped_rail_gate.enabled=true;
    CHECK(scoped_client->execute(scoped_rail_gate).ok);
    apply_scoped.scoped_profile=document(scoped_rail); rail_limits_calls=0; rail_request_flip_at=4;
    const int before_rail_race=rail_set_calls; auto raced_rail=scoped_client->execute(apply_scoped);
    CHECK(!raced_rail.ok && !raced_rail.may_have_changed && rail_set_calls==before_rail_race && raw_limits[1][2]==850);
    rail_request_flip_at=0; raw_limits[1][2]=700;
    // Change an observed policy after capture but before the first board writer.
    mutate_power_at_policy_read=policy_reads+2; apply_scoped.scoped_profile=document(scoped_power);
    const int before_power_race=power_writes, before_policy_race=policy_writes;
    const auto raced_power=scoped_client->execute(apply_scoped);
    CHECK(!raced_power.ok && !raced_power.may_have_changed && power_writes==before_power_race && policy_writes==before_policy_race && peer==101);
    mutate_power_at_policy_read=0; peer=100;
    policy_external_test=true; apply_scoped.scoped_profile=document(scoped_power);
    auto scoped_result=scoped_client->execute(apply_scoped);
    CHECK(scoped_result.ok && board==250000 && peer==250 && pin_present && pinned_value==250);
    CHECK(scoped_client->read(false).undo_available && scoped_client->execute(undo).ok);
    CHECK(board==200000 && peer==100 && pin_present && pinned_value==100);
    // Board setter changes the selected policy from 100 to 200 even though its
    // requested profile value was already 100; it must be explicitly reasserted.
    auto coupled=scoped_power; coupled["entries"][1]["value"]=100;
    apply_scoped.scoped_profile=document(coupled); const int before_reassert=policy_writes;
    CHECK(scoped_client->execute(apply_scoped).ok && board==250000 && peer==100 && pin_present && pinned_value==100 && policy_writes>before_reassert);
    CHECK(scoped_client->execute(undo).ok && board==200000 && peer==100 && pinned_value==100);
    // A selected policy failure after board dispatch retains the original group Undo.
    peer_fail=true; apply_scoped.scoped_profile=document(scoped_power);
    auto failed_scoped=scoped_client->execute(apply_scoped);
    CHECK(!failed_scoped.ok && failed_scoped.may_have_changed && scoped_client->read(false).undo_available);
    peer_fail=false; CHECK(scoped_client->execute(undo).ok && board==200000 && peer==100 && pin_present && pinned_value==100);
    // A policy setter can perturb the board after its own successful write;
    // final verification must catch that coupled change and retain Undo.
    policy_change_board=true; auto changed_scoped=scoped_client->execute(apply_scoped);
    CHECK(!changed_scoped.ok && changed_scoped.may_have_changed && scoped_client->read(false).undo_available);
    policy_change_board=false; CHECK(scoped_client->execute(undo).ok && board==200000 && peer==100 && pin_present && pinned_value==100);
    // Board-only Undo must not erase an unrelated pin changed after Apply.
    auto board_only=scoped_power; board_only["entries"].erase(1);
    apply_scoped.scoped_profile=document(board_only); CHECK(scoped_client->execute(apply_scoped).ok && board==250000);
    pinned_value=111; const int prior_guard_pins=pin_writes, prior_guard_board=power_writes;
    const auto guarded_undo=scoped_client->execute(undo);
    CHECK(!guarded_undo.ok && scoped_client->read(false).undo_available && pin_writes==prior_guard_pins
        && power_writes==prior_guard_board && board==250000 && pinned_value==111);
    pinned_value=100; CHECK(scoped_client->execute(undo).ok && board==200000 && pin_present && pinned_value==100);
    policy_external_test=false; CHECK(DeleteFileW(scoped_path)!=0);
    // The adapter-derived rail range includes existing requests outside the
    // ordinary 650..1200 mV bounds and the captured initial lower request.
    auto bounds_client=create_core_backend_for_test(rail_api); CHECK(bounds_client->open("0000:02:00.0",error));
    const auto rail_key=ScopedKey::rail_field(1,2);
    auto capture_bounds=[&]() -> ScopedCurrent {
        CHECK(bounds_client->execute(prepare_scoped).ok);
        const auto snap=bounds_client->read(false).scoped_profiles; CHECK(snap!=nullptr);
        if (snap) {
            const auto found=std::find_if(snap->entries.begin(),snap->entries.end(),[&](const auto& row){return row.key==rail_key;});
            CHECK(found!=snap->entries.end()); if (found!=snap->entries.end()) return *found;
        }
        return {};
    };
    const int bounds_writes=rail_set_calls; rail_base=900; override_rail_initial=false;
    raw_limits[1][2]=-300000; auto low_rail=capture_bounds();
    CHECK(low_rail.value==-300000 && low_rail.low==-300000 && low_rail.high==300000 && low_rail.fresh);
    raw_limits[1][2]=350000; auto high_rail=capture_bounds();
    CHECK(high_rail.value==350000 && high_rail.low==-250000 && high_rail.high==350000 && high_rail.fresh);
    raw_limits[1][2]=700; override_rail_initial=true; rail_initial_override=-320000;
    auto initial_low_rail=capture_bounds();
    CHECK(initial_low_rail.value==700 && initial_low_rail.low==-320000 && initial_low_rail.high==300000 && initial_low_rail.fresh);
    CHECK(rail_set_calls==bounds_writes);
    override_rail_initial=false; rail_initial_override=0; raw_limits[1][2]=700;
}
TEST_NOINLINE void power_and_actions_tests(const CoreApi& pa, const CoreApi& advanced_api, Command& undo, Command& power, Command& policy_cmd) {
    std::string error;
    CoreApi power_test = advanced_api; power_test.policy_read = alternate_policies; power_test.current_limits = core_current; power_test.pins_get = no_pins;
    auto xp = create_core_backend_for_test(power_test); CHECK(xp->open("0000:02:00.0", error)); board = 200000; xoc_live = false; auto normal = xp->read(true); CHECK(normal.controls[2].high == 500.0);
    CHECK(normal.power_policies.size() == 2 && normal.power_policies[1].id == 7 && normal.power_policies[1].core_current && normal.power_policies[1].label == "Core current (NVVDD)");
    current_read_fails = true; auto retained = xp->read(true); CHECK(retained.power_policies[1].core_current); current_read_fails = false;
    current_generic_label = true; auto generic = xp->read(true); CHECK(!generic.power_policies[1].core_current); current_generic_label = false; current_index = 19; auto alternate = xp->read(true); CHECK(alternate.power_policies[1].id == 19 && alternate.power_policies[1].core_current);
    Command mode_on{}; mode_on.operation = Command::Operation::set_xoc; mode_on.enabled = true; CHECK(xp->execute(mode_on).ok); auto expanded = xp->read(false); CHECK(expanded.controls[2].high == 1999.0 && expanded.controls[2].detail.find("software envelope") != std::string::npos);
    Command xoc_power{}; xoc_power.operation = Command::Operation::apply; xoc_power.control = ControlId::power; xoc_power.value = 1999; int writes_before = power_writes; CHECK(!xp->execute(xoc_power).ok && power_writes == writes_before && board == 200000);
    xoc_live = true; CHECK(xp->execute(xoc_power).ok && power_writes == writes_before + 1 && board == 1999000); mode_on.enabled = false; CHECK(xp->execute(mode_on).ok); auto carry = xp->read(false); CHECK(carry.controls[2].high == 1999.0 && carry.controls[2].current == 1999.0);
    xoc_power.value = 200; CHECK(xp->execute(xoc_power).ok && board == 200000); CHECK(xp->read(false).controls[2].high == 500.0);
    CoreApi derived_api = pa; derived_api.cpc_live_read = cpc_live; auto derived = create_core_backend_for_test(derived_api); CHECK(derived->open("0000:02:00.0", error));
    derived_test = true; mutable_bounds = true; pin_present = false; board = 200000; peer = 700; callback_matches = true;
    auto derived_before = derived->read(true); CHECK(callback_answer && derived_before.power_policies[1].state.current == 0.7);
    callback_matches = false; derived->read(true); CHECK(!callback_answer); callback_matches = true;
    int prior_peer_writes = policy_writes; power.operation = Command::Operation::apply; power.value = 250; CHECK(derived->execute(power).ok && board == 250000 && peer == 700);
    CHECK(derived->execute(undo).ok && board == 200000 && peer == 700 && policy_writes == prior_peer_writes);
    derived_test = false; mutable_bounds = false;
    peer = 100; pin_present = false; policy_external_test = true; policy_cmd.power_policy = 1; policy_cmd.expected_binding.reset(); policy_cmd.value = 0.1;
    CHECK(derived->execute(policy_cmd).ok && pin_present && pinned_value == 100);
    policy_cmd.operation = Command::Operation::stock; CHECK(derived->execute(policy_cmd).ok && !pin_present && peer == 0);
    CHECK(derived->execute(undo).ok && pin_present && pinned_value == 100 && peer == 100); policy_external_test = false;
    CoreApi actions = pa; actions.max_rise = model_rise; actions.planner.ramp = test_ramp; actions.planner.predict = test_predict; actions.reset_all = test_reset;
    actions.clkdom_flags = flags_get; actions.tuning.lock = lock_gpu; actions.tuning.unlock = unlock_gpu; actions.tuning.identity = identity;
    actions.tuning.vf_status = tuning_vf_status; actions.tuning.clock_status = tuning_clock_status; actions.tuning.vf_set = tuning_vf_set; actions.tuning.vf_clear = tuning_vf_clear;
    actions.tuning.recovery_pending = tuning_recovery; actions.tuning.recover = tuning_recover; actions.tuning.headroom_record = tuning_record;
    auto action_client = create_core_backend_for_test(actions); CHECK(action_client->open("0000:02:00.0", error));
    board = 200000; peer = 100; pin_present = false; fan_mode = 0; fan_level = 40; fan_auto = false; boost_value = 10; core_value = 30; delta = 123; delta2 = 456; xoc_flag = 0;
    auto action_view = action_client->read(true); Command max_cmd{}; max_cmd.operation = Command::Operation::max_it; max_cmd.expected_curve = action_view.curve; max_cmd.expected_curve_freq_div = action_view.curve_freq_div; max_cmd.hold_headroom = false;
    max_cmd.curve_floor_mv = 800; max_cmd.curve_cap_mv = 850; int before_max_power = power_writes, before_max_boost = boost_writes; CHECK(!action_client->execute(max_cmd).ok && power_writes == before_max_power && boost_writes == before_max_boost && fan_level == 40);
    max_cmd.curve_cap_mv = 950; test_foreign_clock = true; CHECK(!action_client->execute(max_cmd).ok && power_writes == before_max_power && fan_level == 40); test_foreign_clock = false;
    fan_refuse = true; auto partial_max = action_client->execute(max_cmd); CHECK(!partial_max.ok && power_writes == before_max_power && action_client->read(false).undo_available); fan_refuse = false;
    CHECK(action_client->execute(undo).ok && fan_level == 40);
    board = 1000000; xoc_flag = 1; xoc_live = true; auto max_result = action_client->execute(max_cmd); CHECK(max_result.ok && board == 500000 && boost_value == 100 && test_vf_held); CHECK(action_client->execute(undo).ok && !test_vf_held && board == 1000000 && boost_value == 10 && fan_level == 40);
    power_throw = true; bool caught_max = false; try { action_client->execute(max_cmd); } catch (const std::exception&) { caught_max = true; } power_throw = false;
    CHECK(caught_max && board == 500000 && action_client->read(false).undo_available); CHECK(action_client->execute(undo).ok && board == 1000000 && fan_level == 40);
    Command reset_command{}; reset_command.operation = Command::Operation::reset_controls; test_foreign_clock = true; int old_reset_calls = reset_calls; CHECK(!action_client->execute(reset_command).ok && reset_calls == old_reset_calls); test_foreign_clock = false;
    nonlinear_curve = true; reset_step_failure = true; core_value = 30; delta = 123; delta2 = 456; auto reset_failed = action_client->execute(reset_command); CHECK(!reset_failed.ok && reset_failed.may_have_changed && action_client->read(false).undo_available); reset_step_failure = false;
    CHECK(action_client->execute(undo).ok && core_value == 30 && delta == 123 && delta2 == 456); nonlinear_curve = false;
    reset_api_failure = true; reset_failed = action_client->execute(reset_command); CHECK(!reset_failed.ok && reset_failed.may_have_changed && action_client->read(false).undo_available); reset_api_failure = false;
    board = 200000; peer = 100; pin_present = false; action_client->read(true); const int policy_before_power = policy_reads;
    Command refresh_power{}; refresh_power.operation = Command::Operation::apply; refresh_power.control = ControlId::power; refresh_power.value = 250;
    CHECK(action_client->execute(refresh_power).ok && policy_reads > policy_before_power); const auto refreshed_power = action_client->read(false);
    CHECK(refreshed_power.power_policies.size() == 2 && refreshed_power.power_policies[1].state.current == 0.2);
}
#undef TEST_NOINLINE
}
int main() {
    std::string error;
    auto missing = api(); missing.identity = nullptr;
    CHECK(!create_core_backend_for_test(missing)->open("0000:02:00.0", error));
    no_device = true; auto absent = create_core_backend_for_test(api()); CHECK(!absent->open("0000:02:00.0", error)); no_device = false;
    perf_frequency_integration_tests();
    auto b = create_core_backend_for_test(api()); CHECK(b->open("0000:02:00.0", error));
    auto initial = b->read(false); CHECK(initial.connected); CHECK(initial.controls[1].current == 20.0); CHECK(initial.controls[2].availability == Availability::unsupported);
    range_fail = 1; CHECK(b->read(false).controls[1].current == 20.0);
    range_fail = 4; auto transient = b->read(false); CHECK(transient.controls[1].availability == Availability::transient && transient.controls[1].current == 20.0);
    changed_id = true; CHECK(!b->execute(command(Command::Operation::apply, 30)).ok && writes == 0); changed_id = false;
    CHECK(!b->execute(command(Command::Operation::apply, 20.5)).ok && writes == 0);
    current = 41; CHECK(!b->execute(command(Command::Operation::apply, 30)).ok && writes == 0); current = 40;
    auto success = b->execute(command(Command::Operation::apply, 30)); CHECK(success.ok && success.readback == 30 && writes == 1);
    mode = 1; auto quantized = b->execute(command(Command::Operation::apply, 35)); CHECK(!quantized.ok && quantized.readback == 34 && writes == 2);
    mode = 2; auto refused = b->execute(command(Command::Operation::apply, 36)); CHECK(!refused.ok && refused.may_have_changed && writes == 3);
    mode = 3; auto unreadable = b->execute(command(Command::Operation::apply, 37)); CHECK(!unreadable.ok && !unreadable.readback && writes == 4);
    mode = 0; auto undone = b->execute(command(Command::Operation::undo)); CHECK(undone.ok && undone.readback == 34 && writes == 5);
    CHECK(b->read(false).undo_available);
    CHECK(b->execute(command(Command::Operation::stock)).ok && current == 0);
    b->close(); CHECK(b->open("0000:02:00.0", error));
    Command core{Command::Operation::apply, ControlId::core, 50}; CHECK(b->execute(core).ok && core_value == 50 && delta == 999);
    Command undo{Command::Operation::undo, ControlId::core, 0}; curve_failure = 1;
    CHECK(!b->execute(undo).ok && core_value == 30 && b->read(false).undo_available);
    curve_failure = 0; CHECK(b->execute(undo).ok && delta == 123 && !b->read(false).undo_available);
    fan_auto = false; fan_mode = 1; auto mixed = b->read(false).controls[3]; CHECK(!mixed.current && mixed.detail.find("Mixed") != std::string::npos);
    Command fan{Command::Operation::apply, ControlId::fan, 50}; CHECK(b->execute(fan).ok); CHECK(b->execute(undo).ok && fan_level == 40);
    fan_mode = 2; CHECK(b->read(false).controls[3].stock_writable); fan.operation = Command::Operation::stock; auto auto_result = b->execute(fan); CHECK(auto_result.ok && auto_result.message.find("cannot be undone") != std::string::npos);
    Command boost{Command::Operation::apply, ControlId::boost, 30}; CHECK(b->execute(boost).ok && boost_value == 30); CHECK(b->execute(undo).ok && boost_value == 10);
    CoreApi pa = api(); pa.lock = lock_gpu; pa.unlock = unlock_gpu; pa.capabilities = capabilities; pa.power = power_read; pa.power_bounds = bounds; pa.power_set = power_set; pa.power_message = message; pa.policy_read = policies; pa.policy_set = policy_set; pa.policy_message = message; pa.pins_get = pins_get; pa.pins_set = pins_set;
    auto pb = create_core_backend_for_test(pa); CHECK(pb->open("0000:02:00.0", error));
    Command power{Command::Operation::apply, ControlId::power, 250};
    table_fail = true; CHECK(!pb->execute(power).ok && power_writes == 0); table_fail = false;
    CHECK(pb->read(true).controls[2].writable); int reads_before = policy_reads; pb->read(false); CHECK(policy_reads == reads_before);
    CHECK(pb->execute(power).ok && policy_reads > reads_before && board == 250000 && peer == 200 && power_writes == 1);
    geometry_change = true; auto geometry = pb->execute(undo); CHECK(!geometry.ok && board == 250000 && power_writes == 1 && pin_present); geometry_change = false;
    peer_fail = true; auto partial = pb->execute(undo); CHECK(!partial.ok && board == 200000 && !pin_present && pb->read(false).undo_available);
    peer_fail = false; CHECK(pb->execute(undo).ok && peer == 100 && pin_present && policy_writes == 2);
    board_mismatch = true; auto mismatch = pb->execute(power); CHECK(!mismatch.ok && mismatch.readback == 251.0 && mismatch.may_have_changed); board_mismatch = false;
    table_fail = true; power.operation = Command::Operation::stock; auto stock = pb->execute(power); CHECK(stock.ok && stock.message.find("cannot be undone") != std::string::npos); table_fail = false;
    wchar_t directory[MAX_PATH]{}; CHECK(GetTempPathW(MAX_PATH, directory) != 0); wchar_t path[MAX_PATH]{}; CHECK(GetTempFileNameW(directory, L"drp", 0, path) != 0);
    // Save captures a fresh configured V/F table, including sparse layout,
    // rather than the last cached observation or any staged UI state.
    wchar_t curve_path[MAX_PATH]{}; CHECK(GetTempFileNameW(directory, L"vfp", 0, curve_path) != 0);
    CHECK(DeleteFileW(curve_path) != 0);
    Command save_curve{}; save_curve.operation = Command::Operation::save_curve_profile; save_curve.profile_path = curve_path;
    const auto cached_curve = pb->read(true); CHECK(cached_curve.curve.size() == 2);
    const int original_delta = delta, original_delta2 = delta2;
    delta = 321; delta2 = -654;
    const int save_curve_writes = curve_writes, save_offset_writes = writes, save_power_writes = power_writes;
    const int save_policy_writes = policy_writes, save_advanced_writes = advanced_writes, save_boost_writes = boost_writes;
    const auto curve_saved = pb->execute(save_curve); CHECK(curve_saved.ok && !curve_saved.may_have_changed);
    const auto curve_doc = read_profile(curve_path);
    CHECK(curve_doc["format"] == "druta-native-curve" && curve_doc["target"]["uuid"] == "GPU-A");
    CHECK(curve_doc["geometry"]["n_entries"] == 7 && curve_doc["geometry"]["freq_div"] == curve_div
        && curve_doc["geometry"]["gpu_indices"] == ProfileJson::array({3,4})
        && curve_doc["geometry"]["other_indices"] == ProfileJson::array({5}));
    CHECK(curve_doc["points"].size() == 2 && curve_doc["points"][0]["idx"] == 3
        && curve_doc["points"][0]["delta_khz"] == 321 && curve_doc["points"][1]["delta_khz"] == -654);
    CHECK(curve_writes == save_curve_writes && writes == save_offset_writes && power_writes == save_power_writes
        && policy_writes == save_policy_writes && advanced_writes == save_advanced_writes && boost_writes == save_boost_writes);
    CHECK(DeleteFileW(curve_path) != 0);
    curve_read_fail = 2; CHECK(!pb->execute(save_curve).ok && GetFileAttributesW(curve_path) == INVALID_FILE_ATTRIBUTES);
    curve_read_fail = 0; changed_id = true;
    CHECK(!pb->execute(save_curve).ok && GetFileAttributesW(curve_path) == INVALID_FILE_ATTRIBUTES);
    changed_id = false;
    CoreApi unavailable_curve_api = pa; unavailable_curve_api.curve_layout = nullptr;
    auto unavailable_curve = create_core_backend_for_test(unavailable_curve_api);
    CHECK(unavailable_curve->open("0000:02:00.0", error));
    CHECK(!unavailable_curve->execute(save_curve).ok && GetFileAttributesW(curve_path) == INVALID_FILE_ATTRIBUTES);
    CHECK(curve_writes == save_curve_writes && writes == save_offset_writes && power_writes == save_power_writes
        && policy_writes == save_policy_writes && advanced_writes == save_advanced_writes && boost_writes == save_boost_writes);
    delta = original_delta; delta2 = original_delta2;
    // Clock bin evidence uses the independently probed step even when V/F
    // detail capture fails; transient step failures preserve a known value.
    CoreApi clock_api = pa; clock_api.telemetry = clock_telemetry; clock_api.clock_classify = clock_classifier;
    auto clocks = create_core_backend_for_test(clock_api); CHECK(clocks->open("0000:02:00.0", error));
    test_clock_step=12500; test_clock_measured=1; curve_read_fail=2;
    auto clock_state=clocks->read(true);
    CHECK(clock_state.curve.empty() && clock_state.clock_monitor && clock_state.clock_monitor->readings[0].bin_mhz == 12.5);
    curve_read_fail=0; clock_step_fail=2; clock_state=clocks->read(true);
    CHECK(clock_state.curve.size()==2 && clock_state.clock_monitor->readings[0].bin_mhz == 12.5);
    test_clock_step=15000; clock_state=clocks->read(true);
    CHECK(clock_state.clock_monitor->readings[0].bin_mhz == 15.0);
    test_clock_measured=0; clock_state=clocks->read(true);
    CHECK(!clock_state.clock_monitor->readings[0].bin_mhz);
    test_clock_measured=1; clock_state=clocks->read(true);
    CHECK(clock_state.clock_monitor->readings[0].bin_mhz == 15.0);
    clocks->close(); CHECK(clocks->open("0000:02:00.0", error));
    clock_state=clocks->read(false);
    CHECK(clock_state.clock_monitor && !clock_state.clock_monitor->readings[0].bin_mhz);
    CoreApi missing_step_api=clock_api; missing_step_api.clock_step=nullptr;
    auto no_step=create_core_backend_for_test(missing_step_api); CHECK(no_step->open("0000:02:00.0", error));
    clock_state=no_step->read(true); CHECK(clock_state.clock_monitor && !clock_state.clock_monitor->readings[0].bin_mhz);
    test_clock_step=1000; test_clock_measured=1; clock_step_fail=0;
    Command save{}; save.operation = Command::Operation::save_profile; save.profile_path = path;
    fan_auto = false; fan_mode = 1; mode = 0; CHECK(pb->execute(save).ok); auto saved = read_profile(path);
    CHECK(saved["format"] == "druta-native-daily" && !saved["controls"].contains("fan") && saved["controls"].contains("core"));
    Command load{}; load.operation = Command::Operation::load_profile; load.profile_path = path;
    ProfileJson doc = {{"format", "druta-native-daily"}, {"version", 1}, {"target", {{"uuid", "other"}, {"slot", "0000:02:00.0"}}}, {"controls", {{"memory", {{"unit", "MHz eff"}, {"scale", 2}, {"value", 80}}}}}};
    int old_writes = writes, old_power = power_writes; write_profile(path, doc); CHECK(!pb->execute(load).ok && writes == old_writes && power_writes == old_power);
    doc["target"]["uuid"] = "GPU-A"; doc["controls"]["unknown"] = 1; write_profile(path, doc); CHECK(!pb->execute(load).ok && writes == old_writes && power_writes == old_power); doc["controls"].erase("unknown");
    doc["controls"]["boost"] = {{"unit", "%"}, {"value", 101}}; write_profile(path, doc); CHECK(!pb->execute(load).ok && writes == old_writes && power_writes == old_power); doc["controls"].erase("boost");
    doc["controls"]["power"] = {{"unit", "mW"}, {"value", 210000}}; doc["controls"]["core"] = {{"unit", "MHz"}, {"value", 50}}; write_profile(path, doc); mode = 1;
    auto failed_load = pb->execute(load); CHECK(!failed_load.ok && failed_load.may_have_changed && board == 210000 && pb->read(false).undo_label == "Undo profile");
    delta = 333; mode = 2; CHECK(!pb->execute(undo).ok && board == 200000 && delta == 333);
    // Completed power is still part of the original selected request set.
    // A change between retries must fail final comparison and become explicit
    // pending recovery, while the never-issued core edit remains untouched.
    board = 199000; mode = 0; CHECK(!pb->execute(undo).ok && board == 199000 && delta == 333 && pb->read(false).undo_available);
    CHECK(pb->execute(undo).ok && board == 200000 && delta == 333); delta = 123;
    doc["controls"].erase("power"); doc["controls"]["fan"] = {{"mode", "manual"}, {"unit", "%"}, {"value", 50}}; doc["controls"]["core"] = {{"unit", "MHz"}, {"value", 50}};
    core_value = 30; delta = 123; fan_level = 40; fan_mode = 1; fan_auto = false; write_profile(path, doc); CHECK(pb->execute(load).ok && core_value == 50 && delta == 999 && fan_level == 50);
    CHECK(pb->read(false).undo_label == "Undo profile"); CHECK(pb->execute(undo).ok && core_value == 30 && delta == 123 && fan_level == 40);
    doc["controls"].erase("fan"); doc["controls"].erase("core"); doc["controls"]["memory"]["value"] = current; write_profile(path, doc);
    auto history = pb->read(false).undo_label; CHECK(pb->execute(load).ok && pb->read(false).undo_label == history);
    ProfileJson py = {{"schema", 2}, {"scope", "tuning_action"}, {"saved_at", "2026-01-01 00:00:00"}, {"saved_ts", 12.0}, {"device", {{"name", nullptr}, {"vbios", "different"}, {"driver", "different"}, {"uuid", nullptr}, {"slot", "0000:02:00.0"}}}, {"incomplete", ProfileJson::array()}, {"tuning_action", {{"kind", "memory"}, {"index", nullptr}, {"value", 80}, {"scale", 4}}}};
    current = 0; write_profile(path, py); CHECK(pb->execute(load).ok && current == 80 && pb->read(false).undo_label == "Undo profile"); CHECK(pb->execute(undo).ok && current == 0);
    py["tuning_action"] = {{"kind", "boost"}, {"index", nullptr}, {"value", 25}}; boost_value = 10; write_profile(path, py); CHECK(pb->execute(load).ok && boost_value == 25); CHECK(pb->execute(undo).ok && boost_value == 10);
    const int old_memory_writes = writes, old_boost_writes = boost_writes; auto reject = [&](const ProfileJson& input) { write_profile(path, input); auto response = pb->execute(load); CHECK(!response.ok && writes == old_memory_writes && boost_writes == old_boost_writes); return response.message; };
    auto bad = py; bad["device"]["driver"] = 42; reject(bad); bad = py; bad["device"]["uuid"] = "GPU-other"; reject(bad); bad = py; bad["incomplete"].push_back("memory"); reject(bad);
    bad = py; bad["scope"] = "power_action"; CHECK(reject(bad).find("unsupported Python profile scope") != std::string::npos);
    bad = py; bad["tuning_action"] = {{"kind", "memory"}, {"index", nullptr}, {"value", 80}, {"scale", 0}}; reject(bad);
    bad = py; bad["tuning_action"]["value"] = true; reject(bad);
    bad = py; bad["tuning_action"] = {{"kind", "domain"}, {"index", 1}, {"value", 0}}; reject(bad);
    bad = py; bad["tuning_action"] = {{"kind", "mystery"}, {"index", nullptr}, {"value", 0}}; CHECK(reject(bad).find("unsupported Python tuning action kind: mystery") != std::string::npos);
    CoreApi advanced_api = pa; advanced_api.telemetry = memory_telemetry; advanced_api.clkdom_ok = dom_ok; advanced_api.clkdom_layout = dom_layout; advanced_api.clkdom_domains = dom_list; advanced_api.clkdom_controls = dom_controls; advanced_api.clkdom_offered = dom_offered; advanced_api.clkdom_requests = dom_read; advanced_api.clkdom_set = dom_set; advanced_api.rail_cap = rail_cap; advanced_api.rail_read = rail_read; advanced_api.rail_set = rail_set; advanced_api.msvdd_reset = rail_reset; advanced_api.clkdom_flags = flags_get; advanced_api.clkdom_flags_set = flags_set;
    xoc_flag = 1; auto ab = create_core_backend_for_test(advanced_api); CHECK(ab->open("0000:02:00.0", error) && xoc_flag == 0); auto adv = ab->read(true); CHECK(adv.advanced_controls.size() == 4 && adv.xoc_known && !adv.xoc_enabled);
    auto memory_label = std::find_if(adv.advanced_controls.begin(), adv.advanced_controls.end(), [](const AdvancedControl& a) { return a.id.kind == AdvancedKind::clock_domain && a.id.domain == 2; }); CHECK(memory_label != adv.advanced_controls.end() && memory_label->label == "Additional memory offset" && memory_label->state.low == -500 && memory_label->state.high == 1000);
    auto find_domain = [](const LiveData& data, uint32_t id) -> ControlState { auto it = std::find_if(data.advanced_controls.begin(), data.advanced_controls.end(), [id](const AdvancedControl& a) { return a.id.kind == AdvancedKind::clock_domain && a.id.domain == id; }); CHECK(it != data.advanced_controls.end()); return it == data.advanced_controls.end() ? ControlState{} : it->state; };
    auto normal_domain = find_domain(adv, 5); CHECK(normal_domain.low == -300 && normal_domain.high == 300 && normal_domain.detail.find("software editing range") != std::string::npos);
    domain_raw[5] = 450000; CHECK(find_domain(ab->read(true), 5).high == 450); domain_raw[5] = 0;
    Command range_gate{}; range_gate.operation = Command::Operation::set_xoc; range_gate.enabled = true; CHECK(ab->execute(range_gate).ok);
    core_low = -700; core_high = 900; mem_low = -400; mem_high = 3000; auto varied_ranges = ab->read(true); CHECK(find_domain(varied_ranges, 5).low == -700 && find_domain(varied_ranges, 5).high == 900); CHECK(find_domain(varied_ranges, 2).low == -2000 && find_domain(varied_ranges, 2).high == 2000);
    memory_p0 = 0; memory_clock = 12000; CHECK(find_domain(ab->read(true), 2).high == 3000); memory_p0 = 8000; memory_clock = 0; ab->read(true);
    domain_raw[5] = 1500000; CHECK(find_domain(ab->read(true), 5).high == 1500); domain_raw[5] = 0;
    range_fail = 4; telemetry_fail = 2; auto transient_ranges = ab->read(true); CHECK(find_domain(transient_ranges, 5).low == -700 && find_domain(transient_ranges, 5).high == 900 && find_domain(transient_ranges, 2).high == 2000);
    range_gate.enabled = false; CHECK(ab->execute(range_gate).ok); auto normal_memory = find_domain(ab->read(true), 2); CHECK(normal_memory.low == -200 && normal_memory.high == 1500);
    core_low = -1000; core_high = 2000; mem_low = -1000; mem_high = 2000;
    CoreApi no_offset_range = advanced_api; no_offset_range.range = nullptr; auto defaults = create_core_backend_for_test(no_offset_range); CHECK(defaults->open("0000:02:00.0", error)); auto default_normal = defaults->read(true); CHECK(find_domain(default_normal, 5).low == -300 && find_domain(default_normal, 2).low == -1000 && find_domain(default_normal, 2).high == 3000);
    range_gate.enabled = true; CHECK(defaults->execute(range_gate).ok); auto default_xoc = defaults->read(true); CHECK(find_domain(default_xoc, 5).low == -1000 && find_domain(default_xoc, 5).high == 1000 && find_domain(default_xoc, 2).high == 3000); xoc_flag = 0;
    auto omitted_profile = ab->execute(save); CHECK(omitted_profile.ok && omitted_profile.message.find("Additional memory offset") != std::string::npos);
    Command dc{}; dc.operation = Command::Operation::apply; dc.advanced = AdvancedId{AdvancedKind::clock_domain, 5}; dc.value = 20;
    auto domain_bound = std::find_if(adv.advanced_controls.begin(), adv.advanced_controls.end(), [](const AdvancedControl& a) { return a.id.kind == AdvancedKind::clock_domain && a.id.domain == 5; }); CHECK(domain_bound != adv.advanced_controls.end() && !domain_bound->binding.empty()); dc.expected_binding = domain_bound->binding; int before_dom_writes = advanced_writes; layout_version = 5; CHECK(!ab->execute(dc).ok && advanced_writes == before_dom_writes); layout_version = 4;
    CHECK(ab->execute(dc).ok && domain_raw[5] == 20000); dc.expected_binding.reset(); CHECK(ab->execute(undo).ok && domain_raw[5] == 0);
    advanced_fail_reads = 12; auto transient_adv = ab->read(true); CHECK(transient_adv.advanced_controls[0].state.availability == Availability::transient && transient_adv.advanced_controls[0].state.current.has_value()); advanced_fail_reads = 0;
    advanced_refuse = true; CHECK(!ab->execute(dc).ok && domain_raw[5] == 0); advanced_refuse = false;
    dc.advanced = AdvancedId{AdvancedKind::clock_domain, 2}; CHECK(!ab->execute(dc).ok); domain_raw[2] = 10000; dc.operation = Command::Operation::stock; auto released = ab->execute(dc); CHECK(released.ok && domain_raw[2] == 0 && released.message.find("cannot be undone") != std::string::npos); dc.operation = Command::Operation::apply; Command gate{}; gate.operation = Command::Operation::set_xoc; gate.enabled = true; CHECK(ab->execute(gate).ok && xoc_flag == 1); CHECK(ab->execute(dc).ok && domain_raw[2] == 20000); CHECK(ab->execute(undo).ok && domain_raw[2] == 0);
    auto xoc_before = ab->read(true); gate.enabled = false; CHECK(ab->execute(gate).ok); advanced_fail_reads = 12; auto xoc_stale = ab->read(true); auto mem_row = std::find_if(xoc_stale.advanced_controls.begin(), xoc_stale.advanced_controls.end(), [](const AdvancedControl& a) { return a.id.kind == AdvancedKind::clock_domain && a.id.domain == 2; }); CHECK(mem_row != xoc_stale.advanced_controls.end() && mem_row->state.availability == Availability::transient && !mem_row->state.writable && mem_row->state.stock_writable); advanced_fail_reads = 0; gate.enabled = true; CHECK(ab->execute(gate).ok);
    dc.advanced = AdvancedId{AdvancedKind::nvvdd, 0}; dc.value = 12.5; CHECK(ab->execute(dc).ok && rail_raw[0] == 12500); CHECK(ab->execute(undo).ok && rail_raw[0] == 0);
    has_msvdd = true; CHECK(ab->read(true).advanced_controls.size() == 5); dc.advanced = AdvancedId{AdvancedKind::msvdd, 5}; CHECK(ab->execute(dc).ok && rail_raw[1] == 12500); gate.enabled = false; CHECK(ab->execute(gate).ok); CHECK(!ab->execute(undo).ok); dc.operation = Command::Operation::stock; CHECK(ab->execute(dc).ok && rail_raw[1] == 0);
    gate.enabled = true; CHECK(ab->execute(gate).ok); domain_raw[1] = 30000; CHECK(ab->execute(save).ok); auto v2 = read_profile(path); CHECK(v2["version"] == 2 && v2["advanced"].is_array());
    gate.enabled = false; CHECK(ab->execute(gate).ok); int saved_adv_writes = advanced_writes, saved_mem_writes = writes; domain_raw[1] = 0; CHECK(!ab->execute(load).ok && advanced_writes == saved_adv_writes && writes == saved_mem_writes); gate.enabled = true; CHECK(ab->execute(gate).ok);
    domain_raw[1] = 0; CHECK(ab->execute(load).ok && domain_raw[1] == 30000); CHECK(ab->execute(undo).ok && domain_raw[1] == 0);
    py["tuning_action"] = {{"kind", "domain"}, {"index", 5}, {"value", 25000}}; write_profile(path, py); CHECK(ab->execute(load).ok && domain_raw[5] == 25000); CHECK(ab->execute(undo).ok && domain_raw[5] == 0);
    py["tuning_action"] = {{"kind", "rail_offset"}, {"index", nullptr}, {"value", 12500}}; write_profile(path, py); CHECK(ab->execute(load).ok && rail_raw[0] == 12500); CHECK(ab->execute(undo).ok && rail_raw[0] == 0);
    CHECK(DeleteFileW(path) != 0);
    peer = 100; board = 200000; pin_present = true; auto policy_view = ab->read(true); CHECK(policy_view.power_policies.size() == 2 && policy_view.power_policies[1].state.writable);
    Command policy_cmd{}; policy_cmd.operation = Command::Operation::apply; policy_cmd.power_policy = 1; policy_cmd.value = 0.25; policy_external_test = true; policy_cmd.expected_binding = policy_view.power_policies[1].binding; CHECK(policy_view.power_policies[1].state.step == 0.001); int pre_policy = policy_writes; geometry_change = true; CHECK(!ab->execute(policy_cmd).ok && policy_writes == pre_policy); geometry_change = false;
    policy_change_board = true; auto policy_changed = ab->execute(policy_cmd); CHECK(policy_changed.ok && peer == 250 && board == 201000 && policy_changed.message.find("coupled") != std::string::npos); policy_change_board = false; policy_external_test = false; CHECK(ab->execute(undo).ok && peer == 100 && board == 200000 && pin_present);
    policy_drop_pin = true; policy_external_test = true; auto pin_change = ab->execute(policy_cmd); CHECK(!pin_change.ok && !pin_present && ab->read(false).undo_available); policy_drop_pin = false; policy_external_test = false; CHECK(ab->execute(undo).ok && peer == 100 && pin_present);
    policy_cmd.power_policy = 9; int old_policy_writes = policy_writes; CHECK(!ab->execute(policy_cmd).ok && policy_writes == old_policy_writes);
    CoreApi rail_api = advanced_api; rail_api.rails.lock = lock_gpu; rail_api.rails.unlock = unlock_gpu; rail_api.rails.identity = identity; rail_api.rails.limits = rail_limits; rail_api.rails.fields = rail_fields; rail_api.rails.initial = rail_initial; rail_api.rails.hold = rail_hold; rail_api.rails.set_raw = rail_raw_set; rail_api.rails.reset = rail_initial_set;
    auto rb = create_core_backend_for_test(rail_api); CHECK(rb->open("0000:02:00.0", error)); auto rail_view = rb->read(true); CHECK(rail_view.rail_controls.size() == 8 && rail_view.rail_writable_known && rail_view.rail_writable && !rail_view.rail_mode_enabled);
    Command rail_command{}; rail_command.operation = Command::Operation::apply; rail_command.rail = RailId{1, 2}; rail_command.value = 1.234; rail_command.expected_binding = rail_view.rail_controls[6].binding; CHECK(!rail_command.expected_binding->empty()); CHECK(!rb->execute(rail_command).ok && rail_set_calls == 0);
    Command rail_gate{}; rail_gate.operation = Command::Operation::set_rail_mode; rail_gate.enabled = true; CHECK(rb->execute(rail_gate).ok); rail_view = rb->read(true); CHECK(rail_view.rail_mode_enabled && rail_view.rail_writable);
    rail_base = 901; CHECK(!rb->execute(rail_command).ok && rail_set_calls == 0); rail_base = 900;
    rail_limits_calls = 0; rail_type_flip_at = 4; CHECK(!rb->execute(rail_command).ok && rail_set_calls == 0); rail_type_flip_at = 0; rail_type = 7;
    CHECK(rb->execute(rail_command).ok && raw_limits[1][2] == 1234 && raw_limits[1][1] == 600); CHECK(rb->execute(undo).ok && raw_limits[1][2] == 700);
    rail_throw = true; bool caught_rail_exception = false; try { rb->execute(rail_command); } catch (const std::exception&) { caught_rail_exception = true; } rail_throw = false; CHECK(caught_rail_exception && raw_limits[1][2] == 1234 && rb->read(false).undo_label == "Undo rail limit"); CHECK(rb->execute(undo).ok && raw_limits[1][2] == 700);
    rail_failed_index = 0; rail_held = 1; rail_view = rb->read(true);
    CHECK(rail_view.rail_writable_known && rail_view.rail_writable && rail_view.rail_controls[0].state.availability == Availability::transient && !rail_view.rail_controls[4].state.writable);
    rail_failed_index = -1; rail_held = -1;
    rail_fields_fail = true; rail_view = rb->read(true); CHECK(rail_view.rail_writable_known && rail_view.rail_writable && rail_view.rail_controls[0].state.availability == Availability::transient); rail_fields_fail = false;
    CHECK(rb->execute(rail_command).ok && raw_limits[1][2] == 1234); rail_gate.enabled = false; CHECK(rb->execute(rail_gate).ok && !rb->read(false).rail_mode_enabled);
    rail_fields_fail = true; rail_view = rb->read(true); CHECK(!rail_view.rail_controls[0].state.writable && rail_view.rail_writable); rail_fields_fail = false;
    flags_fail = true; rail_command.operation = Command::Operation::stock; CHECK(rb->execute(rail_command).ok && raw_limits[1][2] == 700); flags_fail = false;
    scoped_client_tests(rail_api,directory,undo);
    fan_mode = 0; fan_auto = false; fan_level = 40; fan_refuse = true; fan.value = 60; fan.operation = Command::Operation::apply;
    CHECK(!b->execute(fan).ok && b->read(false).undo_available); fan_refuse = false;
    fan_post_fail = true; CHECK(!b->execute(fan).ok && b->read(false).undo_available); fan_post_fail = false;
    curve_div = 2; delta = 123; delta2 = 456; auto fresh = pb->read(true); CHECK(fresh.curve_writable && fresh.curve.size() == 2 && fresh.curve_step_khz == 1000 && fresh.curve_freq_div == 2);
    Command curve{}; curve.operation = Command::Operation::apply_curve; curve.expected_curve = fresh.curve; curve.expected_curve_freq_div = fresh.curve_freq_div; curve.curve_deltas = {{3, 200}};
    int before_curve = curve_writes; curve.expected_curve_freq_div = 1; CHECK(!pb->execute(curve).ok && curve_writes == before_curve); curve.expected_curve_freq_div = 2; curve.expected_curve[0].volt_mv += 1; CHECK(!pb->execute(curve).ok && curve_writes == before_curve); curve.expected_curve = fresh.curve;
    curve.curve_deltas.push_back({3, 300}); CHECK(!pb->execute(curve).ok && curve_writes == before_curve); curve.curve_deltas.pop_back();
    CHECK(pb->execute(curve).ok && delta == 200 && delta2 == 456 && curve_writes == before_curve + 1);
    int changed_writes = curve_writes; voltage_shift = 1; CHECK(!pb->execute(undo).ok && curve_writes == changed_writes); voltage_shift = 0;
    layout_shift = 1; CHECK(!pb->execute(undo).ok && curve_writes == changed_writes); layout_shift = 0;
    curve_div = 1; CHECK(!pb->execute(undo).ok && curve_writes == changed_writes); curve_div = 2;
    CHECK(pb->execute(undo).ok && delta == 123 && delta2 == 456);
    fresh = pb->read(true); curve.expected_curve = fresh.curve; curve_corrupt = true; CHECK(!pb->execute(curve).ok && pb->read(false).undo_label == "Undo V/F curve"); curve_corrupt = false; CHECK(pb->execute(undo).ok && delta == 123 && delta2 == 456);
    Command core_again{Command::Operation::apply, ControlId::core, 60}; CHECK(pb->execute(core_again).ok); changed_writes = writes; voltage_shift = 1; CHECK(!pb->execute(undo).ok && writes == changed_writes); voltage_shift = 0; CHECK(pb->execute(undo).ok);
    fresh = pb->read(true); curve.expected_curve = fresh.curve; curve_geometry_corrupt = true;
    auto geometry_write = pb->execute(curve); CHECK(!geometry_write.ok && geometry_write.may_have_changed && pb->read(false).undo_label == "Undo V/F curve");
    curve_geometry_corrupt = false; voltage_shift = 0; CHECK(pb->execute(undo).ok && delta == 123 && delta2 == 456);
    nonlinear_curve = true; curve_div = 1; delta = 0; delta2 = 0; fresh = pb->read(true); curve.expected_curve = fresh.curve; curve.expected_curve_freq_div = 1; curve.curve_deltas = {{3,150000}};
    CHECK(pb->execute(curve).ok && delta == 150000 && pb->read(true).curve[1].freq_mhz == 2160); CHECK(pb->execute(undo).ok && delta == 0 && delta2 == 0); nonlinear_curve = false; curve_div = 2; curve.expected_curve_freq_div = 2; delta = 123; delta2 = 456;
    CoreApi no_layout = pa; no_layout.curve_layout = nullptr; auto without_layout = create_core_backend_for_test(no_layout); CHECK(without_layout->open("0000:02:00.0", error)); auto layout_state = without_layout->read(false); CHECK(!layout_state.controls[0].writable && layout_state.controls[1].writable);
    CoreApi no_curve = pa; no_curve.curve = nullptr; auto without_curve = create_core_backend_for_test(no_curve); CHECK(without_curve->open("0000:02:00.0", error)); auto no_curve_state = without_curve->read(false); CHECK(!no_curve_state.controls[0].writable && no_curve_state.controls[1].writable);
    CoreApi no_reset = pa; no_reset.reset_curve = nullptr; auto partial_curve = create_core_backend_for_test(no_reset); CHECK(partial_curve->open("0000:02:00.0", error)); auto partial_state = partial_curve->read(true); CHECK(partial_state.curve_writable && !partial_state.curve_reset_writable);
    fresh = pb->read(true); curve.operation = Command::Operation::reset_curve; curve.expected_curve = fresh.curve; curve.curve_deltas.clear(); CHECK(pb->execute(curve).ok && delta == 0 && delta2 == 0); CHECK(pb->execute(undo).ok && delta == 123 && delta2 == 456);
    power_and_actions_tests(pa, advanced_api, undo, power, policy_cmd);
    std::printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
