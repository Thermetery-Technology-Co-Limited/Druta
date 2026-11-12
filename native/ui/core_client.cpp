/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "core_client.h"
#include "profile_io.h"
#include "curve_profile.h"
#include "scoped_profile.h"
#include "clock_monitor_service.h"
#include "timing_model.h"
#include "timing_profile.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <sstream>
#include <locale>
#include <chrono>
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
    std::unique_ptr<PerfViewService> perf_;
    std::unique_ptr<FrequencyLock> frequency_;
    ClockMonitorService clock_monitor_;
    std::optional<uint32_t> clock_architecture_;
    bool clock_architecture_attempted_ = false, clock_step_measured_ = false;
    uint32_t clock_step_khz_ = 0;
    uint64_t clock_sample_ = 0;
    void clock_tick(const dc_telemetry* telemetry) {
        ClockMonitorInput input;
        input.telemetry = telemetry; input.sample_id = ++clock_sample_;
        input.now_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
        input.architecture = clock_architecture_; input.step_khz = clock_step_khz_;
        input.step_measured = clock_step_measured_;
        last_.clock_monitor = std::make_shared<ClockMonitorData>(clock_monitor_.update(api_.clock_classify, input));
    }
    struct PowerSnapshot { uint32_t board = 0; std::vector<dc_policy_row> rows; std::vector<uint32_t> pin_ids; std::vector<int64_t> pin_values; std::optional<uint32_t> selected_policy; std::vector<uint32_t> selected_policies; bool guard_unselected_pins = false; };
    struct Undo { ControlId control; int32_t value = 0; uint32_t scale = 1; dc_vf_delta_snapshot curve{}; dc_vf_layout curve_layout{}; dc_fan_snapshot fan{}; std::vector<dc_vf_point> curve_points; PowerSnapshot power; std::vector<Undo> group; bool curve_entry = false; bool advanced_entry = false; AdvancedId advanced{}; dc_clkdom_layout advanced_layout{}; bool rail_entry = false; RailUndo rail{}; };
    std::vector<Undo> undo_;
    bool suppress_undo_ = false;
    bool rail_mode_ = false;
    std::unique_ptr<RailControls> rail_service_;
    std::unique_ptr<I2cSession> i2c_session_;
    std::unique_ptr<TuningActions> tuning_;
    std::unique_ptr<TimingTransport> timing_transport_;
    std::unique_ptr<TimingsService> timings_;
    TimingViewData timing_data_;
    std::optional<TimingPlan> timing_undo_;
    std::optional<TimingBatchPlan> timing_batch_undo_;
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
    bool current_xoc(bool& xoc) {
        int32_t msvdd = 0, voltage = 0;
        if (!api_.clkdom_flags || retry(api_.clkdom_flags, gpu_, &msvdd, &voltage) != DC_OK || (msvdd != 0 && msvdd != 1) || (voltage != 0 && voltage != 1) || msvdd != voltage) return false;
        xoc = voltage != 0; return true;
    }
    bool effective_xoc(bool& xoc) { xoc = false; return !api_.clkdom_flags || current_xoc(xoc); }
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
    static int32_t DC_CALL confirm_clamp(void* context, uint32_t, uint32_t, uint32_t, const uint32_t* info, uint32_t info_count, const uint32_t* control, uint32_t control_count) {
        auto* self = static_cast<Client*>(context);
        if (!self || !self->api_.cpc_live_read || !info || !control || info_count > DC_CPC_NATIVE_INFO_MAX / 4 || control_count > DC_CPC_NATIVE_CONTROL_MAX / 4) return 0;
        dc_cpc_observation observation{}; observation.size = sizeof(observation); observation.metadata.size = sizeof(observation.metadata); observation.arbitration.size = sizeof(observation.arbitration);
        dc_error error{}; error.size = sizeof(error);
        if (self->api_.cpc_live_read(self->gpu_, &observation, &error) != DC_OK) return 0;
        const auto& metadata = observation.metadata;
        return metadata.role_info_size > 4 && metadata.role_info[4] == 0x30 && observation.requested_mw > metadata.maximum_mw &&
            metadata.native_info_size == info_count * 4 && observation.native_control_size == control_count * 4 &&
            std::memcmp(metadata.native_info, info, metadata.native_info_size) == 0 && std::memcmp(observation.native_control, control, observation.native_control_size) == 0;
    }
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
        if (retry(api_.policy_read, gpu_, confirm_clamp, static_cast<void*>(this), policy_inputs, static_cast<void*>(&inputs), rows, 32u, &count, &error) != DC_OK || !count || count > 32) { why = "Power policy table unavailable or needs clamp confirmation; board-only route is not established"; return false; }
        for (uint32_t i = 0; i < count; ++i) {
            const auto& r = rows[i];
            if (r.policy >= 32 || r.type > 255 || r.channel > 255 || (r.unit != -1 && r.unit != 0 && r.unit != 1) || r.minimum < 0 || r.maximum > UINT32_MAX || r.minimum > r.maximum || r.requested < 0 || r.requested > UINT32_MAX) { why = "Power policy geometry invalid"; return false; }
            for (uint32_t j = 0; j < i; ++j) if (rows[j].policy == r.policy) { why = "Duplicate power policy"; return false; }
        }
        for (uint32_t i = 0; i < pins; ++i) { bool found = false; for (uint32_t j = 0; j < count; ++j) if (rows[j].policy == ids[i]) found = true; if (!found || ids[i] >= 32 || values[i] < 0 || values[i] >= UINT32_MAX) { why = "Power policy pin cannot be restored"; return false; } for (uint32_t j = 0; j < i; ++j) if (ids[j] == ids[i]) { why = "Duplicate power policy pin"; return false; } }
        out.board = board; out.rows.assign(rows, rows + count); out.pin_ids.assign(ids, ids + pins); out.pin_values.assign(values, values + pins);
        std::sort(out.rows.begin(), out.rows.end(), [](const dc_policy_row& a, const dc_policy_row& b) { return a.policy < b.policy; }); return true;
    }
    bool power_geometry(const PowerSnapshot& saved, const PowerSnapshot& now) {
        if (saved.rows.size() != now.rows.size()) return false;
        for (size_t i = 0; i < saved.rows.size(); ++i) { const auto& a = saved.rows[i]; const auto& b = now.rows[i]; if (a.policy != b.policy || a.type != b.type || a.channel != b.channel || a.unit != b.unit || a.board != b.board || a.named_current != b.named_current) return false; }
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
        bool xoc = false; if (!effective_xoc(xoc)) { message = "XOC session state unreadable; no power write issued"; return false; } dc_status status = api_.power_set(gpu_, &st, mw, xoc ? 1 : 0, &ok, text, sizeof(text), &needed);
        message = power_write_message(false, text, needed, sizeof(text)); return status == DC_OK && ok;
    }
    bool set_policy(const dc_policy_row& row, std::string& message, bool pin = false) {
        PolicyInputs inputs{this, power_statics()}; int32_t ok = 0; char text[4096]{}; size_t needed = 0;
        dc_status status = api_.policy_set(gpu_, confirm_clamp, this, policy_inputs, &inputs, row.policy, 1, row.requested, pin ? 1 : 0, row.named_current, &ok, text, sizeof(text), &needed);
        message = power_write_message(true, text, needed, sizeof(text)); return status == DC_OK && ok;
    }
    static bool binding_matches(const Command& cmd, const std::string& actual) { return !cmd.expected_binding || cmd.expected_binding->empty() || *cmd.expected_binding == actual; }
    static std::string policy_binding(const dc_policy_row& row) {
        std::ostringstream out; out.imbue(std::locale::classic());
        out << "policy:" << row.policy << ':' << row.type << ':' << row.channel << ':' << row.unit << ':' << row.minimum << ':' << row.maximum << ':' << row.board << ':' << row.named_current;
        return out.str();
    }
    static std::string board_binding(const PowerSnapshot& power) {
        std::ostringstream binding; binding.imbue(std::locale::classic()); binding << "board";
        for (const auto& row : power.rows) binding << ':' << row.policy << ':' << row.type << ':' << row.channel << ':' << row.unit << ':' << row.board << ':' << row.named_current;
        return binding.str();
    }
    static std::string power_state_token(const PowerSnapshot& power) {
        std::ostringstream out; out.imbue(std::locale::classic()); out << board_binding(power) << ':' << power.board;
        for (const auto& row : power.rows) out << ':' << row.policy << ':' << row.requested;
        // Pin order is not meaningful; compare its canonical mapping.
        std::vector<std::pair<uint32_t, int64_t>> pins;
        for (size_t i = 0; i < power.pin_ids.size(); ++i) pins.emplace_back(power.pin_ids[i], power.pin_values[i]);
        std::sort(pins.begin(), pins.end()); for (const auto& pin : pins) out << ":pin:" << pin.first << ':' << pin.second;
        return out.str();
    }
    static bool selected_policy(const PowerSnapshot& power, uint32_t policy) {
        return (power.selected_policy && *power.selected_policy == policy) || std::find(power.selected_policies.begin(), power.selected_policies.end(), policy) != power.selected_policies.end();
    }
    static bool same_unselected_pins(const PowerSnapshot& saved, const PowerSnapshot& current) {
        for (size_t i = 0; i < saved.pin_ids.size(); ++i) if (!selected_policy(saved, saved.pin_ids[i])) {
            const auto it = std::find(current.pin_ids.begin(), current.pin_ids.end(), saved.pin_ids[i]);
            if (it == current.pin_ids.end() || current.pin_values[static_cast<size_t>(it - current.pin_ids.begin())] != saved.pin_values[i]) return false;
        }
        for (auto id : current.pin_ids) if (!selected_policy(saved, id) && std::find(saved.pin_ids.begin(), saved.pin_ids.end(), id) == saved.pin_ids.end()) return false;
        return true;
    }
    static std::string advanced_binding(AdvancedId id, const dc_clkdom_layout& layout) {
        std::ostringstream out; out.imbue(std::locale::classic());
        out << "clock:" << static_cast<int>(id.kind) << ':' << id.domain << ':' << layout.version << ':' << layout.block_size << ':' << layout.header << ':' << layout.stride << ':' << layout.mask_dword << ':' << layout.mode << ':' << layout.freq_khz << ':' << layout.has_nvvdd_uv << ':' << layout.nvvdd_uv << ':' << layout.has_msvdd_uv << ':' << layout.msvdd_uv;
        return out.str();
    }
    static std::string rail_binding(uint32_t rail, uint32_t field, int32_t type, double base) {
        uint64_t bits = 0; static_assert(sizeof(bits) == sizeof(base), "double width"); std::memcpy(&bits, &base, sizeof(bits));
        std::ostringstream out; out.imbue(std::locale::classic()); out << "rail:" << rail << ':' << field << ':' << type << ':' << std::hex << bits; return out.str();
    }
    ActionResult execute_policy(const Command& cmd) {
        ActionResult result{}; if (!power_api() || !cmd.power_policy || *cmd.power_policy >= 32) { result.message = "Power policy interface or selection unavailable"; return result; }
        if (api_.lock(gpu_) != DC_OK) { result.message = "Could not lock GPU"; return result; }
        struct Unlock { CoreApi& api; dc_gpu* gpu; ~Unlock() { api.unlock(gpu); } } unlock{api_, gpu_};
        PowerSnapshot before; std::string why;
        if (!power_capture(before, why)) { result.message = "Power policy capture failed; no write issued: " + why; return result; }
        if (cmd.expected_power_state && *cmd.expected_power_state != power_state_token(before)) { result.message = "Power requests or pins changed during profile application; no write issued"; return result; }
        auto it = std::find_if(before.rows.begin(), before.rows.end(), [&](const dc_policy_row& r) { return r.policy == *cmd.power_policy; });
        if (it == before.rows.end() || it->board || !it->consistent || !(it->writable || it->named_current) || (it->unit != 0 && it->unit != 1)) { result.message = "Selected policy is unavailable or not writable"; return result; }
        if (!binding_matches(cmd, policy_binding(*it))) { result.message = "Policy binding changed; review the request before applying"; return result; }
        const double raw = cmd.operation == Command::Operation::stock ? static_cast<double>(it->default_value) : cmd.value * 1000.0;
        if (!std::isfinite(raw) || std::abs(raw - std::nearbyint(raw)) > 0.000001 || raw < it->minimum || raw > it->maximum) { result.message = "Policy request is outside its current range or precision"; return result; }
        const int64_t target = static_cast<int64_t>(std::nearbyint(raw)); result.requested = target / 1000.0;
        const auto old_pin = std::find(before.pin_ids.begin(), before.pin_ids.end(), *cmd.power_policy);
        const bool stock = cmd.operation == Command::Operation::stock;
        if (it->requested == target && ((stock && old_pin == before.pin_ids.end()) || (!stock && old_pin != before.pin_ids.end() && before.pin_values[static_cast<size_t>(old_pin - before.pin_ids.begin())] == target))) { result.ok = true; result.readback = target / 1000.0; result.message = "Policy request and pin already match"; return result; }
        Undo saved{}; saved.control = ControlId::power; saved.power = before; saved.power.selected_policy = *cmd.power_policy; remember(saved);
        if (stock && old_pin != before.pin_ids.end()) {
            const size_t pos = static_cast<size_t>(old_pin - before.pin_ids.begin());
            auto ids = before.pin_ids; auto values = before.pin_values; ids.erase(ids.begin() + pos); values.erase(values.begin() + pos);
            if (api_.pins_set(gpu_, ids.data(), values.data(), static_cast<uint32_t>(ids.size())) != DC_OK) { result.message = "Could not remove selected policy pin; Undo retained"; return result; }
            result.may_have_changed = true;
        }
        dc_policy_row desired = *it; desired.requested = target; std::string message; const bool wrote = set_policy(desired, message, !stock); result.may_have_changed = true;
        PowerSnapshot after;
        if (!power_capture(after, why) || !power_geometry(before, after)) { result.message = "Policy readback or geometry unavailable; Undo retained"; return result; }
        auto selected = std::find_if(after.rows.begin(), after.rows.end(), [&](const dc_policy_row& r) { return r.policy == *cmd.power_policy; });
        result.readback = selected->requested / 1000.0;
        bool pins_same = true;
        for (size_t i = 0; i < before.pin_ids.size(); ++i) if (before.pin_ids[i] != *cmd.power_policy) { auto p = std::find(after.pin_ids.begin(), after.pin_ids.end(), before.pin_ids[i]); if (p == after.pin_ids.end() || after.pin_values[static_cast<size_t>(p - after.pin_ids.begin())] != before.pin_values[i]) pins_same = false; }
        for (auto id : after.pin_ids) if (id != *cmd.power_policy && std::find(before.pin_ids.begin(), before.pin_ids.end(), id) == before.pin_ids.end()) pins_same = false;
        auto selected_pin = std::find(after.pin_ids.begin(), after.pin_ids.end(), *cmd.power_policy);
        if (stock ? selected_pin != after.pin_ids.end() : (selected_pin == after.pin_ids.end() || after.pin_values[static_cast<size_t>(selected_pin - after.pin_ids.begin())] != target)) pins_same = false;
        bool peers_changed = after.board != before.board;
        for (size_t i = 0; i < before.rows.size(); ++i) if (before.rows[i].policy != *cmd.power_policy && before.rows[i].requested != after.rows[i].requested) peers_changed = true;
        result.ok = wrote && selected->requested == target && pins_same;
        result.message = message.empty() ? (result.ok ? "Policy request applied" : "Policy request failed") : message;
        if (selected->requested != target) result.message += "; selected request readback differs";
        if (!pins_same) result.message += "; policy pins changed; Undo retained";
        if (peers_changed) result.message += "; coupled requests also changed; Undo can restore the captured state";
        return result;
    }
    ActionResult execute_power(const Command& cmd, bool undoing) {
        ActionResult result{}; if (!power_base_api()) { result.message = "Power control interfaces unavailable"; return result; }
        if (api_.lock(gpu_) != DC_OK) { result.message = "Could not lock GPU"; return result; }
        struct Unlock { CoreApi& api; dc_gpu* gpu; ~Unlock() { api.unlock(gpu); } } unlock{api_, gpu_};
        PowerSnapshot current; std::string why; const bool captured = power_capture(current, why);
        if (undoing) {
            const PowerSnapshot saved = undo_.back().power;
            if (!captured || !power_geometry(saved, current)) { result.message = "Power Undo refused: " + (captured ? std::string("policy geometry changed") : why); return result; }
            if (saved.guard_unselected_pins && !same_unselected_pins(saved, current)) { result.message = "Power Undo refused: an unselected policy pin changed; current pins preserved"; return result; }
            if (api_.pins_set(gpu_, nullptr, nullptr, 0) != DC_OK) { result.message = "Could not suppress policy pins; no power write issued"; return result; }
            bool ok = true; std::string message;
            if (current.board != saved.board) { result.may_have_changed = true; if (!set_board(saved.board, message)) ok = false; }
            for (const auto& row : saved.rows) {
                PowerSnapshot fresh; if (!power_capture(fresh, why) || !power_geometry(saved, fresh)) { ok = false; message += "; policy geometry or readback changed"; break; }
                const auto it = std::find_if(fresh.rows.begin(), fresh.rows.end(), [&](const dc_policy_row& r) { return r.policy == row.policy; });
                const bool pinned = std::find(saved.pin_ids.begin(), saved.pin_ids.end(), row.policy) != saved.pin_ids.end();
                const bool selected = (saved.selected_policy && *saved.selected_policy == row.policy) || std::find(saved.selected_policies.begin(), saved.selected_policies.end(), row.policy) != saved.selected_policies.end();
                if (row.board || (!pinned && !selected) || it->requested == row.requested) continue;
                if (!(it->writable || it->named_current) || !it->consistent || row.requested < it->minimum || row.requested > it->maximum) { ok = false; message += "; selected policy no longer restorable in fresh bounds"; continue; }
                result.may_have_changed = true; std::string note; if (!set_policy(row, note)) { ok = false; message += "; " + note; }
            }
            PowerSnapshot final; bool verified = power_capture(final, why) && power_geometry(saved, final);
            std::vector<uint32_t> verified_ids; std::vector<int64_t> verified_values;
            if (verified) {
                if (final.board != saved.board) ok = false;
                for (size_t i = 0; i < saved.rows.size(); ++i) { const auto& row = saved.rows[i]; const bool pinned = std::find(saved.pin_ids.begin(), saved.pin_ids.end(), row.policy) != saved.pin_ids.end(); const bool selected = (saved.selected_policy && *saved.selected_policy == row.policy) || std::find(saved.selected_policies.begin(), saved.selected_policies.end(), row.policy) != saved.selected_policies.end(); if (!row.board && (pinned || selected) && row.requested != final.rows[i].requested) ok = false; }
                for (size_t i = 0; i < saved.pin_ids.size(); ++i) for (size_t j = 0; j < saved.rows.size(); ++j) if (saved.rows[j].policy == saved.pin_ids[i] && final.rows[j].requested == saved.rows[j].requested) { verified_ids.push_back(saved.pin_ids[i]); verified_values.push_back(saved.pin_values[i]); }
                if (api_.pins_set(gpu_, verified_ids.data(), verified_values.data(), static_cast<uint32_t>(verified_ids.size())) != DC_OK || verified_ids.size() != saved.pin_ids.size()) ok = false;
                if (saved.guard_unselected_pins) {
                    PowerSnapshot restored; PowerSnapshot expected_pins = saved;
                    expected_pins.selected_policy.reset(); expected_pins.selected_policies.clear();
                    if (!power_capture(restored, why) || !same_unselected_pins(expected_pins, restored)) ok = false;
                }
            } else ok = false;
            result.ok = ok; result.message = ok ? "Board power and captured explicit policy requests restored; unpinned peers left to the driver" : "Power restoration incomplete or unverified; Undo retained" + message;
            if (ok) undo_.pop_back(); return result;
        }
        const bool stock = cmd.operation == Command::Operation::stock;
        if (!captured && !stock) { result.message = "Power capture failed; no write issued: " + why; return result; }
        if (cmd.expected_power_state && (!captured || *cmd.expected_power_state != power_state_token(current))) { result.message = "Power requests or pins changed during profile application; no write issued"; return result; }
        if (cmd.expected_binding && (!captured || !binding_matches(cmd, board_binding(current)))) { result.message = "Board policy geometry changed; no write issued"; return result; }
        dc_cl_statics st = power_statics(); int64_t low = 0, high = 0; dc_error error{}; error.size = sizeof(error);
        bool xoc = false; if (!effective_xoc(xoc) || api_.power_bounds(gpu_, &st, xoc ? 1 : 0, &low, &high, &error) != DC_OK) { result.message = "Power bounds or XOC state unavailable; no write issued"; return result; }
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

    static bool advanced_equal(const AdvancedId& a, const AdvancedId& b) { return a.kind == b.kind && a.domain == b.domain; }
    static bool layout_equal(const dc_clkdom_layout& a, const dc_clkdom_layout& b) {
        return a.version == b.version && a.block_size == b.block_size && a.header == b.header && a.stride == b.stride && a.mask_dword == b.mask_dword && a.mode == b.mode && a.freq_khz == b.freq_khz && a.has_nvvdd_uv == b.has_nvvdd_uv && a.nvvdd_uv == b.nvvdd_uv && a.has_msvdd_uv == b.has_msvdd_uv && a.msvdd_uv == b.msvdd_uv;
    }
    bool advanced_capture(AdvancedId which, Undo& capture, std::string& why) {
        if ((which.kind != AdvancedKind::clock_domain && which.kind != AdvancedKind::nvvdd && which.kind != AdvancedKind::msvdd) || which.domain >= DC_CLKDOM_SLOTS || !api_.clkdom_ok || !api_.clkdom_layout || !api_.clkdom_domains || !api_.clkdom_ok(gpu_)) { why = "Clock control capability unavailable"; return false; }
        dc_clkdom_layout layout{}; layout.size = sizeof(layout);
        if (retry(api_.clkdom_layout, gpu_, &layout) != DC_OK) { why = "Clock control layout unavailable"; return false; }
        uint32_t domains[DC_CLKDOM_SLOTS]{}; size_t count = 0;
        if (retry(api_.clkdom_domains, gpu_, domains, static_cast<size_t>(DC_CLKDOM_SLOTS), &count) != DC_OK || count > DC_CLKDOM_SLOTS || std::find(domains, domains + count, which.domain) == domains + count) { why = "Selected control is unavailable"; return false; }
        int64_t raw = 0;
        if (which.kind == AdvancedKind::clock_domain) {
            if (!api_.clkdom_requests || !api_.clkdom_set || !api_.clkdom_controls || !api_.clkdom_offered) { why = "Clock offset interface unavailable"; return false; }
            uint32_t controls[DC_CLKDOM_SLOTS]{}; size_t n = 0;
            if (retry(api_.clkdom_controls, gpu_, controls, static_cast<size_t>(DC_CLKDOM_SLOTS), &n) != DC_OK || n > DC_CLKDOM_SLOTS || std::find(controls, controls + n, which.domain) == controls + n || !api_.clkdom_offered(gpu_, which.domain)) { why = "Clock offset is not offered"; return false; }
            dc_clkdom_requests requests{}; requests.size = sizeof(requests);
            if (retry(api_.clkdom_requests, gpu_, &requests) != DC_OK || requests.count > DC_CLKDOM_SLOTS) { why = "Clock offset request unreadable"; return false; }
            auto row = std::find_if(requests.rows, requests.rows + requests.count, [&](const dc_clkdom_request& r) { return r.domain == which.domain; });
            if (row == requests.rows + requests.count || std::count_if(requests.rows, requests.rows + requests.count, [&](const dc_clkdom_request& r) { return r.domain == which.domain; }) != 1 || row->freq_khz % 1000) { why = "Clock offset request is absent or not exactly restorable"; return false; }
            raw = row->freq_khz;
        } else {
            if (!api_.rail_cap || !api_.rail_set) { why = "Rail offset interface unavailable"; return false; }
            const int64_t rail = which.kind == AdvancedKind::nvvdd ? 0 : 1; dc_rail_offset_capability cap{}; cap.size = sizeof(cap);
            if (retry(api_.rail_cap, gpu_, rail, 1, static_cast<int64_t>(which.domain), &cap) != DC_OK || !cap.available || !cap.has_domain || cap.domain != which.domain || !cap.has_value) { why = "Selected rail request capability unavailable"; return false; }
            double mv = cap.value_mv; if (!std::isfinite(mv) || mv * 1000 < INT32_MIN || mv * 1000 > INT32_MAX || std::abs(mv * 1000 - std::nearbyint(mv * 1000)) > 0.000001) { why = "Rail request unreadable or not exactly restorable"; return false; }
            raw = static_cast<int64_t>(std::nearbyint(mv * 1000));
        }
        capture = {}; capture.control = ControlId::count; capture.advanced_entry = true; capture.advanced = which; capture.advanced_layout = layout; capture.value = static_cast<int32_t>(raw); return true;
    }
    bool advanced_preflight(AdvancedId id, int32_t target, Undo& before, std::string& why, bool allow_stock_escape = false) {
        if (!advanced_capture(id, before, why)) return false;
        bool xoc = false; const bool known = current_xoc(xoc);
        if (id.kind == AdvancedKind::clock_domain) {
            if (target % 1000) { why = "Clock offset must be whole MHz"; return false; }
            if (id.domain == 2 && (!known || !xoc) && !(allow_stock_escape && target == 0)) { why = "Additional memory offset requires XOC session mode"; return false; }
            return true;
        }
        if (id.kind == AdvancedKind::msvdd && allow_stock_escape && target == 0) return api_.msvdd_reset != nullptr;
        if (!known) { why = "XOC session state unreadable"; return false; }
        if (id.kind == AdvancedKind::msvdd && !xoc) { why = "MSVDD offset requires XOC session mode"; return false; }
        dc_request_envelopes env{}; env.size = sizeof(env);
        if (!api_.envelopes || api_.envelopes(&env) != DC_OK) { why = "Rail request envelope unavailable"; return false; }
        double floor_mv = xoc ? -500.0 : -100.0;
        double ceiling_mv = (xoc || id.kind == AdvancedKind::msvdd) ? env.rail_offset_xoc_max_mv : env.rail_offset_max_mv;
        if (!std::isfinite(ceiling_mv) || ceiling_mv < 0 || ceiling_mv * 1000.0 > INT32_MAX) { why = "Rail request envelope invalid"; return false; }
        double floor_uv = floor_mv * 1000.0, ceiling_uv = ceiling_mv * 1000.0;
        if (target < std::min(floor_uv, static_cast<double>(before.value)) || target > std::max(ceiling_uv, static_cast<double>(before.value)) || (!(allow_stock_escape && target == 0) && (before.value < std::min(floor_uv, static_cast<double>(target)) || before.value > std::max(ceiling_uv, static_cast<double>(target))))) { why = "Rail request or its Undo is outside the current envelope"; return false; }
        return true;
    }
    void read_advanced(bool details) {
        auto& rows = last_.advanced_controls;
        auto refresh = [&](AdvancedId id, const std::string& label) {
            auto it = std::find_if(rows.begin(), rows.end(), [&](const AdvancedControl& row) { return advanced_equal(row.id, id); });
            Undo capture{}; std::string why;
            if (!advanced_capture(id, capture, why)) { if (it != rows.end()) { it->state.availability = Availability::transient; it->state.detail = why;
                bool xoc = false; if (current_xoc(xoc) && !xoc) { if (id.kind == AdvancedKind::clock_domain && id.domain == 2) { it->state.writable = false; it->state.stock_writable = api_.clkdom_set && api_.lock && api_.unlock; } if (id.kind == AdvancedKind::msvdd) { it->state.writable = false; it->state.stock_writable = api_.msvdd_reset && api_.lock && api_.unlock; } }
            } return; }
            if (it == rows.end()) { rows.push_back({}); it = rows.end() - 1; it->id = id; it->label = label; }
            it->binding = advanced_binding(id, capture.advanced_layout);
            auto& state = it->state; const bool previous_writable = state.writable, previous_stock = state.stock_writable; state.availability = Availability::available; state.current = capture.value / 1000.0; state.stock = 0; state.unit = id.kind == AdvancedKind::clock_domain ? "MHz" : "mV"; state.step = id.kind == AdvancedKind::clock_domain ? 1.0 : 0.001;
            state.writable = api_.lock && api_.unlock; state.stock_writable = state.writable; state.detail = id.kind == AdvancedKind::clock_domain ? "Requested offset; physical response may differ" : "Stored request; physical voltage response is unverified";
            if (id.kind == AdvancedKind::clock_domain && id.domain == 2) { bool xoc = false; if (!current_xoc(xoc)) { state.availability = Availability::transient; state.writable = previous_writable; state.stock_writable = previous_stock; state.detail = "XOC session state unreadable"; } else if (!xoc) { state.writable = false; state.stock_writable = true; state.detail = "Additional memory offset requires XOC session mode; Stock remains available"; } }
            if (id.kind == AdvancedKind::nvvdd && (!api_.envelopes || !api_.clkdom_flags)) { state.writable = false; state.stock_writable = false; state.detail = "Rail request envelope or XOC state unavailable"; }
            if (id.kind != AdvancedKind::clock_domain) { bool xoc = false; dc_request_envelopes env{}; env.size = sizeof(env);
                if (current_xoc(xoc) && api_.envelopes && api_.envelopes(&env) == DC_OK) { const double low = xoc || id.kind == AdvancedKind::msvdd ? -500.0 : -100.0; const double high = xoc || id.kind == AdvancedKind::msvdd ? env.rail_offset_xoc_max_mv : env.rail_offset_max_mv; if (std::isfinite(high) && high >= 0) { state.low = std::min(low, *state.current); state.high = std::max(high, *state.current); } }
            }
            if (id.kind == AdvancedKind::msvdd) { int32_t enabled = 0, xoc = 0; if (!api_.clkdom_flags || retry(api_.clkdom_flags, gpu_, &enabled, &xoc) != DC_OK) { state.availability = Availability::transient; state.writable = previous_writable; state.stock_writable = previous_stock; state.detail = "MSVDD opt-in state unreadable"; } else { state.writable = enabled != 0 && api_.lock && api_.unlock && api_.envelopes; state.stock_writable = api_.msvdd_reset && api_.lock && api_.unlock; state.detail = enabled ? "Experimental stored request; physical response unverified" : "Experimental MSVDD opt-in is disabled"; } }
            if (id.kind == AdvancedKind::clock_domain) {
                // These are Python frontend editing/typo ranges, not measured
                // hardware limits; setters still validate the actual request.
                const bool xoc = last_.xoc_known && last_.xoc_enabled;
                double lo = -300.0, hi = 300.0;
                const auto& core = last_.controls[index(ControlId::core)];
                const auto& memory = last_.controls[index(ControlId::memory)];
                if (id.domain == 2) {
                    double mem_lo = -1000.0, mem_hi = 3000.0;
                    if (memory.low && memory.high && std::isfinite(*memory.low) && std::isfinite(*memory.high) && *memory.low <= *memory.high) { mem_lo = std::trunc(*memory.low); mem_hi = std::trunc(*memory.high); }
                    if (!xoc) { lo = mem_lo; hi = mem_hi; }
                    else {
                        uint32_t clock = 0;
                        if ((last_.telemetry.present & DC_T_MEM_P0MAX) && last_.telemetry.mem_p0max) clock = last_.telemetry.mem_p0max;
                        else if (last_.telemetry.present & DC_T_MEM) clock = last_.telemetry.mem;
                        const double typo = std::max({std::abs(mem_hi), static_cast<double>(clock / 4), 1000.0});
                        lo = -typo; hi = typo;
                    }
                } else if (xoc) {
                    lo = -1000.0; hi = 1000.0;
                    if (core.low && core.high && std::isfinite(*core.low) && std::isfinite(*core.high) && *core.low <= *core.high) { lo = *core.low; hi = *core.high; }
                }
                state.low = std::min(lo, *state.current); state.high = std::max(hi, *state.current);
                state.detail += "; slider range is a software editing range, not a hardware limit";
            }
        };
        if (!details) return;
        { auto old = rows; for (const auto& row : old) refresh(row.id, row.label); }
        if (details && api_.clkdom_domains && api_.clkdom_controls && api_.clkdom_offered) {
            uint32_t controls[DC_CLKDOM_SLOTS]{}; size_t n = 0;
            if (retry(api_.clkdom_controls, gpu_, controls, static_cast<size_t>(DC_CLKDOM_SLOTS), &n) == DC_OK && n <= DC_CLKDOM_SLOTS) for (size_t i = 0; i < n; ++i) {
                if (controls[i] >= DC_CLKDOM_SLOTS || !api_.clkdom_offered(gpu_, controls[i])) continue;
                char label[128]{}; size_t needed = 0; if (!api_.clkdom_label || api_.clkdom_label(gpu_, controls[i], label, sizeof(label), &needed) != DC_OK || needed > sizeof(label) || !label[0]) std::memcpy(label, "Clock domain", sizeof("Clock domain"));
                refresh({AdvancedKind::clock_domain, controls[i]}, controls[i] == 2 ? "Additional memory offset" : label);
            }
        }
        if (details && api_.rail_cap) {
            dc_rail_offset_capability cap{}; cap.size = sizeof(cap);
            if (retry(api_.rail_cap, gpu_, int64_t{0}, 1, int64_t{0}, &cap) == DC_OK && cap.available && cap.has_domain && cap.domain == 0) refresh({AdvancedKind::nvvdd, 0}, "NVVDD offset");
            cap = {}; cap.size = sizeof(cap);
            if (retry(api_.rail_cap, gpu_, int64_t{1}, 0, int64_t{0}, &cap) == DC_OK && cap.available && cap.has_domain && cap.domain >= 0 && cap.domain < DC_CLKDOM_SLOTS) refresh({AdvancedKind::msvdd, static_cast<uint32_t>(cap.domain)}, "MSVDD offset");
        }
    }
    ActionResult advanced_action(const Command& cmd, bool undoing) {
        ActionResult result{}; AdvancedId id = undoing ? undo_.back().advanced : *cmd.advanced;
        if (!api_.lock || !api_.unlock || api_.lock(gpu_) != DC_OK) { result.message = "Could not lock GPU for offset action"; return result; }
        struct Unlock { CoreApi& api; dc_gpu* gpu; ~Unlock() { api.unlock(gpu); } } unlock{api_, gpu_};
        dc_gpu_identity identity_now{}; Undo before{}; std::string why;
        if (!identity(identity_now) || !advanced_capture(id, before, why)) { result.message = "Offset capture failed; no write issued: " + why; return result; }
        if (!undoing && !binding_matches(cmd, advanced_binding(id, before.advanced_layout))) { result.message = "Offset binding changed; review the request before applying"; return result; }
        if (undoing && !layout_equal(before.advanced_layout, undo_.back().advanced_layout)) { result.message = "Offset layout changed; Undo retained"; return result; }
        double value = undoing ? undo_.back().value / 1000.0 : (cmd.operation == Command::Operation::stock ? 0.0 : cmd.value);
        const double raw = value * 1000.0;
        if (!std::isfinite(raw) || raw < INT32_MIN || raw > INT32_MAX || std::abs(raw - std::nearbyint(raw)) > 0.000001 || (id.kind == AdvancedKind::clock_domain && std::trunc(value) != value)) { result.message = "Offset is not exactly representable; no write issued"; return result; }
        Undo preflight{}; const bool stock = !undoing && cmd.operation == Command::Operation::stock;
        const bool strict = advanced_preflight(id, static_cast<int32_t>(std::nearbyint(raw)), preflight, why);
        if (!strict && (!stock || !advanced_preflight(id, static_cast<int32_t>(std::nearbyint(raw)), preflight, why, true))) { result.message = "Offset preflight failed; no write issued: " + why; return result; }
        if (!layout_equal(before.advanced_layout, preflight.advanced_layout) || before.value != preflight.value) { result.message = "Offset changed during preflight; no write issued"; return result; }
        result.requested = value;
        if (before.value == std::nearbyint(raw)) { result.ok = true; result.readback = value; result.message = "Offset already matches"; if (undoing) undo_.pop_back(); return result; }
        const bool undo_allowed = !stock || strict;
        if (!undoing && undo_allowed) remember(before);
        dc_outcome out{}; dc_status status;
        if (id.kind == AdvancedKind::clock_domain) status = api_.clkdom_set(gpu_, id.domain, value, &out);
        else if (id.kind == AdvancedKind::msvdd && !undoing && cmd.operation == Command::Operation::stock && api_.msvdd_reset) status = api_.msvdd_reset(gpu_, id.domain, &out);
        else status = api_.rail_set(gpu_, value, id.domain, id.kind == AdvancedKind::nvvdd ? 0 : 1, &out);
        result.may_have_changed = true; Undo after{};
        if (!identity(identity_now) || !advanced_capture(id, after, why) || !layout_equal(before.advanced_layout, after.advanced_layout)) { result.message = "Offset readback or geometry unavailable; Undo retained"; return result; }
        result.readback = after.value / 1000.0; result.ok = status == DC_OK && out.ok && after.value == std::nearbyint(raw);
        result.message = out.message[0] ? out.message : (result.ok ? "Offset applied" : "Offset write failed"); if (after.value != std::nearbyint(raw)) result.message += "; request readback differs";
        if (!undo_allowed) result.message += "; prior request cannot be undone under the current session bounds";
        if (undoing && result.ok) undo_.pop_back(); return result;
    }

    void read_rails() {
        if (!rail_service_) return;
        auto rows = rail_service_->read(); dc_rails_hold_record hold{}; hold.size = sizeof(hold);
        bool hold_known = false;
        if (api_.rails.hold) for (int i = 0; i < 2 && !hold_known; ++i) hold_known = api_.rails.hold(gpu_, &hold) == DC_OK && (!hold.present || (hold.rail >= 0 && hold.rail < static_cast<int>(DC_RAILS_COUNT)));
        const bool writer_available = api_.rails.set_raw && api_.rails.lock && api_.rails.unlock && api_.rails.identity && api_.rails.limits && api_.rails.fields && api_.rails.hold;
        bool any_writable = false; bool rail_established[DC_RAILS_COUNT]{};
        std::vector<RailControl> updated;
        for (uint32_t rail = 0; rail < DC_RAILS_COUNT; ++rail) for (uint32_t field = 0; field < DC_RAILS_LIMIT_FIELDS; ++field) {
            const auto& f = rows[rail].fields[field]; if (!f.established || !f.requested_uv || !rows[rail].type) continue;
            RailControl c{}; c.id = {rail, field}; if (f.estimated_base_mv) c.binding = rail_binding(rail, field, *rows[rail].type, *f.estimated_base_mv); c.state.availability = f.stale ? Availability::transient : Availability::available; c.state.current = *f.requested_uv / 1000.0; c.state.unit = "mV"; c.state.step = 0.001;
            if (f.initial_uv) c.state.stock = *f.initial_uv / 1000.0; c.estimated_base_mv = f.estimated_base_mv;
            if (f.estimated_base_mv && last_.xoc_known) {
                c.state.low = std::min(DC_RAILS_LIMIT_MIN_MV - *f.estimated_base_mv, *c.state.current);
                c.state.high = std::max((last_.xoc_enabled ? DC_RAILS_LIMIT_XOC_MAX_MV : DC_RAILS_LIMIT_MAX_MV) - *f.estimated_base_mv, *c.state.current);
            }
            const bool held = hold_known && hold.present && hold.rail == static_cast<int>(rail);
            rail_established[rail] = true;
            if (hold_known && writer_available && !held) any_writable = true;
            c.state.writable = rail_mode_ && writer_available && hold_known && !held;
            c.state.stock_writable = api_.rails.reset && f.initial_uv && hold_known && !held;
            c.state.detail = f.stale ? "Last request retained; fresh read unavailable" : held ? "A hold owns this rail" : "Stored request; initial value is captured, base voltage is estimated";
            c.state.detail += "; slider bounds are software request limits, not measured voltage limits";
            if (f.stale || !hold_known) { auto old = std::find_if(last_.rail_controls.begin(), last_.rail_controls.end(), [&](const RailControl& p) { return p.id.rail == rail && p.id.field == field; }); if (old != last_.rail_controls.end()) { c.state.writable = rail_mode_ && !held && old->state.writable; c.state.stock_writable = !held && old->state.stock_writable; } }
            updated.push_back(std::move(c));
        }
        last_.rail_controls = std::move(updated);
        if (!writer_available) { last_.rail_writable_known = true; last_.rail_writable = false; }
        else if (hold_known && any_writable) { last_.rail_writable_known = true; last_.rail_writable = true; }
        else if (hold_known) {
            bool all_established = true; for (bool established : rail_established) all_established = all_established && established;
            last_.rail_writable_known = all_established; if (all_established) last_.rail_writable = false;
        }

    }
    ActionResult rail_action(const Command& cmd, bool undoing) {
        ActionResult result{}; if (!rail_service_) { result.message = "Rail controls unavailable"; return result; }
        if (!undoing && (!cmd.rail || cmd.rail->rail >= DC_RAILS_COUNT || cmd.rail->field >= DC_RAILS_LIMIT_FIELDS)) { result.message = "Invalid rail or field"; return result; }
        bool xoc = false; if ((undoing || cmd.operation != Command::Operation::stock) && !effective_xoc(xoc)) { result.message = "XOC session state unreadable; no rail write issued"; return result; }
        if (!api_.rails.lock || !api_.rails.unlock || api_.rails.lock(gpu_) != DC_OK) { result.message = "Could not lock GPU for rail action"; return result; }
        struct UnlockRail { RailApi& api; dc_gpu* gpu; ~UnlockRail() { api.unlock(gpu); } } unlock{api_.rails, gpu_};
        const bool bound = !undoing && cmd.expected_binding && !cmd.expected_binding->empty();
        auto binding_now = [&]() -> std::optional<std::string> {
            auto rows = rail_service_->read(); const auto& row = rows[cmd.rail->rail]; const auto& field = row.fields[cmd.rail->field];
            if (!row.type || !field.established || field.stale || !field.estimated_base_mv) return std::nullopt;
            return rail_binding(cmd.rail->rail, cmd.rail->field, *row.type, *field.estimated_base_mv);
        };
        if (bound) { auto current = binding_now(); if (!current || !binding_matches(cmd, *current)) { result.message = "Rail binding changed or unavailable; review the request before applying"; return result; } }
        struct BindingChanged {};
        RailResult action; bool recorded = false;
        auto capture = [&](const RailUndo& before) {
            if (bound) { auto current = binding_now(); if (!current || !binding_matches(cmd, *current)) throw BindingChanged{}; }
            if (cmd.expected_request_raw && before.requested_uv != *cmd.expected_request_raw) throw BindingChanged{};
            Undo saved{}; saved.control = ControlId::count; saved.rail_entry = true; saved.rail = before; remember(saved); recorded = !suppress_undo_;
        };
        try {
            if (undoing) action = rail_service_->restore(undo_.back().rail, rail_mode_, xoc);
            else if (cmd.operation == Command::Operation::stock) action = rail_service_->stock(static_cast<int>(cmd.rail->rail), cmd.rail->field, capture);
            else { double raw = cmd.value * 1000.0; if (!std::isfinite(raw) || raw < INT32_MIN || raw > INT32_MAX || std::abs(raw - std::nearbyint(raw)) > 0.000001) { result.message = "Rail request is not exactly representable in microvolts"; return result; } action = rail_service_->apply(static_cast<int>(cmd.rail->rail), cmd.rail->field, static_cast<int32_t>(std::nearbyint(raw)), rail_mode_, xoc, capture); }
        } catch (const BindingChanged&) { result.message = "Rail binding or request changed during preflight; no write issued"; return result; }
        result.ok = action.ok; result.may_have_changed = action.may_have_changed; result.message = action.message;
        if (undoing) { if (action.ok) undo_.pop_back(); }
        else if (recorded) { if (!action.may_have_changed) undo_.pop_back(); else if (!rail_mode_) result.message += "; enable rail mode to use Undo"; }
        return result;
    }

    bool curve_table(dc_vf_delta_snapshot& snap, std::vector<dc_vf_point>& points, std::string& why, dc_vf_layout* captured_layout = nullptr) {
        if (!api_.curve_layout || !api_.curve || !api_.snapshot) { why = "Curve interfaces unavailable"; return false; }
        dc_vf_layout layout{}; layout.size = sizeof(layout); dc_vf_curve curve{}; curve.size = sizeof(curve);
        if (retry(api_.curve_layout, gpu_, 1, &layout) != DC_OK || !layout.n_entries || layout.n_entries > DC_VFP_POINTS || !layout.gpu_count || layout.gpu_count > layout.n_entries || layout.other_count > layout.n_entries || layout.freq_div == 0 || retry(api_.curve, gpu_, &curve) != DC_OK || curve.count != layout.gpu_count || !capture_curve(snap) || snap.count != layout.gpu_count || snap.freq_div != layout.freq_div) { why = "Complete V/F table unavailable"; return false; }
        for (uint32_t i = 0; i < layout.other_count; ++i) { if (layout.other_idx[i] >= layout.n_entries) { why = "Invalid trailing V/F geometry"; return false; } for (uint32_t j = 0; j < i; ++j) if (layout.other_idx[j] == layout.other_idx[i]) { why = "Duplicate trailing V/F geometry"; return false; } }
        for (uint32_t i = 0; i < layout.gpu_count; ++i) {
            const uint32_t idx = layout.gpu_idx[i]; bool point = false, delta = false;
            if (idx >= layout.n_entries || std::find(layout.other_idx, layout.other_idx + layout.other_count, idx) != layout.other_idx + layout.other_count) { why = "Invalid GPU V/F geometry"; return false; }
            for (uint32_t j = 0; j < i; ++j) if (layout.gpu_idx[j] == idx) { why = "Duplicate V/F geometry"; return false; }
            for (uint32_t j = 0; j < curve.count; ++j) if (curve.points[j].idx == idx) { if (point || !std::isfinite(curve.points[j].volt_mv) || !std::isfinite(curve.points[j].freq_mhz)) { why = "Invalid V/F point"; return false; } point = true; }
            for (uint32_t j = 0; j < snap.count; ++j) if (snap.deltas[j].idx == idx) delta = true;
            if (!point || !delta) { why = "Incomplete V/F point geometry"; return false; }
        }
        for (uint32_t i = 0; i < curve.count; ++i) { auto it = std::find_if(snap.deltas, snap.deltas + snap.count, [&](const dc_vf_delta& d) { return d.idx == curve.points[i].idx; }); if (it == snap.deltas + snap.count || it->delta_khz != curve.points[i].delta_khz) { why = "V/F reads disagree"; return false; } }
        points.assign(curve.points, curve.points + curve.count); if (captured_layout) *captured_layout = layout; return true;
    }
    static bool same_curve_layout(const dc_vf_layout& a, const dc_vf_layout& b) {
        if (!a.n_entries || a.n_entries != b.n_entries || a.gpu_count != b.gpu_count || a.other_count != b.other_count || a.freq_div != b.freq_div) return false;
        return std::equal(a.gpu_idx, a.gpu_idx + a.gpu_count, b.gpu_idx) && std::equal(a.other_idx, a.other_idx + a.other_count, b.other_idx);
    }
    bool same_curve_geometry(const dc_vf_delta_snapshot& a, const dc_vf_delta_snapshot& b) {
        if (a.freq_div != b.freq_div || a.count != b.count) return false;
        for (uint32_t i = 0; i < a.count; ++i) if (std::find_if(b.deltas, b.deltas + b.count, [&](const dc_vf_delta& d) { return d.idx == a.deltas[i].idx; }) == b.deltas + b.count) return false;
        return true;
    }
    bool same_curve_mapping(const std::vector<dc_vf_point>& saved, const std::vector<dc_vf_point>& now, uint32_t div) {
        if (!div || saved.size() != now.size()) return false;
        // Evaluated curve frequencies include driver propagation and are not
        // the per-row base plus that row's boost-table request.
        for (const auto& p : saved) {
            auto it = std::find_if(now.begin(), now.end(), [&](const dc_vf_point& q) { return q.idx == p.idx; });
            if (it == now.end() || !std::isfinite(p.volt_mv) || !std::isfinite(p.freq_mhz) || !std::isfinite(it->volt_mv) || !std::isfinite(it->freq_mhz) || p.volt_mv <= 0 || p.freq_mhz <= 0 || it->freq_mhz <= 0 || p.volt_mv != it->volt_mv) return false;
        }
        return true;
    }
    ActionResult curve_action(const Command& cmd, bool undoing) {
        ActionResult result{}; const bool reset = cmd.operation == Command::Operation::reset_curve;
        if (!api_.lock || !api_.unlock || !api_.apply_deltas || (!undoing && reset && !api_.reset_curve) || (!undoing && !reset && !api_.envelopes)) { result.message = "V/F write interfaces unavailable"; return result; }
        if (api_.lock(gpu_) != DC_OK) { result.message = "Could not lock GPU for V/F write"; return result; }
        struct Unlock { CoreApi& api; dc_gpu* gpu; ~Unlock() { api.unlock(gpu); } } unlock{api_, gpu_};
        dc_gpu_identity id{}; dc_vf_delta_snapshot before{}; dc_vf_layout before_layout{}; std::vector<dc_vf_point> points; std::string why;
        if (!identity(id) || !curve_table(before, points, why, &before_layout)) { result.message = "V/F read failed or identity changed: " + why; return result; }
        dc_vf_delta_snapshot desired = before;
        if (undoing) {
            desired = undo_.back().curve;
            if (!same_curve_geometry(before, desired) || !same_curve_layout(undo_.back().curve_layout, before_layout) || !same_curve_mapping(undo_.back().curve_points, points, desired.freq_div)) { result.message = "V/F geometry changed; Undo retained"; return result; }
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
        if (!undoing) { Undo saved{}; saved.control = ControlId::count; saved.curve_entry = true; saved.curve = before; saved.curve_layout = before_layout; saved.curve_points = points; remember(saved); }
        const auto before_points = points; dc_outcome out{}; dc_status status;
        if (undoing) status = api_.apply_deltas(gpu_, desired.deltas, desired.count, &out);
        else if (reset) status = api_.reset_curve(gpu_, &out);
        else status = api_.apply_deltas(gpu_, cmd.curve_deltas.data(), cmd.curve_deltas.size(), &out);
        result.may_have_changed = true; dc_vf_delta_snapshot after{}; dc_vf_layout after_layout{}; points.clear();
        if (!identity(id) || !curve_table(after, points, why, &after_layout)) { result.message = "V/F readback unavailable; Undo retained"; return result; }
        result.ok = status == DC_OK && out.ok && same_curve(after, desired) && same_curve_layout(before_layout, after_layout) && same_curve_mapping(before_points, points, before.freq_div);
        result.message = out.message[0] ? out.message : (result.ok ? "V/F curve applied" : "V/F write failed");
        if (!same_curve(after, desired) || !same_curve_layout(before_layout, after_layout) || !same_curve_mapping(before_points, points, before.freq_div)) result.message += "; point readback or geometry differs; Undo retained";
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
            else if (!api_.snapshot || !api_.apply_deltas || !api_.clock_step || retry(api_.clock_step, gpu_, &grid, &measured) != DC_OK || !grid || !curve_table(item.undo.curve, item.undo.curve_points, why, &item.undo.curve_layout)) { why = "core curve or grid unavailable"; return false; }
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
            bool xoc = false; if (!effective_xoc(xoc) || api_.power_bounds(gpu_, &st, xoc ? 1 : 0, &low, &high, &error) != DC_OK) { why = "power bounds or XOC state unavailable"; return false; }
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
        ProfileJson controls = ProfileJson::object(); ProfileJson advanced = ProfileJson::array();
        if (kind == "memory") {
            if (!action["scale"].is_number()) throw std::runtime_error("invalid memory scale");
            const double saved_scale = action["scale"].get<double>();
            if (!std::isfinite(saved_scale) || saved_scale <= 0 || saved_scale >= 2147483648.0 || (value / saved_scale) * saved_scale != value) throw std::runtime_error("invalid or nonrepresentable memory scale");
            dc_mem_offset_scale current{}; if (!api_.scale || retry(api_.scale, gpu_, &current) != DC_OK || !current.scale || !std::memchr(current.unit, 0, sizeof(current.unit))) throw std::runtime_error("current memory scale unavailable");
            controls["memory"] = {{"value", value}, {"unit", current.unit}, {"scale", current.scale}};
        } else if (kind == "boost") {
            if (value < 0 || value > 100) throw std::runtime_error("invalid voltage boost request");
            controls["boost"] = {{"value", value}, {"unit", "%"}};
        } else if (kind == "domain") {
            if (value % 1000) throw std::runtime_error("clock domain offset cannot be replayed exactly");
            advanced.push_back({{"kind", "clock_domain"}, {"domain", action["index"]}, {"value", value}, {"unit", "kHz"}});
        } else {
            advanced.push_back({{"kind", "nvvdd"}, {"domain", 0}, {"value", value}, {"unit", "uV"}});
        }
        ProfileJson translated = {{"format", "druta-native-daily"}, {"version", 1}, {"target", {{"uuid", id.uuid}, {"slot", id.slot}}}, {"controls", controls}};
        if (!advanced.empty()) { translated["version"] = 2; translated["advanced"] = advanced; } return translated;
    }
    ScopedSnapshot scoped_capture(PowerSnapshot* power_before = nullptr,
                                  std::array<RailRow, DC_RAILS_COUNT>* rails_before = nullptr) {
        dc_gpu_identity id{};
        if (!identity(id) || !id.uuid[0]) throw std::runtime_error("Fresh paired GPU identity unavailable");
        ScopedSnapshot snapshot; snapshot.target = {id.uuid, id.slot};
        bool xoc = false; const bool xoc_known = effective_xoc(xoc);
        PowerSnapshot power; std::string why;
        if (power_capture(power, why)) {
            if (power_before) *power_before = power;
            dc_cl_statics statics = power_statics(); int64_t low = 0, high = 0;
            dc_error error{}; error.size = sizeof(error);
            if (xoc_known && api_.power_bounds(gpu_, &statics, xoc ? 1 : 0, &low, &high, &error) == DC_OK) {
                ScopedCurrent row; row.key = ScopedKey::board_power(); row.binding = board_binding(power); row.unit = "mW";
                row.value = power.board; row.low = low; row.high = high; row.fresh = row.writable = true;
                snapshot.entries.push_back(std::move(row));
            }
            for (const auto& policy : power.rows) {
                if (policy.board || !policy.consistent || !(policy.writable || policy.named_current) || (policy.unit != 0 && policy.unit != 1)) continue;
                ScopedCurrent row; row.key = ScopedKey::policy_request(policy.policy); row.binding = policy_binding(policy);
                row.unit = policy.unit == 0 ? "mW" : "mA"; row.value = policy.requested;
                row.low = policy.minimum; row.high = policy.maximum; row.fresh = row.writable = true;
                auto pin = std::find(power.pin_ids.begin(), power.pin_ids.end(), policy.policy);
                if (pin != power.pin_ids.end()) row.pin_value = power.pin_values[static_cast<size_t>(pin - power.pin_ids.begin())];
                snapshot.entries.push_back(std::move(row));
            }
        }
        if (rail_service_) {
            const auto rails = rail_service_->read(); if (rails_before) *rails_before = rails;
            dc_rails_hold_record hold{}; hold.size = sizeof(hold);
            const bool hold_known = retry(api_.rails.hold, gpu_, &hold) == DC_OK && (!hold.present || (hold.rail >= 0 && hold.rail < static_cast<int>(DC_RAILS_COUNT)));
            const bool writer = api_.rails.set_raw && api_.rails.lock && api_.rails.unlock && api_.rails.identity && api_.rails.limits && api_.rails.fields;
            for (uint32_t rail = 0; rail < DC_RAILS_COUNT; ++rail) for (uint32_t field = 0; field < DC_RAILS_LIMIT_FIELDS; ++field) {
                const auto& f = rails[rail].fields[field];
                if (!rails[rail].type || !f.established || !f.requested_uv || !f.estimated_base_mv || !xoc_known) continue;
                // Match Rails::set_limits: retain current requests above the
                // normal ceiling, and allow the current/captured initial floor.
                const double low = std::min({std::ceil((DC_RAILS_LIMIT_MIN_MV - *f.estimated_base_mv) * 1000.0), static_cast<double>(*f.requested_uv), static_cast<double>(f.initial_uv.value_or(*f.requested_uv))});
                const double high = std::max(std::floor(((xoc ? DC_RAILS_LIMIT_XOC_MAX_MV : DC_RAILS_LIMIT_MAX_MV) - *f.estimated_base_mv) * 1000.0), static_cast<double>(*f.requested_uv));
                if (!std::isfinite(low) || !std::isfinite(high) || low < INT32_MIN || high > INT32_MAX || low > high) continue;
                ScopedCurrent row; row.key = ScopedKey::rail_field(rail, field);
                row.binding = rail_binding(rail, field, *rails[rail].type, *f.estimated_base_mv); row.unit = "uV";
                row.value = *f.requested_uv; row.low = static_cast<int64_t>(low); row.high = static_cast<int64_t>(high);
                row.fresh = !f.stale; row.writable = rail_mode_ && writer && hold_known && (!hold.present || hold.rail != static_cast<int>(rail));
                snapshot.entries.push_back(std::move(row));
            }
        }
        // Individual unavailable families do not erase unrelated qualified rows.
        if (!identity(id)) throw std::runtime_error("GPU identity changed during profile capture");
        last_.scoped_profiles = std::make_shared<ScopedSnapshot>(snapshot);
        return snapshot;
    }
    ActionResult scoped_action(const Command& cmd) {
        ActionResult result; bool aggregate_stored = false;
        if (!api_.lock || !api_.unlock || api_.lock(gpu_) != DC_OK) { result.message = "Could not lock GPU for profile capture"; return result; }
        struct Unlock { CoreApi& api; dc_gpu* gpu; ~Unlock() { api.unlock(gpu); } } unlock{api_, gpu_};
        try {
            PowerSnapshot power; std::array<RailRow, DC_RAILS_COUNT> rails{};
            const auto captured = scoped_capture(&power, &rails);
            if (cmd.operation == Command::Operation::prepare_scoped_profile) {
                result.ok = true; result.message = "Captured " + std::to_string(captured.entries.size()) + " understood rail/power requests for profile selection"; return result;
            }
            if (!cmd.scoped_profile) throw std::runtime_error("No explicit profile selection/document");
            if (cmd.operation == Command::Operation::save_scoped_profile) {
                if (cmd.profile_path.empty()) throw std::runtime_error("Choose a profile path");
                write_profile(cmd.profile_path, capture_scoped_profile(captured, cmd.scoped_profile->selection));
                result.ok = true; result.message = "Selected configured rail/power requests saved; omitted controls are unchanged. Loading previews; Apply is explicit."; return result;
            }
            const auto plan = plan_scoped_profile(cmd.scoped_profile->document, captured);
            if (std::none_of(plan.steps.begin(), plan.steps.end(), [](const auto& step) { return step.execute_required; })) {
                result.ok = true; result.message = "Selected requests already match"; return result;
            }
            Undo power_undo{}; power_undo.control = ControlId::power; power_undo.power = power;
            power_undo.power.guard_unselected_pins = true;
            for (const auto& step : plan.steps) if (step.key.kind == ScopedKey::Kind::policy) power_undo.power.selected_policies.push_back(step.key.policy);
            Undo aggregate{}; aggregate.control = ControlId::count; remember(aggregate); aggregate_stored = true;
            bool power_recorded = false;
            std::string expected_power = power_state_token(power);
            suppress_undo_ = true;
            struct Reset { bool& flag; ~Reset() { flag = false; } } reset{suppress_undo_};
            for (const auto& step : plan.steps) {
                if (!step.execute_required) continue;
                bool appended = false;
                if (step.key.kind == ScopedKey::Kind::rail) {
                    const auto& row = rails[step.key.rail]; const auto& field = row.fields[step.key.field];
                    Undo saved{}; saved.control = ControlId::count; saved.rail_entry = true;
                    saved.rail = {static_cast<int>(step.key.rail), step.key.field, *row.type, *field.requested_uv};
                    undo_.back().group.push_back(saved); appended = true;
                } else if (!power_recorded) { undo_.back().group.push_back(power_undo); power_recorded = true; appended = true; }
                // Existing writers re-read target, geometry, gate and range. Stop
                // on coupling-induced changes rather than rewriting bindings.
                auto command = step.command;
                if (step.key.kind == ScopedKey::Kind::rail) command.expected_request_raw = step.before;
                else command.expected_power_state = expected_power;
                const auto action = execute(command);
                if (!action.may_have_changed && appended) {
                    undo_.back().group.pop_back();
                    if (step.key.kind != ScopedKey::Kind::rail) power_recorded = false;
                }
                result.may_have_changed = result.may_have_changed || action.may_have_changed;
                if (!action.ok) throw std::runtime_error(action.message);
                if (step.key.kind != ScopedKey::Kind::rail) {
                    PowerSnapshot after; std::string why;
                    if (!power_capture(after, why)) throw std::runtime_error("Coupled power readback unavailable: " + why);
                    expected_power = power_state_token(after);
                }
            }
            const auto mismatch = scoped_profile_readback_error(plan, scoped_capture());
            if (!mismatch.empty()) throw std::runtime_error(mismatch);
            if (undo_.back().group.empty()) { undo_.pop_back(); aggregate_stored = false; }
            result.ok = true; result.message = "Selected rail/power requests and policy pins verified; grouped Undo retains captured requests";
        } catch (const std::exception& error) {
            if (aggregate_stored && undo_.back().group.empty()) { undo_.pop_back(); aggregate_stored = false; }
            result.may_have_changed = result.may_have_changed || aggregate_stored;
            result.message = std::string("Scoped profile failed: ") + error.what() + (aggregate_stored ? "; grouped Undo retained" : "; no writes issued");
        }
        return result;
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
                const bool v1 = keys_are(doc, {"format", "version", "target", "controls"}) && doc["version"] == 1;
                const bool v2 = keys_are(doc, {"format", "version", "target", "controls", "advanced"}) && doc["version"] == 2 && doc["advanced"].is_array();
                if ((!v1 && !v2) || doc["format"] != "druta-native-daily" || !doc["version"].is_number_integer() || !keys_are(doc["target"], {"uuid", "slot"}) || doc["target"]["uuid"] != id.uuid || doc["target"]["slot"] != id.slot || !doc["controls"].is_object() || (doc["controls"].empty() && (!v2 || doc["advanced"].empty()))) throw std::runtime_error("invalid profile format or target adapter");
                for (auto it = doc["controls"].begin(); it != doc["controls"].end(); ++it) if (it.key() != "power" && it.key() != "memory" && it.key() != "core" && it.key() != "fan" && it.key() != "boost") throw std::runtime_error("unknown profile control");
            }
            std::vector<ProfileItem> items; std::string included, omitted;
            for (const auto& spec : {std::pair<ControlId, const char*>{ControlId::power, "power"}, {ControlId::memory, "memory"}, {ControlId::boost, "boost"}, {ControlId::fan, "fan"}, {ControlId::core, "core"}}) {
                if (!save && !doc["controls"].contains(spec.second)) continue;
                ProfileItem item; std::string why; const ProfileJson* desired = save ? nullptr : &doc["controls"][spec.second];
                if (!profile_item(spec.first, desired, item, why)) { if (!save) throw std::runtime_error(std::string(spec.second) + ": " + why); if (!omitted.empty()) omitted += "; "; omitted += std::string(spec.second) + " (" + why + ")"; continue; }
                if (save) { if (!included.empty()) included += ", "; included += spec.second; doc["controls"][spec.second] = item.data; } else if (item.changed) items.push_back(std::move(item));
            }
            if (save) {
                read_advanced(true); ProfileJson advanced = ProfileJson::array();
                for (const auto& entry : last_.advanced_controls) {
                    Undo captured{}; std::string why;
                    if (entry.state.availability != Availability::available || !entry.state.writable || !advanced_capture(entry.id, captured, why) || !advanced_preflight(entry.id, captured.value, captured, why)) { if (!omitted.empty()) omitted += "; "; omitted += entry.label + " (" + (why.empty() ? "not currently qualified" : why) + ")"; continue; }
                    const char* kind = entry.id.kind == AdvancedKind::clock_domain ? "clock_domain" : entry.id.kind == AdvancedKind::nvvdd ? "nvvdd" : "msvdd";
                    advanced.push_back({{"kind", kind}, {"domain", entry.id.domain}, {"value", captured.value}, {"unit", entry.id.kind == AdvancedKind::clock_domain ? "kHz" : "uV"}});
                    if (!included.empty()) included += ", "; included += entry.label;
                }
                if (!advanced.empty()) { doc["version"] = 2; doc["advanced"] = std::move(advanced); }
            } else if (doc["version"] == 2) {
                if (doc["advanced"].size() > 34) throw std::runtime_error("too many advanced controls");
                std::vector<AdvancedId> seen;
                for (const auto& entry : doc["advanced"]) {
                    if (!keys_are(entry, {"kind", "domain", "value", "unit"}) || !entry["kind"].is_string() || !entry["domain"].is_number_integer() || !entry["value"].is_number_integer()) throw std::runtime_error("invalid advanced profile entry");
                    const auto kind = entry["kind"].get<std::string>(); AdvancedId selected{};
                    if (kind == "clock_domain") selected.kind = AdvancedKind::clock_domain; else if (kind == "nvvdd") selected.kind = AdvancedKind::nvvdd; else if (kind == "msvdd") selected.kind = AdvancedKind::msvdd; else throw std::runtime_error("unknown advanced control kind");
                    if (entry["domain"].is_number_unsigned() && entry["domain"].get<uint64_t>() >= DC_CLKDOM_SLOTS) throw std::runtime_error("invalid advanced control index");
                    auto domain = entry["domain"].get<int64_t>();
                    if (domain < 0 || domain >= DC_CLKDOM_SLOTS || (selected.kind == AdvancedKind::nvvdd && domain != 0)) throw std::runtime_error("invalid advanced control index"); selected.domain = static_cast<uint32_t>(domain);
                    if (entry["value"].is_number_unsigned() && entry["value"].get<uint64_t>() > INT32_MAX) throw std::runtime_error("advanced request outside signed range");
                    auto raw = entry["value"].get<int64_t>(); if (raw < INT32_MIN || raw > INT32_MAX || entry["unit"] != (selected.kind == AdvancedKind::clock_domain ? "kHz" : "uV")) throw std::runtime_error("invalid advanced request units or value");
                    if (std::find_if(seen.begin(), seen.end(), [&](const AdvancedId& old) { return advanced_equal(old, selected); }) != seen.end()) throw std::runtime_error("duplicate advanced profile control"); seen.push_back(selected);
                    ProfileItem item; std::string why; if (!advanced_preflight(selected, static_cast<int32_t>(raw), item.undo, why)) throw std::runtime_error("advanced profile: " + why);
                    item.command.operation = Command::Operation::apply; item.command.advanced = selected; item.command.expected_binding = advanced_binding(selected, item.undo.advanced_layout); item.command.value = raw / 1000.0;
                    if (std::abs(item.command.value * 1000 - raw) > 0.000001) throw std::runtime_error("advanced profile request is not exactly representable");
                    item.changed = item.undo.value != raw; if (item.changed) items.push_back(std::move(item));
                }
            }
            if (save) { if (doc["controls"].empty() && !doc.contains("advanced")) throw std::runtime_error("no fully qualified controls to save"); write_profile(cmd.profile_path, doc); result.ok = true; result.message = "Native daily profile saved. Included: " + included + "." + (omitted.empty() ? "" : " Omitted: " + omitted + ".") + " Core curve, individual policy rows, and rail limits are not saved."; return result; }
            if (items.empty()) { result.ok = true; result.message = python_profile ? "Python tuning action already matches current request" : "Profile already matches current requests; core curve is not saved"; return result; }
            Undo aggregate{}; aggregate.control = ControlId::count; remember(aggregate); aggregate_stored = true;
            suppress_undo_ = true;
            struct Reset { bool& flag; ~Reset() { flag = false; } } reset{suppress_undo_};
            for (const auto& item : items) { undo_.back().group.push_back(item.undo); auto step = execute(item.command); if (!step.may_have_changed) undo_.back().group.pop_back(); result.may_have_changed = result.may_have_changed || step.may_have_changed; if (!step.ok) { if (undo_.back().group.empty()) { undo_.pop_back(); aggregate_stored = false; } result.message = "Profile partially applied: " + step.message + (aggregate_stored ? "; aggregate Undo retained" : "; no writes were issued"); return result; } }
            if (undo_.back().group.empty()) undo_.pop_back();
            result.ok = true; result.message = python_profile ? "Python tuning action profile applied" : "Native daily profile applied; core curve is not saved"; return result;
        } catch (const std::exception& e) { result.message = std::string("Profile failed: ") + e.what() + (aggregate_stored ? "; aggregate Undo retained" : ""); result.may_have_changed = aggregate_stored; return result; }
    }

    ActionResult release_hold() {
        ActionResult out; out.ok=true;
        if (!tuning_ || !maintenance_needed()) { out.message="No owned V/F hold"; return out; }
        const auto r=tuning_->release(); last_.tuning=tuning_->state();
        out.ok=r.ok; out.may_have_changed=r.may_have_changed; out.message=r.message; return out;
    }
    ActionResult release_frequency() {
        ActionResult out; out.ok=true;
        if (!frequency_) return out;
        const auto r=frequency_->release(); last_.frequency=frequency_->data();
        out.ok=r.ok; out.may_have_changed=r.may_have_changed; out.message=r.message; return out;
    }
    ActionResult frequency_action(const Command& cmd) {
        ActionResult out;
        if (!frequency_) { out.message="Frequency lock service unavailable"; return out; }
        if (cmd.operation==Command::Operation::frequency_lock_release) return release_frequency();
        last_.frequency=frequency_->read();
        if (cmd.operation==Command::Operation::frequency_lock_refresh) {
            out.ok=last_.frequency.tables_current || last_.frequency.legacy_known;
            out.message=last_.frequency.message; return out;
        }
        const bool legacy=cmd.operation==Command::Operation::frequency_legacy_p0_max_fan;
        if (legacy ? (!last_.frequency.legacy_known || !last_.frequency.legacy_available) : !last_.frequency.nvml_capable) {
            out.message=last_.frequency.message; return out;
        }
        // Validate the intended acquisition before releasing a working owner.
        // The service repeats these checks after handover, under its GPU lock.
        if (!legacy) {
            const auto& f=last_.frequency;
            const auto low=cmd.operation==Command::Operation::frequency_lock_max ? f.maximum_mhz.value_or(0) : cmd.frequency_min_mhz;
            const auto high=cmd.operation==Command::Operation::frequency_lock_max ? f.maximum_mhz.value_or(0) : cmd.frequency_max_mhz;
            if (!f.tables_current || !f.minimum_mhz || !f.maximum_mhz || low<*f.minimum_mhz || low>high || high>*f.maximum_mhz) {
                out.message="Frequency range is outside current lockable bounds; existing owners retained"; return out;
            }
            bool i2c_clock=false;
            if (i2c_session_) { const auto& i=i2c_session_->data();
                i2c_clock=i.clock_owned && f.current && f.current->minimum==i.clock_mhz && f.current->maximum==i.clock_mhz;
            }
            if (!f.current_known || (f.current && !i2c_clock && f.ownership.mode!=FrequencyLockMode::nvml)) {
                out.message="Competing frequency state is unreadable or foreign; existing owners retained"; return out;
            }
            dc_vf_lock_status vf{};
            // NVML range locking has an independent request channel. Missing
            // V/F capability must not remove a qualified frequency control.
            if (api_.tuning.vf_status && retry(api_.tuning.vf_status,gpu_,0,0u,&vf)==DC_OK && !vf.error[0] &&
                vf.locked && (!tuning_ || !tuning_->state().owned)) {
                out.message="A foreign V/F hold is active; existing owners retained"; return out;
            }
        }
        // Capture the fan Undo before changing either half of this combined action.
        dc_fan_snapshot fan_before{};
        if (legacy) {
            dc_fan_capabilities caps{}; caps.size=sizeof(caps);
            if (!api_.fan_set || !api_.fan_restore || retry(api_.fan_caps,gpu_,&caps)!=DC_OK || !caps.manual ||
                caps.min>100 || caps.max!=100 || !capture_fan(fan_before)) {
                out.message="Fresh manual fan capability and captured requests are required for P0 + maximum fan"; return out;
            }
        }
        if (i2c_session_ && i2c_session_->owned()) {
            out=i2c_session_->release(); last_.i2c=i2c_session_->data(); if (!out.ok) return out;
        }
        if (tuning_ && tuning_->state().maintenance_needed()) { out=release_hold(); if (!out.ok) return out; }
        const bool retained_legacy=frequency_->data().ownership.mode==FrequencyLockMode::legacy_p0 &&
            frequency_->data().ownership.confirmed && !frequency_->data().ownership.recovery;
        const auto r=legacy ? frequency_->hold_legacy_p0() :
            cmd.operation==Command::Operation::frequency_lock_max ? frequency_->lock_max() :
            frequency_->apply(cmd.frequency_min_mhz,cmd.frequency_max_mhz);
        last_.frequency=frequency_->data(); out.ok=r.ok; out.may_have_changed|=r.may_have_changed; out.message=r.message;
        if (legacy && out.ok) {
            // The established fan path independently captures and verifies its
            // request immediately before writing, retaining its typed Undo.
            Command fan; fan.operation=Command::Operation::apply; fan.control=ControlId::fan; fan.value=100;
            const auto applied=execute(fan);
            out.ok=applied.ok; out.may_have_changed|=applied.may_have_changed; out.message+="; "+applied.message;
            if (!applied.ok && !retained_legacy) {
                const auto released=release_frequency(); out.may_have_changed|=released.may_have_changed;
                out.message+="; "+released.message;
            }
        }
        return out;
    }
    bool locks_clear(std::string& why) {
        dc_vf_lock_status vf{}; dc_clk_lock_status clock{};
        if (!api_.tuning.vf_status || !api_.tuning.clock_status || retry(api_.tuning.vf_status,gpu_,0,0u,&vf)!=DC_OK || vf.error[0] ||
            retry(api_.tuning.clock_status,gpu_,&clock)!=DC_OK || clock.error[0]) { why="Clock/V/F hold status unavailable"; return false; }
        if (vf.locked || clock.locked) { why="A clock or V/F hold is already active; release its owner first"; return false; }
        return true;
    }
    ActionResult hold_action(const Command& cmd) {
        ActionResult out; dc_gpu_identity id{}; dc_vf_delta_snapshot snap{}; std::vector<dc_vf_point> points; std::string why;
        if (!tuning_ || !identity(id) || !curve_table(snap,points,why)) { out.message="Fresh curve or GPU identity unavailable: "+why; return out; }
        if (!same_curve_mapping(cmd.expected_curve,points,snap.freq_div) || cmd.expected_curve_freq_div!=snap.freq_div) { out.message="Curve geometry changed; refresh before holding a point"; return out; }
        for (const auto& p:points) { auto old=std::find_if(cmd.expected_curve.begin(),cmd.expected_curve.end(),[&](const auto& q){return q.idx==p.idx;});
            if (old==cmd.expected_curve.end() || old->delta_khz!=p.delta_khz) { out.message="Curve request changed; refresh before holding a point"; return out; } }
        const auto point=std::find_if(points.begin(),points.end(),[&](const auto& p){return p.idx==cmd.hold_point_index;});
        if (point==points.end() || !std::isfinite(point->volt_mv) || point->volt_mv<=0 || point->volt_mv*1000>UINT32_MAX) { out.message="Selected curve voltage unavailable"; return out; }
        bool xoc=false; if (!effective_xoc(xoc)) { out.message="XOC state unreadable"; return out; }
        if (maintenance_needed()) { out=release_hold(); if (!out.ok) return out; }
        const auto r=tuning_->hold(6,static_cast<uint32_t>(std::llround(point->volt_mv*1000)),cmd.hold_headroom,cmd.headroom_margin_mv,xoc);
        last_.tuning=tuning_->state(); out.ok=r.ok; out.may_have_changed=r.may_have_changed; out.message=r.message; return out;
    }
    ActionResult max_action(const Command& cmd) {
        ActionResult result; dc_gpu_identity id{}; std::string why;
        if (!tuning_ || !api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK) { result.message="Max it interfaces unavailable"; return result; }
        struct Unlock { CoreApi& a; dc_gpu* g; ~Unlock(){a.unlock(g);} } lock{api_,gpu_};
        if (!identity(id)) { result.message="GPU identity changed"; return result; }
        const auto fresh=read(true);
        if (!fresh.max_it_available || !fresh.curve_writable || fresh.curve.empty() || cmd.expected_curve_freq_div!=fresh.curve_freq_div ||
            !same_curve_mapping(cmd.expected_curve,fresh.curve,fresh.curve_freq_div)) { result.message="Max it needs the current complete curve"; return result; }
        for (const auto& p:fresh.curve) { auto previous=std::find_if(cmd.expected_curve.begin(),cmd.expected_curve.end(),[&](const auto& q){return p.idx==q.idx;});
            if (previous==cmd.expected_curve.end() || previous->delta_khz!=p.delta_khz) { result.message="Curve changed since Max it was requested"; return result; } }
        CurvePlanRequest request; request.kind=CurvePlanKind::ramp; request.points=fresh.curve; request.freq_div=fresh.curve_freq_div;
        request.step_khz=fresh.curve_step_khz; request.max_abs_delta_khz=fresh.curve_max_abs_delta_khz;
        request.max_rise_khz=fresh.curve_model_max_rise_khz; request.cap_mv=cmd.curve_cap_mv; request.floor_mv=cmd.curve_floor_mv;
        const auto plan=tuning_->prepare_max(api_.planner,request);
        if (!plan.ok) { result.message="Max it plan refused before writes: "+plan.message; return result; }
        const dc_vf_point* selected=nullptr;
        for (const auto& p:fresh.curve) if (p.volt_mv<=cmd.curve_cap_mv && (!selected || p.volt_mv>selected->volt_mv)) selected=&p;
        if (!selected) { result.message="No curve point at or below the cap; no writes issued"; return result; }
        dc_capabilities caps{}; caps.size=sizeof(caps);
        if (retry(api_.capabilities,gpu_,&caps)!=DC_OK || !(caps.present&DC_C_PL_MAX) || !caps.pl_max_mw || caps.pl_max_mw==UINT32_MAX) { result.message="Normal board power maximum unavailable"; return result; }
        std::vector<ProfileItem> items;
        const ProfileJson targets[]={{{"mode","manual"},{"value",100},{"unit","%"}},{{"value",caps.pl_max_mw},{"unit","mW"}},{{"value",100},{"unit","%"}}};
        const ControlId controls[]={ControlId::fan,ControlId::power,ControlId::boost};
        for (size_t i=0;i<3;++i) { ProfileItem item; if (!profile_item(controls[i],&targets[i],item,why)) { result.message="Max it capture failed before writes: "+why; return result; } items.push_back(std::move(item)); }
        Undo curve_before{}; curve_before.control=ControlId::count; curve_before.curve_entry=true;
        if (!curve_table(curve_before.curve,curve_before.curve_points,why,&curve_before.curve_layout)) { result.message="Max it curve capture failed: "+why; return result; }
        if (maintenance_needed()) { result=release_hold(); if (!result.ok) return result; }
        if (!locks_clear(why)) { result.message=why; return result; }
        Undo group{}; group.control=ControlId::count; remember(group); suppress_undo_=true;
        struct Unsuppress { bool& flag; ~Unsuppress(){flag=false;} } unsuppress{suppress_undo_};
        auto step=[&](const Command& action,const Undo& before) {
            undo_.back().group.push_back(before); const auto r=execute(action);
            if (!r.may_have_changed) undo_.back().group.pop_back(); result.may_have_changed|=r.may_have_changed;
            if (!result.message.empty()) result.message+="\n"; result.message+=r.message; return r.ok;
        };
        bool okay=true;
        for (const auto& item:items) if (item.changed && !step(item.command,item.undo)) { okay=false; break; }
        if (okay && !plan.changes.empty()) { Command curve=cmd; curve.operation=Command::Operation::apply_curve; curve.curve_deltas=plan.changes; okay=step(curve,curve_before); }
        if (okay) {
            if (selected) { Command hold=cmd; hold.operation=Command::Operation::hold_curve_point; hold.hold_point_index=selected->idx;
                dc_vf_delta_snapshot updated{}; std::string capture_error;
                if (!curve_table(updated,hold.expected_curve,capture_error)) { result.message+="\nHold skipped: "+capture_error; okay=false; }
                hold.expected_curve_freq_div=updated.freq_div;
                if (okay) {
                const auto r=hold_action(hold); okay=r.ok; result.may_have_changed|=r.may_have_changed; result.message+="\n"+r.message;
                }
            } else { okay=false; result.message+="\nNo curve point at or below the cap"; }
        }
        if (undo_.back().group.empty()) undo_.pop_back();
        result.ok=okay; result.message=(okay?"Max it applied: ":"Max it incomplete; captured Undo retained: ")+result.message; return result;
    }
    static int32_t DC_CALL reset_statics(void* context,const dc_cl_statics*,dc_cl_statics* out) {
        *out=static_cast<Client*>(context)->power_statics(); return 0;
    }
    ActionResult reset_action() {
        ActionResult out; std::string why;
        if (!api_.reset_all || !api_.lock || !api_.unlock || api_.lock(gpu_)!=DC_OK) { out.message="Reset interfaces unavailable"; return out; }
        struct Unlock { CoreApi& a; dc_gpu* g; ~Unlock(){a.unlock(g);} } lock{api_,gpu_};
        dc_gpu_identity id{}; if (!identity(id)) { out.message="GPU identity unavailable"; return out; }
        if (i2c_session_ && i2c_session_->owned()) { out=i2c_session_->release(); last_.i2c=i2c_session_->data(); if (!out.ok) return out; }
        if (maintenance_needed()) { out=release_hold(); if (!out.ok) return out; }
        if (!locks_clear(why)) { out.message=why; return out; }
        bool xoc=false; if (!effective_xoc(xoc)) { out.message="XOC state unavailable"; return out; }
        const auto fresh=read(true); Undo saved{}; saved.control=ControlId::count; std::string omitted;
        bool core_captured=false;
        for (auto c:{ControlId::core,ControlId::memory,ControlId::power,ControlId::fan,ControlId::boost}) {
            ProfileItem item; if (profile_item(c,nullptr,item,why)) saved.group.push_back(item.undo);
            else { if (!omitted.empty()) omitted+=", "; omitted+=label(c); }
            if (c==ControlId::core) core_captured=!saved.group.empty();
        }
        Undo curve{}; curve.control=ControlId::count; curve.curve_entry=true;
        if (!core_captured) { if (curve_table(curve.curve,curve.curve_points,why,&curve.curve_layout)) saved.group.push_back(curve); else omitted+=", V/F curve"; }
        for (const auto& a:fresh.advanced_controls) { Undo u{}; if (advanced_capture(a.id,u,why) && advanced_preflight(a.id,u.value,u,why)) saved.group.push_back(u); else omitted+=", "+a.label; }
        if (rail_service_) { const auto rows=rail_service_->read();
            for (uint32_t rail=0;rail<DC_RAILS_COUNT;++rail) for (uint32_t field=0;field<DC_RAILS_LIMIT_FIELDS;++field) {
                const auto& r=rows[rail]; if (!r.type || !r.fields[field].requested_uv || r.fields[field].stale) continue;
                Undo u{}; u.control=ControlId::count; u.rail_entry=true; u.rail={static_cast<int>(rail),field,*r.type,*r.fields[field].requested_uv}; saved.group.push_back(u);
            }
        }
        if (!saved.group.empty()) remember(saved);
        std::vector<dc_reset_step> steps(DC_RESET_STEPS_MAX); for (auto& s:steps) s.size=sizeof(s);
        uint32_t count=0; dc_rails_setter_writes writes{}; writes.size=sizeof(writes); dc_error error{}; error.size=sizeof(error);
        PolicyInputs inputs{this,power_statics()}; out.may_have_changed=true;
        dc_status status=DC_E_FAILED;
        try { status=api_.reset_all(gpu_,confirm_clamp,this,policy_inputs,&inputs,reset_statics,this,xoc?1:0,steps.data(),static_cast<uint32_t>(steps.size()),&count,&writes,&error); }
        catch (...) { count=0; }
        out.ok=status==DC_OK && count<=steps.size(); out.message.clear();
        if (count<=steps.size()) for (uint32_t i=0;i<count;++i) { out.ok=out.ok && steps[i].ok!=0; out.message+="\n"+std::string(steps[i].name)+": "+steps[i].message; }
        // Keep only changed or unreadable requests. An untouched field does not
        // become ours to replay merely because Reset inspected it.
        if (!saved.group.empty() && !undo_.empty()) {
            const auto rail_now=rail_service_ ? rail_service_->read() : std::array<RailRow,DC_RAILS_COUNT>{};
            auto unchanged=[&](const Undo& before) {
                if (before.rail_entry) { const auto& row=rail_now[before.rail.rail]; const auto& f=row.fields[before.rail.field];
                    return row.type && *row.type==before.rail.type && !f.stale && f.requested_uv && *f.requested_uv==before.rail.requested_uv; }
                if (before.advanced_entry) { Undo after{}; return advanced_capture(before.advanced,after,why) && layout_equal(before.advanced_layout,after.advanced_layout) && before.value==after.value; }
                if (before.curve_entry) { dc_vf_delta_snapshot after{}; std::vector<dc_vf_point> points;
                    dc_vf_layout layout{}; return curve_table(after,points,why,&layout) && same_curve(before.curve,after) && same_curve_layout(before.curve_layout,layout) && same_curve_mapping(before.curve_points,points,after.freq_div); }
                ProfileItem after; if (!profile_item(before.control,nullptr,after,why)) return false;
                if (before.control==ControlId::power) return before.power.board==after.undo.power.board && before.power.pin_ids==after.undo.power.pin_ids && before.power.pin_values==after.undo.power.pin_values;
                if (before.control==ControlId::fan) return std::memcmp(&before.fan,&after.undo.fan,sizeof(before.fan))==0;
                if (before.control==ControlId::core) return before.value==after.undo.value && same_curve(before.curve,after.undo.curve) && same_curve_mapping(before.curve_points,after.undo.curve_points,after.undo.curve.freq_div);
                return before.value==after.undo.value && before.scale==after.undo.scale;
            };
            auto& group=undo_.back().group;
            group.erase(std::remove_if(group.begin(),group.end(),unchanged),group.end());
            if (group.empty()) undo_.pop_back();
        }
        if (!omitted.empty()) out.message+="\nUndo unavailable for uncaptured controls: "+omitted;
        if (error.message[0]) out.message+="\n"+std::string(error.message);
        out.message=(out.ok?"Reset controls completed":"Reset incomplete")+out.message;
        return out;
    }
    ActionResult timing_action(const Command& cmd) {
        ActionResult out;
        auto publish = [&] {
            timing_data_.undo_available = timing_undo_.has_value() || timing_batch_undo_.has_value();
            timing_data_.initial_available = timings_ && timings_->initial_snapshot().has_value();
            timing_data_.identity=opened_;
            timing_data_.helper_token=timing_transport_ ? timing_transport_->token() : "";
            last_.timings = std::make_shared<TimingViewData>(timing_data_);
        };
        if (cmd.operation == Command::Operation::timings_select_helper) {
            if (timing_undo_ || timing_batch_undo_) { out.message = "Undo the selected timing edit before changing helpers"; return out; }
            if (cmd.timing_helper_path.empty()) { out.message = "Select an absolute nvtune helper path"; return out; }
            auto transport = make_timing_transport(cmd.timing_helper_path);
            std::string why;
            if (!transport || !transport->pin(why)) { out.message = why.empty() ? "Helper could not be opened" : why; return out; }
            transport->unpin();
            timings_.reset(); timing_transport_ = std::move(transport); timing_data_ = {};
            timing_data_.helper_path = cmd.timing_helper_path;
            TimingsApi a; a.identity=api_.identity; a.telemetry=api_.telemetry; a.capabilities=api_.capabilities; a.lock=api_.lock; a.unlock=api_.unlock;
            timings_ = std::make_unique<TimingsService>(a,gpu_,opened_,*timing_transport_);
            out.ok=true; out.message="Timing helper selected; Read fields captures this adapter";
        } else if (!timings_) { out.message="Choose the nvtune helper first";
        } else if (cmd.operation == Command::Operation::timings_read) {
            timing_data_.preview.reset();
            timing_data_.batch_preview.reset(); ++timing_data_.capture_revision;
            auto fresh=timings_->capture(); out.ok=fresh.ok; out.message=fresh.message;
            if (fresh.ok) timing_data_.snapshot=std::move(fresh);
            else { timing_data_.snapshot.ok=false; timing_data_.snapshot.message=out.message; }
        } else if (cmd.operation == Command::Operation::timings_preview) {
            timing_data_.batch_preview.reset();
            timing_data_.preview=timings_->preview(cmd.timing_field,cmd.timing_value);
            out.ok=timing_data_.preview->ok; out.message=timing_data_.preview->message;
        } else if (cmd.operation == Command::Operation::timings_apply) {
            if (!timing_data_.preview || !timing_data_.preview->ok || timing_data_.preview->field!=cmd.timing_field || timing_data_.preview->value!=cmd.timing_value) {
                out.message="Preview the selected field and value before Apply";
            } else if (timing_undo_ || timing_batch_undo_) { out.message="Undo the previous timing edit before applying another";
            } else {
                const auto r=timings_->apply(*timing_data_.preview,[&](const TimingPlan& p) { timing_undo_=p; publish(); });
                out.ok=r.ok; out.may_have_changed=r.may_have_changed; out.message=r.message;
                if (!r.may_have_changed) timing_undo_.reset();
                timing_data_.preview.reset();
                if (r.may_have_changed) timing_data_.snapshot=timings_->capture();
            }
        } else if (cmd.operation == Command::Operation::timings_undo) {
            if (timing_batch_undo_) {
                const auto r=timings_->restore(*timing_batch_undo_);
                out.ok=r.ok; out.may_have_changed=r.may_have_changed; out.message=r.message;
                if (r.ok) timing_batch_undo_.reset();
                timing_data_.preview.reset(); timing_data_.batch_preview.reset();
                timing_data_.snapshot=timings_->capture(); ++timing_data_.capture_revision;
            } else if (!timing_undo_) { out.message="No timing edit to undo";
            } else {
                const auto r=timings_->restore(*timing_undo_);
                out.ok=r.ok; out.may_have_changed=r.may_have_changed; out.message=r.message;
                if (r.ok) timing_undo_.reset();
                timing_data_.preview.reset(); timing_data_.snapshot=timings_->capture();
            }
        } else if (cmd.operation == Command::Operation::timings_preview_batch) {
            timing_data_.preview.reset();
            if (!cmd.timing_editor) { out.message="No staged timing fields"; }
            else {
                timing_data_.batch_preview=timings_->preview(cmd.timing_editor->fields);
                out.ok=timing_data_.batch_preview->ok; out.message=timing_data_.batch_preview->message;
            }
        } else if (cmd.operation == Command::Operation::timings_apply_batch || cmd.operation == Command::Operation::timings_restore_initial) {
            const bool initial=cmd.operation==Command::Operation::timings_restore_initial;
            bool matched=initial;
            if (!initial && cmd.timing_editor && timing_data_.batch_preview && timing_data_.batch_preview->ok) {
                const auto& plans=timing_data_.batch_preview->fields;
                const auto& requests=cmd.timing_editor->fields;
                matched=plans.size()==requests.size();
                for (const auto& plan:plans) if (matched) {
                    matched=std::count_if(requests.begin(),requests.end(),[&](const auto& request) {
                        return request.field==plan.field && request.value==plan.value;
                    })==1;
                }
            }
            if (!matched) { out.message="Preview every staged timing field before Apply"; }
            else if (timing_undo_ || timing_batch_undo_) { out.message="Undo the previous timing edit before applying another"; }
            else {
                const auto before=[&](const TimingBatchPlan& saved) { timing_batch_undo_=saved; publish(); };
                const auto r=initial ? timings_->restore_initial(before) : timings_->apply(*timing_data_.batch_preview,before);
                out.ok=r.ok; out.may_have_changed=r.may_have_changed; out.message=r.message;
                if (r.undo) timing_batch_undo_=r.undo;
                else if (!r.may_have_changed) timing_batch_undo_.reset();
                timing_data_.batch_preview.reset(); timing_data_.preview.reset();
                if (r.may_have_changed) { timing_data_.snapshot=timings_->capture(); ++timing_data_.capture_revision; }
            }
        } else if (cmd.operation == Command::Operation::timings_save_profile) {
            try {
                if (cmd.profile_path.empty() || !cmd.timing_editor) throw std::runtime_error("Choose timing fields and a profile path");
                auto fresh=timings_->capture(); dc_gpu_identity id{};
                if (!fresh.ok || !identity(id)) throw std::runtime_error(fresh.ok ? "GPU identity unavailable" : fresh.message);
                timing_data_.snapshot=fresh; ++timing_data_.capture_revision;
                write_profile(cmd.profile_path,capture_timing_editor_profile(id,fresh,cmd.timing_editor->fields));
                out.ok=true; out.message="Timing profile saved; loading stages fields for explicit Preview and Apply";
            } catch (const std::exception& error) { out.message=std::string("Timing profile save failed: ")+error.what(); }
        }
        timing_data_.message=out.message; publish(); return out;
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
        LOAD(clock_classify, dc_clock_domains_classify);
        LOAD(frequency.lock, dc_gpu_lock); LOAD(frequency.unlock, dc_gpu_unlock); LOAD(frequency.identity, dc_gpu_identity_get);
        LOAD(frequency.architecture, dc_gpu_architecture); LOAD(frequency.export_address, dc_export_address);
        LOAD(frequency.capabilities, dc_gpu_capabilities_get); LOAD(frequency.lockable_clocks, dc_gpu_lockable_clocks);
        LOAD(frequency.clock_status, dc_gpu_clk_lock_status); LOAD(frequency.clocks_lock, dc_gpu_clocks_lock);
        LOAD(frequency.last_lock, dc_gpu_clocks_last_lock); LOAD(frequency.clocks_reset, dc_gpu_clocks_reset);
        LOAD(frequency.legacy_supported, dc_legacy_p0_supported); LOAD(frequency.legacy_owned, dc_legacy_p0_owned);
        LOAD(frequency.legacy_hold, dc_legacy_p0_hold); LOAD(frequency.legacy_release, dc_legacy_p0_release);
        LOAD(perf.identity, dc_gpu_identity_get); LOAD(perf.read_group, dc_perf_read_group);
        LOAD(planner.hard_deflatten, dc_vfp_compute_hard_deflatten);
        LOAD(reset_all, dc_gpu_reset_all);
        LOAD(tuning.lock, dc_gpu_lock); LOAD(tuning.unlock, dc_gpu_unlock); LOAD(tuning.identity, dc_gpu_identity_get);
        LOAD(tuning.vf_status, dc_gpu_vf_lock_status); LOAD(tuning.clock_status, dc_gpu_clk_lock_status);
        LOAD(tuning.vf_set, dc_gpu_vf_lock_set); LOAD(tuning.vf_clear, dc_gpu_vf_lock_clear);
        LOAD(tuning.recovery_pending, dc_gpu_vf_lock_recovery_pending); LOAD(tuning.recover, dc_gpu_vf_lock_recover);
        LOAD(tuning.headroom_record, dc_rails_hold_record_get); LOAD(tuning.headroom_apply, dc_rails_hold_apply);
        LOAD(tuning.headroom_restore, dc_rails_hold_restore); LOAD(tuning.headroom_live, dc_rails_hold_live_ok);
        LOAD(tuning.headroom_overwritten, dc_rails_hold_overwritten);
        LOAD(cpc_live_read, dc_cpc_live_read);
        LOAD(i2c.identity, dc_gpu_identity_get); LOAD(i2c.read, dc_i2c_read); LOAD(i2c.write, dc_i2c_write);
        LOAD(i2c.read_bytes,dc_i2c_read_bytes); LOAD(i2c.controller_write,dc_i2c_controller_write);
        LOAD(i2c.verify_hold.architecture,dc_gpu_architecture);
        LOAD(i2c.verify_hold.vf_status,dc_gpu_vf_lock_status);LOAD(i2c.verify_hold.clock_status,dc_gpu_clk_lock_status);
        LOAD(i2c.verify_hold.vf_entry,dc_gpu_vf_lock_entry_get);LOAD(i2c.verify_hold.vf_set,dc_gpu_vf_lock_set);
        LOAD(i2c.verify_hold.vf_pending,dc_gpu_vf_lock_recovery_pending);LOAD(i2c.verify_hold.vf_recovery_get,dc_gpu_vf_lock_recovery_get);
        LOAD(i2c.verify_hold.vf_recovery_set,dc_gpu_vf_lock_recovery_set);LOAD(i2c.verify_hold.vf_recover,dc_gpu_vf_lock_recover);
        LOAD(i2c.verify_hold.curve,dc_gpu_vf_curve_read);LOAD(i2c.verify_hold.vcore,dc_gpu_read_vcore_mv);
        LOAD(i2c.verify_hold.legacy_supported,dc_legacy_p0_supported);LOAD(i2c.verify_hold.legacy_owned,dc_legacy_p0_owned);
        LOAD(i2c.verify_hold.legacy_request,dc_legacy_p0_request);LOAD(i2c.verify_hold.legacy_release,dc_legacy_p0_release);
        LOAD(i2c.verify_hold.telemetry,dc_gpu_read_telemetry);
        LOAD(i2c.architecture,dc_gpu_architecture); LOAD(i2c.flags,dc_gpu_clkdom_flags_get);
        LOAD(i2c.lock, dc_gpu_lock); LOAD(i2c.unlock, dc_gpu_unlock); LOAD(i2c.clocks, dc_gpu_lockable_clocks);
        LOAD(i2c.clamp, dc_gpu_clocks_lock); LOAD(i2c.release, dc_gpu_clocks_reset);
        LOAD(i2c.clock_status, dc_gpu_clk_lock_status); LOAD(i2c.vf_status, dc_gpu_vf_lock_status); LOAD(i2c.telemetry, dc_gpu_read_telemetry);
        LOAD(rails.lock, dc_gpu_lock); LOAD(rails.unlock, dc_gpu_unlock); LOAD(rails.identity, dc_gpu_identity_get); LOAD(rails.limits, dc_rails_limits); LOAD(rails.fields, dc_rails_limit_fields); LOAD(rails.profile, dc_rails_profile); LOAD(rails.initial, dc_rails_initial_get); LOAD(rails.hold, dc_rails_hold_record_get); LOAD(rails.set_raw, dc_rails_set_limits_raw); LOAD(rails.reset, dc_rails_reset_limits); LOAD(planner.deflatten, dc_vfp_compute_deflatten); LOAD(planner.ramp, dc_vfp_compute_ramp); LOAD(planner.rephase, dc_vfp_compute_rephase); LOAD(planner.predict, dc_vfp_predict_curve); LOAD(abi_version, dc_abi_version); LOAD(enumerate, dc_enumerate_gpus); LOAD(open, dc_gpu_open); LOAD(close, dc_gpu_close); LOAD(identity, dc_gpu_identity_get);
        LOAD(telemetry, dc_gpu_read_telemetry); LOAD(capabilities, dc_gpu_capabilities_get); LOAD(range, dc_gpu_clock_offset_range_get); LOAD(scale, dc_gpu_mem_offset_scale); LOAD(step, dc_gpu_memory_offset_step_units); LOAD(set_offset, dc_gpu_clock_offset_set);
        LOAD(lock, dc_gpu_lock); LOAD(unlock, dc_gpu_unlock); LOAD(power_bounds, dc_pl_bounds); LOAD(power_set, dc_pl_set); LOAD(power_message, dc_pl_message_get); LOAD(current_limits, dc_cl_get_current_limits); LOAD(policy_read, dc_cl_power_policies_read); LOAD(policy_set, dc_cl_set_power_policy); LOAD(policy_message, dc_cl_policy_message_get); LOAD(pins_get, dc_cl_pins_get); LOAD(pins_set, dc_cl_pins_set); LOAD(envelopes, dc_request_envelopes_get); LOAD(reset_curve, dc_gpu_vf_curve_reset); LOAD(clock_step, dc_gpu_clock_step_khz); LOAD(max_rise, dc_gpu_max_rise_khz); LOAD(snapshot, dc_gpu_vf_delta_snapshot); LOAD(apply_deltas, dc_gpu_vf_deltas_apply); LOAD(fan_set, dc_gpu_fan_set); LOAD(fan_reset, dc_gpu_fan_reset); LOAD(fan_restore, dc_gpu_fan_restore); LOAD(boost_set, dc_gpu_voltage_boost_set); LOAD(fan_state, dc_gpu_fan_state_get); LOAD(fan_caps, dc_gpu_fan_capabilities_get); LOAD(boost, dc_gpu_voltage_boost_get); LOAD(clkdom_ok, dc_gpu_clkdom_ok); LOAD(clkdom_layout, dc_gpu_clkdom_layout); LOAD(clkdom_domains, dc_gpu_clkdom_domains); LOAD(clkdom_controls, dc_gpu_clkdom_controls_for_ui); LOAD(clkdom_offered, dc_gpu_clkdom_offset_slider_offered); LOAD(clkdom_label, dc_gpu_clkdom_control_label); LOAD(clkdom_requests, dc_gpu_clkdom_requests); LOAD(clkdom_set, dc_gpu_clkdom_offset_set); LOAD(rail_cap, dc_gpu_rail_offset_capability); LOAD(rail_read, dc_gpu_rail_offset_read); LOAD(rail_set, dc_gpu_rail_offset_set); LOAD(msvdd_reset, dc_gpu_msvdd_offset_reset); LOAD(clkdom_flags_set, dc_gpu_clkdom_flags_set); LOAD(clkdom_flags, dc_gpu_clkdom_flags_get); LOAD(curve_layout, dc_gpu_vf_layout); LOAD(curve, dc_gpu_vf_curve_read); LOAD(power, dc_pl_read); LOAD(enforced_power, dc_pl_enforced_read);
#undef LOAD
    }
