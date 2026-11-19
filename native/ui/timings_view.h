/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "backend.h"
#include "timing_model.h"
#include "timing_profile.h"
#include "timing_helper_config.h"
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>
namespace druta::ui {
// Released 1.9.1 timings.driver_state: nvtune's kernel driver service, by a
// read-only service-manager query. running is unset when the query itself
// failed: that is not evidence the driver is stopped.
struct TimingDriverState { std::string state; std::optional<bool> running; };
TimingDriverState timing_driver_state();
struct TimingsHelperSuggestion { std::wstring path; std::string message; bool registration_present=false; std::string registration_message;
    TimingDriverState driver; };
// Read-only discovery; an explicit environment override is exclusive. driver
// stays unread here: only Classic's setup screen asks (TimingSetupHost).
TimingsHelperSuggestion suggest_timing_helper(const TimingHelperConfigPaths* paths=nullptr);
struct TimingsViewState {
    std::uint64_t generation = 0;
    bool initialized = false;
    // Focused 'Auto-capture on stable P0 entry': off, reset per adapter.
    bool auto_capture = false;
    // Classic 'Auto-capture at P0': released 1.9.1 tim_auto is armed by
    // default and, being one checkbox, keeps the user's choice across cards.
    // Read only: it submits timings_read on a telemetry P0 entry.
    bool classic_auto_capture = true;
    // Discovery (or a helper selection) ended without a helper; Classic then
    // shows the released setup screen instead of the working tab.
    bool helper_missing = false;
    // nvtune's driver service as last probed (discovery or Locate); unset
    // when unknown. Classic shows the released 'driver is not running'
    // setup screen while it is known stopped and no read has succeeded.
    std::optional<bool> helper_driver_running;
    std::string helper_driver_state;
    bool helper_registration_present = false;
    std::string helper_registration_message;
    double capture_cap_mv = 1100;
    std::string field;
    std::uint32_t value = 0;
    std::vector<TimingRequest> staged;
    std::string message;
    // Released 1.9.1 tw_profile_status: the last timing profile Save / Load
    // result. Also kept in message, which is where Focused shows it; Classic
    // shows it under the profile buttons instead.
    std::string profile_status;
    bool profile_status_ok = true;
    // Released 1.9.1 tim_state visibility: hidden by a capture in the top
    // band and shown by one outside it; a failed read leaves it as it was.
    bool state_line_hidden = false;
    // Released 1.9.1 tim_title / tim_sub keep the last successful capture's
    // wording when a read fails; this is that capture, unset before one.
    std::optional<TimingSnapshot> titled;
    // Classic 'Re-read timings' was pressed this frame: the host counts the
    // plain read as a timing read in flight (released 1.9.1 _tim_busy).
    bool read_requested = false;
    // Set by the host each tick: a plain timing read (startup, automatic or
    // Re-read) is in flight; Classic says '  reading…' instead of the clock.
    bool read_in_flight = false;
    // The last blue read's timing_allow_hold: released 1.9.1 says
    // 'inducing GPU load…' when no hold can be taken.
    bool capture_hold_requested = true;
    // Released 1.9.1 tim_busy keeps the colour the live line last set (GOOD
    // at P0, DIM below) for the busy text; unset before the first live line.
    std::optional<bool> live_at_p0;
    // The staged set 'Preview all staged' was last pressed for, so a refused
    // preview is shown only against the edits it refused.
    std::vector<TimingRequest> previewed;
    std::wstring observed_helper;
    std::string observed_token;
    // Released 1.9.1 _tim_caps: this session's stable captures keyed by
    // memory clock, for the cross-capture comparison. Read data only.
    std::vector<TimingCapture> captures;
    std::string filed_signature;
    // Set by the host each tick (Classic): timing actions completed so far.
    std::uint64_t timing_actions = 0;
    // Released 1.9.1 tw_clear: 'Revert edits' empties tw_result. The result it
    // cleared stays hidden until a later timing action completes or the
    // session publishes another result.
    struct ClearedResult { std::string message; bool ok = false; TimingViewData::Source source = TimingViewData::Source::other;
        std::uint64_t capture_revision = 0, timing_actions = 0; };
    std::optional<ClearedResult> result_cleared;
};
struct TimingsViewOutput { std::vector<Command> commands; bool choose_helper = false,forget_helper=false,test_signing_setup=false,save_profile=false,load_profile=false,cancel_capture=false;
    // Classic log lines this frame wrote (text, [ok]/[!!] tag or untagged), as released 1.9.1 logs them.
    std::vector<std::pair<std::string,std::optional<bool>>> classic_log; };
void sync_timings_view(TimingsViewState& state, const TimingViewData* data, std::uint64_t generation);
bool select_timing_field(TimingsViewState& state, const TimingViewData& data, const std::string& field);
bool can_preview_timing(const TimingsViewState& state, const TimingViewData* data, bool busy, bool unlocked);
bool can_apply_timing(const TimingsViewState& state, const TimingViewData* data, bool busy, bool unlocked);
bool can_capture_timing(const TimingViewData* data,bool busy);
bool stage_timing_field(TimingsViewState& state,const TimingViewData& data,const std::string& field,uint32_t value);
// Inline 'new value' cell: equal to the captured register retires the staged
// entry, anything else stages it. Writes still need Preview and Apply.
bool edit_timing_cell(TimingsViewState& state,const TimingViewData& data,const std::string& field,uint32_t value);
// Staging is the same in both presentations. Classic words the result as
// released 1.9.1 stage_timing_profile does (fields, changes, path, source
// capture); Focused keeps its own sentence.
bool stage_timing_profile(const ProfileJson& document,const TimingViewData& data,TimingsViewState& state,
                          bool classic=false,const std::string& path={});
// A timing profile Save / Load result: message and profile_status.
void note_timing_profile(TimingsViewState& state,std::string message,bool ok);
// Released 1.9.1 poll_timing_profile for a finished save: the path on
// success, the error otherwise.
void note_timing_profile_saved(TimingsViewState& state,bool ok,const std::string& path,const std::string& error);
// Staged fields whose value differs from the captured register (an unknown
// captured value counts): released 1.9.1 len(_tw_pending), Apply's count.
std::size_t timing_pending_changes(const TimingsViewState& state,const TimingViewData& data);
bool can_preview_timing_batch(const TimingsViewState& state,const TimingViewData* data,bool busy);
bool can_apply_timing_batch(const TimingsViewState& state,const TimingViewData* data,bool busy,bool unlocked);
// Released 1.9.1 run()/select_gpu(): one read-only capture per adapter
// session. True means submit one timings_read now; nothing without a helper,
// nothing while busy, opening, disconnected, capturing or holding.
// Classic only: at 539bb9d Focused opened its Timings page with no capture.
struct TimingStartupRead { std::uint64_t generation = 0; bool done = false; };
bool want_timing_startup_read(TimingStartupRead& state, std::uint64_t generation, bool busy, bool opening, bool connected,
                              const TimingViewData* timings, bool classic);
bool timing_auto_capture_armed(const TimingsViewState& state, bool classic);
// Released 1.9.1 tw_result: empty at rest. A read's own result (the
// snapshot's message) is not repeated there, nor a P0 capture's note or a
// batch preview's result, which have their own lines; write and profile
// results are, GOOD when the action succeeded and BAD when it did not.
std::string classic_timing_result(const TimingViewData& data);
// Released 1.9.1 relayout() H for the current frame's font and viewport: the
// client height less menu_h(), the tab selector and the gate bar.
float classic_timing_share_height();
// The same line as the page shows it: empty while 'Revert edits' cleared it.
std::string classic_timing_result(const TimingsViewState& state, const TimingViewData& data);
// Released 1.9.1 open_timing_profile refuses before the picker opens: while a
// timing read runs, without a successful read and, for Save, a capture
// make_profile would refuse (no confirmed layout, outside the top band).
// Empty when the picker may open. Classic only.
std::string classic_timing_profile_refusal(const TimingsViewState& state, const TimingViewData* data, bool save);
// Released 1.9.1 open_timing_profile's picker: its title, the profile
// library's timings folder (created here; throws when it cannot be) and
// timings.json as Save's default name.
struct TimingProfileDialog { std::wstring title, folder, file; };
TimingProfileDialog classic_timing_profile_dialog(bool save, const std::wstring& library);
enum class TimingTone { text, good, warn, bad };
TimingTone classic_timing_result_tone(const TimingViewData& data);
// Released 1.9.1 tw_plan: one text item. Nothing staged, a lost readback, or
// timingwrite.Plan.summary ('2 field(s): RC 106->110, RAS 66->70') with the
// register words appended once the explicit batch preview has checked these
// exact edits (GOOD), and its refusal first when it did not (BAD).
struct TimingPlanLine { std::string text; TimingTone tone = TimingTone::text; };
TimingPlanLine classic_timing_plan(const TimingsViewState& state, const TimingViewData& data);
// Released 1.9.1 timings_p0_watch's receipt for an automatic capture.
std::string timing_auto_capture_receipt(std::uint32_t memory_mhz, std::optional<std::uint32_t> top_memory_mhz);
// Live memory clock for the released 1.9.1 tim_busy status line. Unset
// members are unknown; the line is empty without a memory clock.
struct TimingLiveClock { std::optional<std::uint32_t> memory_mhz, pstate, top_memory_mhz, divisor; };
// Released 1.9.1 is_p0: the p-state decides when known, else the clock.
bool timing_live_at_p0(const TimingLiveClock& live);
std::string timing_live_status(const TimingLiveClock& live, const std::vector<TimingCapture>& captures, bool armed);
TimingsViewOutput draw_timings(TimingsViewState& state, const TimingViewData* data,
                               std::uint64_t generation, bool busy, bool unlocked,bool connected=true,bool classic=false,
                               const TimingLiveClock* live=nullptr);
}
