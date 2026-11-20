// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "core/sha256.h"

#include <cstring>

namespace druta {

namespace {

constexpr std::uint32_t kRound[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

inline std::uint32_t rotr(std::uint32_t x, unsigned n) { return (x >> n) | (x << (32u - n)); }

using State = std::array<std::uint32_t, 8>;

void block(State& state, const std::uint8_t* chunk) {
    std::uint32_t w[64];
    for (unsigned i = 0; i < 16; ++i)
        w[i] = (std::uint32_t{chunk[4 * i]} << 24) | (std::uint32_t{chunk[4 * i + 1]} << 16) |
               (std::uint32_t{chunk[4 * i + 2]} << 8) | std::uint32_t{chunk[4 * i + 3]};
    for (unsigned i = 16; i < 64; ++i) {
        const std::uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
        const std::uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    std::uint32_t a = state[0], b = state[1], c = state[2], d = state[3];
    std::uint32_t e = state[4], f = state[5], g = state[6], h = state[7];
    for (unsigned i = 0; i < 64; ++i) {
        const std::uint32_t s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        const std::uint32_t ch = (e & f) ^ (~e & g);
        const std::uint32_t t1 = h + s1 + ch + kRound[i] + w[i];
        const std::uint32_t s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        const std::uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        const std::uint32_t t2 = s0 + maj;
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }
    state[0] += a;
    state[1] += b;
    state[2] += c;
    state[3] += d;
    state[4] += e;
    state[5] += f;
    state[6] += g;
    state[7] += h;
}

}  // namespace

std::array<std::uint8_t, 32> sha256(const std::uint8_t* data, std::size_t size) {
    State state{0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    const std::uint64_t bits = std::uint64_t{size} * 8u;
    std::size_t done = 0;
    for (; size - done >= 64; done += 64) block(state, data + done);
    // The tail, the 0x80 marker and the 64-bit length: one final block, or two when the tail leaves
    // fewer than 8 bytes for the length.
    std::uint8_t last[128] = {};
    const std::size_t tail = size - done;
    if (tail) std::memcpy(last, data + done, tail);
    last[tail] = 0x80;
    const std::size_t blocks = tail < 56 ? 1 : 2;
    for (unsigned i = 0; i < 8; ++i) last[64 * blocks - 8 + i] = static_cast<std::uint8_t>(bits >> (56 - 8 * i));
    for (std::size_t k = 0; k < blocks; ++k) block(state, last + 64 * k);
    std::array<std::uint8_t, 32> out{};
    for (unsigned i = 0; i < 8; ++i)
        for (unsigned k = 0; k < 4; ++k) out[4 * i + k] = static_cast<std::uint8_t>(state[i] >> (24 - 8 * k));
    return out;
}

std::string sha256_hex(const std::uint8_t* data, std::size_t size) {
    static const char digits[] = "0123456789abcdef";
    const std::array<std::uint8_t, 32> digest = sha256(data, size);
    std::string out(64, '0');
    for (std::size_t i = 0; i < 32; ++i) {
        out[2 * i] = digits[digest[i] >> 4];
        out[2 * i + 1] = digits[digest[i] & 0xF];
    }
    return out;
}

}  // namespace druta
