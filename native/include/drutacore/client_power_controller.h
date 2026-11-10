/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The client-power controller on a dc_gpu handle (client_power_runtime.ClientPowerController):
 * dc_cpc_*. Included by drutacore.h; do not include directly.
 *
 * Increment A3: the paired transport check (_transport), the guarded single-command exchange
 * (_exchange), _reset_transport, a fresh controller, and the controller state. Increment A4: the
 * metadata read (read_metadata: _stable_locked over _metadata_once and _client_group). Increment
 * A5: the full read (read: _stable_locked over _once, then the optional _arbitration). Increment
 * A6: the live power request (read_live_power_request) and its bookkeeping. Increment A7: the
 * request SET with its compensating restoration (set_request, _rollback). One sticky
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

/* The largest CONTROL and NATIVE_CONTROL packet of any DC_CP_*_SIZES group (SIZES), and the room
 * for the arbitration error text. */
#define DC_CPC_CONTROL_MAX 808
#define DC_CPC_NATIVE_CONTROL_MAX 13876
#define DC_CPC_ARBITRATION_ERROR_MAX 1024

/* read()'s dict: {**metadata, "requested_mw", "control_hex", "native_control_hex", "arbitration",
 * "arbitration_error"}. metadata is read_metadata()'s dict for the same capture. control and
 * native_control hold control_size / native_control_size bytes. Exactly one of has_arbitration (the
 * decoded report, available = 1, with its mask: power_arbitration.decode's dict) and
 * has_arbitration_error (Python's error text, never empty) is 1. */
typedef struct dc_cpc_observation {
    uint32_t size;                  /* sizeof(dc_cpc_observation), set by the caller */
    dc_cpc_metadata metadata;       /* metadata.size: sizeof(dc_cpc_metadata), set by the caller */
    uint32_t requested_mw;
    uint32_t control_size;
    uint32_t native_control_size;
    uint8_t control[DC_CPC_CONTROL_MAX];
    uint8_t native_control[DC_CPC_NATIVE_CONTROL_MAX];
    int32_t has_arbitration;
    dc_arbitration_report arbitration;  /* arbitration.size: sizeof(dc_arbitration_report), set by
                                         * the caller (and written back on success) */
    int32_t has_arbitration_error;
    char arbitration_error[DC_CPC_ARBITRATION_ERROR_MAX];
} dc_cpc_observation;

/* read(): two equal fresh captures of the metadata, the selected client CONTROL and the native
 * CONTROL under the GPU lock, one fresh pair after a transient failure (as dc_cpc_read_metadata),
 * then the optional native STATUS read and its arbitration decode.
 *
 * STATUS is diagnostic: its failure is reported in arbitration_error, not as a status. The legacy
 * and RM26-record groups read no STATUS ("Power arbitration diagnostics are unavailable for this
 * policy layout"). Otherwise STATUS is tried at most twice: a transient failure resets the handle's
 * power transport cache and tries again (the cache is reset after a second transient failure too),
 * any other failure ends the attempts, and a decode that is not available reports its reason. An
 * identity failure during STATUS fails the whole read (DC_E_FAILED, DC_ERR_IDENTITY).
 *
 * DC_OK with *out (the sizes written back), or DC_E_FAILED with *error: the ClientPowerError of the
 * failing step (kind and message as Python's _once raises them: DC_ERR_MALFORMED for an echo, header
 * or type disagreement, DC_ERR_UNSUPPORTED for a missing or zero FE request, DC_ERR_UNSTABLE for
 * client and native requests that disagree or two captures that differ). The controller state keeps
 * every group the captures selected. DC_E_ARGUMENT for a NULL handle or out, a wrong out->size,
 * out->metadata.size or out->arbitration.size, or a dc_error whose size is wrong; *error and *out
 * are then left untouched.
 *
 * DC_E_BUFFER (neither *error nor *out written) when a stable capture holds a payload larger than
 * its array. Unreachable while the DC_CPC_*_MAX are SIZES' packets, the largest of every
 * DC_CP_*_SIZES group. */
DC_API dc_status DC_CALL dc_cpc_read(dc_gpu* gpu, dc_cpc_observation* out, dc_error* error);

/* The live power request (nvbackend.GPU.read_live_power_request) and its bookkeeping on the handle:
 * _live_power_last, _live_power_error, _live_power_error_kind and _board_policy. */

