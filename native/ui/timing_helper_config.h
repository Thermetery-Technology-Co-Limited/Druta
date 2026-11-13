// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <optional>
#include <string>
namespace druta::ui {
struct TimingHelperConfigPaths {std::wstring current,legacy;};
struct TimingHelperConfigRead {std::optional<std::wstring> registered_path;bool legacy=false;std::string message;};
TimingHelperConfigPaths timing_helper_config_paths();
// Read never creates files or launches a helper. It does not perform fallback search.
TimingHelperConfigRead read_timing_helper_config(const TimingHelperConfigPaths&);
// Explicit mutation only; throws on invalid helper, malformed existing config or I/O failure.
void set_timing_helper_config(const TimingHelperConfigPaths&,const std::wstring& helper);
void forget_timing_helper_config(const TimingHelperConfigPaths&);
}
