/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <cstdint>
#include <cstddef>
#include <functional>
#include <optional>
#include <string>
#ifdef _WIN32
#define DRUTA_CUDA_CALL __stdcall
#else
#define DRUTA_CUDA_CALL
#endif
namespace druta::ui {
// Narrow CUDA driver API; injection is for deterministic tests. All driver
// calls, including cleanup, execute on one worker thread.
struct I2cCudaApi {
    using Device=int; using Context=void*; using Pointer=uint64_t;
    int (DRUTA_CUDA_CALL *init)(unsigned)=nullptr;
    int (DRUTA_CUDA_CALL *device_by_bus)(Device*,const char*)=nullptr;
    int (DRUTA_CUDA_CALL *device_bus)(char*,int,Device)=nullptr;
    int (DRUTA_CUDA_CALL *ctx_create)(Context*,unsigned,Device)=nullptr;
    int (DRUTA_CUDA_CALL *ctx_destroy)(Context)=nullptr;
    int (DRUTA_CUDA_CALL *ctx_sync)()=nullptr;
    int (DRUTA_CUDA_CALL *mem_info)(size_t*,size_t*)=nullptr;
    int (DRUTA_CUDA_CALL *mem_alloc)(Pointer*,size_t)=nullptr;
    int (DRUTA_CUDA_CALL *mem_free)(Pointer)=nullptr;
    int (DRUTA_CUDA_CALL *copy)(Pointer,Pointer,size_t)=nullptr;
};
struct I2cWarmupSample { std::optional<double> memory_mhz; std::optional<int> pstate; };
struct I2cWarmupCallbacks {
    std::function<bool()> bound;
    std::function<std::optional<I2cWarmupSample>()> sample;
    std::function<std::optional<double>()> voltage_mv;
    std::function<bool()> cancelled;
};
struct I2cWarmupResult { bool ok=false; std::optional<double> voltage_mv; std::string message; };
// Deadline includes device setup and workload. The selected PCI slot is
// mandatory and round-tripped through CUDA; no ordinal fallback exists.
I2cWarmupResult i2c_cuda_warmup(const std::string& slot,const I2cWarmupCallbacks& callbacks,
    const I2cCudaApi* injected=nullptr, unsigned settle_timeout_ms=15000,
    unsigned max_duration_ms=180000, unsigned poll_ms=400);
}
#undef DRUTA_CUDA_CALL
