// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Read-only port of the bounded field descriptions in src/druta/vbios_fields.py.
// Format research adapted there from NVIDIA VBIOS Vix Editor (panchovix, GPL-3.0-or-later).
#include "vbios_override_inventory.h"
#include "pascal_clock_table.h"
#include "vbios_rail_clamps.h"
#include "vbios_rom_reader.h"
#include <array>
#include <set>
#include <sstream>
#include <stdexcept>

namespace druta::ui {
namespace {
struct Table {
    size_t at, header, stride, count;
    size_t row(size_t i) const { return at + header + stride * i; }
};

struct IdleTable {
    const char* name;
    uint32_t pointer, owner;
    uint8_t header, stride;
};
constexpr IdleTable archive_idle[] = {
    {"GR PG", 0x80, 0x39, 10, 5}, {"MS", 0x84, 0x3a, 7, 16},     {"GR RG", 0xd4, 0x7e, 12, 1},
    {"DIFR", 0xe8, 0x8e, 11, 18}, {"NVDEC", 0x11c, 0xb4, 10, 2}, {"NVENC", 0x120, 0xb5, 10, 2},
    {"OFA", 0x124, 0xb6, 10, 2},  {"NVJPG", 0x128, 0xb7, 10, 2},
};
constexpr IdleTable legacy_idle[] = {
    {"GR PG", 0x80, 0, 10, 5},
    {"MS", 0x84, 0, 7, 15},
    {"GR RG", 0xd4, 0, 9, 1},
    {"DIFR", 0xe8, 0, 11, 17},
};

class InventoryReader : public VbiosRomReader {
  public:
    explicit InventoryReader(const std::vector<uint8_t>& image) : VbiosRomReader(image) {}

    VbiosOverrideInventory read(uint32_t device, uint32_t architecture) {
        read_directory();
        require(device && image_regions.front().device == device, "ROM PCI device differs from the selected GPU");
        VbiosOverrideInventory inventory;
        auto add = [&](std::string name, auto inspect) {
            try {
                inventory.families.push_back({name, inspect(), true});
            } catch (const std::runtime_error& error) {
                inventory.families.push_back({name, error.what(), false});
            }
        };
        add("Clock offset ranges", [&] { return clocks(); });
        add("P-state clock overrides", [&] {
            require(clock_override_architecture(architecture), "No P-state clock override decoder for this architecture");
            const auto parsed = inspect_pascal_clocks(b, architecture);
            if (!parsed.ok) throw std::runtime_error(parsed.message);
            require(parsed.device_id == device, "Clock table belongs to a different device");
            std::set<int> states;
            std::ostringstream ratios;
            for (const auto& field : parsed.fields) {
                if (field.pstate >= 0) states.insert(field.pstate);
                else if (field.domain == PascalClockDomain::xbar || field.domain == PascalClockDomain::sys)
                    ratios << ". " << (field.domain == PascalClockDomain::xbar ? "XBAR" : "SYS / Video") << " ratio " << field.current << "%";
            }
            require(!parsed.fields.empty(), "No identified P-state clock fields");
            return std::to_string(parsed.fields.size()) + " clock fields across " + std::to_string(states.size()) +
                " P-states" + ratios.str();
        });
        add("Factory clocks", [&] { return factory(); });
        add("Operating temperature", [&] { return thermal(); });
        add("Fan channels and curves", [&] { return fans(); });
        add("Performance controllers", [&] { return perf(); });
        auto idle_family = [&](const IdleTable& table) {
            add(std::string("Idle power / ") + table.name, [&] { return idle(table); });
        };
        if (archive())
            for (const auto& table : archive_idle)
                idle_family(table);
        else
            for (const auto& table : legacy_idle)
                idle_family(table);
        add("Voltage rails", [&] { return voltage(device); });
        return inventory;
    }

