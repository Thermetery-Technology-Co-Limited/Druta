// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/vflock.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <map>

#include "backend/gdi.h"
#include "backend/nvapi.h"
#include "backend/rm_transport.h"

namespace druta::backend {
namespace {

using HandlePtrFn = int(DRUTA_CDECL*)(void*, void*);
constexpr std::uint32_t BoostLock = 0xE440B867;  // the getter
constexpr std::uint32_t VfLockSet = 0x39442CFB;  // the setter; ONE ClockLock for both
constexpr std::uint32_t kArchKepler = 2;
constexpr std::uint32_t kArchTuring = 6;

// The entries the driver says are real: count, not the 32 the struct reserves.
std::uint32_t entry_count(const ClockLock& cl) { return std::min<std::uint32_t>(cl.count, 32); }

LockEntry* find_domain(ClockLock& cl, std::int64_t domain) {
    for (std::uint32_t k = 0; k < entry_count(cl); ++k)
        if (static_cast<std::int64_t>(cl.locks[k].domain) == domain) return &cl.locks[k];
    return nullptr;
}

std::string domain_list(const ClockLock& cl) {  // Python: str([e.domain for e in entries])
    std::string out = "[";
    for (std::uint32_t k = 0; k < entry_count(cl); ++k) {
        if (k) out += ", ";
        out += std::to_string(cl.locks[k].domain);
    }
    return out + "]";
}

std::string millivolts_2f(std::int64_t volt_uv) {  // Python: f"{volt_uv / 1000.0:.2f}"
    char text[64];
    std::snprintf(text, sizeof text, "%.2f", static_cast<double>(volt_uv) / 1000.0);
    return text;
}

}  // namespace

bool VfLock::vf_curve_applicable() {
    // Kepler uses ordinary clock offsets, not this V/F table.
    auto arch = telemetry_.arch();
    return !(arch && *arch == kArchKepler);
}

bool VfLock::available() {
    if (!vf_curve_applicable()) return false;
    Nvapi& a = gpu_.nvapi;
    return a.ok && a.interface(BoostLock) && a.interface(VfLockSet);
}

std::optional<ClockLock> VfLock::read_raw() {
    if (!vf_curve_applicable()) return std::nullopt;
    Nvapi& a = gpu_.nvapi;
    auto fn = a.ok ? a.get<HandlePtrFn>(BoostLock) : nullptr;
    if (!fn) return std::nullopt;
    ClockLock cl{};
    cl.version = nvapi_ver<ClockLock>(kVfLockVersion);
    if (fn(a.gpu, &cl) != 0) return std::nullopt;
    return cl;
}

std::optional<ClkLockRange> VfLock::legacy_clk_lock() {
    // This private ABI is a Turing-generation fallback. Runtime record identifiers, modes,
    // units and lengths decide whether the live driver actually has it; device ids, VBIOS
    // and driver strings do not. Pascal's NVML frequency-lock setter remains unsupported.
    auto arch = telemetry_.arch();
    if (!gpu_.nvapi.ok || !arch || *arch != kArchTuring) return std::nullopt;
    auto records = legacy_clk_limit_records();
    if (!records || records->size() != 2) return std::nullopt;
    const std::uint32_t idents[2] = {0x4C, 0x4B};
    std::uint32_t values[2];
    for (std::size_t i = 0; i < 2; ++i) {
        // R472 GET returns 82 dwords per record. Mode 2 is an explicit clock in kHz (unit 1);
        // the final three words are the effective value and may differ from this client's
        // requested constraint.
        const std::vector<std::uint32_t>& record = (*records)[i];
        if (record.size() != 82 || record[0] != idents[i] || record[1] != 2 || record[4] != 1 ||
            !(0 < record[3] && record[3] <= 10000000))
            return std::nullopt;
        values[i] = record[3] / 1000;
    }
    if (values[0] > values[1]) return std::nullopt;
    return ClkLockRange{values[0], values[1]};
}

std::optional<std::vector<std::vector<std::uint32_t>>> VfLock::legacy_clk_limit_records() {
    // Read RM GET 0x20802077; never submit its paired SET command. The transport belongs to
    // this NvAPI GPU/client and is captured once from its read-only voltage getter. Later
    // polls escape directly, with fresh count/pointer/record buffers. A rejected cached
    // transport is discarded.
    if (!rm_) return std::nullopt;
    if (!rm_->legacy_clk) {
        auto captured = rm_->capture_legacy_clk();
        if (!captured) return std::nullopt;
        rm_->legacy_clk = captured;
    }
    const CapturedTransport& transport = *rm_->legacy_clk;
    std::uint32_t records[2][82] = {};
    records[0][0] = 0x4C;
    records[1][0] = 0x4B;
    const std::uint64_t address = static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(records));
    std::uint32_t packet[21] = {};
    for (std::size_t i = 0; i < 17; ++i) packet[i] = transport.header[i];
    packet[17] = 2;  // two records
    packet[18] = 0;
    packet[19] = static_cast<std::uint32_t>(address & 0xFFFFFFFFu);
    packet[20] = static_cast<std::uint32_t>(address >> 32);  // 0 in a 32-bit process
    packet[2] = 84;
    packet[14] = 0x20802077;
    packet[15] = 16;
    packet[16] = 0;
    EscapeResult r = escape_raw(gpu_, packet, sizeof packet, transport.fields);
    if (r.outcome != EscapeOutcome::issued || r.ntstatus != 0 || packet[16] != 0 || packet[17] != 2) {
        rm_->legacy_clk.reset();
        return std::nullopt;
    }
    std::vector<std::vector<std::uint32_t>> out;
    for (const auto& record : records) out.emplace_back(record, record + 82);
    return out;
}

