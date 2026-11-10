// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The voltage-rail getters (port of nvbackend.GPU._read_volt_rail_blocks, _read_volt_rail_deltas,
// read_volt_rail_state and read_rail_live_mv): increment R2.
//
// Two NVAPI getters, each called with one singleton rail mask at a time (1 << rail, rails 0 and 1;
// the combined mask 3 is never sent): VoltRailsCtlGet (0xA3070DB0, the signed control deltas, at
// version 0x00020AC8 with the 0x00010AC8 fallback after INCOMPATIBLE_STRUCT_VERSION only) and
// VoltRailsAbs (0x5D0634EE, the absolute and live microvolts, at 0x00010AC8). A rail is read when
// the getter returns 0 and echoes both the version and the mask; record contents never decide
// presence (a stock control record is entirely zero). The class keeps the per-GPU caches of the
// Python original with the same semantics: the rails each getter has read (_volt_rail_masks_cache),
// the retry time of a rail that was not read (_volt_rail_retry_after, two seconds on Python's
// time.monotonic(), here druta::monotonic_seconds()), the last attempt per getter and rail
// (_volt_rail_getter_diagnostics) and the version each getter last answered at
// (_volt_rail_read_versions). No board, VBIOS or driver identity is consulted.
//
// Increment R3 adds the reference capture and the readers over it (read_volt_rail_limits,
// _volt_rail_profile, volt_rail_diagnostics, volt_rail_limits_supported, volt_rail_limit_fields,
// stock_limit_mv, volt_rail_limits_mv) and two more pieces of per-GPU state that refresh() keeps, as
// _refresh_volt_rail_capabilities keeps them: the reference (_volt_rail_reference, the per-rail bases
// estimated from the first stable paired sample) and the first-read record (_volt_rail_initial_uv,
// the four control deltas in microvolts, set once per rail). The architecture is the paired
// adapter's own (Telemetry::arch(), Python's self.arch()). Every reference and first-read value is an
// estimate for this adapter at the operating point it was read, never a factory default or a
// generation constant.
//
// Increment R4 adds the write sequence (_write_rail_records, the preconditions of
// _write_rail_records_locked, and _verify_rail_records) over the already-ported locked write
// (rail_write_locked in hook.h). The whole sequence holds the process-wide rail hook mutex after the
// caller's handle lock; the settle re-send is decided by the profile's settle_vmin only.
//
// Increment R5 adds the verified setters over that sequence (set_volt_rail_limits,
// set_volt_rail_limits_raw, reset_volt_rail_limits, _adopt_verified_hold_fields). The two session
// flags they read (volt_limits_write_enabled, voltage_xoc_enabled) are passed per call by the
// binding, never stored here. The restore target is this session's first read (initial_), never a
// factory default.
//
// Increment R6 adds the hold-headroom runtime over the setters' store (hold_headroom_grain_mv,
// hold_headroom_architecture, hold_headroom_rail_unavailable, hold_headroom_overwritten,
// hold_headroom_record, apply_hold_headroom, _headroom_unread, hold_headroom_live_ok and
// restore_hold_headroom) and the four pieces of session state they keep (_hold_headroom_grain,
// _hold_headroom_offset_mv, _hold_headroom_unread, _hold_headroom_live_trip). All of it is process
// memory for this session only: nothing is persisted or reapplied after a driver restart (the crash
// marker is the binding's). The V/F curve the grain is read from is the handle's own reader, passed
// in as a callable (the caller composes it under the same handle lock).
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <vector>

#include "backend/gpu.h"
#include "backend/hook.h"
#include "backend/outcome.h"

