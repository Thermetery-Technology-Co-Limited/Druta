// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Port of nvbackend.read_clock_domains (the row loop) and
// nvbackend.classify_domain_names. The evidence behind every table lives in
// the Python comments beside the originals; the tables here are verbatim
// copies and carry the scope of that evidence with them: each was earned on
// the boards named there, and a name outside its table stays a bare index.
#include "backend/clkdom_names.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <utility>

namespace druta::backend {
namespace {

struct DomainId {
    std::uint32_t domain;
    const char* name;
    const char* grade;
    const char* kind;
};

// PRIV_DOMAIN_ID: earned on TU102 only. Domain 31 is a PCIe link generation,
// not a clock; `kind` is structural and applies on every card.
constexpr DomainId kPrivDomainId[] = {
    {0, "GPC", kGradeConfirmed, kKindFreq},      {1, "XBAR", kGradeConfirmed, kKindFreq},
    {2, "SYSCLK", kGradeLikely, kKindFreq},      {4, "MEM", kGradeConfirmed, kKindFreq},
    {5, "LTCCLK", kGradeLikely, kKindFreq},      {21, "VIDEO", kGradeConfirmed, kKindFreq},
    {31, "PCIe link gen", kGradeLikely, kKindPcieGen},
};

struct ScaledName {  // KEPLER_NAMES / MAXWELL_NAMES: (name, scale), always LIKELY
    std::uint32_t domain;
    const char* name;
    std::uint32_t scale;
};

// GK104/GK110 ROM and live-state correlations (experiments/kepler-gtx770-clock-crosscheck.md).
constexpr ScaledName kKeplerNames[] = {
    {6, "DISP", 1}, {16, "XBAR2CLK", 2}, {17, "SYS2CLK", 2}, {18, "HUB", 1},
    {20, "PWR", 1}, {21, "MSD", 1},      {25, "L2C2CLK", 2},
};

// GM107's distinct P0 ROM values (experiments/maxwell-gtx745-clock-domains.md).
constexpr ScaledName kMaxwellNames[] = {
    {6, "DISP", 1}, {16, "XBAR2CLK", 2}, {17, "SYS2CLK", 2}, {18, "HUB", 1}, {20, "PWR", 1},
};

struct GradedName {  // the (name, grade) tables
    std::uint32_t domain;
    const char* name;
    const char* grade;
};

// GP102: behaviour measured, words by analogy with TU102, so both hedged; the row reads scale 2.
constexpr GradedName kPascalNames[] = {{16, "XBAR2CLK", kGradeLikely}, {17, "SYS2CLK", kGradeLikely}};

// RTX 5080 / 580.97: its own table, so TU102's LTCCLK is never put on Blackwell's empty domain 5.
constexpr GradedName kBlackwellNames[] = {
    {0, "GPC", kGradeConfirmed}, {4, "MEM", kGradeConfirmed},  {1, "XBAR", kGradeConfirmed},
    {2, "SYSCLK", kGradeLikely}, {21, "VIDEO", kGradeConfirmed}, {31, "PCIe link gen", kGradeLikely},
};

// RTX 3070 Ti beside GPU-Z 2.71.
constexpr GradedName kAmpereNames[] = {
    {1, "Crossbar", kGradeConfirmed}, {2, "SYS", kGradeConfirmed},
    {5, "LTC", kGradeLikely},         {21, "Video", kGradeConfirmed},
};

// RTX 4080 SUPER beside GPU-Z 2.71; corroborates names, not the physical-counter indices.
constexpr GradedName kAdaNames[] = {
    {1, "Crossbar", kGradeConfirmed}, {2, "SYS", kGradeConfirmed},
    {5, "LTC", kGradeLikely},         {21, "Video", kGradeConfirmed},
};

template <typename Entry, std::size_t N>
const Entry* lookup(const Entry (&table)[N], std::uint32_t domain) {
    for (const Entry& entry : table)
        if (entry.domain == domain) return &entry;
    return nullptr;
}

bool populated_listed(std::uint32_t domain) {
    return std::find(std::begin(kPrivPopulated), std::end(kPrivPopulated), domain) != std::end(kPrivPopulated);
}

// Python: bool(b) and abs(a - b) <= max(0.5, abs(b) * tol).
bool close(double a, double b, double tol = 0.005) {
    return b != 0.0 && std::fabs(a - b) <= std::max(0.5, std::fabs(b) * tol);
}

double mhz(std::uint32_t khz) { return static_cast<double>(khz) / 1000.0; }

bool is_one_of(const std::optional<std::uint32_t>& value, std::initializer_list<std::uint32_t> set) {
    return value && std::find(set.begin(), set.end(), *value) != set.end();
}

}  // namespace

std::vector<PrivDomainRow> private_domain_rows(const AllClocksPriv& pc) {
    std::vector<PrivDomainRow> rows;
    for (std::uint32_t dom = 0; dom < kPrivNDomains; ++dom) {
        const std::uint32_t ai = kPrivABase + kPrivAStride * dom;
        const std::uint32_t bi = kPrivBBase + kPrivBStride * dom;
        const std::uint32_t prog = pc.w[ai], flags = pc.w[ai + 1];
        const std::uint32_t meas = pc.w[bi], srcid = pc.w[bi + 1];
        // An unlisted domain is reported once it carries anything, so a surprise is visible.
        if (!populated_listed(dom) && !(prog || meas || flags)) continue;
        PrivDomainRow row;
        row.domain = dom;
        const DomainId* id = lookup(kPrivDomainId, dom);
        row.kind = id ? id->kind : kKindFreq;
        row.prog_khz = prog;
        row.meas_khz = meas;
        row.flags = flags;
        row.srcid = srcid;
        if (row.kind == kKindFreq) {
            row.prog_mhz = mhz(prog);
            row.meas_mhz = mhz(meas);
            // B minus A, signed: the measured counter may sit below the target.
            if (prog && meas)
                row.delta_mhz = static_cast<double>(static_cast<std::int64_t>(meas) - static_cast<std::int64_t>(prog)) /
                                1000.0;
        }
        rows.push_back(std::move(row));
    }
    return rows;
}

void classify_domain_names(std::vector<PrivDomainRow>& rows, std::optional<std::uint32_t> core_mhz,
                           std::optional<std::uint32_t> mem_nvml, bool blackwell,
                           std::optional<std::uint32_t> architecture) {
    // Python's falsy None/0 for the public readings.
    const double core = core_mhz ? static_cast<double>(*core_mhz) : 0.0;
    const double mem = mem_nvml ? static_cast<double>(*mem_nvml) : 0.0;

    // populated: frequency rows with a non-zero programmed value, in row order.
    std::vector<const PrivDomainRow*> populated;
    for (const PrivDomainRow& r : rows)
        if (r.kind == kKindFreq && r.prog_mhz && *r.prog_mhz != 0.0) populated.push_back(&r);
    auto is_populated = [&](std::uint32_t dom) {
        return std::any_of(populated.begin(), populated.end(), [&](const PrivDomainRow* r) { return r->domain == dom; });
    };

    const bool legacy = is_one_of(architecture, {2, 3, 4});
    const bool modern = is_one_of(architecture, {6, 7, 8}) || blackwell || is_one_of(architecture, {10});
    std::optional<std::uint32_t> gpc_dom, mem_dom;
    std::uint32_t gpc_scale = 1;
    if (legacy || modern) {
        // Known families: their established primary slots, never a frequency match
        // (Kepler's idle MEM and graphics clocks both read 324 MHz).
        const std::uint32_t slot = legacy ? 15 : 0;
        gpc_scale = legacy ? 2 : 1;
        if (is_populated(slot)) gpc_dom = slot;
        if (is_populated(4)) mem_dom = 4;
    } else {
        // Unknown family: a name only for a UNIQUE correlation with the driver's own figures.
        std::vector<std::pair<std::uint32_t, std::uint32_t>> candidates;
        std::vector<std::uint32_t> memory;
        for (const PrivDomainRow* r : populated) {
            for (std::uint32_t scale : {1u, 2u})
                if (core != 0.0 && close(*r->prog_mhz, static_cast<double>(scale) * core))
                    candidates.emplace_back(r->domain, scale);
            if (close(*r->prog_mhz, mem)) memory.push_back(r->domain);
        }
        if (candidates.size() == 1) {
            gpc_dom = candidates[0].first;
            gpc_scale = candidates[0].second;
        }
        if (memory.size() == 1) mem_dom = memory[0];
        if (gpc_dom && gpc_dom == mem_dom) gpc_dom = mem_dom = std::nullopt;
    }

    const bool unknown = !architecture;
    const bool turing_like = is_one_of(architecture, {6}) || (unknown && gpc_dom == 0u);
    const bool blind = unknown && core == 0.0;
    const bool pascal_like = is_one_of(architecture, {4}) || (unknown && gpc_dom == 15u);
    const std::uint32_t arch = architecture.value_or(0);

    for (PrivDomainRow& r : rows) {
        const std::uint32_t dom = r.domain;
        r.scale = 1;
        if (r.kind == kKindFreq && !(r.prog_khz || r.meas_khz)) {
            r.name = "";
            r.grade = kGradeUnpopulated;
            continue;
        }
        const ScaledName* scaled = nullptr;
        const GradedName* graded = nullptr;
        const DomainId* id = nullptr;
        if (gpc_dom == dom) {
            r.name = gpc_scale == 2 ? "GPC2CLK" : "GPC";
            r.grade = kGradeConfirmed;
            r.scale = gpc_scale;
        } else if (mem_dom == dom) {
            r.name = "MEM";
            r.grade = kGradeConfirmed;
        } else if (!unknown && arch == 2 && (scaled = lookup(kKeplerNames, dom))) {
            r.name = scaled->name;
            r.scale = scaled->scale;
            r.grade = kGradeLikely;
        } else if (!unknown && arch == 3 && (scaled = lookup(kMaxwellNames, dom))) {
            r.name = scaled->name;
            r.scale = scaled->scale;
            r.grade = kGradeLikely;
        } else if (pascal_like && (graded = lookup(kPascalNames, dom))) {
            r.name = graded->name;
            r.grade = graded->grade;
            r.scale = 2;
        } else if (!unknown && arch == 7 && (graded = lookup(kAmpereNames, dom))) {
            r.name = graded->name;
            r.grade = graded->grade;
        } else if (!unknown && arch == 8 && (graded = lookup(kAdaNames, dom))) {
            r.name = graded->name;
            r.grade = graded->grade;
        } else if (blackwell) {
            graded = lookup(kBlackwellNames, dom);
            r.name = graded ? graded->name : "";
            r.grade = graded ? graded->grade : kGradeUnnamed;
        } else if ((id = lookup(kPrivDomainId, dom)) && (turing_like || blind || r.kind == kKindPcieGen)) {
            r.name = id->name;
            // Without an architecture or a core reading nothing was checked: hedge every name.
            r.grade = (blind && std::string(id->grade) == kGradeConfirmed) ? kGradeLikely : id->grade;
        } else {
            r.name = "";
            r.grade = kGradeUnnamed;
        }
    }
}

std::vector<PrivDomainRow> read_clock_domains(const AllClocksPriv& pc, std::optional<std::uint32_t> core_mhz,
                                              std::optional<std::uint32_t> mem_nvml, bool blackwell,
                                              std::optional<std::uint32_t> architecture) {
    std::vector<PrivDomainRow> rows = private_domain_rows(pc);
    classify_domain_names(rows, core_mhz, mem_nvml, blackwell, architecture);
    return rows;
}

}  // namespace druta::backend