#endif
    ~Client() override { close();
#ifdef _WIN32
        if (module_ && !gpu_) FreeLibrary(module_);
#endif
    }
    PlannerApi planner_api() const override { return api_.planner; }
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
        close(); if (gpu_) { error="Owned control recovery must finish before changing GPU"; return false; }
        if (!valid()) { error = "Core DLL is missing required exports or has an incompatible ABI"; return false; }
        if (slot.empty() || api_.open(slot.c_str(), &gpu_) != DC_OK || !gpu_) { error = "Could not open selected GPU"; return false; }
        opened_.size = sizeof(opened_);
        if (retry(api_.identity, gpu_, &opened_) != DC_OK || !opened_.slot[0] || !(opened_.nvapi_ok || opened_.nvml_ok) || opened_.pairing_error[0] || slot != opened_.slot) {
            error = "Could not verify selected GPU identity and pairing"; close(); return false;
        }
        last_.identity = opened_; last_.connected = true; rail_mode_ = false; last_.rail_mode_known = true; last_.rail_mode_enabled = false; rail_service_ = std::make_unique<RailControls>(api_.rails, gpu_, opened_);
        if (api_.clkdom_flags_set && api_.clkdom_flags && api_.clkdom_flags_set(gpu_, 0, 0) == DC_OK) { bool xoc = true; if (current_xoc(xoc) && !xoc) { last_.xoc_known = true; last_.xoc_enabled = false; } }
        auto i2c_api=api_.i2c;
        i2c_api.before_clock_acquire=[this](std::string& why) {
            if (!frequency_ || !frequency_->needs_cleanup()) return true;
            const auto result=release_frequency(); why=result.message; return result.ok;
        };
        i2c_session_ = std::make_unique<I2cSession>(std::move(i2c_api), gpu_, opened_);
        tuning_ = std::make_unique<TuningActions>(api_.tuning, gpu_, opened_);
        frequency_ = std::make_unique<FrequencyLock>(api_.frequency, gpu_, opened_);
        perf_ = std::make_unique<PerfViewService>(api_.perf, gpu_, opened_);
        last_.perf_diagnostics = std::make_shared<PerfViewModel>(perf_->model());
        error.clear(); return true;
    }
    ActionResult prepare_close() override {
        if (timings_ && timings_->pending()) { ActionResult r; r.message="Timing helper is still running; keeping the session open until its exit is confirmed"; return r; }
        if (i2c_session_ && i2c_session_->owned()) { auto result=i2c_session_->release(); last_.i2c=i2c_session_->data(); if (!result.ok) return result; }
        if (tuning_ && tuning_->state().maintenance_needed()) { const auto result=release_hold(); if (!result.ok) return result; }
        if (frequency_ && frequency_->needs_cleanup()) return release_frequency();
        ActionResult result; result.ok=true; return result;
    }
    bool maintenance_needed() const override {
        if (timings_ && timings_->pending()) return true;
        if (!tuning_) return false;
        const auto s=tuning_->state(); return s.owned || s.headroom || s.recovery;
    }
    std::optional<TuningState> tuning_state() const override { return tuning_ ? tuning_->state() : TuningState{}; }
    std::optional<FrequencyLockData> frequency_state() const override { return frequency_ ? frequency_->data() : FrequencyLockData{}; }
    ActionResult maintain() override {
        ActionResult out; out.ok=true; if (!maintenance_needed()) return out;
        if (timings_ && timings_->pending()) { out.ok=false; out.message="Waiting for timing helper exit; further operations are blocked"; return out; }
        const auto r=tuning_->maintain(); last_.tuning=tuning_->state();
        out.ok=r.ok; out.may_have_changed=r.may_have_changed; out.message=r.message; return out;
    }
    std::optional<I2cViewData> i2c_state() const override {
        if (!i2c_session_) return I2cViewData{};
        auto state=i2c_session_->data();
        if (i2c_session_->owned() && !state.clock_owned && !state.status.owned) state.status.recovery_pending=true;
        return state;
    }
    void close() override {
        if (!prepare_close().ok) return;
        clock_monitor_.reset(); clock_architecture_.reset(); clock_architecture_attempted_ = false;
        clock_step_measured_ = false; clock_step_khz_ = 0; clock_sample_ = 0;
        timings_.reset(); timing_transport_.reset(); timing_undo_.reset(); timing_batch_undo_.reset(); timing_data_={};
        tuning_.reset();
        frequency_.reset();
        perf_.reset();
        i2c_session_.reset(); rail_service_.reset(); rail_mode_ = false; if (gpu_ && api_.close) api_.close(gpu_); gpu_ = nullptr; opened_ = {}; last_ = {}; undo_.clear();
    }
    LiveData read(bool details) override {
        if (tuning_) last_.tuning=tuning_->state();
        if (frequency_) last_.frequency=details ? frequency_->read() : frequency_->data();
        last_.hold_available=api_.tuning.lock && api_.tuning.unlock && api_.tuning.identity && api_.tuning.vf_set && api_.tuning.vf_clear && api_.tuning.vf_status && api_.tuning.clock_status && api_.tuning.recovery_pending && api_.tuning.recover;
        last_.reset_controls_available=api_.reset_all && api_.tuning.vf_status && api_.tuning.clock_status;
        last_.max_it_available=last_.hold_available && api_.planner.ramp && api_.planner.predict && api_.fan_set && api_.fan_restore && api_.boost_set && power_api() && api_.apply_deltas;
        last_.timings=std::make_shared<TimingViewData>(timing_data_);
        if (i2c_session_) last_.i2c=i2c_session_->data();
        if (!gpu_) { last_.status = valid() ? "No GPU selected" : "Core DLL unavailable or incompatible"; clock_tick(nullptr); return last_; }
        dc_gpu_identity id{}; if (!identity(id)) { last_.connected = false; last_.status = "GPU identity read failed or changed"; clock_tick(nullptr); return last_; }
        if (details || !clock_architecture_attempted_) {
            clock_architecture_attempted_ = true;
            uint32_t architecture = 0;
            if (retry(api_.i2c.architecture, gpu_, &architecture) == DC_OK && architecture) clock_architecture_ = architecture;
        }
        last_.connected = true; last_.identity = id; last_.status.clear(); bool xoc_state = false; if (current_xoc(xoc_state)) { last_.xoc_known = true; last_.xoc_enabled = xoc_state; }
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
            bool xoc = false; if (effective_xoc(xoc) && api_.power_bounds(gpu_, &bounds_st, xoc ? 1 : 0, &lo, &hi, &bounds_err) == DC_OK && bounds_st.pl_def_state == DC_CL_STATIC_VALUE) power.stock_writable = bounds_st.pl_def_mw > 0 && bounds_st.pl_def_mw < UINT32_MAX && bounds_st.pl_def_mw >= lo && bounds_st.pl_def_mw <= hi;
        }
        int32_t has = 0; uint32_t mw = 0; dc_error err{}; err.size = sizeof(err);
        if (retry(api_.power, gpu_, &st, &has, &mw, &err) == DC_OK && has) {
            power.availability = Availability::available; power.current = mw / 1000.0; power.unit = "W";
            if (details) { PowerSnapshot captured; std::string reason; bool captured_ok = false;
                dc_current_row currents[32]{}; uint32_t current_count = 0; bool current_answered = false;
                if (power_api() && api_.lock(gpu_) == DC_OK) {
                    captured_ok = power_capture(captured, reason);
                    if (captured_ok && api_.current_limits) {
                        for (int attempt = 0; attempt < 2; ++attempt) { for (auto& row : currents) { row = {}; row.size = sizeof(row); } current_count = 0;
                            if (api_.current_limits(gpu_, confirm_clamp, this, currents, 32, &current_count) == DC_OK && current_count > 0 && current_count <= 32) { current_answered = true; break; }
                        }
                    }
                    api_.unlock(gpu_);
                }
                else reason = "Power policy interfaces or lock unavailable";
                if (captured_ok) { power.writable = true; power.detail = "Configured request";
                    const auto previous_policies = last_.power_policies;
                    last_.power_policies.clear();
                    unsigned candidates = 0; uint32_t candidate_id = 0;
                    if (current_answered) for (uint32_t i = 0; i < current_count; ++i) {
                        const auto& current = currents[i]; if (!std::memchr(current.label, 0, sizeof(current.label)) || std::strcmp(current.label, "Core current") != 0) continue;
                        const auto it = std::find_if(captured.rows.begin(), captured.rows.end(), [&](const dc_policy_row& row) { return row.policy == current.policy && row.channel == current.channel && row.unit == 1 && row.named_current && row.consistent && row.minimum == current.minimum_ma && row.maximum == current.maximum_ma && row.requested == current.requested_ma; });
                        if (it != captured.rows.end()) { ++candidates; candidate_id = current.policy; }
                    }
                    for (const auto& row : captured.rows) { PowerPolicy policy{}; policy.id = row.policy; policy.label = "Policy " + std::to_string(row.policy); policy.named_current = row.named_current != 0; policy.binding = policy_binding(row);
                        if (current_answered) policy.core_current = candidates == 1 && candidate_id == row.policy;
                        else if (row.named_current && row.unit == 1 && row.consistent) { auto old = std::find_if(previous_policies.begin(), previous_policies.end(), [&](const PowerPolicy& p) { return p.id == row.policy && p.binding == policy.binding && p.core_current; }); policy.core_current = old != previous_policies.end(); }
                        if (policy.core_current) policy.label = "Core current (NVVDD)"; policy.state.availability = Availability::available; policy.state.current = row.requested / 1000.0; policy.state.low = row.minimum / 1000.0; policy.state.high = row.maximum / 1000.0; policy.state.stock = row.default_value / 1000.0; policy.state.unit = row.unit == 0 ? "W" : row.unit == 1 ? "A" : "unknown"; policy.state.step = 0.001; policy.state.writable = !row.board && row.consistent && (row.writable || row.named_current) && (row.unit == 0 || row.unit == 1); policy.state.stock_writable = policy.state.writable && row.default_value >= row.minimum && row.default_value <= row.maximum; policy.state.detail = row.board ? "Board request is controlled separately" : row.consistent ? "Configured policy request" : "Policy record is inconsistent"; last_.power_policies.push_back(std::move(policy)); }
                } else { power.availability = Availability::transient; power.detail = reason; for (auto& policy : last_.power_policies) { policy.state.availability = Availability::transient; policy.state.detail = reason; } }
            }
            if (st.pl_min_state == DC_CL_STATIC_VALUE) power.low = std::min(st.pl_min_mw / 1000.0, *power.current);
            if (st.pl_max_state == DC_CL_STATIC_VALUE) power.high = std::max(st.pl_max_mw / 1000.0, *power.current);
            if (last_.xoc_known && last_.xoc_enabled && api_.envelopes) { dc_request_envelopes env{}; env.size = sizeof(env);
                if (api_.envelopes(&env) == DC_OK && env.power_limit_xoc_max_mw > 0 && env.power_limit_xoc_max_mw < UINT32_MAX && power.high) {
                    power.high = std::max(*power.high, env.power_limit_xoc_max_mw / 1000.0);
                    if (power.availability == Availability::available) power.detail = "XOC software envelope; the adapter's validated request route may allow less";
                }
            } else if (power.availability == Availability::available && power.detail.find("XOC software envelope;") == 0) power.detail = "Configured request";
            if (st.pl_def_state == DC_CL_STATIC_VALUE) power.stock = st.pl_def_mw / 1000.0;
        } else {
            failure(power, !api_.power, "Configured power request unavailable");
            if (details) for (auto& policy:last_.power_policies) {
                policy.state.availability=Availability::transient;
                policy.state.detail="Coupled power/current readback unavailable; showing last request";
            }
        }
        has = 0; mw = 0; if (retry(api_.enforced_power, gpu_, &has, &mw) == DC_OK && has) last_.enforced_power_w = mw / 1000.0; else last_.enforced_power_w.reset();
        read_advanced(details); if (details) read_rails();
        if (details) {
            dc_vf_delta_snapshot snapshot{}; std::vector<dc_vf_point> points; std::string why; uint32_t curve_step = 0; int32_t measured = 0;
            if (api_.lock && api_.unlock && api_.lock(gpu_) == DC_OK) {
                dc_vf_layout captured_layout{};
                const bool complete = curve_table(snapshot, points, why, &captured_layout);
                const bool grid = retry(api_.clock_step, gpu_, &curve_step, &measured) == DC_OK && curve_step; uint32_t max_rise = 0; if (retry(api_.max_rise, gpu_, &max_rise) != DC_OK) max_rise = 0;
                if (grid) { clock_step_khz_ = curve_step; clock_step_measured_ = measured != 0; }
                last_.curve_layout = complete ? captured_layout : dc_vf_layout{};
                api_.unlock(gpu_);
                dc_request_envelopes limits{}; limits.size = sizeof(limits); bool envelope = api_.envelopes && api_.envelopes(&limits) == DC_OK && limits.max_abs_delta_khz > 0;
                if (complete) { last_.curve_max_abs_delta_khz = envelope ? limits.max_abs_delta_khz : 0; last_.curve_model_max_rise_khz = max_rise; last_.curve = std::move(points); last_.curve_error.clear(); last_.curve_step_khz = grid ? curve_step : 0; last_.curve_freq_div = snapshot.freq_div; last_.curve_writable = api_.apply_deltas && envelope && grid; last_.curve_reset_writable = api_.apply_deltas && api_.reset_curve; }
                else { last_.curve.clear(); last_.curve_error = why; last_.curve_writable = false; last_.curve_reset_writable = false; last_.curve_step_khz = 0; last_.curve_freq_div = 0; last_.curve_max_abs_delta_khz = 0; last_.curve_model_max_rise_khz = 0; }
            } else { last_.curve.clear(); last_.curve_error = "Could not lock GPU for curve"; last_.curve_writable = false; last_.curve_reset_writable = false; last_.curve_step_khz = 0; last_.curve_freq_div = 0; last_.curve_max_abs_delta_khz = 0; last_.curve_model_max_rise_khz = 0; }
        }
        clock_tick(last_.telemetry_valid ? &t : nullptr);
        last_.undo_available = !undo_.empty(); last_.undo_label = undo_.empty() ? "" : std::string("Undo ") + (undo_.back().curve_entry ? "V/F curve" : undo_.back().advanced_entry ? "advanced offset" : undo_.back().rail_entry ? "rail limit" : label(undo_.back().control)); return last_;
    }
    ActionResult execute(const Command& cmd) override {
        ActionResult result{};
        if (!gpu_) { result.message = "No GPU selected"; return result; }
        if (timings_ && timings_->pending()) { result.message="Timing helper exit is unconfirmed; further operations are blocked"; return result; }
        if (is_frequency_operation(cmd.operation)) return frequency_action(cmd);
        if (cmd.operation == Command::Operation::refresh_perf_diagnostics) {
            if (!perf_) { result.message = "PERF diagnostics unavailable"; return result; }
            last_.perf_diagnostics = std::make_shared<PerfViewModel>(perf_->refresh());
            result.ok = std::all_of(last_.perf_diagnostics->groups.begin(), last_.perf_diagnostics->groups.end(),
                [](const auto& group) { return group.freshness == PerfFreshness::current; });
            result.message = result.ok ? "PERF diagnostics refreshed" : "PERF diagnostics refreshed; inspect individual group status";
            return result;
        }
        if (is_i2c_operation(cmd.operation)) {
            if (!i2c_session_) { result.message="I2C session unavailable"; return result; }
            // I2C validates the selected controller and owns its own temporary
            // hold. It must never release an unrelated/user-owned V/F request.
            result=i2c_session_->execute(cmd); last_.i2c=i2c_session_->data(); return result;
        }
        if (cmd.operation == Command::Operation::set_rail_mode) {
            dc_gpu_identity id{}; if (!identity(id)) { result.message = "GPU identity unavailable; rail mode unchanged"; return result; }
            rail_mode_ = cmd.enabled; last_.rail_mode_known = true; last_.rail_mode_enabled = rail_mode_; result.ok = true; result.message = rail_mode_ ? "Rail limits session mode enabled" : "Rail limits session mode disabled"; return result;
        }
        if (cmd.operation == Command::Operation::set_xoc) {
            dc_gpu_identity id{}; if (!identity(id) || !api_.clkdom_flags_set || !api_.clkdom_flags) { result.message = "XOC session gate unavailable"; return result; }
            if (api_.clkdom_flags_set(gpu_, cmd.enabled ? 1 : 0, cmd.enabled ? 1 : 0) != DC_OK) { result.message = "XOC session gate update failed"; return result; }
            bool actual = false; if (!current_xoc(actual) || actual != cmd.enabled) { last_.xoc_known = false; result.message = "XOC session gate readback unavailable or mismatched"; return result; }
            last_.xoc_known = true; last_.xoc_enabled = actual; result.ok = true; result.message = actual ? "XOC session mode enabled" : "XOC session mode disabled"; return result;
        }
        if (cmd.operation==Command::Operation::release_hold) return release_hold();
        if (cmd.operation == Command::Operation::prepare_scoped_profile || cmd.operation == Command::Operation::save_scoped_profile) return scoped_action(cmd);
        if (cmd.operation == Command::Operation::save_curve_profile) {
            if (cmd.profile_path.empty()) { result.message = "Choose a V/F profile path"; return result; }
            try {
                // Explicit save captures fresh configured requests, never the UI draft.
                const auto captured = read(true);
                write_profile(cmd.profile_path, capture_curve_profile(captured));
                result.ok = true; result.message = "Configured V/F curve saved. Loading stages a draft; Apply remains explicit.";
            } catch (const std::exception& error) { result.message = std::string("V/F profile save failed: ") + error.what(); }
            return result;
        }
        if ((cmd.operation==Command::Operation::reset_controls || cmd.operation==Command::Operation::max_it ||
             cmd.operation==Command::Operation::hold_curve_point) && frequency_ && frequency_->needs_cleanup()) {
            result=release_frequency(); if (!result.ok) return result;
        }
        if (cmd.operation==Command::Operation::reset_controls) return reset_action();
        if (i2c_session_ && i2c_session_->owned()) { result.message="Release the owned I2C request and clock clamp before other tuning writes"; return result; }
        if (cmd.operation==Command::Operation::max_it) return max_action(cmd);
        if (cmd.operation==Command::Operation::hold_curve_point) return hold_action(cmd);
        if (cmd.operation==Command::Operation::undo && maintenance_needed()) { result=release_hold(); if (!result.ok) return result; }
        if (maintenance_needed() && cmd.operation!=Command::Operation::save_profile && !is_timing_operation(cmd.operation)) {
            result.message="Release the owned V/F hold before changing tuning requests"; return result;
        }
        if (is_timing_operation(cmd.operation)) return timing_action(cmd);
        if (cmd.operation == Command::Operation::save_profile || cmd.operation == Command::Operation::load_profile) return profile_action(cmd);
        if (cmd.operation == Command::Operation::apply_scoped_profile) return scoped_action(cmd);
        if (cmd.operation == Command::Operation::apply_curve || cmd.operation == Command::Operation::reset_curve) return curve_action(cmd, false);
        if (cmd.operation == Command::Operation::undo && !undo_.empty() && undo_.back().curve_entry) return curve_action(cmd, true);
        if (cmd.operation == Command::Operation::undo && !undo_.empty() && undo_.back().rail_entry) return rail_action(cmd, true);
        if (cmd.rail && (cmd.operation == Command::Operation::apply || cmd.operation == Command::Operation::stock)) return rail_action(cmd, false);
        if (cmd.operation == Command::Operation::undo && !undo_.empty() && undo_.back().advanced_entry) return advanced_action(cmd, true);
        if (cmd.advanced && (cmd.operation == Command::Operation::apply || cmd.operation == Command::Operation::stock)) return advanced_action(cmd, false);
        if (cmd.power_policy && (cmd.operation == Command::Operation::apply || cmd.operation == Command::Operation::stock)) return execute_policy(cmd);
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
        if (control == ControlId::power) {
            result=execute_power(cmd,undoing);
            // Explicit power operations refresh their coupled policies even without
            // ordinary telemetry polling (including paused/minimized sessions).
            if (result.may_have_changed || result.ok) read(true);
            return result;
        }
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
            std::string why; if (!curve_table(prior.curve, prior.curve_points, why, &prior.curve_layout)) { result.message = "Complete V/F curve snapshot unavailable; no write issued"; return result; }
            if (undoing && (!same_curve_geometry(prior.curve, undo_.back().curve) || !same_curve_layout(prior.curve_layout, undo_.back().curve_layout) || !same_curve_mapping(undo_.back().curve_points, prior.curve_points, prior.curve.freq_div))) { result.message = "V/F geometry changed; core Undo retained with no write issued"; return result; }
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
            dc_vf_delta_snapshot got{}; dc_vf_layout got_layout{}; std::vector<dc_vf_point> got_points; std::string why;
            result.ok = curve_status == DC_OK && curve_out.ok && identity(check) && curve_table(got, got_points, why, &got_layout) && same_curve(saved, got) && same_curve_layout(undo_.back().curve_layout, got_layout) && same_curve_mapping(undo_.back().curve_points, got_points, saved.freq_div);
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