namespace druta::backend {

class Telemetry;

// The getter a read goes through (Python's `function` name).
enum class RailGetter : int { Control = 0, Abs = 1 };  // "VoltRailsCtlGet", "VoltRailsAbs"

inline constexpr std::size_t kRailGetters = 2;
inline constexpr std::size_t kRailCount = 2;           // rails 0 (NVVDD) and 1 (MSVDD), positional
inline constexpr std::size_t kRailBlockSize = 8192;    // the zeroed buffer each attempt hands the getter
inline constexpr std::uint32_t kRailControlVersion = 0x00020AC8;  // _read_volt_rail_deltas
inline constexpr std::uint32_t kRailControlV1 = 0x00010AC8;       // the VoltRailsCtlGet fallback layout
inline constexpr std::uint32_t kLiveRailVersion = 0x00010AC8;     // GPU.LIVE_RAIL_VER
inline constexpr std::uint32_t kRailRecordBase = 0x48;            // LIVE_RAIL_BASE (both blocks)
inline constexpr std::uint32_t kRailRecordStride = 0x54;          // LIVE_RAIL_STRIDE (both blocks)
inline constexpr double kRailRetrySeconds = 2.0;
inline constexpr int kNvapiIncompatibleStructVersion = -9;

using RailBlock = std::vector<std::uint8_t>;  // kRailBlockSize bytes, as the getter left them

// One rail's entry of _volt_rail_getter_diagnostics[function]. Python's except branch (status None,
// "error" str(exc)) is reached when ctypes converts a Windows structured exception inside the getter
// into OSError. The core, like every other NVAPI call it makes, has no such guard, so a faulting
// export is not survived: "error" is always None and status always an int here.
struct RailDiagnostic {
    std::uint32_t version = 0;  // "0x%08X" in Python
    std::int32_t status = 0;
    bool valid = false;
    std::int32_t attempts = 0;  // 1 or 2
};

// {rail: buffer}: Python's out dict of _read_volt_rail_blocks.
struct RailBlocks {
    std::array<std::optional<RailBlock>, kRailCount> rails;
};

// _read_volt_rail_deltas' row: type, then VOLT_LIMIT_FIELDS in millivolts.
struct RailDeltas {
    std::int32_t type = 0;
    double reliability = 0, alt_reliability = 0, overvoltage = 0, vmin = 0;
};

// read_volt_rail_state's row: "_boost_mv", then LIVE_RAIL_FIELDS ("type" the raw u32, the rest mV).
struct RailState {
    double boost_mv = 0;
    std::uint32_t type = 0;
    double live = 0, reliability = 0, alt_reliability = 0, overvoltage = 0, effective = 0, vmin = 0;
};

// The four caches, as the GPU keeps them. An absent optional is an absent dict key.
struct RailCaches {
    std::array<std::optional<std::uint32_t>, kRailGetters> known;  // bit r: rail r in the tuple
    std::array<std::array<std::optional<double>, kRailCount>, kRailGetters> retry_after;
    std::array<std::array<std::optional<RailDiagnostic>, kRailCount>, kRailGetters> diagnostics;
    std::array<std::optional<std::uint32_t>, kRailGetters> versions;
};

inline constexpr double kVoltLimitMinMv = 650.0;   // VOLT_LIMIT_MIN_MV (a request bound, not a measurement)
inline constexpr double kVoltLimitMaxMv = 1200.0;  // VOLT_LIMIT_MAX_MV (the same)
inline constexpr double kVoltLimitXocMaxMv = 1500.0;  // VOLT_LIMIT_XOC_MAX_MV (the same, with XOC on)
inline constexpr std::uint32_t kArchPascal = 4;    // GPU.ARCH_PASCAL (NVML's enum)

// _RAIL_NAME[rail] for rails 0 and 1 ("core rail (NVVDD)", "MSVDD"); nullptr for any other rail.
const char* rail_name(std::size_t rail);

using RailFields = std::array<double, 4>;          // VOLT_LIMIT_FIELDS order, mV
using RailInitial = std::array<std::int32_t, 4>;   // VOLT_LIMIT_FIELDS order, round(delta * 1000)

// One row of read_volt_rail_limits: _read_volt_rail_deltas' row, then the keys the reader adds.
struct RailLimitRow {
    RailDeltas deltas;                    // type, reliability .. vmin
    std::optional<RailFields> base;       // _base_mv: the reference's bases, or {} (nullopt)
    // _headroom_mv is always None here; _reference_kind is a constant text (the binding adds both).
    bool absolute_ok = false;             // _absolute_ok: the rail was in this read's state
    double boost_mv = 0.0;                // _boost_mv: the state's, else 0.0
    std::optional<double> effective_mv;   // _effective_mv: the state's "effective", else None
};
using RailLimits = std::array<std::optional<RailLimitRow>, kRailCount>;

// _volt_rail_profile(): the reference and the first-read record as they stand, rails in insertion
// order (both dicts gain a rail at the same moment, so one order serves both).
struct RailProfile {
    std::array<std::optional<RailFields>, kRailCount> bases;     // "bases" (and "fields": a key here)
    std::array<std::optional<RailInitial>, kRailCount> poweron;  // "poweron"
    std::vector<std::size_t> order;                              // the dicts' key order
    std::uint32_t control_version = kRailControlVersion;         // "control_version"
    bool settle_vmin = false;                                    // "settle_vmin"
    double min_mv = kVoltLimitMinMv, max_mv = kVoltLimitMaxMv;   // "min_mv", "max_mv"
};

// volt_rail_diagnostics' per-rail reasons, in their precedence order.
enum class RailReason : int {
    none = 0,                   // ""
    no_architecture = 1,        // "GPU architecture could not be detected. Retry detection."
    unknown_architecture = 2,   // "Rail limit control is not yet understood for this GPU architecture."
    no_control = 3,             // "The driver did not return this rail's control settings. Retry detection."
    invalid_state = 4,          // "The driver did not return a valid rail status. Retry detection."
    no_reference = 5,           // "Waiting for stable voltage readings to establish this rail's reference."
};
const char* rail_reason_text(RailReason reason);

struct RailDiagnosis {
    std::optional<std::uint32_t> architecture;           // "architecture"
    std::array<bool, kRailCount> present{};              // the rail is a key of "rails"
    std::array<RailReason, kRailCount> reasons{};        // its "reason" ("available" is reason none)
    bool available = false;                              // "available"
    RailReason reason = RailReason::none;                // "reason": none, or rail 0's
    const char* architecture_source = nullptr;           // "architecture_source" (None: nullptr)
};

// stock_limit_mv's outcome: None, a value, or the KeyError of a field outside VOLT_LIMIT_FIELDS.
struct RailStockLimit {
    enum class Kind { none = 0, value = 1, bad_field = 2 } kind = Kind::none;
    double mv = 0.0;
};

// volt_rail_limits_mv(): per rail, abs_limit_mv of each field (NaN where the base is absent).
using RailLimitsMv = std::array<std::optional<RailFields>, kRailCount>;

// Increment R4: _write_rail_records, the preconditions of _write_rail_records_locked, and
// _verify_rail_records. A record is one entry of Python's `records` dict as the binding describes it.
// The kind of a record's key: a plain int, another int (a bool: `1 << key` works but
// rail_transport._is_plain_int refuses it), or a key for which `1 << key` raises TypeError (a float,
// Decimal or Fraction equal to the rail).
enum class RailKey : int { plain = 0, other_int = 1, no_shift = 2 };

struct RailRecord {
    std::int32_t rail = 0;                  // the rail the key equals (0 or 1; keys naming no rail are `foreign`)
    RailKey key = RailKey::plain;
    std::uint32_t count = 0;                // len(values)
    bool is_list = true;                    // the values are a list (else a tuple): verify compares and prints it
    bool exact = false;                     // count 4 and every value a plain int in the int32 range
    std::array<std::int64_t, 4> values{};   // the first min(count, 4) values; one equal to no integer is INT64_MIN
};
struct RailRecords {
    std::array<RailRecord, kRailCount> records{};
    std::size_t count = 0;                  // records in the dict's order
    bool foreign = false;                   // the dict also holds a key that names no rail
};

// _write_rail_records' (ok, status) and what it left in the writer's three attributes.
struct RailWriteOutcome {
    bool ok = false;
    std::optional<std::uint32_t> status;
    // false: no locked write got past its preconditions, so _volt_rail_write_error stays as the
    // sequence cleared it (None) and the protocol and observed packets are untouched. true: `result`
    // is the last locked write that ran, and it set all three.
    bool attributes = false;
    std::uint32_t writes = 0;               // locked writes that ran past their preconditions
    // Python's TypeError at `rail_mask = sum(1 << rail ...)`: a no_shift key reached it. The sequence
    // stops there (ok false, status nullopt, attributes untouched); the binding raises.
    bool type_error = false;
    RailWriteResult result;
};

// _verify_rail_records' (back, error): back is set exactly when error is empty.
struct RailVerifyOutcome {
    std::optional<RailLimits> back;
    std::string error;
};

// The hold-headroom record (Python's _hold_headroom dict, apply_hold_headroom's record): one store
// that the R5 setters adopt into and clear and the R6 runtime writes, restores and checks. The dict's
// keys in their order: "rail", "hold_mv", "margin_mv", "ceiling_mv", "boost_mv", "live_offset_mv",
// "grain_mv" (None: nullopt), "input_uv" (HOLD_HEADROOM_FIELDS), "prior_uv" and "written_uv" (keyed
// by VOLT_LIMIT_FIELDS, an absent optional an absent key, in field order as apply inserts them), and
// "raised_ceiling_mv" only once the verified read-back has set it. A float key a record set from
// outside lacks (the binding's mirror) is NaN, which every reader treats as Python treats the
// missing key.
// The record's keys besides "rail" and "raised_ceiling_mv" (HoldRecord::keys): a record apply made
// has them all; one set from outside has those its dict had. Readers that index a missing key
// (restore's record["written_uv"] / record["prior_uv"]) raise KeyError as Python does.
inline constexpr std::uint32_t kHoldKeyHold = 1u << 0, kHoldKeyMargin = 1u << 1, kHoldKeyCeiling = 1u << 2,
                               kHoldKeyBoost = 1u << 3, kHoldKeyLiveOffset = 1u << 4, kHoldKeyGrain = 1u << 5,
                               kHoldKeyInput = 1u << 6, kHoldKeyPrior = 1u << 7, kHoldKeyWritten = 1u << 8,
                               kHoldKeysAll = (1u << 9) - 1u;
// HoldOutcome::key_error beyond the four field indices: the dict that was missing.
inline constexpr std::size_t kHoldKeyErrorWritten = 4, kHoldKeyErrorPrior = 5;

struct HoldRecord {
    bool present = false;  // false: None
    std::int32_t rail = 0;
    std::uint32_t keys = 0;  // kHoldKey* bits
    double hold_mv = 0.0, margin_mv = 0.0, ceiling_mv = 0.0, boost_mv = 0.0, live_offset_mv = 0.0;
    std::optional<double> grain_mv;
    std::array<std::optional<std::int32_t>, 3> input_uv;  // HOLD_HEADROOM_FIELDS, a prefix of the fields
    std::array<std::optional<std::int32_t>, 4> written_uv;
    std::array<std::optional<std::int32_t>, 4> prior_uv;
    std::optional<double> raised_ceiling_mv;
};

// The session state the hold-headroom runtime keeps besides the record (GPU attributes, absent until
// set: an absent optional or false).
struct HoldState {
    std::optional<double> grain;      // _hold_headroom_grain
    std::optional<double> offset_mv;  // _hold_headroom_offset_mv
    bool unread = false;              // _hold_headroom_unread
    bool live_trip = false;           // _hold_headroom_live_trip
};

// apply_hold_headroom / restore_hold_headroom's (ok, message): ok 1 True, 0 None, -1 False.
// key_error: Python's KeyError raised out of restore on a record set from outside, nothing after it
// ran: a field index (record["prior_uv"][key] without that key), kHoldKeyErrorWritten
// (record["written_uv"]) or kHoldKeyErrorPrior (record["prior_uv"]).
struct HoldOutcome {
    int ok = 1;
    std::string message;
    std::optional<std::size_t> key_error;
    bool aborted = false;  // the before_write hook raised a BaseException: the binding re-raises it
};

// What apply_hold_headroom's before_write(record) returned: anything but False (proceed), False,
// an Exception (raised; its str() in the error text), or a BaseException the binding re-raises.
// No value is zero: a hook that fails without answering is refused, never taken for proceed.
enum class Tracked : int { proceed = 1, refused = 2, raised = 3, abort = 4 };
using BeforeWrite = std::function<Tracked(const HoldRecord& planned, std::string& error)>;
// read_vf_curve()'s points' volt_mv (nullopt: no points, or the read raised).
using CurveVolts = std::function<std::optional<std::vector<double>>()>;

// hold_headroom_rail_unavailable's (kind, detail).
struct HoldUnavailable {
    bool transient = false;  // "transient", else "unanswered"
    std::string detail;
};

// hold_headroom_overwritten's answer: Python's False, True, or the OverflowError of int(round(inf)).
enum class HoldOverwritten : int { no = 0, yes = 1, overflow = 2 };

// The bits of apply's `invalid`: float(hold_mv) / float(margin_mv) raised TypeError or ValueError.
inline constexpr std::uint32_t kHoldBadHold = 1u;
inline constexpr std::uint32_t kHoldBadMargin = 2u;

// set_volt_rail_limits' keyword arguments that name a field: the field indices in the caller's
// keyword order and, per field index, its value in mV (nullopt: passed as None). `unknown` is
// Python's ", ".join(sorted(unknown)) of the other keywords, nullopt when there are none; a present
// text may be empty (the keyword "") and is refused for its presence.
struct RailLimitRequest {
    std::size_t count = 0;
    std::array<std::size_t, 4> order{};
    std::array<std::optional<double>, 4> value_mv;
    std::optional<std::string> unknown;
};

// set_volt_rail_limits_raw's keyword arguments, already checked by the binding (named fields, plain
// ints in the int32 range): field indices in keyword order and, per field index, the microvolts.
struct RailRawRequest {
    std::size_t count = 0;
    std::array<std::size_t, 4> order{};
    std::array<std::int32_t, 4> uv{};
};

// reset_volt_rail_limits' `fields`: has_fields is its truthiness; mask is the set of keys of
// tuple(fields) that name a field; bad is ", ".join(sorted(set(keys) - VOLT_LIMIT_FIELDS)), nullopt
// when that set is empty (a present text may be empty: the key ""); joined is ", ".join(keys), the
// text the success message prints.
struct RailResetRequest {
    bool has_fields = false;
    std::uint32_t mask = 0;
    std::optional<std::string> bad;
    std::string joined;
};

// What the _write_rail_records calls of one setter left in the writer's three attributes: calls is
// how many ran (0: untouched); attributes is whether any ran a locked write (result is the last such
// write's, which set _volt_rail_write_protocol and _volt_rail_observed_packets); last_attributes is whether
// the LAST call did (else that call cleared _volt_rail_write_error to None).
struct RailWriterTrace {
    std::uint32_t calls = 0;
    bool attributes = false;
    bool last_attributes = false;
    RailWriteResult result;
};

class Rails {
public:
    explicit Rails(Gpu& gpu, Telemetry& telemetry) : gpu_(gpu), telemetry_(telemetry) {}

