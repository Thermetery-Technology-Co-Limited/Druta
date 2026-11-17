// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "timings_service.h"
#include <optional>
#include <string>
#include <vector>
namespace druta::ui {
// Optional evidence must originate from the same target and capture as snapshot.
// No inferred field is promoted to cycles or time merely by its name.
struct TimingPartitionValue { std::string scope; std::optional<uint32_t> value; bool differs=false; };
struct TimingDiagnosticRow {
    std::string field, reg, unit="raw", provenance;
    std::optional<uint32_t> value;
    std::optional<double> nanoseconds;
    std::vector<TimingPartitionValue> partitions;
};
struct TimingDiagnostics { std::vector<TimingDiagnosticRow> rows; std::string clock_status, partition_status; };
// released_ns_rule: released 1.9.1 decoding for Classic. A field in an
// INFERRED register still converts (Field.ns_ok; Classic flags it on the
// name), and a register absent from `dump --raw` decodes from the agreeing
// `nvtune save` words, as 1.9.1 decodes the save. Writes are unaffected.
TimingDiagnostics timing_diagnostics(const TimingSnapshot&,const TimingDiagnosticEvidence& = {},bool released_ns_rule=false);
// Released 1.9.1 _split_siblings: the fields holding the rest of one value
// spread over several bit ranges (REFRESH + REFRESH_LO), from the parsed names.
std::vector<std::string> timing_split_siblings(const std::string& field,const std::vector<TimingField>& fields);
// Released 1.9.1 Reading.ns_refusal, in its order: why a decoded row has no
// nanosecond value. row is that field's timing_diagnostics(..., true) row.
std::string timing_ns_refusal(const TimingSnapshot&,const TimingField&,const TimingDiagnosticRow& row);
// Released 1.9.1 Timings header box: identity, the clock the decode is
// counted against, warnings about that clock, and the raw broadcast words.
struct TimingHeader { std::string ident, clock, words; bool clock_trustworthy=false; std::vector<std::string> warnings; };
TimingHeader timing_header(const TimingSnapshot&);
// Released 1.9.1 Snapshot.state_tag: "P0", "P2 band", "idle", or "?" when
// the clock, state, offset or enumerated clock list is unknown.
std::string timing_state_tag(const TimingSnapshot&);
// Released 1.9.1 Snapshot.perf_band: true / false / unknown (nullopt).
std::optional<bool> timing_perf_band(const TimingSnapshot&);
// Released 1.9.1 Snapshot.state_headline for a capture outside the top band
// (the tim_state line); empty for an in-band capture.
std::string timing_state_headline(const TimingSnapshot&);
// One capture filed under its memory clock for the cross-capture comparison.
struct TimingCaptureField { std::string name, reg, bits; bool structural=false; std::optional<uint32_t> cycles; };
struct TimingCapture {
    uint32_t memory_mhz=0; std::optional<double> true_mhz; std::optional<uint32_t> pstate;
    bool top_band=false; std::string tag; std::vector<TimingCaptureField> fields;
};
// Only an ok capture whose memory clock held still has a clock to file it by.
std::optional<TimingCapture> timing_capture_for_comparison(const TimingSnapshot&);
// Released 1.9.1 timings.compare / cycles_agree / _verdict.
inline constexpr double timing_ratio_tolerance=0.10, timing_flat_tolerance=0.10;
bool timing_cycles_agree(double base,double other,double clock_ratio);
struct TimingComparisonRow {
    std::string name, reg, bits;
    std::vector<std::optional<uint32_t>> cycles; std::vector<std::optional<double>> ratios;
    std::string verdict, note;
};
// order: capture indexes, slowest first (the baseline). Empty rows and ratios
// unless at least two captures have a memory clock.
struct TimingComparison { std::vector<size_t> order; std::vector<double> clock_ratios; std::vector<TimingComparisonRow> rows; };
TimingComparison compare_timing_captures(const std::vector<TimingCapture>&);
}
