// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/gdi.h"

#include <algorithm>
#include <iterator>
#include <memory>
#include <vector>

#include "backend/exports.h"
#include "core/mutex.h"
#include "win/d3dkmt.h"
#include "win/display_interfaces.h"

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
constexpr const char* kDisplayDevices = "EnumDisplayDevicesW";
constexpr const char* kFromDeviceName = "D3DKMTOpenAdapterFromDeviceName";

struct Cache {
    unsigned generation = 0;
    std::unique_ptr<ExportSource> gdi;
    std::unique_ptr<ExportSource> user;
    std::unique_ptr<ExportSource> cfg;
    bool device_interfaces = false;
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
    c.user.reset();
    c.cfg.reset();
    c.device_interfaces = false;
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
        if (cap.enum_adapters2) {
            cap.method = AdapterMethod::enum2;
        } else {
            if (cap.open_from_display_name) {
                c.user = loader().open_user32(error);
                if (c.user && c.user->has(kDisplayDevices)) cap.method = AdapterMethod::gdi_display_name;
            }
            if (c.gdi->has(kFromDeviceName)) {
                c.cfg = loader().open_cfgmgr32(error);
                c.device_interfaces = c.cfg && c.cfg->has(win::kInterfaceSize) && c.cfg->has(win::kInterfaceList);
                if (c.device_interfaces && cap.method == AdapterMethod::none) cap.method = AdapterMethod::device_interface;
            }
        }
    }
    if (!cap.gdi32_loaded) {
        cap.detail = error;
    } else if (!cap.escape) {
        // The probe's wording for an image without the escape path.
        cap.detail = "XPDM: no D3DKMT escape path; driver escapes are not possible on this image";
    } else if (!cap.complete()) {
        cap.detail = "gdi32 lacks";
        const char* sep = " ";
        for (const auto& [present, name] : {std::pair{cap.method != AdapterMethod::none, kEnum2}, std::pair{cap.query_adapter_info, kQuery},
                                            std::pair{cap.close_adapter, kClose}}) {
            if (present) continue;
            cap.detail += sep;
            cap.detail += name;
            sep = ", ";
        }
        if (cap.method == AdapterMethod::none) {
            cap.detail += "; display-name fallback lacks ";
            cap.detail += cap.open_from_display_name ? "user32!EnumDisplayDevicesW" : "gdi32!D3DKMTOpenAdapterFromGdiDisplayName";
            if (c.gdi->has(kFromDeviceName)) {
                cap.detail += "; device-interface lookup lacks";
                if (!c.cfg || !c.cfg->has(win::kInterfaceSize)) cap.detail += " CM_Get_Device_Interface_List_SizeW";
                if (!c.cfg || !c.cfg->has(win::kInterfaceList)) cap.detail += " CM_Get_Device_Interface_ListW";
            }
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

struct OpenedAdapters {
    KmtFn close;
    std::vector<win::D3DKMT_ADAPTERINFO> items;

    template <typename Request>
    void open_one(KmtFn open, Request& request) {
        // Allocate storage before opening so allocation failure cannot leak a handle.
        items.emplace_back();
        if (open(&request) == 0 && request.hAdapter)
            items.back() = {request.hAdapter, request.LuidLow, request.LuidHigh, 0, 0};
        else
            items.pop_back();
    }

    void close_all() {
        // Different display heads can give us multiple opens of one adapter.
        // Close each successful open, even when their LUIDs matched.
        for (const auto& item : items) {
            win::D3DKMT_CLOSEADAPTER request{item.hAdapter};
            close(&request);
        }
        items.clear();
    }
    ~OpenedAdapters() { close_all(); }
};

void open_display_adapters(const Cache& c, OpenedAdapters& adapters) {
    auto enumerate = c.user->get<win::EnumDisplayDevicesFn>(kDisplayDevices);
    auto open = c.gdi->get<KmtFn>(kFromDisplayName);
    for (std::uint32_t index = 0;; ++index) {
        win::DisplayDeviceW display{};
        display.cb = sizeof display;
        if (!enumerate(nullptr, index, &display, 0)) return;
        auto end = std::find(std::begin(display.DeviceName), std::end(display.DeviceName), u'\0');
        if (end == std::begin(display.DeviceName) || end == std::end(display.DeviceName)) continue;
        win::D3DKMT_OPENADAPTERFROMGDIDISPLAYNAME request{};
        std::copy(std::begin(display.DeviceName), end, request.DeviceName);
        adapters.open_one(open, request);
    }
}

std::string open_device_adapters(const Cache& c, OpenedAdapters& adapters) {
    std::vector<std::u16string> paths;
    std::string error = win::display_interface_paths(c.cfg->get<win::InterfaceSizeFn>(win::kInterfaceSize),
                                                    c.cfg->get<win::InterfaceListFn>(win::kInterfaceList), paths);
    if (!error.empty()) return error;
    auto open = c.gdi->get<KmtFn>(kFromDeviceName);
    for (const auto& path : paths) {
        win::D3DKMT_OPENADAPTERFROMDEVICENAME request{};
        request.pDeviceName = path.c_str();
        adapters.open_one(open, request);
    }
    return {};
}

std::vector<win::D3DKMT_ADAPTERINFO> matching_adapters(const OpenedAdapters& opened, KmtFn query,
                                                       const EscapeSlot& location, bool deduplicate_luids) {
    std::vector<win::D3DKMT_ADAPTERINFO> matches;
    for (const auto& item : opened.items) {
        win::D3DKMT_ADAPTERADDRESS address{};
        win::D3DKMT_QUERYADAPTERINFO request{item.hAdapter, win::KMTQAITYPE_ADAPTERADDRESS, &address, sizeof address};
        if (query(&request) != 0 || address.BusNumber != location.bus || address.DeviceNumber != location.device ||
            address.FunctionNumber != location.function) continue;
        if (deduplicate_luids && (item.LuidLow || item.LuidHigh) &&
            std::any_of(matches.begin(), matches.end(), [&](const auto& previous) {
                return previous.LuidLow == item.LuidLow && previous.LuidHigh == item.LuidHigh;
            })) continue;
        matches.push_back(item);
    }
    return matches;
}

}  // namespace

const EscapeCapability& escape_capability() {
    LockGuard guard(cache_mutex());
    return current().capability;
}

void* escape_export(bool require_complete) {
    LockGuard guard(cache_mutex());
    Cache& c = current();
    if (!c.gdi || (require_complete ? !c.capability.complete() : !c.capability.escape)) return nullptr;
    return c.gdi->resolve(kEscape);
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
    auto query = c.gdi->get<KmtFn>(kQuery);
    auto escape = c.gdi->get<KmtFn>(kEscape);

    OpenedAdapters opened{c.gdi->get<KmtFn>(kClose), {}};
    std::vector<win::D3DKMT_ADAPTERINFO> matches;
    if (c.capability.method == AdapterMethod::enum2) {
        auto enumerate = c.gdi->get<KmtFn>(kEnum2);
        opened.items.resize(win::kMaxAdapters);
        win::D3DKMT_ENUMADAPTERS2 adapters{win::kMaxAdapters, opened.items.data()};
        if (enumerate(&adapters) != 0) {
            opened.items.clear();  // no returned handles on failure
            return unavailable(RmErrorKind::transient, false, "D3DKMTEnumAdapters2 failed; nothing was closed");
        }
        opened.items.resize(std::min(adapters.NumAdapters, win::kMaxAdapters));
        matches = matching_adapters(opened, query, *location, false);
    } else {
        if (c.capability.method == AdapterMethod::gdi_display_name) {
            open_display_adapters(c, opened);
            matches = matching_adapters(opened, query, *location, true);
        }
        if (matches.empty() && c.device_interfaces) {
            opened.close_all();
            const std::string error = open_device_adapters(c, opened);
            if (!error.empty()) return unavailable(RmErrorKind::transient, false, error);
            matches = matching_adapters(opened, query, *location, true);
        }
    }
    if (matches.size() != 1)
        return unavailable(RmErrorKind::transient, false,
                           matches.empty() ? "no adapter at the paired PCI slot " + slot
                                           : std::to_string(matches.size()) + " adapters at the paired PCI slot " + slot);
    win::D3DKMT_ESCAPE request{};
    request.hAdapter = matches[0].hAdapter;
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
