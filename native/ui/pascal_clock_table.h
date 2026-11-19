// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
#include <string>
#include <vector>

namespace druta::ui {

enum class PascalClockDomain { gpc, xbar, sys };

// pstate -1 denotes the shared master-program ratio in percent.
// All other values are absolute maxima in the firmware's doubled-clock MHz.
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

// Input is a PCI image starting at the legacy ROM header, as returned by the
// driver's complete image capture. No external file offsets or hardware I/O.
PascalClockTable inspect_pascal_clocks(const std::vector<uint8_t>& image);
PascalClockPlan plan_pascal_clocks(const std::vector<uint8_t>& image,
                                  const std::vector<PascalClockEdit>& edits);
}
