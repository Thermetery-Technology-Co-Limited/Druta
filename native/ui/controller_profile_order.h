// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_catalog.h"
namespace druta::ui {
// Input is already flattened and Python tuning/full-tune controls normalized.
// Validate every original before partitioning mixed native documents so the
// host can order voltage requests before its controller and clock requests.
// This helper does not choose Apply order, perform I/O, or remove any target.
std::vector<ProfileComponent> split_controller_dependencies(const std::vector<ProfileComponent>&);
}
