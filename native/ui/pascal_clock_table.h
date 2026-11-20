// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace druta::ui {

enum class PascalClockDomain {
    gpc, xbar, sys, tfaw, trc, trfc, tras, trp, tcl, twl, trcd_rd, trcd_wr, rpre, wpre, cdlr,
    twr, w2r_bus, r2w_bus, pdex, pden2pdex, aond, tccdl, tccds, trefi, trrd, wrcrc
};

struct PascalTimingField {
    PascalClockDomain domain;
    const char* key;
    const char* label;
    unsigned word, shift, width;
    bool writable;
};

const std::vector<PascalTimingField>& pascal_timing_fields();
const PascalTimingField* pascal_timing_field(PascalClockDomain domain);
const char* pascal_timing_label(PascalClockDomain domain);

// Both sides of this controller sample must have reported P0 at the same memory clock.
struct PascalTimingSample {
    int memory_mhz = 0;
    uint32_t config1 = 0, config3 = 0;
    uint32_t config0 = 0, config2 = 0, config4 = 0, config5 = 0;
    uint8_t present = 0x0a; // Older receipts captured CONFIG1 and CONFIG3.
    uint32_t word(unsigned index) const;
    void set_word(unsigned index, uint32_t value);
    bool operator==(const PascalTimingSample& rhs) const {
        if (memory_mhz != rhs.memory_mhz || present != rhs.present) return false;
        for (unsigned i=0; i<6; ++i)
            if ((present & (1u<<i)) && word(i)!=rhs.word(i)) return false;
        return true;
    }
    bool operator!=(const PascalTimingSample& rhs) const { return !(*this == rhs); }
};
bool matches_pascal_timing(const PascalTimingSample& sample, const PascalTimingSample& expected);
int pascal_timing_value(const PascalTimingSample& sample, PascalClockDomain domain);
PascalTimingSample with_pascal_timing(PascalTimingSample sample, PascalClockDomain domain, int value);
inline constexpr uint32_t pascal_tfaw_mask = 0x1fe00;
inline PascalTimingSample with_tfaw(PascalTimingSample sample, int cycles) {
    sample.config3 = (sample.config3 & ~pascal_tfaw_mask) | ((uint32_t(cycles) << 9) & pascal_tfaw_mask);
    return sample;
}

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
    std::optional<PascalTimingSample> timing_sample;
    std::vector<PascalClockField> timing_fields;
    std::string timing_message;
};

struct PascalClockEdit {
    PascalClockDomain domain;
    int pstate, expected, value;
    std::optional<PascalTimingSample> timing_sample = {};
};

struct PascalClockPlan {
    bool ok = false;
    std::string message;
    std::vector<uint8_t> image;
};

// Annotate a clock inspection with the selected GPU's independently observed P0 timing.
void inspect_pascal_tfaw(PascalClockTable&, const std::vector<uint8_t>& image,
                         uint32_t architecture, const PascalTimingSample& sample);
void inspect_pascal_timings(PascalClockTable&, const std::vector<uint8_t>& image,
                            uint32_t architecture, const PascalTimingSample& sample);

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
