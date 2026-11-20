// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// PCI/BIT parsing follows Druta's vbios_fields.py (ROM layout research adapted
// there from Vix Editor, Copyright (C) 2026 panchovix, GPL-3.0-or-later).
#include "pascal_clock_table.h"
#include "vbios_voltage_graph.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <map>
#include <optional>
#include <set>
#include <stdexcept>

namespace druta::ui {
namespace {
using Bytes = std::vector<uint8_t>;
constexpr unsigned frequency_mask = 0x3fff;
constexpr int minimum_ratio = 1;
constexpr int maximum_ratio = 200;

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

    std::optional<size_t> pointer(unsigned tag, unsigned offset, bool allow_version_one = false) const {
        const auto it = tokens.find(tag);
        if (it == tokens.end() || (it->second.version != 2 && !(allow_version_one && it->second.version == 1)) ||
            offset + 4 > it->second.payload.size) return {};
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
            const auto domain = i == 1 ? PascalClockDomain::xbar : PascalClockDomain::sys;
            if (matches == 1 && b[ratio] >= minimum_ratio && b[ratio] <= maximum_ratio)
                out.push_back({domain, i, le16(b, program(*dependent) + 1), ratio});
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
                const std::vector<Binding>& bindings, const PascalClockTable* original) {
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
            if (!floor || floor > nominal || nominal > maximum || !bind.program_cap) continue;
            // An unowned image must retain the original firmware tuple qualification.
            // Only a verified receipt can establish that a higher value is ours.
            if (!original && maximum > bind.program_cap) continue;
            int upper = int(std::min(frequency_mask, bind.program_cap * 2u));
            if (original) {
                const auto prior = std::find_if(original->fields.begin(), original->fields.end(), [&](const auto& old) {
                    return old.domain == bind.domain && old.pstate == 15 - int(level);
                });
                // Do not promote a tuple that was unqualified in the original
                // image, or hide the independently qualified sibling fields.
                if (prior == original->fields.end()) continue;
                upper = prior->maximum;
            }
            // Never expand the authorized range from an observed value. A
            // value outside it remains readable but cannot authorize an edit.
            out.located.push_back({{bind.domain, 15 - int(level), int(maximum), int(nominal),
                                    upper}, tuple + 2});
        }
    }
}

// Nine-clock PERF 4.x layout from Druta's decode_kepler_clocks.py
// (GK104 and GM107 captures). Clock records 0, 1 and 4 use doubled MHz.
void add_legacy_targets(Decoded& out, const Directory& dir, const PascalClockTable* original) {
    const auto perf = dir.pointer('P', 0, true);
    require(perf.has_value(), "Missing performance table pointer");
    const auto& b = dir.bytes;
    const size_t at = *perf;
    dir.own({at, 6}, true);
    const unsigned header = b[at + 1], row = b[at + 2], entry = b[at + 3], domains = b[at + 4], count = b[at + 5];
    require(b[at] == 0x40 && header >= 6 && row >= 1 && entry == 4 && domains == 9 && count && count <= 16,
            "Unrecognized legacy performance clock layout");
    const unsigned stride = row + domains * entry;
    dir.own({at, header + count * stride}, true);
    const std::array<std::pair<PascalClockDomain, unsigned>, 3> columns{{
        {PascalClockDomain::gpc, 0}, {PascalClockDomain::xbar, 1}, {PascalClockDomain::sys, 4}
    }};
    std::set<int> states;
    for (unsigned i = 0; i < count; ++i) {
        const size_t start = at + header + i * stride;
        const int level = b[start];
        if (level == 0xff) continue;
        require(level <= 15 && states.insert(15 - level).second, "Unexpected or duplicate legacy performance level");
        for (const auto& [domain, column] : columns) {
            const size_t offset = start + row + column * entry;
            const int raw = int(le16(b, offset) & frequency_mask);
            if (raw && raw != frequency_mask)
                out.located.push_back({{domain, 15 - level, raw, raw, raw}, offset});
        }
    }
    out.table.state_targets = true;
    if (original)
        require(original->ok && original->state_targets && original->device_id == out.table.device_id,
                "Original clock ranges belong to a different layout or device");
    // This table contains targets, not operating limits. Offer the range of
    // targets the adapter already programs for each domain across its states.
    for (auto& l : out.located) {
        auto& f = l.field;
        if (original) {
            const auto prior = std::find_if(original->fields.begin(), original->fields.end(), [&](const auto& old) {
                return old.domain == f.domain && old.pstate == f.pstate;
            });
            require(prior != original->fields.end() && prior->minimum <= f.current && f.current <= prior->maximum,
                    "Running clock differs from the original target range");
            f.minimum = prior->minimum;
            f.maximum = prior->maximum;
        } else {
            for (const auto& peer : out.located) if (peer.field.domain == f.domain) {
                f.minimum = std::min(f.minimum, peer.field.current);
                f.maximum = std::max(f.maximum, peer.field.current);
            }
        }
    }
}

