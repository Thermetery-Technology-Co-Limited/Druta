// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_limits_service.h"
#include <drutacore/vbios_image.h>
#include <drutacore/voltage_peers.h>
#include <drutacore/perf_diagnostics.h>
namespace druta::ui {
struct VoltageEvidenceApi {
    VoltagePolicyApi target;
    decltype(&dc_gpu_vbios_image_get) image=nullptr;
    decltype(&dc_voltage_peers_read) peers=nullptr;
    decltype(&dc_sha256_hex) sha256=nullptr;
    decltype(&dc_rails_blocks) blocks=nullptr;
    decltype(&dc_rails_state) state=nullptr;
    decltype(&dc_rails_deltas) deltas=nullptr;
    decltype(&dc_rails_caches_get) caches=nullptr;
    decltype(&dc_rails_refresh) refresh=nullptr;
    decltype(&dc_nvapi_interface_present) interface_present=nullptr;
};
// Read-only current target request/policy capture. Acquires the recursive GPU lock;
// no ROM or equation graph is read. Per-rail availability remains independent.
VoltageEquationEvidence capture_voltage_retained_evidence(const VoltageEvidenceApi&,const dc_gpu_identity&,dc_gpu*,const dc_target&);
// A new provider for each opened session. Only explicit service captures call
// it; there is no startup/telemetry read, persistence, or hardware writer.
std::function<VoltageEquationEvidence(dc_gpu*,const VoltageEquationPackets&)>
make_voltage_equation_provider(VoltageEvidenceApi,const dc_gpu_identity&);
}
