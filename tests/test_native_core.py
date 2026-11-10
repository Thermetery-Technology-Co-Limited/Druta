# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native core must behave exactly like the Python modules it replaces.

The rail_transport tests are re-run here against the native implementation
unchanged: the functions below are the Python test module's own, with its
module globals pointed at druta.native for the duration of each test.
"""
import pytest

from druta import native, nvbackend

if not native.available():
    pytest.skip("native core not built (native/build.sh linux)", allow_module_level=True)

import tests.test_rail_transport as rail_tests  # noqa: E402
from tests.test_rail_transport import (  # noqa: E402,F401  (collected here, against the native core)
    test_566_full_record_tail_is_preserved,
    test_known_layouts_include_statically_recovered_566_schema,
    test_malformed_packets_are_not_recognized_or_mutated,
    test_masks_records_values_and_boost_are_strictly_validated,
    test_packet_summary_reports_only_understood_layouts,
    test_prepare_write_preserves_every_unowned_byte,
    test_recognizes_and_prepares_each_known_layout,
)


@pytest.fixture(autouse=True)
def _native_rail_transport(monkeypatch):
    monkeypatch.setattr(rail_tests, "KNOWN_LAYOUTS", native.rail_known_layouts())
    monkeypatch.setattr(rail_tests, "recognize_packet", native.rail_recognize_packet)
    monkeypatch.setattr(rail_tests, "prepare_write", native.rail_prepare_write)
    monkeypatch.setattr(rail_tests, "packet_summary", native.rail_packet_summary)


def test_abi_version_and_status_text():
    library = native.load()
    assert library.dc_abi_version() == native.ABI_VERSION
    assert library.dc_status_text(native.DC_E_MASK) == b"requested_mask must select rail 0, rail 1, or both"
    assert library.dc_status_text(native.DC_E_CALLER) == (
        b"a caller-supplied callback reported a failure; nothing was written")
    assert library.dc_status_text(12345) == b"unknown status"


SLOTS = ["0000:01:00.0", "00000000:01:00.0", "0010:ab:1f.7", "0000:01:00.f", "  0000:01:00.0\n", "", None,
         "000:01:00.0", "000000000:01:00.0", "0000:1:00.0", "0000:01:00.00", "0000:01:00", "0000-01-00.0",
         "0000:01:00.0 x", "0000:01:00.0 ", "zz", 12]


@pytest.mark.parametrize("text", SLOTS)
def test_parse_slot_matches_python(text):
    assert native.parse_slot(text) == nvbackend.parse_slot(text)


@pytest.mark.parametrize("a", SLOTS[:6])
@pytest.mark.parametrize("b", SLOTS[:6])
def test_same_slot_matches_python(a, b):
    assert native.same_slot(a, b) == nvbackend.same_slot(a, b)


@pytest.mark.parametrize("values", [(0, 1, 0, 0), (0x10, 0xab, 0x1f, 7), (0xFFFF, 0xFF, 0x1F, 7), (0, 0, 0, 15)])
def test_format_slot_matches_python(values):
    assert native.format_slot(*values) == nvbackend.format_slot(*values)
    assert native.parse_slot(native.format_slot(*values)) == nvbackend.parse_slot(nvbackend.format_slot(*values))


def test_rail_prepare_write_rejects_non_bytes_like_python():
    layout = native.rail_known_layouts()[0]
    source = rail_tests.packet(layout, 1)
    with pytest.raises(ValueError):
        native.rail_prepare_write(bytearray(source), 1, {0: (1, 2, 3, 4)}, 20)
    with pytest.raises(ValueError):
        native.rail_prepare_write(source, 1, [(0, (1, 2, 3, 4))], 20)


def test_rail_prepare_write_output_is_bytes_of_the_same_size():
    layout = native.rail_known_layouts()[0]
    source = rail_tests.packet(layout, 3)
    result = native.rail_prepare_write(source, 3, {0: (1, 2, 3, 4), 1: (5, 6, 7, 8)}, 50)
    assert type(result) is bytes and len(result) == len(source)
    assert result == rail_tests.prepare_write(source, 3, {0: (1, 2, 3, 4), 1: (5, 6, 7, 8)}, 50) or True  # same packet
    from druta import rail_transport
    assert result == rail_transport.prepare_write(source, 3, {0: (1, 2, 3, 4), 1: (5, 6, 7, 8)}, 50)


def test_candidate_paths_are_absolute_and_honor_override(tmp_path):
    paths = native.candidate_paths({"DRUTA_NATIVE_LIB": str(tmp_path / "x.so")})
    assert paths[0] == tmp_path / "x.so"
    assert all(path.is_absolute() for path in paths)


def test_load_reports_an_unusable_candidate_as_native_unavailable(monkeypatch):
    """A candidate whose is_file() raises (a name too long, a directory the account cannot search;
    Python 3.13 re-raises those) is one path's failure, not an escaping OSError."""
    import errno
    from pathlib import Path

    class Unusable(type(Path())):
        def is_file(self, *args, **kwargs):
            raise OSError(errno.ENAMETOOLONG, "File name too long")

    path = Unusable("/unusable/libdrutacore.so")
    saved = native._library, native._error
    try:
        native._library, native._error = None, None
        monkeypatch.setattr(native, "candidate_paths", lambda environ=None: [path])
        with pytest.raises(native.NativeUnavailable) as raised:
            native.load()
        assert f"{path}: [Errno {errno.ENAMETOOLONG}] File name too long" in str(raised.value)
        with pytest.raises(native.NativeUnavailable):  # the failure is kept, as for a missing file
            native.load()
    finally:
        native._library, native._error = saved
