/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "shunt_model.h"
#include <cstddef>
#include <optional>
#include <string>
#include <vector>

namespace druta::ui {
// Display-only arithmetic. No backend, hardware commands or implicit file I/O.
struct ShuntViewState {
    ShuntConfig config;
    ShuntCorrection correction;
    bool open = false;
    bool dirty = false;
    bool save_requested = false;
    std::string status;
    std::vector<double> parallel_resistors{5.0, 5.0};
    ShuntViewState();
};
void set_shunt_config(ShuntViewState& state, ShuntConfig config);
bool edit_shunt_rail(ShuntViewState& state, std::size_t index, double original, double effective);
bool add_shunt_rail(ShuntViewState& state, const std::string& kind);
bool remove_shunt_rail(ShuntViewState& state, std::size_t index);
void reset_shunt_no_mod(ShuntViewState& state); // retains topology and original values
bool request_shunt_save(ShuntViewState& state);
bool take_shunt_save_request(ShuntViewState& state);
void draw_shunt_window(ShuntViewState& state);
void draw_shunt_power_summary(const ShuntViewState& state,
    std::optional<double> reported_draw, std::optional<double> requested_limit,
    std::optional<double> enforced_limit, bool stale = false);
}
