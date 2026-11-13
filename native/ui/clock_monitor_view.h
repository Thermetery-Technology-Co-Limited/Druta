// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "session.h"
namespace druta::ui {
void draw_clock_secondary(const SessionSnapshot& snapshot,const char* key);
void draw_clock_monitor_table(const SessionSnapshot& snapshot,bool classic=false);
}
