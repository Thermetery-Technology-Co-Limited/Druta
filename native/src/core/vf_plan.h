// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Pure V/F curve planners: port of the static planning methods of
// nvbackend.GPU (peak_info .. compute_rephase). No driver, no Windows.
//
// Exact parity with the Python is the contract, so the arithmetic follows
// Python's rules rather than C's: a value that Python keeps as an int is an
// int64 here, a float operand turns the result into a double (Num), int() and
// round() refuse NaN and infinities, // and % floor towards negative infinity,
// int-versus-float comparisons are exact, and max()/min() keep the FIRST of
// equal candidates. Points are referred to by their position in the caller's
// array; the caller echoes its own idx and volt_mv values from those.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <utility>
#include <vector>

namespace druta::vfplan {

// Existing hard-de-flatten UI preset. Callers still pass an explicit floor to the planner;
// this is not a measured device value or a lower bound imposed by the core.
inline constexpr double kHardFloorMv = 800.0;

inline constexpr std::size_t kMaxPoints = 128;           // VFP_POINTS
inline constexpr std::int64_t kStepKhz = 15000;          // VF_STEP_KHZ, the fallback grid
inline constexpr std::int64_t kMaxRiseKhz = 45000;       // VF_MAX_RISE_KHZ
inline constexpr std::int64_t kMaxRiseBins = 3;          // VF_MAX_RISE_BINS
inline constexpr std::int64_t kExtraPointsAboveCap = 0;  // GPU.EXTRA_POINTS_ABOVE_CAP
// Python's ints are unbounded; these are not. Every integer input and every
// rounded frequency is kept within +-2^53, where a double holds it exactly and
// the planners' sums cannot overflow int64.
inline constexpr std::int64_t kIntLimit = std::int64_t{1} << 53;

// Each value stands for the Python exception (or refusal) named beside it.
enum class Error {
    none,
    argument,       // malformed call (too many points, NULL arrays)
    floordiv_zero,  // ZeroDivisionError: x // 0
    mod_zero,       // ZeroDivisionError: x % 0
    nan_int,        // ValueError: int()/round() of NaN
    inf_int,        // OverflowError: int()/round() of an infinity
    none_int,       // TypeError: int(None)
    none_add,       // TypeError: None + 0.01 (below_cap with a None cap)
    none_sub,       // TypeError: None - 0.01 (above_floor with a None floor)
    empty_index,    // IndexError: [][0]
    empty_min,      // ValueError: min() of nothing
    float_slice,    // TypeError: slice index is a float
    unordered,      // a NaN voltage would be sorted (refused, see vfplan.h)
    big_int,        // an integer beyond +-2^53 (refused)
    missing,        // cascade_meta: a lifted row has no voltage
};

// A Python scalar: None, int or float.
struct Num {
    enum class Kind : std::uint8_t { none, integer, real };
    Kind kind = Kind::none;
    std::int64_t i = 0;
    double f = 0.0;

