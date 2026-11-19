// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// PCI/BIT parsing follows Druta's vbios_fields.py (ROM layout research adapted
// there from Vix Editor, Copyright (C) 2026 panchovix, GPL-3.0-or-later).
#include "pascal_clock_table.h"
#include "vbios_voltage_graph.h"
#include <algorithm>
#include <array>
#include <cstring>
#include <map>
#include <optional>
#include <set>
#include <stdexcept>

namespace druta::ui {
namespace {
using Bytes = std::vector<uint8_t>;
constexpr unsigned frequency_mask = 0x3fff;

void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

struct Span {
    size_t at, size;
    size_t end() const { return at + size; }
    bool contains(Span other) const { return at <= other.at && other.end() <= end(); }
    bool overlaps(Span other) const { return at < other.end() && other.at < end(); }
};

unsigned le16(const Bytes& b, size_t at) {
    return b[at] | (unsigned(b[at + 1]) << 8);
}
uint32_t le32(const Bytes& b, size_t at) {
    return le16(b, at) | (uint32_t(le16(b, at + 2)) << 16);
}
struct Image { Span extent; int vendor, device, type; bool npds; };
struct Token { unsigned version; Span payload; };

class Directory {
public:
    explicit Directory(const Bytes& b) : bytes(b) {
        require(b.size() >= 26 && b.size() <= vbios_voltage_max_image_bytes, "Invalid VBIOS capture size");
        find_images();
        find_bit();
    }
    const Bytes& bytes;
    Image legacy{};
    std::optional<Image> efi;
    std::map<unsigned, Token> tokens;

    std::optional<size_t> pointer(unsigned tag, unsigned offset) const {
        const auto it = tokens.find(tag);
        if (it == tokens.end() || it->second.version != 2 || offset + 4 > it->second.payload.size) return {};
        uint64_t ptr = le32(bytes, it->second.payload.at + offset);
        if (!ptr) return {};
        if (efi && ptr >= legacy.extent.end()) ptr += efi->extent.size;
        require(ptr < bytes.size(), "BIT table pointer exceeds the captured image");
        return static_cast<size_t>(ptr);
    }
    void own(Span table, bool legacy_only = false) const {
        require(table.at <= bytes.size() && table.size <= bytes.size() - table.at,
                "Clock table exceeds the captured image");
        const bool owned = std::any_of(images.begin(), images.end(), [&](const Image& image) {
            return image.vendor == 0x10de && image.extent.end() <= bytes.size()
                && (image.extent.at == 0 || (!legacy_only && image.npds && image.type == 0xe0))
                && image.extent.contains(table);
        });
        require(owned, "Clock table has no understood complete PCI/NPDS owner");
        for (const Span protected_span : metadata)
            require(!table.overlaps(protected_span), "Clock table overlaps the image or BIT directory");
    }
private:
    std::vector<Image> images;
    std::vector<Span> metadata;

