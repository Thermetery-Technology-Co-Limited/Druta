// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/target.h"

namespace druta::backend {
namespace {

RmError error(RmErrorKind kind, std::string message) {
    RmError e;
    e.kind = kind;
    e.message = std::move(message);
    return e;
}

bool same_address(const PciSlot& a, const PciSlot& b) {
    return a.domain == b.domain && a.bus == b.bus && a.device == b.device && a.function == b.function;
}

}  // namespace

TargetResult TargetBinding::bind() {
    TargetResult out;
    TestLookup test = TestLookup::none;  // the test seam (target.h): none unless a test armed it
    if (test_nth_ && --test_nth_ == 0) {
        test = test_mode_;
        test_mode_ = TestLookup::none;
    }
    if (test == TestLookup::fail) {
        out.error = error(RmErrorKind::identity, "GPU binding changed; create a fresh PERF reader");
        return out;
    }
    const Nvapi& api = gpu_.nvapi;
    if (!gpu_.pairing_error.empty()) {
        out.error = error(RmErrorKind::identity, gpu_.pairing_error);
        return out;
    }
    if (!api.ok || !api.gpu || !api.selected) {
        out.error = error(RmErrorKind::transient, "A live PCI-paired NVAPI handle is unavailable");
        return out;
    }
    const std::string slot = gpu_.slot();
    const std::optional<PciSlot> address = parse_slot(slot);
    const std::optional<PciSlot> api_address = parse_slot(api.selected->slot);
    if (!address || address->domain != 0 || !api_address || !same_address(*address, *api_address)) {
        out.error = error(RmErrorKind::identity, "NVAPI handle does not identify the selected PCI address");
        return out;
    }
    void* handle = api.gpu;
    if (api.selected->handle != handle) {
        out.error = error(RmErrorKind::identity, "NVAPI handle changed without its target binding");
        return out;
    }
    if (const NvmlDevice* other = gpu_.nvml.selected) {
        const std::optional<PciSlot> other_address = parse_slot(other->slot);
        if (!other_address || !same_address(*other_address, *address)) {
            out.error = error(RmErrorKind::identity, "NVML and NVAPI identify different PCI addresses");
            return out;
        }
        if (other->devid && api.selected->devid != *other->devid) {
            out.error = error(RmErrorKind::identity, "NVML and NVAPI disagree about devid");
            return out;
        }
        if (other->subsys && api.selected->subsys != *other->subsys) {
            out.error = error(RmErrorKind::identity, "NVML and NVAPI disagree about subsys");
            return out;
        }
    }
    const std::optional<std::uint32_t> architecture = telemetry_.arch();
    if (!architecture) {
        out.error = error(RmErrorKind::transient, "GPU architecture could not be read");
        return out;
    }
    if (!probe_architecture(*architecture)) {
        out.error = error(RmErrorKind::unsupported, "Modern PERF groups require a Pascal-or-later architecture");
        return out;
    }
    const Identity identity{&api, handle, *address, api.selected->devid, api.selected->subsys, *architecture};
    if (identity_ && !(*identity_ == identity)) {
        out.error = error(RmErrorKind::identity, "GPU binding changed; create a fresh PERF reader");
        return out;
    }
    identity_ = identity;
    Target target;
    target.slot = slot;
    target.architecture = *architecture;
    target.device_id = api.selected->devid;
    target.subsystem_id = api.selected->subsys;
    target.handle = handle;
    if (test == TestLookup::changed) target.subsystem_id ^= 1u;
    out.target = std::move(target);
    return out;
}

TargetResult TargetBinding::bind_client_power() {
    // ClientPowerController._binding: the PERF binding's kinds pass through; then the identity must
    // be complete (devid within 16 bits, subsys within 32). Python's catch-all for any other
    // exception ("GPU binding could not be read: ...") has no native path.
    TargetResult out = bind();
    if (!out.target) return out;
    if (out.target->device_id > 0xFFFF) {
        out.target.reset();
        out.error = error(RmErrorKind::identity, "Client power needs a complete paired PCI identity");
    }
    return out;
}

}  // namespace druta::backend
