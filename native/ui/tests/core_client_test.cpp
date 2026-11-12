/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "core_client.h"
#include "profile_io.h"
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
int curve_div = 1, voltage_shift = 0, base_shift = 0;
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
dc_status DC_CALL clock_step(dc_gpu*, uint32_t* v, int32_t* m) { *v = 1000; *m = 1; return DC_OK; }
dc_status DC_CALL snapshot(dc_gpu*, dc_vf_delta_snapshot* o) { o->freq_div = curve_div; o->count = 2; o->deltas[0] = {3, delta}; o->deltas[1] = {4, delta2}; return DC_OK; }
dc_status DC_CALL deltas(dc_gpu*, const dc_vf_delta* d, size_t n, dc_outcome* o) { ++curve_writes; if (!curve_failure) { for (size_t i = 0; i < n; ++i) { if (d[i].idx == 3) delta = static_cast<int>(d[i].delta_khz); else if (d[i].idx == 4) delta2 = static_cast<int>(d[i].delta_khz); } if (curve_corrupt) ++delta2; if (curve_geometry_corrupt) ++base_shift; } o->ok = !curve_failure; return DC_OK; }
dc_status DC_CALL curve_layout(dc_gpu*, int32_t, dc_vf_layout* l) { l->gpu_count = 2; l->gpu_idx[0] = 3; l->gpu_idx[1] = 4; l->freq_div = curve_div; return DC_OK; }
dc_status DC_CALL curve_read(dc_gpu*, dc_vf_curve* c) { c->count = 2; c->points[0] = {3, 900.0 + voltage_shift, 1500.0 + base_shift + delta / (1000.0 * curve_div), delta}; c->points[1] = {4, 950, 1600.0 + delta2 / (1000.0 * curve_div), delta2}; return DC_OK; }
dc_status DC_CALL envelopes(dc_request_envelopes* e) { e->max_abs_delta_khz = 1000000; e->rail_offset_max_mv = 100; e->rail_offset_xoc_max_mv = 500; e->power_limit_xoc_max_mw = 1999000; return DC_OK; }
dc_status DC_CALL curve_reset(dc_gpu*, dc_outcome* o) { ++curve_writes; delta = delta2 = 0; o->ok = 1; return DC_OK; }
dc_status DC_CALL fan_get(dc_gpu*, dc_fan_state* s) { if (fan_mode == 2 || fan_read_fail > 0) { if (fan_read_fail > 0) --fan_read_fail; return DC_E_UNAVAILABLE; } std::memcpy(s->source, "nvml", 5); s->count = 2; for (int i = 0; i < 2; ++i) { s->fans[i].id = i; s->fans[i].level = fan_level + (fan_mode == 1 ? i : 0); s->fans[i].policy = 1; s->fans[i].manual = !fan_auto; } return DC_OK; }
dc_status DC_CALL fan_caps(dc_gpu*, dc_fan_capabilities* c) { c->manual = 1; c->automatic = 1; c->max = 100; return DC_OK; }
dc_status DC_CALL fan_set(dc_gpu*, int32_t x, dc_outcome* o) { if (!fan_refuse) { fan_level = x; fan_mode = 0; fan_auto = false; } if (fan_post_fail) fan_read_fail = 2; o->ok = 1; return DC_OK; }
dc_status DC_CALL fan_reset(dc_gpu*, dc_outcome* o) { fan_level = 20; fan_auto = true; if (fan_mode == 2) fan_mode = 0; o->ok = 1; return DC_OK; }
dc_status DC_CALL fan_restore(dc_gpu*, const dc_fan_snapshot* s, dc_outcome* o) { CHECK(s->count == 2 && s->fans[1].level == s->fans[0].level + 1); fan_level = s->fans[0].level; fan_mode = s->fans[1].level != s->fans[0].level ? 1 : 0; fan_auto = !s->fans[0].manual; o->ok = 1; return DC_OK; }
dc_status DC_CALL boost_get(dc_gpu*, int32_t* v) { *v = boost_value; return DC_OK; }
dc_status DC_CALL boost_set(dc_gpu*, int64_t v, dc_outcome* o) { ++boost_writes; boost_value = static_cast<int>(v); o->ok = 1; return DC_OK; }
uint32_t board = 200000; int64_t peer = 100; bool table_fail = false, board_mismatch = false, peer_fail = false, geometry_change = false, xoc_live = false; bool pin_present = true; int power_writes = 0, policy_writes = 0, policy_reads = 0; bool policy_external_test = false, policy_change_board = false, policy_drop_pin = false;
dc_status DC_CALL lock_gpu(dc_gpu*) { return DC_OK; }
dc_status DC_CALL unlock_gpu(dc_gpu*) { return DC_OK; }
dc_status DC_CALL capabilities(dc_gpu*, dc_capabilities* c) { c->present = DC_C_PL_MIN | DC_C_PL_MAX | DC_C_PL_DEF; c->pl_min_mw = 100000; c->pl_max_mw = 500000; c->pl_def_mw = 200000; return DC_OK; }
dc_status DC_CALL power_read(dc_gpu*, dc_cl_statics*, int32_t* has, uint32_t* value, dc_error*) { *has = 1; *value = board; return DC_OK; }
dc_status DC_CALL bounds(dc_gpu*, dc_cl_statics*, int32_t xoc, int64_t* low, int64_t* high, dc_error*) { *low = 100000; *high = xoc && xoc_live ? 1999000 : 500000; if (board > *high) *high = board; return DC_OK; }
dc_status DC_CALL power_set(dc_gpu*, dc_cl_statics*, int64_t value, int32_t, int32_t* ok, char*, size_t, size_t* needed) { ++power_writes; board = static_cast<uint32_t>(value) + (board_mismatch ? 1000 : 0); peer = 200; *ok = 1; *needed = 1; return DC_OK; }
dc_status DC_CALL message(dc_gpu*, char* out, size_t, size_t* n) { out[0] = 0; *n = 1; return DC_OK; }
dc_status DC_CALL pins_get(dc_gpu*, uint32_t* ids, int64_t* vals, uint32_t, uint32_t* n) { *n = pin_present ? 1 : 0; if (*n) { ids[0] = 1; vals[0] = 100; } return DC_OK; }
dc_status DC_CALL pins_set(dc_gpu*, const uint32_t* ids, const int64_t* values, uint32_t n) { if (n) { CHECK(n == 1 && ids[0] == 1 && values[0] == 100); } pin_present = n != 0; return DC_OK; }
dc_status DC_CALL policies(dc_gpu*, dc_cl_confirm_fn confirm, void*, dc_cl_policy_inputs_fn input, void* context, dc_policy_row* rows, uint32_t, uint32_t* n, dc_error*) {
    ++policy_reads; CHECK(confirm == nullptr); if (table_fail) return DC_E_FAILED;
    dc_cl_statics st{}; st.size = sizeof(st); int32_t has = 0; int64_t mw = 0; CHECK(input(context, &has, &mw, &st) == 0 && has && mw == board);
    *n = 2; rows[0].policy = 0; rows[0].board = 1; rows[0].unit = 0; rows[0].minimum = 100000; rows[0].maximum = 350000; rows[0].requested = board;
    rows[1].policy = 1; rows[1].unit = 1; rows[1].minimum = 0; rows[1].maximum = geometry_change ? 501 : 500; rows[1].requested = peer; rows[1].writable = 1; rows[1].consistent = 1; return DC_OK;
}
dc_status DC_CALL policy_set(dc_gpu*, dc_cl_confirm_fn confirm, void*, dc_cl_policy_inputs_fn, void*, int64_t id, int32_t whole, int64_t value, int32_t pin, int32_t, int32_t* ok, char*, size_t, size_t* needed) {
    CHECK(!confirm && id == 1 && whole && !pin && (policy_external_test || !pin_present)); ++policy_writes; *ok = !peer_fail; if (!peer_fail) { peer = value; if (policy_change_board) board += 1000; if (policy_drop_pin) pin_present = false; } *needed = 1; return DC_OK;
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
int rail_type = 7, rail_limits_calls = 0, rail_type_flip_at = 0; double rail_base = 900;
int raw_limits[2][4] = {{100, 200, 300, 400}, {500, 600, 700, 800}}; bool rail_fields_fail = false, rail_throw = false; int rail_set_calls = 0, rail_failed_index = -1, rail_held = -1;
dc_status DC_CALL rail_fields(dc_gpu*, int32_t rail, uint32_t* count) { if (rail_fields_fail || rail == rail_failed_index) return DC_E_UNAVAILABLE; *count = 4; return DC_OK; }
dc_status DC_CALL rail_limits(dc_gpu*, dc_rails_limit_set* out) { ++rail_limits_calls; if (rail_type_flip_at && rail_limits_calls == rail_type_flip_at) ++rail_type; for (int i = 0; i < 2; ++i) { out->present |= 1u << i; out->rails[i].type = i + rail_type; out->rails[i].has_base = 1; for (int j = 0; j < 4; ++j) { out->rails[i].fields[j] = raw_limits[i][j] / 1000.0; out->rails[i].base[j] = rail_base; } } return DC_OK; }
dc_status DC_CALL rail_initial(dc_gpu*, int32_t rail, int32_t* has, int32_t values[4]) { *has = 1; for (int i = 0; i < 4; ++i) values[i] = (rail * 4 + i + 1) * 100; return DC_OK; }
dc_status DC_CALL rail_hold(dc_gpu*, dc_rails_hold_record* out) { out->present = rail_held >= 0; out->rail = rail_held; return DC_OK; }
dc_status DC_CALL rail_raw_set(dc_gpu*, int32_t rail, const dc_rails_raw_request* request, int32_t enabled, int32_t, dc_outcome* out, dc_rails_setter_writes* write_info) { CHECK(enabled && request->count == 1); ++rail_set_calls; auto field = request->order[0]; raw_limits[rail][field] = request->uv[field]; write_info->calls = 1; if (rail_throw) throw std::runtime_error("post-write failure"); out->ok = 1; return DC_OK; }
dc_status DC_CALL rail_initial_set(dc_gpu*, int32_t, int32_t rail, const dc_rails_reset_request* request, dc_outcome* out, dc_rails_setter_writes* write_info) { for (int i = 0; i < 4; ++i) if (request->field_mask & (1u << i)) raw_limits[rail][i] = (rail * 4 + i + 1) * 100; write_info->calls = 1; out->ok = 1; return DC_OK; }
uint32_t current_index = 7; bool current_read_fails = false, current_generic_label = false;
dc_status DC_CALL alternate_policies(dc_gpu*, dc_cl_confirm_fn confirm, void*, dc_cl_policy_inputs_fn, void*, dc_policy_row* rows, uint32_t, uint32_t* n, dc_error*) {
    CHECK(!confirm); *n = 2; rows[0].policy = 0; rows[0].board = 1; rows[0].unit = 0; rows[0].minimum = 100000; rows[0].maximum = 500000; rows[0].requested = board;
    rows[1].policy = current_index; rows[1].channel = 17; rows[1].unit = 1; rows[1].minimum = 1000; rows[1].maximum = 500000; rows[1].default_value = 150000; rows[1].requested = 120000; rows[1].named_current = 1; rows[1].consistent = 1; return DC_OK;
}
dc_status DC_CALL core_current(dc_gpu*, dc_cl_confirm_fn confirm, void*, dc_current_row* rows, uint32_t, uint32_t* n) {
    CHECK(!confirm); if (current_read_fails) return DC_E_FAILED; *n = 1; rows[0].policy = current_index; rows[0].channel = 17; rows[0].minimum_ma = 1000; rows[0].maximum_ma = 500000; rows[0].requested_ma = 120000;
    if (current_generic_label) std::memcpy(rows[0].label, "Other current", sizeof("Other current")); else std::memcpy(rows[0].label, "Core current", sizeof("Core current")); return DC_OK;
}
dc_status DC_CALL no_pins(dc_gpu*, uint32_t*, int64_t*, uint32_t, uint32_t* n) { *n = 0; return DC_OK; }
CoreApi api() { CoreApi a{}; a.abi_version = version; a.enumerate = enumerate; a.open = open_gpu; a.close = close_gpu; a.identity = identity; a.range = range; a.scale = scale; a.step = step; a.set_offset = setter; a.clock_step = clock_step; a.snapshot = snapshot; a.apply_deltas = deltas; a.curve_layout = curve_layout; a.curve = curve_read; a.envelopes = envelopes; a.reset_curve = curve_reset; a.fan_state = fan_get; a.fan_caps = fan_caps; a.fan_set = fan_set; a.fan_reset = fan_reset; a.fan_restore = fan_restore; a.boost = boost_get; a.boost_set = boost_set; return a; }
Command command(Command::Operation op, double value = 0) { return {op, ControlId::memory, value}; }
}
int main() {
    std::string error;
    auto missing = api(); missing.identity = nullptr;
    CHECK(!create_core_backend_for_test(missing)->open("0000:02:00.0", error));
    no_device = true; auto absent = create_core_backend_for_test(api()); CHECK(!absent->open("0000:02:00.0", error)); no_device = false;
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
    delta = 333; mode = 2; CHECK(!pb->execute(undo).ok && board == 200000 && delta == 333); board = 199000; mode = 0; CHECK(pb->execute(undo).ok && board == 199000 && delta == 333); board = 200000; delta = 123;
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
    fan_mode = 0; fan_auto = false; fan_level = 40; fan_refuse = true; fan.value = 60; fan.operation = Command::Operation::apply;
    CHECK(!b->execute(fan).ok && b->read(false).undo_available); fan_refuse = false;
    fan_post_fail = true; CHECK(!b->execute(fan).ok && b->read(false).undo_available); fan_post_fail = false;
    curve_div = 2; delta = 123; delta2 = 456; auto fresh = pb->read(true); CHECK(fresh.curve_writable && fresh.curve.size() == 2 && fresh.curve_step_khz == 1000 && fresh.curve_freq_div == 2);
    Command curve{}; curve.operation = Command::Operation::apply_curve; curve.expected_curve = fresh.curve; curve.expected_curve_freq_div = fresh.curve_freq_div; curve.curve_deltas = {{3, 200}};
    int before_curve = curve_writes; curve.expected_curve_freq_div = 1; CHECK(!pb->execute(curve).ok && curve_writes == before_curve); curve.expected_curve_freq_div = 2; curve.expected_curve[0].volt_mv += 1; CHECK(!pb->execute(curve).ok && curve_writes == before_curve); curve.expected_curve = fresh.curve;
    curve.curve_deltas.push_back({3, 300}); CHECK(!pb->execute(curve).ok && curve_writes == before_curve); curve.curve_deltas.pop_back();
    CHECK(pb->execute(curve).ok && delta == 200 && delta2 == 456 && curve_writes == before_curve + 1);
    int changed_writes = curve_writes; voltage_shift = 1; CHECK(!pb->execute(undo).ok && curve_writes == changed_writes); voltage_shift = 0;
    base_shift = 1; CHECK(!pb->execute(undo).ok && curve_writes == changed_writes); base_shift = 0;
    CHECK(pb->execute(undo).ok && delta == 123 && delta2 == 456);
    fresh = pb->read(true); curve.expected_curve = fresh.curve; curve_corrupt = true; CHECK(!pb->execute(curve).ok && pb->read(false).undo_label == "Undo V/F curve"); curve_corrupt = false; CHECK(pb->execute(undo).ok && delta == 123 && delta2 == 456);
    Command core_again{Command::Operation::apply, ControlId::core, 60}; CHECK(pb->execute(core_again).ok); changed_writes = writes; voltage_shift = 1; CHECK(!pb->execute(undo).ok && writes == changed_writes); voltage_shift = 0; CHECK(pb->execute(undo).ok);
    fresh = pb->read(true); curve.expected_curve = fresh.curve; curve_geometry_corrupt = true;
    auto geometry_write = pb->execute(curve); CHECK(!geometry_write.ok && geometry_write.may_have_changed && pb->read(false).undo_label == "Undo V/F curve");
    curve_geometry_corrupt = false; base_shift = 0; CHECK(pb->execute(undo).ok && delta == 123 && delta2 == 456);
    CoreApi no_layout = pa; no_layout.curve_layout = nullptr; auto without_layout = create_core_backend_for_test(no_layout); CHECK(without_layout->open("0000:02:00.0", error)); auto layout_state = without_layout->read(false); CHECK(!layout_state.controls[0].writable && layout_state.controls[1].writable);
    CoreApi no_curve = pa; no_curve.curve = nullptr; auto without_curve = create_core_backend_for_test(no_curve); CHECK(without_curve->open("0000:02:00.0", error)); auto no_curve_state = without_curve->read(false); CHECK(!no_curve_state.controls[0].writable && no_curve_state.controls[1].writable);
    CoreApi no_reset = pa; no_reset.reset_curve = nullptr; auto partial_curve = create_core_backend_for_test(no_reset); CHECK(partial_curve->open("0000:02:00.0", error)); auto partial_state = partial_curve->read(true); CHECK(partial_state.curve_writable && !partial_state.curve_reset_writable);
    fresh = pb->read(true); curve.operation = Command::Operation::reset_curve; curve.expected_curve = fresh.curve; curve.curve_deltas.clear(); CHECK(pb->execute(curve).ok && delta == 0 && delta2 == 0); CHECK(pb->execute(undo).ok && delta == 123 && delta2 == 456);
    CoreApi power_test = advanced_api; power_test.policy_read = alternate_policies; power_test.current_limits = core_current; power_test.pins_get = no_pins;
    auto xp = create_core_backend_for_test(power_test); CHECK(xp->open("0000:02:00.0", error)); board = 200000; xoc_live = false; auto normal = xp->read(true); CHECK(normal.controls[2].high == 500.0);
    CHECK(normal.power_policies.size() == 2 && normal.power_policies[1].id == 7 && normal.power_policies[1].core_current && normal.power_policies[1].label == "Core current (NVVDD)");
    current_read_fails = true; auto retained = xp->read(true); CHECK(retained.power_policies[1].core_current); current_read_fails = false;
    current_generic_label = true; auto generic = xp->read(true); CHECK(!generic.power_policies[1].core_current); current_generic_label = false; current_index = 19; auto alternate = xp->read(true); CHECK(alternate.power_policies[1].id == 19 && alternate.power_policies[1].core_current);
    Command mode_on{}; mode_on.operation = Command::Operation::set_xoc; mode_on.enabled = true; CHECK(xp->execute(mode_on).ok); auto expanded = xp->read(false); CHECK(expanded.controls[2].high == 1999.0 && expanded.controls[2].detail.find("software envelope") != std::string::npos);
    Command xoc_power{}; xoc_power.operation = Command::Operation::apply; xoc_power.control = ControlId::power; xoc_power.value = 1999; int writes_before = power_writes; CHECK(!xp->execute(xoc_power).ok && power_writes == writes_before && board == 200000);
    xoc_live = true; CHECK(xp->execute(xoc_power).ok && power_writes == writes_before + 1 && board == 1999000); mode_on.enabled = false; CHECK(xp->execute(mode_on).ok); auto carry = xp->read(false); CHECK(carry.controls[2].high == 1999.0 && carry.controls[2].current == 1999.0);
    xoc_power.value = 200; CHECK(xp->execute(xoc_power).ok && board == 200000); CHECK(xp->read(false).controls[2].high == 500.0);
    std::printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
