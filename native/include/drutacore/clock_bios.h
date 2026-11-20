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
 * Increment C2: the INFO GET through the paired A612 escape transport (_info_once) on that same
 * reader, the pure _decode, and the two-sample read() with its own fresh reader per call.
 *
 * Errors: two Python exception types, told apart by the return status.
 *   - DC_E_FAILED: PerfDiagnosticError(kind, message). error->kind is DC_ERR_MALFORMED (the guarded
 *     allocation was overrun), DC_ERR_IDENTITY (the target changed, or the binding's own identity
 *     refusal) or the binding's DC_ERR_TRANSIENT / DC_ERR_UNSUPPORTED; error->message is Python's.
 *     PerfDiagnosticError.status is None (rm_status 0, has_ntstatus 0).
 *   - DC_E_UNRECOGNIZED: ClockBiosError(message). error->kind is DC_ERR_NONE and error->message is
 *     Python's text.
 * DC_E_ARGUMENT is only for a NULL or malformed argument; DC_E_BUFFER for a payload capacity below
 * DC_CLOCK_BIOS_CONTROL_SIZE (or DC_CLOCK_BIOS_INFO_SIZE), before the driver is touched.
 * No C++ exception crosses these entries: one that reaches an entry (only allocation can throw) is
 * DC_E_FAILED with DC_ERR_TRANSIENT and "Clock-BIOS reader failed: <what>" in *error (when given). */
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

/* COMMAND, EXPORT, SIZE, HEADER, STRIDE, CAPACITY: the INFO GET of the traced v50 BIOS builder. */
#define DC_CLOCK_BIOS_INFO_COMMAND 0x20809019u
#define DC_CLOCK_BIOS_INFO_EXPORT 0x57B5A5DFu /* the NvAPI export whose presence gates the GET */
#define DC_CLOCK_BIOS_INFO_SIZE 12336u        /* parameter bytes (0x3030) */
#define DC_CLOCK_BIOS_INFO_HEADER 0x30u
#define DC_CLOCK_BIOS_INFO_ROW 0x180u
#define DC_CLOCK_BIOS_INFO_CAPACITY 32u

/* _control_shape's requests: {row index: signed kHz} for every masked row of public type 15. */
typedef struct dc_clock_bios_requests {
    uint32_t size;    /* sizeof(dc_clock_bios_requests), set by the caller */
    uint32_t present; /* bit i set: khz[i] is the request of row i (Python's dict key i) */
    int32_t khz[32];  /* the int32 at row + 0x114; 0 where the bit is clear */
} dc_clock_bios_requests;

/* One row of _decode(raw). */
typedef struct dc_clock_bios_row {
    uint32_t type;            /* raw[row] */
    uint32_t base_type;       /* raw[row + 1] */
    uint32_t api_domain_mask; /* the u32 at row + 4 */
    int32_t typed;            /* type == base_type == 16: the two MHz values below are set (else None) */
    int32_t minimum_mhz;      /* the int16 at row + 0x26; 0 when untyped */
    int32_t maximum_mhz;      /* the int16 at row + 0x28; 0 when untyped */
} dc_clock_bios_row;

/* _decode(raw): {str(index): row} for every bit of the u32 mask at offset 4, in index order. */
typedef struct dc_clock_bios_rows {
    uint32_t size;    /* sizeof(dc_clock_bios_rows), set by the caller */
    uint32_t present; /* the mask: bit i set, rows[i] is decoded (Python's key str(i)) */
    dc_clock_bios_row rows[32];
} dc_clock_bios_rows;

/* read(gpu)'s result. available 0 is Python's (None, message), the message in error->message;
 * available 1 the observation {**_LAYOUT, "identity", "info_hex", "user_control", "rows",
 * "stable_reads", "user_control_error"}. */
typedef struct dc_clock_bios_observation {
    uint32_t size;      /* sizeof(dc_clock_bios_observation), set by the caller */
    int32_t available;  /* 1: an observation (the fields below); 0: none */
    dc_target identity; /* slot, architecture, device_id, subsystem_id; handle 0 */
    uint8_t info[12336];               /* info_hex's bytes */
    int32_t has_user_control;          /* 0: user_control is None */
    uint32_t user_control_mask;        /* user_control["mask"] */
    uint8_t user_control[24996];       /* user_control["payload_hex"]'s bytes */
    dc_clock_bios_rows rows;           /* _decode(info) */
    uint32_t stable_reads;             /* always 2 */
    int32_t has_user_control_error;    /* 0: user_control_error is None */
    char user_control_error[1024];     /* its text (truncated as dc_error.message) */
} dc_clock_bios_observation;

/* Python's `_binding(reader) != identity` on the caller's own identity object: return nonzero when
 * the caller's identity differs from *bound (the identity the reader bound: slot, architecture,
 * device_id and subsystem_id; bound->handle is 0, as Python's identity has no handle). Called once by
 * dc_clock_bios_control, after the guards and the binding and before the status check, under the
 * handle lock; it must not call back into the GPU. *bound is valid only during the call. */
typedef int32_t (DC_CALL *dc_clock_bios_identity_fn)(void* context, const dc_target* bound);

