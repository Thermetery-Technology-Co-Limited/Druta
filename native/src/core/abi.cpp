// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI: thin, exception-free wrappers over the C++ core.
#include "drutacore/drutacore.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <map>
#include <type_traits>
#include <new>
#include <optional>
#include <string>
#include <vector>

#include "backend/clocks.h"
#include "backend/exports.h"
#include "backend/fan.h"
#include "backend/gpu.h"
#include "backend/telemetry.h"
#include "backend/vfcurve.h"
#include "backend/vflock.h"
#include "backend/voltage.h"
#include "core/abi_internal.h"
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
        case DC_E_FAILED: return "the operation failed; see the error record";
        case DC_E_CALLER: return "a caller-supplied callback reported a failure; nothing was written";
        case DC_E_STATE: return "not allowed in the current state: the calling thread holds no level of the lock taken through dc_gpu_lock";
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

using druta::abi::optional_arg;
using druta::abi::put_outcome;

DC_API void DC_CALL dc_set_export_resolver(dc_resolve_fn resolve, void* context) {
    druta::backend::set_export_resolver(resolve, context);
}

DC_API dc_status DC_CALL dc_export_address(const char* library, const char* name, void** out) {
    if (!library || !name || !*name || !out ||
        (std::strcmp(library, "nvapi") != 0 && std::strcmp(library, "nvml") != 0)) return DC_E_ARGUMENT;
    *out = nullptr;
    try {
        *out = druta::backend::export_address(library, name);
        return *out ? DC_OK : DC_E_UNRECOGNIZED;
    } catch (...) {
        return DC_E_FAILED;
    }
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
    // new (std::nothrow) covers only the allocation: the constructors (the slot string, the
    // enumeration, the readers' members) can still throw, and no exception crosses the C boundary.
    try {
        *gpu = new (std::nothrow) dc_gpu(slot ? slot : "");
    } catch (...) {
        *gpu = nullptr;
        return DC_E_FAILED;
    }
    return *gpu ? DC_OK : DC_E_BUFFER;
}

DC_API void DC_CALL dc_gpu_close(dc_gpu* gpu) { delete gpu; }

DC_API dc_status DC_CALL dc_gpu_lock(dc_gpu* handle) {
    if (!handle) return DC_E_ARGUMENT;
    try {
        handle->gpu.lock().lock_exported();
        return DC_OK;
    } catch (...) {  // std::recursive_mutex::lock's std::system_error: nothing was taken
    }
    return DC_E_FAILED;
}

