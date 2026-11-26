// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Exercise the C ABI with a simulated NVAPI adapter and no installed driver.
#include <drutacore/drutacore.h>
#include "backend/nvapi.h"
#include "backend/nvml.h"
#include "backend/structs.h"
#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {
constexpr uint32_t compact = 0x10964, extended = 0x261a4;
constexpr uint32_t pascal = 0x130, turing = 0x160, ampere = 0x170, ada = 0x190, blackwell = 0x1b0;
constexpr uint32_t bit(unsigned domain) { return 1u << domain; }
struct Fixture {
    uint32_t architecture = turing;
    uint32_t version = compact;
    uint32_t present = bit(0) | bit(1) | bit(3) | bit(5) | bit(9);
    std::array<int32_t, DC_CLKDOM_SLOTS> mode{};
    uint32_t missing_in_combined = 0;
    bool has_setter = true;
    unsigned gets = 0, sets = 0;
    druta::backend::AllClocksPriv clocks{};
    bool private_clocks_available = true;
    bool has_nvml = false;
    unsigned memory_mhz = 0;
} fixture;
void check(bool result, const char* expression, int line) {
    if (!result) { std::fprintf(stderr, "clock discovery line %d: %s\n", line, expression); std::exit(1); }
}
#define CHECK(test) check((test), #test, __LINE__)

