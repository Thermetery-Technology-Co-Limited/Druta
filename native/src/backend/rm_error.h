// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The error model of the RM escape transport (DESIGN-TRANSPORT.md 3.6): what
// Python raises as ValueError (transient, or invalid before the driver was
// touched) or CurrentLimitUnsupported (a fixed property of this image).
#pragma once

#include <cstdint>
#include <optional>
#include <string>

namespace druta::backend {

enum class RmErrorKind : std::int32_t { none = 0, transient = 1, invalid = 2, unsupported = 3 };

struct RmError {
    RmErrorKind kind = RmErrorKind::none;
    bool fixed = false;                 // ValueError.fixed
    std::optional<long> ntstatus;       // unset when no escape was issued (Python prints "None")
    std::uint32_t rm_status = 0;        // packet word 16 after the escape
    std::string message;                // str(exc) of the Python path
};

}  // namespace druta::backend
