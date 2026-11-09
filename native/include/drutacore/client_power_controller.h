/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The client-power controller on a dc_gpu handle (client_power_runtime.ClientPowerController):
 * dc_cpc_*. Included by drutacore.h; do not include directly.
 *
 * Increment A3: the paired transport check (_transport), the guarded single-command exchange
 * (_exchange), _reset_transport, a fresh controller, and the controller state. One sticky
 * controller per handle, with its own target binding. A ClientPowerError is DC_E_FAILED with a
 * dc_error: kind DC_ERR_IDENTITY / TRANSIENT / UNSUPPORTED / MALFORMED, Python's message, and
 * rm_status (nonzero only for an RM rejection). DC_ERR_INVALID is the one bare ValueError Python
 * raises (see dc_cpc_exchange). DC_E_ARGUMENT only for a NULL or malformed argument. */
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

/* _exchange(command, payload, write=...): one exact command and one escape. write is nonzero when
 * a _Write is given (isinstance(write, _Write)). *dispatched is set on every return: 1 once the
 * packet reached the escape, where Python sets write.dispatched = True for any write object passed
 * (the caller applies that to its own object). DC_OK with the `size` response bytes in out (capacity >= size), or
 * DC_E_FAILED with *error. DC_ERR_INVALID with "Can only assign sequence of same size" is the bare
 * ValueError Python raises when _transport switched the size table under the payload. */
DC_API dc_status DC_CALL dc_cpc_exchange(dc_gpu* gpu, uint32_t command, const uint8_t* payload, uint32_t size,
                                         int32_t write, uint8_t* out, uint32_t capacity, int32_t* dispatched,
                                         dc_error* error);

/* _reset_transport: clear the handle's cached power transport and its layout choice. */
DC_API dc_status DC_CALL dc_cpc_reset_transport(dc_gpu* gpu);
/* A fresh controller: every attribute back to __init__, the binding's sticky identity included. */
DC_API dc_status DC_CALL dc_cpc_reset(dc_gpu* gpu);
/* The controller's attributes. */
DC_API dc_status DC_CALL dc_cpc_state_get(dc_gpu* gpu, dc_cpc_state* out);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_CLIENT_POWER_CONTROLLER_H */
