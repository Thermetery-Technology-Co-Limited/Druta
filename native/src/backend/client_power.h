// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The pure client-power tables and geometry helpers (port of client_power_runtime's constants,
// _small_native_geometry, _definition_stride, _native_geometry, _coupled_requests, _project and
// _matches). Increment A2. Nothing here touches a driver: ClientPowerController stays in Python.
//
// The Python helpers take dicts. Here a binding is a struct with explicit presence, the hex
// payloads are byte spans, and every Python exception the helpers can raise on a malformed input is
// a CpError carrying the exception type and Python's own message, raised at the point in the
// evaluation order where Python raises it.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace druta::backend {

// ---- the command ids and size groups --------------------------------- //
inline constexpr std::uint32_t kCpInfo = 0x2080A630, kCpControl = 0x2080A632, kCpSet = 0x2080E633;
inline constexpr std::uint32_t kCpRoleInfo = 0x2080A61C, kCpNativeInfo = 0x2080A618,
                               kCpNativeControl = 0x2080A61A, kCpNativeStatus = 0x2080A619;
inline constexpr std::size_t kCpCommandCount = 7;
// The order of every size group's entries (and of the Python dicts' keys).
inline constexpr std::uint32_t kCpCommands[kCpCommandCount] = {
    kCpInfo, kCpControl, kCpSet, kCpRoleInfo, kCpNativeInfo, kCpNativeControl, kCpNativeStatus};
// Index of each command in kCpCommands and in a CpSizes row.
enum CpCommandIndex : std::size_t {
    kCpIdxInfo, kCpIdxControl, kCpIdxSet, kCpIdxRoleInfo, kCpIdxNativeInfo, kCpIdxNativeControl, kCpIdxNativeStatus,
};

struct CpSizes {
    std::uint32_t bytes[kCpCommandCount];  // packet bytes for kCpCommands[i]
    constexpr std::uint32_t operator[](std::size_t i) const { return bytes[i]; }
};

enum class CpSizeGroup : std::uint32_t {
    sizes, legacy, turing, info216, rm26, rm26_extended, a612_compact,
};
inline constexpr std::size_t kCpSizeGroupCount = 7;

// SIZES, LEGACY_SIZES, ... A612_COMPACT_SIZES by group; nullptr for an unknown group.
const CpSizes* cp_size_group(std::uint32_t group);

// _RM26_COMMANDS: the 26xx wire command for kCpCommands[i].
extern const std::uint32_t kCpRm26Commands[kCpCommandCount];

// SIZE_REFUSALS, LEGACY_GROUP_ARCHITECTURES, RM26_ARCHITECTURES (see client_power.cpp for the
// evidence each rests on).
struct CpU32List {
    const std::uint32_t* values;
    std::size_t count;
};
CpU32List cp_size_refusals();
CpU32List cp_legacy_group_architectures();
CpU32List cp_rm26_architectures();

inline constexpr std::uint32_t kCpInfoBase = 0x24, kCpInfoStride = 0x24, kCpControlBase = 0x28,
                               kCpControlStride = 0xC, kCpCapacity = 64;
inline constexpr std::uint32_t kCpNativeInfoBase = 0xCC, kCpNativeInfoStride = 0x104;
inline constexpr std::uint32_t kCpNativeControlBase = 0x14, kCpNativeControlStride = 0xC4;
inline constexpr std::uint32_t kCpNativeStatusBase = 0x9C, kCpNativeStatusStride = 0x1730;  // _LAYOUT["status"]
inline constexpr std::uint32_t kCpRole = 1, kCpClient = 0xFE, kCpMissing = 0xFFFFFFFF;
extern const std::uint32_t kCpNativeCapacity;

// _STATE_KEYS, in order.
inline constexpr std::size_t kCpStateKeyCount = 10;
extern const char* const kCpStateKeys[kCpStateKeyCount];
// The payload keys' positions in kCpStateKeys.
enum CpStateKey : std::size_t {
    kCpKeySchema, kCpKeyStableReads, kCpKeyIdentity, kCpKeyBinding, kCpKeyRequestedMw,
    kCpKeyInfoHex, kCpKeyControlHex, kCpKeyRoleInfoHex, kCpKeyNativeInfoHex, kCpKeyNativeControlHex,
};
inline constexpr std::size_t kCpFirstPayloadKey = kCpKeyInfoHex;
inline constexpr std::size_t kCpPayloadCount = 5;

