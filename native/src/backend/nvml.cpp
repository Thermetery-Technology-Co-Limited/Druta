// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/nvml.h"

#include <algorithm>
#include <tuple>

#include "core/slot.h"

namespace druta::backend {
namespace {

using InitFn = int(DRUTA_CDECL*)();
using CountFn = int(DRUTA_CDECL*)(unsigned*);
using HandleFn = int(DRUTA_CDECL*)(unsigned, void**);
using PciFn = int(DRUTA_CDECL*)(void*, NvmlPciInfo*);
using DeviceStringFn = int(DRUTA_CDECL*)(void*, char*, unsigned);
using SystemStringFn = int(DRUTA_CDECL*)(char*, unsigned);
using ErrorStringFn = const char*(DRUTA_CDECL*)(int);

constexpr unsigned kStringBuffer = 96;

// Python: parse_slot(slot) or (0xFFFF, 0xFF, 0xFF, index) -- unparseable
// slots sort last, by enumeration index.
std::tuple<std::uint32_t, std::uint32_t, std::uint32_t, std::uint32_t> order_key(const std::string& slot,
                                                                                 std::uint32_t index) {
    if (auto parsed = parse_slot(slot)) return {parsed->domain, parsed->bus, parsed->device, parsed->function};
    return {0xFFFF, 0xFF, 0xFF, index};
}

}  // namespace

Nvml::Nvml(const std::string& slot) {
    std::string error;
    library_ = loader().open_nvml(error);
    if (!library_) {
        err_detail = "nvml.dll not loadable: " + error;
        return;
    }
    dll_path = library_->path();
    const char* init_name = has("nvmlInit_v2") ? "nvmlInit_v2" : has("nvmlInit") ? "nvmlInit" : nullptr;
    if (!init_name) {
        err_detail = "NVML has no initialization export";
        return;
    }
    int status = get<InitFn>(init_name)();
    if (status != 0) {
        err_detail = std::string(init_name) + " status " + std::to_string(status);
        return;
    }
    // The index spaces changed together in R319: never mix a v1 count with a
    // v2 index lookup (or vice versa) on a partially exported DLL.
    const char* count_name = nullptr;
    const char* handle_name = nullptr;
    if (has("nvmlDeviceGetCount_v2") && has("nvmlDeviceGetHandleByIndex_v2")) {
        count_name = "nvmlDeviceGetCount_v2";
        handle_name = "nvmlDeviceGetHandleByIndex_v2";
    } else if (has("nvmlDeviceGetCount") && has("nvmlDeviceGetHandleByIndex")) {
        count_name = "nvmlDeviceGetCount";
        handle_name = "nvmlDeviceGetHandleByIndex";
    } else {
        err_detail = "NVML has no matching device-count/index exports";
        return;
    }
    unsigned count = 0;
    status = get<CountFn>(count_name)(&count);
    if (status != 0) {
        err_detail = std::string(count_name) + " status " + std::to_string(status);
        return;
    }
    auto handle_fn = get<HandleFn>(handle_name);
    for (unsigned i = 0; i < count; ++i) {
        void* device = nullptr;
        if (handle_fn(i, &device) != 0) continue;
        NvmlDevice entry;
        entry.dev = device;
        entry.nvml_index = i;
        if (auto pci = read_pci(device)) {
            entry.slot = format_slot(pci->domain, pci->bus, pci->device);
            entry.devid = pci->pci_device_id >> 16;
            entry.subsys = pci->pci_subsystem_id;
        }
        // The only identity that survives two IDENTICAL cards in one host,
        // where name and VBIOS are equal by construction. Profiles lean on it.
        device_string(device, "nvmlDeviceGetName", entry.name);
        device_string(device, "nvmlDeviceGetUUID", entry.uuid);
        gpus.push_back(entry);
    }
    select(slot);
}

std::optional<NvmlPciInfo> Nvml::read_pci(void* device) const {
    // V2 and V3 share the 16-byte bus ID and five leading identity words. The
    // unversioned pre-v2 layout cannot establish subsystem identity and is
    // deliberately not used for pairing or private write profiles.
    const char* name = has("nvmlDeviceGetPciInfo_v3")   ? "nvmlDeviceGetPciInfo_v3"
                       : has("nvmlDeviceGetPciInfo_v2") ? "nvmlDeviceGetPciInfo_v2"
                                                        : nullptr;
    if (!name) return std::nullopt;
    NvmlPciInfo pci{};
    if (get<PciFn>(name)(device, &pci) != 0) return std::nullopt;
    return pci;
}

bool Nvml::device_string(void* device, const char* export_name, std::string& out) const {
    auto fn = get<DeviceStringFn>(export_name);
    if (!fn) return false;
    char buffer[kStringBuffer] = {};
    if (fn(device, buffer, kStringBuffer) != 0) return false;
    buffer[kStringBuffer - 1] = '\0';
    if (!buffer[0]) return false;
    out = buffer;
    return true;
}

bool Nvml::system_string(const char* export_name, std::string& out) const {
    auto fn = get<SystemStringFn>(export_name);
    if (!fn) return false;
    char buffer[kStringBuffer] = {};
    if (fn(buffer, kStringBuffer) != 0) return false;
    buffer[kStringBuffer - 1] = '\0';
    if (!buffer[0]) return false;
    out = buffer;
    return true;
}

std::string Nvml::errstr(int status) const {
    if (auto fn = get<ErrorStringFn>("nvmlErrorString")) {
        if (const char* text = fn(status)) return text;
    }
    return std::to_string(status);
}

void Nvml::select(const std::string& slot) {
    // Same rule as NvAPI: lowest slot by default, exact match or failure when
    // a slot is named. A named card is that card or nothing.
    if (gpus.empty()) {
        err_detail = "NVML enumerated no GPUs";
        return;
    }
    if (!slot.empty()) {
        auto hit = std::find_if(gpus.begin(), gpus.end(),
                                [&](const NvmlDevice& g) { return same_slot(g.slot, slot); });
        if (hit == gpus.end()) {
            std::string seen;
            for (const NvmlDevice& g : gpus) seen += (seen.empty() ? "" : ", ") + (g.slot.empty() ? "?" : g.slot);
            err_detail = "no NVML device at slot " + slot + " (NVML sees: " + (seen.empty() ? "none" : seen) + ")";
            return;
        }
        selected = &*hit;
    } else {
        auto lowest = std::min_element(gpus.begin(), gpus.end(), [](const NvmlDevice& a, const NvmlDevice& b) {
            return order_key(a.slot, a.nvml_index) < order_key(b.slot, b.nvml_index);
        });
        selected = &*lowest;
    }
    dev = selected->dev;
    ok = true;
}

}  // namespace druta::backend
