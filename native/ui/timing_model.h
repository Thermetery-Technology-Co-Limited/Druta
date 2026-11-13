/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "timings_service.h"
#include "timing_diagnostics.h"
#include "timing_capture.h"
#include <optional>
#include <string>
namespace druta::ui {
// Worker-owned result, published as an immutable snapshot. Preview is the
// canonical backend plan; the view cannot synthesize or replace it.
struct TimingViewData {
    std::wstring helper_path;
    TimingSnapshot snapshot;
    std::optional<TimingPlan> preview;
    std::optional<TimingBatchPlan> batch_preview;
    bool initial_available=false;
    std::uint64_t capture_revision=0;
    dc_gpu_identity identity{};
    std::string helper_token;
    bool undo_available = false;
    bool capture_hold_pending = false;
    bool capture_running = false;
    bool capture_workload_pending = false;
    TimingCaptureMethod capture_method = TimingCaptureMethod::none;
    std::optional<TimingSnapshot> capture_diagnostic;
    std::string message;
};
}
