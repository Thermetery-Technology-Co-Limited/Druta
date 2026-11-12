/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef DRUTACORE_I2C_H
#define DRUTACORE_I2C_H
/* Included by drutacore.h after dc_error. */
#ifdef __cplusplus
extern "C" {
#endif
/* One NVAPI Ex transaction on the selected, freshly PCI-verified GPU.
 * port/reg are bytes, addr7 is a 7-bit bus address, width is 1..4 bytes.
 * Values use little-endian byte order; out is untouched on failure. No read or
 * write is retried. The transport refuses railctl's nonvolatile/password
 * commands. Identity and regulator-specific authorization are the caller's
 * responsibility; a successful bus reply is not proof of controller identity.
 * Writes can have occurred on DC_E_FAILED: never repeat automatically.
 * error is optional, otherwise caller sets error->size = sizeof(dc_error).
 * Ex packet geometry is presently qualified for 64-bit processes only. */
DC_API dc_status DC_CALL dc_i2c_read(dc_gpu* gpu, uint32_t port, uint32_t addr7, uint32_t reg,
                                     uint32_t width, uint32_t* value, dc_error* error);
DC_API dc_status DC_CALL dc_i2c_write(dc_gpu* gpu, uint32_t port, uint32_t addr7, uint32_t reg,
                                      uint32_t width, uint32_t value, dc_error* error);
#ifdef __cplusplus
}
#endif
#endif