int fake_adapter;
int DC_CALL init() { return 0; }
int DC_CALL enumerate(void** handles, unsigned* count) { handles[0] = &fake_adapter; *count = 1; return 0; }
int DC_CALL pci(void*, unsigned* id, unsigned* sub, unsigned* rev, unsigned* ext) {
    *id = 0xabcd10de; *sub = 0x12345678; *rev = 0; *ext = 0; return 0;
}
int DC_CALL bus(void*, unsigned* out) { *out = 2; return 0; }
int DC_CALL slot(void*, unsigned* out) { *out = 3; return 0; }
int DC_CALL arch(void*, void* data) { static_cast<uint32_t*>(data)[1] = fixture.architecture; return 0; }
int DC_CALL get(void* handle, void* data) {
    CHECK(handle == &fake_adapter);
    ++fixture.gets;
    auto* words = static_cast<uint32_t*>(data);
    if (words[0] != fixture.version) return -9;
    const uint32_t mask = words[2];
    if (!mask || (mask & ~fixture.present)) return -1;
    const bool multiple = (mask & (mask - 1)) != 0;
    const uint32_t header = words[0] == compact ? 0x64u : 0x124u;
    const uint32_t stride = words[0] == compact ? 0x48u : 0x304u;
    for (unsigned d = 0; d < DC_CLKDOM_SLOTS; ++d) {
        if (!(mask & bit(d)) || (multiple && (fixture.missing_in_combined & bit(d)))) continue;
        words[(header + d * stride) / 4] = static_cast<uint32_t>(fixture.mode[d]);
    }
    return 0;
}
int DC_CALL set(void*, void*) { ++fixture.sets; return -1; }
int DC_CALL clocks(void* handle, void* data) {
    CHECK(handle == &fake_adapter);
    if (!fixture.private_clocks_available) return -1;
    auto* result = static_cast<druta::backend::AllClocksPriv*>(data);
    std::memcpy(result->w, fixture.clocks.w, sizeof(result->w));
    return 0;
}
void* DC_CALL query(unsigned id) {
    using namespace druta::backend::nvapi_id;
    switch (id) {
        case Initialize: return reinterpret_cast<void*>(init);
        case EnumPhysicalGPUs: return reinterpret_cast<void*>(enumerate);
        case GetPCIIdentifiers: return reinterpret_cast<void*>(pci);
        case GetBusId: return reinterpret_cast<void*>(bus);
        case GetBusSlotId: return reinterpret_cast<void*>(slot);
        case GetArchInfo: return reinterpret_cast<void*>(arch);
        case 0x1bd69f49: return reinterpret_cast<void*>(clocks);
        case 0xf58938f5: return reinterpret_cast<void*>(get);
        case 0xd14b69cf: return fixture.has_setter ? reinterpret_cast<void*>(set) : nullptr;
        default: return nullptr;
    }
}
int DC_CALL nv_count(unsigned* out) { *out = 1; return 0; }
int DC_CALL nv_device(unsigned, void** out) { *out = &fake_adapter; return 0; }
int DC_CALL nv_pci(void*, druta::backend::NvmlPciInfo* out) {
    out->domain = 0; out->bus = 2; out->device = 3;
    out->pci_device_id = 0xabcd10de; out->pci_subsystem_id = 0x12345678;
    return 0;
}
int DC_CALL nv_clock(void* handle, unsigned type, unsigned* out) {
    CHECK(handle == &fake_adapter);
    if (type != 2 || !fixture.memory_mhz) return -1;
    *out = fixture.memory_mhz;
    return 0;
}
void* DC_CALL resolve(void*, const char* library, const char* name) {
    if (fixture.has_nvml && std::strcmp(library, "nvml") == 0) {
        if (!*name) return &fake_adapter;
        if (!std::strcmp(name, "nvmlInit_v2")) return reinterpret_cast<void*>(init);
        if (!std::strcmp(name, "nvmlDeviceGetCount_v2")) return reinterpret_cast<void*>(nv_count);
        if (!std::strcmp(name, "nvmlDeviceGetHandleByIndex_v2")) return reinterpret_cast<void*>(nv_device);
        if (!std::strcmp(name, "nvmlDeviceGetPciInfo_v3")) return reinterpret_cast<void*>(nv_pci);
        if (!std::strcmp(name, "nvmlDeviceGetClockInfo")) return reinterpret_cast<void*>(nv_clock);
        return nullptr;
    }
    if (std::strcmp(library, "nvapi")) return nullptr;
    if (!*name) return &fake_adapter;
    return std::strcmp(name, "nvapi_QueryInterface") ? nullptr : reinterpret_cast<void*>(query);
}
dc_gpu* open() {
    dc_gpu* gpu = nullptr;
    CHECK(dc_gpu_open("0000:02:03.0", &gpu) == DC_OK && gpu);
    return gpu;
}
using List = decltype(&dc_gpu_clkdom_domains);
uint32_t mask_for(dc_gpu* gpu, List read) {
    uint32_t ids[DC_CLKDOM_SLOTS]{}, mask = 0;
    size_t count = 0;
    CHECK(read(gpu, ids, DC_CLKDOM_SLOTS, &count) == DC_OK && count <= DC_CLKDOM_SLOTS);
    for (size_t i = 0; i < count; ++i) { CHECK(ids[i] < DC_CLKDOM_SLOTS); mask |= bit(ids[i]); }
    return mask;
}
bool retry(dc_gpu* gpu, double seconds) {
    int32_t changed = 0;
    CHECK(dc_gpu_retry_clock_capabilities(gpu, 1, seconds, &changed) == DC_OK);
    return changed != 0;
}
bool adjustable(dc_gpu* gpu, uint32_t domain, int32_t observed_mode) {
    const auto gets = fixture.gets;
    dc_outcome out{};
    CHECK(dc_gpu_clkdom_offset_write_capability(gpu, domain, observed_mode, &out) == DC_OK);
    CHECK(fixture.gets == gets && fixture.sets == 0);
    CHECK(out.ok || out.message[0]);
    return out.ok != 0;
}
void test_turing_rows(bool bad_first) {
    fixture = Fixture{};
    const unsigned late = bad_first ? 1u : 3u;
    const unsigned good = bad_first ? 3u : 1u;
    fixture.present = bit(1) | bit(2) | bit(3) | bit(5) | bit(9);
    fixture.mode[good] = 9;
    fixture.mode[2] = 1;  // readable memory record, no qualified compact Turing memory offset control
    fixture.mode[5] = -1;
    fixture.mode[9] = 10;
    dc_gpu* gpu = open();
    dc_clkdom_layout layout{};
    layout.size = sizeof(layout);
    CHECK(dc_gpu_clkdom_layout(gpu, &layout) == DC_OK);
    CHECK(std::strcmp(layout.name, "turing-compact") == 0 && !layout.has_nvvdd_uv && !layout.has_msvdd_uv);
    CHECK(mask_for(gpu, dc_gpu_clkdom_domains) == (bit(good) | bit(2)));
    CHECK(mask_for(gpu, dc_gpu_clkdom_controls_for_ui) == bit(good));
    CHECK(!retry(gpu, 0));
    fixture.mode[late] = 1;
    CHECK(retry(gpu, 1));
    CHECK(mask_for(gpu, dc_gpu_clkdom_controls_for_ui) == (bit(1) | bit(3)));

    // The single-domain probes succeeded. A later combined reply still has
    // to contain every requested record before it can supply their values.
    fixture.missing_in_combined = bit(late);
    dc_clkdom_requests rows{};
    rows.size = sizeof(rows);
    CHECK(dc_gpu_clkdom_requests(gpu, &rows) == DC_E_UNAVAILABLE && rows.count == 0);
    CHECK(dc_gpu_clkdom_offset_read_pending(gpu));
    fixture.missing_in_combined = 0;
    CHECK(retry(gpu, 4));
    CHECK(!dc_gpu_clkdom_offset_read_pending(gpu));
    CHECK(dc_gpu_clkdom_requests(gpu, &rows) == DC_OK && rows.count == 3);
    CHECK(rows.rows[0].domain == 1 && rows.rows[1].domain == 2 && rows.rows[2].domain == 3);
    CHECK(rows.rows[1].mode == 1);
    for (unsigned i = 0; i < rows.count; ++i)
        CHECK(adjustable(gpu, rows.rows[i].domain, rows.rows[i].mode) == (rows.rows[i].domain == good));
    CHECK(!adjustable(gpu, 2, 9)); // even an offset mode cannot qualify the compact memory field
    CHECK(!adjustable(gpu, 4, 9)); // this adapter never answered for this domain
    const auto gets = fixture.gets;
    CHECK(!retry(gpu, 12) && fixture.gets == gets);
    CHECK(fixture.sets == 0);
    dc_gpu_close(gpu);
}
void test_other_layout(uint32_t architecture, uint32_t version, bool has_setter) {
    fixture = Fixture{};
    fixture.architecture = architecture;
    fixture.version = version;
    fixture.has_setter = has_setter;
    fixture.present = bit(1) | bit(3);
    // These two layouts retain their existing acceptance of echoed zero modes.
    dc_gpu* gpu = open();
    dc_clkdom_layout layout{};
    layout.size = sizeof(layout);
    CHECK(dc_gpu_clkdom_layout(gpu, &layout) == DC_OK && layout.version == version);
    CHECK(layout.has_nvvdd_uv && (layout.has_msvdd_uv != 0) == (version == extended));
    CHECK(mask_for(gpu, dc_gpu_clkdom_domains) == fixture.present);
    dc_clkdom_requests rows{};
    rows.size = sizeof(rows);
    CHECK(dc_gpu_clkdom_requests(gpu, &rows) == DC_OK && rows.count == 2);
    if (architecture == turing) CHECK(adjustable(gpu, 1, rows.rows[0].mode) == has_setter);
    CHECK(fixture.sets == 0);
    dc_gpu_close(gpu);
}
void counter(unsigned id, unsigned source) {
    fixture.clocks.w[2 * id] = 1500000;
    fixture.clocks.w[64 + 7 * id] = 1500000;
    fixture.clocks.w[64 + 7 * id + 1] = source;
}
uint32_t matched(dc_gpu* gpu, unsigned control, int32_t mode) {
    uint32_t mask = 0xffffffff;
    dc_outcome out{};
    CHECK(dc_gpu_clkdom_known_offset_pattern(gpu, control, mode, &mask, &out) == DC_OK);
    CHECK(out.ok == (mask != 0) && out.message[0] && fixture.sets == 0);
    return mask;
}
void test_known_ampere() {
    fixture = Fixture{};
    fixture.architecture = ampere;
    fixture.version = extended;
    fixture.present = bit(1) | bit(2) | bit(3) | bit(5) | bit(9);
    fixture.mode.fill(10);
    for (unsigned id : {1u, 2u, 4u, 5u, 21u}) counter(id, 32);
    dc_gpu* gpu = open();
    CHECK(matched(gpu, 1, 10) == (bit(1) | bit(2) | bit(5) | bit(21)));
    CHECK(matched(gpu, 2, 10) == bit(4) && matched(gpu, 3, 10) == bit(2));
    CHECK(matched(gpu, 5, 10) == bit(21) && matched(gpu, 9, 10) == bit(5));
    fixture.private_clocks_available = false;
    CHECK(matched(gpu, 3, 10) == 0);
    fixture.private_clocks_available = true;
    counter(5, 0);  // a coupled clock changed parent
    CHECK(matched(gpu, 1, 10) == 0 && matched(gpu, 3, 10) == bit(2));
    counter(5, 32);
    CHECK(matched(gpu, 1, 10) != 0);  // missing telemetry never poisons the next read
    fixture.mode[1] = 9;  // a previously good combined GET supplied stale mode
    CHECK(matched(gpu, 1, 10) == 0 && matched(gpu, 1, 9) == 0);
    CHECK(matched(gpu, 7, 10) == 0);
    dc_gpu_close(gpu);
}
void test_known_turing() {
    fixture = Fixture{};
    fixture.version = extended;
    fixture.mode.fill(9);
    counter(0, 32);
    counter(1, 0);
    dc_gpu* gpu = open();
    CHECK(matched(gpu, 1, 9) == bit(1));
    CHECK(matched(gpu, 3, 9) == 0);  // control record exists but counter has no data
    counter(1, 32);
    CHECK(matched(gpu, 1, 9) == 0);   // same mode, different topology
    dc_gpu_close(gpu);
    fixture.version = compact;
    counter(1, 0);
    gpu = open();
    CHECK(matched(gpu, 1, 9) == 0);
    dc_gpu_close(gpu);
    fixture.version = extended;
    for (const auto other : {ada, blackwell}) {
        fixture.architecture = other;
        gpu = open();
        CHECK(matched(gpu, 1, 9) == 0);
        dc_gpu_close(gpu);
    }
}
void test_known_pascal() {
    fixture = Fixture{};
    fixture.architecture = pascal;
    fixture.version = extended;
    fixture.present = bit(1) | bit(2);
    fixture.mode[1] = 5;
    fixture.mode[2] = 4;
    fixture.has_nvml = true;
    counter(4, 32);
    dc_gpu* gpu = open();
    CHECK(matched(gpu, 1, 5) == 0 && matched(gpu, 2, 4) == 0);
    fixture.memory_mhz = 5702;
    CHECK(matched(gpu, 2, 4) == 0);  // an unrelated or differently scaled private clock
    fixture.clocks.w[2 * 4] = fixture.clocks.w[64 + 7 * 4] = 5702378;
    CHECK(matched(gpu, 2, 4) == bit(4));
    fixture.memory_mhz = 0;
    CHECK(matched(gpu, 2, 4) == 0);   // A and B mirrors are insufficient
    dc_gpu_close(gpu);
}
}
int main() {
    dc_set_export_resolver(resolve, nullptr);
    test_turing_rows(false);
    test_turing_rows(true);
    test_other_layout(turing, extended, true);
    test_other_layout(turing, extended, false);
    test_other_layout(pascal, compact, true);
    test_known_ampere();
    test_known_turing();
    test_known_pascal();
    dc_set_export_resolver(nullptr, nullptr);
    return 0;
}
