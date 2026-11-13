// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_io.h"
#include <string>
#include <vector>
namespace druta::ui {
// Pure structural selection; current adapter/transport qualification stays in
// the service's stage/preflight path. Empty UUID means the format did not save it.
struct ProfileSelection {std::string slot,uuid;std::vector<std::string> targets;};
}
