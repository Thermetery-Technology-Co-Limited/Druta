/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
// Manual, explicit live qualification. No command is issued on startup.
#include "backend.h"
#include "timing_model.h"
#include <windows.h>
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
#include <string>

namespace {
using namespace druta::ui;
bool absolute(const std::wstring& p) { return p.size() >= 3 && ((p[1] == L':' && (p[2] == L'\\' || p[2] == L'/')) || (p[0] == L'\\' && p[1] == L'\\')); }
std::string narrow(const wchar_t* value) {
    int n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value, -1, nullptr, 0, nullptr, nullptr);
    if (!n) return {}; std::string out(static_cast<size_t>(n), '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value, -1, &out[0], n, nullptr, nullptr)) return {};
    out.pop_back(); return out;
}
std::wstring widen(const std::string& value) {
    if (value.empty() || value.size() > INT_MAX) return {};
    int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0);
    if (!n) return {}; std::wstring out(static_cast<size_t>(n), L'\0');
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), &out[0], n)) return {};
    return out;
}
void number(const char* key, const std::optional<double>& n) { std::cout << ' ' << key << '='; if (n) std::cout << *n; else std::cout << "unavailable"; }
void result(const ActionResult& r) { std::cout << "ok=" << r.ok << " may_have_changed=" << r.may_have_changed; number("requested", r.requested); number("readback", r.readback); std::cout << "\n" << r.message << '\n'; }
void display(const LiveData& d) {
    std::cout << std::setprecision(12) << "slot=" << d.identity.slot << " name=" << d.identity.name << " connected=" << d.connected << " status=" << d.status
              << "\nxoc_known=" << d.xoc_known << " xoc_enabled=" << d.xoc_enabled << " rail_mode=" << d.rail_mode_enabled << "\n";
    const char* names[] = {"core", "memory", "board_power", "fan", "boost"};
    for (size_t i = 0; i < d.controls.size(); ++i) { const auto& s = d.controls[i]; std::cout << names[i] << " unit=" << s.unit << " availability=" << static_cast<int>(s.availability) << " writable=" << s.writable; number("request", s.current); number("low", s.low); number("high", s.high); number("stock", s.stock); std::cout << " detail=" << s.detail << '\n'; }
    number("enforced_board_power_w", d.enforced_power_w); std::cout << '\n';
    for (const auto& p : d.power_policies) { std::cout << "policy id=" << p.id << " label=" << p.label << " unit=" << p.state.unit << " writable=" << p.state.writable; number("request", p.state.current); number("low", p.state.low); number("high", p.state.high); std::cout << " detail=" << p.state.detail << '\n'; }
    for (const auto& r : d.rail_controls) { std::cout << "rail=" << r.id.rail << " field=" << r.id.field << " unit=" << r.state.unit << " writable=" << r.state.writable; number("requested_delta", r.state.current); number("low", r.state.low); number("high", r.state.high); number("estimated_base_mv", r.estimated_base_mv); std::cout << " detail=" << r.state.detail << '\n'; }
    std::cout << "hold owned=" << d.tuning.owned << " confirmed=" << d.tuning.confirmed << " recovery=" << d.tuning.recovery << " headroom=" << d.tuning.headroom << " domain=" << d.tuning.domain << " request_uv=" << d.tuning.voltage_uv << "\ncurve divisor=" << d.curve_freq_div << " points=" << d.curve.size() << " error=" << d.curve_error << '\n';
    for (const auto& p : d.curve) std::cout << "point index=" << p.idx << " voltage_mv=" << p.volt_mv << " frequency_mhz=" << p.freq_mhz << " delta_raw_khz=" << p.delta_khz << '\n';
    std::cout << "telemetry_valid=" << d.telemetry_valid << " present=0x" << std::hex << d.telemetry.present << std::dec << " core_mhz=" << d.telemetry.core << " mem_mhz=" << d.telemetry.mem << " vcore_mv=" << d.telemetry.vcore_mv << " board_power_w=" << d.telemetry.power_w << "\n";
    std::cout << "i2c available=" << d.i2c.available << " gate=" << d.i2c.gate << " verified=" << d.i2c.verified << " clock_owned=" << d.i2c.clock_owned << " message=" << d.i2c.message << '\n';
    for (size_t i = 0; i < d.i2c.candidates.size(); ++i) { const auto& c = d.i2c.candidates[i]; std::cout << "candidate=" << i << " port=" << static_cast<unsigned>(c.route.port) << " addr7=" << static_cast<unsigned>(c.route.addr7) << " controller_vout_mv=" << c.controller_vout_mv << " " << c.description << '\n'; }
    number("i2c_nominal_request_mv", d.i2c.status.requested_mv); number("i2c_controller_vout_mv", d.i2c.status.controller_vout_mv); std::cout << '\n';
    if (d.timings) { std::cout << "timings=" << d.timings->message << " snapshot=" << d.timings->snapshot.message << '\n'; for (const auto& f : d.timings->snapshot.fields) std::cout << "timing field=" << f.name << " reg=" << f.reg << " max=" << f.maximum << '\n'; if (d.timings->preview) { const auto& p = *d.timings->preview; std::cout << "preview ok=" << p.ok << " field=" << p.field << " prior=" << p.prior << " target=" << p.value << " message=" << p.message << '\n'; } }
    std::cout << "undo_available=" << d.undo_available << " " << d.undo_label << '\n';
}
}
int wmain(int argc, wchar_t** argv) {
    if (argc != 3 || !absolute(argv[1])) { std::cerr << "usage: actions_live_check ABSOLUTE_DLL SLOT\n"; return 2; }
    auto backend = druta::ui::create_core_backend(argv[1]); std::string why; if (!backend || !backend->open(narrow(argv[2]), why)) { std::cerr << "open failed: " << why << '\n'; return 1; }
    std::cout << "Manual actions: read, power W, xoc 0|1, rails 0|1, max CAP FLOOR, reset, undo, hold INDEX [0|1], release, maintain, helper ABS_PATH, timings-read, preview FIELD VALUE, timings-undo, scan, select INDEX, i2c-read, i2c-verify, i2c-release, quit\n";
    std::string line;
    for (;;) {
        std::cout << "> " << std::flush; const bool eof = !std::getline(std::cin, line);
        if (eof) line = "quit";
        try {
            std::istringstream in(line); std::string op; in >> op; if (op.empty()) continue;
            if (op == "quit") { auto r = backend->prepare_close(); result(r); if (r.ok) { backend->close(); return 0; } std::cout << "Cleanup failed; session remains open.\n"; if (eof) { std::cin.clear(); Sleep(1000); } continue; }
            if (op == "read") { display(backend->read(true)); continue; }
            if (op == "maintain") { result(backend->maintain()); continue; }
            Command cmd{}; bool valid = true;
            if (op == "power") { cmd.operation = Command::Operation::apply; cmd.control = ControlId::power; valid = !!(in >> cmd.value); }
            else if (op == "xoc" || op == "rails") { int flag = -1; valid = !!(in >> flag) && (flag == 0 || flag == 1); cmd.enabled = flag == 1; cmd.operation = op == "xoc" ? Command::Operation::set_xoc : Command::Operation::set_rail_mode; }
            else if (op == "max" || op == "hold") { auto d = backend->read(true); cmd.expected_curve = d.curve; cmd.expected_curve_freq_div = d.curve_freq_div; if (op == "max") { cmd.operation = Command::Operation::max_it; valid = !!(in >> cmd.curve_cap_mv >> cmd.curve_floor_mv); } else { cmd.operation = Command::Operation::hold_curve_point; int headroom = 1; valid = !!(in >> cmd.hold_point_index); std::string option; if (in >> option) { valid = valid && (option == "0" || option == "1"); headroom = option == "1"; } cmd.hold_headroom = headroom == 1; } }
            else if (op == "reset") cmd.operation = Command::Operation::reset_controls;
            else if (op == "undo") cmd.operation = Command::Operation::undo;
            else if (op == "release") cmd.operation = Command::Operation::release_hold;
            else if (op == "helper") { std::string path; std::getline(in >> std::ws, path); cmd.timing_helper_path = widen(path); valid = absolute(cmd.timing_helper_path); cmd.operation = Command::Operation::timings_select_helper; }
            else if (op == "timings-read") cmd.operation = Command::Operation::timings_read;
            else if (op == "preview") { cmd.operation = Command::Operation::timings_preview; valid = !!(in >> cmd.timing_field >> cmd.timing_value); }
            else if (op == "timings-undo") cmd.operation = Command::Operation::timings_undo;
            else if (op == "scan") cmd.operation = Command::Operation::i2c_scan;
            else if (op == "select") { size_t i = 0; auto d = backend->read(false); valid = !!(in >> i) && i < d.i2c.candidates.size(); if (valid) cmd.i2c_route = d.i2c.candidates[i].route; cmd.operation = Command::Operation::i2c_select; }
            else if (op == "i2c-read") cmd.operation = Command::Operation::i2c_read;
            else if (op == "i2c-verify") { cmd.operation = Command::Operation::i2c_verify; cmd.enabled = true; }
            else if (op == "i2c-release") cmd.operation = Command::Operation::i2c_release;
            else valid = false;
            if (!valid) { std::cout << "Invalid command or argument; no action issued.\n"; continue; }
            result(backend->execute(cmd));
        } catch (const std::exception& e) { std::cout << "Action raised: " << e.what() << "; inspect ownership with read before continuing.\n"; }
    }
}
