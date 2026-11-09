/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The client-power controller on a dc_gpu handle (client_power_runtime.ClientPowerController):
 * dc_cpc_*. Included by drutacore.h; do not include directly.
 *
 * Increment A3: the paired transport check (_transport), the guarded single-command exchange
 * (_exchange), _reset_transport, a fresh controller, and the controller state. Increment A4: the
 * metadata read (read_metadata: _stable_locked over _metadata_once and _client_group). One sticky
 * controller per handle, with its own target binding. A ClientPowerError is DC_E_FAILED with a
 * dc_error: kind DC_ERR_IDENTITY / TRANSIENT / UNSUPPORTED / MALFORMED, Python's message, and
 * rm_status (nonzero only for an RM rejection); DC_ERR_UNSTABLE for two differing fresh reads. DC_ERR_INVALID is the one bare ValueError Python
 * raises (see dc_cpc_exchange) and the caller stops dc_cpc_exchange documents. DC_E_ARGUMENT only
 * for a NULL or malformed argument. No C++ exception crosses this ABI: one is reported as
 * DC_E_FAILED, with a transient *error where the call takes one. */
#ifndef DRUTACORE_CLIENT_POWER_CONTROLLER_H
#define DRUTACORE_CLIENT_POWER_CONTROLLER_H

#ifdef __cplusplus
extern "C" {
#endif

/* drutacore.h includes this header before it defines dc_rm_transport; the tag is completed there. */

typedef struct dc_cpc_state {
    uint32_t size;          /* sizeof(dc_cpc_state), set by the caller */
    int32_t sizes;          /* self.sizes: a DC_CP_*_SIZES group of drutacore/client_power.h */
    int32_t legacy;         /* _legacy */
    int32_t native_policy124;  /* _native_policy124 */
    int32_t role_info216;   /* _role_info216 */
    int32_t rm26_records;   /* _rm26_records */
    int32_t rm26;           /* _rm26 */
    int32_t rm26_extended;  /* _rm26_extended */
    int32_t has_transport_key;     /* _transport_key is not None */
    uint32_t transport_key[3];     /* (header[14], header[2], header[15]) */
} dc_cpc_state;

/* _transport: the paired power transport, checked and keyed. DC_OK with *out (captured = 1,
 * header_count 17), or DC_E_FAILED with *error. */
DC_API dc_status DC_CALL dc_cpc_transport(dc_gpu* gpu, dc_rm_transport* out, dc_error* error);

/* dc_cpc_exchange flags: Python's own failures inside _exchange that only the caller can see, each
 * refused at its own point (after the binding and the transport, before any dispatch) with
 * DC_E_FAILED, kind DC_ERR_INVALID and the matching DC_CPC_STOP_* message. A C caller with a plain
 * byte payload passes 0. */
#define DC_CPC_COMMAND_NOT_STORABLE 1u /* `packet[14] = command` raises (a non-integer equal to a known
                                         command): refused there, unless the RM26 table supplies the
                                         wire command, in which case the exchange goes on */