Decoded decode(const Bytes& b, uint32_t architecture, const PascalClockTable* original) {
    require(clock_override_architecture(architecture), "No firmware clock override layout for this architecture");
    Decoded out;
    const Directory dir(b);
    const auto i = dir.tokens.find('i');
    require(i != dir.tokens.end() && i->second.version == 2 && i->second.payload.size >= 5,
            "Missing VBIOS selector");
    const size_t selector = i->second.payload.at;
    out.table.selector_version = le32(b, selector);
    out.table.selector_oem = b[selector + 4];
    out.table.device_id = static_cast<uint16_t>(dir.legacy.device);
    if (architecture == 4) {
        if (original)
            require(original->ok && !original->state_targets && original->device_id == out.table.device_id,
                    "Original clock ranges belong to a different layout or device");
        const auto tables = read_programs(dir);
        const auto bindings = tables.bindings();
        add_states(out, dir, tables, bindings, original);
        for (const auto& bound : bindings) {
            if (bound.ratio)
                out.located.push_back({{bound.domain, -1, b[*bound.ratio], minimum_ratio, maximum_ratio},
                                       *bound.ratio});
        }
    } else add_legacy_targets(out, dir, original);
    require(!out.located.empty(), "No qualified firmware clock fields");
    for (const auto& l : out.located) out.table.fields.push_back(l.field);
    out.table.ok = true;
    return out;
}

