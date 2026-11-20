// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The per-domain clock-offset control block (private NvAPI ids 0xF58938F5
// GET / 0xD14B69CF SET): discovery of the accepted control indices, layout
// validation, the request readers, the frequency and rail-offset writers
// and the bounded retry. Port of nvbackend.GPU.clkdom_* and of
// read_clk_domain_offsets, set_clk_domain_offset, rail_offset_capability,
// read_rail_offset_mv, set_rail_offset_mv, reset_msvdd_offset_mv,
// retry_clock_capabilities and refresh_capabilities.
//
// The block is a byte buffer plus offsets, never a struct: four fields of a
// 772-byte entry are known, and every write hands the driver its own bytes
// back with exactly one dword changed (a diff guard refuses otherwise).
// Layouts are selected by NVML architecture and then VALIDATED by a
// one-domain GET that echoes the version and the mask; nothing here decides
// from a driver version or a device id.
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "backend/clkdom_names.h"
#include "backend/gpu.h"
#include "backend/outcome.h"
#include "backend/telemetry.h"

namespace druta::backend {

// nvbackend.ClkDomLayout: the small part of the layout that is used.
struct ClkDomLayout {
    const char* name;
    std::uint32_t version;
    std::uint32_t size;
    std::uint32_t header;
    std::uint32_t stride;
    std::uint32_t mask_dword;
    std::uint32_t mode;
    std::uint32_t freq_khz;
    std::optional<std::uint32_t> nvvdd_uv;
    std::optional<std::uint32_t> msvdd_uv;
};

// Measured on Turing (header, stride and the field offsets self-check:
// 0x124 + 32 * 0x304 == 24996, the size the version word declares).
inline constexpr ClkDomLayout kClkDomLayoutTuring{"turing", 0x000261A4, 0x61A4, 0x124, 0x304, 2,
                                                  0x000, 0x10C, 0x110, 0x114};
// Blackwell keeps the geometry and shifts the frequency and rail fields by one dword.
inline constexpr ClkDomLayout kClkDomLayoutBlackwell{"blackwell", 0x000261A4, 0x61A4, 0x124, 0x304, 2,
                                                     0x000, 0x114, 0x118, 0x11C};
inline constexpr std::uint32_t kClkDomSlots = 32;
inline constexpr std::uint32_t kClkDomXbar = 1;
inline constexpr std::uint32_t kClkDomBufBytes = 65536;  // far larger than the 24996 declared

// One request record of read_clk_domain_offsets.
struct ClkDomRequest {
    std::int32_t mode = 0;
    std::int32_t freq_khz = 0;             // the logical (UI) sign
    std::optional<std::int32_t> msvdd_uv;  // unset when the layout has no MSVDD field
};

// rail_offset_capability's record.
struct RailOffsetCapability {
    bool available = false;
    std::optional<std::int64_t> domain;  // the control that answered, or the one asked for
    std::optional<double> value_mv;
    bool experimental = false;
    std::string reason;
};

// clkdom_layout's failure record: (kind, text), or unset.
struct CapabilityError {
    std::string kind;  // "unavailable" | "unsupported_layout" | "read_failure" | "unsupported_response"
    std::string text;
};

// The driver's own bytes of one GET, as 32-bit words.
struct ClkDomBlock {
    std::vector<std::uint32_t> words;  // kClkDomBufBytes / 4 words
    std::int32_t word(std::uint32_t byte_offset) const;
    std::uint32_t uword(std::uint32_t byte_offset) const { return words[byte_offset / 4]; }
    void set_word(std::uint32_t byte_offset, std::int32_t value);
};

class ClkDom {
public:
    ClkDom(Gpu& gpu, Telemetry& telemetry) : gpu_(gpu), telemetry_(telemetry) {}

    // Flags the UI sets (GPU.msvdd_write_enabled / voltage_xoc_enabled).
    bool msvdd_write_enabled = false;
    bool voltage_xoc_enabled = false;
    static constexpr double kRailOffsetMaxMv = 200.0;
    static constexpr double kRailOffsetXocMaxMv = 500.0;

