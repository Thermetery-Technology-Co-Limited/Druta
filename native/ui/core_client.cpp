/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "core_client.h"
#include "profile_io.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

namespace druta::ui {
namespace {
size_t index(ControlId id) { return static_cast<size_t>(id); }
template<class F, class... A> dc_status retry(F f, A... args) {
    if (!f) return DC_E_UNRECOGNIZED;
    dc_status s = f(args...);
    return s == DC_OK ? s : f(args...);
}
bool same_identity(const dc_gpu_identity& a, const dc_gpu_identity& b) {
    return a.slot[0] && b.slot[0] && (b.nvapi_ok || b.nvml_ok) && std::strcmp(a.slot, b.slot) == 0 &&
        std::strcmp(a.uuid, b.uuid) == 0 && a.nvapi_selected == b.nvapi_selected &&
        a.nvml_selected == b.nvml_selected && a.nvapi_handle == b.nvapi_handle &&
        a.nvapi_devid == b.nvapi_devid && a.nvapi_subsys == b.nvapi_subsys &&
        a.nvml_has_devid == b.nvml_has_devid && a.nvml_devid == b.nvml_devid &&
        a.nvml_subsys == b.nvml_subsys && !a.pairing_error[0] && !b.pairing_error[0];
}
class Client final : public IBackend {
    CoreApi api_{};
    dc_gpu* gpu_ = nullptr;
    dc_gpu_identity opened_{};
    LiveData last_{};
    struct PowerSnapshot { uint32_t board = 0; std::vector<dc_policy_row> rows; std::vector<uint32_t> pin_ids; std::vector<int64_t> pin_values; };
    struct Undo { ControlId control; int32_t value = 0; uint32_t scale = 1; dc_vf_delta_snapshot curve{}; dc_fan_snapshot fan{}; std::vector<dc_vf_point> curve_points; PowerSnapshot power; std::vector<Undo> group; bool curve_entry = false; };
    std::vector<Undo> undo_;
    bool suppress_undo_ = false;
    void remember(const Undo& u) { if (suppress_undo_) return; if (undo_.size() == 32) undo_.erase(undo_.begin()); undo_.push_back(u); }
    const char* label(ControlId c) const { switch (c) { case ControlId::core: return "core offset"; case ControlId::memory: return "memory offset"; case ControlId::fan: return "fan"; case ControlId::boost: return "voltage boost"; case ControlId::power: return "board power"; default: return "profile"; } }
    bool capture_fan(dc_fan_snapshot& snap) {
        dc_fan_state state{}; state.size = sizeof(state);
        if (retry(api_.fan_state, gpu_, &state) != DC_OK || !state.count || state.count > 32 || !state.source[0] || !std::memchr(state.source, 0, sizeof(state.source))) return false;
        snap = {}; snap.size = sizeof(snap); snap.is_mapping = 1; std::memcpy(snap.source, state.source, sizeof(snap.source)); snap.count = state.count;
        for (uint32_t i = 0; i < state.count; ++i) { const auto& a = state.fans[i]; auto& b = snap.fans[i]; if (a.level > 100 || (a.manual != 0 && a.manual != 1)) return false; for (uint32_t j = 0; j < i; ++j) if (state.fans[j].id == a.id) return false; b.has_manual = b.has_id = b.level_valid = b.has_policy = 1; b.manual = a.manual; b.id = a.id; b.level = static_cast<int32_t>(a.level); b.policy = a.policy; }
        return true;
    }
    bool capture_curve(dc_vf_delta_snapshot& snap) {
        snap = {}; snap.size = sizeof(snap);
        if (retry(api_.snapshot, gpu_, &snap) != DC_OK || !snap.count || snap.count > DC_VFP_POINTS || !snap.freq_div) return false;
        for (uint32_t i = 0; i < snap.count; ++i) for (uint32_t j = 0; j < i; ++j) if (snap.deltas[i].idx == snap.deltas[j].idx) return false;
        return true;
    }
    bool same_curve(const dc_vf_delta_snapshot& a, const dc_vf_delta_snapshot& b) { if (a.count != b.count || a.freq_div != b.freq_div) return false; for (uint32_t i = 0; i < a.count; ++i) { bool found = false; for (uint32_t j = 0; j < b.count; ++j) if (a.deltas[i].idx == b.deltas[j].idx && a.deltas[i].delta_khz == b.deltas[j].delta_khz) { found = true; break; } if (!found) return false; } return true; }

#ifdef _WIN32
    HMODULE module_ = nullptr;
#endif
    bool valid() const { return api_.abi_version && api_.abi_version() == DC_ABI_VERSION && api_.enumerate && api_.open && api_.close && api_.identity; }
    bool identity(dc_gpu_identity& id) { id = {}; id.size = sizeof(id); return retry(api_.identity, gpu_, &id) == DC_OK && same_identity(opened_, id); }
    void failure(ControlState& c, bool absent, const char* text) {
        c.availability = absent ? Availability::unsupported : Availability::transient;
        c.detail = text;
    }
    bool power_base_api() const { return api_.lock && api_.unlock && api_.power && api_.capabilities && api_.power_bounds && api_.power_set && api_.power_message; }
    bool power_api() const { return power_base_api() && api_.policy_read && api_.policy_set && api_.policy_message && api_.pins_get && api_.pins_set; }
    dc_cl_statics power_statics() {
        dc_cl_statics st{}; st.size = sizeof(st); dc_capabilities caps{}; caps.size = sizeof(caps);
        if (retry(api_.capabilities, gpu_, &caps) == DC_OK) {
            if (caps.present & DC_C_PL_MIN) { st.pl_min_state = DC_CL_STATIC_VALUE; st.pl_min_mw = caps.pl_min_mw; }
            if (caps.present & DC_C_PL_MAX) { st.pl_max_state = DC_CL_STATIC_VALUE; st.pl_max_mw = caps.pl_max_mw; }
            if (caps.present & DC_C_PL_DEF) { st.pl_def_state = DC_CL_STATIC_VALUE; st.pl_def_mw = caps.pl_def_mw; }
        }
        return st;
    }
    struct PolicyInputs { Client* client; dc_cl_statics statics; };
    static int32_t DC_CALL policy_inputs(void* context, int32_t* has, int64_t* mw, dc_cl_statics* st) {
        auto* in = static_cast<PolicyInputs*>(context); uint32_t value = 0; dc_error err{}; err.size = sizeof(err);
        *st = in->statics; if (in->client->api_.power(in->client->gpu_, st, has, &value, &err) != DC_OK) return 1;
        *mw = value; in->statics = *st; return 0;
    }
    bool power_capture(PowerSnapshot& out, std::string& why) {
        if (!power_api()) { why = "Power policy interfaces unavailable"; return false; }
        dc_gpu_identity id{}; if (!identity(id)) { why = "GPU identity changed or is unreadable"; return false; }
        dc_cl_statics st = power_statics(); int32_t has = 0; uint32_t board = 0; dc_error error{}; error.size = sizeof(error);
        if (retry(api_.power, gpu_, &st, &has, &board, &error) != DC_OK || !has || !board || board == UINT32_MAX) { why = "Configured board request unavailable"; return false; }
        uint32_t ids[32]{}; int64_t values[32]{}; uint32_t pins = 0;
        if (api_.pins_get(gpu_, ids, values, 32, &pins) != DC_OK || pins > 32) { why = "Power policy pins unavailable"; return false; }
        dc_policy_row rows[32]{}; for (auto& row : rows) row.size = sizeof(row);
        uint32_t count = 0; PolicyInputs inputs{this, st}; error = {}; error.size = sizeof(error);
        // A null confirmation refuses a derived clamp needing external confirmation.
        if (retry(api_.policy_read, gpu_, static_cast<dc_cl_confirm_fn>(nullptr), static_cast<void*>(nullptr), policy_inputs, static_cast<void*>(&inputs), rows, 32u, &count, &error) != DC_OK || !count || count > 32) { why = "Power policy table unavailable or needs clamp confirmation; board-only route is not established"; return false; }
        for (uint32_t i = 0; i < count; ++i) {
            const auto& r = rows[i];
            if (r.policy >= 32 || r.type > 255 || r.channel > 255 || (r.unit != -1 && r.unit != 0 && r.unit != 1) || r.minimum < 0 || r.maximum > UINT32_MAX || r.minimum > r.maximum || r.requested < 0 || r.requested > UINT32_MAX) { why = "Power policy geometry invalid"; return false; }
            for (uint32_t j = 0; j < i; ++j) if (rows[j].policy == r.policy) { why = "Duplicate power policy"; return false; }
            if (!r.board && (r.writable || r.named_current) && (r.requested < r.minimum || r.requested > r.maximum)) { why = "A peer policy request cannot be restored in its current range"; return false; }
        }
        for (uint32_t i = 0; i < pins; ++i) { bool found = false; for (uint32_t j = 0; j < count; ++j) if (rows[j].policy == ids[i]) found = true; if (!found || ids[i] >= 32 || values[i] < 0 || values[i] >= UINT32_MAX) { why = "Power policy pin cannot be restored"; return false; } for (uint32_t j = 0; j < i; ++j) if (ids[j] == ids[i]) { why = "Duplicate power policy pin"; return false; } }
        out.board = board; out.rows.assign(rows, rows + count); out.pin_ids.assign(ids, ids + pins); out.pin_values.assign(values, values + pins);
        std::sort(out.rows.begin(), out.rows.end(), [](const dc_policy_row& a, const dc_policy_row& b) { return a.policy < b.policy; }); return true;
    }
    bool power_geometry(const PowerSnapshot& saved, const PowerSnapshot& now) {
        if (saved.rows.size() != now.rows.size()) return false;
        for (size_t i = 0; i < saved.rows.size(); ++i) { const auto& a = saved.rows[i]; const auto& b = now.rows[i]; if (a.policy != b.policy || a.type != b.type || a.channel != b.channel || a.unit != b.unit || a.minimum != b.minimum || a.maximum != b.maximum || a.board != b.board || a.named_current != b.named_current) return false; }
        return true;
    }
    std::string power_write_message(bool policy, char* initial, size_t needed, size_t capacity) {
        if (needed <= capacity) return initial;
        if (needed > 1024 * 1024) return std::string(initial) + " (message truncated)";
        std::vector<char> text(needed); size_t full = 0;
        auto getter = policy ? api_.policy_message : api_.power_message;
        if (getter(gpu_, text.data(), text.size(), &full) != DC_OK) return std::string(initial) + " (message truncated)";
        return text.data();
    }
    bool set_board(uint32_t mw, std::string& message) {
        dc_cl_statics st = power_statics(); int32_t ok = 0; char text[4096]{}; size_t needed = 0;
        dc_status status = api_.power_set(gpu_, &st, mw, 0, &ok, text, sizeof(text), &needed);
        message = power_write_message(false, text, needed, sizeof(text)); return status == DC_OK && ok;
    }
    bool set_policy(const dc_policy_row& row, std::string& message) {
        PolicyInputs inputs{this, power_statics()}; int32_t ok = 0; char text[4096]{}; size_t needed = 0;
        dc_status status = api_.policy_set(gpu_, nullptr, nullptr, policy_inputs, &inputs, row.policy, 1, row.requested, 0, row.named_current, &ok, text, sizeof(text), &needed);
        message = power_write_message(true, text, needed, sizeof(text)); return status == DC_OK && ok;
    }
    ActionResult execute_power(const Command& cmd, bool undoing) {
        ActionResult result{}; if (!power_base_api()) { result.message = "Power control interfaces unavailable"; return result; }
        if (api_.lock(gpu_) != DC_OK) { result.message = "Could not lock GPU"; return result; }
        struct Unlock { CoreApi& api; dc_gpu* gpu; ~Unlock() { api.unlock(gpu); } } unlock{api_, gpu_};
        PowerSnapshot current; std::string why; const bool captured = power_capture(current, why);
        if (undoing) {
            const PowerSnapshot saved = undo_.back().power;
            if (!captured || !power_geometry(saved, current)) { result.message = "Power Undo refused: " + (captured ? std::string("policy geometry changed") : why); return result; }
            if (api_.pins_set(gpu_, nullptr, nullptr, 0) != DC_OK) { result.message = "Could not suppress policy pins; no power write issued"; return result; }
            bool ok = true; std::string message;
            if (current.board != saved.board) { result.may_have_changed = true; if (!set_board(saved.board, message)) ok = false; }
            for (const auto& row : saved.rows) {
                PowerSnapshot fresh; if (!power_capture(fresh, why) || !power_geometry(saved, fresh)) { ok = false; message += "; policy geometry or readback changed"; break; }
                const auto it = std::find_if(fresh.rows.begin(), fresh.rows.end(), [&](const dc_policy_row& r) { return r.policy == row.policy; });
                if (row.board || it->requested == row.requested) continue;
                if (!(it->writable || it->named_current) || !it->consistent) { ok = false; message += "; peer policy not restorable"; continue; }
                result.may_have_changed = true; std::string note; if (!set_policy(row, note)) { ok = false; message += "; " + note; }
            }
            PowerSnapshot final; bool verified = power_capture(final, why) && power_geometry(saved, final);
            std::vector<uint32_t> verified_ids; std::vector<int64_t> verified_values;
            if (verified) {
                if (final.board != saved.board) ok = false;
                for (size_t i = 0; i < saved.rows.size(); ++i) if (saved.rows[i].requested != final.rows[i].requested) ok = false;
                for (size_t i = 0; i < saved.pin_ids.size(); ++i) for (size_t j = 0; j < saved.rows.size(); ++j) if (saved.rows[j].policy == saved.pin_ids[i] && final.rows[j].requested == saved.rows[j].requested) { verified_ids.push_back(saved.pin_ids[i]); verified_values.push_back(saved.pin_values[i]); }
                if (api_.pins_set(gpu_, verified_ids.data(), verified_values.data(), static_cast<uint32_t>(verified_ids.size())) != DC_OK || verified_ids.size() != saved.pin_ids.size()) ok = false;
            } else ok = false;
            result.ok = ok; result.message = ok ? "Board power and peer policy requests restored" : "Power restoration incomplete or unverified; Undo retained" + message;
            if (ok) undo_.pop_back(); return result;
        }
        const bool stock = cmd.operation == Command::Operation::stock;
        if (!captured && !stock) { result.message = "Power capture failed; no write issued: " + why; return result; }
        dc_cl_statics st = power_statics(); int64_t low = 0, high = 0; dc_error error{}; error.size = sizeof(error);
        if (api_.power_bounds(gpu_, &st, 0, &low, &high, &error) != DC_OK) { result.message = "Power bounds unavailable; no write issued"; return result; }
        double mw = stock ? (st.pl_def_state == DC_CL_STATIC_VALUE ? static_cast<double>(st.pl_def_mw) : -1.0) : cmd.value * 1000.0;
        if (!std::isfinite(mw) || std::trunc(mw) != mw || mw <= 0 || mw >= UINT32_MAX || mw < low || mw > high) { result.message = "Power request is not a whole mW within the current bounds"; return result; }
        result.requested = mw / 1000.0;
        if (captured && current.board == static_cast<uint32_t>(mw)) { result.ok = true; result.readback = mw / 1000.0; result.message = "Board request already matches"; return result; }
        dc_gpu_identity before_write{}; if (!identity(before_write)) { result.message = "GPU identity changed or unreadable; no write issued"; return result; }
        if (captured) { Undo prior{}; prior.control = ControlId::power; prior.power = current; remember(prior); }
        std::string note; const bool set_ok = set_board(static_cast<uint32_t>(mw), note); result.may_have_changed = true;
        dc_gpu_identity id{}; st = power_statics(); int32_t has = 0; uint32_t got = 0; error = {}; error.size = sizeof(error);
        if (!identity(id) || retry(api_.power, gpu_, &st, &has, &got, &error) != DC_OK || !has) { result.message = note + "; board readback unavailable"; return result; }
        result.readback = got / 1000.0; result.ok = set_ok && got == static_cast<uint32_t>(mw); result.message = note.empty() ? (result.ok ? "Board power applied" : "Board power write failed") : note;
        if (got != static_cast<uint32_t>(mw)) result.message += "; readback differs from request";
        if (!captured) result.message += "; prior policy state unavailable, so this stock action cannot be undone";
        return result;
    }

