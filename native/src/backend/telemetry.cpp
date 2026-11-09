// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/telemetry.h"

#include <algorithm>
#include <cmath>
#include <set>

namespace druta::backend {
namespace {

using HandleU32Fn = int(DRUTA_CDECL*)(void*, std::uint32_t*);
using HandleU64Fn = int(DRUTA_CDECL*)(void*, std::uint64_t*);
using HandlePtrFn = int(DRUTA_CDECL*)(void*, void*);
using HandleIdxPtrFn = int(DRUTA_CDECL*)(void*, std::uint32_t, void*);
using HandleIdxU32Fn = int(DRUTA_CDECL*)(void*, std::uint32_t, std::uint32_t*);

// Interface ids of the readers this file uses (nvbackend.NvAPI.__init__).
namespace id {
constexpr std::uint32_t ThermalSettings = 0xE3640A56;
constexpr std::uint32_t ThermalSensors = 0x65FE3AAD;
constexpr std::uint32_t CoolerSettings = 0xDA141340;
constexpr std::uint32_t TachReading = 0x5F608315;
constexpr std::uint32_t FanCoolersControl = 0x814B209F;
constexpr std::uint32_t FanCoolersStatus = 0x35AED5E8;
constexpr std::uint32_t VoltRailsStatus = 0x465F9BCF;
constexpr std::uint32_t PowerTopo = 0xEDCF624E;
constexpr std::uint32_t PowerPolStatus = 0x70916171;
constexpr std::uint32_t PerfDecrease = 0x7F7F4600;
constexpr std::uint32_t DynPstates = 0x60DED2ED;
constexpr std::uint32_t CurrentPstate = 0x927DA4F6;
constexpr std::uint32_t Pstates20Get = 0x6FF81213;
constexpr std::uint32_t AllClocks = 0xDCB616C3;
constexpr std::uint32_t AllClocksPrivate = 0x1BD69F49;
constexpr std::uint32_t BoostLock = 0xE440B867;
constexpr std::uint32_t RamType = 0x57F7CAAC;
constexpr std::uint32_t VoltCtrlGet = 0x9DF23CA1;
}  // namespace id

constexpr std::uint32_t kPrivSlotCore = 0, kPrivSlotXbar = 2, kPrivSlotMem = 8, kPrivSlotVideo = 42;

// nvbackend.PCIE_ERR_FIELDS, in sorted field-id order
constexpr std::pair<std::uint32_t, const char*> kPcieErrFields[] = {
    {173, "correctable"}, {174, "naks_rx"}, {175, "receiver"}, {176, "bad_tlp"}, {177, "naks_tx"},
    {178, "bad_dllp"},    {179, "non_fatal"}, {180, "fatal"},  {181, "unsupp_req"}, {182, "lcrc"}, {183, "lane"},
};

// nvbackend.MEM_TYPES: only positively identified types are scaled
struct MemType {
    std::uint32_t id;
    const char* name;
    std::uint32_t div;
};
constexpr MemType kMemTypes[] = {{7, "DDR3", 1}, {8, "GDDR5", 2}, {10, "GDDR5X", 4}, {14, "GDDR6", 4}, {15, "GDDR6X", 8}};

// Python round(x, 1) on a value whose tenfold is exactly representable:
// ties-to-even on the exact value.
double round1(double value) {
    return std::nearbyint(value * 10.0) / 10.0;
}

}  // namespace

double TypedValue::as_double() const {
    switch (type) {
        case 0: return d;
        case 4: case 5: return static_cast<double>(s);
        default: return static_cast<double>(u);
    }
}

const char* architecture_name(std::uint32_t architecture) {
    switch (architecture) {
        case 1: return "Fermi";
        case 2: return "Kepler";
        case 3: return "Maxwell";
        case 4: return "Pascal";
        case 5: return "Volta";
        case 6: return "Turing";
        case 7: return "Ampere";
        case 8: return "Ada";
        case 9: return "Hopper";
        case 10: return "Blackwell";
        default: return nullptr;
    }
}

std::optional<std::uint32_t> read_architecture(Gpu& gpu, const char** source) {
    // NVML's enum first; then the public NVAPI architecture id translated to
    // it. Unlisted ids stay unknown; nothing here reads a device id.
    if (gpu.nvml.ok) {
        if (auto fn = gpu.nvml.get<HandleU32Fn>("nvmlDeviceGetArchitecture")) {
            std::uint32_t value = 0;
            if (fn(gpu.nvml.dev, &value) == 0 && architecture_name(value)) {
                if (source) *source = "NVML";
                return value;
            }
        }
    }
    if (gpu.nvapi.ok) {
        if (auto reader = gpu.nvapi.get<HandlePtrFn>(nvapi_id::GetArchInfo)) {
            for (std::uint32_t revision : {2u, 1u}) {
                GpuArchInfo info{};
                info.version = nvapi_ver<GpuArchInfo>(revision);
                int status = reader(gpu.nvapi.gpu, &info);
                if (status == -9) continue;  // NVAPI_INCOMPATIBLE_STRUCT_VERSION
                std::optional<std::uint32_t> architecture;
                switch (info.architecture) {
                    case 0xC0: case 0xD0: architecture = 1; break;
                    case 0xE0: case 0xF0: case 0x100: architecture = 2; break;
                    case 0x110: case 0x120: architecture = 3; break;
                    case 0x130: architecture = 4; break;
                    case 0x140: case 0x150: architecture = 5; break;
                    case 0x160: architecture = 6; break;
                    case 0x170: architecture = 7; break;
                    case 0x190: architecture = 8; break;
                    case 0x1B0: architecture = 10; break;
                    default: break;
                }
                if (status == 0 && info.version == nvapi_ver<GpuArchInfo>(revision) && architecture) {
                    if (source) *source = revision == 2 ? "NVAPI V2" : "NVAPI V1";
                    return architecture;
                }
                break;
            }
        }
    }
    return std::nullopt;
}

// ---- clocks ---------------------------------------------------------------------------------------

std::optional<AllClocksPriv> Telemetry::priv_clocks() {
    Nvapi& a = gpu_.nvapi;
    auto fn = a.ok ? a.get<HandlePtrFn>(id::AllClocksPrivate) : nullptr;
    if (!fn) return std::nullopt;
    AllClocksPriv pc{};
    pc.version = nvapi_ver<AllClocksPriv>(2);
    if (fn(a.gpu, &pc) != 0) return std::nullopt;
    return pc;
}

void Telemetry::read_clocks(Snapshot& d, const AllClocksPriv* priv) {
    Nvapi& a = gpu_.nvapi;
    if (auto fn = a.ok ? a.get<HandlePtrFn>(id::AllClocks) : nullptr) {
        ClkFreqs cf{};
        cf.version = nvapi_ver<ClkFreqs>(2);
        cf.clockType = 0;  // CURRENT
        if (fn(a.gpu, &cf) == 0) {
            for (std::uint32_t i : {0u, 4u, 8u}) {
                if (!(cf.domain[i].present & 1)) continue;
                std::uint32_t mhz = cf.domain[i].frequency / 1000;
                if (i == 0) d.core = mhz;
                if (i == 4) d.mem = mhz;
                if (i == 8) d.video = mhz;
            }
        }
    }
    // XBAR (and a fallback for the domains above) via the private getter:
    // array-A slots, i.e. the PROGRAMMED target.
    std::optional<AllClocksPriv> own;
    if (!priv) {
        own = priv_clocks();
        if (own) priv = &*own;
    }
    if (priv) {
        auto take = [&](std::uint32_t slot, std::optional<std::uint32_t>& field, bool always) {
            std::uint32_t v = priv->w[slot] / 1000;
            if (v && (always || !field)) field = v;
        };
        take(kPrivSlotCore, d.core, false);
        take(kPrivSlotXbar, d.xbar, true);
        take(kPrivSlotMem, d.mem, false);
        take(kPrivSlotVideo, d.video, false);
    }
    if (auto range = offset_range(0)) d.core_off = range->current;
    if (auto range = offset_range(2)) d.mem_off = range->current;
}

std::optional<Telemetry::Pstates20> Telemetry::read_pstates20(std::string& error) {
    Nvapi& a = gpu_.nvapi;
    auto getter = a.ok ? a.get<HandlePtrFn>(id::Pstates20Get) : nullptr;
    if (!getter) {
        error = "NVAPI Pstates20 is unavailable";
        return std::nullopt;
    }
    struct Attempt {
        bool v2;
        std::uint32_t version;
    };
    for (Attempt attempt : {Attempt{true, 3}, Attempt{true, 2}, Attempt{false, 1}}) {
        Pstates20 info;
        std::uint32_t expected = attempt.v2 ? nvapi_ver<Pstates20V2>(attempt.version) : nvapi_ver<Pstates20V1>(attempt.version);
        info.table.version = expected;
        info.v2 = attempt.v2;
        int status = getter(a.gpu, &info.table);
        if (status == -9) continue;  // NVAPI_INCOMPATIBLE_STRUCT_VERSION
        if (status != 0) {
            error = "NVAPI Pstates20 read failed: status " + std::to_string(status);
            return std::nullopt;
        }
        const Pstates20V2& t = info.table;
        if (t.version != expected || !(1 <= t.num_pstates && t.num_pstates <= 16) ||
            !(1 <= t.num_clocks && t.num_clocks <= 8) || t.num_voltages > 4 || (info.v2 && t.num_ov_voltages > 4)) {
            error = "NVAPI Pstates20 returned an invalid table";
            return std::nullopt;
        }
        return info;
    }
    error = "NVAPI Pstates20 structure version is unsupported";
    return std::nullopt;
}

const Pstate20Clock* Telemetry::pstate20_clock(const Pstates20& info, std::uint32_t ctype) const {
    if (ctype != 0 && ctype != 2) return nullptr;
    const Pstate20Entry* state = nullptr;
    int states = 0;
    for (std::uint32_t i = 0; i < info.table.num_pstates; ++i) {
        if (info.table.pstates[i].pstate == 0) {
            state = &info.table.pstates[i];
            ++states;
        }
    }
    if (states != 1) return nullptr;
    const std::uint32_t domain = ctype == 0 ? 0 : 4;
    const Pstate20Clock* clock = nullptr;
    int clocks = 0;
    for (std::uint32_t i = 0; i < info.table.num_clocks; ++i) {
        if (state->clocks[i].domain == domain) {
            clock = &state->clocks[i];
            ++clocks;
        }
    }
    return clocks == 1 ? clock : nullptr;
}

std::optional<OffsetRange> Telemetry::offset_range(std::uint32_t ctype) {
    if (ctype != 0 && ctype != 2) return std::nullopt;
    Nvml& nv = gpu_.nvml;
    if (nv.ok) {
        if (auto fn = nv.get<HandlePtrFn>("nvmlDeviceGetClockOffsets")) {
            ClockOffset co{};
            co.version = nvml_ver<ClockOffset>(1);
            co.type = ctype;
            co.pstate = 0;
            if (fn(nv.dev, &co) == 0) return OffsetRange{co.mn, co.mx, co.off};
        }
    }
    std::string error;
    auto info = read_pstates20(error);
    if (!info) return std::nullopt;
    const Pstate20Clock* clock = pstate20_clock(*info, ctype);
    if (!clock || !(clock->flags & 1)) return std::nullopt;
    const Pstate20Delta& delta = clock->delta;
    if (delta.minimum > delta.maximum) return std::nullopt;
    // Round bounds inward, in integer MHz. NVML doubles memory offsets.
    const std::int64_t scale = ctype == 2 ? 2 : 1;
    const std::int64_t min_scaled = static_cast<std::int64_t>(delta.minimum) * scale;
    const std::int64_t max_scaled = static_cast<std::int64_t>(delta.maximum) * scale;
    auto floor_div = [](std::int64_t n, std::int64_t q) { return n >= 0 ? n / q : -((-n + q - 1) / q); };
    const std::int64_t low = -floor_div(-min_scaled, 1000);  // ceil(min/1000)
    const std::int64_t high = floor_div(max_scaled, 1000);   // floor(max/1000)
    if (low > high) return std::nullopt;
    // int(delta.value * scale / 1000): truncation toward zero of the float quotient
    const std::int64_t current = static_cast<std::int64_t>(std::trunc(static_cast<double>(delta.value) * static_cast<double>(scale) / 1000.0));
    return OffsetRange{static_cast<std::int32_t>(low), static_cast<std::int32_t>(high), static_cast<std::int32_t>(current)};
}

// ---- temperatures ---------------------------------------------------------------------------------

void Telemetry::read_temps(Snapshot& d) {
    Nvapi& a = gpu_.nvapi;
    if (!a.ok) return;
    using ThermalFn = int(DRUTA_CDECL*)(void*, std::uint32_t, void*);
    if (auto fn = a.get<ThermalFn>(id::ThermalSettings)) {
        ThermalSettings ts{};
        ts.version = nvapi_ver<ThermalSettings>(2);
        if (fn(a.gpu, 15, &ts) == 0) {
            for (std::uint32_t k = 0; k < std::min<std::uint32_t>(ts.count, 3); ++k)
                if (ts.sensor[k].target == 1) d.temp_edge = ts.sensor[k].cur;  // GPU
        }
    }
    if (auto fn = a.get<HandlePtrFn>(id::ThermalSensors)) {
        std::optional<std::pair<ThermalSensorsEx, std::uint32_t>> best;
        for (std::uint32_t nbits = 1; nbits <= 32; ++nbits) {
            ThermalSensorsEx tx{};
            tx.version = nvapi_ver<ThermalSensorsEx>(2);
            tx.mask = nbits == 32 ? 0xFFFFFFFFu : (1u << nbits) - 1;
            if (fn(a.gpu, &tx) == 0)
                best = {tx, nbits};
            else
                break;
        }
        if (best) {
            // keep only physically plausible channels: some reserved slots
            // return spurious values, so bound to (0, 150) C before max().
            std::vector<double> temps;
            for (std::uint32_t k = 0; k < best->second; ++k) {
                std::int32_t raw = best->first.temps[k];
                if (0 < raw && raw < 150 * 256) temps.push_back(raw / 256.0);
            }
            if (!temps.empty()) {
                d.temp_hotspot = *std::max_element(temps.begin(), temps.end());
                d.temp_sensors.clear();
                for (double t : temps) d.temp_sensors.push_back(round1(t));
            }
        }
    }
    if (d.temp_hotspot && d.temp_edge) d.temp_delta = *d.temp_hotspot - *d.temp_edge;
}

// ---- power ----------------------------------------------------------------------------------------

std::optional<std::uint32_t> Telemetry::read_power_limit_nvml() {
    // Configured NVML request only; never substitute enforced telemetry.
    Nvml& nv = gpu_.nvml;
    auto fn = nv.ok ? nv.get<HandleU32Fn>("nvmlDeviceGetPowerManagementLimit") : nullptr;
    if (!fn) return std::nullopt;
    std::uint32_t value = 0;
    if (fn(nv.dev, &value) == 0 && value > 0) return value;
    return std::nullopt;
}

void Telemetry::read_power(Snapshot& d) {
    Nvml& nv = gpu_.nvml;
    if (nv.ok) {
        // read_power_limit_mw: the NVML request; the live-power (RM) fallback
        // is not part of this slice and stays with the transport layer.
        d.pl_requested_mw = read_power_limit_nvml();
        std::uint32_t v = 0;
        if (auto fn = nv.get<HandleU32Fn>("nvmlDeviceGetPowerUsage"); fn && fn(nv.dev, &v) == 0) d.power_w = v / 1000.0;
        if (auto fn = nv.get<HandleU32Fn>("nvmlDeviceGetEnforcedPowerLimit"); fn && fn(nv.dev, &v) == 0) d.pl_now_mw = v;
    }
    Nvapi& a = gpu_.nvapi;
    if (!a.ok) return;
    if (auto fn = a.get<HandlePtrFn>(id::PowerTopo)) {
        PwrTopo pt{};
        pt.version = nvapi_ver<PwrTopo>(1);
        if (fn(a.gpu, &pt) == 0) {
            for (std::uint32_t k = 0; k < std::min<std::uint32_t>(pt.count, 4); ++k) {
                const PwrTopoEntry& e = pt.entries[k];
                if (e.domain == 0) d.pwr_gpu_pct = e.power_pcm / 1000.0;
                if (e.domain == 1) d.pwr_board_pct = e.power_pcm / 1000.0;
            }
        }
    }
    if (auto fn = a.get<HandlePtrFn>(id::PowerPolStatus)) {
        PwrPolStatus ps{};
        ps.version = nvapi_ver<PwrPolStatus>(1);
        if (fn(a.gpu, &ps) == 0 && ps.count) d.pl_target_pct = ps.entries[0].target_pcm / 1000.0;
    }
    if (auto fn = a.get<HandlePtrFn>(id::VoltRailsStatus)) {
        VoltStatus vs{};
        vs.version = nvapi_ver<VoltStatus>(1);
        if (fn(a.gpu, &vs) == 0 && vs.value_uV) d.vcore_mv = vs.value_uV / 1000.0;
    }
}

std::optional<double> Telemetry::read_vcore_mv() {
    // The same single VoltRailsStatus call read_power makes, without the rest of the sweep.
    Nvapi& a = gpu_.nvapi;
    auto fn = a.ok ? a.get<HandlePtrFn>(id::VoltRailsStatus) : nullptr;
    if (!fn) return std::nullopt;
    VoltStatus vs{};
    vs.version = nvapi_ver<VoltStatus>(1);
    if (fn(a.gpu, &vs) == 0 && vs.value_uV) return vs.value_uV / 1000.0;
    return std::nullopt;
}

// ---- fans -----------------------------------------------------------------------------------------

std::optional<NativeFanState> Telemetry::native_fan_data() {
    // Pascal exposes classic CoolerSettings; Turing exposes ClientFanCoolers.
    // Classic policy 1 is manual; the client interface uses mode 0/1.
    Nvapi& a = gpu_.nvapi;
    if (!a.ok) return std::nullopt;
    using CoolerFn = int(DRUTA_CDECL*)(void*, std::uint32_t, void*);
    if (auto getter = a.get<CoolerFn>(id::CoolerSettings)) {
        CoolerSettings buf{};
        buf.version = nvapi_ver<CoolerSettings>(1);
        if (getter(a.gpu, 7, &buf) == 0 && 0 < buf.count && buf.count <= 20) {
            NativeFanState state;
            state.source = "nvapi_cooler";
            for (std::uint32_t index = 0; index < buf.count; ++index) {
                const CoolerSetting& row = buf.entries[index];
                auto in = [](std::uint32_t v, std::initializer_list<std::uint32_t> set) {
                    return std::find(set.begin(), set.end(), v) != set.end();
                };
                if (!(row.current_min <= row.current_max && row.current_max <= 100 && row.current_level <= 100 &&
                      in(row.current_policy, {1, 2, 4, 8, 16}) && in(row.default_policy, {2, 4, 8, 16})))
                    return std::nullopt;
                NativeFan fan;
                fan.id = index;
                fan.level = row.current_level;
                fan.policy = row.current_policy;
                fan.default_policy = row.default_policy;
                fan.manual = row.current_policy == 1;
                fan.min = row.current_min;
                fan.max = row.current_max;
                state.fans.push_back(fan);
            }
            if (state.fans.size() == 1) {
                if (auto tach = a.get<HandleU32Fn>(id::TachReading)) {
                    std::uint32_t rpm = 0;
                    if (tach(a.gpu, &rpm) == 0) state.fans[0].rpm = rpm;
                }
            }
            return state;
        }
    }
    auto control_get = a.get<HandlePtrFn>(id::FanCoolersControl);
    auto status_get = a.get<HandlePtrFn>(id::FanCoolersStatus);
    if (!control_get || !status_get) return std::nullopt;
    FanCoolersControl ctl{};
    ctl.version = nvapi_ver<FanCoolersControl>(1);
    FanCoolersStatus status{};
    status.version = nvapi_ver<FanCoolersStatus>(1);
    if (control_get(a.gpu, &ctl) != 0 || status_get(a.gpu, &status) != 0 ||
        !(0 < ctl.count && ctl.count <= 32 && ctl.count == status.count))
        return std::nullopt;
    std::map<std::uint32_t, const FanCoolerStatusEntry*> by_id;
    for (std::uint32_t i = 0; i < status.count; ++i) by_id[status.entries[i].id] = &status.entries[i];
    std::set<std::uint32_t> control_ids;
    for (std::uint32_t i = 0; i < ctl.count; ++i) control_ids.insert(ctl.entries[i].id);
    if (by_id.size() != status.count || control_ids.size() != ctl.count) return std::nullopt;
    NativeFanState state;
    state.source = "nvapi_client";
    state.control = ctl;
    for (std::uint32_t index = 0; index < ctl.count; ++index) {
        const FanCoolerControlEntry& row = ctl.entries[index];
        auto live = by_id.find(row.id);
        if (live == by_id.end() || (row.mode != 0 && row.mode != 1) || row.level > 100 ||
            !(live->second->min <= live->second->max && live->second->max <= 100))
            return std::nullopt;
        NativeFan fan;
        fan.id = row.id;
        fan.level = row.level;
        fan.policy = row.mode;
        fan.default_policy = 0;
        fan.manual = row.mode == 1;
        fan.min = live->second->min;
        fan.max = live->second->max;
        fan.rpm = live->second->rpm;
        state.fans.push_back(fan);
    }
    return state;
}

void Telemetry::read_fan(Snapshot& d) {
    Nvml& nv = gpu_.nvml;
    auto native = native_fan_data();
    std::uint32_t nf = 0;
    bool counted = false;
    if (auto fn = nv.ok ? nv.get<HandleU32Fn>("nvmlDeviceGetNumFans") : nullptr) counted = fn(nv.dev, &nf) == 0;
    if (!counted && native) {
        nf = static_cast<std::uint32_t>(native->fans.size());
        counted = true;
    }
    if (counted) d.num_fans = nf;
    std::vector<FanReading> fans;
    // R470 has GetFanSpeed[_v2], but no GetNumFans. Read its first fan
    // without claiming the adapter has exactly one.
    for (std::uint32_t f = 0; f < (counted ? nf : 1u); ++f) {
        FanReading reading;
        std::uint32_t speed = 0;
        if (auto fn = nv.ok ? nv.get<HandleIdxU32Fn>("nvmlDeviceGetFanSpeed_v2") : nullptr) {
            if (fn(nv.dev, f, &speed) == 0) reading.duty = speed;
        }
        if (!reading.duty && f == 0) {
            if (auto fn = nv.ok ? nv.get<HandleU32Fn>("nvmlDeviceGetFanSpeed") : nullptr) {
                if (fn(nv.dev, &speed) == 0) reading.duty = speed;
            }
        }
        if (auto fn = nv.ok ? nv.get<HandlePtrFn>("nvmlDeviceGetFanSpeedRPM") : nullptr) {
            FanSpeedInfo fi{};
            fi.version = nvml_ver<FanSpeedInfo>(1);
            fi.fan = f;
            if (fn(nv.dev, &fi) == 0) reading.rpm = fi.speed;
        }
        if (counted || reading.duty || reading.rpm) fans.push_back(reading);
    }
    d.fans = fans;
    if (native) {
        d.num_fans = static_cast<std::uint32_t>(native->fans.size());
        std::vector<FanReading> merged;
        for (std::size_t index = 0; index < native->fans.size(); ++index) {
            FanReading from_nvml = index < fans.size() ? fans[index] : FanReading{};
            FanReading out;
            out.duty = from_nvml.duty ? from_nvml.duty : std::optional<std::uint32_t>(native->fans[index].level);
            out.rpm = from_nvml.rpm ? from_nvml.rpm : native->fans[index].rpm;
            merged.push_back(out);
        }
        d.fans = merged;
    }
}

// ---- utilisation, throttle, PCIe, misc ------------------------------------------------------------

void Telemetry::read_util(Snapshot& d) {
    Nvapi& a = gpu_.nvapi;
    auto fn = a.ok ? a.get<HandlePtrFn>(id::DynPstates) : nullptr;
    if (!fn) return;
    DynPstates dp{};
    dp.version = nvapi_ver<DynPstates>(1);
    if (fn(a.gpu, &dp) != 0) return;
    std::optional<std::uint32_t>* fields[] = {&d.util_gpu, &d.util_fb, &d.util_vid, &d.util_bus};
    for (std::uint32_t k = 0; k < 4; ++k)
        if (dp.util[k].present) *fields[k] = dp.util[k].percentage;
}

void Telemetry::read_throttle(Snapshot& d) {
    Nvml& nv = gpu_.nvml;
    if (nv.ok) {
        // EventReasons renamed the older ThrottleReasons API; both expose the
        // same 64-bit mask.
        for (const char* name : {"nvmlDeviceGetCurrentClocksEventReasons", "nvmlDeviceGetCurrentClocksThrottleReasons"}) {
            if (auto fn = nv.get<HandleU64Fn>(name)) {
                std::uint64_t v = 0;
                if (fn(nv.dev, &v) == 0) {
                    d.event_mask = v;
                    break;
                }
            }
        }
    }
    Nvapi& a = gpu_.nvapi;
    if (auto fn = a.ok ? a.get<HandleU32Fn>(id::PerfDecrease) : nullptr) {
        std::uint32_t pd = 0;
        if (fn(a.gpu, &pd) == 0) d.perf_decrease = pd;
    }
}

void Telemetry::read_pcie(Snapshot& d) {
    Nvml& nv = gpu_.nvml;
    if (!nv.ok) return;
    std::uint32_t v = 0;
    if (auto fn = nv.get<HandleU32Fn>("nvmlDeviceGetCurrPcieLinkGeneration"); fn && fn(nv.dev, &v) == 0) d.pcie_gen = v;
    if (auto fn = nv.get<HandleU32Fn>("nvmlDeviceGetCurrPcieLinkWidth"); fn && fn(nv.dev, &v) == 0) d.pcie_width = v;
    using FieldsFn = int(DRUTA_CDECL*)(void*, int, FieldValue*);
    auto fields = nv.get<FieldsFn>("nvmlDeviceGetFieldValues");
    if (!fields) return;
    constexpr std::size_t count = sizeof kPcieErrFields / sizeof kPcieErrFields[0];
    FieldValue arr[count] = {};
    for (std::size_t i = 0; i < count; ++i) arr[i].fieldId = kPcieErrFields[i].first;
    if (fields(nv.dev, static_cast<int>(count), arr) != 0) return;
    std::map<std::string, TypedValue> per;
    for (std::size_t i = 0; i < count; ++i) {
        const FieldValue& fv = arr[i];
        if (fv.nvmlReturn != 0) continue;
        TypedValue value;
        value.type = fv.valueType;
        switch (fv.valueType) {
            case 0: value.d = fv.value.d; break;
            case 1: value.u = fv.value.ui; break;
            case 2: value.u = fv.value.ul; break;
            case 4: value.s = fv.value.sll; break;
            case 5: value.s = fv.value.si; break;
            default: value.type = 3; value.u = fv.value.ull; break;
        }
        per[kPcieErrFields[i].second] = value;
    }
    // headline = the three non-overlapping AER aggregates only
    double total = 0;
    for (const char* key : {"correctable", "non_fatal", "fatal"}) {
        auto it = per.find(key);
        if (it != per.end()) total += it->second.as_double();
    }
    d.pcie_err_total = total;
    d.pcie_err = per;
    if (!pcie_baseline) pcie_baseline = total;
    d.pcie_err_since = total - *pcie_baseline;
}

std::optional<std::int32_t> Telemetry::read_voltage_boost() {
    Nvapi& a = gpu_.nvapi;
    auto fn = a.ok ? a.get<HandlePtrFn>(id::VoltCtrlGet) : nullptr;
    if (!fn) return std::nullopt;
    VoltBoost vc{};
    vc.version = nvapi_ver<VoltBoost>(1);
    if (fn(a.gpu, &vc) == 0) return vc.percent;
    return std::nullopt;
}

void Telemetry::read_misc(Snapshot& d) {
    Nvapi& a = gpu_.nvapi;
    if (auto fn = a.ok ? a.get<HandleU32Fn>(id::CurrentPstate) : nullptr) {
        std::uint32_t v = 0;
        if (fn(a.gpu, &v) == 0) d.pstate = v;
    }
    d.vboost_pct = read_voltage_boost();
    Nvml& nv = gpu_.nvml;
    using MinMaxFn = int(DRUTA_CDECL*)(void*, std::uint32_t, std::uint32_t, std::uint32_t*, std::uint32_t*);
    if (auto fn = nv.ok ? nv.get<MinMaxFn>("nvmlDeviceGetMinMaxClockOfPState") : nullptr) {
        std::uint32_t mn = 0, mx = 0;
        if (fn(nv.dev, 0, 0, &mn, &mx) == 0) d.core_p0max = mx;
        if (fn(nv.dev, 2, 0, &mn, &mx) == 0) d.mem_p0max = mx;
    }
    if (!d.core_p0max || !d.mem_p0max) {
        std::string error;
        if (auto info = read_pstates20(error)) {
            for (std::uint32_t ctype : {0u, 2u}) {
                std::optional<std::uint32_t>& field = ctype == 0 ? d.core_p0max : d.mem_p0max;
                const Pstate20Clock* clock = pstate20_clock(*info, ctype);
                if (field || !clock || (clock->kind != 0 && clock->kind != 1)) continue;
                // SINGLE has one value, RANGE has min then max, both in kHz.
                std::uint32_t maximum = clock->kind == 1 ? clock->data[1] : clock->data[0];
                if (maximum) field = maximum / 1000;
            }
        }
    }
    if (auto fn = nv.ok ? nv.get<HandleU64Fn>("nvmlDeviceGetTotalEnergyConsumption") : nullptr) {
        std::uint64_t v = 0;
        if (fn(nv.dev, &v) == 0) d.energy_j = static_cast<double>(v) / 1000.0;
    }
    // V/F point-lock state. Version 2 is the verified one; the other two are
    // kept as a fallback for a driver that numbers this struct differently.
    if (auto fn = a.ok ? a.get<HandlePtrFn>(id::BoostLock) : nullptr) {
        for (std::uint32_t ver : {kVfLockVersion, 1u, 3u}) {
            ClockLock bl{};
            bl.version = nvapi_ver<ClockLock>(ver);
            if (fn(a.gpu, &bl) != 0) continue;
            const std::uint32_t count = std::min<std::uint32_t>(bl.count, 32);
            std::vector<std::uint32_t> locked;
            std::optional<double> lock_mv;
            std::map<std::uint32_t, std::uint32_t> freq;
            for (std::uint32_t k = 0; k < count; ++k) {
                const LockEntry& e = bl.locks[k];
                if (e.lockMode == kVfLockModePoint) {
                    locked.push_back(e.domain);
                    if (!lock_mv) lock_mv = e.volt_uV / 1000.0;
                }
                if (e.lockMode == kVfLockModeFreq) freq[e.domain] = e.volt_uV;  // last one wins, as a dict
            }
            d.vf_locked_domains = locked;
            d.vf_lock_mv = lock_mv;
            if (auto hi = freq.find(kClkLockDomainMax); hi != freq.end()) {
                auto lo = freq.find(kClkLockDomainMin);
                std::uint32_t low = lo != freq.end() ? lo->second : hi->second;
                d.clk_lock_mhz = {low / 1000, hi->second / 1000};
            }
            break;
        }
    }
    // The Turing legacy clock-lock fallback (_read_legacy_clk_lock) needs the
    // RM transport and is not part of this slice.
}

std::optional<std::uint32_t> Telemetry::arch() {
    // Under the handle lock like read(): the cache and its source are written here and read by the
    // rail entries, which hold the lock, while dc_gpu_architecture does not take it itself.
    LockGuard guard(gpu_.lock());
    if (!arch_cache_) arch_cache_ = read_architecture(gpu_, &arch_source_);
    return arch_cache_;
}

// ---- the tick -------------------------------------------------------------------------------------

Snapshot Telemetry::read() {
    LockGuard guard(gpu_.lock());
    Snapshot d;
    // ONE private-getter call per tick, shared by the tiles and the domain readout.
    d.priv_clocks = priv_clocks();
    read_clocks(d, d.priv_clocks ? &*d.priv_clocks : nullptr);
    read_temps(d);
    read_power(d);
    read_fan(d);
    read_util(d);
    read_throttle(d);
    read_pcie(d);
    read_misc(d);
    return d;
}

std::vector<std::uint32_t> Telemetry::supported_nvml_clocks(const char* function, std::optional<std::uint32_t> selector) {
    // Query the required count first, allow bounded growth, never trust a
    // returned count beyond the allocated buffer.
    Nvml& nv = gpu_.nvml;
    if (!nv.ok || !nv.has(function)) return {};
    using ListFn = int(DRUTA_CDECL*)(void*, std::uint32_t*, std::uint32_t*);
    using ListSelFn = int(DRUTA_CDECL*)(void*, std::uint32_t, std::uint32_t*, std::uint32_t*);
    auto call = [&](std::uint32_t* count, std::uint32_t* values) {
        if (selector) return nv.get<ListSelFn>(function)(nv.dev, *selector, count, values);
        return nv.get<ListFn>(function)(nv.dev, count, values);
    };
    constexpr std::uint32_t maximum = 4096;
    std::uint32_t count = 0;
    int status = call(&count, nullptr);
    if (status != 0 && status != 7) return {};  // NVML_SUCCESS / NVML_ERROR_INSUFFICIENT_SIZE
    std::uint32_t capacity = count;
    for (int attempt = 0; attempt < 3; ++attempt) {
        if (!(0 < capacity && capacity <= maximum)) return {};
        std::vector<std::uint32_t> values(capacity);
        count = capacity;
        status = call(&count, values.data());
        if (status == 7) {
            if (count <= capacity) return {};
            capacity = count;
            continue;
        }
        if (status != 0 || !(0 < count && count <= capacity)) return {};
        values.resize(count);
        if (std::any_of(values.begin(), values.end(), [](std::uint32_t v) { return v == 0; })) return {};
        std::sort(values.begin(), values.end());
        values.erase(std::unique(values.begin(), values.end()), values.end());
        return values;
    }
    return {};
}

StaticCapabilities Telemetry::read_static() {
    LockGuard guard(gpu_.lock());
    StaticCapabilities s;
    Nvml& nv = gpu_.nvml;
    if (nv.ok) {
        using ConstraintsFn = int(DRUTA_CDECL*)(void*, std::uint32_t*, std::uint32_t*);
        // Power metadata getters are optional and independent.
        if (auto fn = nv.get<ConstraintsFn>("nvmlDeviceGetPowerManagementLimitConstraints")) {
            std::uint32_t mn = 0, mx = 0;
            if (fn(nv.dev, &mn, &mx) == 0) {
                s.pl_min_mw = mn;
                s.pl_max_mw = mx;
            }
        }
        if (auto fn = nv.get<HandleU32Fn>("nvmlDeviceGetPowerManagementDefaultLimit")) {
            std::uint32_t d = 0;
            if (fn(nv.dev, &d) == 0) s.pl_def_mw = d;
        }
        auto memclks = supported_nvml_clocks("nvmlDeviceGetSupportedMemoryClocks", std::nullopt);
        if (!memclks.empty()) {
            s.mem_clocks = memclks;
            auto g = supported_nvml_clocks("nvmlDeviceGetSupportedGraphicsClocks", memclks.back());
            if (!g.empty()) {
                s.gfx_min = *std::min_element(g.begin(), g.end());
                s.gfx_max = *std::max_element(g.begin(), g.end());
            }
        }
        if (auto fn = nv.get<ConstraintsFn>("nvmlDeviceGetMinMaxFanSpeed")) {
            std::uint32_t mn = 0, mx = 0;
            if (fn(nv.dev, &mn, &mx) == 0) {
                s.fan_min = mn;
                s.fan_max = mx;
            }
        }
    }
    Nvapi& a = gpu_.nvapi;
    if (auto fn = a.ok ? a.get<HandleU32Fn>(id::RamType) : nullptr) {
        std::uint32_t v = 0;
        if (fn(a.gpu, &v) == 0) {
            s.mem_type_id = v;
            s.mem_type = "RAM type " + std::to_string(v);
            for (const MemType& type : kMemTypes) {
                if (type.id == v) {
                    s.mem_type = type.name;
                    s.mem_div = type.div;
                }
            }
        }
    }
    s.core_off_range = offset_range(0);
    s.mem_off_range = offset_range(2);
    if (!s.fan_min || !s.fan_max) {
        if (auto native = native_fan_data()) {
            std::uint32_t lo = 0, hi = 100;
            bool first = true;
            for (const NativeFan& fan : native->fans) {
                lo = first ? fan.min : std::max(lo, fan.min);
                hi = first ? fan.max : std::min(hi, fan.max);
                first = false;
            }
            s.fan_min = lo;
            s.fan_max = hi;
        }
    }
    return s;
}

}  // namespace druta::backend
