// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/rails.h"

#include <cstring>

#include "backend/nvapi.h"
#include "core/calling.h"
#include "core/sleep.h"

namespace druta::backend {
namespace {

using HandlePtrFn = int(DRUTA_CDECL*)(void*, void*);

std::uint32_t word_u32(const RailBlock& block, std::size_t index) {
    std::uint32_t v = 0;
    std::memcpy(&v, block.data() + index * 4, sizeof v);
    return v;
}

std::int32_t word_i32(const RailBlock& block, std::size_t index) {
    std::int32_t v = 0;
    std::memcpy(&v, block.data() + index * 4, sizeof v);
    return v;
}

void put_u32(RailBlock& block, std::size_t index, std::uint32_t value) {
    std::memcpy(block.data() + index * 4, &value, sizeof value);
}

std::size_t record_word(std::size_t rail) { return (kRailRecordBase + rail * kRailRecordStride) / 4; }

}  // namespace

RailBlocks Rails::blocks(RailGetter function, std::uint32_t version) {
    RailBlocks out;
    Nvapi& a = gpu_.nvapi;
    const std::size_t f = static_cast<std::size_t>(function);
    auto fn = a.get<HandlePtrFn>(function == RailGetter::Control ? nvapi_id::VoltRailsCtlGet : nvapi_id::VoltRailsAbs);
    if (!(a.ok && fn)) return out;
    const std::uint32_t known = caches_.known[f].value_or(0u);
    auto& retry = caches_.retry_after[f];
    auto& diagnostics = caches_.diagnostics[f];
    const double now = druta::monotonic_seconds();
    std::vector<std::size_t> candidates;
    for (std::size_t r = 0; r < kRailCount; ++r) {
        if ((known >> r & 1u) || now >= retry[r].value_or(0.0)) candidates.push_back(r);
    }
    // R470 exposes the same NVAPI record fields at V1. Retry only after
    // INCOMPATIBLE_STRUCT_VERSION, not after an absent-rail error.
    std::vector<std::uint32_t> layouts;
    if (function == RailGetter::Control) {
        layouts = {kRailControlVersion, kRailControlV1};
    } else {
        layouts = {version};
    }
    const std::uint32_t preferred = caches_.versions[f].value_or(version);
    std::vector<std::uint32_t> requested{preferred};
    for (std::uint32_t v : layouts) {
        if (v != preferred) requested.push_back(v);
    }
    for (std::size_t rail : candidates) {
        const std::uint32_t mask = 1u << rail;
        for (std::uint32_t candidate_version : requested) {
            // One immediate read-only retry handles transient startup failures. Missing rails are
            // retried at a bounded cadence; a failed first read is never a permanent capability verdict.
            int status = 0;
            bool valid = false;
            RailBlock buf;
            for (std::int32_t attempt = 0; attempt < 2; ++attempt) {
                buf.assign(kRailBlockSize, 0);
                put_u32(buf, 0, candidate_version);
                put_u32(buf, 1, mask);
                status = fn(a.gpu, buf.data());
                valid = status == 0 && word_u32(buf, 0) == candidate_version && word_u32(buf, 1) == mask;
                diagnostics[rail] = RailDiagnostic{candidate_version, status, valid, attempt + 1};
                if (valid || status == kNvapiIncompatibleStructVersion) break;
            }
            if (valid) {
                out.rails[rail] = std::move(buf);
                caches_.versions[f] = candidate_version;
                retry[rail].reset();
                break;
            }
            if (status != kNvapiIncompatibleStructVersion) break;
        }
        if (!out.rails[rail]) retry[rail] = now + kRailRetrySeconds;
    }
    // Keep successful identities through a transient failure, but never fabricate fresh data from
    // the cache. Every write still needs reads.
    std::uint32_t read = known;
    for (std::size_t r = 0; r < kRailCount; ++r) {
        if (out.rails[r]) read |= 1u << r;
    }
    caches_.known[f] = read;
    return out;
}

std::array<std::optional<RailDeltas>, kRailCount> Rails::deltas() {
    std::array<std::optional<RailDeltas>, kRailCount> out;
    RailBlocks b = blocks(RailGetter::Control, kRailControlVersion);
    for (std::size_t rail = 0; rail < kRailCount; ++rail) {
        if (!b.rails[rail]) continue;
        const RailBlock& block = *b.rails[rail];
        const std::size_t base = record_word(rail);
        RailDeltas d;
        d.type = word_i32(block, base);
        d.reliability = word_i32(block, base + 1) / 1000.0;
        d.alt_reliability = word_i32(block, base + 2) / 1000.0;
        d.overvoltage = word_i32(block, base + 3) / 1000.0;
        d.vmin = word_i32(block, base + 4) / 1000.0;
        out[rail] = d;
    }
    return out;
}

std::optional<std::array<std::optional<RailState>, kRailCount>> Rails::state() {
    std::array<std::optional<RailState>, kRailCount> out;
    bool any = false;
    RailBlocks b = blocks(RailGetter::Abs, kLiveRailVersion);
    for (std::size_t rail = 0; rail < kRailCount; ++rail) {
        if (!b.rails[rail]) continue;
        const RailBlock& block = *b.rails[rail];
        const std::size_t base = record_word(rail);
        RailState s;
        s.boost_mv = word_u32(block, 2) / 1000.0;
        s.type = word_u32(block, base);
        s.live = word_u32(block, base + 1) / 1000.0;
        s.reliability = word_u32(block, base + 2) / 1000.0;
        s.alt_reliability = word_u32(block, base + 3) / 1000.0;
        s.overvoltage = word_u32(block, base + 4) / 1000.0;
        s.effective = word_u32(block, base + 5) / 1000.0;
        s.vmin = word_u32(block, base + 6) / 1000.0;
        // THE INDEX IDENTIFIES THE RAIL, not the type field, which is only decoded for the caller to
        // check. An all-zero record means the mask selected a rail this card does not have:
        // reporting 0.0 mV as a live voltage would be worse than reporting nothing.
        if (s.live != 0.0 || s.reliability != 0.0) {
            out[rail] = s;
            any = true;
        }
    }
    if (!any) return std::nullopt;
    return out;
}

std::optional<double> Rails::live_mv(std::int32_t rail) {
    auto s = state();
    if (!s || rail < 0 || static_cast<std::size_t>(rail) >= kRailCount) return std::nullopt;
    const auto& row = (*s)[static_cast<std::size_t>(rail)];
    if (!row) return std::nullopt;
    return row->live;
}

}  // namespace druta::backend
