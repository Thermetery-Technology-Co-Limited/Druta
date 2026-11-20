// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// NVML: enumeration and selection of one device by PCI slot.
// Port of nvbackend.Nvml; error texts and fallbacks are the Python ones.
#pragma once

#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "backend/exports.h"

namespace druta::backend {

struct NvmlDevice {
    void* dev = nullptr;
    std::uint32_t nvml_index = 0;
    std::string slot;  // "" when the versioned PCI reader is absent or failed
    std::optional<std::uint32_t> devid;
    std::optional<std::uint32_t> subsys;
    std::string name;
    std::string uuid;
};

// nvmlPciInfo_t: v2 and v3 share the 16-byte legacy bus id and the five
// identity words; this buffer covers the longer v3 (68 bytes).
struct NvmlPciInfo {
    char bus_id_legacy[16];
    std::uint32_t domain;
    std::uint32_t bus;
    std::uint32_t device;
    std::uint32_t pci_device_id;
    std::uint32_t pci_subsystem_id;
    char bus_id[32];
};
static_assert(sizeof(NvmlPciInfo) == 68, "nvmlPciInfo_t v3 layout");

class Nvml {
public:
    explicit Nvml(const std::string& slot = "");

    bool ok = false;
    void* dev = nullptr;
    std::vector<NvmlDevice> gpus;
    const NvmlDevice* selected = nullptr;
    std::string err_detail;
    std::string dll_path;

    bool has(const char* name) const { return library_ && library_->has(name); }
    template <typename Fn>
    Fn get(const char* name) const {
        return library_ ? library_->get<Fn>(name) : nullptr;
    }
    std::string errstr(int status) const;

    // Read a string export of the form status(device, buffer, 96) or
    // status(buffer, 96) into out; true only on status 0 and a non-empty string.
    bool device_string(void* device, const char* export_name, std::string& out) const;
    bool system_string(const char* export_name, std::string& out) const;

private:
    std::optional<NvmlPciInfo> read_pci(void* device) const;
    void select(const std::string& slot);
    std::unique_ptr<ExportSource> library_;
};

}  // namespace druta::backend