std::optional<VfLockEntry> VfLock::entry(std::int64_t domain) {
    auto cl = read_raw();
    if (!cl) return std::nullopt;
    const LockEntry* e = find_domain(*cl, domain);
    if (!e) return std::nullopt;
    return VfLockEntry{e->domain, e->lockMode, e->volt_uV, cl->count};
}

Status<PointLock> VfLock::read_vf_lock_status(std::optional<std::int64_t> domain) {
    auto cl = read_raw();
    if (!cl) return {std::nullopt, "V/F lock getter failed; the current lock is unknown"};
    std::vector<const LockEntry*> entries;
    for (std::uint32_t k = 0; k < entry_count(*cl); ++k)
        if (!domain || static_cast<std::int64_t>(cl->locks[k].domain) == *domain) entries.push_back(&cl->locks[k]);
    if (domain && entries.empty())
        return {std::nullopt, "V/F lock domain " + std::to_string(*domain) + " is missing; its current state is unknown"};
    for (const LockEntry* e : entries) {
        // mode 3 ONLY: a mode-2 entry is the NVML frequency lock and its field is kHz
        if (e->lockMode == kVfLockModePoint) return {PointLock{e->domain, e->lockMode, e->volt_uV, cl->count}, std::nullopt};
    }
    return {std::nullopt, std::nullopt};
}

std::optional<PointLock> VfLock::read_vf_lock() { return read_vf_lock_status(std::nullopt).value; }

std::optional<ClkLockRange> VfLock::read_clk_lock() {
    auto cl = read_raw();
    if (!cl) return legacy_clk_lock();
    std::map<std::uint32_t, std::uint32_t> by_dom;  // last one wins, as a dict comprehension
    for (std::uint32_t k = 0; k < entry_count(*cl); ++k)
        if (cl->locks[k].lockMode == kVfLockModeFreq) by_dom[cl->locks[k].domain] = cl->locks[k].volt_uV;
    if (by_dom.empty()) return legacy_clk_lock();
    auto hi = by_dom.find(kClkLockDomainMax);
    if (hi == by_dom.end()) return legacy_clk_lock();
    auto lo = by_dom.find(kClkLockDomainMin);
    std::uint32_t low = lo != by_dom.end() ? lo->second : hi->second;
    return ClkLockRange{low / 1000, hi->second / 1000};
}

