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
 * Increment P2: the pure decode_group (dc_perf_decode_group), one group of the bounded read loop
 * (dc_perf_read_group) and the embedded SHA-256 (dc_sha256_hex).
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
 * DC_E_ARGUMENT is only for a NULL or malformed argument, and for a transport dc_perf_get cannot
 * use (fields == NULL, Python's None, or more header words than the packet holds) once the checks
 * Python runs before building the packet (the rules and the first binding) have passed: Python
 * raises TypeError (unpacking None) or IndexError (ctypes' invalid index) there. DC_E_BUFFER for an
 * allowed size larger than capacity, before the driver is touched.
 * No C++ exception crosses this ABI: an exception inside any dc_perf_* or dc_sha256_hex call (an
 * allocation failure, for example) is DC_E_FAILED, with kind DC_ERR_TRANSIENT and the message
 * "PERF reader failed: <what>" ("PERF reader failed" for a non-standard exception) when a dc_error
 * is given. Any handle lock it held is released; *out is then unspecified. dc_perf_read_group then
 * reports no group result, so this is not one of the loop's per-group failures.
 * Messages longer than dc_error.message (1023 bytes and a NUL) are truncated: only the
 * unknown-group text of a long name reaches that limit. */
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

/* _binding() alone on the reader's sticky identity: DC_OK, or DC_E_FAILED with the
 * PerfDiagnosticError (DC_ERR_IDENTITY, DC_ERR_TRANSIENT or DC_ERR_UNSUPPORTED). A binding that
 * must run before an exception of the caller's own (Python's _get binds before it unpacks the
 * transport and builds the packet) runs this first and raises its error instead, if any. */
DC_API dc_status DC_CALL dc_perf_bind(dc_gpu* gpu, dc_error* error);

/* _get(command, size, transport, mask): one read-only PERF GET through the caller's transport.
 * The transport is the (header, fields) pair after the ctypes marshalling Python's packet build
 * does: header[0..header_count) are the words as stored into the c_uint32 packet (already reduced
 * modulo 2^32), copied from packet word 0 into a zeroed packet of 17 + size / 4 words, so a shorter
 * header leaves the remaining words 0 and a word past 16 lands in the payload; words 2, 14, 15
 * and 16 are then set to the packet bytes, command, size and 0. fields are the escape fields
 * (hAdapter is replaced by the paired adapter). fields == NULL is Python's None (header_count must
 * then be 0). What dc_perf_transport returned is passed as its 17 words and fields.
 * has_mask is DC_PERF_MASK_*: BYTES passes mask[0..mask_size); OTHER is a value that is not bytes
 * (refused for CONTROL, as any mask is for INFO). On DC_OK out[0..size) holds the response bytes
 * after the 68-byte header. A transport failure (a non-zero or missing NTSTATUS) drops the cached
 * power transport and layout choice. Order: the allow-set and mask rules, the binding, the
 * transport (DC_E_ARGUMENT, above), the escape, the binding again, then the NTSTATUS, RM status
 * and echo checks. */
DC_API dc_status DC_CALL dc_perf_get(dc_gpu* gpu, uint32_t command, uint32_t size, const uint32_t* header,
                                     uint32_t header_count, const dc_escape_fields* fields, const uint8_t* mask,
                                     uint32_t mask_size, int32_t has_mask, uint8_t* out, uint32_t capacity,
                                     dc_error* error);

/* A fresh reader: forget the reader's sticky target identity. */
DC_API dc_status DC_CALL dc_perf_reset(dc_gpu* gpu);


/* ---- increment P2: decode_group and the read loop ---------------------------------------------- */

#define DC_PERF_MAX_ENTRIES 512        /* the largest capacity (limits) */
#define DC_PERF_MAX_INFO_STRIDE 0x350  /* the largest INFO row (cf_controller) */
#define DC_PERF_MAX_CONTROL_STRIDE 0x2C4 /* the largest CONTROL row (cf_controller) */
#define DC_PERF_MAX_MASK_BYTES 64
#define DC_PERF_MAX_FLAGS 3
#define DC_PERF_TARGET_COUNT 3
#define DC_PERF_ISSUE_CHARS 64
#define DC_PERF_LABEL_CHARS 32

/* One "entries" row. The issue texts are Python's fixed strings; at most one is ever appended. */
typedef struct dc_perf_entry {
    uint32_t index;
    uint32_t type, control_type;           /* INFO and CONTROL row byte 0 */
    uint32_t info_bytes, control_bytes;    /* the row lengths (the group's strides) */
    int32_t has_driver_label;              /* 0: driver_label is None */
    char driver_label[DC_PERF_LABEL_CHARS];
    uint32_t issue_count;                  /* 0 or 1 */
    char issue[DC_PERF_ISSUE_CHARS];
    int32_t has_policy;                    /* role_bytes_raw, role_id and controller_mask are present */
    uint32_t role_bytes_raw[2];
    uint32_t role_id;
    uint32_t controller_mask;
    int32_t has_registry_flag;             /* registry_controlled_flag_raw is present */
    uint32_t registry_controlled_flag_raw;
    uint8_t info_raw[DC_PERF_MAX_INFO_STRIDE];       /* info_hex: the first info_bytes */
    uint8_t control_raw[DC_PERF_MAX_CONTROL_STRIDE]; /* control_hex: the first control_bytes */
} dc_perf_entry;

/* One "flags" item, in Python's dict order: label -> {"raw", "boolean"}. */
typedef struct dc_perf_flag {
    char label[DC_PERF_LABEL_CHARS];
    uint32_t raw;
    int32_t has_boolean;  /* 0: boolean is None (raw is not 0 or 1) */
    int32_t boolean;
} dc_perf_flag;

/* One "registry_targets" item (registry_name is always "RmPerfCfPolicyOverrides"). */
typedef struct dc_perf_target {
    char field[DC_PERF_LABEL_CHARS];  /* bit0, bits2_1, bit3 */
    int32_t has_index;                /* 0: index is None (0xFFFF) */
    uint32_t index;
    int32_t present;
    int32_t has_flag_raw;
    uint32_t flag_raw;
    int32_t has_issue;
    char issue[DC_PERF_ISSUE_CHARS];
} dc_perf_target;

/* decode_group's dict without slot, architecture and captured_at (the caller adds those). group,
 * capacity, info_command, control_command, info_bytes and control_bytes are the layout's. */
typedef struct dc_perf_snapshot {
    uint32_t size;  /* sizeof(dc_perf_snapshot), set by the caller */
    char group[DC_PERF_NAME_CHARS];
    uint32_t capacity, mask_bytes;
    uint8_t mask[DC_PERF_MAX_MASK_BYTES];  /* mask_hex: the first mask_bytes */
    uint32_t info_command, control_command, info_bytes, control_bytes;
    char info_sha256[65];
    char control_sha256[65];
    int32_t has_control_sha256;  /* 0: control_sha256 is None (no CONTROL) */
    uint32_t flag_count;
    dc_perf_flag flags[DC_PERF_MAX_FLAGS];
    uint32_t target_count;  /* 0 or DC_PERF_TARGET_COUNT */
    dc_perf_target targets[DC_PERF_TARGET_COUNT];
    uint32_t issue_count;
    char issues[DC_PERF_MAX_FLAGS][DC_PERF_ISSUE_CHARS];
    uint32_t entry_count;  /* present_count */
    dc_perf_entry entries[DC_PERF_MAX_ENTRIES];
} dc_perf_snapshot;

/* decode_group(name, info, control): pure, no driver call. control == NULL with has_control == 0 is
 * Python's None. DC_OK fills *out; DC_E_FAILED fills *error with _layout's ValueError (kind
 * DC_ERR_INVALID, "Unknown PERF diagnostic group " + an ASCII repr of the name, truncated at
 * 1023 bytes for a long name) or the PerfDiagnosticError (kind DC_ERR_MALFORMED) with Python's
 * message. */
DC_API dc_status DC_CALL dc_perf_decode_group(const char* name, const uint8_t* info, size_t info_size,
                                              const uint8_t* control, size_t control_size, int32_t has_control,
                                              dc_perf_snapshot* out, dc_error* error);

enum { DC_PERF_STATUS_READY = 0, DC_PERF_STATUS_EMPTY = 1, DC_PERF_STATUS_FAILED = 2 };

/* One group of PerfDiagnosticsReader.read, on the handle's reader. */
typedef struct dc_perf_group_result {
    uint32_t size;      /* sizeof(dc_perf_group_result), set by the caller */
    int32_t status;     /* DC_PERF_STATUS_*: FAILED carries the error's kind in *error */
    uint32_t attempts;  /* Python's _attempt after the loop */
    int32_t has_target; /* a binding succeeded: slot and architecture update the report */
    char slot[32];
    uint32_t architecture;
    dc_perf_snapshot snapshot;  /* READY / EMPTY only; its size is set by the caller */
} dc_perf_group_result;

/* The attempts loop of read() for group `name`, attempts in 1..3: per attempt, under the handle
 * lock (taken per attempt, as Python takes gpu._lock per attempt; it is released between attempts):
 * the binding, the presence of both NvAPI getter exports (DC_ERR_UNSUPPORTED "PERF getter exports
 * are unavailable"), _transport, the INFO GET, its mask, the CONTROL GET only when a mask byte is
 * set, and decode_group. A DC_ERR_TRANSIENT failure retries; any other kind stops. DC_OK always
 * reports the group (FAILED with *error filled as dc_perf_get does); DC_E_FAILED with kind
 * DC_ERR_INVALID is _layout's ValueError for an unknown name or attempts outside 1..3 ("PERF read
 * attempts must be between 1 and 3"), before anything is read. */
DC_API dc_status DC_CALL dc_perf_read_group(dc_gpu* gpu, const char* name, uint32_t attempts,
                                            dc_perf_group_result* out, dc_error* error);

/* hashlib.sha256(data).hexdigest(): 64 lowercase hex characters and a NUL into out. */
DC_API dc_status DC_CALL dc_sha256_hex(const uint8_t* data, size_t size, char out[65]);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_PERF_DIAGNOSTICS_H */
