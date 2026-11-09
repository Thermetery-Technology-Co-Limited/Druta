/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The PERF diagnostics reader (perf_diagnostics.PerfDiagnosticsReader): dc_perf_*.
 * Included by drutacore.h; do not include directly.
 *
 * Increment P1: the pure group layouts (LAYOUTS, PROBE_ARCHITECTURES, _GET_COMMANDS) and the
 * reader's _transport and _get on a handle. Each dc_gpu keeps one reader with its own sticky
 * target binding (the Python reader's _identity); dc_perf_reset is a fresh reader.
 *
 * Errors: DC_E_FAILED fills the dc_error with the exception Python raises.
 *   - kind DC_ERR_INVALID: a plain ValueError raised before the driver was touched (the allow-set
 *     and request-mask rules of _get). Not a PerfDiagnosticError.
 *   - kind DC_ERR_TRANSIENT, DC_ERR_UNSUPPORTED, DC_ERR_IDENTITY, DC_ERR_MALFORMED: a
 *     PerfDiagnosticError of that kind, with Python's message.
 *   PerfDiagnosticError.status (one attribute in Python) is carried as: the NTSTATUS of a failed
 *   escape in ntstatus (has_ntstatus = 1, ntstatus != 0); the RM status of a rejection in rm_status
 *   (rm_status != 0). Every other error has rm_status = 0 and has_ntstatus = 0 or ntstatus = 0,
 *   which is Python's None. An escape that could not be issued at all is Python's
 *   "PERF GET transport failed (NTSTATUS None)" with status None (has_ntstatus = 0), as
 *   nvbackend.GPU._legacy_clk_escape returns None there.
 * DC_E_ARGUMENT is only for a NULL or malformed argument, and for transport->captured == 0 once the
 * checks Python runs before unpacking the transport (the rules and the first binding) have passed:
 * Python's tuple unpacking of None raises TypeError there. DC_E_BUFFER for an allowed size larger
 * than capacity, before the driver is touched. */
#ifndef DRUTACORE_PERF_DIAGNOSTICS_H
#define DRUTACORE_PERF_DIAGNOSTICS_H

#ifdef __cplusplus
extern "C" {
#endif

#define DC_PERF_GROUP_COUNT 3
#define DC_PERF_NAME_CHARS 16

/* One perf_diagnostics.GroupLayout with its LAYOUTS key. */
typedef struct dc_perf_layout {
    uint32_t size; /* sizeof(dc_perf_layout), set by the caller */
    char name[DC_PERF_NAME_CHARS];
    uint32_t info_id, control_id;           /* NvAPI getter interface ids */
    uint32_t info_command, control_command; /* RM GET commands */
    uint32_t info_size, control_size;       /* parameter bytes of each GET */
    uint32_t capacity, mask_bytes;
    uint32_t info_offset, info_stride, control_offset, control_stride;
} dc_perf_layout;

/* LAYOUTS in Python's order (limits, cf_controller, cf_policy). At most capacity rows are written
 * (each row's size set by the caller); *count is the total. */
DC_API dc_status DC_CALL dc_perf_layouts(dc_perf_layout* out, uint32_t capacity, uint32_t* count);
/* architecture in PROBE_ARCHITECTURES: 1 or 0. */
DC_API int32_t DC_CALL dc_perf_probe_architecture(uint32_t architecture);
/* (command, size) in _GET_COMMANDS: 1 or 0. */
DC_API int32_t DC_CALL dc_perf_get_allowed(uint32_t command, uint32_t size);

/* _transport(): GPU._current_limit_transport_layouts first (CurrentLimitUnsupported is
 * DC_ERR_UNSUPPORTED, any other failure DC_ERR_TRANSIENT, with its message), then the cached
 * power transport must be the 17-word A612 header whose word 2 is 68 + word 15 (else
 * DC_ERR_UNSUPPORTED; an RM26 transport is refused). DC_OK with *out, or DC_E_FAILED. */
DC_API dc_status DC_CALL dc_perf_transport(dc_gpu* gpu, dc_rm_transport* out, dc_error* error);

/* _get(command, size, transport, mask)'s request mask, as Python sees the argument. */
enum { DC_PERF_MASK_NONE = 0, DC_PERF_MASK_BYTES = 1, DC_PERF_MASK_OTHER = 2 };

/* _get(command, size, transport, mask): one read-only PERF GET through the caller's transport
 * (what dc_perf_transport returned, or an altered copy). has_mask is DC_PERF_MASK_*: BYTES passes
 * mask[0..mask_size); OTHER is a value that is not bytes (refused for CONTROL, as any mask is for
 * INFO). On DC_OK out[0..size) holds the response bytes after the 68-byte header. A transport
 * failure (a non-zero or missing NTSTATUS) drops the cached power transport and layout choice. */
DC_API dc_status DC_CALL dc_perf_get(dc_gpu* gpu, uint32_t command, uint32_t size, const dc_rm_transport* transport,
                                     const uint8_t* mask, uint32_t mask_size, int32_t has_mask, uint8_t* out,
                                     uint32_t capacity, dc_error* error);

/* A fresh reader: forget the reader's sticky target identity. */
DC_API dc_status DC_CALL dc_perf_reset(dc_gpu* gpu);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_PERF_DIAGNOSTICS_H */
