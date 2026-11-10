// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of the private clock-domain rows (drutacore/clkdom.h).
#include "drutacore/clkdom.h"

#include <cstring>

#include "backend/clkdom_names.h"
#include "core/abi_internal.h"
#include "core/mutex.h"

namespace {

using druta::backend::AllClocksPriv;
using druta::backend::PrivDomainRow;

AllClocksPriv payload_from(const uint32_t* words) {
    AllClocksPriv pc{};
    std::memcpy(pc.w, words, sizeof(pc.w));
    return pc;
}

void put_rows(const std::vector<PrivDomainRow>& rows, dc_clock_domains* out) {
    const uint32_t size = out->size;
    std::memset(out, 0, sizeof(*out));
    out->size = size;
    // At most one row per domain, so the 32 slots always suffice.
    const std::size_t count = std::min<std::size_t>(rows.size(), DC_PRIV_DOMAINS);
    for (std::size_t k = 0; k < count; ++k) {
        const PrivDomainRow& r = rows[k];
        dc_clock_domain_row& c = out->rows[k];
        c.domain = r.domain;
        DC_COPY(c.name, r.name);
        DC_COPY(c.grade, r.grade);
        DC_COPY(c.kind, r.kind);
        c.prog_khz = r.prog_khz;
        c.meas_khz = r.meas_khz;
        c.flags = r.flags;
        c.srcid = r.srcid;
        c.scale = r.scale;
        c.has_prog_mhz = r.prog_mhz ? 1 : 0;
        c.has_meas_mhz = r.meas_mhz ? 1 : 0;
        c.has_delta_mhz = r.delta_mhz ? 1 : 0;
        c.prog_mhz = r.prog_mhz.value_or(0.0);
        c.meas_mhz = r.meas_mhz.value_or(0.0);
        c.delta_mhz = r.delta_mhz.value_or(0.0);
    }
    out->count = static_cast<uint32_t>(count);
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_gpu_msvdd_domains_get(dc_gpu* handle, uint32_t which, uint32_t* out,
                                                  size_t capacity, size_t* count) {
    if (!handle || which > 1 || !count || (!out && capacity)) return DC_E_ARGUMENT;
    try {
        druta::LockGuard guard(handle->gpu.lock());
        const auto& domains = which == 0 ? handle->clkdom.msvdd_domains_written()
                                        : handle->clkdom.msvdd_domains_seen();
        *count = domains.size();
        if (domains.size() > capacity) return DC_E_BUFFER;
        if (!domains.empty()) std::copy(domains.begin(), domains.end(), out);
        return DC_OK;
    } catch (...) {
        return DC_E_FAILED;
    }
}

DC_API dc_status DC_CALL dc_gpu_clock_domains(dc_gpu* handle, const uint32_t* words, int32_t has_core,
                                              uint32_t core_mhz, int32_t has_mem, uint32_t mem_mhz,
                                              dc_clock_domains* out) {
    if (!handle || !out || out->size != sizeof(dc_clock_domains)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    std::optional<AllClocksPriv> pc;
    if (words)
        pc = payload_from(words);
    else
        pc = handle->telemetry.priv_clocks();
    if (!pc) return DC_E_UNAVAILABLE;  // Python's (None, PRIV_UNAVAIL)
    // Python asks clkdom_is_blackwell() and then arch(): two reads, because an
    // unrecognised or failed answer is not cached and is asked for again.
    const std::optional<uint32_t> first = handle->telemetry.arch();
    const bool blackwell = first && *first == 10;
    const std::optional<uint32_t> architecture = handle->telemetry.arch();
    put_rows(druta::backend::read_clock_domains(*pc, druta::abi::optional_u32(has_core, core_mhz),
                                                druta::abi::optional_u32(has_mem, mem_mhz), blackwell, architecture),
             out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_clock_domains_classify(const uint32_t* words, size_t word_count, int32_t has_core,
                                                   uint32_t core_mhz, int32_t has_mem, uint32_t mem_mhz,
                                                   int32_t has_arch, uint32_t architecture,
                                                   dc_clock_domains* out) {
    if (!words || word_count != DC_PRIV_CLK_WORDS || !out || out->size != sizeof(dc_clock_domains))
        return DC_E_ARGUMENT;
    const AllClocksPriv pc = payload_from(words);
    const std::optional<uint32_t> arch = druta::abi::optional_u32(has_arch, architecture);
    put_rows(druta::backend::read_clock_domains(pc, druta::abi::optional_u32(has_core, core_mhz),
                                                druta::abi::optional_u32(has_mem, mem_mhz), arch && *arch == 10, arch),
             out);
    return DC_OK;
}

}  // extern "C"
