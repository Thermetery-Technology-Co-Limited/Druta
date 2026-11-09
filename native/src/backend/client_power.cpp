// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Port of client_power_runtime's module constants and its pure helpers _small_native_geometry,
// _definition_stride, _native_geometry, _coupled_requests, _project and _matches. The tables are
// verbatim copies in the Python dict order with the evidence comments beside them: generation
// lists are qualification only, read-only RM status decides which group a card speaks, and the
// driver versions named in comments are evidence, never branches.
//
// Python's integers are unbounded and its struct/bytes operations accept negative offsets. The
// offset arithmetic here keeps both: a value that leaves int64 is carried as "beyond" in its
// direction (every offset formula adds one variable term to small constants, so the direction is
// all Python's result depends on), and pack_into / unpack_from / indexing / slicing follow
// CPython's own checks and messages.
#include "backend/client_power.h"

#include <algorithm>
#include <cstddef>

namespace druta::backend {
namespace {

// ---- the size groups ---------------------------------------------------- //
// Entries follow kCpCommands: INFO, CONTROL, SET, ROLE_INFO, NATIVE_INFO, NATIVE_CONTROL,
// NATIVE_STATUS.
constexpr CpSizes kSizes = {{0x924, 0x328, 0x328, 0x108, 20512, 13876, 397048}};
constexpr CpSizes kLegacySizes = {{1160, 392, 392, 232, 8632, 6356, 190272}};
// The smaller client group can accompany either native policy ABI. Select
// with read-only INFO acceptance, never a device or driver version.
constexpr CpSizes kTuringSizes = {{1160, 392, 392, 232, 8620, 4432, 172080}};  // {**LEGACY_SIZES, ...}
// A third native ABI behind the same 1160-byte client group, whose policy
// INFO record is 216 bytes rather than 228. Its role INFO shrinks with the
// record (see _definition_stride), which is how it is detected: the role GET
// is refused for its size before the native INFO GET is ever reached.
// Selected by read-only acceptance, never by a device or driver version.
constexpr CpSizes kInfo216Sizes = {{1160, 392, 392, 220, 8236, 4432, 171048}};  // {**LEGACY_SIZES, ...}
constexpr CpSizes kRm26Sizes = {{644, 388, 388, 120, 5008, 2108, 14148}};
constexpr CpSizes kRm26ExtendedSizes = {{900, 388, 388, 136, 5520, 2124, 69148}};
// The compact record layout above, reached through the A612 command family
// rather than the 26xx one. Command family and record layout are independent:
// this driver answers 0x2080A6xx while laying its policies out the compact
// way, with a 128-byte native INFO record of its own. Selected by read-only
// INFO acceptance like every other group.
constexpr CpSizes kA612CompactSizes = {{900, 388, 388, 132, 5392, 2124, 70684}};

constexpr const CpSizes* kGroups[kCpSizeGroupCount] = {
    &kSizes, &kLegacySizes, &kTuringSizes, &kInfo216Sizes, &kRm26Sizes, &kRm26ExtendedSizes, &kA612CompactSizes,
};

// Generations whose client power-policy semantics have been validated against
// a live adapter, and so may fall back from the modern client group to the
// smaller one. Generation decides whether the semantics are understood at all;
// WHICH group a card speaks is still decided by read-only INFO acceptance
// below, never by a device, board or driver version.
//   4 Pascal, 6 Turing, 7 Ampere
constexpr std::uint32_t kLegacyGroupArchitectures[] = {4, 6, 7};
// The same question for the RM26 packet family, kept as a separate list
// because it rests on separate evidence: a generation qualified against one
// packet family is not thereby qualified against the other, and merging the
// two would silently claim Ampere on RM26, which nothing here has tested.
//   4 Pascal, 6 Turing
constexpr std::uint32_t kRm26Architectures[] = {4, 6};
// RM statuses that answer "this parameter block is not the size this driver's
// structure has". Both have been observed as the ONLY reply a given driver
// gives to every wrong size while the right one succeeds: 0x1F across the
// 580/610 drivers, 0x3A across 472.12 and 516.94. Which one a driver uses is
// a property of that driver, not of the command family - 516.94 returns 0x3A
// on the A612 family that others answer 0x1F on - so both are treated as the
// definite, retry-will-not-help answer that selects the next candidate size.
constexpr std::uint32_t kSizeRefusals[] = {0x1F, 0x3A};

constexpr std::uint32_t min_u32(std::uint32_t a, std::uint32_t b) { return a < b ? a : b; }

// The understood native interface exposes one u32 occupancy mask. Buffer tails
// are preserved; their size must not be mistaken for extra addressable rows.
constexpr std::uint32_t kNativeCapacity = min_u32(
    min_u32(32, (kSizes[kCpIdxNativeInfo] - kCpNativeInfoBase) / kCpNativeInfoStride),
    min_u32((kSizes[kCpIdxNativeControl] - kCpNativeControlBase) / kCpNativeControlStride,
            (kSizes[kCpIdxNativeStatus] - kCpNativeStatusBase) / kCpNativeStatusStride));
static_assert(kNativeCapacity == 32, "NATIVE_CAPACITY of the Python module");

// ---- Python integers past int64 ----------------------------------------- //
struct Wide {
    std::int64_t v = 0;
    int beyond = 0;  // +1: above INT64_MAX, -1: below INT64_MIN (v is then meaningless)
};

constexpr std::int64_t kMax = INT64_MAX, kMin = INT64_MIN;

Wide wide(std::int64_t v) { return Wide{v, 0}; }

Wide add(Wide a, Wide b) {
    if (a.beyond || b.beyond) {
        // One variable term plus small constants: the beyond term decides the direction.
        return Wide{0, a.beyond ? a.beyond : b.beyond};
    }
    if (b.v > 0 && a.v > kMax - b.v) return Wide{0, 1};
    if (b.v < 0 && a.v < kMin - b.v) return Wide{0, -1};
    return Wide{a.v + b.v, 0};
}

Wide mul(Wide a, Wide b) {
    const int sa = a.beyond ? a.beyond : (a.v > 0) - (a.v < 0);
    const int sb = b.beyond ? b.beyond : (b.v > 0) - (b.v < 0);
    if (sa == 0 || sb == 0) return Wide{0, 0};
    if (a.beyond || b.beyond) return Wide{0, sa * sb};
    // |a.v * b.v| > INT64 range? Compare magnitudes without overflowing.
    const std::uint64_t ma = a.v < 0 ? 0u - static_cast<std::uint64_t>(a.v) : static_cast<std::uint64_t>(a.v);
    const std::uint64_t mb = b.v < 0 ? 0u - static_cast<std::uint64_t>(b.v) : static_cast<std::uint64_t>(b.v);
    const std::uint64_t limit = sa * sb > 0 ? static_cast<std::uint64_t>(kMax) : static_cast<std::uint64_t>(kMax) + 1u;
    if (ma > limit / mb) return Wide{0, sa * sb};
    const std::uint64_t m = ma * mb;
    if (sa * sb > 0) return Wide{static_cast<std::int64_t>(m), 0};
    return Wide{m == static_cast<std::uint64_t>(kMax) + 1u ? kMin : -static_cast<std::int64_t>(m), 0};
}

std::string dec(std::int64_t v) { return std::to_string(v); }
std::string dec(std::uint64_t v) { return std::to_string(v); }

bool fail(CpError& error, CpErrorKind kind, std::string message) {
    error.kind = kind;
    error.message = std::move(message);
    return false;
}

bool fail_payload(CpError& error, std::size_t key, int side) {
    error.kind = CpErrorKind::payload;
    error.key = key;
    error.side = side;
    error.message = kCpStateKeys[key];
    return false;
}

const char kIndexSized[] = "cannot fit 'int' into an index-sized integer";

// struct.unpack_from's offset handling: the normalised offset, or struct.error / OverflowError.
bool unpack_offset(std::size_t length, Wide offset, std::int64_t size, std::int64_t& at, CpError& error) {
    if (offset.beyond) return fail(error, CpErrorKind::overflow, "Python int too large to convert to C ssize_t");
    const std::int64_t n = static_cast<std::int64_t>(length);
    std::int64_t off = offset.v;
    if (off < 0) {
        if (off + size > 0)
            return fail(error, CpErrorKind::struct_error,
                        "not enough data to unpack " + dec(size) + " bytes at offset " + dec(off));
        if (off + n < 0)
            return fail(error, CpErrorKind::struct_error,
                        "offset " + dec(off) + " out of range for " + dec(n) + "-byte buffer");
        off += n;
    }
    if (n - off < size)
        return fail(error, CpErrorKind::struct_error,
                    "unpack_from requires a buffer of at least " +
                        dec(static_cast<std::uint64_t>(size) + static_cast<std::uint64_t>(off)) + " bytes for unpacking " +
                        dec(size) + " bytes at offset " + dec(off) + " (actual buffer size is " + dec(n) + ")");
    at = off;
    return true;
}

std::uint32_t le32(const std::vector<std::uint8_t>& raw, std::int64_t at) {
    const std::size_t i = static_cast<std::size_t>(at);
    return static_cast<std::uint32_t>(raw[i]) | static_cast<std::uint32_t>(raw[i + 1]) << 8 |
           static_cast<std::uint32_t>(raw[i + 2]) << 16 | static_cast<std::uint32_t>(raw[i + 3]) << 24;
}

// _u32(raw, offset) = struct.unpack_from("<I", raw, offset)[0]
bool u32_at(const std::vector<std::uint8_t>& raw, Wide offset, std::uint32_t& value, CpError& error) {
    std::int64_t at = 0;
    if (!unpack_offset(raw.size(), offset, 4, at, error)) return false;
    value = le32(raw, at);
    return true;
}

// struct.pack_into("<I", raw, offset, mw)
bool pack_u32(std::vector<std::uint8_t>& raw, Wide offset, CpMwKind kind, std::int64_t mw, CpError& error) {
    if (offset.beyond) return fail(error, CpErrorKind::index_error, kIndexSized);
    const std::int64_t n = static_cast<std::int64_t>(raw.size());
    std::int64_t off = offset.v;
    if (off < 0) {
        if (off + 4 > 0)
            return fail(error, CpErrorKind::struct_error, "no space to pack 4 bytes at offset " + dec(off));
        if (off + n < 0)
            return fail(error, CpErrorKind::struct_error,
                        "offset " + dec(off) + " out of range for " + dec(n) + "-byte buffer");
        off += n;
    }
    if (n - off < 4)
        return fail(error, CpErrorKind::struct_error,
                    "pack_into requires a buffer of at least " + dec(std::uint64_t{4} + static_cast<std::uint64_t>(off)) +
                        " bytes for packing 4 bytes at offset " + dec(off) + " (actual buffer size is " + dec(n) + ")");
    if (kind == CpMwKind::not_integer)
        return fail(error, CpErrorKind::struct_error, "required argument is not an integer");
    if (kind != CpMwKind::integer || mw < 0 || mw > static_cast<std::int64_t>(kCpMissing))
        return fail(error, CpErrorKind::struct_error, "'I' format requires 0 <= number <= 4294967295");
    const std::uint32_t v = static_cast<std::uint32_t>(mw);
    const std::size_t i = static_cast<std::size_t>(off);
    raw[i] = static_cast<std::uint8_t>(v);
    raw[i + 1] = static_cast<std::uint8_t>(v >> 8);
    raw[i + 2] = static_cast<std::uint8_t>(v >> 16);
    raw[i + 3] = static_cast<std::uint8_t>(v >> 24);
    return true;
}

// raw[index] for bytes.
bool byte_at(const std::vector<std::uint8_t>& raw, Wide index, std::uint32_t& value, CpError& error) {
    if (index.beyond) return fail(error, CpErrorKind::index_error, kIndexSized);
    const std::int64_t n = static_cast<std::int64_t>(raw.size());
    std::int64_t i = index.v;
    if (i < 0) i += n;
    if (i < 0 || i >= n) return fail(error, CpErrorKind::index_error, "index out of range");
    value = raw[static_cast<std::size_t>(i)];
    return true;
}

// A slice bound normalised as Python does for raw[start:stop].
std::int64_t slice_bound(Wide x, std::int64_t n) {
    if (x.beyond) return x.beyond > 0 ? n : 0;
    std::int64_t v = x.v;
    if (v < 0) {
        v += n;
        return v < 0 ? 0 : v;
    }
    return v > n ? n : v;
}

// old[at:at + 4] == new[at:at + 4] for two payloads of equal length.
bool slices_equal(const std::vector<std::uint8_t>& old, const std::vector<std::uint8_t>& now, Wide at) {
    const std::int64_t n = static_cast<std::int64_t>(old.size());
    const std::int64_t start = slice_bound(at, n), stop = slice_bound(add(at, wide(4)), n);
    if (stop <= start) return true;
    const auto first = static_cast<std::ptrdiff_t>(start), last = static_cast<std::ptrdiff_t>(stop);
    return std::equal(old.begin() + first, old.begin() + last, now.begin() + first);
}

std::int64_t stride_or(const CpBinding& binding, std::int64_t fallback) {
    return binding.native_info_stride && *binding.native_info_stride ? *binding.native_info_stride : fallback;
}

}  // namespace

// _RM26_COMMANDS
const std::uint32_t kCpRm26Commands[kCpCommandCount] = {
    0x20802630, 0x20802632, 0x20802633, 0x2080261C, 0x20802618, 0x2080261A, 0x20802619,
};

const std::uint32_t kCpNativeCapacity = kNativeCapacity;

const char* const kCpStateKeys[kCpStateKeyCount] = {
    "schema",   "stable_reads", "identity",      "binding",         "requested_mw",
    "info_hex", "control_hex",  "role_info_hex", "native_info_hex", "native_control_hex",
};

const CpSizes* cp_size_group(std::uint32_t group) {
    return group < kCpSizeGroupCount ? kGroups[group] : nullptr;
}

CpU32List cp_size_refusals() { return {kSizeRefusals, sizeof(kSizeRefusals) / sizeof(kSizeRefusals[0])}; }
CpU32List cp_legacy_group_architectures() {
    return {kLegacyGroupArchitectures, sizeof(kLegacyGroupArchitectures) / sizeof(kLegacyGroupArchitectures[0])};
}
CpU32List cp_rm26_architectures() {
    return {kRm26Architectures, sizeof(kRm26Architectures) / sizeof(kRm26Architectures[0])};
}

// Native control size/stride selected by the accepted INFO geometry.
CpSmallGeometry small_native_geometry(const CpBinding& binding) {
    if (binding.native_layout == CpNativeLayout::policy124) return {kTuringSizes[kCpIdxNativeControl], 124};
    return {kLegacySizes[kCpIdxNativeControl], 184};
}

// The native policy INFO record stride this role GET implies: a 4-byte semantic selector followed
// by exactly one native policy INFO record (264/260, 232/228, 220/216, 136/132 and 120/116 so far).
// The derived value is not trusted blindly: the controller still has to bind it to exactly one
// occupied native policy before anything uses it.
std::optional<std::int64_t> definition_stride(std::int64_t role_info_size) {
    if (role_info_size < kMin + 4) return std::nullopt;
    return role_info_size - 4;
}

// native_info_stride is recorded only when it differs from the layout's usual record size, so a
// binding captured before it existed still reads back with the geometry it was captured under.
CpGeometry native_geometry(const CpBinding& binding) {
    if (binding.layout == CpLayout::rm26) {
        if (binding.native_layout == CpNativeLayout::rm26_extended)
            return {60, stride_or(binding, 132), 20, 52, kRm26ExtendedSizes[kCpIdxNativeControl]};
        return {52, stride_or(binding, 116), 4, 52, kRm26Sizes[kCpIdxNativeControl]};
    }
    if (binding.layout == CpLayout::legacy32) {
        const CpSmallGeometry small = small_native_geometry(binding);
        return {88, stride_or(binding, 228), 20, small.control_stride, small.control_size};
    }
    return {kCpNativeInfoBase, stride_or(binding, kCpNativeInfoStride), kCpNativeControlBase, kCpNativeControlStride,
            kSizes[kCpIdxNativeControl]};
}

// v30 firmware policies recalculate peer votes in both RM packet families.
// Their table version is independent of the client/native buffer sizes.
bool coupled_requests(const std::vector<std::uint8_t>& role_info, CpError& error) {
    if (role_info.size() < 5) return fail(error, CpErrorKind::index_error, "index out of range");
    return role_info[4] == 0x30;
}

std::optional<CpProjection> project(const CpBinding& binding, CpMwKind mw_kind, std::int64_t mw,
                                    const CpPayload& control, const CpPayload& native_control, CpError& error) {
    const bool legacy = binding.layout == CpLayout::legacy32;
    const CpGeometry g = native_geometry(binding);
    const std::int64_t control_base = binding.layout == CpLayout::rm26 ? 4 : legacy ? 8 : kCpControlBase;
    if (!binding.client_index) return fail(error, CpErrorKind::key_error, "client_index"), std::nullopt;
    if (!binding.native_index) return fail(error, CpErrorKind::key_error, "native_index"), std::nullopt;
    CpProjection out;
    if (!control) return fail_payload(error, kCpKeyControlHex, 0), std::nullopt;
    out.control = *control;
    const Wide control_at = add(add(wide(control_base), mul(wide(*binding.client_index), wide(kCpControlStride))), wide(4));
    if (!pack_u32(out.control, control_at, mw_kind, mw, error)) return std::nullopt;
    if (!native_control) return fail_payload(error, kCpKeyNativeControlHex, 0), std::nullopt;
    out.native_control = *native_control;
    const Wide native_at = add(add(wide(g.control_base), mul(wide(*binding.native_index), wide(g.control_stride))), wide(4));
    if (!pack_u32(out.native_control, native_at, mw_kind, mw, error)) return std::nullopt;
    return out;
}

// v30 board writes may recalculate occupied peers' request words.
std::optional<bool> matches(const CpMatchInput& in, CpError& error) {
    const auto payload_index = [](std::size_t key) { return key - kCpFirstPayloadKey; };
    const auto key_equal = [&](std::size_t key, bool& equal) {
        switch (in.compare[key]) {
        case CpCompare::equal: equal = true; return true;
        case CpCompare::different: equal = false; return true;
        case CpCompare::missing: return fail(error, CpErrorKind::key_error, kCpStateKeys[key]);
        case CpCompare::bytes: break;
        }
        if (key < kCpFirstPayloadKey) return fail(error, CpErrorKind::range, "bytes compare of a non-payload key");
        const CpPayload& a = in.expected[payload_index(key)];
        const CpPayload& b = in.observed[payload_index(key)];
        if (!a) return fail_payload(error, key, 0);
        if (!b) return fail_payload(error, key, 1);
        equal = *a == *b;
        return true;
    };
    // _coupled_requests(expected)
    const std::size_t role = payload_index(kCpKeyRoleInfoHex);
    if (in.compare[kCpKeyRoleInfoHex] == CpCompare::missing)
        return fail(error, CpErrorKind::key_error, kCpStateKeys[kCpKeyRoleInfoHex]), std::nullopt;
    if (!in.expected[role]) return fail_payload(error, kCpKeyRoleInfoHex, 0), std::nullopt;
    const bool coupled = coupled_requests(*in.expected[role], error);
    if (error) return std::nullopt;
    if (!coupled) {
        // actual == expected: a missing or extra key is a difference, never a KeyError.
        if (in.extra_keys) return false;
        for (std::size_t key = 0; key < kCpStateKeyCount; ++key) {
            if (in.compare[key] == CpCompare::missing) return false;
            bool equal = false;
            if (!key_equal(key, equal)) return std::nullopt;
            if (!equal) return false;
        }
        return true;
    }
    for (std::size_t key = 0; key < kCpStateKeyCount; ++key) {
        if (key == kCpKeyNativeControlHex) continue;
        bool equal = false;
        if (!key_equal(key, equal)) return std::nullopt;
        if (!equal) return false;
    }
    const std::size_t nc = payload_index(kCpKeyNativeControlHex), ni = payload_index(kCpKeyNativeInfoHex);
    if (in.compare[kCpKeyNativeControlHex] == CpCompare::missing)
        return fail(error, CpErrorKind::key_error, kCpStateKeys[kCpKeyNativeControlHex]), std::nullopt;
    if (!in.expected[nc]) return fail_payload(error, kCpKeyNativeControlHex, 0), std::nullopt;
    if (!in.observed[nc]) return fail_payload(error, kCpKeyNativeControlHex, 1), std::nullopt;
    if (!in.expected[ni]) return fail_payload(error, kCpKeyNativeInfoHex, 0), std::nullopt;
    if (!in.binding_usable) {
        error.kind = CpErrorKind::binding;
        error.message = "binding";
        return std::nullopt;
    }
    const std::vector<std::uint8_t>& old = *in.expected[nc];
    const std::vector<std::uint8_t>& now = *in.observed[nc];
    const std::vector<std::uint8_t>& info = *in.expected[ni];
    const CpGeometry g = native_geometry(in.binding);
    if (old.size() != now.size() || static_cast<std::int64_t>(old.size()) != g.control_size) return false;
    std::uint32_t mask = 0;
    if (!u32_at(info, wide(4), mask, error)) return std::nullopt;
    if (!in.binding.native_index) return fail(error, CpErrorKind::key_error, "native_index"), std::nullopt;
    const std::int64_t selected = *in.binding.native_index;
    std::vector<bool> allowed(old.size(), false);
    for (std::int64_t i = 0; i < 32; ++i) {
        if (!(mask >> i & 1u) || i == selected) continue;
        const Wide at = add(add(wide(g.control_base), mul(wide(i), wide(g.control_stride))), wide(4));
        const Wide row = add(wide(g.info_base), mul(wide(i), wide(g.info_stride)));
        if (slices_equal(old, now, at)) continue;
        std::int64_t bounds = 0;
        if (!unpack_offset(info.size(), add(row, wide(8)), 12, bounds, error)) return std::nullopt;
        const std::uint32_t minimum = le32(info, bounds), rated = le32(info, bounds + 4), maximum = le32(info, bounds + 8);
        std::uint32_t value = 0, other = 0;
        if (!byte_at(info, row, value, error)) return std::nullopt;
        if (value != 0x30) return false;
        if (!byte_at(info, add(row, wide(6)), value, error)) return std::nullopt;
        if (value != 0 && value != 1) return false;
        if (!u32_at(old, add(at, wide(-4)), value, error)) return std::nullopt;
        if (!byte_at(info, add(row, wide(4)), other, error)) return std::nullopt;
        if (value != other) return false;
        if (!(minimum <= rated && rated <= maximum) || maximum == 0) return false;
        if (!u32_at(old, at, value, error)) return std::nullopt;
        if (value == kCpMissing) return false;
        if (!u32_at(now, at, value, error)) return std::nullopt;
        if (value == kCpMissing) return false;
        // allowed.update(range(at, at + 4)): only indices of the payload can ever be consulted.
        const std::int64_t n = static_cast<std::int64_t>(old.size());
        if (!at.beyond && at.v > -4 && at.v < n)
            for (std::int64_t j = std::max<std::int64_t>(at.v, 0); j < std::min<std::int64_t>(at.v + 4, n); ++j)
                allowed[static_cast<std::size_t>(j)] = true;
    }
    for (std::size_t j = 0; j < old.size(); ++j)
        if (old[j] != now[j] && !allowed[j]) return false;
    return true;
}

}  // namespace druta::backend
