// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/clocks.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <map>
#include <set>

#include "backend/nvapi.h"
#include "backend/nvml.h"

namespace druta::backend {
namespace {

using HandlePtrFn = int(DRUTA_CDECL*)(void*, void*);
using LockFn = int(DRUTA_CDECL*)(void*, std::uint32_t, std::uint32_t);
using HandleFn = int(DRUTA_CDECL*)(void*);
constexpr std::uint32_t Pstates20Set = 0x0F4DAE6B;
constexpr std::uint32_t kArchPascal = 4;
constexpr std::int64_t kInt32Min = -(std::int64_t(1) << 31);

std::string g_format(double value, bool sign) {  // Python's :g / :+g
    char text[64];
    std::snprintf(text, sizeof text, sign ? "%+g" : "%g", value);
    return text;
}

std::int64_t floor_div(std::int64_t a, std::int64_t b) {
    std::int64_t q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) --q;
    return q;
}

// Infer the spacing of the adapter's evaluated GPU V/F points when NVML
// cannot enumerate lockable clocks. Some adjacent points skip several bins,
// and many top points share a frequency. Only a regular, unshaped curve with
// a run of adjacent bins and whole-bin gaps across its full range qualifies.
std::optional<std::uint32_t> curve_grid_khz(VfCurve& curve) {
    const auto layout = curve.layout();
    if (!layout || !layout->freq_div) return {};
    const auto reading = curve.read_vf_curve();
    if (!reading.points || reading.points->size() != layout->gpu_idx.size() || reading.points->empty()) return {};
    const auto& points = *reading.points;
    std::vector<double> frequencies;
    for (const auto& point : points) {
        const double khz = point.freq_mhz * 1000;
        if (point.delta_khz != points.front().delta_khz || !std::isfinite(khz) || khz <= 0 ||
            (!frequencies.empty() && khz < frequencies.back())) return {};
        if (frequencies.empty() || khz != frequencies.back()) frequencies.push_back(khz);
    }
    if (frequencies.size() < 8) return {};
    std::vector<double> gaps;
    for (size_t i = 1; i < frequencies.size(); ++i) gaps.push_back(frequencies[i] - frequencies[i-1]);
    const double smallest = *std::min_element(gaps.begin(), gaps.end());
    // Some NVAPI curves round to whole MHz in the wire domain (half MHz for GPC2CLK).
    const double resolution = 1000.0 / layout->freq_div;
    unsigned adjacent = 0, longest = 0;
    double adjacent_span = 0, seed = 0;
    for (double gap : gaps) {
        if (gap > smallest + resolution) { adjacent = 0; adjacent_span = 0; continue; }
        adjacent_span += gap;
        if (++adjacent > longest) { longest = adjacent; seed = adjacent_span / adjacent; }
    }
    if (longest < 7 || seed < 5000 || seed > 30000) return {};
    // Infer the integer distance between each pair before fitting the full
    // span. Rounding individual frequencies would accumulate quantization error.
    std::vector<double> bins{0.0};
    bool has_skipped_bin = false;
    for (double gap : gaps) {
        const double count = std::round(gap / seed);
        if (count < 1 || std::abs(gap - count * seed) > resolution + count * resolution / longest) return {};
        if (count > 1) has_skipped_bin = true;
        bins.push_back(bins.back() + count);
    }
    // A uniformly sampled voltage ramp alone could be stepping by two or more
    // physical bins. Require the same spacing to explain sparse rows as well.
    if (!has_skipped_bin) return {};
    // The difference of two rounded readings can be off by one quantum.
    // Intersect the step intervals from *all* pairs. A single step and phase
    // must explain every frequency, including the points that skip bins.
    double low = 5000, high = 30000;
    for (size_t end = 1; end < bins.size(); ++end) {
        for (size_t start = 0; start < end; ++start) {
            const double span = frequencies[end] - frequencies[start];
            const double count = bins[end] - bins[start];
            low = std::max(low, (span - resolution) / count);
            high = std::min(high, (span + resolution) / count);
            if (low > high) return {};
        }
    }
    return static_cast<std::uint32_t>(std::round((low + high) / 2));
}

}  // namespace

MemOffsetScale Clocks::mem_offset_scale() const {
    // NVML mem units are DDR-doubled. For a known GDDR type the slider is in
    // TRUE memory MHz (NVML = true * 2 * div); otherwise the reported scale.
    if (caps_.mem_div && *caps_.mem_div) return {2 * *caps_.mem_div, "MHz true"};
    return {2, "MHz eff"};
}