Status<ClkLockRange> VfLock::read_clk_lock_status() {
    auto cl = read_raw();
    if (!cl) {
        if (auto legacy = legacy_clk_lock()) return {legacy, std::nullopt};
        return {std::nullopt, "frequency-lock getter failed; current ownership is unknown"};
    }
    if (!(0 < cl->count && cl->count <= 32)) return {std::nullopt, "frequency-lock table has an invalid count"};
    std::map<std::uint32_t, const LockEntry*> by_domain;
    for (std::uint32_t k = 0; k < entry_count(*cl); ++k) {
        const LockEntry& e = cl->locks[k];
        if (by_domain.count(e.domain)) return {std::nullopt, "frequency-lock table repeats a domain"};
        by_domain[e.domain] = &e;
    }
    if (!by_domain.count(kClkLockDomainMin) || !by_domain.count(kClkLockDomainMax))
        return {std::nullopt, "frequency-lock domains are missing; current ownership is unknown"};
    const LockEntry& low = *by_domain[kClkLockDomainMin];
    const LockEntry& high = *by_domain[kClkLockDomainMax];
    if (low.lockMode == 0 && high.lockMode == 0) {
        // Older Turing drivers expose a separate live frequency request in RM
        // even while the modern records report mode 0. Never mistake that
        // independent channel for free ownership.
        if (auto legacy = legacy_clk_lock()) return {legacy, std::nullopt};
        auto arch = telemetry_.arch();
        if (arch && *arch == kArchTuring) {
            auto records = legacy_clk_limit_records();
            bool ambiguous = !records || records->size() != 2;
            const std::uint32_t idents[2] = {0x4C, 0x4B};
            for (std::size_t i = 0; !ambiguous && i < 2; ++i) {
                const std::vector<std::uint32_t>& row = (*records)[i];
                if (row.size() != 82 || row[0] != idents[i] || row[1] != 0 || row[4] != 1) ambiguous = true;
            }
            if (ambiguous) return {std::nullopt, "legacy frequency-lock state is unreadable or ambiguous"};
        }
        return {std::nullopt, std::nullopt};
    }
    if (low.lockMode != kVfLockModeFreq || high.lockMode != kVfLockModeFreq)
        return {std::nullopt, "frequency-lock domains have an unsupported or conflicting mode"};
    std::uint32_t lo = low.volt_uV, hi = high.volt_uV;  // Mode2 fields are kHz, not voltage.
    if (!(0 < lo && lo <= hi && hi <= 10000000) || lo % 1000 || hi % 1000)
        return {std::nullopt, "frequency-lock range has invalid units or bounds"};
    return {ClkLockRange{lo / 1000, hi / 1000}, std::nullopt};
}