// BIT P+4 maps each memory frequency band and strap column to a BIT P+8
// tweak row. A controller fingerprint must resolve one row in the P0 band.
Span timing_row(const Directory& dir, uint32_t architecture, const PascalTimingSample& sample) {
    require(architecture == 4, "BIOS memory timing override is supported only on Pascal");
    require(sample.memory_mhz > 0 && sample.memory_mhz <= 0x3fff && (sample.present&0x0a)==0x0a &&
            !(sample.present&0xc0) && (sample.config1 || sample.config3),
            "Missing P0 memory clock or controller fingerprint");
    const auto& b = dir.bytes;
    struct Rows { Span extent; size_t first; unsigned base, sub, columns, count, stride; };
    const auto rows = [&](unsigned pointer, unsigned version, unsigned minimum_base) {
        const auto pos = dir.pointer('P', pointer);
        require(pos.has_value(), "Missing memory table pointer");
        dir.own({*pos, 6});
        const unsigned header=b[*pos+1], base=b[*pos+2], sub=b[*pos+3], columns=b[*pos+4], count=b[*pos+5];
        require(b[*pos] == version && header >= 6 && base >= minimum_base && count && (!columns || sub),
                "Unrecognized memory table geometry");
        const unsigned stride = base + sub * columns;
        const Span extent{*pos, header + stride * count};
        dir.own(extent);
        return Rows{extent, *pos + header, base, sub, columns, count, stride};
    };
    const auto bands = rows(4, 0x11, 4), timings = rows(8, 0x20, 24);
    require(bands.columns && !bands.extent.overlaps(timings.extent), "Invalid memory band and timing table layout");
    // Ignore unrecognized tables elsewhere in the image, but keep the parsed
    // memory tables disjoint from the clock structures patched by this planner.
    const auto programs=read_programs(dir);
    auto distinct=[&](Span other){return !bands.extent.overlaps(other) && !timings.extent.overlaps(other);};
    require(distinct(programs.domains) && distinct(programs.programs),"Memory and clock program tables overlap");
    if(const auto perf=dir.pointer('P',0)) {
        dir.own({*perf,10});
        require(b[*perf]==0x50 && b[*perf+1]==10 && b[*perf+2]==3 && b[*perf+4]==6 && b[*perf+5]==9,
                "Unrecognized Pascal performance-state layout");
        const Span extent{*perf,10+size_t(b[*perf+3])*57};
        dir.own(extent);
        require(distinct(extent),"Memory and performance tables overlap");
    }
    std::optional<unsigned> selected;
    for (unsigned i = 0; i < bands.count; ++i) {
        const size_t at = bands.first + i * bands.stride;
        const unsigned lo = le16(b, at) & 0x3fff, hi = le16(b, at + 2) & 0x3fff;
        require(lo <= hi, "Invalid memory band frequency range");
        if (unsigned(sample.memory_mhz) < lo || unsigned(sample.memory_mhz) > hi) continue;
        require(!selected, "P0 memory clock matches multiple memory bands");
        selected = i;
    }
    require(selected.has_value(), "P0 memory clock does not match a memory band");
    const auto row_index = [&](unsigned band, unsigned col) {
        return b[bands.first + band * bands.stride + bands.base + col * bands.sub];
    };
    std::optional<unsigned> matched;
    for (unsigned col = 0; col < bands.columns; ++col) {
        const unsigned index = row_index(*selected, col);
        if (index == 255) continue;
        require(index < timings.count, "Memory strap selects a timing row outside the table");
        const size_t at = timings.first + index * timings.stride;
        // GP107 runtime may mask CONFIG3's upper byte. Preserve it in the
        // image; physical verification compares the entire observed word.
        const uint32_t comparable=sample.config3>>24 ? UINT32_MAX : 0x00ffffffu;
        if (le32(b, at+4) != sample.config1 || ((le32(b, at+12) ^ sample.config3) & comparable)) continue;
        require(!matched || *matched == index, "P0 controller fingerprint matches multiple timing rows");
        matched = index;
    }
    require(matched.has_value(), "No P0 timing row matches the controller fingerprint");
    for (unsigned i = 0; i < bands.count; ++i) {
        if (i == *selected) continue;
        const size_t at = bands.first + i * bands.stride;
        if (!(le16(b, at) & 0x3fff) && !(le16(b, at+2) & 0x3fff)) continue;
        for (unsigned col = 0; col < bands.columns; ++col)
            require(row_index(i, col) != *matched, "P0 timing row is also referenced by another memory band");
    }
    return {timings.first + *matched * timings.stride, timings.base};
}

int timing_maximum(const PascalTimingField& field) { return int((1u<<field.width)-1); }
uint32_t timing_mask(const PascalTimingField& field) { return uint32_t(timing_maximum(field))<<field.shift; }
bool timing_matches_row(const Bytes& image,Span row,const PascalTimingField& field,const PascalTimingSample& sample) {
    if (!(sample.present & (1u<<field.word))) return false;
    const auto offset=size_t(field.word)*4;
    if(offset+4>row.size)return false;
    const auto stored=le32(image,row.at+offset);
    const auto physical=sample.word(field.word);
    // Some Pascal controllers expose a zero CONFIG3 upper byte with nonzero CCD values on disk.
    if(field.word==3 && field.shift>=24 && (stored>>24)!=(physical>>24))return false;
    return ((stored^physical)&timing_mask(field))==0;
}