    bool ok() const;                                                   // clkdom_ok
    std::pair<int, ClkDomBlock> get(std::uint32_t mask);               // _clkdom_get
    static bool valid_block(const ClkDomBlock& block, const ClkDomLayout& layout, std::uint32_t mask);
    const std::vector<std::uint32_t>& domains();                       // clkdom_domains (cached discovery)
    bool is_blackwell();                                               // clkdom_is_blackwell
    const ClkDomLayout* candidate(std::optional<std::uint32_t> architecture) const;  // _clkdom_candidate
    const ClkDomLayout* layout();                                      // clkdom_layout (validated, cached)
    const std::map<std::uint32_t, std::string>& understood(std::optional<std::uint32_t> architecture);  // _clkdom_understood
    std::vector<std::uint32_t> controls_for_ui();                      // clkdom_controls_for_ui
    std::int32_t control_polarity(std::int64_t control);               // clkdom_control_polarity
    std::string control_label(std::int64_t control);                   // clkdom_control_label
    std::optional<std::uint32_t> step_mhz();                           // clkdom_step_mhz
    std::optional<bool> delta_inert(std::optional<std::int64_t> domain);  // clkdom_delta_inert (tri-state)
    bool offset_slider_offered(std::int64_t control);                  // clkdom_offset_slider_offered
    std::optional<bool> delta_clears_ceiling(std::int64_t domain);     // clkdom_delta_clears_ceiling
    // clkdom_pairing: {control index: private domain}, or {} when unknown. rows == nullptr reads a snapshot.
    std::map<std::uint32_t, std::uint32_t> pairing(const std::vector<PrivDomainRow>* rows);
    // clkdom_measurements (Blackwell only), in Python's dict order: gpc, xbar, sys, memory.
    std::vector<std::pair<std::string, std::optional<std::uint32_t>>> measurements();
    std::optional<std::uint32_t> clk_measure_freq(std::uint32_t mask);   // _clk_measure_freq

    // read_clk_domain_offsets: (requests by domain, error); error set means no requests.
    std::pair<std::optional<std::map<std::uint32_t, ClkDomRequest>>, std::string> read_requests();
    std::optional<double> read_xbar_offset_mhz();
    Outcome set_clk_domain_offset(std::int64_t domain, double mhz);

    RailOffsetCapability rail_offset_capability(std::int64_t rail, std::optional<std::int64_t> domain);
    std::optional<double> read_rail_offset_mv(std::int64_t domain, std::int64_t rail);
    Outcome set_rail_offset_mv(double mv, std::int64_t domain, std::int64_t rail);
    Outcome reset_msvdd_offset_mv(std::int64_t domain);

    // retry_clock_capabilities(now): at most two delayed read-only retries; true when something recovered.
    bool retry_clock_capabilities(double now);
    // refresh_capabilities: releases every discovery cache; returns the previous capability error.
    std::optional<CapabilityError> refresh_capabilities();

    const std::optional<CapabilityError>& capability_error() const { return capability_error_; }
    const std::map<std::uint32_t, std::string>& probe_errors() const { return probe_errors_; }
    bool offset_read_pending() const { return offset_read_pending_; }
    const std::set<std::uint32_t>& msvdd_domains_seen() const { return msvdd_seen_; }
    const std::set<std::uint32_t>& msvdd_domains_written() const { return msvdd_written_; }

private:
    Outcome set_msvdd_offset_mv(double mv, std::int64_t domain, bool reset);
    std::set<std::uint32_t> cached_ui_controls(std::optional<std::uint32_t> architecture);
    bool retry_needed(std::optional<std::uint32_t> architecture);

    Gpu& gpu_;
    Telemetry& telemetry_;
    // Python: _clkdom_valid (None | list), _clkdom_probe_errors, _clkdom_layout_cache (None | False | layout),
    // _clkdom_capability_error, _clkdom_pair, _clkdom_offset_read_pending, _clkdom_retry_attempts/_due.
    std::optional<std::vector<std::uint32_t>> valid_;
    std::map<std::uint32_t, std::string> probe_errors_;
    enum class LayoutCache { none, failed, valid } layout_cache_ = LayoutCache::none;
    const ClkDomLayout* layout_cached_ = nullptr;
    std::optional<CapabilityError> capability_error_;
    std::optional<std::map<std::uint32_t, std::uint32_t>> pair_;
    bool offset_read_pending_ = false;
    unsigned retry_attempts_ = 0;
    std::optional<double> retry_due_;
    std::set<std::uint32_t> msvdd_seen_, msvdd_written_;
};

}  // namespace druta::backend
