// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/gdi.h"

#include <memory>
#include <vector>

#include "backend/exports.h"
#include "core/mutex.h"
#include "win/d3dkmt.h"

namespace druta::backend {
namespace {

using win::KmtFn;

// The four exports main's _legacy_clk_gdi requires, in its order.
constexpr const char* kEnum2 = "D3DKMTEnumAdapters2";
constexpr const char* kQuery = "D3DKMTQueryAdapterInfo";
constexpr const char* kClose = "D3DKMTCloseAdapter";
constexpr const char* kEscape = "D3DKMTEscape";
constexpr const char* kEnum = "D3DKMTEnumAdapters";
constexpr const char* kFromDisplayName = "D3DKMTOpenAdapterFromGdiDisplayName";
constexpr const char* kFromHdc = "D3DKMTOpenAdapterFromHdc";

struct Cache {
    unsigned generation = 0;
    std::unique_ptr<ExportSource> gdi;
    EscapeCapability capability;
};

Mutex& cache_mutex() {
    static Mutex mutex;
    return mutex;
}

Cache& cache() {
    static Cache c;
    return c;
}

// (Re)computes the capability for the current loader; holds cache_mutex.
Cache& current() {
    Cache& c = cache();
    unsigned generation = loader_generation();
    if (c.generation == generation) return c;
    std::string error;
    c.gdi = loader().open_gdi32(error);
    EscapeCapability cap;
    cap.gdi32_loaded = c.gdi != nullptr;
    if (c.gdi) {
        cap.escape = c.gdi->has(kEscape);
        cap.query_adapter_info = c.gdi->has(kQuery);
        cap.close_adapter = c.gdi->has(kClose);
        cap.enum_adapters2 = c.gdi->has(kEnum2);
        cap.enum_adapters = c.gdi->has(kEnum);
        cap.open_from_display_name = c.gdi->has(kFromDisplayName);
        cap.open_from_hdc = c.gdi->has(kFromHdc);
        // Main's path, and the only one with parity evidence; the fallbacks
        // are reported above and selected in a later increment.
        if (cap.enum_adapters2) cap.method = AdapterMethod::enum2;
    }
    if (!cap.gdi32_loaded) {
        cap.detail = error;
    } else if (!cap.escape) {
        // The probe's wording for an image without the escape path.
        cap.detail = "XPDM: no D3DKMT escape path; driver escapes are not possible on this image";
    } else if (!cap.complete()) {
        cap.detail = "gdi32 lacks";
        const char* sep = " ";
        for (const auto& [present, name] : {std::pair{cap.enum_adapters2, kEnum2}, std::pair{cap.query_adapter_info, kQuery},
                                            std::pair{cap.close_adapter, kClose}}) {
            if (present) continue;
            cap.detail += sep;
            cap.detail += name;
            sep = ", ";
        }
    }
    c.capability = cap;
    c.generation = generation;
    return c;
}

bool hex2(std::string_view text, std::size_t at, std::uint32_t& out) {
    std::uint32_t value = 0;
    for (std::size_t i = at; i < at + 2; ++i) {
        char ch = text[i];
        std::uint32_t digit;
        if (ch >= '0' && ch <= '9') digit = static_cast<std::uint32_t>(ch - '0');
        else if (ch >= 'a' && ch <= 'f') digit = static_cast<std::uint32_t>(ch - 'a' + 10);
        else if (ch >= 'A' && ch <= 'F') digit = static_cast<std::uint32_t>(ch - 'A' + 10);
        else return false;
        value = value * 16 + digit;
    }
    out = value;
    return true;
}

EscapeResult unavailable(RmErrorKind kind, bool fixed, std::string message) {
    EscapeResult r;
    r.outcome = EscapeOutcome::unavailable;
    r.why.kind = kind;
    r.why.fixed = fixed;
    r.why.message = std::move(message);
    return r;
}

}  // namespace

const EscapeCapability& escape_capability() {
    LockGuard guard(cache_mutex());
    return current().capability;
}

std::optional<EscapeSlot> escape_slot(std::string_view slot) {
    // re.fullmatch(r"0+:([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])", slot)
    std::size_t colon = slot.find(':');
    if (colon == std::string_view::npos || colon == 0) return std::nullopt;
    for (std::size_t i = 0; i < colon; ++i)
        if (slot[i] != '0') return std::nullopt;
    if (slot.size() != colon + 8) return std::nullopt;  // ":BB:DD.F"
    if (slot[colon + 3] != ':' || slot[colon + 6] != '.') return std::nullopt;
    EscapeSlot out{};
    if (!hex2(slot, colon + 1, out.bus) || !hex2(slot, colon + 4, out.device)) return std::nullopt;
    char fn = slot[colon + 7];
    if (fn < '0' || fn > '7') return std::nullopt;
    out.function = static_cast<std::uint32_t>(fn - '0');
    return out;
}

EscapeResult escape_raw(Gpu& gpu, void* packet, std::uint32_t size, const EscapeFields& fields) {
    // Order, as Python: slot, capability (no gdi call when incomplete),
    // enumerate, query each, match exactly one, escape; close every handle.
    std::string slot = gpu.nvapi.selected ? gpu.nvapi.selected->slot : std::string();
    auto location = escape_slot(slot);
    if (!location)
        return unavailable(RmErrorKind::invalid, false, "the paired PCI slot '" + slot + "' is not a usable adapter address");
    LockGuard guard(cache_mutex());
    Cache& c = current();
    if (!c.capability.complete())
        return unavailable(RmErrorKind::unsupported, true, c.capability.detail);
    auto enumerate = c.gdi->get<KmtFn>(kEnum2);
    auto query = c.gdi->get<KmtFn>(kQuery);
    auto close = c.gdi->get<KmtFn>(kClose);
    auto escape = c.gdi->get<KmtFn>(kEscape);

    win::D3DKMT_ADAPTERINFO items[win::kMaxAdapters] = {};
    win::D3DKMT_ENUMADAPTERS2 adapters{win::kMaxAdapters, items};
    if (enumerate(&adapters) != 0)
        return unavailable(RmErrorKind::transient, false, "D3DKMTEnumAdapters2 failed; nothing was closed");
    std::uint32_t count = adapters.NumAdapters < win::kMaxAdapters ? adapters.NumAdapters : win::kMaxAdapters;
    struct CloseAll {  // Python's finally: every enumerated handle, escape or not
        KmtFn close;
        const win::D3DKMT_ADAPTERINFO* items;
        std::uint32_t count;
        ~CloseAll() {
            for (std::uint32_t i = 0; i < count; ++i) {
                win::D3DKMT_CLOSEADAPTER request{items[i].hAdapter};
                close(&request);
            }
        }
    } closer{close, items, count};

    std::vector<std::uint32_t> matches;
    for (std::uint32_t i = 0; i < count; ++i) {
        win::D3DKMT_ADAPTERADDRESS address{};
        win::D3DKMT_QUERYADAPTERINFO request{items[i].hAdapter, win::KMTQAITYPE_ADAPTERADDRESS, &address,
                                             sizeof address};
        if (query(&request) == 0 && address.BusNumber == location->bus && address.DeviceNumber == location->device &&
            address.FunctionNumber == location->function)
            matches.push_back(items[i].hAdapter);
    }
    if (matches.size() != 1)
        return unavailable(RmErrorKind::transient, false,
                           matches.empty() ? "no adapter at the paired PCI slot " + slot
                                           : std::to_string(matches.size()) + " adapters at the paired PCI slot " + slot);
    win::D3DKMT_ESCAPE request{};
    request.hAdapter = matches[0];
    request.hDevice = fields.hDevice;
    request.Type = fields.Type;
    request.Flags = fields.Flags;
    request.pPrivateDriverData = packet;
    request.PrivateDriverDataSize = size;
    request.hContext = fields.hContext;
    EscapeResult r;
    r.outcome = EscapeOutcome::issued;
    r.ntstatus = escape(&request);
    return r;
}

}  // namespace druta::backend
