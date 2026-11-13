// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
#include <functional>
#include <optional>
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
enum class TimingHoldState {none,owned,foreign,uncertain};
struct TimingHoldAcquire {bool confirmed=false,cleanup_required=false;std::string message;};
struct TimingCaptureAction {bool ok=false;std::string message;};
struct TimingCaptureCallbacks {
    std::function<std::optional<TimingBandObservation>()> observe;
    std::function<TimingHoldState()> hold_state;
    std::function<TimingHoldAcquire()> acquire;
    std::function<TimingCaptureAction()> release;
    std::function<TimingCaptureAction()> capture;
    std::function<void(unsigned)> sleep_ms;
};
struct TimingCaptureResult {bool ok=false,captured=false,cleanup_pending=false;std::string message;};
// Worker-only explicit operation. No preexisting hold is acquired, changed or released.
TimingCaptureResult capture_qualified_timings(const TimingCaptureTarget&,const TimingCaptureCallbacks&);
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
