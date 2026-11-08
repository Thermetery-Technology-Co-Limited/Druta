// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Live telemetry and the static capability record: a port of nvbackend.GPU's
// read()/_read_* family and the non-identity part of _read_static. Every
// value is optional: an export that is absent, a call that fails, or a value
// outside its plausible range leaves the field unset, exactly as the Python
// dict leaves the key out.
#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "backend/gpu.h"
#include "backend/structs.h"

namespace druta::backend {

// nvmlFieldValue_t carried with its type, as Python's _field_val keeps the
// union member that matches nvmlValueType.
struct TypedValue {
    std::uint32_t type = 3;  // 0 double, 1 u32, 2 unsigned long, 3 u64, 4 i64, 5 i32
    double d = 0;
    std::uint64_t u = 0;
    std::int64_t s = 0;
    double as_double() const;
};

struct NativeFan {
    std::uint32_t id = 0;
    std::uint32_t level = 0;
    std::uint32_t policy = 0;
    std::uint32_t default_policy = 0;
    bool manual = false;
    std::uint32_t min = 0;
    std::uint32_t max = 0;
    std::optional<std::uint32_t> rpm;
};

struct NativeFanState {
    std::string source;  // "nvml", "nvapi_cooler" or "nvapi_client"
    std::vector<NativeFan> fans;
    FanCoolersControl control{};  // the ClientFanCoolers getter buffer (nvapi_client only), reused by writes
};

struct FanReading {
    std::optional<std::uint32_t> duty;
    std::optional<std::uint32_t> rpm;
};

struct Snapshot {
    std::optional<std::uint32_t> core, mem, video, xbar;  // MHz
    std::optional<std::int32_t> core_off, mem_off;
    std::optional<AllClocksPriv> priv_clocks;  // the raw private getter payload (read_clock_domains input)
    std::optional<std::int32_t> temp_edge;
    std::optional<double> temp_hotspot;
    std::vector<double> temp_sensors;  // rounded to one decimal, as Python's round(t, 1)
    std::optional<double> temp_delta;
    std::optional<std::uint32_t> pl_requested_mw;
    std::optional<double> power_w;
    std::optional<std::uint32_t> pl_now_mw;
    std::optional<double> pwr_gpu_pct, pwr_board_pct, pl_target_pct, vcore_mv;
    std::optional<std::uint32_t> num_fans;
    std::vector<FanReading> fans;
    std::optional<std::uint32_t> util_gpu, util_fb, util_vid, util_bus;
    std::optional<std::uint64_t> event_mask;
    std::optional<std::uint32_t> perf_decrease;
    std::optional<std::uint32_t> pcie_gen, pcie_width;
    std::optional<std::map<std::string, TypedValue>> pcie_err;
    std::optional<double> pcie_err_total, pcie_err_since;
    std::optional<std::uint32_t> pstate;
    std::optional<std::int32_t> vboost_pct;
    std::optional<std::uint32_t> core_p0max, mem_p0max;
    std::optional<double> energy_j;
    std::optional<std::vector<std::uint32_t>> vf_locked_domains;
    std::optional<double> vf_lock_mv;
    std::optional<std::pair<std::uint32_t, std::uint32_t>> clk_lock_mhz;
};

struct OffsetRange {
    std::int32_t low, high, current;
};

struct StaticCapabilities {
    std::optional<std::uint32_t> pl_min_mw, pl_max_mw, pl_def_mw;
    std::vector<std::uint32_t> mem_clocks;
    std::optional<std::uint32_t> gfx_min, gfx_max;
    std::optional<std::uint32_t> fan_min, fan_max;
    std::optional<std::uint32_t> mem_type_id;
    std::string mem_type = "unknown";
    std::optional<std::uint32_t> mem_div;
    std::optional<OffsetRange> core_off_range, mem_off_range;
};

// NVML architecture enum (nvbackend.GPU.ARCH_*), or nullopt.
std::optional<std::uint32_t> read_architecture(Gpu& gpu);
const char* architecture_name(std::uint32_t architecture);

class Telemetry {
public:
    explicit Telemetry(Gpu& gpu) : gpu_(gpu) {}

    Snapshot read();
    StaticCapabilities read_static();

    // nvbackend.GPU.arch: NVML's enum, cached once a recognized value is seen.
    std::optional<std::uint32_t> arch();

    // The pieces, public so parity tests can run each against its Python twin.
    void read_clocks(Snapshot& d, const AllClocksPriv* priv);
    void read_temps(Snapshot& d);
    void read_power(Snapshot& d);
    void read_fan(Snapshot& d);
    void read_util(Snapshot& d);
    void read_throttle(Snapshot& d);
    void read_pcie(Snapshot& d);
    void read_misc(Snapshot& d);

    std::optional<AllClocksPriv> priv_clocks();
    std::optional<NativeFanState> native_fan_data();
    std::optional<OffsetRange> offset_range(std::uint32_t ctype);
    std::optional<std::int32_t> read_voltage_boost();
    std::optional<std::uint32_t> read_power_limit_nvml();
    std::vector<std::uint32_t> supported_nvml_clocks(const char* function, std::optional<std::uint32_t> selector);

    // The first PCIe error total seen by this GPU object (Python _pcie_baseline).
    std::optional<double> pcie_baseline;

private:
    std::optional<std::uint32_t> arch_cache_;

public:
    // The public P-state table (_read_pstates20 / _pstate20_clock), shared with the clock writers.
    struct Pstates20 {
        Pstates20V2 table{};
        bool v2 = false;
    };
    std::optional<Pstates20> read_pstates20(std::string& error);
    const Pstate20Clock* pstate20_clock(const Pstates20& info, std::uint32_t ctype) const;

private:
    Gpu& gpu_;
};

}  // namespace druta::backend
