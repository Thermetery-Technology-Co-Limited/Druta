// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// GP10x rail/VFE layout follows the read-only inventory in src/druta/vbios_fields.py.
#include "vbios_rail_clamps.h"
#include "vbios_rom_reader.h"
#include <cmath>
#include <cstring>

namespace druta::ui {
namespace {
class RailClampReader : public VbiosRomReader {
public:
    explicit RailClampReader(const std::vector<uint8_t>& image) : VbiosRomReader(image) {}

    std::vector<VbiosRailClamps> read(uint32_t expected_device_id) {
        read_directory();
        if (image_regions.front().device != expected_device_id) return {};
        const size_t rail = pointer('P', 0x68);
        own(rail, 5);
        const auto stride = byte(rail + 2), rails = byte(rail + 3), hal = byte(rail + 4);
        if (byte(rail) != 0x10 || byte(rail + 1) != 5 || stride < 7 || stride > 12 || hal > 1)
            return {};
        const size_t rail_end = rail + 5 + size_t(stride) * rails;
        own(rail, rail_end - rail);

        const size_t vfe = pointer('P', 0x20);
        own(vfe, 7);
        const auto variables = byte(vfe + 3), equations = byte(vfe + 5);
        if (byte(vfe) != 0x30 || byte(vfe + 1) != 7 || byte(vfe + 2) != 25 ||
            byte(vfe + 4) != 24 || !equations)
            return {};
        const size_t variable_base = vfe + 7, equation_base = variable_base + 25 * size_t(variables);
        const size_t vfe_end = equation_base + 24 * size_t(equations);
        own(vfe, vfe_end - vfe, 0x14); // VFE object when the table lives in an object archive
        if (vfe < rail_end && rail < vfe_end) return {};

        const auto upper = [&](uint8_t index) -> std::optional<float> {
            if (index == 0xff || index >= equations) return {};
            const size_t eq = equation_base + 24 * size_t(index);
            const uint8_t variable = byte(eq + 1);
            // Terminal quadratic equation, absolute voltage output. A voltage delta
            // uses the same units but cannot supply an absolute bound.
            if (byte(eq) != 1 || byte(eq + 2) != 0xff || (byte(eq + 23) & 0xf) != 2 ||
                variable >= variables || !byte(variable_base + 25 * size_t(variable)))
                return {};
            float values[5]; // lower, upper, three polynomial coefficients
            for (size_t i = 0; i < 5; ++i) {
                const auto bits = u32(eq + 3 + 4 * i);
                std::memcpy(&values[i], &bits, sizeof(bits));
                if (!std::isfinite(values[i])) return {};
            }
            if (values[0] < 0 || values[0] > values[1]) return {};
            return values[1];
        };
        std::vector<VbiosRailClamps> result;
        for (unsigned i = 0; i < rails && i <= hal; ++i) {
            const size_t row = rail + 5 + size_t(stride) * i;
            VbiosRailClamps clamps{i == 1, upper(byte(row + 4)), upper(byte(row + 5))};
            if (clamps.rel_uv || clamps.alt_uv) result.push_back(clamps);
        }
        return result;
    }
};
}

std::vector<VbiosRailClamps> inspect_vbios_rail_clamps(const std::vector<uint8_t>& image,
                                                      uint32_t expected_device_id) {
    try {
        return RailClampReader(image).read(expected_device_id);
    } catch (const std::runtime_error&) {
        return {};
    }
}
}