    // _read_volt_rail_blocks(function, version): {} without a ready NVAPI half or getter.
    RailBlocks blocks(RailGetter function, std::uint32_t version);
    // _read_volt_rail_deltas(): VoltRailsCtlGet at 0x00020AC8.
    std::array<std::optional<RailDeltas>, kRailCount> deltas();
    // read_volt_rail_state(): nullopt is Python's None (no rail kept).
    std::optional<std::array<std::optional<RailState>, kRailCount>> state();
    // read_rail_live_mv(rail): one state() read, then that rail's live mV or nullopt.
    std::optional<double> live_mv(std::int32_t rail);

    // read_volt_rail_limits(): nullopt is Python's None (no row).
    std::optional<RailLimits> limits();
    // _volt_rail_profile(): nullopt unless the architecture is one of _VOLT_RAIL_ARCHITECTURES.
    std::optional<RailProfile> profile();
    // volt_rail_diagnostics() without the keys the binding fills from its own state.
    RailDiagnosis diagnostics();
    // volt_rail_limits_supported(rail): rail nullopt is Python's None; any rail other than 0 and 1
    // is a key no set holds.
    bool limits_supported(std::optional<std::int32_t> rail);
    // volt_rail_limit_fields(rail): 4 (VOLT_LIMIT_FIELDS) or 0 (the empty tuple).
    std::size_t limit_fields(std::int32_t rail);
    // stock_limit_mv(rail, key) with key as its VOLT_LIMIT_FIELDS index (anything else is bad_field).
    RailStockLimit stock_limit_mv(std::int32_t rail, std::int32_t field);
    // volt_rail_limits_mv(): nullopt when read_volt_rail_limits() is None.
    std::optional<RailLimitsMv> limits_mv();

