// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "drutacore/voltage_peers.h"
#include <functional>
#include <string>
namespace druta::backend {
// Injectable one-round acquisition; driver-specific guarded getter logic lives
// in ABI adapter. This layer checks retained bytes only; curve is diagnostic.
bool voltage_peers_stable(const dc_voltage_peers& first,const dc_voltage_peers& second,std::string& error);
}