    static Num none() { return {}; }
    static Num of(std::int64_t v) { return {Kind::integer, v, 0.0}; }
    static Num real(double v) { return {Kind::real, 0, v}; }
    bool is_none() const { return kind == Kind::none; }
    bool is_float() const { return kind == Kind::real; }
    // float(x) for an int (correctly rounded), x for a float.
    double as_double() const { return kind == Kind::integer ? static_cast<double>(i) : f; }
};

struct Point {
    std::uint32_t idx = 0;
    double volt_mv = 0.0;
    double freq_mhz = 0.0;
    std::int32_t delta_khz = 0;
};
using Points = std::vector<Point>;

// One planned edit; idx and volt_mv are points[pos]'s.
struct Change {
    std::size_t pos = 0;
    double old_mhz = 0.0;
    double new_mhz = 0.0;
    Num delta_khz;
};

struct Plan {
    std::vector<Change> changes;  // in position order
    double before_mhz = 0.0;
    double after_mhz = 0.0;
};

// ---- peak_info / resolve_vf_point ----------------------------------------------------------------

struct Peak {
    bool found = false;      // false: (0.0, None, 0.0, 0)
    std::size_t peak_pos = 0;
    std::size_t park_pos = 0;
    std::size_t n_at_peak = 0;
};

// cap_mv none: the whole table.
Error peak_info(const Points& points, const Num& cap_mv, Peak& out);

// nullopt is Python's None.
Error resolve_vf_point(const Points& points, const Num& volt_mv, std::optional<std::size_t>& out);

// ---- compute_deflatten -------------------------------------------------------------------------

struct DeflattenMeta {
    bool full = false;       // false: only clamped/boundary_idx(None)/unique
    bool clamped = false;
    std::size_t boundary_pos = 0;
    bool unique = false;
    std::int64_t lowered = 0;
    double lowered_by_mhz = 0.0;
};

Error compute_deflatten(const Points& points, const Num& vcap_mv, const Num& max_khz,
                        const Num& extra_points_above, const Num& step_khz, Plan& plan, DeflattenMeta& meta);

// ---- the shape law ---------------------------------------------------------------------------

Error evaluate_curve_law(const std::vector<Num>& khz, const Num& max_rise_khz, std::vector<std::int64_t>& out);

// values[i]: the planned kHz of position i. real is in voltage order; order[k] is the position at
// voltage rank k.
Error predict_curve(const Points& points, const std::vector<Num>& values, const Num& max_rise_khz,
                    std::vector<std::int64_t>& real, std::vector<std::size_t>& order);

struct CascadeRow {
    Num real;
    Num khz;
    bool has_volt = true;
    double volt_mv = 0.0;
};

struct Cascade {
    std::size_t lifted_below = 0;
    double lift_max_mhz = 0.0;
    std::optional<std::size_t> lowest_row;  // the row whose volt_mv is lift_lowest_mv
    std::size_t missing_row = 0;            // Error::missing: first lifted row without a voltage
};

Error cascade_meta(const std::vector<CascadeRow>& rows, Cascade& out);

// ---- compute_hard_deflatten --------------------------------------------------------------------

struct HardMeta {
    bool has_band = false;   // false: floor/park fields are None
    double target_mhz = 0.0;
    std::size_t n_flat = 0;
    std::size_t floor_pos = 0;
    std::size_t park_pos = 0;
    double park_mhz = 0.0;
    bool parks_at_floor = false;
    std::size_t lifted_below = 0;
    double lift_max_mhz = 0.0;
    std::optional<std::size_t> lift_lowest_pos;
};

Error compute_hard_deflatten(const Points& points, const Num& floor_mv, const Num& target_khz,
                             const Num& step_khz, Plan& plan, HardMeta& meta);

// ---- compute_ramp ------------------------------------------------------------------------------

struct RampMeta {
    bool has_band = false;   // false: boundary/lo/cap fields are None
    bool clamped = false;
    bool unique = false;
    std::size_t boundary_pos = 0;  // also the cap point
    std::size_t lo_pos = 0;
    std::size_t rungs = 0;
    double top_mhz = 0.0;
    double floor_before_mhz = 0.0;
    double floor_after_mhz = 0.0;
    double floor_cost_mhz = 0.0;
    std::size_t leveled_above = 0;
    std::optional<double> under_band_mhz;
    std::size_t shadowed = 0;
    std::size_t delivered = 0;
    std::size_t lifted_below = 0;
    double lift_max_mhz = 0.0;
    std::optional<std::size_t> lift_lowest_pos;
    std::size_t dropped_rungs = 0;
};

Error compute_ramp(const Points& points, const Num& lo_mv, const Num& cap_mv, const Num& max_khz,
                   const Num& step_khz, Plan& plan, RampMeta& meta);

// ---- compute_rephase ---------------------------------------------------------------------------

struct Rephase {
    std::vector<std::pair<std::size_t, std::int64_t>> changes;  // (position in deltas, new delta)
    std::int64_t phase = 0;
};

Error compute_rephase(const std::vector<Num>& deltas, const Num& step_khz, Rephase& out);

}  // namespace druta::vfplan
