// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// SHA-256 (FIPS 180-4) for the PERF snapshot digests (hashlib.sha256(...).hexdigest()). Embedded: the
// core imports only kernel32 and msvcrt, so no OS or library hash is used.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

namespace druta {

class Sha256 {
public:
    Sha256();
    void update(const std::uint8_t* data, std::size_t size);
    // The digest of everything updated so far. The object is spent afterwards (construct a new one).
    std::array<std::uint8_t, 32> finish();

private:
    void block(const std::uint8_t* chunk);
    std::array<std::uint32_t, 8> state_;
    std::array<std::uint8_t, 64> buffer_{};
    std::size_t buffered_ = 0;
    std::uint64_t length_ = 0;  // bytes hashed
};

// hashlib.sha256(data).digest() / .hexdigest() (64 lowercase hex characters).
std::array<std::uint8_t, 32> sha256(const std::uint8_t* data, std::size_t size);
std::string sha256_hex(const std::uint8_t* data, std::size_t size);

}  // namespace druta
