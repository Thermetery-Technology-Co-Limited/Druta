/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <cstdint>
#include <memory>
#include <string>
namespace druta::ui {
enum class TimingCommand { help, fields, dump, preview, commit };
struct TimingProcessResult { bool launched=false, timed_out=false; uint32_t exit_code=0; std::string output, error; };
// Typed operations only. A pin keeps the selected helper immutable across a
// preview/commit sequence; a transport must reject changes while pinned.
class TimingTransport {
public:
    virtual ~TimingTransport() = default;
    // True while a launched helper has not been confirmed terminated.
    virtual bool pending() const { return false; }
    virtual bool pin(std::string& error) = 0;
    virtual bool unchanged(std::string& error) = 0;
    virtual std::string token() const = 0;
    virtual void unpin() = 0;
    virtual TimingProcessResult run(TimingCommand op, const std::string& slot, const std::string& field,
                                    uint32_t value, bool commit_flag, bool preview_flag) = 0;
};
std::unique_ptr<TimingTransport> make_timing_transport(const std::wstring& absolute_exe);
}
