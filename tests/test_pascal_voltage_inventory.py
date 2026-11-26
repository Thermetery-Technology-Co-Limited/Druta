# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only inventory of the GP10x 1.x rail / 3.x VFE table pair."""

import json
import os
import struct
import subprocess

import pytest

from druta import vbios_fields
from tests.test_vbios_fields import extend, fixture, voltage_fixture

RAIL = 0x800
VFE = 0x4000
EQUATIONS = VFE + 7 + 25 * 2


def pascal_image():
    # Same selector roles and REL/ALT bounds as the TITAN Xp capture. The
    # surrounding image, counts and coefficients are synthetic.
    image = bytearray(fixture(device=0xABCD))
    image[0x2000:0x2002] = bytes(2)  # legacy and EFI images followed by NPDS
    struct.pack_into("<I", image, 0x268, RAIL)
    struct.pack_into("<I", image, 0x220, VFE - 0x1000)
    image[RAIL:RAIL + 16] = bytes.fromhex("10 05 0b 01 00 00 35 0c 00 00 01 03 00 02 04 ff")
    image[VFE:VFE + 7] = bytes((0x30, 7, 25, 2, 24, 5, 200))
    image[VFE + 7] = 4
    image[VFE + 7 + 25] = 3
    for index, (low, high, c0, c1) in enumerate((
        (1025000, 1068750, 1100000, -781.25),
        (1075000, 1093750, 1182000, -781.25),
        (950000, 950000, 950000, 0),
        (1200000, 1200000, 1200000, 0),
        (650000, 650000, 650000, 0),
    )):
        struct.pack_into("<BBB5fB", image, EQUATIONS + index * 24, 1, 1, 255, low, high, c0, c1, 0, 0xA2)
    return image


def tables(report):
    return {t["name"]: t for t in report["tables"]}


def test_gp10x_selectors_and_stored_equations_are_read_only():
    image = pascal_image()
    before = bytes(image)
    report = vbios_fields.inspect(image)
    rail, vfe = (tables(report)[name] for name in ("voltage_rail", "vfe"))
    assert rail["capability"] == vfe["capability"] == "read-only"
    row = rail["rows"][0]
    assert row["domain"] == "logic"
    assert row["boot_voltage_uv"] == 800000
    assert {k: v["index"] for k, v in row["selectors"].items()} == {
        "REL": 0, "ALT": 1, "OV": 3, "power": 0, "boot": 2, "VMIN": 4, "voltage_margin": None,
    }
    assert row["selectors"]["ALT"]["offset"] == RAIL + 5 + 5
    assert "voltage_scale_exp_power" not in row["selectors"]  # entry ends at +10
    assert [eq["index"] for eq in vfe["equations"]] == [0, 1, 2, 3, 4]
    rel, alt = vfe["equations"][:2]
    assert (rel["variable_index"], rel["next_equation"], rel["output_units"]) == (1, None, "uV")
    assert (rel["output_min"], rel["output_max"], rel["coefficients"]) == (1025000, 1068750, [1100000, -781.25, 0])
    assert (alt["output_min"], alt["output_max"]) == (1075000, 1093750)
    assert not any(f["key"].startswith("voltage.") for f in report["fields"])
    with pytest.raises(ValueError, match="unknown field"):
        vbios_fields.plan(image, {"voltage.0.REL.value": 1100000})
    assert bytes(image) == before


def test_power_equation_indices_never_resolve_in_vfe_namespace():
    image = pascal_image()
    image[RAIL + 2] = 12
    image[RAIL + 5 + 7] = 2  # valid VFE number, but this byte indexes the power equation table
    image[RAIL + 5 + 8] = 255  # remove the actual VFE boot root that used equation 2
    image[RAIL + 5 + 11] = 240  # power namespace may exceed VFE count
    report = vbios_fields.inspect(image)
    selectors = tables(report)["voltage_rail"]["rows"][0]["selectors"]
    for name, index in (("power", 2), ("voltage_scale_exp_power", 240)):
        assert (selectors[name]["namespace"], selectors[name]["index"]) == ("power_equation", index)
    assert selectors["REL"]["namespace"] == "vfe"
    assert [eq["index"] for eq in tables(report)["vfe"]["equations"]] == [0, 1, 3, 4]
    assert all(d["group"] != "voltage" for d in report["diagnostics"])


def test_only_domains_accepted_by_rail_hal_contribute_selectors():
    image = pascal_image()
    image[RAIL + 3] = 2
    image[RAIL + 16:RAIL + 27] = image[RAIL + 5:RAIL + 16]
    image[RAIL + 16 + 4] = 250
    single = vbios_fields.inspect(image)
    assert [r["domain"] for r in tables(single)["voltage_rail"]["rows"]] == ["logic"]
    assert not any("selector 250" in d["message"] for d in single["diagnostics"])
    image[RAIL + 4] = 1
    split = vbios_fields.inspect(image)
    assert [r["domain"] for r in tables(split)["voltage_rail"]["rows"]] == ["logic", "sram"]
    assert any("selector 250" in d["message"] for d in split["diagnostics"])
    image[RAIL + 4] = 2
    unknown = vbios_fields.inspect(image)
    assert tables(unknown)["voltage_rail"]["rows"] == []
    assert "vfe" not in tables(unknown)


@pytest.mark.parametrize("byte,value", [(RAIL + 2, 6), (VFE + 1, 9), (VFE + 2, 29), (VFE + 4, 25), (VFE + 5, 0)])
def test_unknown_geometry_never_decodes_equations(byte, value):
    image = pascal_image()
    image[byte] = value
    report = vbios_fields.inspect(image)
    assert "vfe" not in tables(report)
    assert any(d["group"] == "voltage" and "geometry" in d["message"] for d in report["diagnostics"])