    // _write_rail_records(records): under the process-wide rail hook mutex (taken after the caller's
    // handle lock), a pre-write limits() snapshot, the profile, the settle decision (the profile's
    // settle_vmin only), the locked write and, when it settled with status 0, a verified identity
    // re-send.
    RailWriteOutcome write_records(const RailRecords& records);
    // _verify_rail_records(records, boost): boost nullopt is Python's None.
    RailVerifyOutcome verify_records(const RailRecords& records, std::optional<std::int32_t> boost);

    // Increment R5. Each setter notes its _write_rail_records calls in the caller's `trace` as they
    // run, so the trace still describes a write that ran when a later step throws.
    // set_volt_rail_limits(rail, **limits); rail -1 is any key that names no rail.
    Outcome set_limits(std::int32_t rail, const RailLimitRequest& limits, bool write_enabled, bool xoc,
                       RailWriterTrace& trace);
    // set_volt_rail_limits_raw(rail, **limits_uv) after the binding's type and range check.
    Outcome set_limits_raw(std::int32_t rail, const RailRawRequest& limits, bool write_enabled, bool xoc,
                           RailWriterTrace& trace);
    // reset_volt_rail_limits(rail, fields); rail nullopt is None.
    Outcome reset_limits(std::optional<std::int32_t> rail, const RailResetRequest& fields, RailWriterTrace& trace);
    // _adopt_verified_hold_fields(rail, fields, back), fields as a VOLT_LIMIT_FIELDS bit mask.
    void adopt_verified_hold_fields(std::int32_t rail, std::uint32_t fields, const RailLimits& back);
    const HoldRecord& hold_record() const { return hold_; }
    void set_hold_record(const HoldRecord& record) { hold_ = record; }

