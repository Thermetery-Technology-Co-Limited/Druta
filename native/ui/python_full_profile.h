// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_catalog.h"
namespace druta::ui {
bool is_python_full_profile(const ProfileJson&);
ProfileDescription describe_python_full_profile(const ProfileJson&);
const ProfileJson& python_full_source(const ProfileJson&);
ProfileJson select_python_full_profile(const ProfileJson&,const std::vector<std::string>&);
}