const PascalClockField* find_field(const PascalClockTable& table, PascalClockDomain domain, int pstate) {
    const auto found = std::find_if(table.fields.begin(), table.fields.end(), [&](const PascalClockField& field) {
        return field.domain == domain && field.pstate == pstate;
    });
    return found == table.fields.end() ? nullptr : &*found;
}

std::optional<int> preview_value(const PascalClockField* field, const std::vector<PascalClockEdit>& edits) {
    if (!field) return {};
    std::optional<int> staged;
    for (const auto& edit : edits) {
        if (edit.domain != field->domain || edit.pstate != field->pstate) continue;
        if (staged || edit.expected != field->current || field->current < field->minimum ||
            field->current > field->maximum || edit.value < field->minimum || edit.value > field->maximum)
            return {};
        staged = edit.value;
    }
    return staged.value_or(field->current);
}

std::optional<double> positive_frequency(double mhz) {
    if (!std::isfinite(mhz) || mhz <= 0) return {};
    return mhz;
}
}

bool clock_override_architecture(uint32_t architecture) {
    return architecture == 2 || architecture == 3 || architecture == 4;
}

const std::vector<PascalTimingField>& pascal_timing_fields() {
    // The word and bit ranges are NVIDIA's v0x20 Memory Tweak Table base entry.
    // https://nvidia.github.io/open-gpu-doc/MemoryTweakTable/MemoryTweakTable.html
    using D=PascalClockDomain;
    static const std::vector<PascalTimingField> fields{
        {D::trc,"trc","tRC",0,0,8,true}, {D::trfc,"trfc","tRFC",0,8,9,true},
        {D::tras,"tras","tRAS",0,17,7,true}, {D::trp,"trp","tRP",0,24,7,true},
        {D::tcl,"tcl","tCL",1,0,7,false}, {D::twl,"twl","tWL",1,7,7,true},
        {D::trcd_rd,"trcd_rd","tRCDRD",1,14,6,true}, {D::trcd_wr,"trcd_wr","tRCDWR",1,20,6,true},
        {D::rpre,"rpre","RPRE",2,0,4,true}, {D::wpre,"wpre","WPRE",2,4,4,true},
        {D::cdlr,"cdlr","CDLR",2,8,7,true}, {D::twr,"twr","tWR",2,16,7,true},
        {D::w2r_bus,"w2r_bus","W2R_BUS",2,24,4,true}, {D::r2w_bus,"r2w_bus","R2W_BUS",2,28,4,true},
        {D::pdex,"pdex","PDEX",3,0,5,true}, {D::pden2pdex,"pden2pdex","PDEN2PDEX",3,5,4,true},
        {D::tfaw,"tfaw","tFAW",3,9,8,true}, {D::aond,"aond","AOND",3,17,7,true},
        {D::tccdl,"tccdl","tCCDL",3,24,4,true}, {D::tccds,"tccds","tCCDS",3,28,4,true},
        {D::trefi,"trefi","tREFI",4,3,12,true}, {D::trrd,"trrd","tRRD",4,15,6,true},
        {D::wrcrc,"wrcrc","WRCRC",5,4,7,true}
    };
    return fields;
}
const PascalTimingField* pascal_timing_field(PascalClockDomain domain) {
    for(const auto& field:pascal_timing_fields())if(field.domain==domain)return &field;
    return nullptr;
}
const char* pascal_timing_label(PascalClockDomain domain) {
    const auto* field=pascal_timing_field(domain);
    return field?field->label:"";
}
namespace {
using TimingMember=uint32_t PascalTimingSample::*;
constexpr std::array<TimingMember,6> words{
    &PascalTimingSample::config0,&PascalTimingSample::config1,&PascalTimingSample::config2,&PascalTimingSample::config3,
    &PascalTimingSample::config4,&PascalTimingSample::config5
};
}
uint32_t PascalTimingSample::word(unsigned index) const { return this->*words.at(index); }
void PascalTimingSample::set_word(unsigned index,uint32_t value) {
    this->*words.at(index)=value;
    present|=static_cast<uint8_t>(1u<<index);
}
bool matches_pascal_timing(const PascalTimingSample& sample,const PascalTimingSample& expected) {
    if(sample.memory_mhz!=expected.memory_mhz || (sample.present&expected.present)!=expected.present)return false;
    for(unsigned i=0; i<words.size(); ++i)
        if((expected.present&(1u<<i)) && sample.word(i)!=expected.word(i))return false;
    return true;
}
int pascal_timing_value(const PascalTimingSample& sample,PascalClockDomain domain) {
    const auto* field=pascal_timing_field(domain);
    require(field&&(sample.present&(1u<<field->word)),"Missing controller timing field");
    return int((sample.word(field->word)&timing_mask(*field))>>field->shift);
}
PascalTimingSample with_pascal_timing(PascalTimingSample sample,PascalClockDomain domain,int value) {
    const auto* field=pascal_timing_field(domain);
    require(field&&field->writable&&(sample.present&(1u<<field->word))&&value>=0&&
            value<=timing_maximum(*field),"Invalid or read-only controller timing change");
    const auto updated=(sample.word(field->word)&~timing_mask(*field))|(uint32_t(value)<<field->shift);
    sample.set_word(field->word,updated);
    return sample;
}

