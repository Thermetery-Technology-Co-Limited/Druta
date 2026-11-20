// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// SHA-256 (FIPS 180-4) for the PERF snapshot digests (hashlib.sha256(...).hexdigest()). Embedded: the
// core imports only kernel32 and msvcrt, so no OS or library hash is used. One-shot only: every
// caller hashes one whole buffer, so there is no incremental state carried between calls.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

namespace druta {

// hashlib.sha256(data).digest() / .hexdigest() (64 lowercase hex characters).
std::array<std::uint8_t, 32> sha256(const std::uint8_t* data, std::size_t size);
std::string sha256_hex(const std::uint8_t* data, std::size_t size);

}  // namespace druta
