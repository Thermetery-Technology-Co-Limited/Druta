// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "pascal_clock_table.h"

namespace druta::ui {

struct PascalClockApply {
    std::vector<PascalClockEdit> edits;
    bool arm_safe_mode = false;
};

struct PascalClockViewState {
    uint64_t generation = 0;
    int selected_pstate = 0;
    std::vector<PascalClockEdit> drafts;
    bool reviewing = false;
    bool arm_safe_mode = false;
    bool view_recovery_script_requested = false;
    unsigned automatic_reads = 0;

    void sync(uint64_t next_generation);
    void stage(const PascalClockField& field, int raw_value);
    void discard();
    bool current(const PascalClockTable* table) const;
    bool has_ratio_edit() const;
    PascalClockApply confirm(const PascalClockTable* table, bool enabled);
};

bool has_writable_pascal_clock(const PascalClockTable* table, PascalClockDomain domain);

struct PascalClockReference {
    double peak_mhz, point_mv, ceiling_mv;
    bool staged = false;
};

// Per-state maxima use doubled MHz in the image. Shared program ratios use whole percent.
double pascal_clock_display_value(int pstate, int raw);
int pascal_clock_raw_value(const PascalClockField& field, double displayed);

PascalClockApply draw_pascal_clock_sliders(PascalClockViewState& state, const PascalClockTable* table,
                                          uint64_t generation, bool edit_enabled, bool apply_enabled,
                                          const std::optional<PascalClockReference>& reference = {});
}
