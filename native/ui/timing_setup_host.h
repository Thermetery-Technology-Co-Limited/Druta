// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <memory>
#include <functional>
#include "timings_view.h"
#include "test_signing_view.h"
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
namespace druta::ui {
class Session;struct ViewState;struct SessionSnapshot;
enum class TimingSetupActionState {waiting,accepted,rejected,stale,superseded};
TimingSetupActionState timing_setup_action_state(const SessionSnapshot&,uint64_t generation,uint64_t completed,Command::Operation);
struct TimingSetupApi {
 std::function<TimingsHelperSuggestion()> discover;
 std::function<void(const std::wstring&)> persist;
 std::function<void()> forget;
 std::function<TestSigningStatus()> probe;
 std::function<TestSigningResult(bool)> execute;
 std::function<std::string()> reboot;
};
class TimingSetupHost {
public:
 explicit TimingSetupHost(TimingSetupApi api={});~TimingSetupHost();TimingSetupHost(const TimingSetupHost&)=delete;TimingSetupHost& operator=(const TimingSetupHost&)=delete;
 void consume(Session&,ViewState&,HWND);
 // Explicit chosen path; validation still happens on the Session worker.
 void request_helper(std::wstring path);
 void request_signing(TestSigningViewIntent);
 void draw();
 bool pending() const;
private:struct Impl;std::unique_ptr<Impl> impl_;
};
}
