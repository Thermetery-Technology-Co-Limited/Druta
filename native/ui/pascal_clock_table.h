// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace druta::ui {

enum class PascalClockDomain { gpc, xbar, sys };

// pstate -1 denotes the shared master-program ratio in percent.
// State fields use doubled MHz: HAL 2 maxima or v0x40 independent targets.
struct PascalClockField {
    PascalClockDomain domain;
    int pstate, current, minimum, maximum;
};

struct PascalClockTable {
    bool ok = false;
    std::string message;
    std::vector<PascalClockField> fields;
    uint32_t selector_version = 0;
    uint8_t selector_oem = 0;
    uint16_t device_id = 0;
    bool state_targets = false;
};

struct PascalClockEdit {
    PascalClockDomain domain;
    int pstate, expected, value;
};

struct PascalClockPlan {
    bool ok = false;
    std::string message;
    std::vector<uint8_t> image;
};

struct PascalClockPreview {
    std::optional<double> nominal_mhz;
    std::optional<double> ceiling_mhz;
};

// Pascal's global ratios use the P0 V/F peak under the configured NVVDD ceiling.
// Per-state ceiling fields and legacy independent MHz targets are separate.
PascalClockPreview pascal_clock_preview(const PascalClockTable& table,
    const std::vector<PascalClockEdit>& edits, PascalClockDomain domain, int pstate,
    std::optional<double> p0_peak_mhz = std::nullopt);

// Input is a PCI image starting at the legacy ROM header, as returned by the
// driver's complete image capture. No external file offsets or hardware I/O.
bool clock_override_architecture(uint32_t architecture);
PascalClockTable inspect_pascal_clocks(const std::vector<uint8_t>& image, uint32_t architecture = 4,
                                       const PascalClockTable* original_ranges = nullptr);
PascalClockPlan plan_pascal_clocks(const std::vector<uint8_t>& image,
                                  const std::vector<PascalClockEdit>& edits, uint32_t architecture = 4,
                                  const PascalClockTable* original_ranges = nullptr);
}