    bool magic(size_t at, const char* value, size_t n) const {
        return at <= bytes.size() && n <= bytes.size() - at && !std::memcmp(bytes.data() + at, value, n);
    }
    void find_images() {
        unsigned legacy_count = 0;
        for (size_t start = 0; start + 26 <= bytes.size(); start += 512) {
            const bool npds = magic(start, "VN", 2);
            if (!npds && !magic(start, "\x55\xaa", 2)) continue;
            const size_t header = start + le16(bytes, start + 24);
            if (!magic(header, npds ? "NPDS" : "PCIR", 4)) continue;
            require(header >= start + 26 && header + 24 <= bytes.size(), "Invalid PCI/NPDS header");
            const size_t size = le16(bytes, header + 16) * 512u;
            const size_t hs = le16(bytes, header + 10);
            require(hs >= 24 && header + hs <= start + size && header + hs <= bytes.size(),
                    "Invalid PCI/NPDS image size");
            require(images.empty() || images.back().extent.end() <= start, "Overlapping PCI/NPDS images");
            Image image{{start, size}, int(le16(bytes, header + 4)), int(le16(bytes, header + 6)),
                        bytes[header + 20], npds};
            images.push_back(image);
            metadata.push_back({start, 26});
            metadata.push_back({header, hs});
            if (image.vendor == 0x10de && image.type == 0 && !npds) {
                ++legacy_count;
                legacy = image;
            }
        }
        require(legacy_count == 1 && legacy.extent.at == 0 && legacy.extent.end() <= bytes.size(),
                "Expected one complete NVIDIA legacy image starting at zero");
        for (const Image& img : images) {
            if (img.extent.at == legacy.extent.end() && img.type == 3) {
                require(img.extent.end() <= bytes.size()
                    && ((img.vendor == 0 && img.device == 0) ||
                        (img.vendor == legacy.vendor && (!img.device || img.device == legacy.device))),
                    "Truncated or inconsistent EFI image");
                efi = img;
            }
            if (efi && img.extent.at == efi->extent.end() && img.type == 0xe0)
                require(img.npds, "Object-archive VBIOS does not use the Pascal table layout");
        }
    }
    void find_bit() {
        unsigned found = 0;
        for (size_t at = 0; at + 6 <= legacy.extent.end(); ++at) {
            if (!magic(at, "\xff\xb8" "BIT\0", 6)) continue;
            require(++found == 1 && at + 12 <= legacy.extent.end(), "Ambiguous or incomplete BIT directory");
            const unsigned hs = bytes[at + 8], stride = bytes[at + 9], count = bytes[at + 10];
            const size_t size = hs + stride * count;
            require(hs >= 12 && stride >= 6 && count && at + size <= legacy.extent.end(), "Invalid BIT directory dimensions");
            unsigned sum = 0;
            for (size_t p = at; p < at + hs; ++p) sum += bytes[p];
            require(!(sum & 255), "BIT header checksum mismatch");
            metadata.push_back({at, size});
            for (unsigned i = 0; i < count; ++i) {
                const size_t row = at + hs + stride * i;
                const Span payload{le16(bytes, row + 4), le16(bytes, row + 2)};
                require(payload.end() <= legacy.extent.end() &&
                        tokens.emplace(bytes[row], Token{bytes[row + 1], payload}).second,
                        "Duplicate or out-of-bounds BIT token");
                if (payload.size) metadata.push_back(payload);
            }
        }
        require(found == 1, "No BIT directory in the legacy image");
    }
};

struct LocatedField { PascalClockField field; size_t offset; };
struct Decoded { PascalClockTable table; std::vector<LocatedField> located; };

// GP10x BIOS clock-domain HAL 2 maps indices 0, 1, 3 to GPC2, XBAR2, SYS2.
// The C v0x10 entry gives the master index and program range; a domain name
// alone does not identify the writable ratio byte.
struct Binding { PascalClockDomain domain; unsigned index, program_cap; std::optional<size_t> ratio; };
struct Programs {
    const Bytes& b;
    Span domains, programs;
    unsigned count, stride, slots;
    size_t row(unsigned i) const { return domains.at + 7 + 9 * i; }
    size_t program(unsigned i) const { return programs.at + 8 + stride * i; }
    std::optional<unsigned> owned_program(unsigned i, unsigned role, unsigned kind) const {
        const size_t at = row(i);
        const unsigned idx = b[at + 1];
        if ((b[at] & 31u) != role || idx != b[at + 2] || idx >= count || (b[program(idx)] & 0x7fu) != kind)
            return {};
        for (unsigned other = 0; other < 9; ++other) {
            if (other == i) continue;
            const size_t p = row(other);
            if (b[p + 1] <= idx && idx <= b[p + 2]) return {};
        }
        return idx;
    }
    std::vector<Binding> bindings() const {
        const auto master = owned_program(0, 1, 0x20);
        if (!master) return {};
        const size_t at = program(*master);
        std::vector<Binding> out{{PascalClockDomain::gpc, 0, le16(b, at + 1), {}}};
        for (unsigned i : {1u, 3u}) {
            const auto dependent = owned_program(i, 2, 0x22);
            if (!dependent || *dependent == *master || (b[row(i) + 3] & 15) != 0) continue;
            unsigned matches = 0;
            size_t ratio = 0;
            for (unsigned j = 0; j < slots; ++j) {
                const size_t slot = at + 13 + 3 * j;
                if (b[slot] == i) { ++matches; ratio = slot + 1; }
            }
            if (matches == 1 && b[ratio] >= 1 && b[ratio] <= 100)
                out.push_back({i == 1 ? PascalClockDomain::xbar : PascalClockDomain::sys,
                               i, le16(b, program(*dependent) + 1), ratio});
        }
        return out;
    }
};

Programs read_programs(const Directory& dir) {
    const Bytes& b = dir.bytes;
    const auto c = dir.pointer('C', 8), p = dir.pointer('C', 12);
    require(c && p, "No Pascal clock domain or clock program table");
    const Span domains{*c, 7 + 9 * 9};
    dir.own(domains, true);
    require(b[*c] == 0x10 && b[*c + 1] == 7 && b[*c + 2] == 9 && b[*c + 3] == 9 && b[*c + 4] == 2,
            "Unrecognized Pascal clock-domain table or HAL");
    dir.own({*p, 8}, true);
    const unsigned count = b[*p + 3], slots = b[*p + 5], vf_count = b[*p + 7];
    require(b[*p] == 0x10 && b[*p + 1] == 8 && b[*p + 2] == 13 &&
            b[*p + 4] == 3 && b[*p + 6] == 2 && count && slots && vf_count,
            "Unrecognized Pascal clock program layout");
    const unsigned stride = 13 + slots * 3 + vf_count * 2;
    const Span programs{*p, 8 + stride * count};
    dir.own(programs, true);
    require(!domains.overlaps(programs), "Clock domain and program tables overlap");
    return {b, domains, programs, count, stride, slots};
}

void add_states(Decoded& out, const Directory& dir, const Programs& tables,
                const std::vector<Binding>& bindings) {
    const auto perf = dir.pointer('P', 0);
    if (!perf) return;
    const Bytes& b = dir.bytes;
    const Span header{*perf, 10};
    require(!header.overlaps(tables.domains) && !header.overlaps(tables.programs), "Performance and clock tables overlap");
    dir.own(header);
    if (b[*perf] != 0x50 || b[*perf + 1] != 10 || b[*perf + 2] != 3 || b[*perf + 4] != 6 || b[*perf + 5] != 9)
        return;
    const unsigned count = b[*perf + 3], stride = 3 + 9 * 6;
    const Span extent{*perf, 10 + count * stride};
    dir.own(extent);
    require(!extent.overlaps(tables.domains) && !extent.overlaps(tables.programs), "Performance and clock tables overlap");
    std::set<int> states;
    for (unsigned i = 0; i < count; ++i) {
        const size_t at = header.end() + i * stride;
        const unsigned level = b[at];
        if (level == 0xff) continue;
        require(level <= 15 && states.insert(15 - int(level)).second, "Unexpected or duplicate performance level");
        for (const auto& bind : bindings) {
            const size_t tuple = at + 3 + bind.index * 6;
            const unsigned nominal = le16(b, tuple) & frequency_mask;
            const uint32_t packed = le32(b, tuple + 2);
            const unsigned floor = packed & frequency_mask, maximum = (packed >> 14) & frequency_mask;
            if (!floor || floor > nominal || nominal > maximum || maximum > bind.program_cap) continue;
            out.located.push_back({{bind.domain, 15 - int(level), int(maximum), int(nominal),
                                    int(std::min(bind.program_cap, frequency_mask))}, tuple + 2});
        }
    }
}

Decoded decode(const Bytes& b) {
    Decoded out;
    const Directory dir(b);
    const auto i = dir.tokens.find('i');
    require(i != dir.tokens.end() && i->second.version == 2 && i->second.payload.size >= 5,
            "Missing VBIOS selector");
    const size_t selector = i->second.payload.at;
    out.table.selector_version = le32(b, selector);
    out.table.selector_oem = b[selector + 4];
    out.table.device_id = static_cast<uint16_t>(dir.legacy.device);
    const auto tables = read_programs(dir);
    const auto bindings = tables.bindings();
    add_states(out, dir, tables, bindings);
    for (const auto& bound : bindings) {
        if (bound.ratio)
            out.located.push_back({{bound.domain, -1, b[*bound.ratio], 1, 100}, *bound.ratio});
    }
    require(!out.located.empty(), "No qualified Pascal clock fields");
    for (const auto& l : out.located) out.table.fields.push_back(l.field);
    out.table.ok = true;
    return out;
}
}

PascalClockTable inspect_pascal_clocks(const Bytes& image) {
    try { return decode(image).table; }
    catch (const std::exception& e) {
        PascalClockTable out;
        out.message = e.what();
        return out;
    }
}

PascalClockPlan plan_pascal_clocks(const Bytes& image, const std::vector<PascalClockEdit>& edits) {
    try {
        const auto decoded = decode(image);
        require(!edits.empty(), "Select at least one clock field");
        std::set<size_t> seen;
        Bytes patched = image;
        for (const auto& edit : edits) {
            const auto it = std::find_if(decoded.located.begin(), decoded.located.end(), [&](const LocatedField& l) {
                return l.field.domain == edit.domain && l.field.pstate == edit.pstate;
            });
            require(it != decoded.located.end(), "Requested clock field is unavailable on this image");
            require(seen.insert(it->offset).second, "Clock edit selected the same field twice");
            const auto& f = it->field;
            require(edit.expected == f.current, "Captured clock field changed since inspection");
            require(edit.value >= f.minimum && edit.value <= f.maximum, "Clock setting exceeds the qualified range");
            if (f.pstate == -1) {
                patched[it->offset] = static_cast<uint8_t>(edit.value);
            } else {
                // PERF 5.x PARAM1[27:14] is the maximum. Both nibble/byte
                // boundaries are shared with other (including reserved) bits.
                const uint32_t packed = (le32(image, it->offset) & ~(frequency_mask << 14)) |
                                        (uint32_t(edit.value) << 14);
                for (unsigned j = 0; j < 4; ++j)
                    patched[it->offset + j] = static_cast<uint8_t>(packed >> (8 * j));
            }
        }
        return {true, {}, std::move(patched)};
    } catch (const std::exception& e) {
        return {false, e.what(), {}};
    }
}
}