  private:
    std::set<std::string> clock_domains;
    bool archive() const { return !object_regions.empty(); }
    Table table(uint32_t ptr, uint32_t owner, uint8_t version, std::initializer_list<uint8_t> headers,
                uint8_t stride) const {
        const auto at = pointer('P', ptr);
        const uint8_t header = byte(at + 1), count = byte(at + 3);
        require(byte(at) == version && std::find(headers.begin(), headers.end(), header) != headers.end() &&
                    byte(at + 2) == stride && count,
                "Unrecognized table format or empty inventory");
        own(at, header + size_t(stride) * count, owner);
        return {at, header, stride, count};
    }
    std::string clocks() {
        const size_t at = pointer('C', 8);
        own(at, 9, 3);
        constexpr std::array<uint8_t, 9> v40 = {0x40, 13, 15, 10, 3, 1, 3, 0xe8, 3};
        const bool v50 = byte(at) == 0x50 && byte(at + 1) == 13 && byte(at + 2) == 15 && byte(at + 3) == 9 &&
                         byte(at + 4) == 3 && (byte(at + 5) == 1 || byte(at + 5) == 2);
        require(v50 || std::equal(v40.begin(), v40.end(), b.begin() + at), "Unrecognized clock-range layout");
        const Table t{at, 13, 15 + 3u * byte(at + 5), byte(at + 3)};
        own(at, t.header + t.stride * t.count, 3);
        constexpr const char* modern[] = {"GPC",      "XBAR",     "DRAM",     "SYS",     "VID",
                                          "domain 5", "domain 6", "domain 7", "domain 8"};
        constexpr const char* v40_names[] = {"GPC", "XBAR", "DRAM", "SYS",     "HUB",
                                          "VID", "PWR",  "DISP", "PCIEGEN", "HOST"};
        constexpr uint16_t descriptors[] = {0x43, 0x84, 0x182, 0xc5, 0x106, 0x147, 8, 0x200, 0x1c1};
        std::ostringstream out;
        for (size_t i = 0; i < t.count; ++i) {
            const size_t row = t.row(i);
            const auto descriptor = u16(row + 9);
            const bool recognized =
                v50 ? byte(row) == 2 && (descriptor == descriptors[i] || (i == 2 && descriptor == 0x1182))
                    : byte(row) == 1;
            const int low = static_cast<int16_t>(u16(row + 3)), high = static_cast<int16_t>(u16(row + 5));
            if (!recognized || low > high)
                continue;
            const char* name = v50 ? modern[i] : v40_names[i];
            if (!clock_domains.empty())
                out << ", ";
            clock_domains.insert(name);
            out << name << ' ' << low << ".." << high << " MHz";
        }
        require(!clock_domains.empty(), "No identified domain with ordered offset bounds");
        return out.str();
    }
    std::string factory() const {
        const size_t at = pointer('P', 0x98);
        own(at, 27, 0x26);
        constexpr uint8_t header[] = {0x10, 7, 5, 2, 5, 2};
        require(std::equal(std::begin(header), std::end(header), b.begin() + at) && byte(at + 6) <= 1,
                "Unrecognized factory clock layout");
        constexpr const char* names[] = {"GPC offset", "Memory offset", "Base clock", "Boost clock"};
        std::ostringstream out;
        unsigned identified = 0;
        for (unsigned i = 0; i < 4; ++i) {
            const size_t row = at + 7 + i * 5;
            const uint8_t flag = byte(row);
            if (flag > 1 || !clock_domains.count(i == 1 ? "DRAM" : "GPC"))
                continue;
            const int64_t raw = u32(row + 1);
            const int64_t khz = !flag ? 0 : i < 2 ? static_cast<int32_t>(raw) : raw;
            if (khz % 1000 || (i >= 2 && flag && (khz < 1000 || khz > 65535000)))
                continue;
            if (identified++)
                out << ", ";
            out << names[i] << ' ';
            if (flag)
                out << khz / 1000 << " MHz";
            else
                out << "disabled";
        }
        require(identified, "No factory record with a known domain, enable flag and whole-MHz encoding");
        return out.str();
    }
    std::string thermal() const {
        const auto t = archive() ? table(0x50, 0x1b, 0x20, {12, 22}, 48) : table(0x50, 0x1b, 0x10, {11}, 40);
        const size_t i = archive() ? 0 : byte(t.at + 9);
        require(i < t.count, "Operating policy index is outside the table");
        const size_t row = t.row(i);
        const int q5 = static_cast<int16_t>(u16(row + 2));
        require(byte(row) == 1 && (!archive() || byte(row + 1) == 1) && q5 > 0 && q5 == u16(row + 4) &&
                    q5 == u16(row + 6),
                "Operating policy type, channel or three thresholds disagree");
        std::ostringstream out;
        out << q5 / 32.0 << " C in policy " << i << " (three matching thresholds)";
        return out.str();
    }
    std::string fans() const {
        const bool modern = archive();
        const auto c = table(0x58, 0x1d, 0x10, {uint8_t(modern ? 9 : 6)}, 26);
        const auto p = table(0x5c, 0x1e, 0x20, {4}, 51);
        const auto m = table(0x64, 0x20, 0x10, {4}, 5);
        const auto ctl = table(0xdc, 0x80, 0x10, {4}, 13);
        const auto s = table(0xe0, 0x83, 0x10, {4}, 6);
        require(c.count == (modern ? 4u : 2u) && p.count == (modern ? 11u : 8u) && m.count == c.count &&
                    ctl.count == c.count && s.count == c.count,
                "Fan table channel counts disagree");
        std::vector<size_t> active;
        for (size_t i = 0; i < c.count; ++i) {
            const size_t cooler = c.row(i);
            const bool enabled = byte(cooler) != 0;
            for (size_t other : {p.row(i), m.row(i), ctl.row(i), s.row(i)})
                require((byte(other) != 0) == enabled, "Fan enable state differs across related tables");
            if (!enabled)
                continue;
            require(byte(p.row(i) + 1) == 1 && byte(m.row(i) + 1) == i && byte(ctl.row(i) + 2) == i &&
                        byte(ctl.row(i) + 5) == (0x30u | 1u << i) && byte(s.row(i) + 1) == i,
                    "Fan route differs across related tables");
            const auto min = u16(cooler + 14), max = u16(cooler + 16), speed = u16(s.row(i) + 2);
            require(min < max && max <= 32767 && min <= speed && speed <= max &&
                        byte(cooler + 2) < byte(cooler + 3) && byte(cooler + 3) <= 100,
                    "Fan speed or duty limits disagree with the linked channel");
            active.push_back(i);
        }
        require(!active.empty(), "No active GPU-driven fan channel");
        unsigned curves = 0;
        for (size_t i = 0; i < p.count; ++i) {
            const size_t row = p.row(i);
            if (!byte(row))
                continue;
            for (size_t j = 0; j < 3; ++j) {
                const uint16_t temp = u16(row + 18 + j * 4), rpm = u16(row + 20 + j * 4);
                const uint8_t duty = byte(row + 14 + j);
                require(temp <= 110 * 32 &&
                            (!j || (u16(row + 18 + (j - 1) * 4) < temp && u16(row + 20 + (j - 1) * 4) <= rpm &&
                                    byte(row + 14 + j - 1) <= duty)),
                        "Fan curve points violate their order or temperature bounds");
                for (auto index : active) {
                    if (i < c.count && i != index)
                        continue;
                    const auto cooler = c.row(index);
                    require(byte(cooler + 2) <= duty && duty <= byte(cooler + 3) && u16(cooler + 14) <= rpm &&
                                rpm <= u16(cooler + 16),
                            "Fan curve exceeds its linked channel limits");
                }
            }
            ++curves;
        }
        std::ostringstream out;
        out << active.size() << (active.size() == 1 ? " linked channel, " : " linked channels, ") << curves
            << (curves == 1 ? " fan curve" : " fan curves");
        for (const size_t i : active) {
            const size_t cooler = c.row(i);
            out << ". Fan " << i << " duty " << unsigned(byte(cooler + 2)) << ".." << unsigned(byte(cooler + 3))
                << "%, " << u16(cooler + 14) << ".." << u16(cooler + 16) << " RPM";
            const auto pwm = u16(cooler + 11);
            if (!byte(cooler + 13) && pwm >= 21000 && pwm <= 28000) out << ", PWM " << pwm << " Hz";
        }
        return out.str();
    }
    std::string perf() const {
        const auto t = archive() ? table(0xa8, 0x60, 0x10, {6, 14}, 57) : table(0xa8, 0x60, 0x10, {6}, 49);
        const auto policies = table(0xac, 0x61, 0x10, {5}, 10);
        const size_t p = policies.row(0);
        const uint32_t members = u32(p + 6);
        require(byte(policies.at + 4) == 0 && (byte(p) & 15) == 1 && (members & 7) == 7 &&
                    (t.count >= 32 || !(members >> t.count)),
                "Unrecognized policy flags or controller membership");
        unsigned qualified = 0;
        for (unsigned i = 0; i < 3; ++i) {
            const size_t row = t.row(i);
            const unsigned kind = byte(row) & 15, domain = i == 2 ? (archive() ? 4 : 5) : i * 2;
            const uint32_t target = u32(row + 3), jump = u32(row + 7);
            if ((kind == 1 || kind == 5) && byte(row + 19) == domain && target && target < jump && jump <= 4096 &&
                byte(row + 20) && byte(row + 21))
                ++qualified;
        }
        require(qualified, "No controller with matching domain, target, jump and hysteresis");
        return std::to_string(qualified) + " utilization controllers, policy 0 " +
               ((byte(p) & 128) ? "enabled" : "disabled");
    }
    std::string idle(const IdleTable& d) const {
        const Table t = table(d.pointer, d.owner, 0x10, {d.header}, d.stride);
        unsigned enabled = 0, dfpr = 0, sw_asr = 0, cg = 0;
        const bool difr = d.pointer == 0xe8 && archive();
        const unsigned mask = d.pointer == 0x128 ? 2 : 1;
        for (size_t i = 0; i < t.count; ++i) {
            const unsigned flags = byte(t.row(i));
            if (!(flags & mask))
                continue;
            ++enabled;
            if (difr) {
                dfpr += bool(flags & 2);
                sw_asr += bool(flags & 4);
                cg += bool(flags & 8);
            }
        }
        std::ostringstream out;
        out << enabled << " of " << t.count << " entries enabled";
        if (difr)
            out << " (DFPR " << dfpr << ", SW_ASR " << sw_asr << ", CG " << cg << ')';
        return out.str();
    }
    std::string voltage(uint32_t device) const {
        const auto old = inspect_vbios_rail_clamps(b, device);
        if (!old.empty()) {
            const auto count = std::count_if(old.begin(), old.end(), [](const auto& r) { return bool(r.rel_uv); }) +
                               std::count_if(old.begin(), old.end(), [](const auto& r) { return bool(r.alt_uv); });
            std::ostringstream out;
            out << count << " absolute REL/ALT bounds";
            for (const auto& r : old) {
                out << ". " << (r.sram ? "SRAM" : "Logic");
                if (r.rel_uv) out << " REL " << *r.rel_uv / 1000.0 << " mV";
                if (r.alt_uv) out << " ALT " << *r.alt_uv / 1000.0 << " mV";
            }
            return out.str();
        }
        const auto graph = parse_vbios_voltage_graph(b);
        if (!graph.ok) throw std::runtime_error(graph.message);
        require(!graph.roles.empty(), "The voltage graph contains no recognized rail roles");
        return std::to_string(graph.rail_roots.size()) + " rail selectors and " +
               std::to_string(graph.equations.size()) + " equations in the voltage graph" +
               (graph.graph_complete && graph.clock_roots_complete ? "" : " (incomplete dependency information)");
    }
};
} // namespace

VbiosOverrideInventory inspect_vbios_override_inventory(const std::vector<uint8_t>& image, uint32_t device, uint32_t architecture) {
    try {
        return InventoryReader(image).read(device, architecture);
    } catch (const std::runtime_error& error) {
        return {error.what(), {}};
    }
}
} // namespace druta::ui