    // Increment R6: the hold-headroom runtime. Every write runs through write_records and is noted in
    // `trace` as the R5 setters note theirs.
    // hold_headroom_grain_mv(): the cached grain when truthy, else this card's modal V/F step.
    std::optional<double> hold_grain_mv(const CurveVolts& volts);
    // hold_headroom_rail_unavailable(rail): nullopt is None. Any rail other than 0 and 1 is a key no
    // cache or diagnosis holds.
    std::optional<HoldUnavailable> hold_rail_unavailable(std::int32_t rail);
    // hold_headroom_overwritten(limits): given nullptr reads read_volt_rail_limits(); a given nullopt
    // is a limits of None.
    HoldOverwritten hold_overwritten(const std::optional<RailLimits>* given);
    // apply_hold_headroom(hold_mv, margin_mv, rail, before_write); before_write empty is None. `xoc` is
    // the session's voltage_xoc_enabled, passed per call. rail -1 is any key that names no rail.
    HoldOutcome apply_hold(double hold_mv, double margin_mv, std::uint32_t invalid, std::int32_t rail, bool xoc,
                           const CurveVolts& volts, const BeforeWrite& before_write, RailWriterTrace& trace);
    // restore_hold_headroom(force).
    HoldOutcome restore_hold(bool force, RailWriterTrace& trace);
    // hold_headroom_live_ok(record, live_mv): record nullptr is the active record; live nullopt reads
    // the record's rail.
    Outcome hold_live_ok(const HoldRecord* record, std::optional<double> live);
    const HoldState& hold_state() const { return state_; }
    void set_hold_state(const HoldState& state) { state_ = state; }
    // _volt_rail_initial_uv[rail] (adopt_foreign_hold_headroom's read and its in-place replacement of
    // an existing entry; a rail without a first read stays without one).
    const std::optional<RailInitial>& initial(std::size_t rail) const { return initial_[rail]; }
    bool replace_initial(std::size_t rail, const RailInitial& values);

