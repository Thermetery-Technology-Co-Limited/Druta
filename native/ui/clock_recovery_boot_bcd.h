// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <string>

namespace druta::ui {
// Each operation concerns one clock receipt. Errors include conflicts and unavailable BCD access.
bool clock_recovery_boot_supported();
void check_clock_recovery_boot_available(const std::string& receipt_id);
void arm_clock_recovery_boot(const std::string& receipt_id);
void disarm_clock_recovery_boot(const std::string& receipt_id);
bool clock_recovery_boot_is_armed(const std::string& receipt_id);

namespace clock_boot_test {
// Isolated store only. The test entry points reject an empty store path.
void arm_file(const std::wstring& file, const std::wstring& source_id, const std::string& receipt_id);
void disarm_file(const std::wstring& file, const std::string& receipt_id);
bool armed_file(const std::wstring& file, const std::string& receipt_id);
}
}