Outcome VfLock::set_vf_lock(std::int64_t volt_uv, std::optional<std::int64_t> domain) {
    if (recovery_pending()) return {false, "V/F lock: the previous change needs recovery before another hold"};
    if (!available()) return {false, "V/F point lock unavailable: 0xE440B867 / 0x39442CFB did not both resolve"};
    if (!(kVfLockMinUv <= volt_uv && volt_uv <= kVfLockMaxUv))
        return {false, "V/F lock: " + std::to_string(volt_uv) + " uV outside the sanity envelope [" +
                           std::to_string(kVfLockMinUv) + ".." + std::to_string(kVfLockMaxUv) +
                           "] uV - the argument is MICROvolts"};
    auto cl = read_raw();
    if (!cl) return {false, "V/F lock: getter failed, refusing to write blind"};
    if (!domain) {
        // mode 3 only: re-targeting a mode-2 (NVML frequency lock) entry would
        // convert the other mechanism's lock into a voltage lock on the wrong domain
        std::int64_t chosen = kVfLockDomain;
        for (std::uint32_t k = 0; k < entry_count(*cl); ++k) {
            if (cl->locks[k].lockMode == kVfLockModePoint) { chosen = cl->locks[k].domain; break; }
        }
        domain = chosen;
    }
    LockEntry* target = find_domain(*cl, *domain);
    if (!target)
        return {false, "V/F lock: domain " + std::to_string(*domain) + " is not in the driver's lock table (it lists " +
                           domain_list(*cl) + ")"};
    Recovery recovery{*domain, {target->lockMode, target->volt_uV}, {kVfLockModePoint, static_cast<std::uint32_t>(volt_uv)}};
    target->lockMode = kVfLockModePoint;
    target->volt_uV = static_cast<std::uint32_t>(volt_uv);
    cl->version = nvapi_ver<ClockLock>(kVfLockVersion);  // re-stamp; keep the rest
    // Record cleanup before calling the setter: even a failed verification
    // must not turn an accepted write into a hold nobody owns on exit.
    recovery_ = recovery;
    Nvapi& a = gpu_.nvapi;
    std::string reason;
    int st = a.get<HandlePtrFn>(VfLockSet)(a.gpu, &*cl);
    if (st != 0) {
        reason = "V/F lock write failed (status " + std::to_string(st) + ") - needs admin";
    } else {
        auto back = read_raw();
        const LockEntry* got = back ? find_domain(*back, *domain) : nullptr;
        if (got && got->lockMode == kVfLockModePoint && got->volt_uV == static_cast<std::uint32_t>(volt_uv)) {
            recovery_.reset();
            return {true, "V/F point lock set on domain " + std::to_string(*domain) + ", requested " + millivolts_2f(volt_uv) +
                              " mV - the hardware holds the highest V/F point at or below that"};
        }
        reason = back ? "V/F lock write returned OK but the requested lock was not read back"
                      : "V/F lock write returned OK but its verification read failed";
    }
    Outcome restored = recover_vf_lock();
    return {false, reason + "; " + (restored.ok ? restored.message : "RECOVERY REQUIRED: " + restored.message)};
}

Outcome VfLock::recover_vf_lock() {
    if (!recovery_) return {true, "no V/F lock recovery is pending"};
    if (!available()) return {false, "V/F lock recovery is unavailable"};
    auto cl = read_raw();
    if (!cl) return {false, "V/F lock recovery getter failed; refusing to write blind"};
    LockEntry* target = find_domain(*cl, recovery_->domain);
    if (!target) return {false, "V/F lock recovery target is missing from the current table"};
    std::pair<std::uint32_t, std::uint32_t> current{target->lockMode, target->volt_uV};
    if (current == recovery_->previous) {
        recovery_.reset();
        return {true, "previous V/F lock state is confirmed restored"};
    }
    if (current != recovery_->requested)
        return {false, "V/F lock target changed concurrently; leaving its current request untouched"};
    target->lockMode = recovery_->previous.first;
    target->volt_uV = recovery_->previous.second;
    Nvapi& a = gpu_.nvapi;
    cl->version = nvapi_ver<ClockLock>(kVfLockVersion);
    int st = a.get<HandlePtrFn>(VfLockSet)(a.gpu, &*cl);
    if (st != 0) return {false, "V/F lock recovery write failed (status " + std::to_string(st) + ")"};
    auto back = read_raw();
    const LockEntry* got = back ? find_domain(*back, recovery_->domain) : nullptr;
    if (!got || std::pair<std::uint32_t, std::uint32_t>{got->lockMode, got->volt_uV} != recovery_->previous)
        return {false, "V/F lock recovery was sent but the previous state could not be verified"};
    recovery_.reset();
    return {true, "previous V/F lock state restored and verified"};
}

