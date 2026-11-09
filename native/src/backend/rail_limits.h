// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The pure rail-limit and hold-headroom helpers (port of the GPU class's static rail helpers). Increment R1.
//
// nvbackend.GPU: VOLT_LIMIT_FIELDS, VOLT_LIMIT_BASE_MV, _VOLT_RAIL_ARCHITECTURES, HOLD_HEADROOM_FIELDS,
// HOLD_HEADROOM_MAX_MARGIN_MV, HOLD_HEADROOM_FALLBACK_GRAIN_MV (and the grain bounds of
// hold_headroom_grain_mv), _valid_volt_rail_state, volt_boost_headroom_mv, abs_limit_mv, rail_ceiling_mv,
// rail_floor_mv, hold_headroom_architecture (arch() as a parameter), hold_headroom_ceiling_mv,
// hold_headroom_targets, _headroom_enforced_mv, hold_headroom_bound_mv and hold_headroom_allowance_mv (the
// instance's hold_headroom_record() as a parameter). Nothing here reads a GPU.
//
// The Python reads dicts whose values may be missing, None, int, float or any other object. A Value keeps
// that distinction, so each helper follows Python's arithmetic exactly: int + int stays int, a float operand
// makes the result float, min()/max() keep the first of equal or unordered (NaN) values, and every place the
// Python raises returns the Error naming its exception instead. Ints travel as doubles. An int beyond +-2^53
// is a `big` value: its sign, truth, float() and range checks are exact, and so is float + big (Python
// converts the int to the nearest double too), but int + big is refused (Error::big_int), as the V/F
// planners refuse such ints, and only when the helper actually reaches it.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace druta::backend::rail {

inline constexpr std::size_t kFieldCount = 4;  // VOLT_LIMIT_FIELDS, in the order they sit in an RM record
enum Field : std::size_t { reliability = 0, alt_reliability = 1, overvoltage = 2, vmin = 3 };
const char* field_name(std::size_t field);  // "reliability", ...; nullptr beyond kFieldCount

// HOLD_HEADROOM_FIELDS: the three ceiling terms of rail_ceiling_mv, a prefix of VOLT_LIMIT_FIELDS.
inline constexpr std::size_t kHeadroomFieldCount = 3;

inline constexpr double kHoldHeadroomMaxMarginMv = 100.0;  // HOLD_HEADROOM_MAX_MARGIN_MV
// The live check's grain when this card's V/F curve cannot be read. Half a TU102 V/F point, a fallback
// only: hold_headroom_grain_mv takes the grain from the current adapter's own curve whenever it can.
// It is one card's estimate, not a generation constant, and so are the sanity bounds below
// (hold_headroom_grain_mv keeps a curve's modal step within [half, twice] the fallback).
inline constexpr double kHoldHeadroomFallbackGrainMv = 6.25;  // HOLD_HEADROOM_FALLBACK_GRAIN_MV
inline constexpr double kHoldHeadroomGrainMinMv = kHoldHeadroomFallbackGrainMv / 2;  // fallback / 2
inline constexpr double kHoldHeadroomGrainMaxMv = 2 * kHoldHeadroomFallbackGrainMv;  // 2 * fallback

// _VOLT_RAIL_ARCHITECTURES: Pascal, Turing, Ampere, Ada and Blackwell (NVML architecture numbers).
inline constexpr std::size_t kVoltRailArchitectureCount = 5;
extern const std::uint32_t kVoltRailArchitectures[kVoltRailArchitectureCount];

inline constexpr double kIntLimit = 9007199254740992.0;  // 2^53

enum class Kind { absent, none, integer, real, big, other };

// One dict value. integer and real carry the number in f (an int exactly, within +-2^53). big is an int
// beyond that: f is float(value), or +-inf where float() overflows. other is any object that is neither None
// nor an int or float: its bool() is truthy and its float(), when that succeeded, is f with has_float set.
struct Value {
    Kind kind = Kind::absent;
    bool truthy = false;     // other only
    bool has_float = false;  // other only
    double f = 0.0;
    static Value absent() { return Value{}; }
    static Value none() { return Value{Kind::none, false, false, 0.0}; }
    static Value integer(double v) { return Value{Kind::integer, false, false, v}; }
    static Value real(double v) { return Value{Kind::real, false, false, v}; }
    bool is_number() const { return kind == Kind::integer || kind == Kind::real; }
    bool is_true() const;  // Python's bool(); absent is the None that .get() returns
};

