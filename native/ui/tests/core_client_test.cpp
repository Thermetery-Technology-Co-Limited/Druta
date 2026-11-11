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
int current = 40, writes = 0, mode = 0, range_fail = 0; bool changed_id = false, no_device = false;
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
    out->present = 1; out->low = -1000; out->high = 2000; out->current = type == 0 ? core_value : current; return DC_OK;
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
dc_status DC_CALL clock_step(dc_gpu*, uint32_t* v, int32_t* m) { *v = 1000; *m = 1; return DC_OK; }
dc_status DC_CALL snapshot(dc_gpu*, dc_vf_delta_snapshot* o) { o->freq_div = curve_div; o->count = 2; o->deltas[0] = {3, delta}; o->deltas[1] = {4, delta2}; return DC_OK; }
dc_status DC_CALL deltas(dc_gpu*, const dc_vf_delta* d, size_t n, dc_outcome* o) { ++curve_writes; if (!curve_failure) { for (size_t i = 0; i < n; ++i) { if (d[i].idx == 3) delta = static_cast<int>(d[i].delta_khz); else if (d[i].idx == 4) delta2 = static_cast<int>(d[i].delta_khz); } if (curve_corrupt) ++delta2; if (curve_geometry_corrupt) ++base_shift; } o->ok = !curve_failure; return DC_OK; }
dc_status DC_CALL curve_layout(dc_gpu*, int32_t, dc_vf_layout* l) { l->gpu_count = 2; l->gpu_idx[0] = 3; l->gpu_idx[1] = 4; l->freq_div = curve_div; return DC_OK; }
dc_status DC_CALL curve_read(dc_gpu*, dc_vf_curve* c) { c->count = 2; c->points[0] = {3, 900.0 + voltage_shift, 1500.0 + base_shift + delta / (1000.0 * curve_div), delta}; c->points[1] = {4, 950, 1600.0 + delta2 / (1000.0 * curve_div), delta2}; return DC_OK; }
dc_status DC_CALL envelopes(dc_request_envelopes* e) { e->max_abs_delta_khz = 1000000; return DC_OK; }
dc_status DC_CALL curve_reset(dc_gpu*, dc_outcome* o) { ++curve_writes; delta = delta2 = 0; o->ok = 1; return DC_OK; }
dc_status DC_CALL fan_get(dc_gpu*, dc_fan_state* s) { if (fan_mode == 2 || fan_read_fail > 0) { if (fan_read_fail > 0) --fan_read_fail; return DC_E_UNAVAILABLE; } std::memcpy(s->source, "nvml", 5); s->count = 2; for (int i = 0; i < 2; ++i) { s->fans[i].id = i; s->fans[i].level = fan_level + (fan_mode == 1 ? i : 0); s->fans[i].policy = 1; s->fans[i].manual = !fan_auto; } return DC_OK; }
dc_status DC_CALL fan_caps(dc_gpu*, dc_fan_capabilities* c) { c->manual = 1; c->automatic = 1; c->max = 100; return DC_OK; }
dc_status DC_CALL fan_set(dc_gpu*, int32_t x, dc_outcome* o) { if (!fan_refuse) { fan_level = x; fan_mode = 0; fan_auto = false; } if (fan_post_fail) fan_read_fail = 2; o->ok = 1; return DC_OK; }
dc_status DC_CALL fan_reset(dc_gpu*, dc_outcome* o) { fan_level = 20; fan_auto = true; if (fan_mode == 2) fan_mode = 0; o->ok = 1; return DC_OK; }
dc_status DC_CALL fan_restore(dc_gpu*, const dc_fan_snapshot* s, dc_outcome* o) { CHECK(s->count == 2 && s->fans[1].level == s->fans[0].level + 1); fan_level = s->fans[0].level; fan_mode = s->fans[1].level != s->fans[0].level ? 1 : 0; fan_auto = !s->fans[0].manual; o->ok = 1; return DC_OK; }
dc_status DC_CALL boost_get(dc_gpu*, int32_t* v) { *v = boost_value; return DC_OK; }
dc_status DC_CALL boost_set(dc_gpu*, int64_t v, dc_outcome* o) { ++boost_writes; boost_value = static_cast<int>(v); o->ok = 1; return DC_OK; }
uint32_t board = 200000; int64_t peer = 100; bool table_fail = false, board_mismatch = false, peer_fail = false, geometry_change = false; bool pin_present = true; int power_writes = 0, policy_writes = 0, policy_reads = 0;
dc_status DC_CALL lock_gpu(dc_gpu*) { return DC_OK; }
dc_status DC_CALL unlock_gpu(dc_gpu*) { return DC_OK; }
dc_status DC_CALL capabilities(dc_gpu*, dc_capabilities* c) { c->present = DC_C_PL_MIN | DC_C_PL_MAX | DC_C_PL_DEF; c->pl_min_mw = 100000; c->pl_max_mw = 350000; c->pl_def_mw = 200000; return DC_OK; }
dc_status DC_CALL power_read(dc_gpu*, dc_cl_statics*, int32_t* has, uint32_t* value, dc_error*) { *has = 1; *value = board; return DC_OK; }
dc_status DC_CALL bounds(dc_gpu*, dc_cl_statics*, int32_t, int64_t* low, int64_t* high, dc_error*) { *low = 100000; *high = 350000; return DC_OK; }
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
    CHECK(!confirm && id == 1 && whole && !pin && !pin_present); ++policy_writes; *ok = !peer_fail; if (!peer_fail) peer = value; *needed = 1; return DC_OK;
}
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
    bad = py; bad["tuning_action"] = {{"kind", "domain"}, {"index", 1}, {"value", 0}}; CHECK(reject(bad).find("unsupported Python tuning action kind: domain") != std::string::npos);
    bad = py; bad["tuning_action"] = {{"kind", "mystery"}, {"index", nullptr}, {"value", 0}}; CHECK(reject(bad).find("unsupported Python tuning action kind: mystery") != std::string::npos);
    CHECK(DeleteFileW(path) != 0);
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
    std::printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
