// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/gpu.h"

#include <algorithm>
#include <cstdio>
#include <tuple>

#include "core/slot.h"

#if defined(_WIN32)
#include <windows.h>

#include "win/dynlib.h"
#endif

namespace druta::backend {

bool is_admin() {
#if defined(_WIN32)
    win::DynLib shell(L"shell32.dll");
    using IsAdminFn = BOOL(WINAPI*)();
    auto fn = shell.get<IsAdminFn>("IsUserAnAdmin");
    return fn && fn() != FALSE;
#else
    return false;
#endif
}

std::vector<GpuListEntry> merge_enumeration(const std::vector<NvmlDevice>& nvml, const std::vector<NvapiGpu>& nvapi) {
    std::vector<GpuListEntry> out;
    for (const NvmlDevice& g : nvml) {
        GpuListEntry entry;
        entry.slot = g.slot;
        entry.uuid = g.uuid;
        entry.devid = g.devid;
        entry.subsys = g.subsys;
        entry.nvml_index = g.nvml_index;
        entry.name = g.name;
        for (const NvapiGpu& a : nvapi) {
            if (!same_slot(a.slot, g.slot)) continue;
            entry.has_nvapi = true;
            if (entry.name.empty() && !a.name.empty()) entry.name = a.name;
        }
        if (entry.name.empty()) entry.name = "GPU";
        out.push_back(entry);
    }
    // A card NVAPI can see but NVML cannot is still a card, and hiding it would
    // be the more confusing failure - it is the shape a half-attached GPU takes.
    for (const NvapiGpu& a : nvapi) {
        bool listed = std::any_of(out.begin(), out.end(), [&](const GpuListEntry& o) { return same_slot(a.slot, o.slot); });
        if (listed) continue;
        GpuListEntry entry;
        entry.slot = a.slot;
        entry.name = a.name.empty() ? "GPU (NVML did not enumerate it)" : a.name;
        entry.devid = a.devid;
        entry.subsys = a.subsys;
        entry.has_nvapi = true;
        out.push_back(entry);
    }
    auto key = [](const GpuListEntry& o) -> std::tuple<std::uint32_t, std::uint32_t, std::uint32_t, std::uint32_t> {
        if (auto parsed = parse_slot(o.slot)) return {parsed->domain, parsed->bus, parsed->device, parsed->function};
        return {0xFFFF, 0xFF, 0xFF, 0};
    };
    std::stable_sort(out.begin(), out.end(), [&](const GpuListEntry& a, const GpuListEntry& b) { return key(a) < key(b); });
    return out;
}

std::vector<GpuListEntry> enumerate_gpus() {
    Nvml nvml;
    Nvapi nvapi;
    return merge_enumeration(nvml.gpus, nvapi.gpus);
}

Gpu::Gpu(const std::string& slot) : requested_slot(slot), nvapi(slot), nvml(slot) {
    pairing_error = pair();
    identity = read_static();
}

std::string Gpu::pair() {
    // Refuse to be half one card and half another: both halves were selected
    // by slot; this re-checks that decision against the PCI device and
    // subsystem ids the two libraries report INDEPENDENTLY. On a mismatch the
    // NVAPI half is dropped rather than used.
    const NvapiGpu* a = nvapi.selected;
    const NvmlDevice* n = nvml.selected;
    if (!a || !n) return "";
    if (!n->devid) return "";  // NVML had no versioned PCI info: pairing is accepted unverified
    if (a->devid == *n->devid && a->subsys == n->subsys.value_or(0)) return "";
    char text[512];
    const std::string& where = !requested_slot.empty() ? requested_slot : !a->slot.empty() ? a->slot : n->slot;
    std::snprintf(text, sizeof text,
                  "NVAPI and NVML disagree about the card at %s: NVAPI says devid %04X/subsys %08X, NVML says devid "
                  "%04X/subsys %08X. NVAPI disabled rather than risk writing the wrong card.",
                  where.c_str(), a->devid, a->subsys, *n->devid, n->subsys.value_or(0));
    nvapi.disable(text);
    return text;
}

std::string Gpu::slot() const {
    // NVML's is preferred because it carries a real PCI domain; NVAPI's always
    // reads 0000. Empty when neither library could be reached.
    if (nvml.selected && !nvml.selected->slot.empty()) return nvml.selected->slot;
    if (nvapi.selected && !nvapi.selected->slot.empty()) return nvapi.selected->slot;
    return requested_slot;
}

std::string Gpu::status_line() const {
    std::string a = nvapi.ok ? "ok" : "FAIL (" + nvapi.err_detail + ")";
    std::string n = nvml.ok ? "ok" : "FAIL (" + nvml.err_detail + ")";
    std::string where = slot();
    return "NVAPI: " + a + "   |   NVML: " + n + (where.empty() ? "" : "   |   slot " + where);
}

StaticIdentity Gpu::read_static() const {
    StaticIdentity s;
    s.admin = is_admin();
    s.slot = slot();
    if (nvml.selected) s.uuid = nvml.selected->uuid;
    if (nvml.ok) {
        nvml.device_string(nvml.dev, "nvmlDeviceGetName", s.name);
        nvml.system_string("nvmlSystemGetDriverVersion", s.driver);
        nvml.device_string(nvml.dev, "nvmlDeviceGetVbiosVersion", s.vbios);
    }
    if (nvapi.ok) {
        // NVAPI fills a field only while it still holds the placeholder.
        if (s.name == "GPU") nvapi.short_string(nvapi_id::GetFullName, nvapi.gpu, s.name);
        if (s.vbios == "?") nvapi.short_string(nvapi_id::GetVbiosVersionString, nvapi.gpu, s.vbios);
        using DriverFn = int(DRUTA_CDECL*)(unsigned*, char*);
        if (auto driver = nvapi.get<DriverFn>(nvapi_id::GetDriverAndBranchVersion); s.driver == "?" && driver) {
            unsigned version = 0;
            char branch[kNvapiShortString] = {};
            if (driver(&version, branch) == 0 && version) {
                char text[32];
                std::snprintf(text, sizeof text, "%u.%02u", version / 100, version % 100);
                s.driver = text;
            }
        }
    }
    return s;
}

}  // namespace druta::backend
