// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/legacy_p0.h"

#include <string>

#include "backend/nvapi.h"
#include "core/calling.h"
#include "core/mutex.h"
#include "core/sleep.h"

namespace druta::backend {
namespace {

using ForcePstateFn = int(DRUTA_CDECL*)(void*, std::uint32_t, std::uint32_t);

}  // namespace

int LegacyP0::force(std::uint32_t pstate, std::uint32_t flags) {
    Nvapi& a = gpu_.nvapi;
    return a.get<ForcePstateFn>(nvapi_id::ForcePstate)(a.gpu, pstate, flags);
}

bool LegacyP0::supported() {
    Nvapi& a = gpu_.nvapi;
    if (!(a.ok && a.interface(nvapi_id::ForcePstate) && gpu_.pairing_error.empty())) return false;
    auto arch = telemetry_.arch();  // an absent architecture is not Kepler or Maxwell
    return arch && (*arch == kArchKeplerP0 || *arch == kArchMaxwellP0);
}

Outcome LegacyP0::hold() {
    bool already_owned;
    {
        LockGuard guard(gpu_.lock());
        if (!supported()) return {false, "legacy P0 hold is unavailable on this GPU/driver"};
        already_owned = owned();
        if (!already_owned) {
            // A transport failure can follow an accepted write. Own the request before calling
            // so a failed rollback remains retryable.
            owned_ = true;
            int status = force(0, 2);
            if (status != 0) {
                return {false, "P0 request failed (NVAPI status " + std::to_string(status) +
                               "); possible ownership retained for explicit release"};
            }
        }
    }
    const std::string reason = "P0 and its top memory band were not observed";
    int consecutive = 0;
    for (int sample = 0; sample < 20; ++sample) {
        Snapshot data = telemetry_.read();  // takes Gpu::lock() for the sample
        // The telemetry fields are uint32: present values are finite by construction, so Python's
        // math.isfinite checks on top and mem always hold here.
        bool good = data.pstate && *data.pstate == 0 && data.mem_p0max && *data.mem_p0max > 0 && data.mem &&
                    static_cast<double>(*data.mem) >= static_cast<double>(*data.mem_p0max) * 0.97;
        consecutive = good ? consecutive + 1 : 0;
        if (consecutive >= 3) {
            std::string core = data.core ? std::to_string(*data.core) : std::string("?");
            return {true, "P0 held; core " + core + " MHz, memory " + std::to_string(*data.mem) +
                              " MHz (not a maximum-core lock)"};
        }
        sleep_ms(100);  // without the lock: a telemetry tick may run meanwhile
    }
    if (already_owned) return {false, reason + "; existing session P0 hold retained"};
    LockGuard guard(gpu_.lock());
    Outcome released = release();
    return {false, reason + "; " + (released.ok ? released.message : "RELEASE FAILED: " + released.message)};
}

Outcome LegacyP0::release() {
    if (!owned()) return {true, "this session owns no legacy P0 hold"};
    int status = force(16, 2);
    if (status != 0) return {false, "P0 release failed (NVAPI status " + std::to_string(status) + ")"};
    // Automatic behaviour may still be P0 under load. Waiting for P8 would incorrectly report a
    // failed release while a game is running.
    owned_ = false;
    return {true, "P0 request released; automatic performance states restored"};
}

Outcome LegacyP0::request(bool& acquired) {
    acquired = false;
    // verification_legacy_p0's own refusal, repeated here so the entry never calls an absent export.
    auto arch = telemetry_.arch();
    if (!(arch && *arch == kArchKeplerP0) || !supported())
        return {false, "Kepler P0 request is unavailable; verification cannot write"};
    if (owned()) return {true, ""};
    // A transport failure may follow an accepted write.
    acquired = true;
    owned_ = true;
    int status = force(0, 2);
    if (status != 0) return {false, "Kepler P0 request failed (NVAPI status " + std::to_string(status) + ")"};
    return {true, ""};
}

}  // namespace druta::backend
