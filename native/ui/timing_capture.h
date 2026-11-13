// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "timing_capture_control.h"
#include <cstdint>
#include <functional>
#include <optional>
#include <memory>
#include <string>
namespace druta::ui {
struct TimingCaptureTarget {uint64_t generation=0;std::string uuid,slot,helper_token;};
bool operator==(const TimingCaptureTarget&,const TimingCaptureTarget&);
struct TimingBandObservation {
    TimingCaptureTarget target;
    std::optional<uint32_t> pstate,memory_mhz,top_memory_mhz;
    std::optional<int32_t> memory_offset_raw;
};
// Raw memory offset uses the existing timing service convention: half lands on reported clock.
bool timing_capture_in_p0_band(const TimingBandObservation&);
bool timing_capture_in_top_band(const TimingBandObservation&);
enum class TimingHoldState {none,owned,foreign,uncertain};
struct TimingHoldAcquire {bool confirmed=false,cleanup_required=false;std::string message;};
struct TimingCaptureAction {bool ok=false;std::string message;bool diagnostic=false;};
struct TimingCaptureCallbacks {
    std::function<std::optional<TimingBandObservation>()> observe;
    std::function<TimingHoldState()> hold_state;
    std::function<TimingHoldAcquire()> acquire;
    std::function<TimingCaptureAction()> release;
    std::function<TimingCaptureAction()> capture;
    std::function<void(unsigned)> sleep_ms;
    std::function<TimingCaptureAction()> capture_loaded;
    // The adapter must keep load alive throughout the supplied callback and
    // invalidate success on early exit, deadline, cancellation or failed cleanup.
    std::function<TimingCaptureAction(const std::function<TimingCaptureAction()>&)> workload;
};
enum class TimingCaptureMethod {none,direct,existing_hold,temporary_hold,cuda};
struct TimingCaptureOptions {bool allow_hold=true;std::shared_ptr<const TimingCaptureControl> control;};
struct TimingCaptureResult {bool ok=false,captured=false,cleanup_pending=false;std::string message;
    TimingCaptureMethod method=TimingCaptureMethod::none;bool diagnostic_captured=false,cancelled=false;};
// Worker-only explicit operation. No preexisting hold is acquired, changed or released.
TimingCaptureResult capture_qualified_timings(const TimingCaptureTarget&,const TimingCaptureCallbacks&,const TimingCaptureOptions& = {});
class TimingAutoCapture {
public:
    // Consumes an already-polled observation; never invokes callbacks or polls.
    bool observe(bool enabled,bool paused,const std::optional<TimingBandObservation>& observation);
    void reset();
private:
    std::optional<TimingCaptureTarget> target_;
    std::optional<uint32_t> previous_clock_,previous_top_;
    std::optional<int32_t> previous_offset_;
    bool fired_=false;
};
}