Outcome VfLock::clear_vf_lock(std::optional<std::int64_t> domain, std::optional<std::int64_t> expected_uv) {
    if (!available()) return {false, "V/F point lock unavailable: 0xE440B867 / 0x39442CFB did not both resolve"};
    auto cl = read_raw();
    if (!cl) return {false, "V/F lock: getter failed, refusing to write blind"};
    std::vector<LockEntry*> entries;
    for (std::uint32_t k = 0; k < entry_count(*cl); ++k)
        if (!domain || static_cast<std::int64_t>(cl->locks[k].domain) == *domain) entries.push_back(&cl->locks[k]);
    if (domain && entries.empty())
        return {false, "V/F lock domain " + std::to_string(*domain) + " is missing; refusing to assume it was released"};
    std::vector<LockEntry*> held;
    for (LockEntry* e : entries)
        if (e->lockMode == kVfLockModePoint) held.push_back(e);
    if (domain && expected_uv && !held.empty() && static_cast<std::int64_t>(held[0]->volt_uV) != *expected_uv)
        return {true, "this V/F request is no longer held; the current target request was left untouched"};
    if (held.empty()) {
        if (!domain) recovery_.reset();
        return {true, "no V/F point lock was set"};
    }
    std::string doms;
    for (LockEntry* e : held) {
        doms += (doms.empty() ? "" : ", ") + std::to_string(e->domain);
        e->lockMode = kVfLockModeOff;  // volt_uV deliberately left as the driver has it
    }
    Nvapi& a = gpu_.nvapi;
    cl->version = nvapi_ver<ClockLock>(kVfLockVersion);
    int st = a.get<HandlePtrFn>(VfLockSet)(a.gpu, &*cl);
    if (st != 0) return {false, "V/F lock release failed (status " + std::to_string(st) + ") - needs admin"};
    // read back: the release is the one call whose failure would leave the
    // card pinned with nothing on screen saying so
    auto back = read_raw();
    if (!back) return {false, "V/F lock release was sent, but the verification read failed"};
    std::vector<const LockEntry*> after;
    for (std::uint32_t k = 0; k < entry_count(*back); ++k)
        if (!domain || static_cast<std::int64_t>(back->locks[k].domain) == *domain) after.push_back(&back->locks[k]);
    if (domain && after.empty()) return {false, "V/F lock release target is missing from the verification read"};
    for (const LockEntry* e : after)
        if (e->lockMode == kVfLockModePoint)
            return {false, "V/F lock release returned OK but the card still reports a lock - another tool is re-asserting it"};
    if (!domain) recovery_.reset();
    return {true, "V/F point lock released (domain" + std::string(held.size() > 1 ? "s" : "") + " " + doms + ")"};
}

Outcome VfLock::self_test() {
    // Hand the driver back the exact bytes its getter just produced: a
    // verified no-op proves both ids resolve and the 780-byte layout is the
    // one this driver expects, without touching a knob.
    if (!available()) return {false, "V/F lock self-test: 0xE440B867 / 0x39442CFB did not both resolve"};
    auto cl = read_raw();
    if (!cl) return {false, "V/F lock self-test: getter 0xE440B867 did not answer"};
    ClockLock before = *cl;
    Nvapi& a = gpu_.nvapi;
    int st = a.get<HandlePtrFn>(VfLockSet)(a.gpu, &*cl);
    if (st != 0)
        return {false, "V/F lock self-test: the driver REFUSED an identity write (status " + std::to_string(st) +
                           ") - do not use the V/F lock here"};
    auto after = read_raw();
    if (!after) return {false, "V/F lock self-test: identity write was accepted but the getter stopped answering"};
    const bool same = std::memcmp(&*after, &before, sizeof(ClockLock)) == 0;
    const std::uint32_t n = entry_count(before);
    std::string held;  // Python: str(['6:mode3', ...]) or 'none'
    for (std::uint32_t k = 0; k < n; ++k) {
        const LockEntry& e = before.locks[k];
        if (e.lockMode == kVfLockModeOff) continue;
        held += (held.empty() ? "" : ", ") + ("'" + std::to_string(e.domain) + ":mode" + std::to_string(e.lockMode) + "'");
    }
    if (!same)
        return {false, "V/F lock self-test: identity write accepted but the state CHANGED - either the layout is wrong "
                       "or another tool wrote between the reads"};
    return {true, "V/F lock self-test passed: identity write accepted, state unchanged, " + std::to_string(n) +
                      " entries, locked " + (held.empty() ? std::string("none") : "[" + held + "]")};
}

}  // namespace druta::backend
