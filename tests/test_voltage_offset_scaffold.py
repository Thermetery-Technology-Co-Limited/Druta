"""Offline scaffold planning and loaded-definition verification."""
import copy
import struct

import pytest

from druta import vfe_equation_runtime as eq
from druta import voltage_offset_scaffold as scaffold
from tests.test_vbios_fields import apply, extend, fixture, voltage_fixture
from tests.test_vfe_equation_runtime import observation


def case(variable=7, free=2):
    src = voltage_fixture()
    start = 0x5000
    base = 14 + 29 * 255
    table = bytearray(src[start:start + base + 6 * 25 + 16 * 5])
    terms = table[base + 6 * 25:]
    table = table[:base + 6 * 25] + bytearray(scaffold._EMPTY * free) + terms
    table[5] = 6 + free
    for i in range(1, 6):
        table[base + 25*i + 1] = variable
    rail = src[0x800:0x800 + 70]
    image = extend(fixture(device=0x4567), [(0x68, None, 0x800, rail), (0x20, 0x14, 0x5000, table)])
    before = observation(index=1, before=20000, next_index=0, variable=variable, peers=(0, 2, 3, 4, 5),
                         slot='0000:04:00.0', device_id=0x4567)
    info = bytearray.fromhex(before['info_hex'])
    for i in range(6):
        at = eq.HEADER+i*eq.INFO_STRIDE
        row = table[base+25*i:base+25*(i+1)]
        info[at] = info[at+0x6c] = 6 if i == 0 else 3
        info[at+0x6e] = 2
        struct.pack_into('<H', info, at+2, row[2] | row[24] << 8)
    before['info_hex'] = info.hex()
    variables = bytearray(0xC364)
    variables[4+variable//8] |= 1 << (variable%8)
    return image, before, bytes(variables)


def loaded_from(before, heads):
    loaded = copy.deepcopy(before)
    for name, stride in (('info_hex', eq.INFO_STRIDE), ('control_hex', eq.CONTROL_STRIDE)):
        data = bytearray.fromhex(before[name])
        source = eq.HEADER + stride
        for h in heads:
            index = h['index']
            at = eq.HEADER + index*stride
            data[4 + index//8] |= 1 << (index%8)
            data[at:at+stride] = data[source:source+stride]
            if name == 'info_hex':
                struct.pack_into('<H', data, at+2, h['next_index'])
            else:
                for off in (4, 8, 16):
                    struct.pack_into('<f', data, at+off, 0)
        loaded[name] = data.hex()
    return loaded


def test_independent_heads_preserve_original_graph_and_project_repeated_values():
    image, before, variables = case()
    requests = [(0, 'REL'), (0, 'ALT')]
    plan, heads = scaffold.plan(image, before, variables, requests)
    after = apply(image, plan)
    assert [h['index'] for h in heads] == [6, 7]
    assert [h['next_index'] for h in heads] == [0, 1]
    assert all(h['variable'] == 7 for h in heads)
    # Existing ALT still points to the original REL; only selectors are changed.
    assert after[0x5000+14+29*255+25+2] == image[0x5000+14+29*255+25+2] == 0
    loaded = loaded_from(before, heads)
    _, verified = scaffold.verify_loaded(image, before, variables, requests, after, loaded)
    assert [h['value_uv'] for h in verified] == [0, 0]
    contract = scaffold.live_contract(image, before, variables, requests, after, loaded, heads[0]['key'], 5000)
    eq.verify(contract['after'], contract)
    next_contract = scaffold.live_contract(image, before, variables, requests, after, contract['after'], heads[0]['key'], 10000)
    assert next_contract['selected'][heads[0]['key']]['before_uv'] == 5000


def test_partial_selection_other_variable_and_refusals():
    image, before, variables = case(variable=11, free=1)
    plan, heads = scaffold.plan(image, before, variables, [(0, 'OV')])
    assert heads[0]['variable'] == 11 and heads[0]['next_index'] == 2
    after, loaded = apply(image, plan), loaded_from(before, heads)
    with pytest.raises(scaffold.VoltageOffsetScaffoldError):
        scaffold.plan(image, before, variables, [(0, 'OV'), (0, 'VMIN')])
    with pytest.raises(scaffold.VoltageOffsetScaffoldError):
        scaffold.plan(image, before, bytes(len(variables)), [(0, 'OV')])
    signed = scaffold.live_contract(image, before, variables, [(0, 'OV')], after, loaded, heads[0]['key'], -5000)
    assert eq.constant_row(signed['after'], heads[0]['index'])['value_uv'] == -5000
    again = scaffold.live_contract(image, before, variables, [(0, 'OV')], after, signed['after'], heads[0]['key'], 0)
    assert again['selected'][heads[0]['key']]['before_uv'] == -5000
    with pytest.raises(scaffold.VoltageOffsetScaffoldError):
        scaffold.live_contract(image, before, variables, [(0, 'OV')], after, loaded, heads[0]['key'], 16777217)
    bad = copy.deepcopy(loaded)
    raw = bytearray.fromhex(bad['info_hex'])
    raw[eq.HEADER+2*eq.INFO_STRIDE+30] ^= 1
    bad['info_hex'] = raw.hex()
    with pytest.raises(scaffold.VoltageOffsetScaffoldError):
        scaffold.verify_loaded(image, before, variables, [(0, 'OV')], after, bad)
    with pytest.raises(scaffold.VoltageOffsetScaffoldError):
        scaffold.verify_loaded(image, before, variables, [(0, 'OV')], image, loaded)


def test_bad_next_and_missing_native_root_rejected():
    image, before, variables = case()
    plan, heads = scaffold.plan(image, before, variables, [(0, 'REL')])
    after, loaded = apply(image, plan), loaded_from(before, heads)
    bad = copy.deepcopy(loaded)
    raw = bytearray.fromhex(bad['info_hex'])
    struct.pack_into('<H', raw, eq.HEADER+heads[0]['index']*eq.INFO_STRIDE+2, 2)
    bad['info_hex'] = raw.hex()
    with pytest.raises(scaffold.VoltageOffsetScaffoldError):
        scaffold.verify_loaded(image, before, variables, [(0, 'REL')], after, bad)
    bad = copy.deepcopy(before)
    for key in ('info_hex', 'control_hex'):
        raw = bytearray.fromhex(bad[key])
        raw[4] &= ~1
        bad[key] = raw.hex()
    with pytest.raises(scaffold.VoltageOffsetScaffoldError):
        scaffold.plan(image, bad, variables, [(0, 'REL')])


def test_second_rail_shared_root_remains_independent():
    image, before, variables = case(variable=13)
    rail = bytearray(image[0x800:0x800+70])
    rail[3] = 2
    other = bytearray(rail[5:70])
    other[1] = 2
    rail.extend(other)
    image = extend(image, [(0x68, None, 0x800, rail)])
    result, heads = scaffold.plan(image, before, variables, [(1, 'REL'), (0, 'REL')])
    assert [h['next_index'] for h in heads] == [0, 0]
    assert [h['index'] for h in heads] == [6, 7]
    after = apply(image, result)
    assert struct.unpack_from('<H', after, 0x800+5+65+13)[0] == 6
    assert struct.unpack_from('<H', after, 0x800+5+13)[0] == 7


def test_non_vfe_rail_bytes_are_preserved_and_do_not_block_scaffold():
    image, before, variables = case()
    modified = bytearray(image)
    row = 0x800 + 5
    for offset in (21, 23, 29, 31, *range(41, 65, 2)):
        struct.pack_into('<H', modified, row+offset, 0x1234)
    result, heads = scaffold.plan(bytes(modified), before, variables, [(0, 'REL')])
    after = apply(bytes(modified), result)
    assert heads[0]['next_index'] == 0
    assert after[row+21:row+33] == modified[row+21:row+33]
    assert after[row+41:row+65] == modified[row+41:row+65]
    struct.pack_into('<H', modified, row+35, 0x1234)
    with pytest.raises(scaffold.VoltageOffsetScaffoldError, match='rail selector'):
        scaffold.plan(bytes(modified), before, variables, [(0, 'REL')])


def test_unresolved_byte_indices_reserve_matching_inactive_slots():
    image, before, variables = case()
    modified = bytearray(image)
    row = 0x800 + 5
    modified[row+21], modified[row+23] = 6, 7
    with pytest.raises(scaffold.VoltageOffsetScaffoldError, match='inactive equation slots'):
        scaffold.plan(bytes(modified), before, variables, [(0, 'REL')])


def test_recognition_allows_unrelated_power_edit_but_activation_is_exact():
    image, before, variables = case()
    requests = [(0, 'REL')]
    plan, heads = scaffold.plan(image, before, variables, requests)
    after, loaded = apply(image, plan), loaded_from(before, heads)
    scaffold.verify_activation(image, before, variables, requests, after, loaded)
    unrelated = bytearray(after)
    unrelated[0x3800+0x38+13] ^= 1
    scaffold.verify_loaded(image, before, variables, requests, bytes(unrelated), loaded)
    with pytest.raises(scaffold.VoltageOffsetScaffoldError):
        scaffold.verify_activation(image, before, variables, requests, bytes(unrelated), loaded)
    for at in (heads[0]['row_offset']+2, 0x5000+14+29*255+8*25, heads[0]['selector_offset']):
        changed = bytearray(after)
        changed[at] ^= 1
        with pytest.raises(scaffold.VoltageOffsetScaffoldError):
            scaffold.verify_loaded(image, before, variables, requests, bytes(changed), loaded)
