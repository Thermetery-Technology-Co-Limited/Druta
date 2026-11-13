/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <cstdint>
#include <cstddef>
#include <functional>
#include <optional>
#include <string>
#include "cuda_driver.h"
namespace druta::ui {
using I2cCudaApi = CudaDriverApi;
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
