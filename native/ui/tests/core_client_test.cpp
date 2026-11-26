/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "core_client.h"
#include "profile_io.h"
#include "scoped_profile.h"
#include "clock_monitor_model.h"
#include "model_input_inventory.h"
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <cstdio>
#define CHECK(x) do { ++checks; if (!(x)) { std::fprintf(stderr, "CHECK failed at line %d: %s\n", __LINE__, #x); ++failures; } } while (0)
int failures = 0, checks = 0, boost_writes = 0;
#include <cstring>
#include <algorithm>
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
    row.prog_khz=1500000; row.meas_khz=1499000; row.has_prog_mhz=1; row.prog_mhz=1500; return DC_OK;
}
std::vector<uint32_t> paired_live; int32_t requests_only=0; int memory_caps_fail=0, pairing_calls=0, blackwell_calls=0;
dc_status DC_CALL clock_pairing(dc_gpu*, int32_t has_rows, const uint32_t* live, size_t n, dc_clkdom_pair* pairs, size_t capacity, size_t* count) {
    ++pairing_calls; CHECK(has_rows == 1); paired_live.assign(live, live + n); *count = 1;
    if (capacity < 1) return DC_E_BUFFER; pairs[0] = {3, 0}; return DC_OK;
}
int32_t DC_CALL clock_blackwell(dc_gpu*) { ++blackwell_calls; return requests_only; }
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
bool power_bounds_failed = false, power_bounds_unsupported = false, power_set_unverified = false;
int power_post_read_failures = 0, power_reads_remaining_to_fail = 0;
int mutate_power_at_policy_read = 0;
dc_status DC_CALL cached_power_error(dc_gpu*, dc_cpc_live_state* out) {
    CHECK(out->size == sizeof(*out) && out->last.size == sizeof(out->last)
          && out->last.metadata.size == sizeof(out->last.metadata) && out->last.arbitration.size == sizeof(out->last.arbitration));
    out->has_error = 1;
    out->has_last = xoc_live ? 1 : 0;
    std::strcpy(out->error, "Client power metadata unavailable");
    return DC_OK;
}
dc_status DC_CALL cpc_live(dc_gpu*, dc_cpc_observation* out, dc_error*) { CHECK(out->size == sizeof(*out) && out->metadata.size == sizeof(out->metadata) && out->arbitration.size == sizeof(out->arbitration)); out->metadata.role_info_size = 5; out->metadata.role_info[4] = 0x30; out->metadata.maximum_mw = 500000; out->requested_mw = 600000; out->metadata.native_info_size = 4; out->native_control_size = 4; uint32_t a = callback_matches ? 42u : 43u, b = 56; std::memcpy(out->metadata.native_info, &a, 4); std::memcpy(out->native_control, &b, 4); return DC_OK; }
dc_status DC_CALL lock_gpu(dc_gpu*) { return DC_OK; }
dc_status DC_CALL unlock_gpu(dc_gpu*) { return DC_OK; }
dc_status DC_CALL capabilities(dc_gpu*, dc_capabilities* c) { c->present = DC_C_PL_MIN | DC_C_PL_MAX | DC_C_PL_DEF; c->pl_min_mw = 100000; c->pl_max_mw = 500000; c->pl_def_mw = 200000; return DC_OK; }
dc_status DC_CALL memory_capabilities(dc_gpu* gpu, dc_capabilities* c) {
    if (memory_caps_fail) { --memory_caps_fail; return DC_E_UNAVAILABLE; }
    capabilities(gpu, c); c->present |= DC_C_MEM_DIV; c->mem_div = 4; std::memcpy(c->mem_type, "GDDR6", sizeof("GDDR6")); return DC_OK;
}
dc_status DC_CALL power_read(dc_gpu*, dc_cl_statics*, int32_t* has, uint32_t* value, dc_error*) {
    if (power_reads_remaining_to_fail > 0) { --power_reads_remaining_to_fail; *has = 0; return DC_OK; }
    *has = 1; *value = board; return DC_OK;
}
dc_status DC_CALL bounds(dc_gpu*, dc_cl_statics*, int32_t xoc, int64_t* low, int64_t* high, dc_error* error) {
    if (power_bounds_failed) return DC_E_FAILED;
    if (power_bounds_unsupported) { error->kind = DC_ERR_UNSUPPORTED; return DC_E_UNAVAILABLE; }
    *low = 100000;
    *high = xoc && xoc_live ? 1999000 : 500000;
    return DC_OK;
}
dc_status DC_CALL power_set(dc_gpu*, dc_cl_statics*, int64_t value, int32_t, int32_t* ok, char*, size_t, size_t* needed) { ++power_writes; board = static_cast<uint32_t>(value) + (board_mismatch ? 1000 : 0); peer = derived_test ? 700 : 200; if (power_throw) throw std::runtime_error("post-dispatch power failure"); power_reads_remaining_to_fail = power_post_read_failures; *ok = !power_set_unverified; *needed = 1; return DC_OK; }
dc_status DC_CALL message(dc_gpu*, char* out, size_t, size_t* n) { out[0] = 0; *n = 1; return DC_OK; }
dc_status DC_CALL pins_get(dc_gpu*, uint32_t* ids, int64_t* vals, uint32_t, uint32_t* n) { *n = pin_present ? 1 : 0; if (*n) { ids[0] = 1; vals[0] = pinned_value; } return DC_OK; }
dc_status DC_CALL pins_set(dc_gpu*, const uint32_t* ids, const int64_t* values, uint32_t n) { ++pin_writes; if (n) { CHECK(n == 1 && ids[0] == 1); pinned_value = values[0]; } pin_present = n != 0; return DC_OK; }
dc_status DC_CALL policies(dc_gpu*, dc_cl_confirm_fn confirm, void* confirm_context, dc_cl_policy_inputs_fn input, void* context, dc_policy_row* rows, uint32_t, uint32_t* n, dc_error* error) {
    ++policy_reads; if (mutate_power_at_policy_read==policy_reads) peer=101;
    CHECK(confirm != nullptr);
    if (table_fail) {
        std::strcpy(error->message, "Policy INFO rejected (RM 0x3A)");
        return DC_E_FAILED;
    }
    if (derived_test) { uint32_t info = 42, control = 56; callback_answer = confirm(confirm_context, 1, 700, 500, &info, 1, &control, 1) != 0; }
    dc_cl_statics st{}; st.size = sizeof(st); int32_t has = 0; int64_t mw = 0; CHECK(input(context, &has, &mw, &st) == 0 && has && mw == board);
    *n = 2; rows[0].policy = 0; rows[0].board = 1; rows[0].unit = 0; rows[0].minimum = 100000; rows[0].maximum = 350000; rows[0].requested = board;
    rows[1].policy = 1; rows[1].unit = 1; rows[1].type = geometry_change ? 1 : 0; rows[1].minimum = 0; rows[1].maximum = geometry_change || (mutable_bounds && board != 200000) ? 501 : 500; rows[1].requested = peer; rows[1].writable = 1; rows[1].named_current = derived_test; rows[1].consistent = 1; return DC_OK;
}
dc_status DC_CALL policy_set(dc_gpu*, dc_cl_confirm_fn confirm, void*, dc_cl_policy_inputs_fn, void*, int64_t id, int32_t whole, int64_t value, int32_t pin, int32_t, int32_t* ok, char*, size_t, size_t* needed) {
    CHECK(confirm && id == 1 && whole && (policy_external_test || !pin_present)); ++policy_writes; *ok = !peer_fail; if (!peer_fail) { peer = value; if (pin) { pin_present = true; pinned_value = value; } if (policy_change_board) board += 1000; if (policy_drop_pin) pin_present = false; } *needed = 1; return DC_OK;
}
int domain_raw[32]{}, rail_raw[2]{}, advanced_writes = 0, xoc_flag = 0, advanced_fail_reads = 0, layout_version = 4; bool has_msvdd = false, advanced_refuse = false, flags_fail = false;
int fixture_clock_mode = 0;
bool effect_at_p0 = true, effect_ambiguous = false, effect_block_restore = false, effect_second_device = false;
dc_status DC_CALL effect_identity(const dc_gpu* g, dc_gpu_identity* out) {
    const auto status = identity(g, out);
    if (effect_second_device) {
        std::memcpy(out->slot, "0000:04:00.0", sizeof("0000:04:00.0"));
        std::memcpy(out->uuid, "GPU-B", sizeof("GPU-B"));
    }
    out->nvapi_selected = out->nvml_selected = out->nvapi_ok = out->nvml_ok;
    std::memcpy(out->nvapi_slot, out->slot, sizeof(out->slot));
    std::memcpy(out->nvml_slot, out->slot, sizeof(out->slot));
    return status;
}
dc_status DC_CALL effect_telemetry(dc_gpu* g, dc_telemetry* out) {
    const auto status = memory_telemetry(g, out);
    out->present |= DC_T_PSTATE; out->pstate = effect_at_p0 ? 0u : 8u; return status;
}
void with_effect_fixture(CoreApi& api) {
    api.identity = effect_identity;
    api.telemetry = effect_telemetry;
    api.clock_test_wait = [](unsigned) {};
    api.clkdom_write_capability = [](dc_gpu*, uint32_t, int32_t, dc_outcome* out) -> dc_status {out->ok = 1; return DC_OK;};
    api.public_clocks = [](dc_gpu*, dc_public_clock_readings* out) -> dc_status {
        out->present = 13u;
        out->mhz[0] = static_cast<uint32_t>(1200 + domain_raw[1]/1000 + (effect_ambiguous ? domain_raw[5]/1000 : 0));
        out->mhz[2] = static_cast<uint32_t>(4500 + domain_raw[2]/1000);
        out->mhz[3] = static_cast<uint32_t>(1500 + domain_raw[5]/1000);
        return DC_OK;
    };
}
Command effect_command(uint32_t domain) {
    Command command; command.operation = Command::Operation::verify_clock_offset;
    command.advanced = AdvancedId{AdvancedKind::clock_domain, domain}; return command;
}
void confirm_offset(IBackend& client, uint32_t domain) {
    const auto verified = client.execute(effect_command(domain));
    if (!verified.ok) std::fprintf(stderr, "Offset verification failed for domain %u: %s\n", domain, verified.message.c_str());
    CHECK(verified.ok);
}
int32_t DC_CALL dom_ok(dc_gpu*) { return 1; }
dc_status DC_CALL dom_layout(dc_gpu*, dc_clkdom_layout* l) { l->version = layout_version; l->block_size = 100; l->header = 4; l->stride = 12; l->freq_khz = 4; l->has_nvvdd_uv = 1; return DC_OK; }
dc_status DC_CALL dom_list(dc_gpu*, uint32_t* out, size_t, size_t* n) { out[0] = 0; out[1] = 1; out[2] = 2; out[3] = 5; *n = 4; return DC_OK; }
dc_status DC_CALL dom_controls(dc_gpu*, uint32_t* out, size_t, size_t* n) { out[0] = 1; out[1] = 2; out[2] = 5; *n = 3; return DC_OK; }
int32_t DC_CALL dom_offered(dc_gpu*, int64_t) { return 1; }
dc_status DC_CALL dom_read(dc_gpu*, dc_clkdom_requests* out) { if (advanced_fail_reads > 0) { --advanced_fail_reads; return DC_E_UNAVAILABLE; } out->count = 4; for (int i = 0; i < 4; ++i) { out->rows[i].domain = i == 3 ? 5 : i; out->rows[i].freq_khz = domain_raw[out->rows[i].domain]; out->rows[i].mode = fixture_clock_mode; } return DC_OK; }
dc_status DC_CALL dom_set(dc_gpu*, int64_t id, double value, dc_outcome* out) { ++advanced_writes; const bool refused = advanced_refuse || (effect_block_restore && value == 0); if (!refused) domain_raw[id] = static_cast<int>(value * 1000); out->ok = !refused; return DC_OK; }
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
// rail_set_last_count: fields named by the last setter call; rail_ignored_field:
// a field the fake driver accepts without storing (a readback mismatch).
uint32_t rail_set_last_count = 0; int rail_ignored_field = -1;
dc_status DC_CALL rail_raw_set(dc_gpu*, int32_t rail, const dc_rails_raw_request* request, int32_t enabled, int32_t, dc_outcome* out, dc_rails_setter_writes* write_info) { CHECK(enabled && (request->count == 1 || request->count == 2)); ++rail_set_calls; rail_set_last_count = request->count; for (uint32_t i = 0; i < request->count; ++i) { auto field = request->order[i]; CHECK(request->passed[field]); if (static_cast<int>(field) != rail_ignored_field) raw_limits[rail][field] = request->uv[field]; } write_info->calls = 1; if (rail_throw) throw std::runtime_error("post-write failure"); out->ok = 1; return DC_OK; }
double rail_live_mv = 920, rail_effective_mv = 1080; bool rail_state_fail = false; int rail_state_calls = 0;
dc_status DC_CALL rail_state(dc_gpu*, dc_rails_state_set* out) { ++rail_state_calls; if (rail_state_fail) return DC_E_FAILED; out->present = 1; out->rails[0].live = rail_live_mv; out->rails[0].effective = rail_effective_mv; out->rails[0].reliability = 1080; return DC_OK; }
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
bool max_move_request=false;
std::vector<double> max_planned_frequencies;
dc_status DC_CALL max_move_boost(dc_gpu* gpu, int64_t v, dc_outcome* out) {
    const auto status=boost_set(gpu,v,out);
    if (v==100) { base_shift=25; if (max_move_request) ++delta; }
    else base_shift=0;
    return status;
}
dc_status DC_CALL max_move_ramp(const dc_vf_point* points, size_t, const dc_vfp_num*, const dc_vfp_num*, const dc_vfp_num*, const dc_vfp_num*, dc_vfp_ramp* out) {
    max_planned_frequencies.push_back(points[0].freq_mhz);
    out->change_count=1; out->changes[0].pos=0; out->changes[0].idx=points[0].idx;
    out->changes[0].delta_khz.kind=DC_VFP_INT; out->changes[0].delta_khz.i=points[0].delta_khz+5000;
    return DC_OK;
}
double max_test_rail_mv = 925, max_test_headroom_mv = 0;
bool max_test_skip_rail=false;
std::vector<double> max_test_ramp_caps;
dc_status DC_CALL max_test_limits(dc_gpu*, dc_rails_limit_set* limits) {
    if (!max_test_rail_mv) return DC_E_UNAVAILABLE;
    limits->present=1;
    auto& rail=limits->rails[0]; rail.absolute_ok=rail.has_base=rail.has_effective=1;
    rail.effective_mv=boost_value==100 ? max_test_rail_mv : 875;
    rail.base[0]=875; rail.boost_mv=boost_value==100 ? 100 : 0;
    return DC_OK;
}
dc_status DC_CALL max_test_ceiling(const dc_rl_row* row, dc_rl_value* out) {
    CHECK(row->size==sizeof(*row) && row->bases_kind==DC_RL_BASES_MAPPING);
    CHECK(row->base[0].f==875 && row->boost_mv.f==100 && row->effective_mv.kind==DC_RL_FLOAT);
    *out=row->effective_mv; return DC_OK;
}
dc_status DC_CALL max_test_ramp(const dc_vf_point*, size_t, const dc_vfp_num*, const dc_vfp_num* cap,
                                const dc_vfp_num*, const dc_vfp_num*, dc_vfp_ramp* out) {
    max_test_ramp_caps.push_back(cap->f); out->change_count=0; return DC_OK;
}
dc_status DC_CALL max_test_headroom(dc_gpu*, double hold_mv, double, uint32_t, int32_t, int32_t,
        dc_rails_before_write_fn, void*, dc_rails_hold_outcome* out, dc_rails_setter_writes* writes) {
    max_test_headroom_mv=hold_mv;
    out->ok=DC_RAILS_HOLD_OK;
    if (max_test_skip_rail) {
        out->ok=DC_RAILS_HOLD_FAILED; out->reserved=DC_RAILS_HOLD_WRITER_ISSUED_NO_SET;
        writes->calls=writes->attributes=writes->last_attributes=1; writes->result.size=sizeof(writes->result);
        std::strcpy(out->message,"Fixture rail writer recognized no packet");
    }
    return DC_OK;
}
dc_status DC_CALL test_predict(const dc_vf_point*, size_t n, const dc_vfp_num* values, const dc_vfp_num*, int64_t* actual, uint32_t* order, size_t capacity) { if (capacity < n) return DC_E_BUFFER; for (size_t i = 0; i < n; ++i) { order[i] = static_cast<uint32_t>(i); actual[i] = values[i].i; } return DC_OK; }
dc_status DC_CALL test_reset(dc_gpu*, dc_cl_confirm_fn, void*, dc_cl_policy_inputs_fn, void*, dc_reset_statics_fn, void*, int32_t, dc_reset_step* steps, uint32_t cap, uint32_t* count, dc_rails_setter_writes*, dc_error*) {
    ++reset_calls; CHECK(cap >= DC_RESET_STEPS_MAX); core_value = 0; delta = 0; delta2 = 0;
    if (reset_api_failure) { *count = 0; return DC_E_FAILED; }
    *count = 2; std::memcpy(steps[0].name, "core offset", sizeof("core offset")); steps[0].ok = 1;
    std::snprintf(steps[0].message, sizeof(steps[0].message), "fixture offset zeroed");
    std::memcpy(steps[1].name, "fan", sizeof("fan")); steps[1].ok = reset_step_failure ? 0 : 1;
    std::snprintf(steps[1].message, sizeof(steps[1].message), "%s", reset_step_failure ? "fixture fan refusal" : "fixture fan to auto"); return DC_OK;
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
// Python apply_vlim with Link ticked: one Command names both reliability fields.
TEST_NOINLINE void linked_rail_tests(const CoreApi& rail_api, const Command& undo) {
    std::string error; raw_limits[1][0] = 500; raw_limits[1][1] = 600; raw_limits[1][2] = 700; raw_limits[1][3] = 800;
    auto client = create_core_backend_for_test(rail_api); CHECK(client->open("0000:02:00.0", error));
    Command gate{}; gate.operation = Command::Operation::set_rail_mode; gate.enabled = true; CHECK(client->execute(gate).ok);
    auto view = client->read(true); CHECK(view.rail_controls.size() == 8);
    const auto binding = [&](uint32_t rail, uint32_t field) {
        for (const auto& r : view.rail_controls) if (r.id.rail == rail && r.id.field == field) return r.binding;
        return std::string();
    };
    Command linked{}; linked.operation = Command::Operation::apply; linked.rail = RailId{1, 0}; linked.value = 1.0; linked.expected_binding = binding(1, 0);
    linked.rail_partner = RailPartnerRequest{RailId{1, 1}, 1.1, binding(1, 1)};
    CHECK(!linked.expected_binding->empty() && !linked.rail_partner->expected_binding.empty());

    // A changed partner binding refuses before any write to either field.
    auto stale = linked; stale.rail_partner->expected_binding = "rail:1:1:stale";
    int calls = rail_set_calls; auto refused = client->execute(stale);
    CHECK(!refused.ok && !refused.may_have_changed && rail_set_calls == calls && raw_limits[1][0] == 500 && raw_limits[1][1] == 600);
    rail_base = 901; refused = client->execute(linked); rail_base = 900; // both references moved
    CHECK(!refused.ok && rail_set_calls == calls && raw_limits[1][0] == 500 && raw_limits[1][1] == 600);
    // A partner on another rail, the same field, or a linked Initial is refused.
    auto other_rail = linked; other_rail.rail_partner->id.rail = 0; CHECK(!client->execute(other_rail).ok && rail_set_calls == calls);
    auto same_field = linked; same_field.rail_partner->id.field = 0; CHECK(!client->execute(same_field).ok && rail_set_calls == calls);
    // Python VLIM_LINKED names only reliability and alt-reliability: overvoltage and vmin never pair.
    for (uint32_t other : {2u, 3u}) {
        auto with_other = linked; with_other.rail_partner = RailPartnerRequest{RailId{1, other}, 1.1, binding(1, other)};
        CHECK(!with_other.rail_partner->expected_binding.empty());
        auto refused_pair = client->execute(with_other);
        CHECK(!refused_pair.ok && !refused_pair.may_have_changed && rail_set_calls == calls);
        for (uint32_t reliability : {0u, 1u}) {
            Command from_other{}; from_other.operation = Command::Operation::apply; from_other.rail = RailId{1, other}; from_other.value = 1.0;
            from_other.expected_binding = binding(1, other);
            from_other.rail_partner = RailPartnerRequest{RailId{1, reliability}, 1.1, binding(1, reliability)};
            refused_pair = client->execute(from_other);
            CHECK(!refused_pair.ok && !refused_pair.may_have_changed && rail_set_calls == calls);
        }
    }
    CHECK(raw_limits[1][0] == 500 && raw_limits[1][1] == 600 && raw_limits[1][2] == 700 && raw_limits[1][3] == 800);
    auto linked_stock = linked; linked_stock.operation = Command::Operation::stock; CHECK(!client->execute(linked_stock).ok && rail_set_calls == calls);
    CHECK(!client->read(false).undo_available);

    // One setter call writes and reads back both; Undo is one entry restoring both in one call.
    auto applied = client->execute(linked);
    CHECK(applied.ok && rail_set_calls == calls + 1 && rail_set_last_count == 2 && raw_limits[1][0] == 1000 && raw_limits[1][1] == 1100 && raw_limits[1][2] == 700);
    CHECK(client->read(false).undo_label == "Undo rail limit");
    auto undone = client->execute(undo);
    CHECK(undone.ok && rail_set_calls == calls + 2 && rail_set_last_count == 2 && raw_limits[1][0] == 500 && raw_limits[1][1] == 600);
    CHECK(!client->read(false).undo_available);

    // The partner's readback differs: failure is reported, Undo is kept and restores both.
    rail_ignored_field = 1; applied = client->execute(linked); rail_ignored_field = -1;
    CHECK(!applied.ok && applied.may_have_changed && raw_limits[1][0] == 1000 && raw_limits[1][1] == 600);
    CHECK(client->read(false).undo_label == "Undo rail limit");
    undone = client->execute(undo);
    CHECK(undone.ok && rail_set_last_count == 2 && raw_limits[1][0] == 500 && raw_limits[1][1] == 600 && !client->read(false).undo_available);

    // Unlinked apply: one field, one entry, exactly as before.
    Command single{}; single.operation = Command::Operation::apply; single.rail = RailId{1, 1}; single.value = 0.65; single.expected_binding = binding(1, 1);
    calls = rail_set_calls; CHECK(client->execute(single).ok && rail_set_calls == calls + 1 && rail_set_last_count == 1 && raw_limits[1][1] == 650 && raw_limits[1][0] == 500);
    CHECK(client->execute(undo).ok && rail_set_last_count == 1 && raw_limits[1][1] == 600 && !client->read(false).undo_available);
}
// Disk Undo of a linked apply names both fields' prior requests, an unlinked one
// only its own field. Undo of a linked entry after only the partner moved
// elsewhere still restores both prior requests in one call.
TEST_NOINLINE void linked_rail_history_tests(const CoreApi& rail_api, const Command& undo) {
    std::string error; raw_limits[1][0] = 500; raw_limits[1][1] = 600; raw_limits[1][2] = 700; raw_limits[1][3] = 800;
    std::vector<std::pair<std::string, ProfileJson>> saved;
    CoreApi with_history = rail_api; with_history.history = std::make_shared<ProfileHistorySink>();
    with_history.history->save = [&](const std::string& action, const ProfileJson& document) { saved.push_back({action, document}); };
    auto client = create_core_backend_for_test(with_history); CHECK(client->open("0000:02:00.0", error));
    Command gate{}; gate.operation = Command::Operation::set_rail_mode; gate.enabled = true; CHECK(client->execute(gate).ok);
    auto view = client->read(true);
    const auto binding = [&](uint32_t rail, uint32_t field) {
        for (const auto& r : view.rail_controls) if (r.id.rail == rail && r.id.field == field) return r.binding;
        return std::string();
    };
    const auto entry = [](const ProfileJson& document, uint32_t field) -> std::optional<int> {
        if (!document.contains("entries")) return std::nullopt;
        for (const auto& e : document["entries"])
            if (e.value("kind", "") == "rail" && e["rail"] == 1 && e["field"] == field) return e["value"].get<int>();
        return std::nullopt;
    };
    Command linked{}; linked.operation = Command::Operation::apply; linked.rail = RailId{1, 0}; linked.value = 1.0; linked.expected_binding = binding(1, 0);
    linked.rail_partner = RailPartnerRequest{RailId{1, 1}, 1.1, binding(1, 1)};
    int calls = rail_set_calls;
    CHECK(client->execute(linked).ok && rail_set_calls == calls + 1 && raw_limits[1][0] == 1000 && raw_limits[1][1] == 1100);
    CHECK(saved.size() == 1);
    if (saved.size() == 1) {
        CHECK(saved[0].first == "rail-limit" && saved[0].second["entries"].size() == 2);
        CHECK(entry(saved[0].second, 0) == 500 && entry(saved[0].second, 1) == 600);
    }
    // Only the partner changes outside this entry; Undo restores both, one call, no new disk record.
    raw_limits[1][1] = 1150; calls = rail_set_calls;
    CHECK(client->execute(undo).ok && rail_set_calls == calls + 1 && rail_set_last_count == 2 && raw_limits[1][0] == 500 && raw_limits[1][1] == 600);
    CHECK(saved.size() == 1 && !client->read(false).undo_available);
    // Unlinked: the disk record names only the moved field, as before.
    view = client->read(true);
    Command single{}; single.operation = Command::Operation::apply; single.rail = RailId{1, 1}; single.value = 0.65; single.expected_binding = binding(1, 1);
    CHECK(client->execute(single).ok && rail_set_last_count == 1 && raw_limits[1][1] == 650 && raw_limits[1][0] == 500);
    CHECK(saved.size() == 2);
    if (saved.size() == 2) CHECK(saved[1].second["entries"].size() == 1 && entry(saved[1].second, 1) == 600 && !entry(saved[1].second, 0));
    CHECK(client->execute(undo).ok && raw_limits[1][1] == 600);
}
// Python refresh_rail_live: the live/effective readout follows every poll and
// keeps the last reading, stale, after a failed read; without the export it is unknown.
TEST_NOINLINE void rail_readout_tests(const CoreApi& rail_api) {
    std::string error;
    CoreApi with_state = rail_api; with_state.rails.state = rail_state;
    with_state.voltage_evidence.caches = [](dc_gpu*, dc_rails_caches* cache) -> dc_status {
        cache->diagnostics[DC_RAILS_CONTROL][0] = {1, DC_RAILS_CONTROL_VERSION, 0, 1, 1};
        return DC_OK;
    };
    auto client = create_core_backend_for_test(with_state); CHECK(client->open("0000:02:00.0", error));
    rail_live_mv = 920; rail_effective_mv = 1080; rail_state_fail = false;
    // Focused (the default) never shows them: no rail-state read on any poll.
    const int focused_calls = rail_state_calls; auto view = client->read(true); view = client->read(false);
    CHECK(rail_state_calls == focused_calls && !view.rail_readouts);
    CHECK(view.rail_getter_cache && view.rail_getter_cache->diagnostics[DC_RAILS_CONTROL][0].version == DC_RAILS_CONTROL_VERSION);
    client->set_live_readouts(true);
    view = client->read(true);
    CHECK(view.rail_readouts && (*view.rail_readouts)[0].listed && (*view.rail_readouts)[1].listed && !(*view.rail_readouts)[0].stale);
    CHECK(view.rail_readouts && (*view.rail_readouts)[0].state && (*view.rail_readouts)[0].state->live == 920 && (*view.rail_readouts)[0].state->effective == 1080);
    CHECK(view.rail_readouts && !(*view.rail_readouts)[1].state);
    rail_live_mv = 905.5; const int calls = rail_state_calls; view = client->read(false);
    CHECK(rail_state_calls > calls && view.rail_readouts && (*view.rail_readouts)[0].state && (*view.rail_readouts)[0].state->live == 905.5 && (*view.rail_readouts)[0].listed);
    rail_state_fail = true; view = client->read(false); rail_state_fail = false;
    CHECK(view.rail_readouts && (*view.rail_readouts)[0].stale && (*view.rail_readouts)[0].state && (*view.rail_readouts)[0].state->live == 905.5);
    CHECK(view.rail_controls.size() == 8);
    view = client->read(false); CHECK(view.rail_readouts && !(*view.rail_readouts)[0].stale);
    client->set_live_readouts(false); const int hidden_calls = rail_state_calls; view = client->read(false); view = client->read(true);
    CHECK(rail_state_calls == hidden_calls);
    // The kept reading is not presented as live once Classic shows it again before its next read.
    CHECK(view.rail_readouts && (*view.rail_readouts)[0].stale && (*view.rail_readouts)[0].state && (*view.rail_readouts)[0].state->live == 905.5);
    client->set_live_readouts(true); view = client->read(false);
    CHECK(rail_state_calls > hidden_calls && view.rail_readouts && !(*view.rail_readouts)[0].stale);
    auto without = create_core_backend_for_test(rail_api); CHECK(without->open("0000:02:00.0", error));
    without->set_live_readouts(true); view = without->read(true);
    CHECK(view.rail_readouts && (*view.rail_readouts)[0].listed && !(*view.rail_readouts)[0].state && !(*view.rail_readouts)[0].stale && view.rail_controls.size() == 8);
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
void adaptive_max_tests(CoreApi actions, Command max_cmd, const Command& undo);
TEST_NOINLINE void model_input_inventory_tests(CoreApi base) {
    static unsigned reads; static bool mismatch, read_failed;
    reads = 0; mismatch = read_failed = false;
    base.identity = [](const dc_gpu* gpu, dc_gpu_identity* id) -> dc_status {
        identity(gpu,id); id->nvapi_ok=id->nvapi_selected=1; id->nvapi_handle=127;
        id->nvapi_devid=0x4321; id->nvapi_subsys=9; return DC_OK;
    };
    base.model_inputs = [](dc_gpu*,dc_model_inputs* out,dc_error* err) -> dc_status {
        ++reads;
        if (read_failed) { std::strcpy(err->message,"Fixture getter unavailable"); return DC_E_UNAVAILABLE; }
        out->target.size=sizeof(out->target); out->target.handle=mismatch ? 128 : 127;
        std::strcpy(out->target.slot,"0000:02:00.0"); out->target.device_id=0x4321; out->target.subsystem_id=9;
        auto& speedo=out->rows[DC_MODEL_SPEEDO]; speedo.state=DC_MODEL_READY; speedo.index=8;
        speedo.active=1207; speedo.raw_hardware=1210; speedo.default_value=1210;
        speedo.minimum=0; speedo.maximum=2000; speedo.override_present=1; speedo.override_value=1207;
        out->rows[DC_MODEL_SPEEDO1].state=DC_MODEL_AMBIGUOUS; out->rows[DC_MODEL_SPEEDO2].state=DC_MODEL_NOT_READY;
        auto& iddq=out->rows[DC_MODEL_IDDQ_NVVDD]; iddq.state=DC_MODEL_READY; iddq.minimum=1; iddq.maximum=0;
        return DC_OK;
    };
    auto client=create_core_backend_for_test(base); std::string error;
    CHECK(client->open("0000:02:00.0",error));
    CHECK(!client->read(true).model_inputs && reads==0);
    Command read; read.operation=Command::Operation::read_tuning_support;
    CHECK(!client->execute(read).may_have_changed);
    auto inventory=client->read(false).model_inputs;
    CHECK(inventory && inventory->error.empty() && reads==1);
    CHECK(inventory->values.rows[DC_MODEL_SPEEDO].active==1207 &&
          inventory->values.rows[DC_MODEL_SPEEDO].raw_hardware==1210 && inventory->values.rows[DC_MODEL_SPEEDO].override_present);
    CHECK(inventory->values.rows[DC_MODEL_SPEEDO1].state==DC_MODEL_AMBIGUOUS &&
          inventory->values.rows[DC_MODEL_SPEEDO2].state==DC_MODEL_NOT_READY &&
          inventory->values.rows[DC_MODEL_IDDQ_NVVDD].state==DC_MODEL_INVALID &&
          inventory->values.rows[DC_MODEL_IDDQ_MSVDD].state==DC_MODEL_ABSENT);
    CHECK(client->read(true).model_inputs==inventory && reads==1);
    mismatch=true; client->execute(read);
    CHECK(client->read(false).model_inputs->error.find("selected GPU")!=std::string::npos);
    mismatch=false; read_failed=true; client->execute(read);
    CHECK(client->read(false).model_inputs->error=="Fixture getter unavailable");
    CHECK(!client->rediscover_capabilities().model_inputs);
}
TEST_NOINLINE void power_and_actions_tests(const CoreApi& pa, const CoreApi& advanced_api, Command& undo, Command& power, Command& policy_cmd) {
    std::string error;
    CoreApi power_test = advanced_api; power_test.policy_read = alternate_policies; power_test.current_limits = core_current; power_test.pins_get = no_pins;
    power_test.cpc_live_state = cached_power_error;
    auto xp = create_core_backend_for_test(power_test); CHECK(xp->open("0000:02:00.0", error)); board = 200000; xoc_live = false; auto normal = xp->read(true); CHECK(normal.controls[2].high == 500.0);
    CHECK(normal.xoc_power_range_note.empty());
    {
        xoc_live = true;
        const int writes = power_writes;
        Command inspect;
        inspect.operation = Command::Operation::read_tuning_support;
        const auto result = xp->execute(inspect);
        const auto inspected = xp->read(false);
        CHECK(result.ok && !result.may_have_changed && inspected.inspected_xoc_max_w == 1999.0);
        CHECK(inspected.xoc_known && !inspected.xoc_enabled && inspected.controls[2].high == 500.0);
        CHECK(power_writes == writes && inspected.firmware_inventory);
        CHECK(inspected.model_inputs && !inspected.model_inputs->error.empty());
        model_input_inventory_tests(power_test);
        xoc_live = false;
    }
    CHECK(normal.power_policies.size() == 2 && normal.power_policies[1].id == 7 && normal.power_policies[1].core_current && normal.power_policies[1].label == "Core current (NVVDD)");
    current_read_fails = true; auto retained = xp->read(true); CHECK(retained.power_policies[1].core_current); current_read_fails = false;
    current_generic_label = true; auto generic = xp->read(true); CHECK(!generic.power_policies[1].core_current); current_generic_label = false; current_index = 19; auto alternate = xp->read(true); CHECK(alternate.power_policies[1].id == 19 && alternate.power_policies[1].core_current);
    Command mode_on{};
    mode_on.operation = Command::Operation::set_xoc;
    mode_on.enabled = true;
    CHECK(xp->execute(mode_on).ok);
    auto limited = xp->read(false);
    CHECK(limited.controls[2].high == 500.0 && limited.controls[2].detail.find("100 to 500 W") != std::string::npos);
    CHECK(limited.xoc_power_range_note.find("100 to 500 W") != std::string::npos &&
          limited.xoc_power_range_note.find("Client power metadata unavailable") != std::string::npos);

    Command xoc_power{};
    xoc_power.operation = Command::Operation::apply;
    xoc_power.control = ControlId::power;
    xoc_power.value = 1000;
    int writes_before = power_writes;
    const auto refused = xp->execute(xoc_power);
    CHECK(!refused.ok && refused.requested == 1000.0 && refused.message.find("100 to 500 W") != std::string::npos);
    CHECK(power_writes == writes_before && board == 200000);

    xoc_live = true;
    auto expanded = xp->read(false);
    CHECK(expanded.controls[2].high == 1999.0);
    CHECK(expanded.xoc_power_range_note.find("last known controller range") != std::string::npos);
    CHECK(expanded.xoc_power_range_note.find("1999 W") != std::string::npos &&
          expanded.xoc_power_range_note.find("written and read back") == std::string::npos);
    power_bounds_failed = true;
    const auto stale = xp->read(false);
    CHECK(stale.controls[2].availability == Availability::transient && stale.controls[2].high == 1999.0);
    const auto unreadable = xp->execute(xoc_power);
    CHECK(!unreadable.ok && unreadable.message.find("bounds or XOC state unavailable") != std::string::npos);
    CHECK(power_writes == writes_before && board == 200000);
    power_bounds_failed = false;
    const auto applied = xp->execute(xoc_power);
    CHECK(applied.ok && applied.readback == 1000.0 && power_writes == writes_before + 1 && board == 1000000);
    xoc_power.value = 1999;
    CHECK(xp->execute(xoc_power).ok && power_writes == writes_before + 2 && board == 1999000);
    CHECK(xp->read(false).xoc_power_range_note.find("written and read back in this session was 1999 W") != std::string::npos);
    CHECK(xp->read(false).inspected_xoc_note.find("written and read back in this session was 1999 W") != std::string::npos);
    mode_on.enabled = false;
    CHECK(xp->execute(mode_on).ok);
    auto carry = xp->read(false);
    CHECK(carry.controls[2].high == 500.0 && carry.controls[2].current == 1999.0);
    CHECK(xp->execute(xoc_power).ok && power_writes == writes_before + 2); // exact request is a no-op
    xoc_power.value = 900;
    CHECK(!xp->execute(xoc_power).ok && power_writes == writes_before + 2 && board == 1999000);
    xoc_power.value = 200;
    CHECK(xp->execute(xoc_power).ok && board == 200000 && xp->read(false).controls[2].high == 500.0);
    Command power_undo{Command::Operation::undo};
    const int pins_before = pin_writes;
    CHECK(!xp->execute(power_undo).ok && power_writes == writes_before + 3 && pin_writes == pins_before);

    power_bounds_unsupported = true;
    const auto read_only = xp->read(false);
    CHECK(read_only.controls[2].availability == Availability::unsupported && !read_only.controls[2].high &&
          read_only.controls[2].current == 200.0 && !read_only.controls[2].stock_writable);
    xoc_power.value = 210;
    CHECK(!xp->execute(xoc_power).ok && power_writes == writes_before + 3);
    power_bounds_unsupported = false;

    power_post_read_failures = 1;
    const auto recovered = xp->execute(xoc_power);
    CHECK(recovered.ok && recovered.readback == 210.0 && power_writes == writes_before + 4);
    xoc_power.value = 220;
    power_post_read_failures = 2;
    const auto verified = xp->execute(xoc_power);
    CHECK(verified.ok && !verified.readback && power_writes == writes_before + 5);
    xoc_power.value = 230;
    power_set_unverified = true;
    const auto unverified = xp->execute(xoc_power);
    CHECK(!unverified.ok && !unverified.readback && power_writes == writes_before + 6);
    power_set_unverified = false;
    power_post_read_failures = 0;

    // Imported requests can still carry exact milliwatts even though Daily edits are whole watts.
    xoc_power.value = 128.002;
    CHECK(xp->execute(xoc_power).ok && board == 128002);
    writes_before = power_writes;
    xoc_power.value = 128.0025;
    const auto fractional_mw = xp->execute(xoc_power);
    CHECK(!fractional_mw.ok && fractional_mw.message.find("whole number of milliwatts") != std::string::npos);
    CHECK(power_writes == writes_before && board == 128002);
    xoc_power.value = 200;
    CHECK(xp->execute(xoc_power).ok && board == 200000);
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
    // Each step's own result is reported beside the unchanged joined message.
    const auto steps_in_message = [](const ActionResult& r) { size_t at = 0;
        for (const auto& s : r.steps) { at = r.message.find(s.message, at); if (s.message.empty() || at == std::string::npos) return false; at += s.message.size(); }
        return !r.steps.empty(); };
    fan_refuse = true; auto partial_max = action_client->execute(max_cmd); CHECK(!partial_max.ok && power_writes == before_max_power && action_client->read(false).undo_available); fan_refuse = false;
    CHECK(partial_max.steps.size() == 1 && partial_max.steps[0].name == "fan" && !partial_max.steps[0].ok && steps_in_message(partial_max));
    CHECK(action_client->execute(undo).ok && fan_level == 40);
    board = 1000000; xoc_flag = 1; xoc_live = true; auto max_result = action_client->execute(max_cmd); CHECK(max_result.ok && board == 500000 && boost_value == 100 && test_vf_held);
    CHECK(max_result.steps.size() == 4 && max_result.steps[0].name == "fan" && max_result.steps[1].name == "board power" && max_result.steps[2].name == "voltage boost"
        && max_result.steps[3].name == "V/F point hold" && std::all_of(max_result.steps.begin(), max_result.steps.end(), [](const ActionStep& s) { return s.ok; })
        && steps_in_message(max_result));
    CHECK(action_client->execute(undo).ok && !test_vf_held && board == 1000000 && boost_value == 10 && fan_level == 40);
    // Evaluated frequencies may move after boost while signed requests stay put.
    {
        auto moving=actions; moving.boost_set=max_move_boost; moving.planner.ramp=max_move_ramp;
        auto client=create_core_backend_for_test(moving); CHECK(client->open("0000:02:00.0",error));
        auto input=client->read(true); Command command=max_cmd; command.expected_curve=input.curve; command.expected_curve_freq_div=input.curve_freq_div;
        max_planned_frequencies.clear(); const int old_delta=delta; const int old_writes=curve_writes;
        const auto moved=client->execute(command);
        CHECK(moved.ok && curve_writes==old_writes+1 && max_planned_frequencies.size()==2 && max_planned_frequencies[1]==max_planned_frequencies[0]+25);
        CHECK(client->execute(undo).ok && delta==old_delta && base_shift==0);
        max_move_request=true; const int writes_before_rejection=curve_writes;
        const auto rejected=client->execute(command);
        CHECK(!rejected.ok && curve_writes==writes_before_rejection && rejected.message.find("signed requests changed")!=std::string::npos);
        max_move_request=false; delta=old_delta; CHECK(client->execute(undo).ok && base_shift==0);
        client->close();
    }
    adaptive_max_tests(actions, max_cmd, undo);
    power_throw = true; bool caught_max = false; try { action_client->execute(max_cmd); } catch (const std::exception&) { caught_max = true; } power_throw = false;
    CHECK(caught_max && board == 500000 && action_client->read(false).undo_available); CHECK(action_client->execute(undo).ok && board == 1000000 && fan_level == 40);
    Command reset_command{}; reset_command.operation = Command::Operation::reset_controls; test_foreign_clock = true; int old_reset_calls = reset_calls; CHECK(!action_client->execute(reset_command).ok && reset_calls == old_reset_calls); test_foreign_clock = false;
    nonlinear_curve = true; reset_step_failure = true; core_value = 30; delta = 123; delta2 = 456; auto reset_failed = action_client->execute(reset_command); CHECK(!reset_failed.ok && reset_failed.may_have_changed && action_client->read(false).undo_available); reset_step_failure = false;
    CHECK(reset_failed.steps.size() == 2 && reset_failed.steps[0].name == "core offset" && reset_failed.steps[0].ok && reset_failed.steps[0].message == "fixture offset zeroed"
        && reset_failed.steps[1].name == "fan" && !reset_failed.steps[1].ok && reset_failed.steps[1].message == "fixture fan refusal"
        && reset_failed.message.rfind("Reset incomplete\ncore offset: fixture offset zeroed\nfan: fixture fan refusal", 0) == 0);
    CHECK(action_client->execute(undo).ok && core_value == 30 && delta == 123 && delta2 == 456); nonlinear_curve = false;
    const int calls_before_ok = reset_calls; auto reset_ok = action_client->execute(reset_command);
    CHECK(reset_ok.ok && reset_calls == calls_before_ok + 1 && reset_ok.steps.size() == 2 && reset_ok.steps[0].ok && reset_ok.steps[1].ok
        && reset_ok.steps[1].message == "fixture fan to auto" && reset_ok.message.rfind("Reset controls completed\ncore offset: fixture offset zeroed\nfan: fixture fan to auto", 0) == 0);
    CHECK(action_client->execute(undo).ok && core_value == 30 && delta == 123 && delta2 == 456);
    reset_api_failure = true; reset_failed = action_client->execute(reset_command); CHECK(!reset_failed.ok && reset_failed.may_have_changed && action_client->read(false).undo_available); reset_api_failure = false;
    board = 200000; peer = 100; pin_present = false; action_client->read(true); const int policy_before_power = policy_reads;
    Command refresh_power{}; refresh_power.operation = Command::Operation::apply; refresh_power.control = ControlId::power; refresh_power.value = 250;
    CHECK(action_client->execute(refresh_power).ok && policy_reads > policy_before_power); const auto refreshed_power = action_client->read(false);
    CHECK(refreshed_power.power_policies.size() == 2 && refreshed_power.power_policies[1].state.current == 0.2);
}
// Python refresh_power_policies: a milliwatt row's reading and effective limit
// come from the same policy row as a current row's; an inconsistent record or a
// missing word stays unknown.
bool watt_row_consistent = true; int watt_table_reads = 0; int64_t watt_row_value = 312250, watt_row_limit = 450000;
dc_status DC_CALL watt_policies(dc_gpu*, dc_cl_confirm_fn, void*, dc_cl_policy_inputs_fn, void*, dc_policy_row* rows, uint32_t, uint32_t* n, dc_error*) {
    ++watt_table_reads; *n = 2; rows[0].policy = 0; rows[0].board = 1; rows[0].unit = 0; rows[0].minimum = 100000; rows[0].maximum = 500000; rows[0].requested = board; rows[0].consistent = 1; rows[0].limit = 480000; rows[0].value = 301000;
    rows[1].policy = 3; rows[1].unit = 0; rows[1].minimum = 1000; rows[1].maximum = 600000; rows[1].requested = 450000; rows[1].writable = 1;
    rows[1].consistent = watt_row_consistent; rows[1].limit = watt_row_limit; rows[1].value = watt_row_value; return DC_OK;
}
TEST_NOINLINE void watt_policy_readback_tests(const CoreApi& pa) {
    std::string error; CoreApi watt_api = pa; watt_api.policy_read = watt_policies; watt_api.pins_get = no_pins;
    auto client = create_core_backend_for_test(watt_api); CHECK(client->open("0000:02:00.0", error)); board = 200000;
    const int reads = watt_table_reads; auto view = client->read(true);
    CHECK(watt_table_reads == reads + 1 && view.power_policies.size() == 2); // the one table read the amp rows use
    if (view.power_policies.size() == 2) {
        const auto& w = view.power_policies[1];
        CHECK(w.state.unit == "W" && w.observed_watts && *w.observed_watts == 312.25 && w.limit_watts && *w.limit_watts == 450.0 && !w.observed_amps && !w.limit_amps);
        CHECK(view.power_policies[0].observed_watts && *view.power_policies[0].observed_watts == 301.0 && view.power_policies[0].limit_watts && *view.power_policies[0].limit_watts == 480.0);
    }
    watt_row_consistent = false; view = client->read(true);
    CHECK(view.power_policies.size() == 2 && !view.power_policies[1].observed_watts && !view.power_policies[1].limit_watts);
    watt_row_consistent = true; watt_row_value = -1; view = client->read(true);
    CHECK(view.power_policies.size() == 2 && !view.power_policies[1].observed_watts && view.power_policies[1].limit_watts);
    watt_row_value = 312250;
}
// Python native_power_limit_for_ui: a read-only native effective limit on every
// Classic poll; Focused never reads it, and a failed read is unknown.
int native_reads = 0; int32_t native_has = 1; uint32_t native_mw = 972070; dc_status native_status = DC_OK;
dc_status DC_CALL native_limit(dc_gpu*, dc_cl_statics* st, int32_t* has, uint32_t* value, char*, size_t, size_t* needed) {
    ++native_reads; CHECK(st && st->size == sizeof(*st) && needed);
    *has = native_has; *value = native_has ? native_mw : 0; if (needed) *needed = 1; return native_status;
}
int nominal_error_kind = DC_ERR_UNSUPPORTED;
int nominal_sets = 0;
bool nominal_mismatch = false;
dc_status DC_CALL no_private_table(dc_gpu*, dc_cl_confirm_fn, void*, dc_cl_policy_inputs_fn, void*,
                                   dc_policy_row*, uint32_t, uint32_t* count, dc_error* error) {
    *count = 0; error->kind = nominal_error_kind; return DC_E_FAILED;
}
dc_status DC_CALL nominal_nvml_set(dc_gpu*, dc_cl_statics*, int64_t mw, dc_outcome* out, int32_t* has, uint32_t* got) {
    ++nominal_sets;
    board = static_cast<uint32_t>(mw) + (nominal_mismatch ? 1000 : 0);
    *has = 1; *got = board; out->ok = 1; return DC_OK;
}
TEST_NOINLINE void nominal_board_only_tests(const CoreApi& pa) {
    const auto previous = board;
    const bool previous_pin = pin_present;
    pin_present = false; board = 200000; nominal_sets = 0;
    auto a = pa; a.policy_read = no_private_table; a.power_set_nvml = nominal_nvml_set;
    auto client = create_core_backend_for_test(a); std::string error; CHECK(client->open("0000:02:00.0", error));
    Command apply{Command::Operation::apply, ControlId::power, 250}, undo{Command::Operation::undo};
    const auto ready = client->read(true);
    CHECK(ready.controls[2].writable && ready.controls[2].high == 500 && ready.power_policies.empty());
    const auto pins_before = pin_writes, full_before = power_writes;
    CHECK(client->execute(apply).ok && nominal_sets == 1 && board == 250000);
    pin_present = true;
    CHECK(!client->execute(undo).ok && nominal_sets == 1 && pin_present);
    pin_present = false;
    nominal_error_kind = DC_ERR_TRANSIENT;
    CHECK(!client->execute(undo).ok && nominal_sets == 1);
    nominal_error_kind = DC_ERR_UNSUPPORTED;
    CHECK(client->execute(undo).ok && board == 200000 && nominal_sets == 2);
    apply.value = 1999;
    CHECK(!client->execute(apply).ok && nominal_sets == 2);
    apply.value = 250;
    for (const auto kind : {DC_ERR_INVALID, DC_ERR_TRANSIENT, DC_ERR_MALFORMED}) {
        nominal_error_kind = kind;
        CHECK(!client->execute(apply).ok && nominal_sets == 2);
    }
    nominal_error_kind = DC_ERR_UNSUPPORTED; pin_present = true;
    CHECK(!client->execute(apply).ok && nominal_sets == 2);
    CHECK(!client->execute(Command{Command::Operation::stock, ControlId::power}).ok && power_writes == full_before);
    pin_present = false; nominal_mismatch = true;
    CHECK(!client->execute(apply).ok && board == 251000 && nominal_sets == 3);
    nominal_mismatch = false;
    CHECK(client->execute(undo).ok && board == 200000 && nominal_sets == 4);
    CHECK(pin_writes == pins_before && power_writes == full_before);
    board = previous; pin_present = previous_pin;
}
TEST_NOINLINE void native_power_limit_tests(const CoreApi& pa) {
    std::string error; CoreApi native_api = pa; native_api.native_power = native_limit; native_api.pins_get = no_pins;
    auto client = create_core_backend_for_test(native_api); CHECK(client->open("0000:02:00.0", error)); board = 200000;
    const int power_before = power_writes, policy_before = policy_writes, pins_before = pin_writes;
    // Focused (the default): no native read on a detail or ordinary poll.
    int reads = native_reads; auto view = client->read(true); view = client->read(false);
    CHECK(native_reads == reads && !view.native_power_limit_w);
    client->set_live_readouts(true);
    view = client->read(false); CHECK(native_reads == reads + 1 && view.native_power_limit_w && *view.native_power_limit_w == 972.07);
    view = client->read(true); CHECK(native_reads == reads + 2 && view.native_power_limit_w && *view.native_power_limit_w == 972.07);
    // A failed read shows unavailable, never the last value.
    native_has = 0; view = client->read(false); CHECK(!view.native_power_limit_w);
    native_has = 1; native_status = DC_E_FAILED; view = client->read(false); CHECK(!view.native_power_limit_w); native_status = DC_OK;
    native_mw = 0xFFFFFFFFu; view = client->read(false); CHECK(!view.native_power_limit_w);
    native_mw = 0; view = client->read(false); CHECK(view.native_power_limit_w && *view.native_power_limit_w == 0.0); // Python: 0 <= mW
    native_mw = 972070; view = client->read(false); CHECK(view.native_power_limit_w && *view.native_power_limit_w == 972.07);
    // Hidden again: no read, and the value is not carried as current.
    client->set_live_readouts(false); reads = native_reads; view = client->read(false);
    CHECK(native_reads == reads && !view.native_power_limit_w);
    CoreApi bare = native_api; bare.native_power = nullptr; auto without = create_core_backend_for_test(bare); CHECK(without->open("0000:02:00.0", error));
    without->set_live_readouts(true); CHECK(!without->read(false).native_power_limit_w);
    CHECK(power_writes == power_before && policy_writes == policy_before && pin_writes == pins_before); // read-only
}
// Python refresh_power_policies: while Classic's Power policies section is open,
// an ordinary poll re-reads the table at most once a second (POLICY_REFRESH_S);
// a closed section, Focused or an unread table makes no read. A failed refresh
// keeps the last rows, marked unavailable. The read carries this session's pins.
bool refresh_table_fails = false;
dc_status DC_CALL refresh_policies(dc_gpu* gpu, dc_cl_confirm_fn confirm, void* context, dc_cl_policy_inputs_fn inputs, void* input_context, dc_policy_row* rows, uint32_t capacity, uint32_t* n, dc_error* error) {
    if (refresh_table_fails) { ++watt_table_reads; *n = 0; return DC_E_FAILED; }
    return watt_policies(gpu, confirm, context, inputs, input_context, rows, capacity, n, error);
}
dc_status DC_CALL one_pin(dc_gpu*, uint32_t* ids, int64_t* values, uint32_t, uint32_t* n) { *n = 1; ids[0] = 3; values[0] = 450000; return DC_OK; }
TEST_NOINLINE void policy_refresh_tests(const CoreApi& pa) {
    std::string error; CoreApi refresh_api = pa; refresh_api.policy_read = refresh_policies; refresh_api.pins_get = one_pin;
    auto client = create_core_backend_for_test(refresh_api); CHECK(client->open("0000:02:00.0", error)); board = 200000;
    const int power_before = power_writes, policy_before = policy_writes, pins_before = pin_writes;
    // An open section before the table was ever read: nothing to refresh.
    client->set_live_readouts(true); client->set_policy_readouts(true);
    int reads = watt_table_reads; auto view = client->read(false); CHECK(watt_table_reads == reads && view.power_policies.empty());
    view = client->read(true); CHECK(view.power_policies.size() == 2);
    if (view.power_policies.size() == 2) CHECK(view.power_policies[1].pinned && *view.power_policies[1].pinned == 450.0 && !view.power_policies[0].pinned);
    // The detail read just captured it: a poll within the second makes no read.
    reads = watt_table_reads; view = client->read(false); CHECK(watt_table_reads == reads);
    Sleep(1050);
    watt_row_value = 333000; view = client->read(false); CHECK(watt_table_reads == reads + 1);
    if (view.power_policies.size() == 2) CHECK(view.power_policies[1].observed_watts && *view.power_policies[1].observed_watts == 333.0);
    view = client->read(false); view = client->read(false); CHECK(watt_table_reads == reads + 1); // at most once a second
    // A closed section and Focused make no read.
    Sleep(1050);
    client->set_policy_readouts(false); reads = watt_table_reads; view = client->read(false); CHECK(watt_table_reads == reads);
    client->set_policy_readouts(true); client->set_live_readouts(false); view = client->read(false); CHECK(watt_table_reads == reads);
    // A failed refresh keeps the rows, marked unavailable, and leaves the board request alone.
    client->set_live_readouts(true); refresh_table_fails = true; view = client->read(false); CHECK(watt_table_reads > reads);
    CHECK(view.power_policies.size() == 2 && view.power_policies[1].state.availability == Availability::transient && view.power_policies[1].state.current == 450.0);
    CHECK(view.controls[static_cast<size_t>(ControlId::power)].availability == Availability::available);
    refresh_table_fails = false; watt_row_value = 312250;
    CHECK(power_writes == power_before && policy_writes == policy_before && pin_writes == pins_before); // read-only
}
// Kept out of main(): main's frame already holds dozens of LiveData values,
// and the nested backend calls overflow the default 1 MB test stack.
TEST_NOINLINE void clock_domain_pairing_tests(const CoreApi& clock_api) {
    std::string error;
    CoreApi paired_api=clock_api; paired_api.clkdom_pairing=clock_pairing; paired_api.clkdom_blackwell=clock_blackwell; paired_api.capabilities=memory_capabilities;
    auto paired=create_core_backend_for_test(paired_api); CHECK(paired->open("0000:02:00.0", error));
    // Focused (live readouts off) never asks the core for the pairing.
    pairing_calls=0; blackwell_calls=0;
    auto paired_state=paired->read(false); paired_state=paired->read(true);
    CHECK(pairing_calls==0 && blackwell_calls==0 && paired_state.clock_domain_pairs.empty() && !paired_state.clock_domain_requests_only);
    CHECK(paired_state.memory_divisor==4u && paired_state.memory_type=="GDDR6");
    paired->set_live_readouts(true); paired_state=paired->read(false);
    CHECK(pairing_calls==1 && blackwell_calls==1);
    CHECK(paired_state.clock_domain_pairs.size()==1 && paired_state.clock_domain_pairs[0].control==3 && paired_state.clock_domain_pairs[0].domain==0
        && paired_live==std::vector<uint32_t>{0} && !paired_state.clock_domain_requests_only);
    memory_caps_fail=8; requests_only=1; paired_state=paired->read(false);
    CHECK(paired_state.memory_divisor==4u && paired_state.memory_type=="GDDR6" && paired_state.clock_domain_requests_only);
    // Hidden again: no call, and the last pairing is kept.
    paired->set_live_readouts(false); const int pairs_before=pairing_calls, blackwell_before=blackwell_calls; requests_only=0;
    paired_state=paired->read(false);
    CHECK(pairing_calls==pairs_before && blackwell_calls==blackwell_before && paired_state.clock_domain_pairs.size()==1 && paired_state.clock_domain_requests_only);
    memory_caps_fail=0; requests_only=0;
}

TEST_NOINLINE void clock_discovery_poll_tests(CoreApi api) {
    static bool partial;
    static int retry_calls, control_reads;
    partial = true;
    retry_calls = control_reads = 0;
    api.clkdom_controls = [](dc_gpu* gpu, uint32_t* out, size_t capacity, size_t* count) -> dc_status {
        ++control_reads;
        if (partial) { out[0] = 1; *count = 1; return DC_OK; }
        return dom_controls(gpu, out, capacity, count);
    };
    api.clock_retry = [](dc_gpu*, int32_t has_time, double, int32_t* changed) -> dc_status {
        CHECK(!has_time); // The core owns its monotonic schedule.
        ++retry_calls;
        *changed = retry_calls == 3;
        if (*changed) partial = false;
        return retry_calls == 2 ? DC_E_UNAVAILABLE : DC_OK;
    };
    auto has_clock = [](const LiveData& data, uint32_t domain) {
        return std::any_of(data.advanced_controls.begin(), data.advanced_controls.end(),
            [&](const AdvancedControl& row) {
                return row.id.kind == AdvancedKind::clock_domain && row.id.domain == domain;
            });
    };
    std::string error;
    auto client = create_core_backend_for_test(api);
    CHECK(client->open("0000:02:00.0", error));
    auto state = client->read(true);
    CHECK(retry_calls == 0 && has_clock(state, 1) && !has_clock(state, 5));
    const int initial_reads = control_reads, initial_writes = advanced_writes;
    state = client->read(false);
    CHECK(retry_calls == 1 && control_reads == initial_reads && !has_clock(state, 5));
    state = client->read(false);
    CHECK(retry_calls == 2 && control_reads == initial_reads && has_clock(state, 1));
    changed_id = true;
    state = client->read(false);
    changed_id = false;
    CHECK(!state.connected && retry_calls == 2 && control_reads == initial_reads);
    state = client->read(false);
    CHECK(state.connected && retry_calls == 3 && control_reads > initial_reads && has_clock(state, 5));
    CHECK(has_clock(state, 1) && advanced_writes == initial_writes);
    const int recovered_reads = control_reads;
    state = client->read(false);
    CHECK(retry_calls == 4 && control_reads == recovered_reads && has_clock(state, 5));

    static int rescans;
    rescans = 0;
    partial = true;
    api.refresh_capabilities = [](dc_gpu*, dc_clkdom_error*) -> dc_status {
        ++rescans;
        partial = false;
        return DC_OK;
    };
    auto manual = create_core_backend_for_test(api);
    CHECK(manual->open("0000:02:00.0", error));
    CHECK(!has_clock(manual->read(true), 5) && rescans == 0);
    changed_id = true;
    CHECK(!manual->rediscover_capabilities().connected && rescans == 0);
    changed_id = false;
    CHECK(has_clock(manual->rediscover_capabilities(), 5) && rescans == 1);
    CHECK(has_clock(manual->read(true), 5) && rescans == 1 && advanced_writes == initial_writes);
}
TEST_NOINLINE void clock_write_capability_tests(CoreApi api) {
    static bool setter_present;
    setter_present = true;
    api.clkdom_write_capability = [](dc_gpu*, uint32_t domain, int32_t, dc_outcome* out) -> dc_status {
        out->ok = setter_present && domain != 1;
        if (!out->ok) std::snprintf(out->message, sizeof(out->message), "Fixture read-only clock");
        return DC_OK;
    };
    const auto row = [](const LiveData& data, uint32_t domain) -> ControlState {
        for (const auto& entry : data.advanced_controls)
            if (entry.id.domain == domain && entry.id.kind == AdvancedKind::clock_domain) return entry.state;
        CHECK(false);
        return {};
    };
    auto client = create_core_backend_for_test(api);
    std::string error;
    CHECK(client->open("0000:02:00.0", error));
    auto state = client->read(true);
    const auto restricted = row(state, 1);
    CHECK(restricted.current == 0 && restricted.availability == Availability::available);
    CHECK(!restricted.writable && !restricted.stock_writable && restricted.detail == "Fixture read-only clock");
    CHECK(!row(state, 5).writable);
    const int original_writes = advanced_writes;
    Command change;
    change.operation = Command::Operation::apply;
    change.advanced = AdvancedId{AdvancedKind::clock_domain, 1};
    change.value = 12;
    CHECK(!client->execute(change).ok && advanced_writes == original_writes);
    change.advanced->domain = 5;
    CHECK(!client->execute(change).ok && advanced_writes == original_writes);
    effect_at_p0 = false;
    CHECK(!client->execute(effect_command(5)).ok && advanced_writes == original_writes);
    effect_at_p0 = true; effect_ambiguous = true;
    CHECK(!client->execute(effect_command(5)).ok && domain_raw[5] == 0 && !row(client->read(true), 5).writable);
    effect_ambiguous = false;
    confirm_offset(*client, 5);
    CHECK(row(client->read(true), 5).writable);
    ++fixture_clock_mode;
    CHECK(!client->execute(change).ok && !row(client->read(true), 5).writable);
    --fixture_clock_mode;
    CHECK(!row(client->read(true), 5).writable);
    confirm_offset(*client, 5);
    ++layout_version;
    CHECK(!row(client->read(true), 5).writable);
    --layout_version;
    confirm_offset(*client, 5);
    const int initial_writes = advanced_writes;
    CHECK(client->execute(change).ok && advanced_writes == initial_writes + 1);
    setter_present = false;
    state = client->read(true);
    CHECK(row(state, 5).current == 12 && !row(state, 5).writable && !row(state, 2).stock_writable);
    change.operation = Command::Operation::stock;
    CHECK(!client->execute(change).ok && advanced_writes == initial_writes + 1);
    Command undo;
    undo.operation = Command::Operation::undo;
    CHECK(!client->execute(undo).ok && client->read(false).undo_available && advanced_writes == initial_writes + 1);
    setter_present = true;
    CHECK(client->execute(undo).ok && domain_raw[5] == 0);
    effect_block_restore = true;
    CHECK(!client->execute(effect_command(5)).ok && domain_raw[5] != 0 && client->auxiliary_cleanup_needed());
    const auto pending = client->read(true);
    CHECK(std::any_of(pending.advanced_controls.begin(), pending.advanced_controls.end(), [](const AdvancedControl& c) {
        return c.id.domain == 5 && c.clock_check == ClockCheck::recovery && !c.state.writable;
    }));
    CHECK(!client->prepare_close().ok && !client->execute(effect_command(2)).ok);
    effect_block_restore = false;
    Command recover = effect_command(5); recover.operation = Command::Operation::restore_clock_test;
    CHECK(client->execute(recover).ok && domain_raw[5] == 0 && !client->auxiliary_cleanup_needed());
    confirm_offset(*client, 5);
    CHECK(row(client->read(true), 5).writable);
    client->close();
    effect_second_device = true;
    CHECK(client->open("0000:04:00.0", error));
    change.operation = Command::Operation::apply;
    CHECK(!row(client->read(true), 5).writable && !client->execute(change).ok);
    client->close(); effect_second_device = false;
    api.clkdom_set = nullptr;
    auto reader = create_core_backend_for_test(api);
    CHECK(reader->open("0000:02:00.0", error));
    const auto no_export = row(reader->read(true), 5);
    CHECK(no_export.current == 0 && !no_export.writable && !no_export.stock_writable);
}
TEST_NOINLINE void known_clock_pattern_tests(CoreApi api, const wchar_t* directory) {
    static bool match, setter, zero_mask;
    static int pattern_reads[DC_CLKDOM_SLOTS];
    match = setter = true; zero_mask = false;
    api.clkdom_known_pattern = [](dc_gpu*, uint32_t id, int32_t mode, uint32_t* mask, dc_outcome* out) -> dc_status {
        ++pattern_reads[id];
        if (!match || mode != 0 || layout_version != 4 || (id != 2 && id != 5)) return DC_E_UNAVAILABLE;
        out->ok = 1;
        *mask = zero_mask ? 0 : 1u << (id == 2 ? 4 : 21);
        std::strcpy(out->message, id == 2 ? "memory" : "video"); return DC_OK;
    };
    api.clkdom_write_capability = [](dc_gpu*, uint32_t, int32_t, dc_outcome* out) -> dc_status {
        out->ok = setter; return DC_OK;
    };
    auto client = create_core_backend_for_test(api);
    std::string error;
    CHECK(client->open("0000:02:00.0", error));
    auto row = [&](uint32_t domain) -> AdvancedControl {
        const auto state = client->read(true);
        for (const auto& clock : state.advanced_controls)
            if (clock.id.kind == AdvancedKind::clock_domain && clock.id.domain == domain) return clock;
        CHECK(false); return {};
    };
    const int original = advanced_writes;
    effect_at_p0 = false;
    std::fill_n(pattern_reads, DC_CLKDOM_SLOTS, 0);
    const auto known = row(5);
    CHECK(pattern_reads[2] == 1 && pattern_reads[5] == 1);
    CHECK(known.clock_check == ClockCheck::known_pattern && known.state.writable && known.can_verify &&
        known.known_clocks == "video" && known.observed_clocks.empty() && !known.observed_private_domain);
    CHECK(row(1).clock_check == ClockCheck::candidate && !row(2).state.writable);
    CHECK(!client->execute(effect_command(5)).ok && row(5).clock_check == ClockCheck::known_pattern &&
        advanced_writes == original);
    Command change = effect_command(5); change.operation = Command::Operation::apply; change.value = 12;
    Command undo; undo.operation = Command::Operation::undo;
    match = false;
    CHECK(!client->execute(change).ok && advanced_writes == original);
    match = true; zero_mask = true;
    CHECK(!row(5).state.writable && !client->execute(change).ok && advanced_writes == original);
    zero_mask = false; setter = false;
    CHECK(row(5).clock_check == ClockCheck::unavailable && !client->execute(change).ok);
    setter = true; ++fixture_clock_mode;
    CHECK(!row(5).state.writable && !client->execute(change).ok);
    --fixture_clock_mode;
    CHECK(client->execute(change).ok && domain_raw[5] == 12000 && advanced_writes == original + 1);
    match = false;
    CHECK(client->execute(undo).ok && domain_raw[5] == 0);
    match = true;
    wchar_t file[MAX_PATH]{};
    CHECK(GetTempFileNameW(directory, L"clk", 0, file) != 0);
    Command load; load.operation = Command::Operation::load_profile; load.profile_path = file;
    write_profile(file, {{"format", "druta-native-daily"}, {"version", 2},
        {"target", {{"uuid", "GPU-A"}, {"slot", "0000:02:00.0"}}}, {"controls", ProfileJson::object()},
        {"advanced", ProfileJson::array({{{"kind", "clock_domain"}, {"domain", 5}, {"value", 12000}, {"unit", "kHz"}}})}});
    CHECK(client->execute(load).ok && domain_raw[5] == 12000);
    CHECK(client->execute(undo).ok && domain_raw[5] == 0);
    match = false;
    CHECK(!client->execute(load).ok && domain_raw[5] == 0);
    CHECK(DeleteFileW(file) != 0);
    match = true; effect_at_p0 = true;
    Command all; all.operation = Command::Operation::verify_all_clock_offsets;
    const auto batch = client->execute(all);
    CHECK(batch.ok && batch.steps.size() == 1 && row(1).clock_check == ClockCheck::verified &&
        row(5).clock_check == ClockCheck::known_pattern);
    effect_ambiguous = true;
    CHECK(!client->execute(effect_command(5)).ok && domain_raw[5] == 0);
    effect_ambiguous = false;
    const auto failed = row(5);
    CHECK(failed.clock_check == ClockCheck::candidate && failed.known_clocks.empty() && !failed.state.writable);
    const int before_retry = advanced_writes;
    CHECK(!client->execute(change).ok && advanced_writes == before_retry);
    setter = false;
    CHECK(row(5).clock_check == ClockCheck::unavailable);
    setter = true;
    CHECK(!row(5).state.writable);
    confirm_offset(*client, 5);
    const auto checked = row(5);
    CHECK(checked.clock_check == ClockCheck::verified && checked.state.writable &&
        checked.known_clocks.empty() && !checked.observed_clocks.empty() && domain_raw[5] == 0);
}
TEST_NOINLINE void clock_batch_tests(CoreApi api) {
    static std::vector<std::pair<int64_t, double>> dispatched;
    api.clkdom_set = [](dc_gpu* gpu, int64_t domain, double value, dc_outcome* outcome) -> dc_status {
        dispatched.emplace_back(domain, value); return dom_set(gpu, domain, value, outcome);
    };
    const auto open = [&]() {
        std::string error; auto client = create_core_backend_for_test(api);
        CHECK(client->open("0000:02:00.0", error));
        dispatched.clear(); return client;
    };
    Command all; all.operation = Command::Operation::verify_all_clock_offsets;
    auto client = open();
    const auto complete = client->execute(all);
    const std::vector<std::pair<int64_t, double>> expected{{1,-45},{1,0},{5,-45},{5,0}};
    CHECK(complete.ok && complete.steps.size() == 2 && dispatched == expected);
    CHECK(complete.message.find("Enable XOC") != std::string::npos);
    const auto steady = dispatched;
    CHECK(client->execute(all).steps.empty() && dispatched == steady);
    client->close();
    client = open();
    client->configure_clock_check_cancellation([] { return !dispatched.empty(); });
    const auto stopped = client->execute(all);
    CHECK(!stopped.ok && stopped.steps.size() == 1 && dispatched.size() == 2 && domain_raw[1] == 0);
    client->close();
    client = open();
    effect_block_restore = true;
    const auto pending = client->execute(all);
    CHECK(!pending.ok && pending.steps.size() == 1 && client->auxiliary_cleanup_needed());
    CHECK(std::all_of(dispatched.begin(), dispatched.end(), [](const auto& call){return call.first == 1;}));
    effect_block_restore = false;
    Command recover = effect_command(1); recover.operation = Command::Operation::restore_clock_test;
    CHECK(client->execute(recover).ok && domain_raw[1] == 0);
    client->close();
}

TEST_NOINLINE void clock_auto_p0_hold_tests(CoreApi api) {
    static struct {
        int acquired = 0, released = 0, headroom = 0, offsets = 0;
        bool reaches_p0 = false;
        uint32_t last_voltage_uv = 0;
    } calls;
    api.i2c.architecture = [](dc_gpu*, uint32_t* out) -> dc_status { *out = 4; return DC_OK; };
    api.curve = [](dc_gpu* gpu, dc_vf_curve* out) -> dc_status {
        const auto status = curve_read(gpu, out);
        if (status == DC_OK) {
            out->points[0].volt_mv = 937.5;
            out->points[1].volt_mv = 987.5;
        }
        return status;
    };
    api.rails.limits = [](dc_gpu*, dc_rails_limit_set* out) -> dc_status {
        out->present = 1;
        auto& rail = out->rails[0];
        rail.absolute_ok = rail.has_base = rail.has_effective = 1;
        rail.effective_mv = 1056.25;
        return DC_OK;
    };
    api.hold_ceiling = [](const dc_rl_row* row, dc_rl_value* out) -> dc_status {
        *out = row->effective_mv;
        return DC_OK;
    };
    api.tuning.lock = lock_gpu;
    api.tuning.unlock = unlock_gpu;
    api.tuning.identity = effect_identity;
    api.tuning.vf_status = tuning_vf_status;
    api.tuning.clock_status = tuning_clock_status;
    api.tuning.recovery_pending = tuning_recovery;
    api.tuning.recover = tuning_recover;
    api.tuning.headroom_record = tuning_record;
    api.tuning.headroom_apply = [](dc_gpu*, double, double, uint32_t, int32_t, int32_t,
            dc_rails_before_write_fn, void*, dc_rails_hold_outcome*, dc_rails_setter_writes*) -> dc_status {
        ++calls.headroom;
        return DC_E_FAILED;
    };
    api.tuning.vf_set = [](dc_gpu* gpu, int64_t uv, int32_t selected, int64_t domain, dc_outcome* out) -> dc_status {
        ++calls.acquired;
        calls.last_voltage_uv = static_cast<uint32_t>(uv);
        CHECK(selected == 1 && domain == 6 && !test_vf_held);
        effect_at_p0 = calls.reaches_p0;
        return tuning_vf_set(gpu, uv, selected, domain, out);
    };
    api.tuning.vf_clear = [](dc_gpu* gpu, int32_t selected, int64_t domain, int32_t exact, int64_t uv, dc_outcome* out) -> dc_status {
        ++calls.released;
        CHECK(selected == 1 && domain == 6 && exact == 1 && uv == test_vf_uv);
        CHECK(domain_raw[2] == 0);
        effect_at_p0 = false;
        return tuning_vf_clear(gpu, selected, domain, exact, uv, out);
    };
    api.clkdom_set = [](dc_gpu* gpu, int64_t domain, double value, dc_outcome* out) -> dc_status {
        ++calls.offsets;
        CHECK(effect_at_p0 && domain == 2);
        return dom_set(gpu, domain, value, out);
    };
    const auto open = [&](bool p0, bool reaches_p0, bool existing_hold = false) {
        calls = {};
        calls.reaches_p0 = reaches_p0;
        effect_at_p0 = p0;
        test_vf_held = existing_hold;
        test_vf_uv = existing_hold ? 975000 : 0;
        test_vf_domain = 6;
        std::string error;
        auto client = create_core_backend_for_test(api);
        CHECK(client->open("0000:02:00.0", error));
        Command xoc;
        xoc.operation = Command::Operation::set_xoc;
        xoc.enabled = true;
        CHECK(client->execute(xoc).ok);
        return client;
    };
    const auto row = [](IBackend& client) {
        const auto live = client.read(true);
        const auto found = std::find_if(live.advanced_controls.begin(), live.advanced_controls.end(),
            [](const auto& control) { return control.id.kind == AdvancedKind::clock_domain && control.id.domain == 2; });
        CHECK(found != live.advanced_controls.end());
        return found == live.advanced_controls.end() ? AdvancedControl{} : *found;
    };
    const int starting_memory_request = domain_raw[2];
    domain_raw[2] = 0;
    auto client = open(false, true);
    const auto candidate = row(*client);
    CHECK(candidate.can_verify && !candidate.state.writable);
    auto verify = effect_command(2);
    verify.expected_binding = candidate.binding;
    const auto automatic = client->execute(verify);
    CHECK(automatic.ok && row(*client).state.writable && row(*client).clock_check == ClockCheck::verified);
    CHECK(calls.acquired == 1 && calls.released == 1 && calls.last_voltage_uv == 937500);
    CHECK(calls.offsets == 2 && calls.headroom == 0 && domain_raw[2] == 0 && !test_vf_held);
    CHECK(!client->auxiliary_cleanup_needed() && client->prepare_close().ok);
    client->close();

    client = open(true, false);
    verify.expected_binding = row(*client).binding;
    CHECK(client->execute(verify).ok && calls.offsets == 2 && domain_raw[2] == 0);
    CHECK(calls.acquired == 0 && calls.released == 0 && calls.headroom == 0 && !test_vf_held);
    client->close();

    client = open(true, false, true);
    verify.expected_binding = row(*client).binding;
    CHECK(client->execute(verify).ok && calls.offsets == 2 && domain_raw[2] == 0);
    CHECK(calls.acquired == 0 && calls.released == 0 && calls.headroom == 0 && test_vf_held && test_vf_uv == 975000);
    CHECK(client->prepare_close().ok && test_vf_held);
    client->close();

    client = open(false, false);
    verify.expected_binding = row(*client).binding;
    const auto never_p0 = client->execute(verify);
    CHECK(!never_p0.ok && never_p0.message.find("P0") != std::string::npos && !row(*client).state.writable);
    CHECK(calls.acquired == 1 && calls.released == 1 && calls.headroom == 0 && calls.offsets == 0);
    CHECK(domain_raw[2] == 0 && !test_vf_held && !client->auxiliary_cleanup_needed());
    client->close();
    domain_raw[2] = starting_memory_request;
    effect_at_p0 = true;
    xoc_flag = 0;
}

// Fake hold-headroom core: the record is the raise; restore drops it unless told to fail.
bool hr_record=false, hr_restore_fails=false; int hr_restores=0, hr_arch_reads=0; int32_t hr_arch=1; dc_status hr_arch_status=DC_OK;
dc_status DC_CALL hr_record_get(dc_gpu*, dc_rails_hold_record* r) { r->present=hr_record?1:0; r->rail=0; return DC_OK; }
dc_status DC_CALL hr_apply(dc_gpu*, double hold_mv, double margin_mv, uint32_t, int32_t, int32_t, dc_rails_before_write_fn, void*, dc_rails_hold_outcome* out, dc_rails_setter_writes*) {
    CHECK(hold_mv==900 && margin_mv==25); hr_record=true; out->ok=DC_RAILS_HOLD_OK; return DC_OK;
}
dc_status DC_CALL hr_restore(dc_gpu*, int32_t force, dc_rails_hold_outcome* out, dc_rails_setter_writes*) {
    CHECK(force==0); ++hr_restores;
    if (hr_restore_fails) { out->ok=DC_RAILS_HOLD_FAILED; std::strcpy(out->message,"headroom restore not verified: fake readback"); return DC_OK; }
    hr_record=false; out->ok=DC_RAILS_HOLD_OK; std::strcpy(out->message,"NVVDD: reliability restored after the hold"); return DC_OK;
}
dc_status DC_CALL hr_live(dc_gpu*, int32_t, const dc_rails_hold_record*, int32_t, double, dc_outcome* out) { out->ok=1; return DC_OK; }
dc_status DC_CALL hr_overwritten(dc_gpu*, int32_t, const dc_rails_limit_set*, int32_t* out) { *out=0; return DC_OK; }
TEST_NOINLINE void hold_only_max_undo_tests(CoreApi actions, Command max_cmd, const Command& undo) {
    auto client=create_core_backend_for_test(actions); std::string error;
    CHECK(client->open("0000:02:00.0",error));
    const auto saved_board=board; const auto saved_boost=boost_value;
    board=500000; boost_value=100;
    Command fan{}; fan.operation=Command::Operation::apply; fan.control=ControlId::fan; fan.value=100;
    CHECK(client->execute(fan).ok && fan_level==100);
    const auto only=client->execute(max_cmd);
    CHECK(only.ok && only.steps.size()==1 && test_vf_held);
    CHECK(client->read(false).undo_label=="Undo Max it V/F hold");
    CHECK(client->execute(undo).ok && !test_vf_held && fan_level==100);
    CHECK(client->read(false).undo_label=="Undo fan");

    Command release{}; release.operation=Command::Operation::release_hold;
    CHECK(client->execute(max_cmd).ok && client->execute(release).ok);
    const auto already_released=client->execute(undo);
    CHECK(already_released.ok && !already_released.may_have_changed && fan_level==100);
    CHECK(client->execute(max_cmd).ok && test_vf_held);
    Command manual=max_cmd; manual.operation=Command::Operation::hold_curve_point;
    manual.hold_headroom=false; manual.hold_point_index=max_cmd.expected_curve.back().idx;
    CHECK(client->execute(manual).ok && test_vf_held);
    const auto replaced=client->execute(undo);
    CHECK(replaced.ok && !replaced.may_have_changed && test_vf_held && fan_level==100);
    CHECK(client->execute(release).ok && client->execute(undo).ok && fan_level==40);
    CHECK(!client->read(false).undo_available);
    board=saved_board; boost_value=saved_boost;
}
TEST_NOINLINE void adaptive_max_tests(CoreApi actions, Command max_cmd, const Command& undo) {
    // An independent board-bound query can qualify Max it when the NVML
    // capabilities structure omits PL_MAX, including with a wider XOC range.
    // The two fans have different maxima and currently run automatically.
    {
        auto partial = actions;
        partial.capabilities = [](dc_gpu* gpu, dc_capabilities* caps) -> dc_status {
            capabilities(gpu,caps); caps->present &= ~DC_C_PL_MAX; caps->pl_max_mw=0; return DC_OK;
        };
        partial.fan_state = [](dc_gpu* gpu, dc_fan_state* out) -> dc_status {
            auto status=fan_get(gpu,out); out->fans[1].max=83; return status;
        };
        std::string error;
        auto limited=create_core_backend_for_test(partial); CHECK(limited->open("0000:02:00.0",error));
        const auto saved_board=board; const bool saved_auto=fan_auto;
        fan_auto=true; fan_level=83;
        const auto discovered=limited->read(true);
        CHECK(discovered.max_it_available && discovered.max_it.fan_percent==83 && discovered.max_it.board_mw==500000);
        CHECK(discovered.max_it.fan_range_note.find("unconfirmed")!=std::string::npos);
        CHECK(discovered.controls[static_cast<size_t>(ControlId::power)].high==1999);
        auto stale=max_cmd; stale.expected_max_it=discovered.max_it; stale.expected_max_it->fan_percent=100;
        CHECK(!limited->execute(stale).may_have_changed && fan_auto && board==saved_board);
        CHECK(limited->execute(max_cmd).ok && fan_level==83 && !fan_auto && board==500000);
        CHECK(limited->execute(undo).ok && fan_auto && fan_level==83 && board==saved_board);
        test_foreign_clock=true;
        CHECK(!limited->read(true).max_it_available && !limited->execute(max_cmd).may_have_changed);
        test_foreign_clock=false; fan_auto=saved_auto; fan_level=40;
        CHECK(limited->read(true).max_it_available);
        partial.fan_caps=[](dc_gpu* gpu,dc_fan_capabilities* caps)->dc_status {
            fan_caps(gpu,caps); caps->automatic=0; return DC_OK;
        };
        auto manual_only=create_core_backend_for_test(partial); CHECK(manual_only->open("0000:02:00.0",error));
        fan_auto=true;
        auto blocked=manual_only->read(true);
        CHECK(!blocked.max_it_available && blocked.max_it.reason.find("restored")!=std::string::npos);
        const int writes=power_writes;
        CHECK(!manual_only->execute(max_cmd).may_have_changed && power_writes==writes && fan_auto && board==saved_board);
        fan_auto=saved_auto;
        CHECK(manual_only->read(true).max_it_available);
        partial.capabilities=[](dc_gpu* gpu,dc_capabilities* caps)->dc_status {
            capabilities(gpu,caps); caps->present |= DC_C_FAN_MIN | DC_C_FAN_MAX;
            caps->fan_min=0; caps->fan_max=83; return DC_OK;
        };
        auto reported=create_core_backend_for_test(partial); CHECK(reported->open("0000:02:00.0",error));
        CHECK(reported->read(true).max_it.fan_range_note=="The adapter reports the manual duty range.");
        partial.fan_state=[](dc_gpu* gpu,dc_fan_state* out)->dc_status {
            fan_get(gpu,out); std::strcpy(out->source,"nvapi_client"); out->fans[1].max=79; return DC_OK;
        };
        auto per_fan=create_core_backend_for_test(partial); CHECK(per_fan->open("0000:02:00.0",error));
        const auto native=per_fan->read(true).max_it;
        CHECK(native.ready() && native.fan_percent==79 && native.fan_range_note=="The cooler reports per-fan duty limits.");
    }
    actions.rails.limits=max_test_limits; actions.hold_ceiling=max_test_ceiling;
    actions.planner.ramp=max_test_ramp;
    actions.tuning.headroom_apply=max_test_headroom; actions.tuning.headroom_restore=hr_restore;
    actions.tuning.headroom_live=hr_live; actions.tuning.headroom_overwritten=hr_overwritten;
    auto client=create_core_backend_for_test(actions); std::string error;
    CHECK(client->open("0000:02:00.0",error));
    max_cmd.hold_headroom=true;
    const auto saved_board=board;
    for (const double ceiling:{925.0, 975.0, 0.0, 895.0}) {
        max_test_rail_mv=ceiling; max_test_headroom_mv=0; max_test_ramp_caps.clear();
        const auto result=client->execute(max_cmd);
        if (ceiling==895) {
            CHECK(!result.ok && !test_vf_held && max_test_headroom_mv==0);
            CHECK(result.message.find("No V/F point")!=std::string::npos);
        } else if (ceiling==925) {
            CHECK(result.ok && test_vf_uv==900000 && max_test_headroom_mv==900);
            CHECK((max_test_ramp_caps==std::vector<double>{950,900}));
            CHECK(result.message.find("NVVDD ceiling 925 mV, curve and hold target 900 mV")!=std::string::npos);
        } else {
            // An already-reachable point and an absent optional rail keep the original plan.
            CHECK(result.ok && test_vf_uv==950000 && max_test_headroom_mv==950);
            CHECK((max_test_ramp_caps==std::vector<double>{950}));
        }
        CHECK(client->execute(undo).ok && !test_vf_held && board==saved_board && boost_value==10 && fan_level==40);
    }
    max_test_rail_mv=950; max_test_skip_rail=true;
    const auto skipped=client->execute(max_cmd);
    CHECK(skipped.ok && skipped.may_have_changed && test_vf_held && client->read(false).undo_available);
    const auto evidence = client->tuning_state()->last_headroom_apply;
    CHECK(evidence && evidence->outcome == HeadroomApplyOutcome::no_set_issued);
    CHECK(!skipped.steps.empty() && skipped.steps.back().ok && skipped.steps.back().message==
          "V/F hold confirmed. Optional headroom skipped: Fixture rail writer recognized no packet");
    CHECK(client->execute(undo).ok && !test_vf_held && board==saved_board && boost_value==10 && fan_level==40);
    CHECK(client->read(true).tuning.last_headroom_apply == evidence);
    client->close();
    CHECK(client->open("0000:02:00.0",error) && !client->read(true).tuning.last_headroom_apply);
    hold_only_max_undo_tests(actions,max_cmd,undo);
    max_test_skip_rail=false;
}
dc_status DC_CALL hr_architecture(dc_gpu*, int32_t* supported) { ++hr_arch_reads; *supported=hr_arch; return hr_arch_status; }
// Python restore_raised_limits_now and hold_headroom_architecture over the fake core.
TEST_NOINLINE void hold_restore_tests() {
    std::string error;
    CoreApi a=api(); a.lock=lock_gpu; a.unlock=unlock_gpu;
    a.tuning.lock=lock_gpu; a.tuning.unlock=unlock_gpu; a.tuning.identity=identity;
    a.tuning.vf_status=tuning_vf_status; a.tuning.clock_status=tuning_clock_status; a.tuning.vf_set=tuning_vf_set; a.tuning.vf_clear=tuning_vf_clear;
    a.tuning.recovery_pending=tuning_recovery; a.tuning.recover=tuning_recover;
    a.tuning.headroom_record=hr_record_get; a.tuning.headroom_apply=hr_apply; a.tuning.headroom_restore=hr_restore;
    a.tuning.headroom_live=hr_live; a.tuning.headroom_overwritten=hr_overwritten; a.hold_architecture=hr_architecture;
    test_vf_held=false; test_foreign_clock=false; hr_record=false;
    // hr-02: the core's generation answer, not a driver or device identity.
    // Focused does not show it and makes no such read.
    auto client=create_core_backend_for_test(a); CHECK(client->open("0000:02:00.0", error));
    const int focused_reads=hr_arch_reads; auto data=client->read(false); data=client->read(true);
    CHECK(hr_arch_reads==focused_reads && !data.hold_headroom_architecture.has_value());
    client->set_live_readouts(true);
    data=client->read(true); CHECK(data.hold_headroom_architecture==true && hr_arch_reads>focused_reads);
    hr_arch=-1; CHECK(client->read(true).hold_headroom_architecture==true); // an unread answer keeps the definite one
    hr_arch_status=DC_E_UNAVAILABLE; hr_arch=1; CHECK(client->read(true).hold_headroom_architecture==true); hr_arch_status=DC_OK;
    {
        hr_arch=-1; auto unread=create_core_backend_for_test(a); CHECK(unread->open("0000:02:00.0", error)); unread->set_live_readouts(true);
        CHECK(!unread->read(false).hold_headroom_architecture.has_value());
        hr_arch=0; CHECK(unread->read(true).hold_headroom_architecture==false);
        hr_arch=1; hr_arch_status=DC_E_UNAVAILABLE; auto failed=create_core_backend_for_test(a); CHECK(failed->open("0000:02:00.0", error)); failed->set_live_readouts(true);
        CHECK(!failed->read(false).hold_headroom_architecture.has_value()); hr_arch_status=DC_OK;
        CoreApi bare=a; bare.hold_architecture=nullptr; auto without=create_core_backend_for_test(bare); CHECK(without->open("0000:02:00.0", error)); without->set_live_readouts(true);
        CHECK(without->read(false).hold_headroom_architecture==false);
    }
    hr_arch=1;
    // hr-01: nothing raised issues nothing.
    Command restore{}; restore.operation=Command::Operation::restore_raised_limits;
    auto restored=client->execute(restore);
    CHECK(restored.ok && !restored.may_have_changed && hr_restores==0 && restored.message=="headroom: no raised limits to restore");
    data=client->read(true);
    Command hold{}; hold.operation=Command::Operation::hold_curve_point; hold.expected_curve=data.curve; hold.expected_curve_freq_div=data.curve_freq_div;
    hold.hold_point_index=3; hold.hold_headroom=true; hold.headroom_margin_mv=25;
    CHECK(client->execute(hold).ok && test_vf_held && hr_record && client->tuning_state()->headroom && client->read(false).tuning.headroom);
    // A changed target is refused before any restore.
    changed_id=true; restored=client->execute(restore); changed_id=false;
    CHECK(!restored.ok && hr_restores==0 && hr_record && client->tuning_state()->headroom);
    // A raise in force: exactly one restore, read back, the hold kept.
    restored=client->execute(restore);
    CHECK(restored.ok && restored.may_have_changed && hr_restores==1 && !hr_record && test_vf_held);
    // Python's "headroom: <message>" line alone; the hold-still-in-force note is the Classic log's own line.
    CHECK(restored.message=="headroom: NVVDD: reliability restored after the hold");
    auto after=client->tuning_state(); CHECK(after && !after->headroom && after->owned && after->confirmed && !client->read(false).tuning.headroom);
    restored=client->execute(restore); CHECK(restored.ok && hr_restores==1);
    Command release{}; release.operation=Command::Operation::release_hold;
    CHECK(client->execute(release).ok && !test_vf_held && hr_restores==1);
    // A failed restore keeps the raise, the flag and the hold, after one bounded retry.
    data=client->read(true); hold.expected_curve=data.curve; hold.expected_curve_freq_div=data.curve_freq_div;
    CHECK(client->execute(hold).ok && hr_record);
    hr_restore_fails=true; restored=client->execute(restore);
    CHECK(!restored.ok && restored.may_have_changed && hr_restores==3 && hr_record && test_vf_held && client->tuning_state()->headroom);
    CHECK(restored.message=="headroom: headroom restore not verified: fake readback" && client->read(false).tuning.headroom);
    hr_restore_fails=false; CHECK(client->execute(release).ok && !test_vf_held && !hr_record && hr_restores==4);
    // A later hold raises again and the same restore takes it off once more.
    data=client->read(true); hold.expected_curve=data.curve; hold.expected_curve_freq_div=data.curve_freq_div;
    CHECK(client->execute(hold).ok && hr_record);
    restored=client->execute(restore);
    CHECK(restored.ok && restored.may_have_changed && hr_restores==5 && !hr_record && test_vf_held && !client->tuning_state()->headroom);
    CHECK(client->execute(release).ok && !test_vf_held && hr_restores==5);
}
// Fake power-feedback transport: one qualified policy (INFO / CONTROL in the
// packet geometry the service understands), the read-only STATUS GET counted.
namespace integral_fake {
dc_gpu_identity id{}; dc_target target{}; dc_power_layout layout{};
std::vector<uint32_t> info, control; int status_reads = 0, sets = 0; bool status_fails = false;
void put8(std::vector<uint32_t>& w, size_t at, uint32_t v) { const auto s = static_cast<uint32_t>((at % 4) * 8); w[at / 4] = (w[at / 4] & ~(255u << s)) | (v << s); }
void put16(std::vector<uint32_t>& w, size_t at, uint16_t v) { put8(w, at, v & 255u); put8(w, at + 1, static_cast<uint32_t>(v >> 8)); }
uint16_t get16(const std::vector<uint32_t>& w, size_t at) { return static_cast<uint16_t>((w[at / 4] >> ((at % 4) * 8)) & 65535u); }
void reset() {
    id = {}; id.size = sizeof(id); id.nvapi_ok = id.nvml_ok = id.nvapi_selected = id.nvml_selected = 1;
    std::snprintf(id.slot, sizeof(id.slot), "0000:02:00.0"); std::snprintf(id.uuid, sizeof(id.uuid), "GPU-feedback");
    id.nvapi_handle = 0x4455; id.nvapi_devid = 0x2203; id.nvapi_subsys = 0x1234;
    target = {}; target.size = sizeof(target); std::snprintf(target.slot, sizeof(target.slot), "%s", id.slot);
    target.architecture = 7; target.handle = id.nvapi_handle; target.device_id = id.nvapi_devid; target.subsystem_id = id.nvapi_subsys;
    layout = {}; layout.size = sizeof(layout); layout.sizes[0] = 20512; layout.sizes[1] = 397048; layout.sizes[2] = layout.sizes[3] = 13876;
    layout.info_base = 0xCC; layout.info_stride = 0x104; layout.status_base = 0x9C; layout.status_stride = 0x1730;
    layout.control_base = 0x14; layout.control_stride = 0xC4;
    info.assign(20512 / 4, 0); control.assign(13876 / 4, 0); info[1] = 1; control[3] = 255; control[4] = 1;
    const size_t a = 0xCC, b = 0x14;
    put8(info, a, 0x30); put8(info, a + 4, 5); put8(info, a + 5, 10); info[(a + 8) / 4] = 10; info[(a + 16) / 4] = 600000;
    put8(info, a + 0x20, 2); put8(info, a + 0x22, 4); put8(info, a + 0x23, 5); put16(info, a + 0x24, 1024); put16(info, a + 0x26, 4096);
    control[b / 4] = 5; control[(b + 4) / 4] = 320000; put8(control, b + 8, 2); put8(control, b + 9, 4); put8(control, b + 10, 5);
    put16(control, b + 12, 1024); put16(control, b + 14, 4096);
    status_reads = sets = 0; status_fails = false;
}
dc_status DC_CALL get_identity(const dc_gpu*, dc_gpu_identity* out) { *out = id; return DC_OK; }
dc_status DC_CALL bind(dc_gpu*, int32_t, dc_target* out, dc_error*) { *out = target; return DC_OK; }
int32_t DC_CALL probe(uint32_t) { return 1; }
dc_status DC_CALL abi(dc_gpu*, dc_power_layout* out, uint32_t* index, dc_error*) { *out = layout; *index = 0; return DC_OK; }
dc_status DC_CALL transport_reset(dc_gpu*, int32_t) { return DC_OK; }
dc_status DC_CALL mask(dc_gpu*, uint32_t) { return DC_OK; }
dc_status DC_CALL rm(dc_gpu*, uint32_t command, const uint32_t* params, uint32_t param_count, int32_t, uint32_t, int32_t, uint32_t* out, uint32_t capacity, uint32_t* count, dc_error* error) {
    if (command == 0x2080E61B) { // SET: the selected policy row
        ++sets; CHECK(params && param_count == control.size());
        if (params && param_count == control.size()) std::copy(params + 0x14 / 4, params + (0x14 + 0xC4) / 4, control.begin() + 0x14 / 4);
        *count = 0; return DC_OK;
    }
    if (command == 0x2080A619) { // STATUS: read-only diagnostics
        ++status_reads; if (status_fails) { error->kind = DC_ERR_TRANSIENT; return DC_E_FAILED; }
        const uint32_t words = 397048 / 4; if (capacity < words) return DC_E_BUFFER;
        std::fill(out, out + words, 0u); *count = words; return DC_OK;
    }
    const auto& words = command == 0x2080A618 ? info : control;
    if (capacity < words.size()) return DC_E_BUFFER;
    std::copy(words.begin(), words.end(), out); *count = static_cast<uint32_t>(words.size()); return DC_OK;
}
dc_status DC_CALL decode(const dc_power_layout*, const dc_arbitration_words*, const dc_arbitration_words*, const dc_arbitration_words*, dc_arbitration_report* out) {
    const uint32_t size = out->size; *out = {}; out->size = size; out->schema = 1; out->available = 1; out->policy_count = 1;
    out->policies[0].policy = 0; out->policies[0].available = 1; out->policies[0].effective = 350000; out->policies[0].pmu.available = 1; out->policies[0].pmu.output = 298250;
    return DC_OK;
}
CoreApi core() {
    CoreApi a{}; a.abi_version = version; a.enumerate = enumerate; a.open = open_gpu; a.close = close_gpu; a.identity = get_identity; a.lock = lock_gpu; a.unlock = unlock_gpu;
    auto& p = a.power_integral; p.lock = lock_gpu; p.unlock = unlock_gpu; p.identity = get_identity; p.target_bind = bind; p.probe_architecture = probe;
    p.abi = abi; p.rm = rm; p.transport_reset = transport_reset; p.policy_mask_set = mask; p.decode = decode;
    return a;
}
}
// Python PowerIntegralController._report: only Classic's explicit Read adds the one
// read-only STATUS GET (PMU output, effective limit); a failed STATUS read leaves
// both unknown. Focused's Read, the history capture and Apply send what they did.
TEST_NOINLINE void power_integral_status_tests() {
    std::string error; integral_fake::reset(); int captures = 0;
    CoreApi a = integral_fake::core(); a.history = std::make_shared<ProfileHistorySink>();
    a.history->save = [&](const std::string&, const ProfileJson&) { ++captures; };
    auto client = create_core_backend_for_test(a); CHECK(client->open("0000:02:00.0", error));
    Command read{}; read.operation = Command::Operation::power_integral_read;
    // Focused: no STATUS read, so both stay unknown.
    CHECK(client->execute(read).ok && integral_fake::status_reads == 0);
    auto shown = client->read(false).power_integral;
    CHECK(shown && shown->current && shown->rows.size() == 1 && !shown->rows[0].pmu_output && !shown->rows[0].effective);
    // Classic: exactly one STATUS read, and the Read publishes its values.
    client->set_live_readouts(true);
    CHECK(client->execute(read).ok && integral_fake::status_reads == 1);
    shown = client->read(false).power_integral;
    CHECK(shown && shown->rows.size() == 1 && shown->rows[0].pmu_output == 298250u && shown->rows[0].effective == 350000u);
    // A failed STATUS read is not fatal: the Read completes, both unknown again.
    integral_fake::status_fails = true;
    CHECK(client->execute(read).ok && integral_fake::status_reads == 2);
    shown = client->read(false).power_integral;
    CHECK(shown && shown->current && shown->rows.size() == 1 && !shown->rows[0].pmu_output && !shown->rows[0].effective);
    integral_fake::status_fails = false;
    // Apply in Classic, its history capture included: no STATUS read.
    Command apply{}; apply.operation = Command::Operation::power_integral_apply; apply.integral_policy = 0; apply.integral_upper = true;
    apply.integral_raw = 5000; apply.expected_request_raw = 4096;
    const auto applied = client->execute(apply);
    CHECK(applied.ok && integral_fake::sets == 1 && captures == 1 && integral_fake::get16(integral_fake::control, 0x14 + 14) == 5000);
    CHECK(integral_fake::status_reads == 2);
    client->close();
}
#undef TEST_NOINLINE
}
int main() {
    std::string error;
    auto missing = api(); missing.identity = nullptr;
    CHECK(!create_core_backend_for_test(missing)->open("0000:02:00.0", error));
    no_device = true; auto absent = create_core_backend_for_test(api()); CHECK(!absent->open("0000:02:00.0", error)); no_device = false;
    perf_frequency_integration_tests();
    {
        auto partial = api();
        partial.enumerate = [](dc_gpu_entry* entries, size_t capacity, size_t* count)->dc_status {
            if (!entries) { *count=3; return DC_E_BUFFER; }
            CHECK(capacity==3);
            *count=2; // one observation has lost its slot, another disappeared between reads
            std::memcpy(entries[1].slot, "0000:02:00.0", sizeof("0000:02:00.0"));
            return DC_OK;
        };
        auto ui = create_core_backend_for_test(partial);
        const auto devices = ui->enumerate();
        CHECK(devices.size()==1 && devices.front().slot=="0000:02:00.0");
    }
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
    table_fail = true;
    const auto refused_power = pb->execute(power);
    CHECK(!refused_power.ok && power_writes == 0 && refused_power.message.find("RM 0x3A") != std::string::npos);
    table_fail = false;
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
    // Classic per-domain readbacks pair against the rows the Monitor already
    // classified (no second read); memory divisor/type come from capabilities.
    CHECK(clock_state.clock_domain_pairs.empty() && !clock_state.memory_divisor && clock_state.memory_type == "unknown");
    clock_domain_pairing_tests(clock_api);
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
    clock_discovery_poll_tests(advanced_api);
    auto effect_api = advanced_api; with_effect_fixture(effect_api);
    clock_write_capability_tests(effect_api);
    known_clock_pattern_tests(effect_api, directory);
    clock_batch_tests(effect_api);
    clock_auto_p0_hold_tests(effect_api);
    xoc_flag = 1; auto ab = create_core_backend_for_test(effect_api); CHECK(ab->open("0000:02:00.0", error) && xoc_flag == 0); auto adv = ab->read(true); CHECK(adv.advanced_controls.size() == 4 && adv.xoc_known && !adv.xoc_enabled);
    auto memory_label = std::find_if(adv.advanced_controls.begin(), adv.advanced_controls.end(), [](const AdvancedControl& a) { return a.id.kind == AdvancedKind::clock_domain && a.id.domain == 2; }); CHECK(memory_label != adv.advanced_controls.end() && memory_label->label == "Additional memory offset" && memory_label->state.low == -500 && memory_label->state.high == 1000);
    auto find_domain = [](const LiveData& data, uint32_t id) -> ControlState { auto it = std::find_if(data.advanced_controls.begin(), data.advanced_controls.end(), [id](const AdvancedControl& a) { return a.id.kind == AdvancedKind::clock_domain && a.id.domain == id; }); CHECK(it != data.advanced_controls.end()); return it == data.advanced_controls.end() ? ControlState{} : it->state; };
    auto normal_domain = find_domain(adv, 5); CHECK(normal_domain.low == -300 && normal_domain.high == 300 && !normal_domain.writable);
    confirm_offset(*ab, 5);
    confirm_offset(*ab, 1);
    domain_raw[5] = 450000; CHECK(find_domain(ab->read(true), 5).high == 450); domain_raw[5] = 0;
    Command range_gate{}; range_gate.operation = Command::Operation::set_xoc; range_gate.enabled = true; CHECK(ab->execute(range_gate).ok);
    confirm_offset(*ab, 2);
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
    linked_rail_tests(rail_api,undo);
    linked_rail_history_tests(rail_api,undo);
    rail_readout_tests(rail_api);
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
    watt_policy_readback_tests(pa);
    nominal_board_only_tests(pa);
    native_power_limit_tests(pa);
    policy_refresh_tests(pa);
    hold_restore_tests();
    power_integral_status_tests();
    std::printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