DC_API dc_status DC_CALL dc_gpu_trylock(dc_gpu* handle, int32_t* acquired) {
    if (!handle || !acquired) return DC_E_ARGUMENT;
    *acquired = handle->gpu.lock().try_lock_exported() ? 1 : 0;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_unlock(dc_gpu* handle) {
    if (!handle) return DC_E_ARGUMENT;
    // Only a level taken through dc_gpu_lock / dc_gpu_trylock: never the level of the entry whose
    // callback this may be running in.
    return handle->gpu.lock().unlock_exported() ? DC_OK : DC_E_STATE;
}

DC_API dc_status DC_CALL dc_gpu_lock_held(const dc_gpu* handle, int32_t* held) {
    if (!handle || !held) return DC_E_ARGUMENT;
    // Read-only: Gpu::lock() has no const overload, and owned_by_caller() changes nothing.
    *held = const_cast<dc_gpu*>(handle)->gpu.lock().owned_by_caller() ? 1 : 0;
    return DC_OK;
}

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
    // The selected record's handle, which a pairing failure leaves in place (nvbackend.GPU._pair keeps
    // NvAPI.selected while NvAPI.disable clears NvAPI.gpu); the binding derives NvAPI.gpu from nvapi_ok.
    out->nvapi_handle = static_cast<uint64_t>(
        reinterpret_cast<std::uintptr_t>(gpu.nvapi.selected ? gpu.nvapi.selected->handle : nullptr));
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

DC_API dc_status DC_CALL dc_gpu_nvml_device(const dc_gpu* handle, void** out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    try {
        druta::LockGuard guard(const_cast<dc_gpu*>(handle)->gpu.lock());
        *out = handle->gpu.nvml.ok ? handle->gpu.nvml.dev : nullptr;
        return DC_OK;
    } catch (...) {
        return DC_E_FAILED;
    }
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

DC_API dc_status DC_CALL dc_gpu_read_vcore_mv(dc_gpu* handle, int32_t* has, double* mv) {
    if (!handle || !has || !mv) return DC_E_ARGUMENT;
    try {
        druta::LockGuard guard(handle->gpu.lock());
        auto value = handle->telemetry.read_vcore_mv();
        *has = value ? 1 : 0;
        *mv = value.value_or(0.0);
        return DC_OK;
    } catch (...) {  // no exception crosses the C boundary
    }
    return DC_E_FAILED;
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
    t.pl_from_live = d.pl_from_live ? 1 : 0;
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

DC_API dc_status DC_CALL dc_gpu_clock_offset_range_get(dc_gpu* handle, uint32_t ctype, dc_offset_range* out) {
    if (!handle || !out || (ctype != 0 && ctype != 2)) return DC_E_ARGUMENT;
    try {
        druta::LockGuard guard(handle->gpu.lock());
        const auto range = handle->telemetry.offset_range(ctype);
        if (!range) return DC_E_UNAVAILABLE;
        const dc_offset_range result{1, range->low, range->high, range->current};
        *out = result;
        return DC_OK;
    } catch (...) {
        return DC_E_FAILED;
    }
}

DC_API dc_status DC_CALL dc_gpu_capabilities_get(dc_gpu* handle, dc_capabilities* out) {
    if (!handle || !out || out->size != sizeof(dc_capabilities)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    const druta::backend::StaticCapabilities& s = handle->capabilities();  // read once per handle, as GPU.static
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
DC_API void DC_CALL dc_set_clock_hook(dc_clock_fn hook) { druta::set_clock_hook(hook); }


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

// verification_p0's accessors. Each body is guarded (no exception crosses the C boundary; only
// allocation can throw) and runs under the handle lock.

DC_API dc_status DC_CALL dc_gpu_vf_lock_recovery_get(dc_gpu* handle, dc_vf_lock_recovery* out) {
    if (!handle || !out || out->size != sizeof(dc_vf_lock_recovery)) return DC_E_ARGUMENT;
    try {
        druta::LockGuard guard(handle->gpu.lock());
        dc_vf_lock_recovery r{};
        r.size = sizeof(dc_vf_lock_recovery);
        if (auto record = handle->vflock.recovery()) {
            r.pending = 1;
            r.domain = record->domain;
            r.previous_mode = record->previous.first;
            r.previous_uv = record->previous.second;
            r.requested_mode = record->requested.first;
            r.requested_uv = record->requested.second;
            r.verification = record->verification ? 1 : 0;
        }
        *out = r;
        return DC_OK;
    } catch (...) {
    }
    return DC_E_FAILED;
}

DC_API dc_status DC_CALL dc_gpu_vf_lock_recovery_set(dc_gpu* handle, const dc_vf_lock_recovery* record) {
    if (!handle || !record || record->size != sizeof(dc_vf_lock_recovery)) return DC_E_ARGUMENT;
    try {
        druta::LockGuard guard(handle->gpu.lock());
        std::optional<druta::backend::RecoveryRecord> value;
        if (record->pending) {
            druta::backend::RecoveryRecord r;
            r.domain = record->domain;
            r.previous = {record->previous_mode, record->previous_uv};
            r.requested = {record->requested_mode, record->requested_uv};
            r.verification = record->verification != 0;
            value = r;
        }
        handle->vflock.set_recovery(value);
        return DC_OK;
    } catch (...) {
    }
    return DC_E_FAILED;
}

DC_API dc_status DC_CALL dc_gpu_vf_lock_entry_get(dc_gpu* handle, int64_t domain, dc_vf_point_lock* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    try {
        druta::LockGuard guard(handle->gpu.lock());
        auto e = handle->vflock.entry(domain);
        if (!e) return DC_E_UNAVAILABLE;
        *out = dc_vf_point_lock{e->domain, e->lockMode, e->volt_uV, e->count};
        return DC_OK;
    } catch (...) {
    }
    return DC_E_FAILED;
}


// ---- V/F curve ------------------------------------------------------------------------------------

DC_API dc_status DC_CALL dc_gpu_vf_layout(dc_gpu* handle, int32_t force, dc_vf_layout* out) {
    if (!handle || !out || out->size != sizeof(dc_vf_layout)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::VfCurve& curve = handle->curve();
    auto lay = curve.layout(force != 0);
    dc_vf_layout l{};
    l.size = sizeof(dc_vf_layout);
    if (!lay) {
        DC_COPY(l.error, curve.layout_error());
        *out = l;
        return DC_E_UNAVAILABLE;
    }
    l.n_entries = lay->n_entries;
    l.gpu_count = static_cast<uint32_t>(std::min<size_t>(lay->gpu_idx.size(), DC_VFP_POINTS));
    for (uint32_t i = 0; i < l.gpu_count; ++i) l.gpu_idx[i] = lay->gpu_idx[i];
    l.other_count = static_cast<uint32_t>(std::min<size_t>(lay->other_idx.size(), DC_VFP_POINTS));
    for (uint32_t i = 0; i < l.other_count; ++i) l.other_idx[i] = lay->other_idx[i];
    l.freq_div = lay->freq_div;
    DC_COPY(l.notes, lay->notes);
    *out = l;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_vf_curve_read(dc_gpu* handle, dc_vf_curve* out) {
    if (!handle || !out || out->size != sizeof(dc_vf_curve)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::CurveResult result = handle->curve().read_vf_curve();
    dc_vf_curve c{};
    c.size = sizeof(dc_vf_curve);
    if (result.points) {
        c.count = static_cast<uint32_t>(std::min<size_t>(result.points->size(), DC_VFP_POINTS));
        for (uint32_t i = 0; i < c.count; ++i) {
            const druta::backend::VfPoint& p = (*result.points)[i];
            c.points[i] = {p.idx, p.volt_mv, p.freq_mhz, p.delta_khz};
        }
    }
    DC_COPY(c.error, result.error);
    *out = c;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_vf_other_rows(dc_gpu* handle, dc_vf_other_rows* out) {
    if (!handle || !out || out->size != sizeof(dc_vf_other_rows)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    std::vector<druta::backend::VfOtherRow> rows = handle->curve().read_vfp_other_rows();
    dc_vf_other_rows r{};
    r.size = sizeof(dc_vf_other_rows);
    r.count = static_cast<uint32_t>(std::min<size_t>(rows.size(), DC_VFP_POINTS));
    for (uint32_t i = 0; i < r.count; ++i) r.rows[i] = {rows[i].idx, rows[i].volt_mv, rows[i].value, rows[i].memory ? 1 : 0};
    *out = r;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_vf_deltas_apply(dc_gpu* handle, const dc_vf_delta* deltas, size_t count,
                                                dc_outcome* out) {
    if (!handle || !out || (!deltas && count)) return DC_E_ARGUMENT;
    std::vector<std::pair<int64_t, int64_t>> wanted;
    wanted.reserve(count);
    for (size_t i = 0; i < count; ++i) wanted.emplace_back(deltas[i].idx, deltas[i].delta_khz);
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->curve().apply_vf_deltas(wanted), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_vf_curve_reset(dc_gpu* handle, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->curve().reset_vf_curve(), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_rephase_deltas(dc_gpu* handle, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::Clocks& clocks = handle->clocks();
    put_outcome(handle->curve().rephase_deltas([&clocks] { return clocks.clock_step_khz(); }), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_vf_delta_snapshot(dc_gpu* handle, dc_vf_delta_snapshot* out) {
    if (!handle || !out || out->size != sizeof(dc_vf_delta_snapshot)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto snapshot = handle->curve().vf_delta_snapshot();
    if (!snapshot) return DC_E_UNAVAILABLE;
    dc_vf_delta_snapshot s{};
    s.size = sizeof(dc_vf_delta_snapshot);
    s.freq_div = snapshot->freq_div;
    s.count = static_cast<uint32_t>(std::min<size_t>(snapshot->deltas.size(), DC_VFP_POINTS));
    for (uint32_t i = 0; i < s.count; ++i) s.deltas[i] = {snapshot->deltas[i].first, snapshot->deltas[i].second};
    *out = s;
    return DC_OK;
}


// ---- clock offsets, frequency lock, clock grid ---------------------------------------------------

DC_API dc_status DC_CALL dc_gpu_mem_offset_scale(dc_gpu* handle, dc_mem_offset_scale* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::MemOffsetScale scale = handle->clocks().mem_offset_scale();
    dc_mem_offset_scale s{};
    s.scale = scale.scale;
    DC_COPY(s.unit, scale.unit);
    *out = s;
    return DC_OK;
}

DC_API uint32_t DC_CALL dc_gpu_memory_offset_step_units(dc_gpu* handle) {
    if (!handle) return 0;
    druta::LockGuard guard(handle->gpu.lock());
    return handle->clocks().memory_offset_step_units();
}

DC_API dc_status DC_CALL dc_gpu_clock_step_khz(dc_gpu* handle, uint32_t* step_khz, int32_t* measured) {
    if (!handle || !step_khz) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    *step_khz = handle->clocks().clock_step_khz();
    if (measured) *measured = handle->clocks().step_is_measured() ? 1 : 0;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_max_rise_khz(dc_gpu* handle, uint32_t* khz) {
    if (!handle || !khz) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    *khz = handle->clocks().max_rise_khz();
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_lockable_clocks(dc_gpu* handle, dc_clock_row* rows, size_t capacity, size_t* count) {
    if (!handle || !count || (!rows && capacity)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    std::vector<druta::backend::ClockRow> table = handle->clocks().lockable_clocks_by_mem();
    *count = table.size();
    if (!rows) return DC_OK;
    if (table.size() > capacity) return DC_E_BUFFER;
    for (size_t i = 0; i < table.size(); ++i) {
        dc_clock_row row{};
        row.mem_mhz = table[i].mem_mhz;
        row.count = static_cast<uint32_t>(table[i].graphics.size());
        for (size_t k = 0; k < std::min<size_t>(table[i].graphics.size(), 512); ++k) row.graphics[k] = table[i].graphics[k];
        rows[i] = row;
    }
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clock_offset_set(dc_gpu* handle, uint32_t ctype, double mhz, int32_t preserve_curve,
                                                 dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->clocks().set_clock_offset(ctype, mhz, preserve_curve != 0), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clocks_lock(dc_gpu* handle, int64_t min_mhz, int64_t max_mhz, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->clocks().lock_gpu_clocks(min_mhz, max_mhz), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clocks_last_lock(dc_gpu* handle, uint32_t* min_mhz, uint32_t* max_mhz) {
    if (!handle || !min_mhz || !max_mhz) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto last = handle->clocks().last_clock_lock_request();
    if (!last) return DC_E_UNAVAILABLE;
    *min_mhz = last->first;
    *max_mhz = last->second;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clocks_reset(dc_gpu* handle, int32_t allow_pascal_noop, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->clocks().reset_gpu_clocks(allow_pascal_noop != 0), out);
    return DC_OK;
}

}  // extern "C"

// ---- per-domain clock-offset control block --------------------------------------------------------

extern "C" {

namespace {

void put_clkdom_error(const std::optional<druta::backend::CapabilityError>& error, dc_clkdom_error* out) {
    dc_clkdom_error e{};
    e.size = sizeof(dc_clkdom_error);
    e.has_error = error ? 1 : 0;
    if (error) {
        DC_COPY(e.kind, error->kind);
        DC_COPY(e.text, error->text);
    }
    *out = e;
}

dc_status put_list(const std::vector<uint32_t>& values, uint32_t* out, size_t capacity, size_t* count) {
    *count = values.size();
    if (!out) return DC_OK;
    if (values.size() > capacity) return DC_E_BUFFER;
    for (size_t i = 0; i < values.size(); ++i) out[i] = values[i];
    return DC_OK;
}

}  // namespace

DC_API int32_t DC_CALL dc_gpu_clkdom_ok(dc_gpu* handle) {
    if (!handle) return 0;
    druta::LockGuard guard(handle->gpu.lock());
    return handle->clkdom.ok() ? 1 : 0;
}

DC_API int32_t DC_CALL dc_gpu_clkdom_is_blackwell(dc_gpu* handle) {
    if (!handle) return 0;
    druta::LockGuard guard(handle->gpu.lock());
    return handle->clkdom.is_blackwell() ? 1 : 0;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_domains(dc_gpu* handle, uint32_t* domains, size_t capacity, size_t* count) {
    if (!handle || !count || (!domains && capacity)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    return put_list(handle->clkdom.domains(), domains, capacity, count);
}

DC_API dc_status DC_CALL dc_gpu_clkdom_layout(dc_gpu* handle, dc_clkdom_layout* out) {
    if (!handle || !out || out->size != sizeof(dc_clkdom_layout)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    const druta::backend::ClkDomLayout* lay = handle->clkdom.layout();
    dc_clkdom_layout l{};
    l.size = sizeof(dc_clkdom_layout);
    if (!lay) {
        *out = l;
        return DC_E_UNAVAILABLE;
    }
    DC_COPY(l.name, std::string(lay->name));
    l.version = lay->version;
    l.block_size = lay->size;
    l.header = lay->header;
    l.stride = lay->stride;
    l.mask_dword = lay->mask_dword;
    l.mode = lay->mode;
    l.freq_khz = lay->freq_khz;
    l.has_nvvdd_uv = lay->nvvdd_uv ? 1 : 0;
    l.nvvdd_uv = lay->nvvdd_uv.value_or(0);
    l.has_msvdd_uv = lay->msvdd_uv ? 1 : 0;
    l.msvdd_uv = lay->msvdd_uv.value_or(0);
    *out = l;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_capability_error(dc_gpu* handle, dc_clkdom_error* out) {
    if (!handle || !out || out->size != sizeof(dc_clkdom_error)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_clkdom_error(handle->clkdom.capability_error(), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_probe_errors(dc_gpu* handle, dc_clkdom_probe_error* errors, size_t capacity,
                                                    size_t* count) {
    if (!handle || !count || (!errors && capacity)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    const auto& table = handle->clkdom.probe_errors();
    *count = table.size();
    if (!errors) return DC_OK;
    if (table.size() > capacity) return DC_E_BUFFER;
    size_t i = 0;
    for (const auto& [domain, text] : table) {
        dc_clkdom_probe_error e{};
        e.domain = domain;
        DC_COPY(e.text, text);
        errors[i++] = e;
    }
    return DC_OK;
}

DC_API int32_t DC_CALL dc_gpu_clkdom_offset_read_pending(dc_gpu* handle) {
    if (!handle) return 0;
    druta::LockGuard guard(handle->gpu.lock());
    return handle->clkdom.offset_read_pending() ? 1 : 0;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_controls_for_ui(dc_gpu* handle, uint32_t* controls, size_t capacity,
                                                       size_t* count) {
    if (!handle || !count || (!controls && capacity)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    return put_list(handle->clkdom.controls_for_ui(), controls, capacity, count);
}

DC_API dc_status DC_CALL dc_gpu_clkdom_control_polarity(dc_gpu* handle, int64_t control, int32_t* polarity) {
    if (!handle || !polarity) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    *polarity = handle->clkdom.control_polarity(control);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_control_label(dc_gpu* handle, int64_t control, char* buffer, size_t size,
                                                     size_t* needed) {
    if (!handle) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    return copy_text(handle->clkdom.control_label(control), buffer, size, needed);
}

DC_API dc_status DC_CALL dc_gpu_clkdom_step_mhz(dc_gpu* handle, uint32_t* step_mhz) {
    if (!handle || !step_mhz) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto step = handle->clkdom.step_mhz();
    if (!step) return DC_E_UNAVAILABLE;
    *step_mhz = *step;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_delta_inert(dc_gpu* handle, int32_t has_domain, int64_t domain, int32_t* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto inert = handle->clkdom.delta_inert(optional_arg(has_domain, domain));
    if (!inert) return DC_E_UNAVAILABLE;
    *out = *inert ? 1 : 0;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_delta_clears_ceiling(dc_gpu* handle, int64_t domain, int32_t* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto clears = handle->clkdom.delta_clears_ceiling(domain);
    if (!clears) return DC_E_UNAVAILABLE;
    *out = *clears ? 1 : 0;
    return DC_OK;
}

DC_API int32_t DC_CALL dc_gpu_clkdom_offset_slider_offered(dc_gpu* handle, int64_t control) {
    if (!handle) return 0;
    druta::LockGuard guard(handle->gpu.lock());
    return handle->clkdom.offset_slider_offered(control) ? 1 : 0;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_pairing(dc_gpu* handle, int32_t has_rows, const uint32_t* live_domains,
                                               size_t live_count, dc_clkdom_pair* pairs, size_t capacity,
                                               size_t* count) {
    if (!handle || !count || (!pairs && capacity) || (has_rows && !live_domains && live_count)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    std::vector<druta::backend::PrivDomainRow> rows;
    if (has_rows) {
        for (size_t i = 0; i < live_count; ++i) {
            druta::backend::PrivDomainRow row;
            row.domain = live_domains[i];
            row.prog_mhz = 1.0;  // only "populated" matters to the pairing
            rows.push_back(row);
        }
    }
    std::map<uint32_t, uint32_t> pair = handle->clkdom.pairing(has_rows ? &rows : nullptr);
    *count = pair.size();
    if (!pairs) return DC_OK;
    if (pair.size() > capacity) return DC_E_BUFFER;
    size_t i = 0;
    for (const auto& [control, domain] : pair) pairs[i++] = {control, domain};
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_measurements(dc_gpu* handle, dc_clkdom_measurement* rows, size_t capacity,
                                                    size_t* count) {
    if (!handle || !count || (!rows && capacity)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto table = handle->clkdom.measurements();
    *count = table.size();
    if (!rows) return DC_OK;
    if (table.size() > capacity) return DC_E_BUFFER;
    for (size_t i = 0; i < table.size(); ++i) {
        dc_clkdom_measurement m{};
        DC_COPY(m.name, table[i].first);
        m.has_value = table[i].second ? 1 : 0;
        m.value_khz = table[i].second.value_or(0);
        rows[i] = m;
    }
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_requests(dc_gpu* handle, dc_clkdom_requests* out) {
    if (!handle || !out || out->size != sizeof(dc_clkdom_requests)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto [rows, error] = handle->clkdom.read_requests();
    dc_clkdom_requests r{};
    r.size = sizeof(dc_clkdom_requests);
    if (!rows) {
        DC_COPY(r.error, error);
        *out = r;
        return DC_E_UNAVAILABLE;
    }
    for (const auto& [domain, request] : *rows) {
        dc_clkdom_request& row = r.rows[r.count++];
        row.domain = domain;
        row.mode = request.mode;
        row.freq_khz = request.freq_khz;
        row.has_msvdd_uv = request.msvdd_uv ? 1 : 0;
        row.msvdd_uv = request.msvdd_uv.value_or(0);
    }
    *out = r;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_xbar_offset_mhz(dc_gpu* handle, double* mhz) {
    if (!handle || !mhz) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto value = handle->clkdom.read_xbar_offset_mhz();
    if (!value) return DC_E_UNAVAILABLE;
    *mhz = *value;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_offset_set(dc_gpu* handle, int64_t domain, double mhz, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->clkdom.set_clk_domain_offset(domain, mhz), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_rail_offset_capability(dc_gpu* handle, int64_t rail, int32_t has_domain,
                                                       int64_t domain, dc_rail_offset_capability* out) {
    if (!handle || !out || out->size != sizeof(dc_rail_offset_capability)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::RailOffsetCapability c = handle->clkdom.rail_offset_capability(rail, optional_arg(has_domain, domain));
    dc_rail_offset_capability r{};
    r.size = sizeof(dc_rail_offset_capability);
    r.available = c.available ? 1 : 0;
    r.has_domain = c.domain ? 1 : 0;
    r.domain = c.domain.value_or(0);
    r.has_value = c.value_mv ? 1 : 0;
    r.value_mv = c.value_mv.value_or(0.0);
    r.experimental = c.experimental ? 1 : 0;
    DC_COPY(r.reason, c.reason);
    *out = r;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_rail_offset_read(dc_gpu* handle, int64_t domain, int64_t rail, double* mv) {
    if (!handle || !mv) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    auto value = handle->clkdom.read_rail_offset_mv(domain, rail);
    if (!value) return DC_E_UNAVAILABLE;
    *mv = *value;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_rail_offset_set(dc_gpu* handle, double mv, int64_t domain, int64_t rail,
                                                dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->clkdom.set_rail_offset_mv(mv, domain, rail), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_msvdd_offset_reset(dc_gpu* handle, int64_t domain, dc_outcome* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_outcome(handle->clkdom.reset_msvdd_offset_mv(domain), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_flags_set(dc_gpu* handle, int32_t msvdd_write_enabled,
                                                 int32_t voltage_xoc_enabled) {
    if (!handle) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    handle->clkdom.msvdd_write_enabled = msvdd_write_enabled != 0;
    handle->clkdom.voltage_xoc_enabled = voltage_xoc_enabled != 0;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_clkdom_flags_get(dc_gpu* handle, int32_t* msvdd_write_enabled,
                                                 int32_t* voltage_xoc_enabled) {
    if (!handle || !msvdd_write_enabled || !voltage_xoc_enabled) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    *msvdd_write_enabled = handle->clkdom.msvdd_write_enabled ? 1 : 0;
    *voltage_xoc_enabled = handle->clkdom.voltage_xoc_enabled ? 1 : 0;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_retry_clock_capabilities(dc_gpu* handle, int32_t has_now, double now,
                                                         int32_t* changed) {
    if (!handle || !changed) return DC_E_ARGUMENT;
    if (has_now && !std::isfinite(now)) return DC_E_RANGE;
    druta::LockGuard guard(handle->gpu.lock());
    double at = has_now ? now : druta::monotonic_seconds();
    *changed = handle->clkdom.retry_clock_capabilities(at) ? 1 : 0;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_gpu_refresh_capabilities(dc_gpu* handle, dc_clkdom_error* previous) {
    if (!handle || (previous && previous->size != sizeof(dc_clkdom_error))) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    std::optional<druta::backend::CapabilityError> before = handle->clkdom.refresh_capabilities();
    handle->cp.refresh_live();  // _live_power_controller / _last / _error / _error_kind = None (nvbackend 3692)
    handle->rails.refresh();  // _refresh_volt_rail_capabilities: the four rail read caches (nvbackend 3696)
    if (previous) put_clkdom_error(before, previous);
    return DC_OK;
}

}  // extern "C"
