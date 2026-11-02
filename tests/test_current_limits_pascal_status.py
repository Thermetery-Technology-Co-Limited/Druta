# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The 11740/11672 transport shares two blocks with 11748/11680, not three.

Its status block is 1032 bytes shorter with its own header and record stride.
These tests pin that the entries stay distinct, so a future edit cannot quietly
carry one geometry's status offsets onto the other and still pass.
"""
import pytest

from druta import nvbackend as n
from tests.test_current_limits import fixture

A618, A619, A61A, E61B = 0x2080A618, 0x2080A619, 0x2080A61A, 0x2080E61B

PASCAL = (11740, 11672)
TURING = (11748, 11680)


def test_the_two_short_transports_are_separate_entries():
    layouts = n.GPU._CURRENT_LIMIT_LAYOUTS
    pascal, turing = layouts[PASCAL], layouts[TURING]
    assert pascal["sizes"][A618] == turing["sizes"][A618] == 8620
    assert pascal["sizes"][A61A] == turing["sizes"][A61A] == 4432
    assert pascal["sizes"][E61B] == turing["sizes"][E61B] == 4432
    assert pascal["info"] == turing["info"] == (0x58, 0xE4)
    assert pascal["control"] == turing["control"] == (0x14, 0x7C)
    # ...and the status block is where they differ.
    assert turing["sizes"][A619] == 172080 and turing["status"] == (0x70, 0x1454)
    assert pascal["sizes"][A619] == 171048 and pascal["status"] == (0x68, 0x1434)


def test_the_short_status_geometry_reads_core_current():
    gpu, _ = fixture(n.GPU.ARCH_PASCAL, short_status=True)
    rows = gpu.get_current_limits()
    assert [r["policy"] for r in rows] == [13]
    assert rows[0]["label"] == "Core current"
    assert rows[0]["channel"] == 11
    assert (rows[0]["default_ma"], rows[0]["maximum_ma"]) == (204700, 218000)
    assert rows[0]["limit_ma"] == rows[0]["requested_ma"] == 204700
    assert (gpu._current_limit_error, gpu._current_limit_read_error) == ("", "")
    assert gpu._current_limit_layout_choice["sizes"][A619] == 171048
    assert gpu._current_limit_layout_choice["status"] == (0x68, 0x1434)


def test_a_write_round_trips_through_the_short_status_geometry():
    gpu, state = fixture(n.GPU.ARCH_PASCAL, short_status=True)
    original = list(state.control)
    assert gpu.set_current_limit_ma(13, 203900)[0]
    assert gpu.get_current_limits()[0]["limit_ma"] == 203900
    assert gpu.set_current_limit_ma(13, 204700)[0]
    assert state.control == original


@pytest.mark.parametrize("field,value", [
    ("size", 172080),                      # the Turing status SIZE
    ("base", 0x70),                        # the Turing status HEADER
    ("stride", 0x1454),                    # the Turing status RECORD stride
])
def test_carrying_any_turing_status_field_over_breaks_the_read(field, value, monkeypatch):
    """Each of the three status fields is independently load-bearing."""
    layout = dict(n.GPU._CURRENT_LIMIT_LAYOUTS[PASCAL])
    base, stride = layout["status"]
    if field == "size":
        layout["sizes"] = {**layout["sizes"], A619: value}
    elif field == "base":
        layout["status"] = (value, stride)
    else:
        layout["status"] = (base, value)
    monkeypatch.setitem(n.GPU._CURRENT_LIMIT_LAYOUTS, PASCAL, layout)
    gpu, _ = fixture(n.GPU.ARCH_PASCAL, short_status=True)
    assert gpu.get_current_limits() == []
    assert gpu._current_limit_error
    assert not gpu.set_current_limit_ma(13, 203900)[0]


def test_the_turing_transport_still_uses_its_own_status_block():
    gpu, _ = fixture(n.GPU.ARCH_PASCAL)                    # 11748/11680
    assert [r["policy"] for r in gpu.get_current_limits()] == [13]
    assert gpu._current_limit_layout_choice["sizes"][A619] == 172080
    assert gpu._current_limit_layout_choice["status"] == (0x70, 0x1454)
    assert (gpu._current_limit_error, gpu._current_limit_read_error) == ("", "")