std::vector<ClockRow> Clocks::lockable_clocks_by_mem() {
    // The driver enumerates a DIFFERENT lockable set per memory clock.
    std::vector<ClockRow> out;
    Nvml& nv = gpu_.nvml;
    if (!(nv.ok && nv.has("nvmlDeviceGetSupportedGraphicsClocks"))) return out;
    for (std::uint32_t m : caps_.mem_clocks) {
        std::vector<std::uint32_t> g = telemetry_.supported_nvml_clocks("nvmlDeviceGetSupportedGraphicsClocks", m);
        if (!g.empty()) out.push_back({m, std::move(g)});
    }
    return out;
}

std::uint32_t Clocks::clock_step_khz() {
    // Span over gaps within the upper, contiguous boost regime of the longest
    // lockable row: GP102's consecutive differences alternate 12 and 13, so a
    // median would accumulate error; the endpoints do not.
    if (step_cache_) return *step_cache_;
    std::optional<std::uint32_t> step;
    std::vector<ClockRow> table = lockable_clocks_by_mem();
    const std::vector<std::uint32_t>* longest = nullptr;
    for (const ClockRow& row : table)
        if (!longest || row.graphics.size() > longest->size()) longest = &row.graphics;  // first maximum, as max()
    std::vector<std::uint32_t> best;
    if (longest) {
        std::set<std::uint32_t> unique(longest->begin(), longest->end());
        best.assign(unique.begin(), unique.end());
    }
    if (best.size() >= 8) {
        // Kepler mixes divider regimes; measure the contiguous upper regime,
        // allowing integer rounding of a fractional clock bin.
        const std::int64_t top_gap = std::int64_t(best[best.size() - 1]) - best[best.size() - 2];
        std::size_t start = best.size() - 2;
        while (start > 0 && std::llabs((std::int64_t(best[start]) - best[start - 1]) - top_gap) <= 1) --start;
        best.erase(best.begin(), best.begin() + static_cast<std::ptrdiff_t>(start));
    }
    if (best.size() >= 8) {
        const std::uint32_t span = best.back() - best.front();
        if (span > 0) step = static_cast<std::uint32_t>(std::nearbyint(span * 1000.0 / static_cast<double>(best.size() - 1)));
    }
    // Keep the NVML grid authoritative when it is available. Otherwise a
    // validated V/F lattice supplies independent evidence on older drivers.
    step_measured_ = step && *step >= 5000 && *step <= 30000;
    if (!step_measured_) step = curve_grid_khz(curve_);
    step_cache_ = step.value_or(kVfStepKhz);
    return *step_cache_;
}

bool Clocks::step_is_measured() {
    clock_step_khz();
    return step_measured_;
}

Outcome Clocks::set_pstate20_offset(std::uint32_t ctype, std::int64_t units, double mhz, const std::string& unit) {
    // One ordinary P0 clock offset, with no voltage/other-domain rows.
    Nvapi& a = gpu_.nvapi;
    const std::string dom = ctype == 0 ? "core" : "mem";
    std::string err;
    auto info = telemetry_.read_pstates20(err);
    const Pstate20Clock* clock = info ? telemetry_.pstate20_clock(*info, ctype) : nullptr;
    if (!clock) return {false, !err.empty() ? err : "no unique P0 " + dom + " clock entry"};
    if (!(clock->flags & 1)) return {false, "P0 " + dom + " clock offset is not editable"};
    const std::int64_t wire_scale = ctype == 2 ? 2 : 1;
    const std::int64_t delta_khz = floor_div(units * 1000, wire_scale);
    if (!(clock->delta.minimum <= delta_khz && delta_khz <= clock->delta.maximum))
        return {false, dom + " offset exceeds the current P0 range"};
    // The request is the type the getter accepted: V2 or V1, by its version word.
    Pstates20V2 v2{};
    Pstates20V1 v1{};
    void* request = info->v2 ? static_cast<void*>(&v2) : static_cast<void*>(&v1);
    auto fill = [&](auto& table) {
        table.version = info->table.version;
        table.num_pstates = 1;
        table.num_clocks = 1;
        table.pstates[0].pstate = 0;
        table.pstates[0].clocks[0].domain = clock->domain;
        table.pstates[0].clocks[0].delta.value = static_cast<std::int32_t>(delta_khz);
    };
    if (info->v2) fill(v2); else fill(v1);
    const int status = a.get<HandlePtrFn>(Pstates20Set)(a.gpu, request);
    if (status != 0) return {false, dom + " offset failed: NVAPI status " + std::to_string(status)};
    std::string err2;
    auto readback = telemetry_.read_pstates20(err2);
    const Pstate20Clock* applied = readback ? telemetry_.pstate20_clock(*readback, ctype) : nullptr;
    if (!applied) return {false, dom + " offset was sent, but readback failed: " + (!err2.empty() ? err2 : "missing P0 entry")};
    if (applied->delta.value != delta_khz)
        return {false, dom + " offset requested " + g_format(mhz, true) + " " + unit + ", but the driver reported " +
                           g_format(static_cast<double>(applied->delta.value) * static_cast<double>(wire_scale) / 1000.0, false) +
                           " API MHz"};
    return {true, dom + " offset set to " + g_format(mhz, true) + " " + unit + " (NVAPI Pstates20)"};
}

