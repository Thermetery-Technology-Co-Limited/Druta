// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "../shunt_model.h"
#include <windows.h>
#include <cmath>
#include <cstdio>
#include <limits>
#include <stdexcept>
using namespace druta::ui;
namespace {
int failures = 0;
void check(bool ok, const char* what) { if (!ok) { ++failures; std::printf("FAIL: %s\n", what); } }
bool close_enough(double a, double b) { return std::abs(a - b) < 1e-9; }
}
int wmain(int argc, wchar_t** argv) {
    if (argc != 2) return 2;
    auto rails = shunt_default_rails();
    auto initial = shunt_correction(rails);
    check(initial.valid && initial.exact && !initial.active && close_enough(initial.factor, 1), "template unchanged");
    for (auto& r : rails) r.mod = 2.5;
    auto uniform = shunt_correction(rails);
    check(uniform.valid && uniform.exact && uniform.active && close_enough(*uniform.apply(100), 200), "uniform correction");
    rails[0].mod = 5;
    auto mixed = shunt_correction(rails);
    check(mixed.valid && !mixed.exact && close_enough(mixed.factor, (66.0 + 300.0 + 300.0) / 366.0), "mixed weighted estimate");
    rails[0].mod = 0;
    check(!shunt_correction(rails).valid && !shunt_correction(rails).apply(100), "zero invalid");
    rails[0].mod = std::numeric_limits<double>::quiet_NaN();
    check(!shunt_correction(rails).valid, "nan invalid");
    check(!shunt_correction({}).valid && !uniform.apply(std::numeric_limits<double>::infinity()), "empty and invalid telemetry");
    check(shunt_parallel_resistance({5, 5}) && close_enough(*shunt_parallel_resistance({5, 5}), 2.5), "parallel");
    check(!shunt_parallel_resistance({5, 0}) && !shunt_parallel_resistance({}), "invalid parallel");
    const std::wstring dir = std::wstring(argv[1]) + L"\\shunt-test-" + std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(GetTickCount());
    if (!CreateDirectoryW(dir.c_str(), nullptr)) return 2;
    const auto path = dir + L"\\shuntmod.json";
    try {
        const auto nested = dir + L"\\nested\\child\\shuntmod.json";
        auto fresh = ShuntConfig::load(nested);
        fresh.save();
        check(read_profile(nested)["rails"].size() == 3, "save creates nested parent directories");
        DeleteFileW(nested.c_str());
        RemoveDirectoryW((dir + L"\\nested\\child").c_str());
        RemoveDirectoryW((dir + L"\\nested").c_str());
        std::wstring invalid = path + std::wstring(1, L'\0') + L"suffix";
        auto invalid_config = ShuntConfig::load(invalid);
        bool invalid_rejected = false;
        try { invalid_config.save(); } catch (const std::exception&) { invalid_rejected = true; }
        check(!invalid_config.load_error.empty() && invalid_rejected, "invalid path fails without overwrite");
        ProfileJson original = {{"future", {{"x", 7}}}, {"rails", ProfileJson::array({
            {{"kind", "slot"}, {"orig", "5"}, {"mod", 2.5}, {"future", "keep"}},
            {{"kind", "future-connector"}, {"orig", 9}, {"mod", 8}, {"extra", 3}}
        })}};
        write_profile(path, original);
        auto config = ShuntConfig::load(path);
        check(config.load_error.empty() && config.rails.size() == 2 && close_enough(config.rails[0].orig, 5), "Python numeric strings load");
        check(!shunt_correction(config.rails).valid, "unknown rail cannot claim exactness");
        config.rails[0].mod = 4;
        config.save();
        auto saved = read_profile(path);
        check(saved["future"] == original["future"] && saved["rails"][0]["future"] == "keep" && saved["rails"][1] == original["rails"][1], "unknown fields preserved");
        check(saved["rails"][0]["mod"] == 4, "edited rail saved");
        saved["rails"][0].erase("orig");
        write_profile(path, saved);
        auto partial = ShuntConfig::load(path);
        check(!shunt_correction(partial.rails).valid, "partial rail invalid");
        bool rejected = false;
        try { partial.save(); } catch (const std::exception&) { rejected = true; }
        check(rejected && read_profile(path) == saved, "invalid save preserves destination");
        write_profile(path, {{"rails", "bad"}, {"future", 42}});
        auto bad = ShuntConfig::load(path);
        rejected = false;
        try { bad.save(); } catch (const std::exception&) { rejected = true; }
        check(!bad.load_error.empty() && rejected && read_profile(path)["future"] == 42, "unreadable schema preserves destination");
    } catch (const std::exception& e) { std::printf("FAIL: exception %s\n", e.what()); ++failures; }
    DeleteFileW(path.c_str());
    RemoveDirectoryW(dir.c_str());
    return failures ? 1 : 0;
}
