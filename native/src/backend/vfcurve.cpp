// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/vfcurve.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <set>

#include "backend/nvapi.h"

namespace druta::backend {
namespace {

using HandlePtrFn = int(DRUTA_CDECL*)(void*, void*);
constexpr std::uint32_t VfpCurveGet = 0x21537AD4;
constexpr std::uint32_t BoostTableGet = 0x23F1B133;
constexpr std::uint32_t BoostTableSet = 0x0733E009;
constexpr std::uint32_t kArchKepler = 2;
constexpr std::uint32_t kArchPascal = 4;
constexpr std::uint32_t kArchTuring = 6;

std::int64_t floor_div(std::int64_t a, std::int64_t b) {  // Python's //
    std::int64_t q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) --q;
    return q;
}

std::string format_0f(double value) {  // Python f"{value:.0f}"
    char text[64];
    std::snprintf(text, sizeof text, "%.0f", value);
    return text;
}

}  // namespace

void set_point_masks(std::uint32_t masks[4], std::int64_t nbits) {
    nbits = std::max<std::int64_t>(0, std::min<std::int64_t>(nbits, kVfpPoints));
    for (std::int64_t i = 0; i < 4; ++i) {
        const std::int64_t lo = i * 32;
        if (nbits >= lo + 32)
            masks[i] = 0xFFFFFFFFu;
        else if (nbits > lo)
            masks[i] = (1u << (nbits - lo)) - 1u;
        else
            masks[i] = 0;
    }
}

bool VfCurve::vf_curve_applicable() {
    auto arch = telemetry_.arch();
    return !(arch && *arch == kArchKepler);
}

std::string VfCurve::curve_error(const VfpCurve& cv, std::uint32_t n) {
    // A driver can accept every mask bit and return only a placeholder row:
    // all requested rows must carry voltage and frequency before the
    // boundary or the frequency scale can be inferred.
    std::uint32_t valid = 0;
    for (std::uint32_t i = 0; i < std::min(n, kVfpPoints); ++i)
        if (cv.entries[i].volt_uV > 0 && cv.entries[i].freq_kHz >= 1000) ++valid;
    if (n < 2 || valid != n)
        return "driver returned an incomplete V/F curve (" + std::to_string(valid) + " valid rows of " +
               std::to_string(n) + "); press Read curve to retry";
    return "";
}

std::uint32_t VfCurve::probe_entry_count() {
    // Acceptance is monotonic, so a binary search finds the width in about
    // seven calls instead of walking 128. All reads.
    Nvapi& a = gpu_.nvapi;
    auto fn = a.get<HandlePtrFn>(VfpCurveGet);
    auto accepted = [&](std::uint32_t n) {
        VfpCurve cv{};
        cv.version = nvapi_ver<VfpCurve>(1);
        set_point_masks(cv.masks, n);
        return fn(a.gpu, &cv) == 0;
    };
    if (accepted(kVfpPoints)) return kVfpPoints;
    std::uint32_t lo = 0, hi = kVfpPoints;  // accepted(lo), not accepted(hi)
    while (hi - lo > 1) {
        const std::uint32_t mid = (lo + hi) / 2;
        if (accepted(mid))
            lo = mid;
        else
            hi = mid;
    }
    return lo;
}