#define DC_CPC_PAYLOAD_NOT_STORABLE 2u /* `buffer[68:] = payload` raises after its length check */
#define DC_CPC_CALLER_ECHO 4u          /* the caller compares the SET echo with its own payload object
                                         (Python's `result != payload`); the core then returns the
                                         response bytes without comparing them */
#define DC_CPC_STOP_COMMAND "caller stop: the command word"
#define DC_CPC_STOP_PAYLOAD "caller stop: the payload bytes"
#define DC_CPC_STOP_DISPATCH "caller stop: the dispatch"

/* Python's `if write is not None: write.dispatched = True`, called once right before the escape
 * (after every check that precedes it in Python). Nonzero refuses the dispatch: DC_E_FAILED,
 * DC_ERR_INVALID, DC_CPC_STOP_DISPATCH, nothing dispatched. It runs under the GPU lock; it must not
 * call back into this GPU. */
typedef int32_t (DC_CALL *dc_cpc_dispatch_fn)(void* context);

/* _exchange(command, payload, write=...): one exact command and one escape. write is nonzero when
 * a _Write is given (isinstance(write, _Write)); before_dispatch (may be NULL) stands for the write
 * object being marked. flags: the DC_CPC_* bits above.
 *
 * *dispatched is set on every return that has a non-NULL dispatched, DC_E_ARGUMENT included: 1 once
 * the packet was handed to the escape, whatever followed (a failure, or a C++ exception reported as
 * a transient DC_E_FAILED), else 0.
 *
 * capacity: the room in out. Once the command and the geometry are understood, a capacity below
 * the command's size in the current table returns DC_E_BUFFER (no *error written) before any
 * binding, transport or dispatch. A response that does not fit therefore never follows a dispatch:
 * the size table can change only through the transport, and a payload sized for the old table then
 * fails its length check before the escape.
 *
 * DC_OK with the `size` response bytes in out, or DC_E_FAILED with *error. DC_ERR_INVALID with
 * "Can only assign sequence of same size" is the bare ValueError Python raises when _transport
 * switched the size table under the payload. DC_E_ARGUMENT for a NULL handle, payload (with size
 * nonzero), out (with capacity nonzero) or dispatched, an unknown flag, or a dc_error whose size is
 * wrong. */
DC_API dc_status DC_CALL dc_cpc_exchange(dc_gpu* gpu, uint32_t command, const uint8_t* payload, uint32_t size,
                                         int32_t write, uint32_t flags, dc_cpc_dispatch_fn before_dispatch,
                                         void* context, uint8_t* out, uint32_t capacity, int32_t* dispatched,
                                         dc_error* error);

/* _reset_transport: clear the handle's cached power transport and its layout choice. */
DC_API dc_status DC_CALL dc_cpc_reset_transport(dc_gpu* gpu);
/* A fresh controller: every attribute back to __init__, the binding's sticky identity included. */
DC_API dc_status DC_CALL dc_cpc_reset(dc_gpu* gpu);
/* The controller's attributes. DC_E_ARGUMENT for a NULL out or a wrong out->size. */
DC_API dc_status DC_CALL dc_cpc_state_get(dc_gpu* gpu, dc_cpc_state* out);

/* The largest INFO, ROLE_INFO and NATIVE_INFO packet of any DC_CP_*_SIZES group (SIZES). */
#define DC_CPC_INFO_MAX 2340
#define DC_CPC_ROLE_INFO_MAX 264
#define DC_CPC_NATIVE_INFO_MAX 20512

/* read_metadata()'s dict. binding.layout is DC_CP_ABSENT, DC_CP_LAYOUT_LEGACY32 or DC_CP_LAYOUT_RM26;
 * binding.native_layout DC_CP_ABSENT, DC_CP_NATIVE_POLICY124 or DC_CP_NATIVE_RM26_EXTENDED (the
 * enums of drutacore/client_power.h). Present keys appear in Python's order: layout, native_layout,
 * native_info_stride. The payloads hold info_size / role_info_size / native_info_size bytes. */
typedef struct dc_cpc_metadata {
    uint32_t size;          /* sizeof(dc_cpc_metadata), set by the caller */
    int32_t schema;         /* 1 */
    int32_t stable_reads;   /* 2 */
    char slot[24];          /* identity: Target::slot, never truncated. A successful read binds only a
                             * slot that parses with PCI domain 0 and equals the NVAPI adapter's
                             * address, and Gpu::slot() then returns an adapter-derived format_slot
                             * text ("0000:bb:dd.f", 12 characters; the requested text is reached
                             * only when no adapter slot exists, which the binding refuses). */
    uint32_t architecture;
    uint32_t device_id;
    uint32_t subsystem_id;
    uint32_t client_index;  /* binding */
    uint32_t native_index;
    uint32_t semantic_role; /* ROLE */
    uint32_t client;        /* CLIENT */
    char units[4];          /* "mW" */
    uint32_t minimum_mw;
    uint32_t rated_mw;
    uint32_t maximum_mw;
    int32_t layout;
    int32_t native_layout;
    int32_t has_native_info_stride; /* recorded only when it differs from the layout's usual stride */
    uint32_t native_info_stride;
    uint32_t info_size;
    uint32_t role_info_size;
    uint32_t native_info_size;
    uint8_t info[DC_CPC_INFO_MAX];
    uint8_t role_info[DC_CPC_ROLE_INFO_MAX];
    uint8_t native_info[DC_CPC_NATIVE_INFO_MAX];
} dc_cpc_metadata;

/* read_metadata(): two equal fresh metadata captures under the GPU lock, one fresh pair after a
 * transient failure (the handle's power transport cache is reset before the retry and after a
 * second transient failure). DC_OK with *out, or DC_E_FAILED with *error: the ClientPowerError of
 * the failing step, DC_ERR_UNSTABLE when the two captures differ. The controller state keeps every
 * group the captures selected. DC_E_ARGUMENT for a NULL handle or out, a wrong out->size, or a
 * dc_error whose size is wrong; *error and *out are then left untouched.
 *
 * DC_E_BUFFER (neither *error nor *out written; the controller state keeps the groups selected)
 * when a stable capture holds a payload larger than DC_CPC_INFO_MAX, DC_CPC_ROLE_INFO_MAX or
 * DC_CPC_NATIVE_INFO_MAX. Unreachable while those are SIZES' packets, the largest of every
 * DC_CP_*_SIZES group; a group with a larger packet must raise them with it. */
DC_API dc_status DC_CALL dc_cpc_read_metadata(dc_gpu* gpu, dc_cpc_metadata* out, dc_error* error);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_CLIENT_POWER_CONTROLLER_H */