std::pair<std::optional<bool>, std::string> Clocks::keep_vf_shape(
    const std::vector<std::pair<std::uint32_t, std::int32_t>>& deltas, std::int64_t shift_khz) {
    // Write deltas back when an offset call flattened them; shift_khz is the
    // change of the graphics offset in the delta field's own units.
    if (deltas.empty()) return {false, "the saved V/F shape could not be shifted"};
    std::vector<std::pair<std::int64_t, std::int64_t>> desired;
    for (const auto& [idx, delta] : deltas) {
        const std::int64_t moved = std::int64_t(delta) + shift_khz;
        if (moved > VfCurve::kMaxAbsDeltaKhz || moved < -VfCurve::kMaxAbsDeltaKhz)
            return {false, "point " + std::to_string(idx) + " would exceed the delta limit"};
        desired.emplace_back(idx, moved);
    }
    CurveResult curve = curve_.read_vf_curve();
    if (!curve.error.empty() || !curve.points || curve.points->empty())
        return {false, !curve.error.empty() ? curve.error : "curve re-read returned nothing"};
    auto current_of = [](const std::vector<VfPoint>& points) {
        std::map<std::int64_t, std::int64_t> current;
        for (const VfPoint& p : points) current[p.idx] = p.delta_khz;
        return current;
    };
    std::map<std::int64_t, std::int64_t> current = current_of(*curve.points);
    bool same = true;
    for (const auto& [idx, delta] : desired) {
        auto it = current.find(idx);
        if (it == current.end() || it->second != delta) { same = false; break; }
    }
    if (same) return {std::nullopt, ""};
    Outcome applied = curve_.apply_vf_deltas(desired);
    if (!applied.ok) return {false, applied.message};
    curve = curve_.read_vf_curve();
    if (!curve.error.empty() || !curve.points || curve.points->empty())
        return {false, !curve.error.empty() ? curve.error : "curve write was sent but the re-read returned nothing"};
    current = current_of(*curve.points);
    std::vector<std::int64_t> missing;
    for (const auto& [idx, delta] : desired) {
        auto it = current.find(idx);
        if (it == current.end() || it->second != delta) missing.push_back(idx);
    }
    if (!missing.empty()) {
        std::string shown;
        for (std::size_t i = 0; i < std::min<std::size_t>(missing.size(), 8); ++i)
            shown += (i ? ", " : "") + std::to_string(missing[i]);
        return {false, "curve write was sent but points " + shown + " did not stick"};
    }
    return {true, "V/F shape kept"};
}

