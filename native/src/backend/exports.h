// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Where driver exports come from. The default source loads the driver's own
// nvapi64.dll / nvml.dll with LoadLibrary and resolves each export with
// GetProcAddress (export presence is the capability check, never a driver
// version). Tests install a resolver that serves fake functions, the same
// seam the Python tests use when they patch _load_nvml_library.
#pragma once

#include <cstdint>
#include <memory>
#include <string>

#include "core/calling.h"

namespace druta::backend {

// A loaded library: exports by name, nullptr when absent.
class ExportSource {
public:
    virtual ~ExportSource() = default;
    virtual void* resolve(const char* name) const = 0;
    virtual std::string path() const = 0;

    template <typename Fn>
    Fn get(const char* name) const {
        return reinterpret_cast<Fn>(resolve(name));
    }
    bool has(const char* name) const { return resolve(name) != nullptr; }
};

// Opens the two driver libraries and the graphics kernel interface (gdi32,
// whose D3DKMT* exports are the escape transport). Returns nullptr and fills
// error when the library cannot be loaded; the error text lists every
// location tried.
class Loader {
public:
    virtual ~Loader() = default;
    virtual std::unique_ptr<ExportSource> open_nvapi(std::string& error) = 0;
    virtual std::unique_ptr<ExportSource> open_nvml(std::string& error) = 0;
    virtual std::unique_ptr<ExportSource> open_gdi32(std::string& error) = 0;
};

// The process-wide loader: the platform default, or the test resolver.
Loader& loader();
// Bumped whenever the loader changes, so per-process capability caches
// (which assume export presence never changes) can tell a test resolver swap.
unsigned loader_generation();
// True while a test resolver serves the exports: a served D3DKMTEscape is a
// trampoline that must never be patched, so the hook uses its buffer patcher.
bool test_resolver_installed();

// Resolve through loader() and keep the source alive so the returned address is never borrowed
// from a temporary DLL. Only "nvapi" and "nvml" are supported. A resolver replacement discards the
// retained sources: its addresses, just like its callback/context, must no longer be in use then.
void* export_address(const char* library, const char* name);

// Test seam: resolve(context, library, name) with library "nvapi", "nvml" or
// "gdi32"; name "" asks whether the library loads at all (non-null means yes).
using ResolveFn = void*(DRUTA_CDECL*)(void* context, const char* library, const char* name);
void set_export_resolver(ResolveFn resolve, void* context);

// Process bitness, as the Python loader reports it in its error ("64-bit process").
inline constexpr unsigned kProcessBits = static_cast<unsigned>(sizeof(void*) * 8);

}  // namespace druta::backend
