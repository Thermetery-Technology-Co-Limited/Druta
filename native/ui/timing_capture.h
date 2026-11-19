// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "timing_capture_control.h"
#include <chrono>
#include <cstdint>
#include <functional>
#include <optional>
#include <memory>
#include <string>
#include <vector>
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
// Released 1.9.1 timings_p0_watch / auto_wanted (Classic 'Auto-capture at
// P0'): the first P0 sample is the entry; an entry seen while a read is in
// flight is consumed without a read, and an entry is consumed quietly when
// its memory clock already has a top-band capture or the last automatic read
// was under 30 s ago. The entry clears once the memory clock falls below
// exit_below_mhz, the next enumerated state down (the top one when only one
// is enumerated); while that is unknown it clears on leaving P0.
struct TimingAutoGate {
    using Clock=std::chrono::steady_clock;
    static constexpr Clock::duration min_gap=std::chrono::seconds(30);
    bool busy=false;
    std::function<bool(uint32_t memory_mhz)> captured_top_band;
    Clock::time_point now=Clock::now();
    std::optional<uint32_t> exit_below_mhz;
};
// Released 1.9.1 exit_below: states[-2], or the top state when only one is
// enumerated; unset when this adapter's memory states are unknown.
std::optional<uint32_t> timing_auto_exit_below(const std::vector<uint32_t>& ascending_memory_states);
// What a session that is busy means for the P0 watch. Classic (released
// 1.9.1 _tim_busy) consumes an entry only while a timing read of its own
// runs (the held capture, or a plain Re-read, startup or automatic read);
// another command in flight only defers the sample, so the entry is still
// captured once it finishes. Focused defers on any busy.
enum class TimingAutoBusy {idle,consume,defer};
TimingAutoBusy timing_auto_busy(bool classic,bool session_busy,bool capture_busy);
// The host's own plain timing reads in flight (released 1.9.1 _tim_busy is
// set by every read, not only the held capture). The Session runs one
// command at a time, so a read queued when it had completed N actions runs
// until that count moves, the session goes idle or the adapter changes.
class TimingReadFlight {
public:
    // A read the host queued, counted from a snapshot taken before queuing it.
    void queued(uint64_t generation,uint64_t completed_actions);
    // A read queued while drawing (Re-read), after the last observe(): it is
    // counted from what that observe() saw.
    void queued_since_observed();
    // Once per host tick; true while one of those reads runs.
    bool observe(uint64_t generation,uint64_t completed_actions,bool session_busy);
private:
    struct Mark {uint64_t generation=0,completed=0;};
    std::optional<Mark> read_,seen_;
};
class TimingAutoCapture {
public:
    // Consumes an already-polled observation; never invokes callbacks or polls.
    bool observe(bool enabled,bool paused,const std::optional<TimingBandObservation>& observation,const TimingAutoGate* gate=nullptr);
    void reset();
private:
    std::optional<TimingCaptureTarget> target_;
    std::optional<uint32_t> previous_clock_,previous_top_;
    std::optional<int32_t> previous_offset_;
    bool fired_=false;
    // Python's _tim_auto_t: kept across toggles and band exits, cleared
    // with the adapter session.
    std::optional<TimingAutoGate::Clock::time_point> last_auto_;
    uint64_t last_auto_generation_=0;
};
}
