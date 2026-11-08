// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The private clock getter's per-domain rows and the naming that is EARNED
// against the card in front of us. Port of nvbackend.read_clock_domains
// (the row loop) and nvbackend.classify_domain_names.
//
// A name is never applied by domain number alone: the frequencies are
// measured either way, the grade says how far the word next to them may be
// trusted. Pure: nothing here touches a driver.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "backend/structs.h"

namespace druta::backend {

// nvbackend.PRIV_*: the partition of the 288 payload words into array A
// (2 words per domain: programmed kHz, flags) and array B (7 words per
// domain: measured kHz, source id, ...).
inline constexpr std::uint32_t kPrivNDomains = 32;
inline constexpr std::uint32_t kPrivABase = 0, kPrivAStride = 2;
inline constexpr std::uint32_t kPrivBBase = 64, kPrivBStride = 7;
static_assert(kPrivBBase == kPrivNDomains * kPrivAStride);
static_assert(kPrivBBase + kPrivNDomains * kPrivBStride == kPrivClkDwords);
// The domains that carry anything on the measured cards; a domain outside
// this set is still reported when it is non-zero.
inline constexpr std::uint32_t kPrivPopulated[] = {0, 1, 2, 3, 4, 5, 6, 20, 21, 22, 31};
inline constexpr const char* kPrivUnavail = "private NvAPI_GPU_GetAllClocks (0x1BD69F49) did not answer";

// Grades (nvbackend.PRIV_CONFIRMED .. PRIV_UNPOPULATED) and kinds (PRIV_FREQ, PRIV_PCIE_GEN).
inline constexpr const char* kGradeConfirmed = "confirmed";
inline constexpr const char* kGradeLikely = "likely";
inline constexpr const char* kGradeUnnamed = "unnamed";
inline constexpr const char* kGradeUnpopulated = "unpopulated";
inline constexpr const char* kKindFreq = "freq";
inline constexpr const char* kKindPcieGen = "pcie_gen";

struct PrivDomainRow {
    std::uint32_t domain = 0;
    std::string name;                 // "" when no name has been earned
    std::string grade = kGradeUnnamed;
    std::string kind = kKindFreq;
    std::uint32_t prog_khz = 0;       // array A: the programmed target
    std::uint32_t meas_khz = 0;       // array B: the measured counter
    std::uint32_t flags = 0;          // array A's second word
    std::uint32_t srcid = 0;          // array B's second word
    std::uint32_t scale = 1;          // 2 for the doubled (GPC2CLK-style) domains
    std::optional<double> prog_mhz, meas_mhz, delta_mhz;  // unset when the row is not a frequency
};

// The row loop of read_clock_domains: every populated domain, unnamed.
std::vector<PrivDomainRow> private_domain_rows(const AllClocksPriv& pc);

// classify_domain_names(rows, core_mhz, mem_nvml, blackwell, architecture):
// fills name, grade and scale in place. core_mhz and mem_nvml are the PUBLIC
// readings (nullopt or 0 behave as Python's None/0: no correlation).
void classify_domain_names(std::vector<PrivDomainRow>& rows, std::optional<std::uint32_t> core_mhz,
                           std::optional<std::uint32_t> mem_nvml, bool blackwell,
                           std::optional<std::uint32_t> architecture);

// Both steps: rows of `pc`, named.
std::vector<PrivDomainRow> read_clock_domains(const AllClocksPriv& pc, std::optional<std::uint32_t> core_mhz,
                                              std::optional<std::uint32_t> mem_nvml, bool blackwell,
                                              std::optional<std::uint32_t> architecture);

}  // namespace druta::backend
