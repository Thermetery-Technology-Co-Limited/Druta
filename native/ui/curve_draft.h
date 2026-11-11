/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <drutacore/drutacore.h>
#include <cstdint>
#include <string>
#include <vector>

namespace druta::ui {
// A point's freq_mhz is evaluated (base frequency plus delta). Drafts never
// access the GPU. `fresh` means the caller actually read this full curve now.
// freq_div is the verified number of raw delta units per physical unit; zero is unknown.
// stage_khz takes raw table kHz; stage_mhz takes physical MHz.
class CurveDraft {
public:
    void observe(std::uint64_t generation, const std::vector<dc_vf_point>& points, bool fresh, std::uint32_t freq_div);
    bool stage_khz(std::uint32_t index, double delta_khz);
    bool stage_mhz(std::uint32_t index, double delta_mhz);
    bool undo_edit();
    bool revert(); // rebase to the latest valid fresh observation
    std::uint64_t generation() const { return generation_; }
    std::uint32_t freq_div() const { return baseline_div_; }
    const std::vector<dc_vf_point>& baseline() const { return baseline_; }
    const std::vector<dc_vf_point>& draft() const { return draft_; }
    bool dirty() const;
    bool can_apply() const;
    bool can_undo() const { return !edits_.empty(); }
    const std::string& status() const { return status_; }
    std::vector<dc_vf_delta> changes() const;
private:
    struct Edit { std::uint32_t index; std::int32_t previous; };
    std::uint64_t generation_ = 0;
    bool initialized_ = false, fresh_ = false, diverged_ = false;
    std::uint32_t baseline_div_ = 0, latest_div_ = 0;
    std::vector<dc_vf_point> baseline_, draft_, latest_;
    std::vector<Edit> edits_;
    std::string status_ = "No fresh curve";
};
}
