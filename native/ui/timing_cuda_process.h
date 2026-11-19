// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <memory>
#include <string>
#include <cstdint>
namespace druta::ui {
namespace timing_cuda_protocol {
constexpr uint32_t magic=0x44544355,version=1;
enum State : long {setup=1,copying=2,cleanup=3,done=4,failed=5};
// Unnamed mapping, inherited by exactly one explicitly spawned child. Only
// state is published interlocked; immutable header/slot are verified by both.
struct alignas(8) Frame {uint32_t signature=magic,size=sizeof(Frame),revision=version,duration_ms=0;uint64_t cookie=0;char slot[16]{};volatile long state=setup;uint32_t result_flags=0;char message[256]{};};
constexpr uint32_t clean=1,cancelled=2,timed_out=4;
}
struct TimingCudaProcessStatus {bool copying=false,finished=false,cleanup_pending=false,ok=false;std::string message;};
class TimingCudaProcess {
public:
    virtual ~TimingCudaProcess()=default;
    virtual bool start(const std::string& slot,unsigned max_duration_ms,std::string& error)=0;
    virtual TimingCudaProcessStatus poll()=0;
    virtual TimingCudaProcessStatus stop(unsigned grace_ms,unsigned terminate_wait_ms)=0;
    virtual bool pending()const=0;
};
std::unique_ptr<TimingCudaProcess> make_timing_cuda_process(const std::wstring& absolute_worker_exe);
// Child entry point; private inherited mapping/event handles are carried only
// by the parent's explicit handle list. Exposed for a fake-child test fixture.
int timing_cuda_worker_entry(int argc,wchar_t** argv,const struct CudaDriverApi* injected=nullptr);
}