std::optional<VfpLayout> VfCurve::layout(bool force) {
    if (!vf_curve_applicable()) return std::nullopt;
    if (layout_cache_ && !force) return layout_cache_;
    layout_cache_.reset();
    layout_error_.clear();
    Nvapi& a = gpu_.nvapi;
    auto fn = a.ok ? a.get<HandlePtrFn>(VfpCurveGet) : nullptr;
    if (!fn) return std::nullopt;
    const std::uint32_t n = probe_entry_count();
    if (!n) return std::nullopt;
    VfpCurve cv{};
    cv.version = nvapi_ver<VfpCurve>(1);
    set_point_masks(cv.masks, n);
    if (fn(a.gpu, &cv) != 0) return std::nullopt;
    layout_error_ = curve_error(cv, n);
    if (!layout_error_.empty()) return std::nullopt;

    struct Row {
        std::uint32_t idx;
        double mv;
        std::uint32_t khz;
    };
    std::vector<Row> rows;
    for (std::uint32_t i = 0; i < n; ++i)
        if (cv.entries[i].freq_kHz) rows.push_back({i, cv.entries[i].volt_uV / 1000.0, cv.entries[i].freq_kHz});
    // The GPU block is the leading run of non-decreasing voltage; the first
    // collapse (GP102: 1243.75 mV then 550.00) is the boundary.
    VfpLayout lay;
    lay.n_entries = n;
    std::optional<double> prev;
    bool broken = false;
    for (const Row& row : rows) {
        if (!broken && (!prev || row.mv >= *prev - 0.01)) {
            lay.gpu_idx.push_back(row.idx);
            prev = row.mv;
        } else {
            broken = true;
            lay.other_idx.push_back(row.idx);
        }
    }
    if (lay.gpu_idx.size() < 2) {
        layout_error_ = "driver returned fewer than two GPU V/F points; press Read curve to retry";
        return std::nullopt;
    }
    std::set<std::uint32_t> gset(lay.gpu_idx.begin(), lay.gpu_idx.end());
    std::uint32_t top_khz = 0;
    for (const Row& row : rows)
        if (gset.count(row.idx)) top_khz = std::max(top_khz, row.khz);
    const double top_raw = top_khz / 1000.0;
    const std::optional<std::uint32_t> gfx_max = caps_.gfx_max;
    auto arch = telemetry_.arch();
    if (arch && *arch == kArchPascal)
        lay.freq_div = 2;
    else if (arch && *arch == kArchTuring)
        lay.freq_div = 1;
    else
        lay.freq_div = (gfx_max && *gfx_max && top_raw > *gfx_max * 1.5) ? 2 : 1;

    std::set<std::uint32_t> mem(caps_.mem_clocks.begin(), caps_.mem_clocks.end());
    std::set<std::uint32_t> oset(lay.other_idx.begin(), lay.other_idx.end());
    std::size_t n_mem = 0;
    for (const Row& row : rows) {
        if (!oset.count(row.idx)) continue;
        const double rounded = std::nearbyint(row.khz / 1000.0);  // Python round(): ties to even
        if (rounded >= 0 && mem.count(static_cast<std::uint32_t>(rounded))) ++n_mem;
    }
    lay.notes = std::to_string(n) + " entries; " + std::to_string(lay.gpu_idx.size()) + " GPU points";
    if (!lay.other_idx.empty())
        lay.notes += "; " + std::to_string(lay.other_idx.size()) + " trailing rows (" + std::to_string(n_mem) +
                     " match mem_clocks)";
    lay.notes += std::string("; GPU frequency is ") + (lay.freq_div == 2 ? "GPC2CLK (halved)" : "direct");
    if (gfx_max && *gfx_max) lay.notes += "; top raw " + format_0f(top_raw) + " vs gfx_max " + std::to_string(*gfx_max);
    layout_cache_ = lay;
    return layout_cache_;
}

CurveResult VfCurve::read_vf_curve() {
    if (!vf_curve_applicable()) return {std::nullopt, "V/F curves are not applicable on this GPU"};
    Nvapi& a = gpu_.nvapi;
    auto curve_fn = a.ok ? a.get<HandlePtrFn>(VfpCurveGet) : nullptr;
    auto table_fn = a.ok ? a.get<HandlePtrFn>(BoostTableGet) : nullptr;
    if (!curve_fn || !table_fn) return {std::nullopt, "VF curve APIs unavailable"};
    auto lay = layout();
    if (!lay) return {std::nullopt, !layout_error_.empty() ? layout_error_ : "could not determine this card's VF table layout"};
    VfpCurve cv{};
    cv.version = nvapi_ver<VfpCurve>(1);
    set_point_masks(cv.masks, lay->n_entries);
    int st = curve_fn(a.gpu, &cv);
    if (st != 0) {
        layout_cache_.reset();
        return {std::nullopt, "curve read failed (status " + std::to_string(st) + ")"};
    }
    std::string err = curve_error(cv, lay->n_entries);
    if (!err.empty()) {
        layout_cache_.reset();
        layout_error_ = err;
        return {std::nullopt, err};
    }
    BoostTable bt{};
    bt.version = nvapi_ver<BoostTable>(1);
    set_point_masks(bt.masks, lay->n_entries);
    st = table_fn(a.gpu, &bt);
    if (st != 0) return {std::nullopt, "delta-table read failed (status " + std::to_string(st) + ")"};
    // GPU rows only: the trailing rows on Pascal are the MEMORY points.
    std::vector<VfPoint> points;
    for (std::uint32_t i : lay->gpu_idx) {
        const VfpEntry& e = cv.entries[i];
        if (e.freq_kHz == 0) continue;
        points.push_back({i, e.volt_uV / 1000.0, e.freq_kHz / (1000.0 * lay->freq_div), bt.rows[i].w[5]});
    }
    if (points.empty()) return {std::nullopt, "curve read returned no points"};
    return {points, ""};
}

