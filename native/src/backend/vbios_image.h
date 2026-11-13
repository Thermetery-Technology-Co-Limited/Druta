// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "backend/gpu.h"
#include "backend/rm_error.h"
#include <cstdint>
#include <vector>
namespace druta::backend {
inline constexpr uint32_t kVbiosImageExport=0xFC13EE11u,kVbiosImageVersion=0x20010u,kVbiosImageCapacity=0xFA000u;
struct VbiosImageResult {std::vector<uint8_t> image;RmError error;bool ok()const{return !image.empty();}};
// Caller holds gpu.lock(). Reuses selected Gpu pairing and the NVAPI resolver;
// fresh read-only PCI queries bracket the image getter. No PERF-generation gate.
VbiosImageResult read_vbios_image(Gpu&);
}
