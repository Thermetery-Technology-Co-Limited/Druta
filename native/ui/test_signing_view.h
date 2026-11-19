// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "test_signing.h"
namespace druta::ui {
struct TestSigningViewState {bool open=false,busy=false,was_open=false;std::optional<TestSigningStatus> status;std::optional<TestSigningResult> result;};
enum class TestSigningViewIntent {refresh,enable,disable,reboot};
// First opening emits one read-only refresh; all machine changes need explicit clicks.
std::vector<TestSigningViewIntent> draw_test_signing(TestSigningViewState&);
}