def test_truncated_equation_table_retains_only_bounded_rail_selectors():
    image = pascal_image()
    report = vbios_fields.inspect(image[:EQUATIONS + 5 * 24 - 1])
    assert tables(report)["voltage_rail"]["rows"][0]["selectors"]["ALT"]["index"] == 1
    assert "vfe" not in tables(report)
    assert any(d["group"] == "voltage" and "capture" in d["message"] for d in report["diagnostics"])


def test_invalid_and_opaque_roots_leave_independent_alt_visible():
    image = pascal_image()
    image[RAIL + 5 + 4] = 5  # outside the declared equation count
    image[RAIL + 5 + 7] = 255  # absent
    image[EQUATIONS + 2 * 24] = 4  # fixed-point equation, distinct numeric encoding
    image[EQUATIONS + 3 * 24] = 0  # disabled OV root
    image[EQUATIONS + 4 * 24 + 23] = 1  # MHz output cannot supply voltage bounds
    report = vbios_fields.inspect(image)
    eqs = {eq["index"]: eq for eq in tables(report)["vfe"]["equations"]}
    assert eqs.keys() == {1, 2, 4}
    assert eqs[1]["output_max"] == 1093750
    assert "output_min" not in eqs[2] and "output_min" not in eqs[4]
    assert sum(d["group"] == "voltage" for d in report["diagnostics"]) == 2


@pytest.mark.parametrize("field,bad", [(1, b"\x02"), (1, b"\xff"), (2, b"\x05"), (3, struct.pack("<f", float("nan")))])
def test_invalid_polynomial_does_not_emit_invalid_numbers_or_hide_other_roots(field, bad):
    image = pascal_image()
    image[EQUATIONS + field:EQUATIONS + field + len(bad)] = bad
    report = vbios_fields.inspect(image)
    eqs = tables(report)["vfe"]["equations"]
    assert "output_min" not in eqs[0] and eqs[1]["output_max"] == 1093750
    json.dumps(report, allow_nan=False)


def test_modern_layout_keeps_its_existing_planner():
    image = voltage_fixture()
    report = vbios_fields.inspect(image)
    assert "voltage_rail" not in tables(report)
    assert vbios_fields.plan(image, {"voltage.0.VMIN": 760000})["patches"]


@pytest.mark.parametrize(("case", "expected"), [
    ("baseline", [("logic", 1068750, 1093750)]),
    ("two-rails", [("logic", 1068750, 1093750), ("sram", 1093750, 1068750)]),
    ("relocated", [("logic", 1056250, 1118750)]),
    ("voltage-delta", [("logic", None, 1093750)]),
    ("chained", [("logic", 1068750, None)]),
    ("unknown-input", [("logic", None, 1093750)]),
    ("bad-coefficient", [("logic", 1068750, None)]),
    ("absent", []),
    ("owned-archive", [("logic", 1068750, 1093750)]),
    ("wrong-owner", []),
    ("truncated", []),
    ("modern", []),
])
def test_stored_bound_replay(case, expected, tmp_path):
    """CTest runs both shipping decoders on these same bytes and independent expectations."""
    image = pascal_image()
    device = 0xABCD
    rel_selector = RAIL + 5 + 4
    if case == "two-rails":
        image[RAIL + 3:RAIL + 5] = bytes((2, 1))
        image[RAIL + 16:RAIL + 27] = image[RAIL + 5:RAIL + 16]
        image[rel_selector + 11:rel_selector + 13] = bytes((1, 0))
    elif case == "relocated":
        image[rel_selector:rel_selector + 2] = bytes((4, 3))
        for index, upper in ((4, 1056250), (3, 1118750)):
            struct.pack_into("<2f", image, EQUATIONS + 24 * index + 3, 950000, upper)
    elif case == "voltage-delta":
        image[EQUATIONS + 23] = 4
    elif case == "chained":
        image[EQUATIONS + 24 + 2] = 0
    elif case == "unknown-input":
        image[EQUATIONS + 1] = 250
    elif case == "bad-coefficient":
        struct.pack_into("<f", image, EQUATIONS + 24 + 15, float("inf"))
    elif case == "absent":
        image[rel_selector:rel_selector + 2] = bytes((255, 255))
    elif case in ("owned-archive", "wrong-owner"):
        owner = 0x14 if case == "owned-archive" else 0x15
        image = extend(fixture(device=device), [
            (0x68, None, RAIL, image[RAIL:RAIL + 16]),
            (0x20, owner, 0x4600, image[VFE:EQUATIONS + 24 * 5]),
        ])
    elif case == "truncated":
        image = image[:EQUATIONS + 24 * 5 - 1]
    elif case == "modern":
        image = voltage_fixture()
        device = struct.unpack_from("<H", image, 0x86)[0]

    expected_rows = [{"domain": domain, "REL": rel, "ALT": alt} for domain, rel, alt in expected]
    report = vbios_fields.inspect(image)
    rows = tables(report).get("voltage_rail", {}).get("rows", [])
    qualified = []
    for row in rows:
        bounds = row.get("stored_upper_bounds_uv", {})
        if any(value is not None for value in bounds.values()):
            qualified.append({"domain": row["domain"], **bounds})
    assert qualified == expected_rows
    if case != "modern":
        assert not any(field["key"].startswith("voltage.") for field in report["fields"])

    reader = os.environ.get("DRUTA_TEST_CLAMP_READER")
    if reader:
        rom = tmp_path / "fixture.rom"
        rom.write_bytes(image)
        result = subprocess.run([reader, "--inspect", str(rom), str(device)],
                                check=True, capture_output=True, text=True, timeout=10)
        assert json.loads(result.stdout) == expected_rows