Outcome Clocks::set_clock_offset(std::uint32_t ctype, double mhz, bool preserve_curve) {
    if (ctype != 0 && ctype != 2) return {false, "clock offset type must be 0 (core) or 2 (memory)"};
    if (!std::isfinite(mhz)) return {false, "clock offset must be a finite number"};
    if (!(static_cast<double>(kInt32Min) <= mhz && mhz < -static_cast<double>(kInt32Min)))
        return {false, "clock offset exceeds the driver's signed 32-bit range"};
    Nvml& nv = gpu_.nvml;
    Nvapi& a = gpu_.nvapi;
    const bool modern = nv.ok && nv.has("nvmlDeviceSetClockOffsets");
    const bool pstates_set = a.ok && a.interface(Pstates20Set) != nullptr;
    if (!modern && !pstates_set) return {false, "clock offset setter is unavailable"};
    const std::string dom = ctype == 0 ? "core" : "mem";
    double scale;
    std::string unit;
    if (ctype == 0) {
        // Snap onto THIS CARD's grid, then round UP to the next whole MHz:
        // measured on GP102, the driver FLOORS the offset to whole bins, so a
        // nearest-rounded request lands one bin short. On an integer grid
        // (TU102's 15) the ceiling is a no-op.
        double whole = std::trunc(mhz);  // Python int()
        const double step_mhz = clock_step_khz() / 1000.0;
        const double want = std::nearbyint(whole / step_mhz) * step_mhz;
        const double truncated = std::trunc(want);
        mhz = truncated + (want > truncated ? 1 : 0);
        scale = 1;
        unit = "MHz";
    } else {
        MemOffsetScale mem = mem_offset_scale();
        scale = mem.scale;
        unit = mem.unit;
        if (!(0 < scale && scale < -static_cast<double>(kInt32Min))) return {false, "memory offset scale is invalid"};
    }
    const double scaled = mhz * scale;
    if (!(static_cast<double>(kInt32Min) <= scaled && scaled < -static_cast<double>(kInt32Min)))
        return {false, dom + " offset exceeds the driver's signed 32-bit range"};
    const std::int64_t units = static_cast<std::int64_t>(std::trunc(scaled));
    const std::int64_t step = ctype == 2 ? memory_offset_step_units() : 1;
    if (static_cast<double>(units) != scaled || units % step != 0)
        return {false, dom + " offset " + g_format(mhz, true) + " " + unit + " is not representable; use multiples of " +
                           g_format(static_cast<double>(step) / scale, false) + " " + unit};
    const auto before = telemetry_.read_offset(ctype);
    const auto& rng = before.range;
    if (before.ambiguous) return {false, dom + " offset readbacks disagree or are unavailable. No write issued"};
    if (modern && ctype == 2 && !rng) return {false, "memory offset range/readback is unavailable; no write issued"};
    std::int64_t lo, hi;
    if (rng) {
        lo = rng->low;
        hi = rng->high;
    } else {
        lo = ctype == 0 ? -1000 : -2000;
        hi = ctype == 0 ? 1000 : 6000;
    }
    if (!(lo <= units && units <= hi))
        return {false, dom + " offset " + g_format(mhz, true) + " " + unit + " out of range [" +
                           g_format(static_cast<double>(lo) / scale, false) + ".." +
                           g_format(static_cast<double>(hi) / scale, false) + "]"};
    // Snapshot the graphics V/F shape only after the request is known to be
    // legal; put it back after the driver flattens it, shifted when the core
    // offset changed. A curve already flat matches the driver's write.
    std::optional<DeltaSnapshot> shape = preserve_curve ? curve_.vf_delta_snapshot() : std::nullopt;
    std::optional<std::int64_t> old_core;
    if (shape && ctype == 0 && rng) old_core = rng->current;
    if (shape && ctype == 0 && !old_core) shape.reset();
    // Re-sending the offset already stored is what flattened this card.
    if (shape && rng && rng->current == units) return {true, dom + " offset set to " + g_format(mhz, true) + " " + unit};
    if (shape && before.pair) {
        const auto check = telemetry_.read_offset(ctype);
        if (!check.range || !check.pair || !before.pair->same_reading(*check.pair))
            return {false, dom + " offset baseline changed during V/F capture. No write issued"};
    }
    Outcome result;
    bool verify_nvml = false;
    if (!modern) {
        result = set_pstate20_offset(ctype, units, mhz, unit);
    } else {
        ClockOffset co{};
        co.version = nvml_ver<ClockOffset>(1);
        co.type = ctype;
        co.pstate = 0;
        co.off = static_cast<std::int32_t>(units);
        const int st = nv.get<HandlePtrFn>("nvmlDeviceSetClockOffsets")(nv.dev, &co);
        if (st == 3 && pstates_set) {
            // NVML_ERROR_NOT_SUPPORTED: an export can be a stub. Only a definite
            // lack of capability permits another setter.
            result = set_pstate20_offset(ctype, units, mhz, unit);
        } else if (st != 0) {
            result = {false, dom + " offset failed: " + nv.errstr(st)};
        } else {
            result = {true, dom + " offset set to " + g_format(mhz, true) + " " + unit};
            verify_nvml = ctype == 2 || shape.has_value() || before.pair.has_value();
        }
    }
    if (result.ok && (before.pair || verify_nvml)) {
        // Read the verified request through the same interface used by subsequent Apply and Undo.
        // An exact P0 change from a known baseline can explain a stale NVML reply without a SET.
        auto after = telemetry_.read_offset(ctype);
        if (before.pair && !after.pair) after = telemetry_.read_offset(ctype);
        const bool verified = before.pair
            ? telemetry_.confirm_paired_offset(ctype, before, after, static_cast<std::int32_t>(units))
            : after.range && after.range->current == units;
        if (!verified) {
            if (!after.range || after.range->current == units ||
                (before.pair && (!after.pair || !before.pair->same_layout(*after.pair))))
                result = {false, (ctype == 2 ? std::string("memory") : dom) + " offset was sent, but readback failed"};
            else
                result = {false, dom + " offset requested " + g_format(mhz, true) + " " + unit + ", but the driver reported " +
                                     g_format(static_cast<double>(after.range->current) / scale, true) + " " + unit};
        } else if (verify_nvml && before.pair && after.pair->nvml.current != units) {
            result.message += " (verified by NVAPI Pstates20)";
        }
    }
    if (!shape) return result;
    // A refused call can still flatten the table: put the saved points back
    // either way, shifted only when the core offset was accepted.
    std::int64_t shift = 0;
    if (result.ok && ctype == 0) shift = (static_cast<std::int64_t>(std::trunc(mhz)) - *old_core) * 1000 * shape->freq_div;
    auto [changed, note] = keep_vf_shape(shape->deltas, shift);
    if (changed && !*changed) {
        const std::string extra = "the V/F curve was flattened and could not be restored: " + note;
        if (result.ok) return {false, result.message + ", but " + extra};
        return {false, result.message + "; " + extra};
    }
    if (!note.empty()) result.message += "; " + note;
    return result;
}

