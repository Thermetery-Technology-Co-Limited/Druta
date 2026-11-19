// Druta - Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include <drutacore/drutacore.h>
#include "backend/i2c.h"
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <initializer_list>
using druta::backend::I2cInfoV3;
namespace {
int checks = 0, failures = 0, calls = 0, mode = 0; unsigned bus = 2, device = 3, subsystem = 0x12345678, expected_reg = 0x21, expected_width = 2, expected_value = 0x1234; bool reading = true;
void* gpu_handle = reinterpret_cast<void*>(0x1234);
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; std::fprintf(stderr, "line %d: %s\n", __LINE__, #x); } } while (0)
int DC_CALL init() { return 0; }
int DC_CALL enumerate(void** out, unsigned* count) { out[0] = gpu_handle; *count = 1; return 0; }
int DC_CALL pci(void*, unsigned* id, unsigned* sub, unsigned* rev, unsigned* ext) { *id = 0x567810de; *sub = subsystem; *rev = *ext = 0; return 0; }
int DC_CALL get_bus(void*, unsigned* out) { *out = bus; return 0; }
int DC_CALL get_device(void*, unsigned* out) { *out = device; return 0; }
int DC_CALL transfer(void* selected, I2cInfoV3* p, std::uint32_t* extra) {
    ++calls; CHECK(selected == gpu_handle && p->version == 0x30040 && p->display_mask == 0 && !p->is_ddc_port);
    CHECK(p->address == 0x40 && p->reg && *p->reg == expected_reg && p->reg_size == 1 && p->data && p->data_size == expected_width);
    CHECK(p->speed == 0xffff && !p->speed_khz && p->port == 3 && p->port_set == 1 && extra[0] == 0 && extra[1] == 0);
    if (!reading) for (unsigned i = 0; i < expected_width; ++i) CHECK(p->data[i] == ((expected_value >> (8 * i)) & 255));
    if (mode == 1) p->data_size = expected_width - 1;
    else if (mode == 2) ++*p->reg;
    else if (mode == 3) p->data[0] ^= 1;
    else if (mode == 4) ++bus;
    else if (mode == 5) return -5;
    else if (reading) { const uint8_t source[4] = {0x34, 0x12, 0x56, 0x78}; for (unsigned i = 0; i < expected_width; ++i) p->data[i] = source[i % 4]; }
    return 0;
}
int indexed_transfer(void* selected, I2cInfoV3* p, std::uint32_t* extra) {
    CHECK(selected == gpu_handle && p->reg_size == 2 && p->reg[0] == 0x0a && p->reg[1] == 0x5a);
    CHECK(p->data_size == 1 && p->data[0] == 0xa5 && extra[0] == 0 && extra[1] == 0);
    if (mode == 1) p->reg[1] ^= 1;
    if (mode == 2) extra[0] = 1;
    p->data[0]=0x5a; return 0;
}
int challenge_mode=0, challenge_calls=0;
int DC_CALL challenge_transfer(void* selected, I2cInfoV3* p, std::uint32_t* extra) {
    ++challenge_calls;
    CHECK(selected==gpu_handle && p->reg_size==1 && p->data_size==2);
    const uint8_t seed=challenge_calls==1?0xa5:0x5a;
    CHECK(p->data[0]==seed && p->data[1]==seed);
    if(challenge_mode==1) { p->data[0]=p->data[1]=0xa5; }
    else if(challenge_mode==2) { p->data[0]=p->data[1]=0x5a; }
    else if(challenge_mode==3) { p->data[0]=p->data[1]=0; extra[0]=0x1234; extra[1]=0x5678; }
    else if(challenge_mode==4) return -5;
    else if(challenge_mode==5) --p->data_size;
    return 0;
}
void* DC_CALL query(unsigned id) {
    switch (id) {
    case 0x0150E828u: return reinterpret_cast<void*>(init);
    case 0xE5AC921Fu: return reinterpret_cast<void*>(enumerate);
    case 0x2DDFB66Eu: return reinterpret_cast<void*>(pci);
    case 0x1BE0B8E5u: return reinterpret_cast<void*>(get_bus);
    case 0x2A0A350Fu: return reinterpret_cast<void*>(get_device);
    case 0x4D7B0709u: return mode == 6 ? nullptr : reinterpret_cast<void*>(transfer);
    case 0x283AC65Au: return mode == 6 ? nullptr : reinterpret_cast<void*>(transfer);
    default: return nullptr;
    }
}
void* DC_CALL resolve(void*, const char* library, const char* name) {
    if (std::strcmp(library, "nvapi") != 0) return nullptr;
    if (!*name) return reinterpret_cast<void*>(1);
    return std::strcmp(name, "nvapi_QueryInterface") == 0 ? reinterpret_cast<void*>(query) : nullptr;
}
}
int main() {
    dc_set_export_resolver(resolve, nullptr); dc_gpu* gpu = nullptr;
    CHECK(dc_gpu_open("0000:02:03.0", &gpu) == DC_OK && gpu);
    dc_error error{}; error.size = sizeof(error); uint32_t value = 0xdeadbeef;
    if constexpr (sizeof(void*) == 8) {
        CHECK(dc_i2c_read(gpu, 3, 0x20, 0x21, 2, &value, &error) == DC_OK && value == 0x1234 && calls == 1);
        reading = false; CHECK(dc_i2c_write(gpu, 3, 0x20, 0x21, 2, 0x1234, &error) == DC_OK && calls == 2);
        expected_reg = 0xe1; CHECK(dc_i2c_write(gpu, 3, 0x20, 0xe1, 2, 0x1234, &error) == DC_OK); expected_reg = 0x21;
        reading = true; expected_width = 1; CHECK(dc_i2c_read(gpu, 3, 0x20, 0x21, 1, &value, &error) == DC_OK && value == 0x34);
        expected_width = 3; CHECK(dc_i2c_read(gpu, 3, 0x20, 0x21, 3, &value, &error) == DC_OK && value == 0x561234);
        expected_width = 4; CHECK(dc_i2c_read(gpu, 3, 0x20, 0x21, 4, &value, &error) == DC_OK && value == 0x78561234); expected_width = 2;
        uint8_t block[32]{}; expected_width=32;
        CHECK(dc_i2c_read_bytes(gpu,3,0x20,0x21,block,32,&error)==DC_OK && block[0]==0x34 && block[31]==0x78);
        mode=1; block[0]=0xa5; CHECK(dc_i2c_read_bytes(gpu,3,0x20,0x21,block,32,&error)==DC_E_FAILED && block[0]==0xa5);
        mode=0; expected_width=2; reading=false;
        const uint8_t pair[]{0x34,0x12}; expected_reg=0x22;
        CHECK(dc_i2c_controller_write(gpu,6,3,0x20,0x22,pair,2,&error)==DC_OK);
        const int typed_before=calls;
        CHECK(dc_i2c_controller_write(gpu,6,3,0x20,0x23,pair,2,&error)==DC_E_ARGUMENT && calls==typed_before);
        CHECK(dc_i2c_controller_write(gpu,6,3,0x20,0x22,pair,1,&error)==DC_E_ARGUMENT && calls==typed_before);
        CHECK(dc_i2c_read_bytes(gpu,3,0x20,0x21,block,33,&error)==DC_E_ARGUMENT && calls==typed_before);
        expected_reg=0x21; reading=true;
        const int before = calls;
        for (uint32_t reg : {4u, 0x11u, 0x12u, 0x13u, 0x14u, 0x15u, 0x16u, 0x17u, 0x18u}) CHECK(dc_i2c_write(gpu, 3, 0x20, reg, 2, 0, &error) != DC_OK && calls == before);
        CHECK(dc_i2c_write(gpu, 3, 0x20, 0x21, 1, 256, &error) == DC_E_ARGUMENT && calls == before);
        CHECK(dc_i2c_read(gpu, 256, 0x20, 0x21, 2, &value, &error) == DC_E_ARGUMENT && calls == before);
        CHECK(dc_i2c_read(gpu, 3, 128, 0x21, 2, &value, &error) == DC_E_ARGUMENT && calls == before);
        CHECK(dc_i2c_read(gpu, 3, 0x20, 0x21, 0, &value, &error) == DC_E_ARGUMENT && calls == before);
        value = 0xdeadbeef; mode = 1; CHECK(dc_i2c_read(gpu, 3, 0x20, 0x21, 2, &value, &error) == DC_E_FAILED && value == 0xdeadbeef && error.kind == DC_ERR_MALFORMED);
        mode = 2; CHECK(dc_i2c_read(gpu, 3, 0x20, 0x21, 2, &value, &error) == DC_E_FAILED && error.kind == DC_ERR_MALFORMED);
        reading = false; mode = 3; CHECK(dc_i2c_write(gpu, 3, 0x20, 0x21, 2, 0x1234, &error) == DC_E_FAILED && error.kind == DC_ERR_MALFORMED);
        mode = 4; CHECK(dc_i2c_write(gpu, 3, 0x20, 0x21, 2, 0x1234, &error) == DC_E_FAILED && error.kind == DC_ERR_IDENTITY); bus = 2;
        reading = true; mode = 5; CHECK(dc_i2c_read(gpu, 3, 0x20, 0x21, 2, &value, &error) == DC_E_FAILED && error.kind == DC_ERR_TRANSIENT);
        mode = 6; const int absent = calls; CHECK(dc_i2c_read(gpu, 3, 0x20, 0x21, 2, &value, &error) == DC_E_UNAVAILABLE && calls == absent);
        mode = 0; device = 4; CHECK(dc_i2c_write(gpu, 3, 0x20, 0x21, 2, 0x1234, &error) == DC_E_UNAVAILABLE && calls == absent); device = 3;
        subsystem++; CHECK(dc_i2c_read(gpu, 3, 0x20, 0x21, 2, &value, &error) == DC_E_UNAVAILABLE && calls == absent); subsystem--;
    } else CHECK(dc_i2c_read(gpu, 3, 0x20, 0x21, 2, &value, &error) == DC_E_UNAVAILABLE && calls == 0);
    // Read-only output challenges distinguish an untouched successful call
    // from a genuine all-A5 response without retrying writes or malformed I/O.
    uint8_t challenge[2]{0xcc,0xdd};
    challenge_mode=0; challenge_calls=0;
    auto challenged=druta::backend::i2c_exchange_bytes(challenge_transfer,gpu_handle,3,0x20,0x21,challenge,2,false);
    CHECK(challenged.geometry_ok && challenged.status!=0 && challenge_calls==2 && challenge[0]==0xcc && challenge[1]==0xdd);
    challenge_mode=0; challenge_calls=0; challenge[0]=challenge[1]=0xa5;
    challenged=druta::backend::i2c_exchange_bytes(challenge_transfer,gpu_handle,3,0x20,0x21,challenge,2,true);
    CHECK(challenged.geometry_ok && challenged.status==0 && challenge_calls==1 && challenge[0]==0xa5 && challenge[1]==0xa5);
    challenge_mode=1; challenge_calls=0;
    challenged=druta::backend::i2c_exchange_bytes(challenge_transfer,gpu_handle,3,0x20,0x21,challenge,2,false);
    CHECK(challenged.geometry_ok && challenged.status==0 && challenge_calls==2 && challenge[0]==0xa5 && challenge[1]==0xa5);
    challenge_mode=2; challenge_calls=0;
    challenged=druta::backend::i2c_exchange_bytes(challenge_transfer,gpu_handle,3,0x20,0x21,challenge,2,false);
    CHECK(challenged.geometry_ok && challenged.status==0 && challenge_calls==1 && challenge[0]==0x5a && challenge[1]==0x5a);
    challenge_mode=3; challenge_calls=0;
    challenged=druta::backend::i2c_exchange_bytes(challenge_transfer,gpu_handle,3,0x20,0x21,challenge,2,false);
    CHECK(challenged.geometry_ok && challenged.status==0 && challenge_calls==1 && challenge[0]==0 && challenge[1]==0);
    challenge_mode=4; challenge_calls=0; challenge[0]=0xcc; challenge[1]=0xdd;
    challenged=druta::backend::i2c_exchange_bytes(challenge_transfer,gpu_handle,3,0x20,0x21,challenge,2,false);
    CHECK(challenged.geometry_ok && challenged.status==-5 && challenge_calls==1 && challenge[0]==0xcc && challenge[1]==0xdd);
    challenge_mode=5; challenge_calls=0;
    challenged=druta::backend::i2c_exchange_bytes(challenge_transfer,gpu_handle,3,0x20,0x21,challenge,2,false);
    CHECK(!challenged.geometry_ok && challenge_calls==1 && challenge[0]==0xcc && challenge[1]==0xdd);
    uint8_t indexed_byte=0x5a;
    mode=0; CHECK(druta::backend::i2c_exchange_bytes(indexed_transfer,gpu_handle,3,0x20,0x0a,&indexed_byte,1,true,true).geometry_ok);
    mode=1; CHECK(!druta::backend::i2c_exchange_bytes(indexed_transfer,gpu_handle,3,0x20,0x0a,&indexed_byte,1,true,true).geometry_ok);
    mode=2; CHECK(!druta::backend::i2c_exchange_bytes(indexed_transfer,gpu_handle,3,0x20,0x0a,&indexed_byte,1,true,true).geometry_ok);
    mode=0;
    const uint8_t gate[]{4,0xc6,0xeb,0xea,0x15}, badgate[]{4,0xc6,0xeb,0xea,0};
    CHECK(druta::backend::i2c_controller_payload(4,0xb2,gate,5));
    CHECK(!druta::backend::i2c_controller_payload(4,0xb2,badgate,5));
    const uint8_t setting[]{1,1};
    CHECK(druta::backend::i2c_controller_payload(4,0x12,setting,2));
    CHECK(!druta::backend::i2c_controller_payload(4,0x14,setting,2));
    for (unsigned family=1;family<=6;++family) CHECK(!druta::backend::i2c_controller_payload(family,4,setting,2));
    dc_gpu_close(gpu);
    if constexpr (sizeof(void*) == 8) { bus = 11; device = 7; subsystem = 0xaabbccddu; gpu_handle = reinterpret_cast<void*>(0x9876); gpu = nullptr; mode = 0; reading = true; CHECK(dc_gpu_open("0000:0b:07.0", &gpu) == DC_OK && gpu); value = 0; CHECK(dc_i2c_read(gpu, 3, 0x20, 0x21, 2, &value, &error) == DC_OK && value == 0x1234); dc_gpu_close(gpu); }
    dc_set_export_resolver(nullptr, nullptr);
    std::printf("i2c_test: %d checks, %d failures\n", checks, failures); return failures ? 1 : 0;
}
