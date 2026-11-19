// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "i2c_driver.h"
#include "profile_io.h"
namespace druta::ui {
// Native compiled-recipe cache; deliberately distinct from Python TOML hashes.
// A hint is never an identity verdict, verified control, gate or saved request.
struct I2cRouteHint {
    std::string uuid;
    I2cFamily family=I2cFamily::unknown;
    I2cRoute route{};
    std::string recipe,contract;
};
struct I2cDiscoveryProgress {
    bool running=false,cancelled=false;
    uint32_t completed=0,total=0;
    std::string label;
    std::vector<I2cCandidate> candidates; // Read-only hits, always unverified.
};
std::string i2c_route_contract(I2cFamily family,const std::string& recipe);
bool valid_i2c_route_hint(const I2cRouteHint& hint);
std::optional<I2cRouteHint> decode_i2c_route_cache(const ProfileJson& document,const std::string& uuid);
ProfileJson encode_i2c_route_cache(const ProfileJson& document,const I2cRouteHint& hint);
// Missing file is no hint; malformed existing files throw and are preserved.
std::optional<I2cRouteHint> load_i2c_route_cache(const std::wstring& path,const std::string& uuid);
void remember_i2c_route(const std::wstring& path,const I2cRouteHint& hint);
}