/* _control_once(gpu, reader, identity): the ClkDomCtlGet presence, the probed inventory (clkdom_domains)
 * as the request mask, one GET into a guarded 24996-byte packet (version at 0, mask at 8, zeros
 * elsewhere), then in order the guards, the binding, identity_differs(context, &bound) (nonzero:
 * DC_E_FAILED, DC_ERR_IDENTITY, "User-clock request target changed"), the status and _control_shape.
 * identity_differs NULL is an identity that equals no binding (Python's None; the GET is still
 * issued, as Python compares after it). A C caller compares its expected identity in its own
 * callback; the Python binding evaluates `bound != identity` there with Python's semantics. On DC_OK
 * *mask is the request mask and payload[0..24996) the driver's bytes: the Python dict is
 * {**_CONTROL_LAYOUT, "mask": *mask, "payload_hex": payload.hex()}. */
DC_API dc_status DC_CALL dc_clock_bios_control(dc_gpu* gpu, dc_clock_bios_identity_fn identity_differs,
                                               void* context, uint32_t* mask, uint8_t* payload,
                                               uint32_t capacity, dc_error* error);

/* The pure part of _control_shape(value, identity) after the dict-shape checks: _integer on the
 * mask (1..0xFFFFFFFF; pass 0 for a value that is not an int), _hex(payload_hex, 24996), the echoed
 * version and mask words, then the requests into *out. payload_hex holds `length` code points of the
 * Python str, one byte each (a non-ASCII code point as any byte >= 0x80); NULL is a value that is not
 * a str. DC_OK, or DC_E_UNRECOGNIZED with Python's ClockBiosError message. No handle is involved. */
DC_API dc_status DC_CALL dc_clock_bios_control_shape(const char* payload_hex, uint32_t length, uint64_t mask,
                                                     dc_clock_bios_requests* out, dc_error* error);

/* _info_once(gpu, reader) on the handle's reader (the same sticky reader as dc_clock_bios_control):
 * the binding, the NvAPI export DC_CLOCK_BIOS_INFO_EXPORT (absent: DC_ERR_UNSUPPORTED "Clock INFO GET
 * export is unavailable"), reader._transport() (its own transient / unsupported failures), the
 * paired envelope words 0, 1, 3, 4 (DC_ERR_MALFORMED), one escape of a guarded packet of 68 + 12336
 * bytes (the transport header with words 2, 14, 15, 16, 5, 11 set), then in order the binding and the
 * identity (DC_ERR_IDENTITY "Clock INFO target changed during GET"), the guards (DC_ERR_MALFORMED),
 * the escape status (DC_ERR_TRANSIENT "Clock INFO transport failed: <NTSTATUS or None>"), the echoed
 * words 0..15 but 5 and 11 and word 5 in {0, 1} (DC_ERR_MALFORMED), the completion word 11
 * (DC_ERR_TRANSIENT) and the RM status word 16 (0x1F / 0x56 DC_ERR_UNSUPPORTED, else
 * DC_ERR_TRANSIENT, "Clock INFO RM status <decimal>"). The transport cache is not dropped here. On
 * DC_OK *identity is the bound identity (slot, architecture, device_id, subsystem_id; handle 0) and
 * payload[0..12336) the INFO bytes. Takes the handle lock. identity->size must be sizeof(dc_target). */
DC_API dc_status DC_CALL dc_clock_bios_info(dc_gpu* gpu, dc_target* identity, uint8_t* payload, uint32_t capacity,
                                            dc_error* error);

/* _decode(raw): pure, no handle. size other than DC_CLOCK_BIOS_INFO_SIZE is DC_E_UNRECOGNIZED
 * "Clock INFO geometry is not understood". out->size must be sizeof(dc_clock_bios_rows). */
DC_API dc_status DC_CALL dc_clock_bios_decode(const uint8_t* raw, uint32_t size, dc_clock_bios_rows* out,
                                              dc_error* error);

/* read(gpu): a fresh reader for this call (the handle's reader is neither used nor changed), two
 * attempts of two samples each (_info_once, then _control_once on that sample's identity, where a
 * ClockBiosError becomes that sample's user_control_error and a PerfDiagnosticError fails the
 * attempt), the paired-sample comparison ("Clock INFO changed between fresh reads"), and the observation;
 * a user control that was not stable leaves a partial observation and another attempt. A failure of
 * a kind other than transient ends the read; the transport cache is dropped after the first attempt.
 * The reader takes the handle lock per attempt (released between attempts, as Python takes
 * gpu._lock per attempt): this entry does NOT hold it around the read. DC_OK either way: out->available
 * says whether there is an observation; without one error->message is the message and error->kind
 * DC_ERR_NONE. error is required here. out->size must be sizeof(dc_clock_bios_observation). */
DC_API dc_status DC_CALL dc_clock_bios_read(dc_gpu* gpu, dc_clock_bios_observation* out, dc_error* error);

/* A fresh reader: forget the reader's sticky target identity. */
DC_API dc_status DC_CALL dc_clock_bios_reset(dc_gpu* gpu);

/* *held = 1 when the handle's reader holds a sticky target identity (Python's
 * `reader._identity is not None`: set by every binding that succeeded since the last reset), else 0. */
DC_API dc_status DC_CALL dc_clock_bios_held(dc_gpu* gpu, int32_t* held);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_CLOCK_BIOS_H */