/* read_live_power_request(): dc_cpc_read on the handle's sticky controller, then the bookkeeping,
 * under the GPU lock.
 *
 * DC_OK with *out: the observation becomes the last one, the error and its kind are cleared, the
 * board policy becomes binding.native_index, and each of the handle's capabilities pl_min_mw,
 * pl_def_mw and pl_max_mw that is absent (DC_C_PL_* clear in dc_gpu_capabilities_get) is filled from
 * binding.minimum_mw, rated_mw and maximum_mw; a present one is never replaced.
 *
 * DC_E_FAILED with *error (dc_cpc_read's kind and message, DC_ERR_INVALID included): the error text
 * is recorded with its kind, which is the error's own, or DC_ERR_TRANSIENT for DC_ERR_INVALID (the
 * bare ValueError is not a ClientPowerError) and for a C++ exception (reported as transient). A
 * transient kind keeps the last observation; any other kind drops it. The board policy and the
 * capabilities are left as they were. Python's (None, str(exc)) is DC_E_FAILED with
 * error->message.
 *
 * DC_E_ARGUMENT as dc_cpc_read (nothing read, the bookkeeping untouched). DC_E_BUFFER as dc_cpc_read
 * (unreachable); the bookkeeping then already holds the successful read. */
DC_API dc_status DC_CALL dc_cpc_live_read(dc_gpu* gpu, dc_cpc_observation* out, dc_error* error);

#define DC_CPC_LIVE_ERROR_MAX 1024

typedef struct dc_cpc_live_state {
    uint32_t size;               /* sizeof(dc_cpc_live_state), set by the caller */
    int32_t has_last;            /* _live_power_last is not None */
    dc_cpc_observation last;     /* last.size, last.metadata.size and last.arbitration.size set by
                                  * the caller; zero apart from those when has_last is 0 */
    int32_t has_error;           /* _live_power_error is not None */
    int32_t error_kind;          /* _live_power_error_kind as DC_ERR_* (never DC_ERR_INVALID); 0 when
                                  * has_error is 0 */
    char error[DC_CPC_LIVE_ERROR_MAX]; /* _live_power_error, truncated to fit */
    int32_t board_policy;        /* _board_policy, or -1 for None */
} dc_cpc_live_state;

/* dc_gpu_refresh_capabilities also releases the live power request (GPU.refresh_capabilities): the
 * controller becomes a fresh one (as dc_cpc_reset) and the last observation, the error and its kind
 * are cleared, with no escape; the board policy and the capabilities are kept. */

/* The bookkeeping above. DC_E_ARGUMENT for a NULL handle or out, or a wrong out->size,
 * out->last.size, out->last.metadata.size or out->last.arbitration.size. DC_E_BUFFER (nothing
 * written) when the last observation does not fit (unreachable, as dc_cpc_read's). */
DC_API dc_status DC_CALL dc_cpc_live_state_get(dc_gpu* gpu, dc_cpc_live_state* out);

/* _board_policy = policy: a native policy index, or -1 for None (DC_E_ARGUMENT below -1). Written
 * by GPU._power_policy_rows and set_power_limit_mw as well as by a successful live read. */
DC_API dc_status DC_CALL dc_cpc_board_policy_set(dc_gpu* gpu, int32_t policy);

/* ---- increment A7: the request SET with its compensating restoration (set_request, _rollback) ---- */

/* set_request's refusal of mw outside 0 < mw < 0xFFFFFFFF (Python's text). */
#define DC_CPC_INVALID_REQUEST \
    "Power request must be positive whole uint32 milliwatts below the missing-client sentinel"

/* The room for _rollback's error text. It is at most two "TypeName: message" entries joined by
 * "; ": the restoring SET's own failure, then the first failure of the read-back and restoration.
 * The SET, read and identity messages are those of dc_cpc_exchange / dc_cpc_read (under 1024 bytes,
 * as dc_error.message carries them), but "Policy N restoration failed: {message}" embeds the policy
 * writer's message (dc_cl_write_policy_limit's), which carries driver-table error texts with no fixed
 * bound. error_length is therefore the full text's length, and dc_cpc_rollback_error_get returns the
 * whole of it when error_length >= DC_CPC_ROLLBACK_ERROR_MAX. */
#define DC_CPC_ROLLBACK_ERROR_MAX 4096

/* _rollback's dict: {"attempted", "verified", "error", "readback"}. error holds the "; "-joined
 * entries, each "TypeName: message" with Python's type name: ClientPowerError, ValueError (the bare
 * ValueError of DC_ERR_INVALID), IndexError ("list index out of range", the policy writer's
 * DC_E_RANGE), MemoryError (an allocation failure in the core). */
typedef struct dc_cpc_rollback {
    uint32_t size;          /* sizeof(dc_cpc_rollback), set by the caller */
    int32_t attempted;      /* the restoring SET was dispatched */
    int32_t verified;       /* the exact original state was read back, and nothing failed */
    int32_t has_error;      /* error is not None */
    uint32_t error_length;  /* strlen of the full text; error holds at most MAX - 1 bytes of it */
    char error[DC_CPC_ROLLBACK_ERROR_MAX];
    int32_t has_readback;   /* readback is not None: the last read-back assigned before any failure */
    dc_cpc_observation readback; /* readback.size, .metadata.size and .arbitration.size set by the caller */
} dc_cpc_rollback;

/* set_request's result. Every nested size field (before, after, rollback, rollback.readback, and
 * each observation's metadata and arbitration) is set by the caller. */