    bool curve_table(dc_vf_delta_snapshot& snap, std::vector<dc_vf_point>& points, std::string& why) {
        if (!api_.curve_layout || !api_.curve || !api_.snapshot) { why = "Curve interfaces unavailable"; return false; }
        dc_vf_layout layout{}; layout.size = sizeof(layout); dc_vf_curve curve{}; curve.size = sizeof(curve);
        if (retry(api_.curve_layout, gpu_, 1, &layout) != DC_OK || !layout.gpu_count || layout.gpu_count > DC_VFP_POINTS || layout.freq_div == 0 || retry(api_.curve, gpu_, &curve) != DC_OK || curve.count != layout.gpu_count || !capture_curve(snap) || snap.count != layout.gpu_count || snap.freq_div != layout.freq_div) { why = "Complete V/F table unavailable"; return false; }
        for (uint32_t i = 0; i < layout.gpu_count; ++i) {
            const uint32_t idx = layout.gpu_idx[i]; bool point = false, delta = false;
            for (uint32_t j = 0; j < i; ++j) if (layout.gpu_idx[j] == idx) { why = "Duplicate V/F geometry"; return false; }
            for (uint32_t j = 0; j < curve.count; ++j) if (curve.points[j].idx == idx) { if (point || !std::isfinite(curve.points[j].volt_mv) || !std::isfinite(curve.points[j].freq_mhz)) { why = "Invalid V/F point"; return false; } point = true; }
            for (uint32_t j = 0; j < snap.count; ++j) if (snap.deltas[j].idx == idx) delta = true;
            if (!point || !delta) { why = "Incomplete V/F point geometry"; return false; }
        }
        for (uint32_t i = 0; i < curve.count; ++i) { auto it = std::find_if(snap.deltas, snap.deltas + snap.count, [&](const dc_vf_delta& d) { return d.idx == curve.points[i].idx; }); if (it == snap.deltas + snap.count || it->delta_khz != curve.points[i].delta_khz) { why = "V/F reads disagree"; return false; } }
        points.assign(curve.points, curve.points + curve.count); return true;
    }
    bool same_curve_geometry(const dc_vf_delta_snapshot& a, const dc_vf_delta_snapshot& b) {
        if (a.freq_div != b.freq_div || a.count != b.count) return false;
        for (uint32_t i = 0; i < a.count; ++i) if (std::find_if(b.deltas, b.deltas + b.count, [&](const dc_vf_delta& d) { return d.idx == a.deltas[i].idx; }) == b.deltas + b.count) return false;
        return true;
    }
    bool same_curve_mapping(const std::vector<dc_vf_point>& saved, const std::vector<dc_vf_point>& now, uint32_t div) {
        if (!div || saved.size() != now.size()) return false;
        auto base = [div](const dc_vf_point& p) { return std::llround(p.freq_mhz * 1000.0 * div) - static_cast<int64_t>(p.delta_khz); };
        for (const auto& p : saved) {
            auto it = std::find_if(now.begin(), now.end(), [&](const dc_vf_point& q) { return q.idx == p.idx; });
            if (it == now.end() || !std::isfinite(p.volt_mv) || !std::isfinite(p.freq_mhz) || !std::isfinite(it->volt_mv) || !std::isfinite(it->freq_mhz) || p.freq_mhz < 0 || it->freq_mhz < 0 || p.freq_mhz * 1000.0 * div > UINT32_MAX || it->freq_mhz * 1000.0 * div > UINT32_MAX || p.volt_mv != it->volt_mv || base(p) != base(*it)) return false;
        }
        return true;
    }
    ActionResult curve_action(const Command& cmd, bool undoing) {
        ActionResult result{}; const bool reset = cmd.operation == Command::Operation::reset_curve;
        if (!api_.lock || !api_.unlock || !api_.apply_deltas || (!undoing && reset && !api_.reset_curve) || (!undoing && !reset && !api_.envelopes)) { result.message = "V/F write interfaces unavailable"; return result; }
        if (api_.lock(gpu_) != DC_OK) { result.message = "Could not lock GPU for V/F write"; return result; }
        struct Unlock { CoreApi& api; dc_gpu* gpu; ~Unlock() { api.unlock(gpu); } } unlock{api_, gpu_};
        dc_gpu_identity id{}; dc_vf_delta_snapshot before{}; std::vector<dc_vf_point> points; std::string why;
        if (!identity(id) || !curve_table(before, points, why)) { result.message = "V/F read failed or identity changed: " + why; return result; }
        dc_vf_delta_snapshot desired = before;
        if (undoing) {
            desired = undo_.back().curve;
            if (!same_curve_geometry(before, desired) || !same_curve_mapping(undo_.back().curve_points, points, desired.freq_div)) { result.message = "V/F geometry changed; Undo retained"; return result; }
        } else {
            if (cmd.expected_curve_freq_div != before.freq_div || cmd.expected_curve.size() != points.size()) { result.message = "V/F baseline changed; refresh the curve"; return result; }
            for (const auto& point : points) {
                const auto it = std::find_if(cmd.expected_curve.begin(), cmd.expected_curve.end(), [&](const dc_vf_point& p) { return p.idx == point.idx; });
                if (it == cmd.expected_curve.end() || !std::isfinite(it->volt_mv) || !std::isfinite(it->freq_mhz) || it->volt_mv != point.volt_mv || it->delta_khz != point.delta_khz || it->freq_mhz != point.freq_mhz || std::count_if(cmd.expected_curve.begin(), cmd.expected_curve.end(), [&](const dc_vf_point& p) { return p.idx == point.idx; }) != 1) { result.message = "V/F baseline changed; refresh the curve"; return result; }
            }
            if (reset) { if (!cmd.curve_deltas.empty()) { result.message = "Reset cannot include point edits"; return result; } for (uint32_t i = 0; i < desired.count; ++i) desired.deltas[i].delta_khz = 0; }
            else {
                dc_request_envelopes limits{}; limits.size = sizeof(limits);
                if (api_.envelopes(&limits) != DC_OK || limits.max_abs_delta_khz <= 0 || cmd.curve_deltas.empty() || cmd.curve_deltas.size() > desired.count) { result.message = "V/F request envelope or edits unavailable"; return result; }
                for (size_t i = 0; i < cmd.curve_deltas.size(); ++i) {
                    const auto& edit = cmd.curve_deltas[i]; auto it = std::find_if(desired.deltas, desired.deltas + desired.count, [&](const dc_vf_delta& d) { return d.idx == edit.idx; });
                    if (it == desired.deltas + desired.count || edit.delta_khz < -limits.max_abs_delta_khz || edit.delta_khz > limits.max_abs_delta_khz || std::count_if(cmd.curve_deltas.begin(), cmd.curve_deltas.end(), [&](const dc_vf_delta& d) { return d.idx == edit.idx; }) != 1) { result.message = "Invalid, duplicate, or out-of-range V/F point"; return result; }
                    it->delta_khz = edit.delta_khz;
                }
            }
        }
        if (same_curve(before, desired)) { result.ok = true; result.message = "V/F curve already matches"; if (undoing) undo_.pop_back(); return result; }
        if (!undoing) { Undo saved{}; saved.control = ControlId::count; saved.curve_entry = true; saved.curve = before; saved.curve_points = points; remember(saved); }
        const auto before_points = points; dc_outcome out{}; dc_status status;
        if (undoing) status = api_.apply_deltas(gpu_, desired.deltas, desired.count, &out);
        else if (reset) status = api_.reset_curve(gpu_, &out);
        else status = api_.apply_deltas(gpu_, cmd.curve_deltas.data(), cmd.curve_deltas.size(), &out);
        result.may_have_changed = true; dc_vf_delta_snapshot after{}; points.clear();
        if (!identity(id) || !curve_table(after, points, why)) { result.message = "V/F readback unavailable; Undo retained"; return result; }
        result.ok = status == DC_OK && out.ok && same_curve(after, desired) && same_curve_mapping(before_points, points, before.freq_div);
        result.message = out.message[0] ? out.message : (result.ok ? "V/F curve applied" : "V/F write failed");
        if (!same_curve(after, desired) || !same_curve_mapping(before_points, points, before.freq_div)) result.message += "; point readback or geometry differs; Undo retained";
        if (undoing && result.ok) undo_.pop_back(); return result;
    }

