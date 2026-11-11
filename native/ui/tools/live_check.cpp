/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
// Explicit, manual qualification only. The default path never issues a command.
#include "backend.h"
#include "profile_io.h"
#include <windows.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <map>
#include <limits>
#include <optional>
#include <string>
#include <utility>

namespace {
using namespace druta::ui;
std::string utf8(const wchar_t* value) {
    if (!value) return {};
    const int n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value, -1, nullptr, 0, nullptr, nullptr);
    if (!n) return {};
    std::string out(static_cast<size_t>(n), '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value, -1, &out[0], n, nullptr, nullptr)) return {};
    out.pop_back(); return out;
}
bool absolute_path(const std::wstring& p) {
    return p.size() >= 3 && ((p[1] == L':' && (p[2] == L'\\' || p[2] == L'/')) ||
        (p[0] == L'\\' && p[1] == L'\\'));
}
const char* availability(Availability a) {
    switch (a) {
    case Availability::available: return "available";
    case Availability::unsupported: return "unsupported";
    case Availability::transient: return "transient";
    default: return "unknown";
    }
}
void optional_value(const char* key, const std::optional<double>& v) {
    std::cout << key << '=';
    if (v) std::cout << std::setprecision(17) << *v; else std::cout << "unavailable";
    std::cout << '\n';
}
void print_data(const LiveData& d) {
    std::cout << "slot=" << d.identity.slot << "\nname=" << d.identity.name
              << "\nconnected=" << d.connected << "\nstatus=" << d.status
              << "\ntelemetry_valid=" << d.telemetry_valid << "\ntelemetry_present=0x"
              << std::hex << d.telemetry.present << std::dec << "\ntelemetry_error=" << d.telemetry_error << '\n';
    const char* labels[] = {"core", "memory", "power", "fan", "boost"};
    for (size_t i = 0; i < d.controls.size(); ++i) {
        const auto& c = d.controls[i];
        std::cout << labels[i] << ".availability=" << availability(c.availability)
                  << '\n' << labels[i] << ".writable=" << c.writable
                  << '\n' << labels[i] << ".unit=" << c.unit
                  << '\n' << labels[i] << ".detail=" << c.detail << '\n';
        if (i == static_cast<size_t>(ControlId::memory)) {
            optional_value("memory.current", c.current); optional_value("memory.low", c.low);
            optional_value("memory.high", c.high); optional_value("memory.step", c.step);
        }
    }
    std::cout << "curve.points=" << d.curve.size() << "\ncurve.error=" << d.curve_error
              << "\ncurve.freq_div=" << d.curve_freq_div << "\ncurve.step_khz=" << d.curve_step_khz
              << "\nlocks.clock.present=" << !!(d.telemetry.present & DC_T_CLK_LOCK)
              << "\nlocks.clock.min_mhz=" << d.telemetry.clk_lock_min_mhz
              << "\nlocks.clock.max_mhz=" << d.telemetry.clk_lock_max_mhz
              << "\nlocks.vf.present=" << !!(d.telemetry.present & DC_T_VF_LOCKED)
              << "\nlocks.vf.count=" << d.telemetry.vf_locked_count << '\n';
}
bool same_identity(const dc_gpu_identity& a, const dc_gpu_identity& b) {
    return a.slot[0] && b.slot[0] && std::strcmp(a.slot, b.slot) == 0 &&
        std::strcmp(a.uuid, b.uuid) == 0 && a.nvapi_selected == b.nvapi_selected &&
        a.nvml_selected == b.nvml_selected && a.nvapi_handle == b.nvapi_handle &&
        a.nvapi_devid == b.nvapi_devid && a.nvapi_subsys == b.nvapi_subsys &&
        a.nvml_has_devid == b.nvml_has_devid && a.nvml_devid == b.nvml_devid &&
        a.nvml_subsys == b.nvml_subsys && !a.pairing_error[0] && !b.pairing_error[0];
}
bool curve_map(const LiveData& data, std::map<uint32_t, int32_t>& out) {
    if (!data.curve_error.empty() || data.curve.empty() || data.curve.size() > DC_VFP_POINTS) return false;
    for (const auto& point : data.curve)
        if (!out.emplace(point.idx, point.delta_khz).second) return false;
    return true;
}
void print_result(const char* name, const ActionResult& result) {
    std::cout << name << ".ok=" << result.ok << '\n' << name << ".may_have_changed=" << result.may_have_changed
              << '\n' << name << ".message=" << result.message << '\n';
    const std::string requested = std::string(name) + ".requested";
    const std::string readback = std::string(name) + ".readback";
    optional_value(requested.c_str(), result.requested); optional_value(readback.c_str(), result.readback);
}
struct Close {
    IBackend* backend;
    ~Close() { try { if (backend) backend->close(); } catch (...) {} }
};
int roundtrip(IBackend& backend, const LiveData& initial) {
    LiveData before = backend.read(true); // fresh identity and range immediately before the command
    const auto& c = before.controls[static_cast<size_t>(ControlId::memory)];
    std::map<uint32_t, int32_t> original_curve;
    if (!before.connected || !before.telemetry_valid || !same_identity(initial.identity, before.identity) ||
        before.undo_available || c.availability != Availability::available || !c.writable ||
        !c.current || !c.low || !c.high || !c.step || !std::isfinite(*c.current) ||
        !std::isfinite(*c.low) || !std::isfinite(*c.high) || !std::isfinite(*c.step) ||
        *c.step <= 0 || *c.low > *c.current || *c.current > *c.high || !curve_map(before, original_curve)) {
        std::cout << "roundtrip.error=Fresh identity, writable range, step, or complete curve unavailable; no command issued\n";
        return 1;
    }
    // The step is expressed in the same units as current by IBackend. Choose the
    // nearest number of whole steps to one unit, at least one step, from current.
    const double steps = std::max(1.0, std::round(1.0 / *c.step));
    const double delta = steps * *c.step;
    if (!std::isfinite(delta)) { std::cout << "roundtrip.error=Invalid step\n"; return 1; }
    double target = *c.current + delta;
    if (!std::isfinite(target) || target > *c.high || target == *c.current) target = *c.current - delta;
    if (!std::isfinite(target) || target < *c.low || target == *c.current) {
        std::cout << "roundtrip.error=No bounded one-unit step fits; no command issued\n"; return 1;
    }
    std::cout << "roundtrip.before=" << std::setprecision(17) << *c.current
              << "\nroundtrip.target=" << target << "\nroundtrip.unit=" << c.unit << '\n';
    Command apply; apply.operation = Command::Operation::apply; apply.control = ControlId::memory; apply.value = target;
    ActionResult applied; bool apply_threw = false;
    try { applied = backend.execute(apply); }
    catch (...) { apply_threw = true; applied.may_have_changed = true; applied.message = "Apply threw; state is uncertain"; }
    print_result("apply", applied);
    LiveData after; bool read_after = false;
    try { after = backend.read(true); read_after = true; }
    catch (...) { std::cout << "apply_read.error=Read threw\n"; }
    const bool undo_needed = applied.may_have_changed || (read_after && after.undo_available);
    bool undo_ok = false;
    if (undo_needed) {
        Command undo; undo.operation = Command::Operation::undo; undo.control = ControlId::memory;
        try { auto result = backend.execute(undo); print_result("undo", result); undo_ok = result.ok; }
        catch (...) { std::cout << "undo.error=Undo threw\n"; }
    } else std::cout << "undo.attempted=0\n";
    LiveData restored; bool read_restored = false;
    try { restored = backend.read(true); read_restored = true; }
    catch (...) { std::cout << "restore_read.error=Read threw\n"; }
    std::map<uint32_t, int32_t> restored_curve;
    const auto& r = restored.controls[static_cast<size_t>(ControlId::memory)];
    const bool verified = read_restored && restored.connected && same_identity(before.identity, restored.identity) &&
        r.availability == Availability::available && r.current && *r.current == *c.current &&
        curve_map(restored, restored_curve) && restored_curve == original_curve;
    std::cout << "restore.verified=" << verified << '\n';
    const bool applied_verified = read_after && after.connected && same_identity(before.identity, after.identity) &&
        after.controls[static_cast<size_t>(ControlId::memory)].availability == Availability::available &&
        after.controls[static_cast<size_t>(ControlId::memory)].current &&
        *after.controls[static_cast<size_t>(ControlId::memory)].current == target;
    std::cout << "apply.verified=" << applied_verified << '\n';
    return !apply_threw && applied.ok && applied_verified && undo_needed && undo_ok && verified ? 0 : 1;
}
struct CurveRow { double voltage; std::int64_t base; std::int32_t delta; };
bool curve_signature(const LiveData& d, std::map<uint32_t, CurveRow>& out) {
    if (!d.curve_freq_div || !d.curve_error.empty() || d.curve.empty() || d.curve.size() > DC_VFP_POINTS) return false;
    for (const auto& p : d.curve) {
        const double khz = p.freq_mhz * 1000.0 * d.curve_freq_div;
        if (!std::isfinite(p.volt_mv) || !std::isfinite(khz) || khz < 0 || khz > UINT32_MAX ||
            std::abs(khz - std::round(khz)) > 0.000001) return false;
        const auto base = static_cast<std::int64_t>(std::llround(khz)) - p.delta_khz;
        if (!out.emplace(p.idx, CurveRow{p.volt_mv, base, p.delta_khz}).second) return false;
    }
    return true;
}
bool same_curve(const LiveData& before, const LiveData& after, std::optional<std::pair<uint32_t, int32_t>> edit = {}) {
    std::map<uint32_t, CurveRow> a, b;
    if (before.curve_freq_div != after.curve_freq_div || !curve_signature(before, a) ||
        !curve_signature(after, b) || a.size() != b.size()) return false;
    for (const auto& entry : a) {
        const auto it = b.find(entry.first);
        const auto expected = edit && edit->first == entry.first ? edit->second : entry.second.delta;
        if (it == b.end() || it->second.voltage != entry.second.voltage ||
            it->second.base != entry.second.base || it->second.delta != expected) return false;
    }
    return true;
}
// Execute the explicitly selected operation once; perform no additional SET after
// the single Undo even if either result is uncertain or readback fails.
int checked_action(IBackend& backend, const LiveData& before, const Command& command,
                   const std::optional<std::pair<uint32_t, int32_t>>& edit,
                   const std::optional<double>& memory_target) {
    ActionResult applied; bool threw = false;
    try { applied = backend.execute(command); }
    catch (...) { threw = true; applied.may_have_changed = true; applied.message = "Action threw; state is uncertain"; }
    print_result("apply", applied);
    LiveData after; bool got_after = false;
    try { after = backend.read(true); got_after = true; } catch (...) { std::cout << "apply_read.error=Read threw\n"; }
    const auto mem = static_cast<size_t>(ControlId::memory);
    const bool apply_verified = got_after && after.connected && same_identity(before.identity, after.identity) &&
        same_curve(before, after, edit) && (!memory_target ||
        (after.controls[mem].availability == Availability::available && after.controls[mem].current &&
         *after.controls[mem].current == *memory_target));
    std::cout << "apply.verified=" << apply_verified << '\n';
    const bool undo_needed = applied.may_have_changed || (got_after && after.undo_available);
    bool undo_ok = false;
    if (undo_needed) {
        Command undo; undo.operation = Command::Operation::undo;
        try { const auto result = backend.execute(undo); print_result("undo", result); undo_ok = result.ok; }
        catch (...) { std::cout << "undo.error=Undo threw\n"; }
    } else std::cout << "undo.attempted=0\n";
    LiveData restored; bool got_restored = false;
    try { restored = backend.read(true); got_restored = true; } catch (...) { std::cout << "restore_read.error=Read threw\n"; }
    if (edit && got_restored) {
        auto it = std::find_if(restored.curve.begin(), restored.curve.end(), [&](const auto& p) { return p.idx == edit->first; });
        if (it != restored.curve.end()) std::cout << "curve.restored_raw_khz=" << it->delta_khz << '\n';
    }
    if (memory_target && got_restored) optional_value("memory.restored", restored.controls[mem].current);
    const bool memory_restored = !memory_target || (restored.controls[mem].availability == Availability::available &&
        restored.controls[mem].current && before.controls[mem].current &&
        *restored.controls[mem].current == *before.controls[mem].current);
    const bool verified = got_restored && restored.connected && same_identity(before.identity, restored.identity) &&
        memory_restored && same_curve(before, restored);
    std::cout << "restore.verified=" << verified << '\n';
    return !threw && applied.ok && apply_verified && undo_needed && undo_ok && verified ? 0 : 1;
}
int curve_roundtrip(IBackend& backend, const LiveData& initial) {
    const auto before = backend.read(true);
    std::map<uint32_t, CurveRow> geometry;
    if (!before.connected || !before.telemetry_valid || !same_identity(initial.identity, before.identity) ||
        before.undo_available || !before.curve_writable || !before.curve_step_khz ||
        !curve_signature(before, geometry)) {
        std::cout << "roundtrip.error=Fresh writable curve, identity, divisor, or step unavailable\n"; return 1;
    }
    const auto point = std::min_element(before.curve.begin(), before.curve.end(), [](const auto& a, const auto& b) {
        return a.volt_mv < b.volt_mv || (a.volt_mv == b.volt_mv && a.idx < b.idx);
    });
    if (static_cast<std::uint64_t>(before.curve_step_khz) * before.curve_freq_div > INT32_MAX) {
        std::cout << "roundtrip.error=Reported step is too large\n"; return 1;
    }
    const std::int64_t raw_step = static_cast<std::int64_t>(before.curve_step_khz) * before.curve_freq_div;
    const std::int64_t target = static_cast<std::int64_t>(point->delta_khz) - raw_step;
    if (raw_step <= 0 || target < std::numeric_limits<int32_t>::min() || target > std::numeric_limits<int32_t>::max()) {
        std::cout << "roundtrip.error=Reported step cannot be represented\n"; return 1;
    }
    const auto value = static_cast<int32_t>(target);
    std::cout << "curve.point=" << point->idx << "\ncurve.before_raw_khz=" << point->delta_khz
              << "\ncurve.requested_raw_khz=" << value << "\ncurve.step_physical_khz=" << before.curve_step_khz << '\n';
    Command cmd; cmd.operation = Command::Operation::apply_curve; cmd.expected_curve = before.curve;
    cmd.expected_curve_freq_div = before.curve_freq_div; cmd.curve_deltas.push_back({point->idx, value});
    return checked_action(backend, before, cmd, std::make_pair(point->idx, value), {});
}
int control_roundtrip(IBackend& backend, const LiveData& initial, ControlId control) {
    const auto before = backend.read(true);
    const auto index = static_cast<size_t>(control);
    const auto& c = before.controls[index];
    std::map<uint32_t, CurveRow> geometry;
    if (!before.connected || !before.telemetry_valid || !same_identity(initial.identity, before.identity) ||
        before.undo_available || c.availability != Availability::available || !c.writable ||
        !c.current || !c.low || !c.high || !std::isfinite(*c.current) || !std::isfinite(*c.low) ||
        !std::isfinite(*c.high) || *c.current < *c.low || *c.current > *c.high || !curve_signature(before, geometry)) {
        std::cout << "control.error=Fresh writable request, range, identity, or full curve unavailable\n"; return 1;
    }
    double target = *c.current;
    if (control == ControlId::core) {
        if (!before.curve_step_khz || std::trunc(*c.current) != *c.current) {
            std::cout << "control.error=Whole core request or reported clock step unavailable\n"; return 1;
        }
        const double step = before.curve_step_khz / 1000.0;
        auto encoded = [step](double value) { return std::ceil(std::nearbyint(std::trunc(value) / step) * step); };
        if (encoded(*c.current) != *c.current) {
            std::cout << "control.error=Prior core request cannot be restored exactly\n"; return 1;
        }
        target = std::ceil((std::nearbyint(*c.current / step) + 1.0) * step);
        if (encoded(target) != target) {
            std::cout << "control.error=Next core clock bin is not exactly representable\n"; return 1;
        }
        std::cout << "control.step_physical_khz=" << before.curve_step_khz << '\n';
    } else {
        if ((control == ControlId::fan || control == ControlId::boost) && std::trunc(*c.current) != *c.current) {
            std::cout << "control.error=Whole percentage request unavailable\n"; return 1;
        }
        target += control == ControlId::power ? -1.0 : 1.0;
    }
    if (!std::isfinite(target) || target < *c.low || target > *c.high || target == *c.current) {
        std::cout << "control.error=Bounded requested change does not fit\n"; return 1;
    }
    std::cout << "control.before=" << std::setprecision(17) << *c.current << "\ncontrol.requested=" << target
              << "\ncontrol.unit=" << c.unit << '\n';
    if (control == ControlId::power) std::cout << "control.verification_scope=board request and V/F curve; peer policies verified by backend Undo\n";
    if (control == ControlId::fan) std::cout << "control.verification_scope=reported level and V/F curve; backend Undo restores captured fan policies\n";
    Command command; command.operation = Command::Operation::apply; command.control = control; command.value = target;
    ActionResult applied; bool threw = false;
    try { applied = backend.execute(command); }
    catch (...) { threw = true; applied.may_have_changed = true; applied.message = "Action threw; state is uncertain"; }
    print_result("apply", applied);
    LiveData after; bool got_after = false;
    try { after = backend.read(true); got_after = true; } catch (...) { std::cout << "apply_read.error=Read threw\n"; }
    if (got_after) optional_value("control.readback", after.controls[index].current);
    // A core offset legitimately changes point deltas, so only its configured
    // offset is checked here; the complete original curve is checked after Undo.
    const bool applied_verified = got_after && after.connected && same_identity(before.identity, after.identity) &&
        after.controls[index].availability == Availability::available && after.controls[index].current &&
        *after.controls[index].current == target && (control == ControlId::core || same_curve(before, after));
    std::cout << "apply.verified=" << applied_verified << '\n';
    const bool undo_needed = applied.may_have_changed || (got_after && after.undo_available);
    bool undo_ok = false;
    if (undo_needed) {
        Command undo; undo.operation = Command::Operation::undo;
        try { const auto result = backend.execute(undo); print_result("undo", result); undo_ok = result.ok; }
        catch (...) { std::cout << "undo.error=Undo threw\n"; }
    } else std::cout << "undo.attempted=0\n";
    LiveData restored; bool got_restored = false;
    try { restored = backend.read(true); got_restored = true; } catch (...) { std::cout << "restore_read.error=Read threw\n"; }
    if (got_restored) optional_value("control.restored", restored.controls[index].current);
    const bool verified = got_restored && restored.connected && same_identity(before.identity, restored.identity) &&
        restored.controls[index].availability == Availability::available && restored.controls[index].current &&
        *restored.controls[index].current == *c.current && same_curve(before, restored);
    std::cout << "restore.verified=" << verified << '\n';
    return !threw && applied.ok && applied_verified && undo_needed && undo_ok && verified ? 0 : 1;
}int profile_roundtrip(IBackend& backend, const LiveData& initial, const std::wstring& path) {
    const auto before = backend.read(true);
    const auto& c = before.controls[static_cast<size_t>(ControlId::memory)];
    std::map<uint32_t, CurveRow> geometry;
    if (!before.connected || !before.telemetry_valid || !same_identity(initial.identity, before.identity) ||
        before.undo_available || c.availability != Availability::available || !c.writable ||
        !c.current || !c.low || !c.high || !c.step || !std::isfinite(*c.current) ||
        !std::isfinite(*c.low) || !std::isfinite(*c.high) || !std::isfinite(*c.step) ||
        *c.step <= 0 || *c.current < *c.low || *c.current + 1 > *c.high || !curve_signature(before, geometry)) {
        std::cout << "roundtrip.error=Fresh memory range, curve, or identity unavailable\n"; return 1;
    }
    if (GetFileAttributesW(path.c_str()) != INVALID_FILE_ATTRIBUTES || GetLastError() != ERROR_FILE_NOT_FOUND) {
        std::cout << "profile.error=Evidence path must name a new file\n"; return 1;
    }
    Command save; save.operation = Command::Operation::save_profile; save.profile_path = path;
    const auto saved = backend.execute(save); print_result("profile.save", saved);
    if (!saved.ok) return 1;
    auto doc = read_profile(path);
    if (!doc.is_object() || !doc.contains("format") || doc["format"] != "druta-native-daily" ||
        !doc.contains("version") || doc["version"] != 1 || !doc.contains("controls") ||
        !doc["controls"].is_object() || !doc["controls"].contains("memory")) {
        std::cout << "profile.error=Saved memory settings unavailable\n"; return 1;
    }
    auto memory = doc["controls"]["memory"];
    if (!memory.is_object() || !memory.contains("value") || !memory["value"].is_number_integer() ||
        !memory.contains("scale") || !memory["scale"].is_number_unsigned() || !memory.contains("unit") ||
        !memory["unit"].is_string() || memory["unit"].get<std::string>() != c.unit) {
        std::cout << "profile.error=Invalid memory profile shape\n"; return 1;
    }
    const auto scale64 = memory["scale"].get<uint64_t>();
    if (!scale64 || scale64 > UINT32_MAX) { std::cout << "profile.error=Invalid memory scale\n"; return 1; }
    const auto scale = static_cast<uint32_t>(scale64);
    const auto current = memory["value"].get<int64_t>();
    const double step_raw = *c.step * scale;
    if (!scale || current < INT32_MIN || current > INT32_MAX ||
        static_cast<double>(current) != *c.current * scale || !std::isfinite(step_raw) ||
        step_raw < 1 || step_raw > UINT32_MAX || std::trunc(step_raw) != step_raw ||
        current + scale > INT32_MAX || (current + scale) % static_cast<int64_t>(step_raw) != 0) {
        std::cout << "profile.error=Memory value or one-MHz step cannot be represented safely\n"; return 1;
    }
    memory["value"] = current + scale;
    doc["controls"] = ProfileJson::object(); doc["controls"]["memory"] = memory;
    write_profile(path, doc);
    std::cout << "profile.before_raw=" << current << "\nprofile.requested_raw=" << current + scale
              << "\nprofile.scale=" << scale << '\n';
    Command load; load.operation = Command::Operation::load_profile; load.profile_path = path;
    return checked_action(backend, before, load, {}, *c.current + 1);
}
}
int wmain(int argc, wchar_t** argv) {
    SetConsoleOutputCP(CP_UTF8);
    std::wstring core, gpu, profile_path, control_name; enum class Mode { read, memory, curve, profile, control } mode = Mode::read;
    const auto usage = [] { std::cerr << "usage: live_check --core ABSOLUTE_DLL --gpu SLOT [--memory-roundtrip | --curve-roundtrip | --profile-roundtrip --profile-path ABSOLUTE_FILE | --control-roundtrip core|power|fan|boost]\n"; };
    for (int i = 1; i < argc; ++i) {
        const std::wstring arg(argv[i]);
        if (arg == L"--core" && i + 1 < argc && core.empty()) core = argv[++i];
        else if (arg == L"--gpu" && i + 1 < argc && gpu.empty()) gpu = argv[++i];
        else if (arg == L"--profile-path" && i + 1 < argc && profile_path.empty()) profile_path = argv[++i];
        else if (arg == L"--memory-roundtrip" && mode == Mode::read) mode = Mode::memory;
        else if (arg == L"--curve-roundtrip" && mode == Mode::read) mode = Mode::curve;
        else if (arg == L"--profile-roundtrip" && mode == Mode::read) mode = Mode::profile;
        else if (arg == L"--control-roundtrip" && mode == Mode::read && i + 1 < argc) { mode = Mode::control; control_name = argv[++i]; }
        else { usage(); return 1; }
    }
    if (!absolute_path(core) || gpu.empty() ||
        (mode == Mode::profile ? !absolute_path(profile_path) : !profile_path.empty()) ||
        (mode == Mode::control && control_name != L"core" && control_name != L"power" && control_name != L"fan" && control_name != L"boost")) { usage(); return 1; }
    try {
        const std::string slot = utf8(gpu.c_str());
        if (slot.empty()) { std::cerr << "Invalid GPU slot encoding\n"; return 1; }
        auto backend = create_core_backend(core); if (!backend) { std::cerr << "Backend unavailable\n"; return 1; }
        Close close{backend.get()};
        const auto devices = backend->enumerate();
        std::cout << "devices.count=" << devices.size() << '\n';
        for (size_t i = 0; i < devices.size(); ++i)
            std::cout << "device." << i << ".slot=" << devices[i].slot << "\ndevice." << i << ".name=" << devices[i].name << '\n';
        std::string error;
        if (!backend->open(slot, error)) { std::cerr << "open.error=" << error << '\n'; return 1; }
        LiveData initial = backend->read(true); print_data(initial);
        if (!initial.connected || !initial.telemetry_valid) return 1;
        if (mode == Mode::memory) return roundtrip(*backend, initial);
        if (mode == Mode::curve) return curve_roundtrip(*backend, initial);
        if (mode == Mode::profile) return profile_roundtrip(*backend, initial, profile_path);
        if (mode == Mode::control) {
            const auto id = control_name == L"core" ? ControlId::core : control_name == L"power" ? ControlId::power :
                control_name == L"fan" ? ControlId::fan : ControlId::boost;
            return control_roundtrip(*backend, initial, id);
        }
        return 0;
    } catch (...) { std::cerr << "live_check.error=Unexpected backend failure\n"; return 1; }
}