// Where the Python raises.
enum class Error {
    none,
    key,        // KeyError: fields[key] of a limit the row does not have
    type,       // TypeError: None or another non-number in arithmetic or an ordering comparison
    attribute,  // AttributeError: _base_mv present but without .get (None, ...)
    conversion, // float() of the value raised (volt_boost_headroom_mv lets it through)
    big_int     // int + int beyond +-2^53 (Python's ints are unbounded, these are not), or the
                // OverflowError of float() / math.isfinite() of an int beyond the double range
};

// A Python number result: an int (exact in v) or a float.
struct Num {
    bool is_int = false;
    double v = 0.0;
};

template <class T>
struct Result {
    Error error = Error::none;
    T value{};
};

enum class Bases { absent, mapping, no_get };

// One rail row (read_volt_rail_limits' shape) or one control state (_valid_volt_rail_state's shape).
struct Row {
    Value limit[kFieldCount];          // reliability .. vmin
    Bases bases = Bases::absent;       // _base_mv: absent means VOLT_LIMIT_BASE_MV, which is {}
    Value base[kFieldCount];           // _base_mv[key] when bases is a mapping
    Value boost_mv;                    // _boost_mv
    Value effective_mv;                // _effective_mv
    Value headroom_mv;                 // _headroom_mv
    Value type;                        // type
};

// The hold-headroom record (apply_hold_headroom's dict); only the keys the helpers read.
struct Record {
    bool truthy = false;  // bool(record): a non-empty mapping
    Value raised_ceiling_mv, hold_mv, margin_mv, grain_mv, live_offset_mv;
};

// hold_headroom_architecture with arch() as a parameter: 1 supported, 0 not, -1 unknown (arch None).
int volt_rail_architecture(bool has_arch, std::int64_t arch);

// _valid_volt_rail_state(rail, state). rail is a Value so 0.0 and True find their dict keys as in Python.
Result<bool> valid_volt_rail_state(const Value& rail, const Row& state);

// volt_boost_headroom_mv(fields); fields == nullptr is None (or an empty dict).
Result<double> volt_boost_headroom_mv(const Row* fields);

Result<Num> abs_limit_mv(const Row& fields, std::size_t key);
Result<Num> rail_ceiling_mv(const Row& fields);
Result<Num> rail_floor_mv(const Row& fields);
Result<Num> hold_headroom_ceiling_mv(const Row& row);
// _headroom_enforced_mv(row, grain); grain == nullptr is None.
Result<Num> headroom_enforced_mv(const Row& row, const Value* grain);

// hold_headroom_targets: planned == false is (None, message); planned == true is (targets, None) with
// has_target/target_mv in HOLD_HEADROOM_FIELDS order (the dict's insertion order).
enum class TargetReason { none, not_numbers, not_finite, margin_not_positive, margin_too_large, hold_too_high,
                          boost_not_number, boost_not_finite, field_missing, field_no_reference };
struct Targets {
    bool planned = false;
    TargetReason reason = TargetReason::none;
    std::size_t field = 0;  // field_missing / field_no_reference: which term
    bool has_target[kHeadroomFieldCount] = {};
    double target_mv[kHeadroomFieldCount] = {};
    std::string message;    // Python's reason text
};
Result<Targets> hold_headroom_targets(const Row& fields, const Value& hold_mv, const Value& margin_mv,
                                      const Value& maximum_mv);

// hold_headroom_bound_mv / hold_headroom_allowance_mv: `record or active or {}`, where active is the
// instance's hold_headroom_record(); nullptr is None.
Result<double> hold_headroom_bound_mv(const Record* record, const Record* active);
Result<double> hold_headroom_allowance_mv(const Record* record, const Record* active);

}  // namespace druta::backend::rail
