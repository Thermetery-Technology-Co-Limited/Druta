// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// One GPU object that provably speaks for one physical card through two
// independent driver libraries. Port of the identity part of nvbackend.GPU
// and of nvbackend.enumerate_gpus.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "backend/nvapi.h"
#include "backend/nvml.h"
#include "core/mutex.h"

namespace druta::backend {

struct GpuListEntry {
    std::string slot;
    std::string name;
    std::string uuid;
    std::optional<std::uint32_t> devid;
    std::optional<std::uint32_t> subsys;
    std::optional<std::uint32_t> nvml_index;  // nullopt: NVML did not enumerate it
    bool has_nvapi = false;
};

// Every NVIDIA GPU on the host, ordered by PCI slot (never by either
// library's index). [] when no driver interface answers.
std::vector<GpuListEntry> enumerate_gpus();

// The pure merge behind enumerate_gpus, for tests without a driver.
std::vector<GpuListEntry> merge_enumeration(const std::vector<NvmlDevice>& nvml, const std::vector<NvapiGpu>& nvapi);

struct StaticIdentity {
    std::string name = "GPU";
    std::string driver = "?";
    std::string vbios = "?";
    bool admin = false;
    std::string slot;
    std::string uuid;
};

class Gpu {
public:
    explicit Gpu(const std::string& slot = "");

    std::string requested_slot;
    Nvapi nvapi;
    Nvml nvml;
    std::string pairing_error;  // "" when the two halves agree (or cannot be compared)
    StaticIdentity identity;

    bool available() const { return nvapi.ok || nvml.ok; }
    std::string slot() const;
    std::string status_line() const;

    // Serializes driver access by the owner (the UI thread writes while a
    // background poll reads), as nvbackend's _synchronized RLock does.
    Mutex& lock() { return lock_; }

private:
    std::string pair();
    StaticIdentity read_static() const;
    Mutex lock_;
};

bool is_admin();

}  // namespace druta::backend