Outcome Clocks::lock_gpu_clocks(std::int64_t mn_mhz, std::int64_t mx_mhz) {
    last_lock_.reset();
    Nvml& nv = gpu_.nvml;
    if (!nv.ok || !nv.has("nvmlDeviceSetGpuLockedClocks")) return {false, "SetGpuLockedClocks not available"};
    if (mn_mhz > mx_mhz) return {false, "lock: min " + std::to_string(mn_mhz) + " > max " + std::to_string(mx_mhz) + " MHz"};
    if (caps_.gfx_min && caps_.gfx_max) {
        const std::int64_t lo = *caps_.gfx_min, hi = *caps_.gfx_max;
        if (!(lo <= mn_mhz && mn_mhz <= hi && lo <= mx_mhz && mx_mhz <= hi))
            return {false, "lock: values must be within [" + std::to_string(lo) + ".." + std::to_string(hi) + "] MHz"};
    }
    // The driver accepts any in-range value, reports success at it, and then
    // runs the next ENUMERATED clock UP. Snap DOWN to a member of the table.
    std::vector<ClockRow> rows = lockable_clocks_by_mem();
    const std::vector<std::uint32_t> table = rows.empty() ? std::vector<std::uint32_t>{} : rows.back().graphics;
    std::int64_t snapped[2];
    const std::int64_t wanted[2] = {mn_mhz, mx_mhz};
    for (int k = 0; k < 2; ++k) {
        std::optional<std::int64_t> below;
        for (std::uint32_t c : table)
            if (c <= wanted[k] && (!below || c > *below)) below = c;
        snapped[k] = below ? *below : wanted[k];
    }
    std::string note;
    if (!table.empty() && (snapped[0] != mn_mhz || snapped[1] != mx_mhz))
        note = " (snapped down from [" + std::to_string(mn_mhz) + ".." + std::to_string(mx_mhz) +
               "]: the driver would have rounded UP to a clock you did not ask for)";
    const int st = nv.get<LockFn>("nvmlDeviceSetGpuLockedClocks")(nv.dev, static_cast<std::uint32_t>(snapped[0]),
                                                                  static_cast<std::uint32_t>(snapped[1]));
    if (st == 0) {
        last_lock_ = {static_cast<std::uint32_t>(snapped[0]), static_cast<std::uint32_t>(snapped[1])};
        return {true, "GPU clock locked to [" + std::to_string(snapped[0]) + ".." + std::to_string(snapped[1]) + "] MHz" + note};
    }
    return {false, "lock failed: " + nv.errstr(st) + " (needs admin)"};
}

Outcome Clocks::reset_gpu_clocks(bool allow_pascal_noop) {
    Nvml& nv = gpu_.nvml;
    if (!nv.ok || !nv.has("nvmlDeviceResetGpuLockedClocks")) return {false, "ResetGpuLockedClocks not available"};
    const int st = nv.get<HandleFn>("nvmlDeviceResetGpuLockedClocks")(nv.dev);
    if (st == 0) {
        last_lock_.reset();
        return {true, "GPU clock lock released"};
    }
    // NVML documents this pair for Volta or newer: a stock reset on a
    // positively identified Pascal card has no such lock to release.
    auto arch = telemetry_.arch();
    if (st == 3 && allow_pascal_noop && arch && *arch == kArchPascal) {
        last_lock_.reset();
        return {true, "NVML frequency lock is not applicable to Pascal; nothing to reset"};
    }
    return {false, "reset failed: " + nv.errstr(st)};
}

}  // namespace druta::backend
