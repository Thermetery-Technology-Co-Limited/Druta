// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "session.h"
#include <string>
namespace druta::ui {
// classic: Python's measured line ("measured 307  Δ +82.1", MEM divided by the adapter
// memory divisor); returns the core subtitle token (" · load 10 %"). Focused: the B line.
std::string draw_clock_secondary(const SessionSnapshot& snapshot,const char* key,bool classic=false);
// classic: Python's bordered ALL CLOCK DOMAINS panel at classic_height (scrolling inside);
// returns the height its content needs, for the page budget. Focused: the collapsing table.
float draw_clock_monitor_table(const SessionSnapshot& snapshot,bool classic=false,float classic_height=0);
}
