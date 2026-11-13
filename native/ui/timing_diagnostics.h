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
TimingDiagnostics timing_diagnostics(const TimingSnapshot&,const TimingDiagnosticEvidence& = {});
}
