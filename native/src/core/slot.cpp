// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "core/slot.h"

#include <cstdio>

namespace druta {
namespace {

bool is_hex(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

std::uint32_t hex_value(char c) {
    if (c >= '0' && c <= '9') return static_cast<std::uint32_t>(c - '0');
    if (c >= 'a' && c <= 'f') return static_cast<std::uint32_t>(c - 'a' + 10);
    return static_cast<std::uint32_t>(c - 'A' + 10);
}

// Consume between min_digits and max_digits hex digits; nullopt otherwise.
std::optional<std::uint32_t> take_hex(std::string_view& text, std::size_t min_digits, std::size_t max_digits) {
    std::size_t n = 0;
    std::uint32_t value = 0;
    while (n < text.size() && n < max_digits && is_hex(text[n])) {
        value = (value << 4) | hex_value(text[n]);
        ++n;
    }
    if (n < min_digits) return std::nullopt;
    if (n < text.size() && is_hex(text[n])) return std::nullopt;  // too many digits
    text.remove_prefix(n);
    return value;
}

bool take(std::string_view& text, char c) {
    if (text.empty() || text.front() != c) return false;
    text.remove_prefix(1);
    return true;
}

std::string_view strip(std::string_view text) {
    // Python str.strip(): ASCII whitespace (the inputs are driver and nvtune strings).
    const char* ws = " \t\n\r\f\v";
    while (!text.empty() && std::string_view(ws).find(text.front()) != std::string_view::npos) text.remove_prefix(1);
    while (!text.empty() && std::string_view(ws).find(text.back()) != std::string_view::npos) text.remove_suffix(1);
    return text;
}

}  // namespace

std::string format_slot(std::uint32_t domain, std::uint32_t bus, std::uint32_t device, std::uint32_t function) {
    char buffer[64];
    std::snprintf(buffer, sizeof buffer, "%04x:%02x:%02x.%u", static_cast<unsigned>(domain),
                  static_cast<unsigned>(bus), static_cast<unsigned>(device), static_cast<unsigned>(function));
    return buffer;
}

std::string format_slot(const PciSlot& slot) {
    return format_slot(slot.domain, slot.bus, slot.device, slot.function);
}

std::optional<PciSlot> parse_slot(std::string_view text) {
    // ^([0-9a-fA-F]{4,8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-9a-fA-F])$ after strip()
    text = strip(text);
    PciSlot slot;
    auto domain = take_hex(text, 4, 8);
    if (!domain || !take(text, ':')) return std::nullopt;
    auto bus = take_hex(text, 2, 2);
    if (!bus || !take(text, ':')) return std::nullopt;
    auto device = take_hex(text, 2, 2);
    if (!device || !take(text, '.')) return std::nullopt;
    auto function = take_hex(text, 1, 1);
    if (!function || !text.empty()) return std::nullopt;
    slot.domain = *domain;
    slot.bus = *bus;
    slot.device = *device;
    slot.function = *function;
    return slot;
}

bool same_slot(std::string_view a, std::string_view b) {
    auto pa = parse_slot(a);
    auto pb = parse_slot(b);
    if (!pa || !pb) return false;
    return pa->bus == pb->bus && pa->device == pb->device;
}

}  // namespace druta
