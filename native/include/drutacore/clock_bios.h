/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The clock-BIOS reader (clock_bios_runtime's read path): dc_clock_bios_*.
 * Included by drutacore.h; do not include directly.
 *
 * Increment C1: the raw guarded user-clock CONTROL GET (_control_once) on a handle and the pure
 * _control_shape. Each dc_gpu keeps one reader with its own sticky target binding (the
 * PerfDiagnosticsReader a Python read() constructs); dc_clock_bios_reset is a fresh reader.
 *
 * Errors: two Python exception types, told apart by the return status.
 *   - DC_E_FAILED: PerfDiagnosticError(kind, message). error->kind is DC_ERR_MALFORMED (the guarded
 *     allocation was overrun), DC_ERR_IDENTITY (the target changed, or the binding's own identity
 *     refusal) or the binding's DC_ERR_TRANSIENT / DC_ERR_UNSUPPORTED; error->message is Python's.
 *     PerfDiagnosticError.status is None (rm_status 0, has_ntstatus 0).
 *   - DC_E_UNRECOGNIZED: ClockBiosError(message). error->kind is DC_ERR_NONE and error->message is
 *     Python's text.
 * DC_E_ARGUMENT is only for a NULL or malformed argument; DC_E_BUFFER for a payload capacity below
 * DC_CLOCK_BIOS_CONTROL_SIZE, before the driver is touched. */
#ifndef DRUTACORE_CLOCK_BIOS_H
#define DRUTACORE_CLOCK_BIOS_H

#ifdef __cplusplus
extern "C" {
#endif

/* _CONTROL_LAYOUT: the understood NVAPI user-clock CONTROL wrapper. */
#define DC_CLOCK_BIOS_CONTROL_VERSION 0x261A4u
#define DC_CLOCK_BIOS_CONTROL_SIZE 24996u /* parameter bytes (0x61A4) */
#define DC_CLOCK_BIOS_CONTROL_HEADER 0x124u
#define DC_CLOCK_BIOS_CONTROL_ROW 0x304u
#define DC_CLOCK_BIOS_CONTROL_CAPACITY 32u

/* _control_shape's requests: {row index: signed kHz} for every masked row of public type 15. */
typedef struct dc_clock_bios_requests {
    uint32_t size;    /* sizeof(dc_clock_bios_requests), set by the caller */
    uint32_t present; /* bit i set: khz[i] is the request of row i (Python's dict key i) */
    int32_t khz[32];  /* the int32 at row + 0x114; 0 where the bit is clear */
} dc_clock_bios_requests;

/* _control_once(gpu, reader, identity): the ClkDomCtlGet presence, the probed inventory (clkdom_domains)
 * as the request mask, one GET into a guarded 24996-byte packet (version at 0, mask at 8, zeros
 * elsewhere), then in order the guards, the binding (compared with *identity on slot, architecture,
 * device_id and subsystem_id; the handle field is ignored), the status and _control_shape.
 * identity NULL is a value that equals no binding (Python compares it after the GET, so the GET is
 * still issued). On DC_OK *mask is the request mask and payload[0..24996) the driver's bytes: the
 * Python dict is {**_CONTROL_LAYOUT, "mask": *mask, "payload_hex": payload.hex()}. */
DC_API dc_status DC_CALL dc_clock_bios_control(dc_gpu* gpu, const dc_target* identity, uint32_t* mask,
                                               uint8_t* payload, uint32_t capacity, dc_error* error);

/* The pure part of _control_shape(value, identity) after the dict-shape checks: _integer on the
 * mask (1..0xFFFFFFFF; pass 0 for a value that is not an int), _hex(payload_hex, 24996), the echoed
 * version and mask words, then the requests into *out. payload_hex holds `length` code points of the
 * Python str, one byte each (a non-ASCII code point as any byte >= 0x80); NULL is a value that is not
 * a str. DC_OK, or DC_E_UNRECOGNIZED with Python's ClockBiosError message. No handle is involved. */
DC_API dc_status DC_CALL dc_clock_bios_control_shape(const char* payload_hex, uint32_t length, uint64_t mask,
                                                     dc_clock_bios_requests* out, dc_error* error);

/* A fresh reader: forget the reader's sticky target identity. */
DC_API dc_status DC_CALL dc_clock_bios_reset(dc_gpu* gpu);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_CLOCK_BIOS_H */
