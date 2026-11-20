// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/voltage.h"

#include <string>

#include "backend/nvapi.h"
#include "backend/structs.h"

namespace druta::backend {
namespace {
using HandlePtrFn = int(DRUTA_CDECL*)(void*, void*);
constexpr std::uint32_t VoltCtrlGet = 0x9DF23CA1;
constexpr std::uint32_t VoltCtrlSet = 0xB9306D9B;
}  // namespace

Outcome set_voltage_boost(Gpu& gpu, std::int64_t pct) {
    Nvapi& a = gpu.nvapi;
    auto get = a.ok ? a.get<HandlePtrFn>(VoltCtrlGet) : nullptr;
    auto set = a.ok ? a.get<HandlePtrFn>(VoltCtrlSet) : nullptr;
    if (!get || !set) return {false, "voltage-control APIs unavailable"};
    if (!(0 <= pct && pct <= 100)) return {false, "voltage boost " + std::to_string(pct) + "% out of [0..100]"};
    VoltBoost vc{};
    vc.version = nvapi_ver<VoltBoost>(1);
    if (get(a.gpu, &vc) != 0) return {false, "voltage GetControl failed (cannot read-modify-write)"};
    vc.version = nvapi_ver<VoltBoost>(1);  // re-stamp; preserve unknown[8]
    vc.percent = static_cast<std::int32_t>(pct);
    int st = set(a.gpu, &vc);
    if (st == 0) return {true, "core voltage boost set to " + std::to_string(pct) + "% (ceiling unlock)"};
    return {false, "voltage set failed (status " + std::to_string(st) + ") - needs admin"};
}

}  // namespace druta::backend
