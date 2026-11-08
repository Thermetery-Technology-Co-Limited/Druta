// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The (ok, message) pair every writer in nvbackend returns.
#pragma once

#include <string>

namespace druta::backend {

struct Outcome {
    bool ok;
    std::string message;
};

}  // namespace druta::backend