PascalClockTable inspect_pascal_clocks(const Bytes& image, uint32_t architecture, const PascalClockTable* original) {
    try { return decode(image, architecture, original).table; }
    catch (const std::exception& e) {
        PascalClockTable out;
        out.message = e.what();
        return out;
    }
}

void inspect_pascal_timings(PascalClockTable& table, const Bytes& image, uint32_t architecture,
                            const PascalTimingSample& sample) {
    table.timing_sample.reset();
    table.timing_fields.clear();
    try {
        require(architecture == 4, "BIOS memory timing override is supported only on Pascal");
        const auto row=timing_row(Directory(image), architecture, sample);
        for(const auto& field:pascal_timing_fields()) {
            if(timing_matches_row(image,row,field,sample))
                table.timing_fields.push_back({field.domain,0,pascal_timing_value(sample,field.domain),
                                               0,timing_maximum(field)});
        }
        table.timing_sample = sample;
        table.timing_message=table.timing_fields.size()==pascal_timing_fields().size()?""
            :"Showing the fields that match both the selected firmware row and the available P0 controller readings.";
    } catch (const std::exception& e) {
        table.timing_message = e.what();
    }
}
void inspect_pascal_tfaw(PascalClockTable& table,const Bytes& image,uint32_t architecture,
                         const PascalTimingSample& sample) {
    inspect_pascal_timings(table,image,architecture,sample);
}

PascalClockPreview pascal_clock_preview(const PascalClockTable& table,
    const std::vector<PascalClockEdit>& edits, PascalClockDomain domain, int pstate,
    std::optional<double> p0_peak_mhz) {
    PascalClockPreview out;
    if (!table.ok || pstate < 0) return out;

    const auto* state = find_field(table, domain, pstate);
    const auto raw = preview_value(state, edits);
    if (table.state_targets) {
        if (raw) out.nominal_mhz = positive_frequency(*raw / 2.0);
        return out;
    }

    if (raw) out.ceiling_mhz = *raw / 2.0;
    if (!p0_peak_mhz || !positive_frequency(*p0_peak_mhz)) return out;
    if (domain == PascalClockDomain::gpc) {
        out.nominal_mhz = p0_peak_mhz;
    } else if (const auto ratio = preview_value(find_field(table, domain, -1), edits)) {
        out.nominal_mhz = positive_frequency(*p0_peak_mhz * (*ratio / 100.0));
    }
    return out;
}

