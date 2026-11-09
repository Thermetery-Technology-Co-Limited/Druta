# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native client-power tables and geometry helpers against client_power_runtime's.

The observations are the ones the client-power tests produce: each wire model of
test_client_power_runtime / _turing / _rm26 / _rm26_extended / _legacy / _driver_compat is read
and written through the Python ClientPowerController, and every (_project, _matches) call the
controller makes is recorded. Each recorded call, and each mutation of it that a peer check or a
key comparison must refuse, runs through the Python helper and the native binding, and the two
outcomes are compared exactly: the value and its type, or the exception type and message.
"""
import copy
import struct
import unittest

import pytest

from druta import client_power_runtime as cp
from druta import native
from druta import native_client_power as ncp

if not native.available() or not ncp.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from tests.test_client_power_driver_compat import ModernCoupledGpu, modern_v30_gpu  # noqa: E402
from tests.test_client_power_legacy import LegacyGpu  # noqa: E402
from tests.test_client_power_rm26 import Rm26Gpu  # noqa: E402
from tests.test_client_power_rm26_extended import ExtendedRm26Gpu  # noqa: E402
from tests.test_client_power_runtime import FakeGpu  # noqa: E402
from tests.test_client_power_turing import TuringGpu  # noqa: E402


def outcome(function, *args):
    try:
        value = function(*args)
    except Exception as exc:  # the comparison is the point
        return ("raise", type(exc), str(exc))
    return ("ok", value, _types(value))


def _types(value):
    if isinstance(value, tuple):
        return tuple(_types(v) for v in value)
    if isinstance(value, dict):
        return tuple((k, _types(v)) for k, v in value.items())
    return type(value)


def same(python, native_fn, *args):
    expected = outcome(python, *copy.deepcopy(args))
    got = outcome(native_fn, *copy.deepcopy(args))
    assert got == expected, args if len(repr(args)) < 400 else "(large arguments)"
    return expected


# ---- fixtures: the wire models of the client-power tests ------------------------------------

FIXTURES = {
    "modern_v40": lambda: FakeGpu(),
    "modern_v40_unrelated_off": lambda: FakeGpu(client_index=0, native_index=2, unrelated=False),
    "modern_v30": lambda: modern_v30_gpu(),
    "modern_v30_coupled": lambda: ModernCoupledGpu(),
    "modern_v30_coupled_pinned": lambda: ModernCoupledGpu(pinned=True),
    "turing_policy124": lambda: TuringGpu(5, 17),
    "turing_record216": lambda: TuringGpu(0, 2, record=216),
    "pascal_policy124": lambda: TuringGpu(0, 2, architecture=4),
    "legacy32": lambda: LegacyGpu(),
    "rm26": lambda: Rm26Gpu(),
    "rm26_pinned": lambda: Rm26Gpu(pinned=True),
    "rm26_extended": lambda: ExtendedRm26Gpu(),
    "rm26_extended_pinned": lambda: ExtendedRm26Gpu(pinned=True),
}
COUPLED = {"modern_v30", "modern_v30_coupled", "modern_v30_coupled_pinned", "turing_policy124",
           "turing_record216", "pascal_policy124", "legacy32", "rm26", "rm26_pinned", "rm26_extended",
           "rm26_extended_pinned"}

_RECORDED = {}


def recorded(name):
    """(befores, [(expected, observed)]) the controller produced for one wire model."""
    if name in _RECORDED:
        return _RECORDED[name]
    gpu = FIXTURES[name]()
    controller = cp.ClientPowerController(gpu)
    befores, pairs = [], []
    original_project, original_matches = cp._project, cp._matches

    def project(before, mw):
        befores.append(copy.deepcopy(before))
        return original_project(before, mw)

    def matches(expected, observed):
        pairs.append((copy.deepcopy(expected), copy.deepcopy(observed)))
        return original_matches(expected, observed)

    cp._project, cp._matches = project, matches
    try:
        before = controller.read()
        for mw in (551000, 1999000, before["requested_mw"]):
            controller.set_request(mw)
    finally:
        cp._project, cp._matches = original_project, original_matches
    assert befores and pairs and all(original_matches(e, o) for e, o in pairs)
    _RECORDED[name] = (befores, pairs)
    return _RECORDED[name]


def test_every_wire_model_is_recorded_with_its_coupling():
    for name in FIXTURES:
        befores, _ = recorded(name)
        assert cp._coupled_requests(befores[0]) == (name in COUPLED), name


# ---- constants ---------------------------------------------------------------------------------

def test_constants_row_by_row():
    got = ncp.constants()
    for name in ncp.COMMAND_NAMES:
        assert got[name] == getattr(cp, name), name
    for name in ncp.SIZE_GROUP_NAMES:
        python = getattr(cp, name)
        assert list(got[name].items()) == list(python.items()), name
    for name in ("SIZE_REFUSALS", "_RM26_COMMANDS", "INFO_BASE", "INFO_STRIDE", "CONTROL_BASE",
                 "CONTROL_STRIDE", "CAPACITY", "NATIVE_INFO_BASE", "NATIVE_INFO_STRIDE", "NATIVE_CONTROL_BASE",
                 "NATIVE_CONTROL_STRIDE", "NATIVE_CAPACITY", "ROLE", "CLIENT", "MISSING",
                 "LEGACY_GROUP_ARCHITECTURES", "RM26_ARCHITECTURES", "_STATE_KEYS", "_LAYOUT"):
        assert got[name] == getattr(cp, name), name
        assert _types(got[name]) == _types(getattr(cp, name)), name
    assert list(got["_RM26_COMMANDS"]) == list(cp._RM26_COMMANDS)
    assert ncp._STATE_KEYS == cp._STATE_KEYS


def test_the_two_generation_lists_stay_separate():
    got = ncp.constants()
    assert got["LEGACY_GROUP_ARCHITECTURES"] == (4, 6, 7)
    assert got["RM26_ARCHITECTURES"] == (4, 6)


# ---- _definition_stride --------------------------------------------------------------------------

def test_definition_stride_for_every_role_info_size():
    sizes = {getattr(cp, group)[cp.ROLE_INFO] for group in ncp.SIZE_GROUP_NAMES}
    assert sizes == {264, 232, 220, 120, 136, 132}
    for size in sorted(sizes) + [0, 3, 4, 24, -1, 2**63 - 1, -(2**63) + 4]:
        same(cp._definition_stride, ncp.definition_stride, size)


def test_definition_stride_outside_int64_is_refused_not_wrapped():
    with pytest.raises(OverflowError):
        ncp.definition_stride(-(2**63))
    with pytest.raises(OverflowError):
        ncp.definition_stride(2**64)


# ---- _small_native_geometry and _native_geometry -------------------------------------------------

LAYOUTS = (None, {"layout": None}, {"layout": "rm26"}, {"layout": "legacy32"}, {"layout": "modern?"})
NATIVE_LAYOUTS = (None, {"native_layout": "policy124"}, {"native_layout": "rm26_extended"},
                  {"native_layout": "other"}, {"native_layout": None})
STRIDES = (None, {"native_info_stride": None}, {"native_info_stride": 0}, {"native_info_stride": 216},
           {"native_info_stride": 132}, {"native_info_stride": 116}, {"native_info_stride": -8},
           {"native_info_stride": True}, {"native_info_stride": False}, {"native_info_stride": "x"},
           {"native_info_stride": 2**70}, {"native_info_stride": 2**63 - 1})


def bindings():
    for layout in LAYOUTS:
        for native_layout in NATIVE_LAYOUTS:
            for stride in STRIDES:
                binding = {"client_index": 3, "native_index": 19}
                for part in (layout, native_layout, stride):
                    binding.update(part or {})
                yield binding


def test_every_native_geometry_branch():
    seen = set()
    for binding in bindings():
        result = same(cp._native_geometry, ncp.native_geometry, binding)
        same(cp._small_native_geometry, ncp.small_native_geometry, binding)
        seen.add(result[1][4])
    # modern, legacy 184 and 124, rm26 short and extended
    assert seen == {13876, 6356, 4432, 2108, 2124}


def test_geometry_of_every_recorded_binding():
    for name in FIXTURES:
        befores, _ = recorded(name)
        same(cp._native_geometry, ncp.native_geometry, befores[0]["binding"])
        same(cp._small_native_geometry, ncp.small_native_geometry, befores[0]["binding"])


# ---- _coupled_requests -----------------------------------------------------------------------

def test_coupled_requests_on_every_observation_and_short_or_bad_role():
    for name in FIXTURES:
        befores, pairs = recorded(name)
        for observation in befores + [e for e, _ in pairs] + [o for _, o in pairs]:
            same(cp._coupled_requests, ncp.coupled_requests, observation)
    for role in ("", "30", "0000003", "00000000", "0000000030", "0000000040", "00000000301122", "zz", "ABCDEF0130"):
        same(cp._coupled_requests, ncp.coupled_requests, {"role_info_hex": role})
    same(cp._coupled_requests, ncp.coupled_requests, {})
    same(cp._coupled_requests, ncp.coupled_requests, {"role_info_hex": 5})


# ---- _project ----------------------------------------------------------------------------------

MWS = (1, 551000, 0, 2**32 - 1, 2**32, -1, 2**70, True, "551000", 1.5, None)


def test_project_on_every_layout():
    layouts = set()
    for name in FIXTURES:
        befores, _ = recorded(name)
        for before in befores:
            layouts.add((before["binding"].get("layout"), before["binding"].get("native_layout")))
            for mw in MWS:
                same(cp._project, ncp.project, before, mw)
    assert layouts == {(None, None), ("legacy32", "policy124"), ("legacy32", None), ("rm26", None),
                       ("rm26", "rm26_extended")}


def test_project_refusals_in_python_order():
    for name in ("modern_v40", "legacy32", "turing_record216", "rm26", "rm26_extended"):
        before = recorded(name)[0][0]
        cases = []
        for key in ("client_index", "native_index"):
            broken = copy.deepcopy(before)
            del broken["binding"][key]
            cases.append(broken)
        both = copy.deepcopy(before)
        del both["binding"]["client_index"], both["binding"]["native_index"]
        cases.append(both)
        for index in (-1, -2, -5, -10**6, 1000, 2**62, 2**70, -(2**70), True, 0):
            for key in ("client_index", "native_index"):
                moved = copy.deepcopy(before)
                moved["binding"][key] = index
                cases.append(moved)
        for key, value in (("control_hex", "zz"), ("control_hex", "ABCD"), ("native_control_hex", "0g"),
                           ("native_control_hex", ""), ("control_hex", ""), ("control_hex", 7)):
            changed = copy.deepcopy(before)
            changed[key] = value
            cases.append(changed)
        missing = copy.deepcopy(before)
        del missing["identity"]
        cases.append(missing)
        for case in cases:
            for mw in (551000, -1, "x"):
                same(cp._project, ncp.project, case, mw)


def test_project_bad_mw_wins_over_a_bad_native_payload():
    before = copy.deepcopy(recorded("rm26")[0][0])
    before["native_control_hex"] = "not hex"
    with pytest.raises(struct.error):
        ncp.project(before, -1)
    with pytest.raises(ValueError):
        ncp.project(before, 1)
    same(cp._project, ncp.project, before, -1)
    same(cp._project, ncp.project, before, 1)


# ---- _matches -----------------------------------------------------------------------------------

def test_matches_on_every_recorded_call():
    for name in FIXTURES:
        _, pairs = recorded(name)
        for expected, observed in pairs:
            assert same(cp._matches, ncp.matches, expected, observed) == ("ok", True, bool)


def flip(text, at):
    raw = bytearray.fromhex(text)
    raw[at] ^= 1
    return raw.hex()


def put(text, at, value):
    raw = bytearray.fromhex(text)
    struct.pack_into("<I", raw, at, value)
    return raw.hex()


def coupled_pair(name):
    """A recorded (expected, observed) where at least one occupied peer request moved."""
    _, pairs = recorded(name)
    for expected, observed in pairs:
        geometry = cp._native_geometry(expected["binding"])
        peers = _moved_peers(expected, observed, geometry)
        if peers:
            return expected, observed, geometry, peers
    raise AssertionError(f"{name}: no recorded write recalculated a peer")


def _moved_peers(expected, observed, geometry):
    info_base, info_stride, control_base, stride, _ = geometry
    old = bytes.fromhex(expected["native_control_hex"])
    new = bytes.fromhex(observed["native_control_hex"])
    mask = cp._u32(bytes.fromhex(expected["native_info_hex"]), 4)
    return [i for i in range(32) if mask >> i & 1 and i != expected["binding"]["native_index"]
            and old[control_base + i * stride + 4:control_base + i * stride + 8]
            != new[control_base + i * stride + 4:control_base + i * stride + 8]]


PEER_COUPLED = ("modern_v30_coupled", "modern_v30_coupled_pinned", "turing_policy124", "turing_record216",
                "legacy32", "rm26", "rm26_pinned", "rm26_extended", "rm26_extended_pinned")


def peer_variants(expected, observed, geometry, peer):
    """Each condition of the peer check broken once, plus the inventory and opaque cases."""
    info_base, info_stride, control_base, stride, size = geometry
    row, at = info_base + peer * info_stride, control_base + peer * stride + 4
    info, old, new = expected["native_info_hex"], expected["native_control_hex"], observed["native_control_hex"]
    raw_info = bytes.fromhex(info)

    def with_info(text):
        # Both sides read the same definitions, so only the peer check can refuse.
        return dict(expected, native_info_hex=text), dict(observed, native_info_hex=text)

    def with_old(text):
        e = copy.deepcopy(expected)
        e["native_control_hex"] = text
        return e, observed

    def with_new(text):
        o = copy.deepcopy(observed)
        o["native_control_hex"] = text
        return expected, o

    minimum, rated, maximum = struct.unpack_from("<III", raw_info, row + 8)
    variants = {
        "version": with_info(flip(info, row)),
        "units": with_info(bytes.fromhex(info)[:row + 6].hex() + "02" + info[2 * (row + 7):]),
        "units_one": with_info(bytes.fromhex(info)[:row + 6].hex() + "01" + info[2 * (row + 7):]),
        # The type word changes on both sides, so only the type check can refuse it.
        "type": (dict(expected, native_control_hex=put(old, at - 4, cp._u32(bytes.fromhex(old), at - 4) ^ 0x100)),
                 dict(observed, native_control_hex=put(new, at - 4, cp._u32(bytes.fromhex(new), at - 4) ^ 0x100))),
        "minimum_above_rated": with_info(put(info, row + 8, rated + 1)),
        "rated_above_maximum": with_info(put(info, row + 12, maximum + 1)),
        "maximum_zero": with_info(put(put(put(info, row + 8, 0), row + 12, 0), row + 16, 0)),
        "old_missing": with_old(put(old, at, cp.MISSING)),
        "new_missing": with_new(put(new, at, cp.MISSING)),
        "peer_not_occupied": with_info(put(info, 4, cp._u32(raw_info, 4) & ~(1 << peer))),
        "opaque_peer_byte": with_new(flip(new, at - 4 + 9 if stride > 12 else at - 4)),
        "selected_request": with_new(flip(new, control_base + expected["binding"]["native_index"] * stride + 4)),
        "tail": with_new(flip(new, size - 1)),
        "longer_observed": with_new(new + "00"),
        "both_longer": (dict(expected, native_control_hex=old + "00"), dict(observed, native_control_hex=new + "00")),
        "short_info": with_info(info[:14]),
        "info_cut_at_peer": with_info(info[:2 * (row + 12)]),
        "info_cut_before_peer": with_info(info[:2 * row]),
    }
    return variants


def test_matches_each_peer_check_condition():
    refused, accepted = set(), set()
    for name in PEER_COUPLED:
        expected, observed, geometry, peers = coupled_pair(name)
        for peer in peers:
            for label, (e, o) in peer_variants(expected, observed, geometry, peer).items():
                result = same(cp._matches, ncp.matches, e, o)
                if result[0] == "ok":
                    (accepted if result[1] else refused).add(label)
    # a v30 peer in watts (units 1) is an understood recalculation
    assert "units_one" in accepted
    # every failure condition of the peer check refuses on some wire model
    assert {"version", "units", "type", "minimum_above_rated", "rated_above_maximum", "maximum_zero",
            "old_missing", "new_missing", "peer_not_occupied", "opaque_peer_byte", "selected_request", "tail",
            "longer_observed", "both_longer"} <= refused


def key_variants(expected, observed):
    out = {}
    for key in cp._STATE_KEYS:
        e = copy.deepcopy(expected)
        del e[key]
        out[f"missing_{key}"] = (e, observed)
    changes = {"schema": 2, "stable_reads": 3, "identity": {"slot": "elsewhere"}, "requested_mw": 1,
               "binding": dict(expected["binding"], units="W")}
    for key, value in changes.items():
        out[f"changed_{key}"] = (dict(expected, **{key: value}), observed)
    for key in cp._STATE_KEYS[5:]:
        raw = bytes.fromhex(expected[key])
        at = 0 if key != "role_info_hex" else len(raw) - 1  # byte 4 decides the coupling
        out[f"flipped_{key}"] = (dict(expected, **{key: flip(expected[key], at)}), observed)
        out[f"upper_{key}"] = (dict(expected, **{key: expected[key].upper()}), observed)
        out[f"upper_both_{key}"] = (dict(expected, **{key: expected[key].upper()}),
                                    dict(observed, **{key: observed[key].upper()}))
        out[f"spaced_{key}"] = (dict(expected, **{key: expected[key][:2] + " " + expected[key][2:]}), observed)
        out[f"bad_{key}"] = (dict(expected, **{key: "x" + expected[key][1:]}), observed)
        out[f"bad_observed_{key}"] = (expected, dict(observed, **{key: "x" + observed[key][1:]}))
        out[f"not_text_{key}"] = (dict(expected, **{key: raw}), observed)
    out["extra_key"] = (dict(expected, arbitration=None), observed)
    out["observed_extra_key"] = (expected, dict(observed, arbitration=None))
    o = copy.deepcopy(observed)
    del o["identity"]
    out["observed_missing_identity"] = (expected, o)
    binding = copy.deepcopy(expected["binding"])
    del binding["native_index"]
    out["binding_without_native_index"] = (dict(expected, binding=binding), dict(observed, binding=binding))
    out["binding_not_a_dict"] = (dict(expected, binding="rm26"), dict(observed, binding="rm26"))
    out["short_role"] = (dict(expected, role_info_hex="00000000"), dict(observed, role_info_hex="00000000"))
    return out


def test_matches_key_comparisons_coupled_and_uncoupled():
    for name in ("modern_v40", "modern_v30_coupled", "legacy32", "turing_record216", "rm26", "rm26_extended"):
        _, pairs = recorded(name)
        expected, observed = pairs[0]
        for _label, (e, o) in key_variants(expected, observed).items():
            same(cp._matches, ncp.matches, e, o)


def test_matches_with_unusual_binding_geometry():
    for name in ("modern_v30_coupled", "rm26", "legacy32"):
        expected, observed, _, peers = coupled_pair(name)
        for stride in (None, 0, -4, -11, -107, -260, 1, 4, 2**40, -(2**40), 2**62, 2**63 - 1, 2**70, False):
            for selected in (peers[0], 40, -1, 2**70, 1.0 * peers[0], "x"):
                binding = dict(expected["binding"], native_info_stride=stride, native_index=selected)
                same(cp._matches, ncp.matches, dict(expected, binding=binding), dict(observed, binding=binding))


def test_uncoupled_matches_never_accepts_a_peer_change():
    expected, observed = recorded("modern_v40")[1][0]
    geometry = cp._native_geometry(expected["binding"])
    peer = next(i for i in range(32) if cp._u32(bytes.fromhex(expected["native_info_hex"]), 4) >> i & 1
                and i != expected["binding"]["native_index"])
    o = dict(observed, native_control_hex=put(observed["native_control_hex"],
                                              geometry[2] + peer * geometry[3] + 4, 12345))
    assert same(cp._matches, ncp.matches, expected, o) == ("ok", False, bool)