    struct ProfileItem { Command command; Undo undo; ProfileJson data; bool changed = false; };
    static bool keys_are(const ProfileJson& object, std::initializer_list<const char*> keys) {
        if (!object.is_object() || object.size() != keys.size()) return false;
        for (auto key : keys) if (!object.contains(key)) return false;
        return true;
    }
    bool profile_item(ControlId control, const ProfileJson* requested, ProfileItem& item, std::string& why) {
        item = {}; item.command.operation = Command::Operation::apply; item.command.control = control; item.undo.control = control;
        auto read_int = [](const ProfileJson& j) -> int64_t { if (!j.is_number_integer()) throw std::runtime_error("profile value must be an integer"); if (j.is_number_unsigned() && j.get<uint64_t>() > static_cast<uint64_t>(INT64_MAX)) throw std::runtime_error("profile integer is too large"); return j.get<int64_t>(); };
        if (control == ControlId::core || control == ControlId::memory) {
            bool memory = control == ControlId::memory; dc_offset_range r{}; dc_mem_offset_scale scale{}; scale.scale = 1;
            if (!api_.set_offset || !api_.range || retry(api_.range, gpu_, memory ? 2u : 0u, &r) != DC_OK || !r.present) { why = "offset range unavailable"; return false; }
            uint32_t step = 1, grid = 0; int32_t measured = 0;
            if (memory) { if (!api_.scale || !api_.step || retry(api_.scale, gpu_, &scale) != DC_OK || !scale.scale || !(step = api_.step(gpu_))) { why = "memory scale or step unavailable"; return false; } }
            else if (!api_.snapshot || !api_.apply_deltas || !api_.clock_step || retry(api_.clock_step, gpu_, &grid, &measured) != DC_OK || !grid || !curve_table(item.undo.curve, item.undo.curve_points, why)) { why = "core curve or grid unavailable"; return false; }
            auto encoded = [grid](double value) { const double whole = std::trunc(value); const double want = std::nearbyint(whole / (grid / 1000.0)) * (grid / 1000.0); return std::ceil(want); };
            if (r.current < r.low || r.current > r.high || (memory ? (static_cast<int64_t>(r.current) % step != 0 || (static_cast<double>(r.current) / scale.scale) * scale.scale != r.current) : encoded(r.current) != r.current)) { why = "prior offset cannot be restored"; return false; }
            item.undo.value = r.current; item.undo.scale = scale.scale;
            const std::string unit = memory ? std::string(scale.unit) : "MHz";
            int64_t desired = r.current;
            if (requested) {
                if (!(memory ? keys_are(*requested, {"value", "unit", "scale"}) : keys_are(*requested, {"value", "unit"})) || !(*requested)["unit"].is_string() || (*requested)["unit"].get<std::string>() != unit || (memory && read_int((*requested)["scale"]) != scale.scale)) throw std::runtime_error("offset unit or scale does not match current adapter");
                desired = read_int((*requested)["value"]);
            }
            if (desired < r.low || desired > r.high || (memory && desired % step) || (!memory && encoded(static_cast<double>(desired)) != desired) || (memory && (static_cast<double>(desired) / scale.scale) * scale.scale != desired)) { why = "saved offset is outside the current range or grid"; return false; }
            item.command.value = static_cast<double>(desired) / scale.scale; item.changed = desired != r.current;
            item.data = {{"value", desired}, {"unit", unit}}; if (memory) item.data["scale"] = scale.scale; return true;
        }
        if (control == ControlId::boost) {
            int32_t value = 0; if (!api_.boost || !api_.boost_set || retry(api_.boost, gpu_, &value) != DC_OK || value < 0 || value > 100) { why = "voltage boost unavailable"; return false; }
            int64_t desired = value; if (requested) { if (!keys_are(*requested, {"value", "unit"}) || (*requested)["unit"] != "%") throw std::runtime_error("invalid boost unit or fields"); desired = read_int((*requested)["value"]); }
            if (desired < 0 || desired > 100) { why = "boost out of range"; return false; }
            item.undo.value = value; item.command.value = static_cast<double>(desired); item.changed = desired != value; item.data = {{"value", desired}, {"unit", "%"}}; return true;
        }
        if (control == ControlId::fan) {
            dc_fan_capabilities caps{}; caps.size = sizeof(caps);
            if (!api_.fan_restore || !api_.fan_caps || !capture_fan(item.undo.fan) || retry(api_.fan_caps, gpu_, &caps) != DC_OK) { why = "fan requests unavailable"; return false; }
            bool all_auto = true, all_manual = true, uniform = true; int32_t value = item.undo.fan.fans[0].level;
            for (uint32_t i = 0; i < item.undo.fan.count; ++i) { const auto& f = item.undo.fan.fans[i]; all_auto = all_auto && !f.manual; all_manual = all_manual && f.manual; uniform = uniform && f.level == value; }
            bool automatic = all_auto; int64_t desired = value;
            if (requested) {
                if (!keys_are(*requested, {"mode", "value", "unit"}) || (*requested)["unit"] != "%" || !(*requested)["mode"].is_string()) throw std::runtime_error("invalid fan fields");
                auto mode = (*requested)["mode"].get<std::string>(); if (mode != "auto" && mode != "manual") throw std::runtime_error("invalid fan mode"); automatic = mode == "auto"; desired = read_int((*requested)["value"]);
                if (automatic && desired != 0) throw std::runtime_error("automatic fan value must be zero");
            } else if (!all_auto && !(all_manual && uniform)) { why = "mixed fan requests omitted"; return false; }
            if (automatic) { if (!caps.automatic || !api_.fan_reset) { why = "automatic fan control unavailable"; return false; } item.command.operation = Command::Operation::stock; item.changed = !all_auto; desired = 0; }
            else { if (!caps.manual || !api_.fan_set || desired < caps.min || desired > caps.max || desired > 100) { why = "manual fan request unavailable or out of range"; return false; } item.command.value = static_cast<double>(desired); item.changed = !(all_manual && uniform && value == desired); }
            item.data = {{"mode", automatic ? "auto" : "manual"}, {"value", desired}, {"unit", "%"}}; return true;
        }
        if (control == ControlId::power) {
            std::string detail; if (!power_capture(item.undo.power, detail)) { why = detail; return false; }
            dc_cl_statics st = power_statics(); dc_error error{}; error.size = sizeof(error); int64_t low = 0, high = 0;
            if (api_.power_bounds(gpu_, &st, 0, &low, &high, &error) != DC_OK) { why = "power bounds unavailable"; return false; }
            int64_t desired = item.undo.power.board;
            if (requested) { if (!keys_are(*requested, {"value", "unit"}) || (*requested)["unit"] != "mW") throw std::runtime_error("invalid power unit or fields"); desired = read_int((*requested)["value"]); }
            if (desired <= 0 || desired >= UINT32_MAX || desired < low || desired > high || item.undo.power.board < low || item.undo.power.board > high) { why = "power request outside restorable bounds"; return false; }
            item.command.value = desired / 1000.0; if (item.command.value * 1000.0 != static_cast<double>(desired)) { why = "power request is not exactly representable"; return false; } item.changed = desired != item.undo.power.board; item.data = {{"value", desired}, {"unit", "mW"}}; return true;
        }
        why = "unknown control"; return false;
    }
    ProfileJson import_python_tuning(const ProfileJson& doc, const dc_gpu_identity& id) {
        if (!doc.is_object() || !doc.contains("scope") || !doc["scope"].is_string()) throw std::runtime_error("unsupported Python profile scope");
        if (doc["scope"] != "tuning_action") throw std::runtime_error("unsupported Python profile scope");
        if (!keys_are(doc, {"schema", "scope", "saved_at", "saved_ts", "device", "incomplete", "tuning_action"}) || !doc["schema"].is_number_integer() || doc["schema"] != 2 || !doc["saved_at"].is_string() || doc["saved_at"].get<std::string>().empty() || !doc["saved_ts"].is_number() || !std::isfinite(doc["saved_ts"].get<double>())) throw std::runtime_error("invalid Python tuning profile metadata");
        const auto& device = doc["device"];
        if (!keys_are(device, {"name", "vbios", "driver", "uuid", "slot"}) || !device["slot"].is_string() || device["slot"].get<std::string>().empty()) throw std::runtime_error("invalid Python tuning profile device metadata");
        for (auto it = device.begin(); it != device.end(); ++it) if (!it.value().is_null() && !it.value().is_string()) throw std::runtime_error("invalid Python tuning profile device metadata");
        if (device["slot"] != id.slot) throw std::runtime_error("Python tuning profile belongs to a different adapter slot");
        if (device["uuid"].is_string() && !device["uuid"].get<std::string>().empty() && device["uuid"] != id.uuid) throw std::runtime_error("Python tuning profile belongs to a different GPU UUID");
        if (!doc["incomplete"].is_array()) throw std::runtime_error("invalid completeness metadata");
        for (const auto& entry : doc["incomplete"]) if (!entry.is_string()) throw std::runtime_error("invalid completeness metadata");
        if (!doc["incomplete"].empty()) throw std::runtime_error("Python tuning profile is incomplete");
        const auto& action = doc["tuning_action"];
        if (!action.is_object() || !action.contains("kind") || !action["kind"].is_string()) throw std::runtime_error("invalid tuning action data");
        const std::string kind = action["kind"].get<std::string>();
        if (kind != "memory" && kind != "boost" && kind != "domain" && kind != "rail_offset") throw std::runtime_error("unsupported Python tuning action kind: " + kind);
        if (!(kind == "memory" ? keys_are(action, {"kind", "index", "value", "scale"}) : keys_are(action, {"kind", "index", "value"}))) throw std::runtime_error("invalid tuning action fields");
        if (kind == "domain") { if (!action["index"].is_number_integer() || action["index"].is_number_unsigned() && action["index"].get<uint64_t>() >= 32 || action["index"].get<int64_t>() < 0 || action["index"].get<int64_t>() >= 32) throw std::runtime_error("invalid clock control index"); }
        else if (!action["index"].is_null()) throw std::runtime_error("index is only valid for a clock domain");
        if (!action["value"].is_number_integer() || (action["value"].is_number_unsigned() && action["value"].get<uint64_t>() > INT32_MAX)) throw std::runtime_error("invalid tuning action value");
        const int64_t value = action["value"].get<int64_t>();
        if (value < INT32_MIN || value > INT32_MAX) throw std::runtime_error("invalid tuning action value");
        ProfileJson controls = ProfileJson::object();
        if (kind == "memory") {
            if (!action["scale"].is_number()) throw std::runtime_error("invalid memory scale");
            const double saved_scale = action["scale"].get<double>();
            if (!std::isfinite(saved_scale) || saved_scale <= 0 || saved_scale >= 2147483648.0 || (value / saved_scale) * saved_scale != value) throw std::runtime_error("invalid or nonrepresentable memory scale");
            dc_mem_offset_scale current{}; if (!api_.scale || retry(api_.scale, gpu_, &current) != DC_OK || !current.scale || !std::memchr(current.unit, 0, sizeof(current.unit))) throw std::runtime_error("current memory scale unavailable");
            controls["memory"] = {{"value", value}, {"unit", current.unit}, {"scale", current.scale}};
        } else if (kind == "boost") {
            if (value < 0 || value > 100) throw std::runtime_error("invalid voltage boost request");
            controls["boost"] = {{"value", value}, {"unit", "%"}};
        } else {
            if (kind == "domain" && value % 1000) throw std::runtime_error("clock domain offset cannot be replayed exactly");
            throw std::runtime_error("unsupported Python tuning action kind: " + kind);
        }
        return {{"format", "druta-native-daily"}, {"version", 1}, {"target", {{"uuid", id.uuid}, {"slot", id.slot}}}, {"controls", controls}};
    }
    ActionResult profile_action(const Command& cmd) {
        ActionResult result{}; const bool save = cmd.operation == Command::Operation::save_profile;
        if (cmd.profile_path.empty()) { result.message = "Choose a profile path"; return result; }
        bool aggregate_stored = false;
        try {
            ProfileJson doc;
            if (!save) doc = read_profile(cmd.profile_path);
            if (!api_.lock || !api_.unlock || api_.lock(gpu_) != DC_OK) { result.message = "Could not lock GPU for profile"; return result; }
            struct Unlock { CoreApi& api; dc_gpu* gpu; ~Unlock() { api.unlock(gpu); } } unlock{api_, gpu_};
            dc_gpu_identity id{}; if (!identity(id)) { result.message = "A verified GPU identity and slot are required for profiles"; return result; }
            const bool python_profile = !save && doc.is_object() && doc.contains("scope");
            if (python_profile) doc = import_python_tuning(doc, id);
            else if (!id.uuid[0]) { result.message = "A verified GPU UUID is required for native profiles"; return result; }
            if (save) doc = {{"format", "druta-native-daily"}, {"version", 1}, {"target", {{"uuid", id.uuid}, {"slot", id.slot}}}, {"controls", ProfileJson::object()}};
            else {
                if (!keys_are(doc, {"format", "version", "target", "controls"}) || doc["format"] != "druta-native-daily" || !doc["version"].is_number_integer() || doc["version"] != 1 || !keys_are(doc["target"], {"uuid", "slot"}) || doc["target"]["uuid"] != id.uuid || doc["target"]["slot"] != id.slot || !doc["controls"].is_object() || doc["controls"].empty()) throw std::runtime_error("invalid profile format or target adapter");
                for (auto it = doc["controls"].begin(); it != doc["controls"].end(); ++it) if (it.key() != "power" && it.key() != "memory" && it.key() != "core" && it.key() != "fan" && it.key() != "boost") throw std::runtime_error("unknown profile control");
            }
            std::vector<ProfileItem> items; std::string included, omitted;
            for (const auto& spec : {std::pair<ControlId, const char*>{ControlId::power, "power"}, {ControlId::memory, "memory"}, {ControlId::boost, "boost"}, {ControlId::fan, "fan"}, {ControlId::core, "core"}}) {
                if (!save && !doc["controls"].contains(spec.second)) continue;
                ProfileItem item; std::string why; const ProfileJson* desired = save ? nullptr : &doc["controls"][spec.second];
                if (!profile_item(spec.first, desired, item, why)) { if (!save) throw std::runtime_error(std::string(spec.second) + ": " + why); if (!omitted.empty()) omitted += "; "; omitted += std::string(spec.second) + " (" + why + ")"; continue; }
                if (save) { if (!included.empty()) included += ", "; included += spec.second; doc["controls"][spec.second] = item.data; } else if (item.changed) items.push_back(std::move(item));
            }
            if (save) { if (doc["controls"].empty()) throw std::runtime_error("no fully qualified controls to save"); write_profile(cmd.profile_path, doc); result.ok = true; result.message = "Native daily profile saved. Included: " + included + "." + (omitted.empty() ? "" : " Omitted: " + omitted + ".") + " Core curve is not saved."; return result; }
            if (items.empty()) { result.ok = true; result.message = python_profile ? "Python tuning action already matches current request" : "Profile already matches current requests; core curve is not saved"; return result; }
            Undo aggregate{}; aggregate.control = ControlId::count; remember(aggregate); aggregate_stored = true;
            suppress_undo_ = true;
            struct Reset { bool& flag; ~Reset() { flag = false; } } reset{suppress_undo_};
            for (const auto& item : items) { undo_.back().group.push_back(item.undo); auto step = execute(item.command); if (!step.may_have_changed) undo_.back().group.pop_back(); result.may_have_changed = result.may_have_changed || step.may_have_changed; if (!step.ok) { if (undo_.back().group.empty()) { undo_.pop_back(); aggregate_stored = false; } result.message = "Profile partially applied: " + step.message + (aggregate_stored ? "; aggregate Undo retained" : "; no writes were issued"); return result; } }
            if (undo_.back().group.empty()) undo_.pop_back();
            result.ok = true; result.message = python_profile ? "Python tuning action profile applied" : "Native daily profile applied; core curve is not saved"; return result;
        } catch (const std::exception& e) { result.message = std::string("Profile failed: ") + e.what() + (aggregate_stored ? "; aggregate Undo retained" : ""); result.may_have_changed = aggregate_stored; return result; }
    }

public:
    explicit Client(CoreApi api) : api_(api) {}
#ifdef _WIN32
    explicit Client(const std::wstring& path) {
        // A fully qualified path is mandatory; never search for a same-named DLL.
        if (path.size() < 3 || !((path[1] == L':' && (path[2] == L'\\' || path[2] == L'/')) || (path[0] == L'\\' && path[1] == L'\\'))) return;
        module_ = LoadLibraryExW(path.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
        if (!module_) return;
#define LOAD(member, name) api_.member = reinterpret_cast<decltype(api_.member)>(GetProcAddress(module_, #name))
        LOAD(abi_version, dc_abi_version); LOAD(enumerate, dc_enumerate_gpus); LOAD(open, dc_gpu_open); LOAD(close, dc_gpu_close); LOAD(identity, dc_gpu_identity_get);
        LOAD(telemetry, dc_gpu_read_telemetry); LOAD(capabilities, dc_gpu_capabilities_get); LOAD(range, dc_gpu_clock_offset_range_get); LOAD(scale, dc_gpu_mem_offset_scale); LOAD(step, dc_gpu_memory_offset_step_units); LOAD(set_offset, dc_gpu_clock_offset_set);
        LOAD(lock, dc_gpu_lock); LOAD(unlock, dc_gpu_unlock); LOAD(power_bounds, dc_pl_bounds); LOAD(power_set, dc_pl_set); LOAD(power_message, dc_pl_message_get); LOAD(policy_read, dc_cl_power_policies_read); LOAD(policy_set, dc_cl_set_power_policy); LOAD(policy_message, dc_cl_policy_message_get); LOAD(pins_get, dc_cl_pins_get); LOAD(pins_set, dc_cl_pins_set); LOAD(envelopes, dc_request_envelopes_get); LOAD(reset_curve, dc_gpu_vf_curve_reset); LOAD(clock_step, dc_gpu_clock_step_khz); LOAD(snapshot, dc_gpu_vf_delta_snapshot); LOAD(apply_deltas, dc_gpu_vf_deltas_apply); LOAD(fan_set, dc_gpu_fan_set); LOAD(fan_reset, dc_gpu_fan_reset); LOAD(fan_restore, dc_gpu_fan_restore); LOAD(boost_set, dc_gpu_voltage_boost_set); LOAD(fan_state, dc_gpu_fan_state_get); LOAD(fan_caps, dc_gpu_fan_capabilities_get); LOAD(boost, dc_gpu_voltage_boost_get); LOAD(curve_layout, dc_gpu_vf_layout); LOAD(curve, dc_gpu_vf_curve_read); LOAD(power, dc_pl_read); LOAD(enforced_power, dc_pl_enforced_read);
#undef LOAD
    }
#endif
    ~Client() override { close();
#ifdef _WIN32
        if (module_) FreeLibrary(module_);
#endif
    }
    std::vector<Device> enumerate() override {
        std::vector<Device> result; if (!valid()) return result;
        size_t n = 0; dc_status s = api_.enumerate(nullptr, 0, &n);
        if ((s != DC_OK && s != DC_E_BUFFER) || n > 1024) return result;
        std::vector<dc_gpu_entry> entries(n); for (auto& e : entries) e.size = sizeof(e);
        if (n && api_.enumerate(entries.data(), n, &n) != DC_OK) return result;
        for (const auto& e : entries) result.push_back({e.slot, e.name, e.uuid});
        return result;
    }
    bool open(const std::string& slot, std::string& error) override {
        close(); if (!valid()) { error = "Core DLL is missing required exports or has an incompatible ABI"; return false; }
        if (slot.empty() || api_.open(slot.c_str(), &gpu_) != DC_OK || !gpu_) { error = "Could not open selected GPU"; return false; }
        opened_.size = sizeof(opened_);
        if (retry(api_.identity, gpu_, &opened_) != DC_OK || !opened_.slot[0] || !(opened_.nvapi_ok || opened_.nvml_ok) || opened_.pairing_error[0] || slot != opened_.slot) {
            error = "Could not verify selected GPU identity and pairing"; close(); return false;
        }
        last_.identity = opened_; last_.connected = true; error.clear(); return true;
    }
    void close() override { if (gpu_ && api_.close) api_.close(gpu_); gpu_ = nullptr; opened_ = {}; last_ = {}; undo_.clear(); }
    LiveData read(bool details) override {
        if (!gpu_) { last_.status = valid() ? "No GPU selected" : "Core DLL unavailable or incompatible"; return last_; }
        dc_gpu_identity id{}; if (!identity(id)) { last_.connected = false; last_.status = "GPU identity read failed or changed"; return last_; }
        last_.connected = true; last_.identity = id; last_.status.clear();
        dc_telemetry t{}; t.size = sizeof(t);
        if (retry(api_.telemetry, gpu_, &t) == DC_OK) { last_.telemetry = t; last_.telemetry_valid = true; last_.telemetry_error.clear(); }
        else { last_.telemetry_valid = false; last_.telemetry_error = "Telemetry unavailable"; }
        dc_mem_offset_scale scale{}; const bool scale_ok = api_.scale && retry(api_.scale, gpu_, &scale) == DC_OK && scale.scale && api_.step;
        const uint32_t step = scale_ok ? api_.step(gpu_) : 0;
        for (auto control : {ControlId::core, ControlId::memory}) {
            auto& c = last_.controls[index(control)]; dc_offset_range r{};
            const bool mem = control == ControlId::memory; auto s = retry(api_.range, gpu_, mem ? 2u : 0u, &r);
            if (s == DC_OK && r.present && (!mem || (scale_ok && step))) {
                double div = mem ? scale.scale : 1; c.availability = Availability::available;
                c.current = r.current / div; c.low = r.low / div; c.high = r.high / div; c.stock = 0;
                c.unit = mem ? scale.unit : "MHz"; c.step = mem ? std::optional<double>(step / div) : std::nullopt;
                c.writable = api_.set_offset && (mem || (api_.snapshot && api_.apply_deltas && api_.clock_step && api_.curve && api_.curve_layout)); c.stock_writable = c.writable; c.detail.clear();
            } else failure(c, !api_.range || (mem && (!api_.scale || !api_.step)), "Fresh offset range or scale unavailable");
        }
        auto& fan = last_.controls[index(ControlId::fan)]; dc_fan_state fs{}; fs.size = sizeof(fs); dc_fan_capabilities fc{}; fc.size = sizeof(fc);
        const bool fan_caps_ok = retry(api_.fan_caps, gpu_, &fc) == DC_OK;
        if (fan_caps_ok) fan.stock_writable = fc.automatic && api_.fan_reset;
        if (retry(api_.fan_state, gpu_, &fs) == DC_OK && fan_caps_ok && fs.count && fs.count <= 32) {
            fan.availability = Availability::available; bool mixed = false;
            for (uint32_t i = 1; i < fs.count; ++i) if (fs.fans[i].level != fs.fans[0].level || fs.fans[i].policy != fs.fans[0].policy || fs.fans[i].manual != fs.fans[0].manual) mixed = true;
            if (mixed) fan.current.reset(); else fan.current = fs.fans[0].level;
            fan.low = fc.min; fan.high = fc.max; fan.unit = "%"; fan.writable = fc.manual && api_.fan_set && api_.fan_restore; fan.detail = mixed ? "Mixed fan requests or policies" : "";
        } else failure(fan, !api_.fan_state || !api_.fan_caps, "Fan request unavailable");
        auto& boost = last_.controls[index(ControlId::boost)]; int32_t v = 0;
        if (retry(api_.boost, gpu_, &v) == DC_OK) { boost.availability = Availability::available; boost.current = v; boost.low = 0; boost.high = 100; boost.unit = "%"; boost.writable = api_.boost_set != nullptr; boost.stock_writable = boost.writable; boost.detail.clear(); }
        else failure(boost, !api_.boost, "Voltage boost unavailable");
        dc_capabilities caps{}; caps.size = sizeof(caps); dc_cl_statics st{}; st.size = sizeof(st);
        if (retry(api_.capabilities, gpu_, &caps) == DC_OK) {
            if (caps.present & DC_C_PL_MIN) { st.pl_min_state = DC_CL_STATIC_VALUE; st.pl_min_mw = caps.pl_min_mw; }
            if (caps.present & DC_C_PL_MAX) { st.pl_max_state = DC_CL_STATIC_VALUE; st.pl_max_mw = caps.pl_max_mw; }
            if (caps.present & DC_C_PL_DEF) { st.pl_def_state = DC_CL_STATIC_VALUE; st.pl_def_mw = caps.pl_def_mw; }
        }
        auto& power = last_.controls[index(ControlId::power)];
        if (power_base_api()) { dc_cl_statics bounds_st = st; int64_t lo = 0, hi = 0; dc_error bounds_err{}; bounds_err.size = sizeof(bounds_err);
            if (api_.power_bounds(gpu_, &bounds_st, 0, &lo, &hi, &bounds_err) == DC_OK && bounds_st.pl_def_state == DC_CL_STATIC_VALUE) power.stock_writable = bounds_st.pl_def_mw > 0 && bounds_st.pl_def_mw < UINT32_MAX && bounds_st.pl_def_mw >= lo && bounds_st.pl_def_mw <= hi;
        }
        int32_t has = 0; uint32_t mw = 0; dc_error err{}; err.size = sizeof(err);
        if (retry(api_.power, gpu_, &st, &has, &mw, &err) == DC_OK && has) {
            power.availability = Availability::available; power.current = mw / 1000.0; power.unit = "W";
            if (details) { PowerSnapshot captured; std::string reason; bool captured_ok = false;
                if (power_api() && api_.lock(gpu_) == DC_OK) { captured_ok = power_capture(captured, reason); api_.unlock(gpu_); }
                else reason = "Power policy interfaces or lock unavailable";
                if (captured_ok) { power.writable = true; power.detail = "Configured request"; } else { power.availability = Availability::transient; power.detail = reason; }
            }
            if (st.pl_min_state == DC_CL_STATIC_VALUE) power.low = st.pl_min_mw / 1000.0;
            if (st.pl_max_state == DC_CL_STATIC_VALUE) power.high = st.pl_max_mw / 1000.0;
            if (st.pl_def_state == DC_CL_STATIC_VALUE) power.stock = st.pl_def_mw / 1000.0;
        } else failure(power, !api_.power, "Configured power request unavailable");
        has = 0; mw = 0; if (retry(api_.enforced_power, gpu_, &has, &mw) == DC_OK && has) last_.enforced_power_w = mw / 1000.0; else last_.enforced_power_w.reset();
        if (details) {
            dc_vf_delta_snapshot snapshot{}; std::vector<dc_vf_point> points; std::string why; uint32_t curve_step = 0; int32_t measured = 0;
            if (api_.lock && api_.unlock && api_.lock(gpu_) == DC_OK) {
                const bool complete = curve_table(snapshot, points, why);
                const bool grid = retry(api_.clock_step, gpu_, &curve_step, &measured) == DC_OK && curve_step;
                api_.unlock(gpu_);
                if (complete) { last_.curve = std::move(points); last_.curve_error.clear(); last_.curve_step_khz = grid ? curve_step : 0; last_.curve_freq_div = snapshot.freq_div; last_.curve_writable = api_.apply_deltas && api_.envelopes && grid; last_.curve_reset_writable = api_.apply_deltas && api_.reset_curve; }
                else { last_.curve.clear(); last_.curve_error = why; last_.curve_writable = false; last_.curve_reset_writable = false; last_.curve_step_khz = 0; last_.curve_freq_div = 0; }
            } else { last_.curve.clear(); last_.curve_error = "Could not lock GPU for curve"; last_.curve_writable = false; last_.curve_reset_writable = false; last_.curve_step_khz = 0; last_.curve_freq_div = 0; }
        }
        last_.undo_available = !undo_.empty(); last_.undo_label = undo_.empty() ? "" : std::string("Undo ") + (undo_.back().curve_entry ? "V/F curve" : label(undo_.back().control)); return last_;
    }
    ActionResult execute(const Command& cmd) override {
        ActionResult result{};
        if (!gpu_) { result.message = "No GPU selected"; return result; }
        if (cmd.operation == Command::Operation::save_profile || cmd.operation == Command::Operation::load_profile) return profile_action(cmd);
        if (cmd.operation == Command::Operation::apply_curve || cmd.operation == Command::Operation::reset_curve) return curve_action(cmd, false);
        if (cmd.operation == Command::Operation::undo && !undo_.empty() && undo_.back().curve_entry) return curve_action(cmd, true);
        if (cmd.operation == Command::Operation::undo && !undo_.empty() && undo_.back().control == ControlId::count) {
            // Restore each completed request only once; keep core V/F restoration last.
            while (!undo_.back().group.empty()) {
                auto& group = undo_.back().group;
                auto it = std::find_if(group.begin(), group.end(), [](const Undo& item) { return item.control != ControlId::core; });
                const size_t position = it == group.end() ? 0 : static_cast<size_t>(it - group.begin());
                Undo item = group[position]; undo_.push_back(item); Command single{}; single.operation = Command::Operation::undo;
                ActionResult step;
                try { step = execute(single); } catch (const std::exception& e) { undo_.pop_back(); result.may_have_changed = true; result.message = std::string("Profile Undo failed: ") + e.what() + "; aggregate Undo retained"; return result; } catch (...) { undo_.pop_back(); result.may_have_changed = true; result.message = "Profile Undo failed; aggregate Undo retained"; return result; }
                result.may_have_changed = result.may_have_changed || step.may_have_changed;
                if (!step.ok) { undo_.pop_back(); result.message = "Profile Undo incomplete: " + step.message + "; aggregate Undo retained"; return result; }
                undo_.back().group.erase(undo_.back().group.begin() + position);
            }
            undo_.pop_back(); result.ok = true; result.message = "Profile requests restored"; return result;
        }
        const bool undoing = cmd.operation == Command::Operation::undo;
        if (undoing && undo_.empty()) { result.message = "Nothing to undo"; return result; }
        const ControlId control = undoing ? undo_.back().control : cmd.control;
        dc_gpu_identity id{}; if (!identity(id)) { result.message = "GPU identity read failed or changed; no write issued"; return result; }
        if (control == ControlId::power) return execute_power(cmd, undoing);
        if (control == ControlId::fan) {
            if ((undoing && !api_.fan_restore) || (!undoing && !(cmd.operation == Command::Operation::stock ? api_.fan_reset != nullptr : (api_.fan_set && api_.fan_restore)))) { result.message = "Fan control interface unavailable"; return result; }
            Undo prior{}; prior.control = control; const bool captured = capture_fan(prior.fan);
            const bool auto_request = !undoing && cmd.operation == Command::Operation::stock;
            if (!captured && !auto_request && !undoing) { result.message = "Fan requests could not be captured; no write issued"; return result; }
            if (!undoing && !auto_request && (!std::isfinite(cmd.value) || std::trunc(cmd.value) != cmd.value || cmd.value < 0 || cmd.value > 100)) { result.message = "Fan request must be a whole percentage from 0 to 100"; return result; }
            if (!undoing && captured && api_.fan_restore) remember(prior);
            dc_outcome out{}; dc_status status;
            if (undoing) status = api_.fan_restore(gpu_, &undo_.back().fan, &out);
            else if (auto_request) status = api_.fan_reset(gpu_, &out);
            else status = api_.fan_set(gpu_, static_cast<int32_t>(cmd.value), &out);
            result.may_have_changed = true; result.ok = status == DC_OK && out.ok;
            if (!undoing && result.ok) {
                dc_fan_snapshot after{}; dc_gpu_identity verified_id{}; bool verified = identity(verified_id) && capture_fan(after);
                if (verified && captured) { verified = std::strcmp(after.source, prior.fan.source) == 0 && after.count == prior.fan.count; for (uint32_t i = 0; verified && i < prior.fan.count; ++i) { bool found = false; for (uint32_t j = 0; j < after.count; ++j) if (after.fans[j].id == prior.fan.fans[i].id) found = true; if (!found) verified = false; } }
                for (uint32_t i = 0; verified && i < after.count; ++i) if (auto_request ? after.fans[i].manual != 0 : (after.fans[i].manual == 0 || after.fans[i].level != static_cast<int32_t>(cmd.value))) verified = false;
                if (!verified) { result.ok = false; result.message = "Fan readback differs or is unavailable; Undo retained when captured"; }
            }
            if (result.message.empty()) result.message = out.message[0] ? out.message : (result.ok ? "Fan request applied" : "Fan request failed");
            if ((!captured || !api_.fan_restore) && auto_request) result.message += "; prior fan requests were unreadable and cannot be undone";
            if (undoing && result.ok) undo_.pop_back();
            return result;
        }
        if (control == ControlId::boost) {
            if (!api_.boost || !api_.boost_set) { result.message = "Voltage boost interface unavailable"; return result; }
            int32_t before = 0;
            if (retry(api_.boost, gpu_, &before) != DC_OK || before < 0 || before > 100) { result.message = "Voltage boost request unreadable; no write issued"; return result; }
            double target = undoing ? undo_.back().value : (cmd.operation == Command::Operation::stock ? 0 : cmd.value);
            if (!std::isfinite(target) || std::trunc(target) != target || target < 0 || target > 100) { result.message = "Voltage boost must be a whole percentage from 0 to 100"; return result; }
            result.requested = target; if (target == before) { result.ok = true; result.readback = before; result.message = "Voltage boost already matches"; if (undoing) undo_.pop_back(); return result; }
            if (!undoing) { Undo prior{}; prior.control = control; prior.value = before; remember(prior); }
            dc_outcome out{}; const dc_status status = api_.boost_set(gpu_, static_cast<int64_t>(target), &out); result.may_have_changed = true;
            int32_t after = 0; dc_gpu_identity check{};
            if (!identity(check) || retry(api_.boost, gpu_, &after) != DC_OK) { result.message = "Voltage boost readback unavailable; Undo retained"; return result; }
            result.readback = after; result.ok = status == DC_OK && out.ok && after == target;
            result.message = out.message[0] ? out.message : (result.ok ? "Voltage boost applied" : "Voltage boost write failed");
            if (after != target) result.message += "; readback differs from request";
            if (undoing && result.ok) undo_.pop_back(); return result;
        }
        const bool memory = control == ControlId::memory;
        if ((!memory && control != ControlId::core) || !api_.range || !api_.set_offset || (memory && (!api_.scale || !api_.step)) || (!memory && (!api_.snapshot || !api_.apply_deltas || !api_.clock_step))) { result.message = "Offset interface unavailable"; return result; }
        dc_offset_range before{}; dc_mem_offset_scale scale{}; scale.scale = 1;
        const uint32_t type = memory ? 2u : 0u;
        if (retry(api_.range, gpu_, type, &before) != DC_OK || !before.present || (memory && (retry(api_.scale, gpu_, &scale) != DC_OK || !scale.scale))) { result.message = "Offset readback or scale unavailable; no write issued"; return result; }
        uint32_t step = 1, core_step = 0; int32_t measured = 0;
        if (memory) step = api_.step(gpu_); else if (retry(api_.clock_step, gpu_, &core_step, &measured) != DC_OK || !core_step) step = 0;
        if (!step) { result.message = "Offset step unavailable; no write issued"; return result; }
        auto core_encoded = [core_step](double value) { const double whole = std::trunc(value); const double want = std::nearbyint(whole / (core_step / 1000.0)) * (core_step / 1000.0); return std::ceil(want); };
        if (before.current < before.low || before.current > before.high || (memory ? (static_cast<int64_t>(before.current) % step != 0 || (static_cast<double>(before.current) / scale.scale) * scale.scale != before.current) : core_encoded(before.current) != before.current)) { result.message = "Prior offset cannot be restored exactly; no write issued"; return result; }
        Undo prior{}; prior.control = control; prior.value = before.current; prior.scale = scale.scale;
        if (undoing && memory && undo_.back().scale != scale.scale) { result.message = "Memory offset scale changed; Undo retained with no write issued"; return result; }
        if (!memory) {
            std::string why; if (!curve_table(prior.curve, prior.curve_points, why)) { result.message = "Complete V/F curve snapshot unavailable; no write issued"; return result; }
            if (undoing && (!same_curve_geometry(prior.curve, undo_.back().curve) || !same_curve_mapping(undo_.back().curve_points, prior.curve_points, prior.curve.freq_div))) { result.message = "V/F geometry changed; core Undo retained with no write issued"; return result; }
        }
        double target = undoing ? static_cast<double>(undo_.back().value) / scale.scale : (cmd.operation == Command::Operation::stock ? 0.0 : cmd.value);
        double scaled = target * scale.scale;
        if (!std::isfinite(scaled) || scaled < std::numeric_limits<int32_t>::min() || scaled > std::numeric_limits<int32_t>::max() || std::trunc(scaled) != scaled) { result.message = "Offset is not exactly representable; no write issued"; return result; }
        if (!memory) scaled = core_encoded(target);
        if (scaled < before.low || scaled > before.high || (memory && static_cast<int64_t>(scaled) % step)) { result.message = "Offset is outside the current range or step; no write issued"; return result; }
        const int32_t units = static_cast<int32_t>(scaled); result.requested = target;
        if (units == before.current && (!undoing || memory)) { result.ok = true; result.readback = target; result.message = "Offset already matches"; if (undoing) undo_.pop_back(); return result; }
        if (!undoing) remember(prior);
        dc_outcome out{}; dc_status status = DC_OK;
        if (units != before.current) { status = api_.set_offset(gpu_, type, target, 1, &out); result.may_have_changed = true; }
        dc_offset_range after{}; dc_gpu_identity check{};
        if (!identity(check) || retry(api_.range, gpu_, type, &after) != DC_OK || !after.present) { result.message = "Offset readback unavailable; Undo retained"; return result; }
        result.readback = static_cast<double>(after.current) / scale.scale;
        result.ok = status == DC_OK && (units == before.current || out.ok) && after.current == units;
        result.message = out.message[0] ? out.message : (result.ok ? "Offset applied" : "Offset write failed");
        if (after.current != units) result.message += "; readback differs from requested value";
        if (!memory && units != target) result.message += "; request was snapped to the card's clock grid";
        if (undoing && !memory && result.ok) {
            const auto& saved = undo_.back().curve; dc_outcome curve_out{};
            const auto curve_status = api_.apply_deltas(gpu_, saved.deltas, saved.count, &curve_out); result.may_have_changed = true;
            dc_vf_delta_snapshot got{}; std::vector<dc_vf_point> got_points; std::string why;
            result.ok = curve_status == DC_OK && curve_out.ok && identity(check) && curve_table(got, got_points, why) && same_curve(saved, got) && same_curve_mapping(undo_.back().curve_points, got_points, saved.freq_div);
            if (!result.ok) result.message += "; V/F delta restoration was incomplete or unreadable; Undo retained";
        }
        if (undoing && result.ok) undo_.pop_back(); return result;
    }
};
}
std::unique_ptr<IBackend> create_core_backend_for_test(CoreApi api) { return std::make_unique<Client>(api); }
std::unique_ptr<IBackend> create_core_backend(const std::wstring& path) {
#ifdef _WIN32
    return std::make_unique<Client>(path);
#else
    (void)path; return std::make_unique<Client>(CoreApi{});
#endif
}
}