typedef struct dc_cpc_set_result {
    uint32_t size;               /* sizeof(dc_cpc_set_result), set by the caller */
    dc_cpc_observation before;   /* the full read before the SET */
    dc_cpc_observation after;    /* the full read after it */
    int32_t has_rollback;        /* the SET was dispatched and then failed */
    dc_cpc_rollback rollback;
} dc_cpc_set_result;

/* set_request(mw): under the GPU lock for the whole call (the peer writes included), read the
 * request (dc_cpc_read), SET the client CONTROL with only the selected client's request word and the
 * bound native policy's request word replaced by mw, read again and require the exact projection
 * (v30 policies may move occupied peers' request words, nothing else).
 *
 * DC_OK with out->before and out->after (has_rollback 0, the rollback zeroed apart from its sizes).
 *
 * DC_E_FAILED with *error (kind, message and rm_status as the Python exception carries them):
 *   - mw outside 0 < mw < 0xFFFFFFFF: DC_ERR_INVALID with DC_CPC_INVALID_REQUEST, nothing read;
 *   - a failure before the SET was handed to the escape (the read, or the SET's own binding,
 *     transport and geometry checks): that failure unchanged (DC_ERR_INVALID is the bare
 *     ValueError), has_rollback 0;
 *   - any failure once the SET was dispatched (the SET's escape, status, echo, the read-back, or
 *     DC_ERR_READBACK "Client-power request or preserved definitions did not read back exactly"):
 *     the handle's power transport cache is reset, the captured client CONTROL is SET once more and
 *     read back; on a v30 policy table whose read-back otherwise matches, every occupied peer whose
 *     request word differs from the capture is written back through the policy writer
 *     (dc_cl_write_policy_limit's, on this handle), the target identity checked before each write,
 *     and the state read once more; the whole captured state must then read back. *error is the
 *     original failure's kind (DC_ERR_TRANSIENT for a bare ValueError) with "{message}. Original
 *     request restored and verified" or "{message}. Original request restoration was not
 *     verified", rm_status 0; has_rollback 1 with out->rollback.
 * out->before / out->after are zeroed apart from their sizes on DC_E_FAILED.
 *
 * DC_E_ARGUMENT for a NULL handle or out, a wrong size anywhere in *out, or a dc_error whose size is
 * wrong; nothing is read or written. DC_E_BUFFER when a reading does not fit its dc_cpc_observation
 * (unreachable, as dc_cpc_read's; the request may then have been written). *out is cleared (sizes
 * kept, has_rollback 0, error_length 0) before anything runs, so no outcome shows a previous call's
 * readings or rollback. An allocation failure (std::bad_alloc, the only C++ exception the core can
 * raise here) is DC_E_FAILED with a transient *error naming it and the cleared *out: before the SET
 * was dispatched nothing was written; after it, the compensation may or may not have run, and only a
 * fresh read tells the state. Session-only: nothing is stored beyond this handle's memory, and
 * nothing is re-applied later. */
DC_API dc_status DC_CALL dc_cpc_set_request(dc_gpu* gpu, int64_t mw, dc_cpc_set_result* out, dc_error* error);

/* The full rollback error text of the handle's last dc_cpc_set_request ("" when it had none):
 * *needed is its length plus the terminating NUL; DC_E_BUFFER (nothing written) when capacity is
 * below that. DC_E_ARGUMENT for a NULL handle or needed, or a NULL out with capacity nonzero.
 * The text is kept per handle, not per thread, and every dc_cpc_set_request clears or replaces it:
 * a caller that sets requests from several threads serializes each dc_cpc_set_request with its
 * fetch under one lock of its own (the handle mutex covers each call, not the pair). */
DC_API dc_status DC_CALL dc_cpc_rollback_error_get(dc_gpu* gpu, char* out, size_t capacity, size_t* needed);

/* TEST SEAM, for the parity tests only (no production caller): make the nth target lookup of this
 * handle's client-power controller from now (1 = the next; each binding check of the controller is
 * one lookup) fail with the sticky reader's identity refusal (DC_CPC_TEST_LOOKUP_FAIL: "GPU binding
 * changed; create a fresh PERF reader") or return a target whose subsystem id differs in its lowest
 * bit (DC_CPC_TEST_LOOKUP_CHANGED). It fires once, then disarms; DC_CPC_TEST_LOOKUP_NONE or nth 0
 * disarms. Inert unless armed. DC_E_ARGUMENT for a NULL handle or an unknown mode. */
#define DC_CPC_TEST_LOOKUP_NONE 0
#define DC_CPC_TEST_LOOKUP_FAIL 1
#define DC_CPC_TEST_LOOKUP_CHANGED 2
DC_API dc_status DC_CALL dc_hook_test_cpc_binding(dc_gpu* gpu, uint32_t nth, int32_t mode);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_CLIENT_POWER_CONTROLLER_H */
