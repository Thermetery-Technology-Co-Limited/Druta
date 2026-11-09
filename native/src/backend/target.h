// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The paired target binding every diagnostic and client-power consumer runs
// first: port of perf_diagnostics.PerfDiagnosticsReader._binding and
// client_power_runtime.ClientPowerController._binding. One instance per
// consumer, as each Python reader keeps its own sticky identity; the identity
// is (the NvAPI object, its handle, the PCI address, devid, subsys, the
// architecture), which stands in for Python's id(api).
//
// Kept verbatim and flagged for the user: a PCI domain other than 0 is refused
// here although nvbackend.same_slot compares bus and device only (NVAPI never
// reports a domain). That refusal blocks client power, PERF and clock-BIOS on
// such hosts; changing it is the user's decision.
#pragma once

#include <cstdint>
#include <optional>
#include <string>

#include "backend/gpu.h"
#include "backend/rm_error.h"
#include "backend/telemetry.h"
#include "core/slot.h"

namespace druta::backend {

// PerfDiagnosticsReader.PROBE_ARCHITECTURES: modern PERF board-object groups occur in Pascal and
// later architectures. Packet rejection remains an independent capability result for each group.
inline bool probe_architecture(std::uint32_t architecture) { return architecture >= 4 && architecture <= 10; }

struct Target {  // what _binding returns: (api, slot, architecture), and the client-power identity dict
    std::string slot;
    std::uint32_t architecture = 0;
    std::uint32_t device_id = 0;
    std::uint32_t subsystem_id = 0;
    void* handle = nullptr;  // api.gpu
};

struct TargetResult {
    std::optional<Target> target;
    RmError error;  // kind identity / transient / unsupported, as PerfDiagnosticError(kind, message)
};

class TargetBinding {
public:
    TargetBinding(Gpu& gpu, Telemetry& telemetry) : gpu_(gpu), telemetry_(telemetry) {}

    // PerfDiagnosticsReader._binding.
    TargetResult bind();
    // ClientPowerController._binding: bind() plus the complete-identity requirement.
    TargetResult bind_client_power();
    // A fresh reader: forget the sticky identity.
    void reset() { identity_.reset(); }

private:
    struct Identity {
        const void* api;
        void* handle;
        PciSlot address;
        std::uint32_t devid, subsys, architecture;
        bool operator==(const Identity& o) const {
            return api == o.api && handle == o.handle && address.domain == o.address.domain && address.bus == o.address.bus &&
                   address.device == o.address.device && address.function == o.address.function && devid == o.devid &&
                   subsys == o.subsys && architecture == o.architecture;
        }
    };
    std::optional<Identity> identity_;
    Gpu& gpu_;
    Telemetry& telemetry_;
};

}  // namespace druta::backend
