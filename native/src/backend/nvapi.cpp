// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/nvapi.h"

#include <algorithm>
#include <tuple>

#include "core/slot.h"

namespace druta::backend {
namespace {

using InitializeFn = int(DRUTA_CDECL*)();
using EnumGpusFn = int(DRUTA_CDECL*)(void**, unsigned*);
using PciIdsFn = int(DRUTA_CDECL*)(void*, unsigned*, unsigned*, unsigned*, unsigned*);
using BusFn = int(DRUTA_CDECL*)(void*, unsigned*);
using ShortStringFn = int(DRUTA_CDECL*)(void*, char*);

std::tuple<std::uint32_t, std::uint32_t, std::uint32_t, std::uint32_t> order_key(const std::string& slot,
                                                                                 std::uint32_t index) {
    if (auto parsed = parse_slot(slot)) return {parsed->domain, parsed->bus, parsed->device, parsed->function};
    return {0xFFFF, 0xFF, 0xFF, index};
}

}  // namespace

Nvapi::Nvapi(const std::string& slot) {
    std::string error;
    library_ = loader().open_nvapi(error);
    if (!library_) {
        err_detail = error;
        return;
    }
    query_ = library_->get<void*(DRUTA_CDECL*)(unsigned)>("nvapi_QueryInterface");
    if (!query_) {
        err_detail = "nvapi_QueryInterface not exported";
        return;
    }
    auto initialize = get<InitializeFn>(nvapi_id::Initialize);
    if (!initialize) {
        err_detail = "NvAPI_Initialize not resolvable";
        return;
    }
    int status = initialize();
    if (status != 0) {
        err_detail = "NvAPI_Initialize status " + std::to_string(status);
        return;
    }
    auto enum_gpus = get<EnumGpusFn>(nvapi_id::EnumPhysicalGPUs);
    auto pci_ids = get<PciIdsFn>(nvapi_id::GetPCIIdentifiers);
    if (!enum_gpus || !pci_ids) {
        // The Python constructor would fail here with a TypeError; a missing
        // enumeration or identity interface is reported instead.
        err_detail = enum_gpus ? "NvAPI_GPU_GetPCIIdentifiers not resolvable" : "NvAPI_EnumPhysicalGPUs not resolvable";
        return;
    }
    auto bus_id = get<BusFn>(nvapi_id::GetBusId);
    auto bus_slot = get<BusFn>(nvapi_id::GetBusSlotId);
    void* handles[64] = {};
    unsigned count = 0;
    enum_gpus(handles, &count);  // status not checked, as in nvbackend
    for (unsigned i = 0; i < count && i < 64; ++i) {
        NvapiGpu entry;
        entry.handle = handles[i];
        entry.enum_index = i;
        unsigned device_id = 0, subsystem = 0, revision = 0, external = 0;
        pci_ids(handles[i], &device_id, &subsystem, &revision, &external);  // status ignored, as in nvbackend
        entry.devid = device_id >> 16;
        entry.subsys = subsystem;
        unsigned bus = 0, device = 0;
        bool has_bus = bus_id && bus_id(handles[i], &bus) == 0 && bus_slot && bus_slot(handles[i], &device) == 0;
        // NVAPI exposes bus and device but no PCI domain, so the slot it
        // yields always reads domain 0000; see same_slot().
        if (has_bus) entry.slot = format_slot(0, bus, device);
        short_string(nvapi_id::GetFullName, handles[i], entry.name);
        gpus.push_back(entry);
    }
    select(slot);
}

void* Nvapi::interface(std::uint32_t id) const {
    return query_ ? query_(id) : nullptr;
}

bool Nvapi::short_string(std::uint32_t id, void* handle, std::string& out) const {
    auto fn = get<ShortStringFn>(id);
    if (!fn) return false;
    char buffer[kNvapiShortString] = {};
    if (fn(handle, buffer) != 0) return false;
    buffer[kNvapiShortString - 1] = '\0';
    if (!buffer[0]) return false;
    out = buffer;
    return true;
}

void Nvapi::disable(const std::string& reason) {
    ok = false;
    gpu = nullptr;
    err_detail = reason;
    // selected is NOT cleared, as in nvbackend.GPU._pair.
}

void Nvapi::select(const std::string& slot) {
    // With no slot asked for, the choice is the LOWEST PCI slot rather than
    // enumeration index 0. Asking for a slot that is not present fails rather
    // than falling back to some other card.
    if (gpus.empty()) {
        if (err_detail.empty()) err_detail = "NvAPI enumerated no GPUs";
        return;
    }
    if (!slot.empty()) {
        auto hit = std::find_if(gpus.begin(), gpus.end(), [&](const NvapiGpu& g) { return same_slot(g.slot, slot); });
        if (hit == gpus.end()) {
            std::string seen;
            for (const NvapiGpu& g : gpus) seen += (seen.empty() ? "" : ", ") + (g.slot.empty() ? "?" : g.slot);
            err_detail = "no NVAPI GPU at slot " + slot + " (NVAPI sees: " + (seen.empty() ? "none" : seen) + ")";
            return;
        }
        selected = &*hit;
    } else {
        auto lowest = std::min_element(gpus.begin(), gpus.end(), [](const NvapiGpu& a, const NvapiGpu& b) {
            return order_key(a.slot, a.enum_index) < order_key(b.slot, b.enum_index);
        });
        selected = &*lowest;
    }
    gpu = selected->handle;
    ok = true;
}

}  // namespace druta::backend
