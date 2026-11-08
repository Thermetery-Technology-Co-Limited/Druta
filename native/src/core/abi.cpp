// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI: thin, exception-free wrappers over the C++ core.
#include "drutacore/drutacore.h"

#include <algorithm>
#include <cstring>
#include <type_traits>
#include <new>
#include <optional>
#include <string>
#include <vector>

#include "backend/exports.h"
#include "backend/fan.h"
#include "backend/gpu.h"
#include "backend/telemetry.h"
#include "backend/vflock.h"
#include "backend/voltage.h"
#include "core/mutex.h"
#include "core/rail_transport.h"
#include "core/sleep.h"
#include "core/slot.h"

extern "C" {

DC_API uint32_t DC_CALL dc_abi_version(void) { return DC_ABI_VERSION; }

DC_API const char* DC_CALL dc_status_text(dc_status status) {
    switch (status) {
        case DC_OK: return "ok";
        case DC_E_ARGUMENT: return "invalid argument";
        case DC_E_BUFFER: return "buffer too small";
        case DC_E_PARSE: return "text does not have the expected shape";
        case DC_E_UNRECOGNIZED: return "payload is not an exact known rail GET packet";
        case DC_E_MASK: return "requested_mask must select rail 0, rail 1, or both";
        case DC_E_RECORDS: return "requested_mask must exactly match the record rails";
        case DC_E_RANGE: return "value out of range";
        case DC_E_UNAVAILABLE: return "not provided by this adapter";
        default: return "unknown status";
    }
}

DC_API dc_status DC_CALL dc_parse_slot(const char* text, uint32_t* domain, uint32_t* bus, uint32_t* device,
                                       uint32_t* function) {
    if (!text || !domain || !bus || !device || !function) return DC_E_ARGUMENT;
    auto slot = druta::parse_slot(text);
    if (!slot) return DC_E_PARSE;
    *domain = slot->domain;
    *bus = slot->bus;
    *device = slot->device;
    *function = slot->function;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_format_slot(uint32_t domain, uint32_t bus, uint32_t device, uint32_t function,
                                        char* buffer, size_t size, size_t* needed) {
    std::string text = druta::format_slot(domain, bus, device, function);
    if (needed) *needed = text.size() + 1;
    if (!buffer) return DC_E_ARGUMENT;
    if (size < text.size() + 1) return DC_E_BUFFER;
    std::memcpy(buffer, text.c_str(), text.size() + 1);
    return DC_OK;
}

DC_API int32_t DC_CALL dc_same_slot(const char* a, const char* b) {
    if (!a || !b) return 0;
    return druta::same_slot(a, b) ? 1 : 0;
}

// ---- voltage-rail packets -----------------------------------------------------------------------

namespace {

dc_status copy_out(const std::vector<uint8_t>& packet, uint8_t* out, size_t out_size, size_t* needed) {
    if (needed) *needed = packet.size();
    if (!out) return DC_E_ARGUMENT;
    if (out_size < packet.size()) return DC_E_BUFFER;
    if (!packet.empty()) std::memcpy(out, packet.data(), packet.size());
    return DC_OK;
}

dc_status copy_text(const std::string& text, char* buffer, size_t buffer_size, size_t* needed) {
    if (needed) *needed = text.size() + 1;
    if (!buffer) return DC_E_ARGUMENT;
    if (buffer_size < text.size() + 1) return DC_E_BUFFER;
    std::memcpy(buffer, text.c_str(), text.size() + 1);
    return DC_OK;
}

}  // namespace

DC_API uint32_t DC_CALL dc_rail_layout_count(void) {
    return static_cast<uint32_t>(druta::rail::known_layouts().size());
}

DC_API dc_status DC_CALL dc_rail_layout_at(uint32_t index, dc_rail_layout* layout) {
    const auto& layouts = druta::rail::known_layouts();
    if (!layout || index >= layouts.size()) return DC_E_ARGUMENT;
    const druta::rail::Layout& source = layouts[index];
    layout->packet_size = source.packet_size;
    layout->params_size = source.params_size;
    layout->get_command = source.get_command;
    layout->set_command = source.set_command;
    layout->mask_word = source.mask_word;
    layout->boost_word = source.boost_word;
    layout->record0 = source.record0;
    layout->record_stride = source.record_stride;
    layout->record_type = source.record_type;
    layout->valid_word = source.valid_word ? static_cast<int32_t>(*source.valid_word) : -1;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rail_recognize(const uint8_t* payload, size_t size, uint32_t requested_mask,
                                           uint32_t* layout_index) {
    if ((!payload && size) || !layout_index) return DC_E_ARGUMENT;
    auto index = druta::rail::recognize_packet(druta::ByteView(payload, size), requested_mask);
    if (!index) return DC_E_UNRECOGNIZED;
    *layout_index = static_cast<uint32_t>(*index);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rail_prepare_write(const uint8_t* payload, size_t size, uint32_t requested_mask,
                                               const dc_rail_record* records, size_t record_count, uint32_t boost,
                                               uint8_t* out, size_t out_size, size_t* needed) {
    if ((!payload && size) || (!records && record_count)) return DC_E_ARGUMENT;
    std::vector<druta::rail::Record> native(record_count);
    for (size_t i = 0; i < record_count; ++i) {
        native[i].rail = records[i].rail;
        for (size_t v = 0; v < 4; ++v) native[i].values[v] = records[i].values[v];
    }
    std::vector<uint8_t> packet;
    switch (druta::rail::prepare_write(druta::ByteView(payload, size), requested_mask, native.data(), record_count,
                                       boost, packet)) {
        case druta::rail::Error::none: break;
        case druta::rail::Error::mask: return DC_E_MASK;
        case druta::rail::Error::records: return DC_E_RECORDS;
        case druta::rail::Error::boost: return DC_E_RANGE;
        case druta::rail::Error::unrecognized: return DC_E_UNRECOGNIZED;
    }
    return copy_out(packet, out, out_size, needed);
}

DC_API dc_status DC_CALL dc_rail_packet_summary(const uint8_t* payload, size_t size, char* buffer, size_t buffer_size,
                                                size_t* needed) {
    if (!payload && size) return DC_E_ARGUMENT;
    return copy_text(druta::rail::packet_summary(druta::ByteView(payload, size)), buffer, buffer_size, needed);
}

// ---- GPU identity ---------------------------------------------------------------------------------

namespace {

void copy_field(char* field, size_t size, const std::string& text) {
    size_t n = text.size() < size - 1 ? text.size() : size - 1;
    std::memcpy(field, text.data(), n);
    field[n] = '\0';
}

#define DC_COPY(field, text) copy_field(field, sizeof(field), text)

}  // namespace

struct dc_gpu {
    druta::backend::Gpu gpu;
    druta::backend::Telemetry telemetry;
    druta::backend::VfLock vflock;  // carries the recovery record across calls, as GPU._vf_lock_recovery
    std::optional<druta::backend::StaticCapabilities> statics;  // GPU.static, read once like Python's
    explicit dc_gpu(const std::string& slot) : gpu(slot), telemetry(gpu), vflock(gpu, telemetry) {}
    const druta::backend::StaticCapabilities& capabilities() {
        if (!statics) statics = telemetry.read_static();
        return *statics;
    }
    druta::backend::FanControl fan() { return druta::backend::FanControl(gpu, telemetry, capabilities()); }
};

DC_API void DC_CALL dc_set_export_resolver(dc_resolve_fn resolve, void* context) {
    druta::backend::set_export_resolver(resolve, context);
}

DC_API dc_status DC_CALL dc_enumerate_gpus(dc_gpu_entry* entries, size_t capacity, size_t* count) {
    if (!count || (!entries && capacity)) return DC_E_ARGUMENT;
    std::vector<druta::backend::GpuListEntry> gpus = druta::backend::enumerate_gpus();
    *count = gpus.size();
    if (gpus.size() > capacity) return DC_E_BUFFER;
    for (size_t i = 0; i < gpus.size(); ++i) {
        if (entries[i].size != sizeof(dc_gpu_entry)) return DC_E_ARGUMENT;
        const druta::backend::GpuListEntry& g = gpus[i];
        DC_COPY(entries[i].slot, g.slot);
        DC_COPY(entries[i].name, g.name);
        DC_COPY(entries[i].uuid, g.uuid);
        entries[i].has_devid = g.devid ? 1 : 0;
        entries[i].devid = g.devid.value_or(0);
        entries[i].subsys = g.subsys.value_or(0);
        entries[i].nvml_index = g.nvml_index ? static_cast<int32_t>(*g.nvml_index) : -1;
        entries[i].has_nvapi = g.has_nvapi ? 1 : 0;
    }
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_open(const char* slot, dc_gpu** gpu) {
    if (!gpu) return DC_E_ARGUMENT;
    *gpu = new (std::nothrow) dc_gpu(slot ? slot : "");
    return *gpu ? DC_OK : DC_E_BUFFER;
}

DC_API void DC_CALL dc_gpu_close(dc_gpu* gpu) { delete gpu; }

DC_API dc_status DC_CALL dc_gpu_identity_get(const dc_gpu* handle, dc_gpu_identity* out) {
    if (!handle || !out || out->size != sizeof(dc_gpu_identity)) return DC_E_ARGUMENT;
    const druta::backend::Gpu& gpu = handle->gpu;
    out->nvapi_ok = gpu.nvapi.ok ? 1 : 0;
    out->nvml_ok = gpu.nvml.ok ? 1 : 0;
    DC_COPY(out->nvapi_error, gpu.nvapi.err_detail);
    DC_COPY(out->nvml_error, gpu.nvml.err_detail);
    DC_COPY(out->nvml_path, gpu.nvml.dll_path);
    DC_COPY(out->pairing_error, gpu.pairing_error);
    DC_COPY(out->requested_slot, gpu.requested_slot);
    DC_COPY(out->slot, gpu.slot());
    out->nvapi_selected = gpu.nvapi.selected ? 1 : 0;
    DC_COPY(out->nvapi_slot, gpu.nvapi.selected ? gpu.nvapi.selected->slot : std::string());
    out->nvapi_devid = gpu.nvapi.selected ? gpu.nvapi.selected->devid : 0;
    out->nvapi_subsys = gpu.nvapi.selected ? gpu.nvapi.selected->subsys : 0;
    out->nvml_selected = gpu.nvml.selected ? 1 : 0;
    DC_COPY(out->nvml_slot, gpu.nvml.selected ? gpu.nvml.selected->slot : std::string());
    out->nvml_has_devid = gpu.nvml.selected && gpu.nvml.selected->devid ? 1 : 0;
    out->nvml_devid = gpu.nvml.selected ? gpu.nvml.selected->devid.value_or(0) : 0;
    out->nvml_subsys = gpu.nvml.selected ? gpu.nvml.selected->subsys.value_or(0) : 0;
    out->nvml_index = gpu.nvml.selected ? static_cast<int32_t>(gpu.nvml.selected->nvml_index) : -1;
    DC_COPY(out->name, gpu.identity.name);
    DC_COPY(out->driver, gpu.identity.driver);
    DC_COPY(out->vbios, gpu.identity.vbios);
    DC_COPY(out->uuid, gpu.identity.uuid);
    out->admin = gpu.identity.admin ? 1 : 0;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_status_line(const dc_gpu* handle, char* buffer, size_t size, size_t* needed) {
    if (!handle) return DC_E_ARGUMENT;
    return copy_text(handle->gpu.status_line(), buffer, size, needed);
}

// ---- telemetry ------------------------------------------------------------------------------------

DC_API dc_status DC_CALL dc_gpu_architecture(dc_gpu* handle, uint32_t* architecture) {
    if (!handle || !architecture) return DC_E_ARGUMENT;
    auto value = handle->telemetry.arch();
    if (!value) return DC_E_UNRECOGNIZED;
    *architecture = *value;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_read_telemetry(dc_gpu* handle, dc_telemetry* out) {
    if (!handle || !out || out->size != sizeof(dc_telemetry)) return DC_E_ARGUMENT;
    druta::backend::Snapshot d = handle->telemetry.read();
    dc_telemetry t{};
    t.size = sizeof(dc_telemetry);
    auto set = [&](uint64_t bit, bool present) { if (present) t.present |= bit; };
    auto put = [&](auto& field, const auto& source, uint64_t bit) {
        if (source) { field = static_cast<std::remove_reference_t<decltype(field)>>(*source); t.present |= bit; }
    };
    put(t.core, d.core, DC_T_CORE);
    put(t.mem, d.mem, DC_T_MEM);
    put(t.video, d.video, DC_T_VIDEO);
    put(t.xbar, d.xbar, DC_T_XBAR);
    put(t.core_off, d.core_off, DC_T_CORE_OFF);
    put(t.mem_off, d.mem_off, DC_T_MEM_OFF);
    if (d.priv_clocks) {
        std::memcpy(t.priv_clocks, d.priv_clocks->w, sizeof t.priv_clocks);
        t.present |= DC_T_PRIV_CLOCKS;
    }
    put(t.temp_edge, d.temp_edge, DC_T_TEMP_EDGE);
    if (d.temp_hotspot) {
        t.temp_hotspot = *d.temp_hotspot;
        t.temp_sensor_count = static_cast<uint32_t>(std::min<size_t>(d.temp_sensors.size(), 32));
        for (uint32_t i = 0; i < t.temp_sensor_count; ++i) t.temp_sensors[i] = d.temp_sensors[i];
        t.present |= DC_T_TEMP_HOTSPOT;
    }
    put(t.temp_delta, d.temp_delta, DC_T_TEMP_DELTA);
    put(t.pl_requested_mw, d.pl_requested_mw, DC_T_PL_REQUESTED);
    put(t.power_w, d.power_w, DC_T_POWER_W);
    put(t.pl_now_mw, d.pl_now_mw, DC_T_PL_NOW);
    put(t.pwr_gpu_pct, d.pwr_gpu_pct, DC_T_PWR_GPU_PCT);
    put(t.pwr_board_pct, d.pwr_board_pct, DC_T_PWR_BOARD_PCT);
    put(t.pl_target_pct, d.pl_target_pct, DC_T_PL_TARGET_PCT);
    put(t.vcore_mv, d.vcore_mv, DC_T_VCORE_MV);
    put(t.num_fans, d.num_fans, DC_T_NUM_FANS);
    t.fan_count = static_cast<uint32_t>(std::min<size_t>(d.fans.size(), 32));
    for (uint32_t i = 0; i < t.fan_count; ++i) {
        t.fans[i].has_duty = d.fans[i].duty ? 1 : 0;
        t.fans[i].duty = d.fans[i].duty.value_or(0);
        t.fans[i].has_rpm = d.fans[i].rpm ? 1 : 0;
        t.fans[i].rpm = d.fans[i].rpm.value_or(0);
    }
    put(t.util_gpu, d.util_gpu, DC_T_UTIL_GPU);
    put(t.util_fb, d.util_fb, DC_T_UTIL_FB);
    put(t.util_vid, d.util_vid, DC_T_UTIL_VID);
    put(t.util_bus, d.util_bus, DC_T_UTIL_BUS);
    put(t.event_mask, d.event_mask, DC_T_EVENT_MASK);
    put(t.perf_decrease, d.perf_decrease, DC_T_PERF_DECREASE);
    put(t.pcie_gen, d.pcie_gen, DC_T_PCIE_GEN);
    put(t.pcie_width, d.pcie_width, DC_T_PCIE_WIDTH);
    if (d.pcie_err) {
        static const char* const names[11] = {"correctable", "naks_rx", "receiver", "bad_tlp", "naks_tx", "bad_dllp",
                                              "non_fatal", "fatal", "unsupp_req", "lcrc", "lane"};
        for (size_t i = 0; i < 11; ++i) {
            auto it = d.pcie_err->find(names[i]);
            if (it == d.pcie_err->end()) continue;
            t.pcie_err[i].present = 1;
            t.pcie_err[i].type = it->second.type;
            t.pcie_err[i].d = it->second.d;
            t.pcie_err[i].u = it->second.u;
            t.pcie_err[i].s = it->second.s;
        }
        t.pcie_err_total = d.pcie_err_total.value_or(0);
        t.pcie_err_since = d.pcie_err_since.value_or(0);
        t.present |= DC_T_PCIE_ERR;
    }
    put(t.pstate, d.pstate, DC_T_PSTATE);
    put(t.vboost_pct, d.vboost_pct, DC_T_VBOOST);
    put(t.core_p0max, d.core_p0max, DC_T_CORE_P0MAX);
    put(t.mem_p0max, d.mem_p0max, DC_T_MEM_P0MAX);
    put(t.energy_j, d.energy_j, DC_T_ENERGY_J);
    if (d.vf_locked_domains) {
        t.vf_locked_count = static_cast<uint32_t>(std::min<size_t>(d.vf_locked_domains->size(), 32));
        for (uint32_t i = 0; i < t.vf_locked_count; ++i) t.vf_locked_domains[i] = (*d.vf_locked_domains)[i];
        t.present |= DC_T_VF_LOCKED;
    }
    put(t.vf_lock_mv, d.vf_lock_mv, DC_T_VF_LOCK_MV);
    if (d.clk_lock_mhz) {
        t.clk_lock_min_mhz = d.clk_lock_mhz->first;
        t.clk_lock_max_mhz = d.clk_lock_mhz->second;
        t.present |= DC_T_CLK_LOCK;
    }
    set(0, false);
    *out = t;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_capabilities_get(dc_gpu* handle, dc_capabilities* out) {
    if (!handle || !out || out->size != sizeof(dc_capabilities)) return DC_E_ARGUMENT;
    druta::backend::StaticCapabilities s = handle->telemetry.read_static();
    dc_capabilities c{};
    c.size = sizeof(dc_capabilities);
    auto put = [&](uint32_t& field, const std::optional<uint32_t>& source, uint32_t bit) {
        if (source) { field = *source; c.present |= bit; }
    };
    put(c.pl_min_mw, s.pl_min_mw, DC_C_PL_MIN);
    put(c.pl_max_mw, s.pl_max_mw, DC_C_PL_MAX);
    put(c.pl_def_mw, s.pl_def_mw, DC_C_PL_DEF);
    c.mem_clock_count = static_cast<uint32_t>(s.mem_clocks.size());
    for (size_t i = 0; i < std::min<size_t>(s.mem_clocks.size(), 512); ++i) c.mem_clocks[i] = s.mem_clocks[i];
    put(c.gfx_min, s.gfx_min, DC_C_GFX_MIN);
    put(c.gfx_max, s.gfx_max, DC_C_GFX_MAX);
    put(c.fan_min, s.fan_min, DC_C_FAN_MIN);
    put(c.fan_max, s.fan_max, DC_C_FAN_MAX);
    put(c.mem_type_id, s.mem_type_id, DC_C_MEM_TYPE_ID);
    DC_COPY(c.mem_type, s.mem_type);
    put(c.mem_div, s.mem_div, DC_C_MEM_DIV);
    auto range = [](dc_offset_range& field, const std::optional<druta::backend::OffsetRange>& source) {
        field.present = source ? 1 : 0;
        if (source) { field.low = source->low; field.high = source->high; field.current = source->current; }
    };
    range(c.core_off_range, s.core_off_range);
    range(c.mem_off_range, s.mem_off_range);
    *out = c;
    return DC_OK;
}


// ---- fan control ----------------------------------------------------------------------------------

namespace {

void put_outcome(const druta::backend::Outcome& outcome, dc_outcome* out) {
    out->ok = outcome.ok ? 1 : 0;
    DC_COPY(out->message, outcome.message);
}

void put_fan_state(const druta::backend::NativeFanState& state, dc_fan_state* out) {
    dc_fan_state s{};
    s.size = sizeof(dc_fan_state);
    DC_COPY(s.source, state.source);
    s.count = static_cast<uint32_t>(std::min<size_t>(state.fans.size(), 32));
    for (size_t i = 0; i < s.count; ++i) {
        const druta::backend::NativeFan& fan = state.fans[i];
        dc_fan_row& row = s.fans[i];
        row.id = fan.id;
        row.level = fan.level;
        row.policy = fan.policy;
        row.default_policy = fan.default_policy;
        row.manual = fan.manual ? 1 : 0;
        row.min = fan.min;
        row.max = fan.max;
        row.has_rpm = fan.rpm ? 1 : 0;
        row.rpm = fan.rpm.value_or(0);
    }
    *out = s;
}

}  // namespace

DC_API dc_status DC_CALL dc_gpu_fan_state_get(dc_gpu* handle, dc_fan_state* out) {
    if (!handle || !out || out->size != sizeof(dc_fan_state)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    std::optional<druta::backend::NativeFanState> state = handle->fan().read_fan_control_state();
    if (!state) return DC_E_UNAVAILABLE;
    put_fan_state(*state, out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_fan_manual_get(dc_gpu* handle, int32_t* manual) {
    if (!handle || !manual) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    std::optional<bool> value = handle->fan().read_fan_manual();
    if (!value) return DC_E_UNAVAILABLE;
    *manual = *value ? 1 : 0;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_fan_capabilities_get(dc_gpu* handle, dc_fan_capabilities* out) {
    if (!handle || !out || out->size != sizeof(dc_fan_capabilities)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::FanCapabilities caps = handle->fan().fan_capabilities();
    dc_fan_capabilities c{};
    c.size = sizeof(dc_fan_capabilities);
    c.manual = caps.manual ? 1 : 0;
    c.automatic = caps.automatic ? 1 : 0;
    DC_COPY(c.source, caps.source);
    c.min = caps.min;
    c.max = caps.max;
    *out = c;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_fan_set(dc_gpu* handle, int32_t percent, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->fan().set_fan(percent), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_fan_reset(dc_gpu* handle, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->fan().reset_fan(), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_fan_restore(dc_gpu* handle, const dc_fan_snapshot* state, dc_outcome* out) {
    if (!handle || !state || !out || state->size != sizeof(dc_fan_snapshot)) return DC_E_ARGUMENT;
    if (!state->is_mapping) {
        // Python: "if not current or not isinstance(state, dict)" — the same refusal either way.
        put_outcome({false, "fan control state unavailable"}, out);
        return DC_OK;
    }
    druta::backend::FanSnapshot snapshot;
    snapshot.source.assign(state->source, strnlen(state->source, sizeof(state->source)));
    snapshot.fans.resize(state->count);  // rows past 32 stay default: a count no card has is refused first
    for (size_t i = 0; i < std::min<size_t>(state->count, 32); ++i) {
        const dc_fan_snapshot_row& in = state->fans[i];
        druta::backend::FanSnapshotRow& row = snapshot.fans[i];
        row.has_manual = in.has_manual != 0;
        row.manual = in.manual != 0;
        row.has_id = in.has_id != 0;
        row.id = in.id;
        row.level_valid = in.level_valid != 0;
        row.level = in.level;
        row.has_policy = in.has_policy != 0;
        row.policy = in.policy;
    }
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->fan().restore_fan_control_state(snapshot), out);
    return DC_OK;
}

DC_API void DC_CALL dc_set_sleep_hook(dc_sleep_fn hook) { druta::set_sleep_hook(hook); }


// ---- voltage boost --------------------------------------------------------------------------------

DC_API dc_status DC_CALL dc_gpu_voltage_boost_get(dc_gpu* handle, int32_t* percent) {
    if (!handle || !percent) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto value = handle->telemetry.read_voltage_boost();
    if (!value) return DC_E_UNAVAILABLE;
    *percent = *value;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_voltage_boost_set(dc_gpu* handle, int64_t percent, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(druta::backend::set_voltage_boost(handle->gpu, percent), out);
    return DC_OK;
}

// ---- V/F point lock and frequency lock ------------------------------------------------------------

namespace {
std::optional<int64_t> optional_arg(int32_t has, int64_t value) {
    return has ? std::optional<int64_t>(value) : std::nullopt;
}
}  // namespace

DC_API dc_status DC_CALL dc_gpu_vf_lock_status(dc_gpu* handle, int32_t has_domain, int64_t domain, dc_vf_lock_status* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto status = handle->vflock.read_vf_lock_status(optional_arg(has_domain, domain));
    dc_vf_lock_status s{};
    if (status.value) {
        s.locked = 1;
        s.lock = {status.value->domain, status.value->lockMode, status.value->volt_uV, status.value->count};
    }
    DC_COPY(s.error, status.error.value_or(""));
    *out = s;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clk_lock_get(dc_gpu* handle, uint32_t* min_mhz, uint32_t* max_mhz) {
    if (!handle || !min_mhz || !max_mhz) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto range = handle->vflock.read_clk_lock();
    if (!range) return DC_E_UNAVAILABLE;
    *min_mhz = range->min_mhz;
    *max_mhz = range->max_mhz;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clk_lock_status(dc_gpu* handle, dc_clk_lock_status* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto status = handle->vflock.read_clk_lock_status();
    dc_clk_lock_status s{};
    if (status.value) {
        s.locked = 1;
        s.min_mhz = status.value->min_mhz;
        s.max_mhz = status.value->max_mhz;
    }
    DC_COPY(s.error, status.error.value_or(""));
    *out = s;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_vf_lock_set(dc_gpu* handle, int64_t volt_uv, int32_t has_domain, int64_t domain,
                                            dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->vflock.set_vf_lock(volt_uv, optional_arg(has_domain, domain)), out);
    return DC_OK;
}

DC_API int32_t DC_CALL dc_gpu_vf_lock_recovery_pending(const dc_gpu* handle) {
    return handle && handle->vflock.recovery_pending() ? 1 : 0;
}

DC_API dc_status DC_CALL dc_gpu_vf_lock_recover(dc_gpu* handle, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->vflock.recover_vf_lock(), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_vf_lock_clear(dc_gpu* handle, int32_t has_domain, int64_t domain, int32_t has_expected,
                                              int64_t expected_uv, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->vflock.clear_vf_lock(optional_arg(has_domain, domain), optional_arg(has_expected, expected_uv)), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_vf_lock_self_test(dc_gpu* handle, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->vflock.self_test(), out);
    return DC_OK;
}

}  // extern "C"
