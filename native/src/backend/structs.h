// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// NVAPI and NVML structure layouts, byte for byte the ctypes layouts in
// nvbackend.py ("verified" there against the driver's own GET output). The
// static_asserts pin the sizes the version words encode.
#pragma once

#include <cstddef>
#include <cstdint>

namespace druta::backend {

// NVAPI: version = sizeof | (v << 16). NVML: version = sizeof | (v << 24).
template <typename S>
constexpr std::uint32_t nvapi_ver(std::uint32_t v) {
    return static_cast<std::uint32_t>(sizeof(S)) | (v << 16);
}
template <typename S>
constexpr std::uint32_t nvml_ver(std::uint32_t v) {
    return static_cast<std::uint32_t>(sizeof(S)) | (v << 24);
}

struct GpuArchInfo {  // NV_GPU_ARCH_INFO V1/V2 share this 16-byte storage
    std::uint32_t version, architecture, implementation, revision;
};
static_assert(sizeof(GpuArchInfo) == 16);

struct Sensor {
    std::uint32_t controller;
    std::int32_t dmin, dmax, cur;
    std::uint32_t target;
};
struct ThermalSettings {
    std::uint32_t version, count;
    Sensor sensor[3];
};
static_assert(sizeof(ThermalSettings) == 68);

struct ThermalSensorsEx {
    std::uint32_t version, mask;
    std::int32_t reserved[8];
    std::int32_t temps[32];
};
static_assert(sizeof(ThermalSensorsEx) == 168);

struct VoltStatus {
    std::uint32_t version, flags, rsvd[8], value_uV, tail[8];
};
static_assert(sizeof(VoltStatus) == 76);

struct PwrTopoEntry {
    std::uint32_t domain, unk1, power_pcm, unk2;
};
struct PwrTopo {
    std::uint32_t version, count;
    PwrTopoEntry entries[4];
};
static_assert(sizeof(PwrTopo) == 72);

struct PwrPolStatusEntry {
    std::uint32_t pstate, unk1, target_pcm, unk2;
};
struct PwrPolStatus {
    std::uint32_t version, count;
    PwrPolStatusEntry entries[4];
};
static_assert(sizeof(PwrPolStatus) == 72);

struct CoolerSetting {
    std::uint32_t type, controller, default_min, default_max, current_min, current_max, current_level,
        default_policy, current_policy, target, control_type, active;
};
struct CoolerSettings {
    std::uint32_t version, count;
    CoolerSetting entries[20];
};
static_assert(sizeof(CoolerSettings) == 968);

struct FanCoolerControlEntry {
    std::uint32_t id, level, mode, reserved[8];
};
struct FanCoolersControl {
    std::uint32_t version, unknown, count, reserved[8];
    FanCoolerControlEntry entries[32];
};
static_assert(sizeof(FanCoolersControl) == 1452);

struct FanCoolerStatusEntry {
    std::uint32_t id, rpm, min, max, level, reserved[8];
};
struct FanCoolersStatus {
    std::uint32_t version, count, reserved[8];
    FanCoolerStatusEntry entries[32];
};
static_assert(sizeof(FanCoolersStatus) == 1704);

struct UtilDomain {
    std::uint32_t present, percentage;
};
struct DynPstates {
    std::uint32_t version, flags;
    UtilDomain util[8];
};
static_assert(sizeof(DynPstates) == 72);

struct ClkDomain {
    std::uint32_t present, frequency;
};
struct ClkFreqs {
    std::uint32_t version, clockType;
    ClkDomain domain[32];
};
static_assert(sizeof(ClkFreqs) == 264);

inline constexpr std::size_t kPrivClkDwords = 288;
struct AllClocksPriv {
    std::uint32_t version;
    std::uint32_t w[kPrivClkDwords];
};
static_assert(sizeof(AllClocksPriv) == 1156);

struct LockEntry {
    std::uint32_t domain, unk1, lockMode, unk2, volt_uV, unk3;
};
struct ClockLock {
    std::uint32_t version, flags, count;
    LockEntry locks[32];
};
static_assert(sizeof(ClockLock) == 780);
inline constexpr std::uint32_t kVfLockVersion = 2;
inline constexpr std::uint32_t kVfLockModeFreq = 2;   // NVML locked clocks; field is kHz
inline constexpr std::uint32_t kVfLockModePoint = 3;  // locked to the point at or below volt_uV
inline constexpr std::uint32_t kClkLockDomainMax = 0;
inline constexpr std::uint32_t kClkLockDomainMin = 1;

struct VoltBoost {  // NV_GPU_CLIENT_VOLT_RAILS_CONTROL_V1
    std::uint32_t version;
    std::int32_t percent;
    std::uint32_t unknown[8];
};
static_assert(sizeof(VoltBoost) == 40);

struct Pstate20Delta {
    std::int32_t value, minimum, maximum;
};
struct Pstate20Clock {
    std::uint32_t domain, kind, flags;
    Pstate20Delta delta;
    std::uint32_t data[5];
};
struct Pstate20Voltage {
    std::uint32_t domain, flags, voltage_uv;
    Pstate20Delta delta;
};
struct Pstate20Entry {
    std::uint32_t pstate, flags;
    Pstate20Clock clocks[8];
    Pstate20Voltage voltages[4];
};
struct Pstates20V1 {
    std::uint32_t version, flags, num_pstates, num_clocks, num_voltages;
    Pstate20Entry pstates[16];
};
struct Pstates20V2 {
    std::uint32_t version, flags, num_pstates, num_clocks, num_voltages;
    Pstate20Entry pstates[16];
    std::uint32_t num_ov_voltages;
    Pstate20Voltage ov_voltages[4];
};
static_assert(sizeof(Pstate20Clock) == 44);
static_assert(sizeof(Pstates20V1) == 7316);
static_assert(sizeof(Pstates20V2) == 7416);

// ---- NVML -------------------------------------------------------------------------------------

union NvmlValue {
    double d;
    std::uint32_t ui;
    unsigned long ul;  // nvmlValueType 2 is "unsigned long": 32-bit on Windows
    std::uint64_t ull;
    std::int64_t sll;
    std::int32_t si;
};
struct FieldValue {
    std::uint32_t fieldId, scopeId;
    std::int64_t timestamp, latencyUsec;
    std::uint32_t valueType, nvmlReturn;
    NvmlValue value;
};
static_assert(sizeof(FieldValue) == 40);

struct ClockOffset {
    std::uint32_t version, type, pstate;
    std::int32_t off, mn, mx;
};
static_assert(sizeof(ClockOffset) == 24);

struct FanSpeedInfo {
    std::uint32_t version, fan, speed;
};
static_assert(sizeof(FanSpeedInfo) == 12);

}  // namespace druta::backend