std::vector<VfOtherRow> VfCurve::read_vfp_other_rows() {
    // Real data, but not points the curve editor may touch.
    Nvapi& a = gpu_.nvapi;
    auto lay = layout();
    auto fn = a.ok ? a.get<HandlePtrFn>(VfpCurveGet) : nullptr;
    if (!lay || lay->other_idx.empty() || !fn) return {};
    VfpCurve cv{};
    cv.version = nvapi_ver<VfpCurve>(1);
    set_point_masks(cv.masks, lay->n_entries);
    if (fn(a.gpu, &cv) != 0) return {};
    std::set<std::uint32_t> mem(caps_.mem_clocks.begin(), caps_.mem_clocks.end());
    std::vector<VfOtherRow> out;
    for (std::uint32_t i : lay->other_idx) {
        const VfpEntry& e = cv.entries[i];
        const double raw = e.freq_kHz / 1000.0;
        const double rounded = std::nearbyint(raw);
        out.push_back({i, e.volt_uV / 1000.0, raw, rounded >= 0 && mem.count(static_cast<std::uint32_t>(rounded)) > 0});
    }
    return out;
}

Outcome VfCurve::apply_vf_deltas(const std::vector<std::pair<std::int64_t, std::int64_t>>& deltas) {
    // Read-modify-write the whole delta table; only differing rows are touched.
    if (!vf_curve_applicable()) return {false, "V/F curves are not applicable on this GPU"};
    Nvapi& a = gpu_.nvapi;
    auto get = a.ok ? a.get<HandlePtrFn>(BoostTableGet) : nullptr;
    auto set = a.ok ? a.get<HandlePtrFn>(BoostTableSet) : nullptr;
    if (!get || !set) return {false, "boost-table APIs unavailable"};
    auto lay = layout();
    if (!lay) return {false, "could not determine this card's VF table layout"};
    std::set<std::uint32_t> writable(lay->gpu_idx.begin(), lay->gpu_idx.end());
    BoostTable bt{};
    bt.version = nvapi_ver<BoostTable>(1);
    set_point_masks(bt.masks, lay->n_entries);
    int st = get(a.gpu, &bt);
    if (st != 0) return {false, "pre-write table read failed (status " + std::to_string(st) + ")"};
    std::uint32_t nchg = 0;
    for (const auto& [idx, delta] : deltas) {
        // GPU rows only: a delta written to a memory V/F row is not a core overclock at all.
        if (idx < 0 || idx > 0xFFFFFFFF || !writable.count(static_cast<std::uint32_t>(idx)))
            return {false, "point index " + std::to_string(idx) + " is not a GPU V/F point on this card (" +
                               std::to_string(lay->gpu_idx.size()) + " GPU points of " + std::to_string(lay->n_entries) +
                               " entries)"};
        if (delta > kMaxAbsDeltaKhz || delta < -kMaxAbsDeltaKhz)
            return {false, "refusing point " + std::to_string(idx) + ": delta " + std::to_string(floor_div(delta, 1000)) +
                               " MHz More than a whole gigahertz of delta? Lol, bro thinks he's Seby. Caught you with a "
                               "mouse-slip and saved you a total driver crash this time. Don't do it next time. :copege:"};
        BoostRow& row = bt.rows[static_cast<std::uint32_t>(idx)];
        if (row.w[5] != static_cast<std::int32_t>(delta)) {
            row.w[5] = static_cast<std::int32_t>(delta);
            ++nchg;
        }
    }
    if (nchg == 0) return {true, "no delta changes to apply"};
    bt.version = nvapi_ver<BoostTable>(1);
    set_point_masks(bt.masks, lay->n_entries);
    st = set(a.gpu, &bt);
    if (st == 0) return {true, "VF delta table written (" + std::to_string(nchg) + " points changed)"};
    return {false, "VF table write failed (status " + std::to_string(st) + ")"};
}

Outcome VfCurve::reset_vf_curve() {
    // Zero every GPU point's delta: the factory curve, with no persisted baseline to poison.
    auto lay = layout();
    if (!lay) return {false, "could not determine this card's VF table layout"};
    std::vector<std::pair<std::int64_t, std::int64_t>> zeros;
    for (std::uint32_t i : lay->gpu_idx) zeros.emplace_back(i, 0);
    return apply_vf_deltas(zeros);
}

std::optional<DeltaSnapshot> VfCurve::vf_delta_snapshot() {
    // A miss is not a failure: a card without a readable curve still has clock offsets.
    if (!vf_curve_applicable()) return std::nullopt;
    CurveResult curve = read_vf_curve();
    if (!curve.error.empty() || !curve.points || curve.points->empty()) return std::nullopt;
    auto lay = layout();
    if (!lay || lay->freq_div == 0) return std::nullopt;
    DeltaSnapshot snapshot;
    snapshot.freq_div = lay->freq_div;
    for (const VfPoint& p : *curve.points) snapshot.deltas.emplace_back(p.idx, p.delta_khz);
    return snapshot;
}

}  // namespace druta::backend