// ---- a captured binding ---------------------------------------------- //
// binding.get("layout") compared with "rm26" / "legacy32", and binding.get("native_layout") with
// "policy124" / "rm26_extended". `other` is present but neither; Python treats it like absent.
enum class CpLayout : std::int32_t { absent = 0, other = 1, rm26 = 2, legacy32 = 3 };
enum class CpNativeLayout : std::int32_t { absent = 0, other = 1, policy124 = 2, rm26_extended = 3 };

struct CpBinding {
    CpLayout layout = CpLayout::absent;
    CpNativeLayout native_layout = CpNativeLayout::absent;
    // The key is present; 0 is Python's falsy value, which `stride or default` replaces.
    std::optional<std::int64_t> native_info_stride;
    std::optional<std::int64_t> native_index;  // absent: binding["native_index"] raises KeyError
    std::optional<std::int64_t> client_index;  // absent: binding["client_index"] raises KeyError
};

// ---- the Python exception a helper raises ----------------------------- //
enum class CpErrorKind : std::int32_t {
    none = 0,
    struct_error = 1,  // struct.error(message)
    index_error = 2,   // IndexError(message)
    overflow = 3,      // OverflowError(message)
    key_error = 4,     // KeyError(message), message is the key
    payload = 5,       // the payload (key, side) was not decodable hex: Python's fromhex raises here
    binding = 6,       // expected["binding"] has no .get: Python raises AttributeError here
    range = 7,         // an int64 intermediate the binding cannot carry (see definition_stride)
};

struct CpError {
    CpErrorKind kind = CpErrorKind::none;
    std::size_t key = 0;  // for payload: the kCpStateKeys index
    int side = 0;         // for payload: 0 expected (before), 1 observed
    std::string message;
    explicit operator bool() const { return kind != CpErrorKind::none; }
};

// ---- the helpers ------------------------------------------------------ //
struct CpSmallGeometry {
    std::uint32_t control_size, control_stride;
};
// _small_native_geometry(binding)
CpSmallGeometry small_native_geometry(const CpBinding& binding);

// _definition_stride(role_info_size): role_info_size - 4; nullopt when that leaves int64.
std::optional<std::int64_t> definition_stride(std::int64_t role_info_size);

struct CpGeometry {
    std::int64_t info_base, info_stride, control_base, control_stride, control_size;
};
// _native_geometry(binding)
CpGeometry native_geometry(const CpBinding& binding);

// A hex payload decoded by the caller; nullopt when Python's fromhex would raise on it.
using CpPayload = std::optional<std::vector<std::uint8_t>>;

// _coupled_requests: byte 4 of the role INFO is 0x30. IndexError for a shorter payload.
bool coupled_requests(const std::vector<std::uint8_t>& role_info, CpError& error);

// The value _project packs: Python's int, or what struct.pack_into refuses.
enum class CpMwKind : std::int32_t { integer = 0, not_integer = 1, out_of_range = 2 };

struct CpProjection {
    std::vector<std::uint8_t> control, native_control;
};
// The payload part of _project(before, mw): the copies of control and native_control with mw
// packed at the selected client and native rows. The rest of the result (the deep copy of the
// state and requested_mw) is plain dict work the caller does.
std::optional<CpProjection> project(const CpBinding& binding, CpMwKind mw_kind, std::int64_t mw,
                                    const CpPayload& control, const CpPayload& native_control, CpError& error);

// How one _STATE_KEYS entry of expected compares with the observation's.
enum class CpCompare : std::int32_t {
    bytes = 0,      // compare the two payloads' bytes (their hex texts are canonical, so equal bytes
                    // mean equal text)
    equal = 1,      // the caller compared the values: equal
    different = 2,  // the caller compared the values: different
    missing = 3,    // expected has no such key
};

struct CpMatchInput {
    CpBinding binding;          // expected["binding"]
    bool binding_usable = true; // false: expected["binding"].get raises
    CpCompare compare[kCpStateKeyCount] = {};
    bool extra_keys = false;    // expected has keys beyond _STATE_KEYS
    CpPayload expected[kCpPayloadCount];  // in kCpStateKeys payload order
    CpPayload observed[kCpPayloadCount];
};
// _matches(expected, observed) after _state(observed). nullopt with error when Python raises.
std::optional<bool> matches(const CpMatchInput& input, CpError& error);

}  // namespace druta::backend
