/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef _CRT_SECURE_NO_WARNINGS
#define _CRT_SECURE_NO_WARNINGS
#endif
#include "i2c_warmup.h"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <mutex>
#include <thread>
#include <utility>
#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif
namespace druta::ui {
namespace {
using Clock=std::chrono::steady_clock;
constexpr size_t mib=size_t(1024)*1024;
struct Slot { unsigned domain,bus,device,function; };
std::optional<Slot> parse(const std::string& text) {
    Slot s{}; int n=0;
    if(std::sscanf(text.c_str(),"%x:%x:%x.%x%n",&s.domain,&s.bus,&s.device,&s.function,&n)!=4 ||
       n!=static_cast<int>(text.size()) || s.domain>0xffff || s.bus>0xff || s.device>0x1f || s.function>7) return {};
    return s;
}
bool same(Slot a,Slot b) { return a.domain==b.domain && a.bus==b.bus && a.device==b.device && a.function==b.function; }

}
I2cWarmupResult i2c_cuda_warmup(const std::string& slot,const I2cWarmupCallbacks& callbacks,
    const I2cCudaApi* injected,unsigned settle_timeout_ms,unsigned max_duration_ms,unsigned poll_ms) {
    I2cWarmupResult result; auto target=parse(slot);
    if(!target || !callbacks.bound || !callbacks.sample || !callbacks.voltage_mv || !callbacks.bound() ||
       !settle_timeout_ms || !max_duration_ms || !poll_ms) {result.message="CUDA warmup requires a bound selected GPU, valid PCI slot, and telemetry";return result;}
    I2cCudaApi cuda{};
#ifdef _WIN32
    CudaDriverLibrary library; std::string load_error;
    if(injected) cuda=*injected;
    else if(!library.open(cuda,load_error)) {result.message="CUDA driver or required entry points unavailable; run a supported workload before verification";return result;}
#else
    if(injected) cuda=*injected; else {result.message="CUDA driver loading is unavailable";return result;}
#endif
    if(!complete_cuda_driver(cuda)) {result.message="CUDA driver entry points unavailable";return result;}
    const auto started_at=Clock::now(), hard_deadline=started_at+std::chrono::milliseconds(max_duration_ms);
    std::atomic<bool> stop{false},started{false},ended{false}; std::mutex error_mutex; std::string worker_error;
    auto fail=[&](const char* text) {std::lock_guard<std::mutex> lock(error_mutex);if(worker_error.empty())worker_error=text;};
    std::thread worker([&] {
        I2cCudaApi::Context context=nullptr; I2cCudaApi::Pointer a=0,b=0;
        try {
            auto check=[&](int status,const char* message) { if(status!=0) {fail(message);return false;} return true; };
            I2cCudaApi::Device device=0; char bus[32]{}; size_t free_bytes=0,total_bytes=0;
            if(check(cuda.init(0),"CUDA initialization failed") &&
               check(cuda.device_by_bus(&device,slot.c_str()),"CUDA could not resolve selected PCI bus") &&
               check(cuda.device_bus(bus,sizeof(bus),device),"CUDA PCI identity read failed")) {
                bus[sizeof(bus)-1]='\0'; auto actual=parse(bus);
                if(!actual || !same(*target,*actual)) fail("CUDA returned a different PCI device");
                else if(check(cuda.ctx_create(&context,0,device),"CUDA context creation failed") && context &&
                        check(cuda.mem_info(&free_bytes,&total_bytes),"CUDA free memory query failed")) {
                    if(free_bytes<256*mib) fail("Less than 256 MiB of free VRAM for CUDA warmup");
                    else {
                        size_t bytes=(free_bytes/20)*3; // floor(30% of free / 2)
                        bytes=std::max(64*mib,std::min(1024*mib,bytes)); bytes=(bytes/mib)*mib;
                        if(check(cuda.mem_alloc(&a,bytes),"CUDA first buffer allocation failed") &&
                           check(cuda.mem_alloc(&b,bytes),"CUDA second buffer allocation failed")) {
                            if(!stop && Clock::now()<hard_deadline) {
                                // Signal only after the first copy succeeds; callbacks require real in-flight work.
                                if(check(cuda.copy(a,b,bytes),"CUDA copy failed")) {
                                    started=true;
                                    while(!stop && Clock::now()<hard_deadline) {
                                        bool ok=true;
                                        for(int i=0;i<8 && !stop && Clock::now()<hard_deadline;++i)
                                            if(!check(cuda.copy(a,b,bytes),"CUDA copy failed")) {ok=false;break;}
                                        if(!ok || !check(cuda.ctx_sync(),"CUDA synchronize failed")) break;
                                    }
                                }
                            }
                        }
                    }
                }
            }
        } catch(...) {fail("CUDA warmup worker failed");}
        ended=true; // no callback may claim the load after copying ends
        if(b && cuda.mem_free(b)!=0) fail("CUDA second buffer cleanup failed");
        if(a && cuda.mem_free(a)!=0) fail("CUDA first buffer cleanup failed");
        if(context && cuda.ctx_destroy(context)!=0) fail("CUDA context cleanup failed");
    });
    auto cancelled=[&] {return callbacks.cancelled && callbacks.cancelled();};
    auto pause=[&](unsigned ms) { for(unsigned elapsed=0;elapsed<ms && !stop && !ended;) {unsigned n=std::min(20u,ms-elapsed);std::this_thread::sleep_for(std::chrono::milliseconds(n));elapsed+=n;} };
    auto failed=[&](const char* msg) {if(result.message.empty())result.message=msg;};
    try {
        const auto startup_deadline=std::min(hard_deadline,Clock::now()+std::chrono::milliseconds(settle_timeout_ms));
        while(!started && !ended && Clock::now()<startup_deadline && !cancelled() && callbacks.bound()) pause(10);
        if(cancelled()) failed("CUDA warmup cancelled");
        else if(!callbacks.bound()) failed("Selected GPU binding changed during CUDA warmup");
        else if(!started || ended) failed("CUDA warmup did not start or ended early");
        else {
            const auto settle_deadline=std::min(hard_deadline,Clock::now()+std::chrono::milliseconds(settle_timeout_ms));
            std::optional<double> last; int stable=0;
            while(Clock::now()<settle_deadline && !ended && !cancelled() && callbacks.bound()) {
                auto sample=callbacks.sample(); auto current=sample?sample->memory_mhz:std::optional<double>{};
                if(current && std::isfinite(*current) && last && *current==*last) ++stable; else stable=0;
                last=current;
                if(stable>=2) break;
                pause(poll_ms);
            }
            if(cancelled()) failed("CUDA warmup cancelled");
            else if(!callbacks.bound()) failed("Selected GPU binding changed during CUDA warmup");
            else if(ended || Clock::now()>=hard_deadline) failed("CUDA workload ended before voltage capture");
            else if(stable<2) failed("GPU memory clock did not settle during CUDA warmup");
            else {
                auto mv=callbacks.voltage_mv();
                if(!mv || !std::isfinite(*mv) || *mv<=0) failed("Loaded operating voltage unavailable");
                else if(ended || cancelled() || !callbacks.bound() || Clock::now()>=hard_deadline) failed("CUDA workload ended or binding changed during voltage capture");
                else result.voltage_mv=mv;
            }
        }
    } catch(...) {failed("CUDA warmup telemetry failed");}
    stop=true; worker.join();
    {std::lock_guard<std::mutex> lock(error_mutex); if(!worker_error.empty()) result.message=worker_error;}
    if(!result.message.empty() || !result.voltage_mv) {result.voltage_mv.reset();return result;}
    if(Clock::now()>=hard_deadline) {result.voltage_mv.reset();result.message="CUDA warmup reached its hard duration limit";return result;}
    result.ok=true;result.message="Settled CUDA workload ended and its observed operating voltage was captured";return result;
}
}