    // The four caches _refresh_volt_rail_capabilities resets (its _volt_rail_write_error is the
    // writer's, not part of these getters). The reference and the first-read record stay: refresh
    // retries discovery without redefining the session's restore target.
    void refresh() { caches_ = RailCaches{}; }
    const RailCaches& caches() const { return caches_; }

private:
    bool has_reference() const { return !order_.empty(); }
    // _write_rail_records_locked: false when a precondition refused or the rail mask raised (nothing
    // ran, attributes untouched; out.type_error tells the two apart).
    bool write_records_locked(const RailRecords& records, RailWriteOutcome& out);
    // write_records, noting what it left in the writer's attributes.
    RailWriteOutcome write_traced(const RailRecords& records, RailWriterTrace& trace);

    Gpu& gpu_;
    Telemetry& telemetry_;
    RailCaches caches_;
    std::array<std::optional<RailFields>, kRailCount> reference_;  // _volt_rail_reference[rail]["bases"]
    std::array<std::optional<RailInitial>, kRailCount> initial_;   // _volt_rail_initial_uv[rail]
    std::vector<std::size_t> order_;  // the rails of reference_ in insertion order (initial_ follows it)
    HoldRecord hold_;                 // _hold_headroom
    HoldState state_;                 // _hold_headroom_grain, _offset_mv, _unread, _live_trip
};

}  // namespace druta::backend