PascalClockPlan plan_pascal_clocks(const Bytes& image, const std::vector<PascalClockEdit>& edits,
                                  uint32_t architecture, const PascalClockTable* original) {
    try {
        const auto decoded = decode(image, architecture, original);
        require(!edits.empty(), "Select at least one clock field");
        std::set<size_t> seen;
        std::set<PascalClockDomain> timing_edits;
        std::optional<Span> selected_row;
        std::optional<PascalTimingSample> timing_baseline;
        Bytes patched = image;
        for (const auto& edit : edits) {
            if (const auto* field=pascal_timing_field(edit.domain)) {
                require(field->writable && edit.pstate==0 && edit.timing_sample &&
                        edit.value>=0 && edit.value<=timing_maximum(*field),
                        "Read-only timing or invalid P0 timing request");
                const auto& sample=*edit.timing_sample;
                const auto row=timing_row(Directory(image),architecture,sample);
                require(timing_matches_row(image,row,*field,sample) && pascal_timing_value(sample,edit.domain)==edit.expected,
                        "Timing field differs between the P0 sample and firmware row");
                require(timing_edits.insert(edit.domain).second,"Duplicate timing edit");
                if(!timing_baseline) {timing_baseline=sample;selected_row=row;}
                else {
                    require(selected_row->at==row.at && timing_baseline->memory_mhz==sample.memory_mhz,
                            "Mixed timing rows or P0 clocks in one transaction");
                    for(unsigned i=0;i<words.size();++i)if(sample.present&(1u<<i)) {
                        require(!(timing_baseline->present&(1u<<i))||timing_baseline->word(i)==sample.word(i),
                                "Contradictory P0 controller samples in one transaction");
                        timing_baseline->set_word(i,sample.word(i));
                    }
                }
                const size_t at=row.at+field->word*4;
                const uint32_t patched_word=(le32(patched,at)&~timing_mask(*field))|(uint32_t(edit.value)<<field->shift);
                for (unsigned j = 0; j < 4; ++j) patched[at+j] = static_cast<uint8_t>(patched_word >> (8*j));
                continue;
            }
            require(!edit.timing_sample, "Unexpected timing fingerprint on a clock edit");
            const auto it = std::find_if(decoded.located.begin(), decoded.located.end(), [&](const LocatedField& l) {
                return l.field.domain == edit.domain && l.field.pstate == edit.pstate;
            });
            require(it != decoded.located.end(), "Requested clock field is unavailable on this image");
            require(seen.insert(it->offset).second, "Clock edit selected the same field twice");
            const auto& f = it->field;
            require(edit.expected == f.current, "Captured clock field changed since inspection");
            require(f.current >= f.minimum && f.current <= f.maximum, "Captured clock exceeds the qualified range");
            require(edit.value >= f.minimum && edit.value <= f.maximum, "Clock setting exceeds the qualified range");
            if (f.pstate == -1) {
                patched[it->offset] = static_cast<uint8_t>(edit.value);
            } else if (decoded.table.state_targets) {
                const unsigned word = (le16(image, it->offset) & ~frequency_mask) | unsigned(edit.value);
                patched[it->offset] = static_cast<uint8_t>(word);
                patched[it->offset + 1] = static_cast<uint8_t>(word >> 8);
            } else {
                // PERF 5.x PARAM1[27:14] is the maximum. Both nibble/byte
                // boundaries are shared with other (including reserved) bits.
                const uint32_t packed = (le32(image, it->offset) & ~(frequency_mask << 14)) |
                                        (uint32_t(edit.value) << 14);
                for (unsigned j = 0; j < 4; ++j)
                    patched[it->offset + j] = static_cast<uint8_t>(packed >> (8 * j));
            }
        }
        if(timing_baseline) {
            for(const auto& edit:edits)if(pascal_timing_field(edit.domain))
                *timing_baseline=with_pascal_timing(*timing_baseline,edit.domain,edit.value);
            require(timing_row(Directory(patched),architecture,*timing_baseline).at==selected_row->at,
                    "Edited P0 fingerprint no longer identifies the same timing row");
        }
        return {true, {}, std::move(patched)};
    } catch (const std::exception& e) {
        return {false, e.what(), {}};
    }
}
}
