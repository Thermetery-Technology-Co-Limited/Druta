"""Offline decoding of the supplied ROM; never edits it or accesses hardware.

Table locations and sense/budget/DCB field layouts follow envytools nvbios
and nouveau's BIOS parsers. Topology relations are inferred from this ROM
and checked against the captured R472 channel metadata and readbacks.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('rom', type=Path, help='User-supplied GTX 770 NVGI-wrapped ROM; read only')
parser.add_argument('--output', type=Path, required=True, help='New output file; existing evidence is preserved')
args = parser.parse_args()
source = args.rom
assert not args.output.exists(), 'Preserve earlier evidence'
b = source.read_bytes()
assert b[:4] == b'NVGI' and b[0x600:0x602] == b'\x55\xaa'
base = 0x600
u16 = lambda offset: struct.unpack_from('<H', b, offset)[0]
u32 = lambda offset: struct.unpack_from('<I', b, offset)[0]
bit = b.index(b'\xff\xb8BIT', base)
entries = {}
for i in range(b[bit + 10]):
    o = bit + b[bit + 8] + i * b[bit + 9]
    entries[chr(b[o])] = (b[o + 1], u16(o + 2), base + u16(o + 4))
assert entries['P'][:2] == (2, 100)
p = entries['P'][2]

def table(rel):
    o = base + u32(p + rel)
    version, hlen, stride, count = b[o:o + 4]
    return {'file_offset': hex(o), 'rom_offset': hex(o - base),
            'version': version, 'header_hex': b[o:o + hlen].hex(),
            'stride': stride, 'count': count,
            'records': [{'file_offset': hex(o + hlen + i * stride),
                         'hex': b[o + hlen + i * stride:o + hlen + (i + 1) * stride].hex()}
                        for i in range(count)]}

report = {'source': source.name, 'sha256': hashlib.sha256(b).hexdigest(),
          'image_base': hex(base), 'read_only': True,
          'shunt_mod_user_report': '5 mOhm stacked over existing 5 mOhm; effective 2.5 mOhm per modified shunt',
          'sense': table(0x28), 'budget': table(0x2c), 'topology': table(0x3c)}
budget = report['budget']
assert (budget['version'], budget['stride'], budget['count']) == (0x20, 34, 9)
budget['cap_entry'] = bytes.fromhex(budget['header_hex'])[9]
for row in budget['records']:
    raw = bytes.fromhex(row['hex'])
    row.update(kind_flags=raw[0], channel=raw[1],
               minimum_mw=struct.unpack_from('<I', raw, 2)[0],
               default_mw=struct.unpack_from('<I', raw, 6)[0],
               maximum_mw=struct.unpack_from('<I', raw, 10)[0])
topo = report['topology']
assert (topo['version'], topo['stride'], topo['count']) == (0x10, 18, 8)
for row in topo['records']:
    raw = bytes.fromhex(row['hex'])
    row.update(kind=raw[0], domain_tag=hex(raw[1]),
               nominal_uv=struct.unpack_from('<I', raw, 2)[0],
               scale_q12=struct.unpack_from('<I', raw, 6)[0],
               offset_mw=struct.unpack_from('<i', raw, 10)[0],
               source_first=raw[14], source_last=raw[15])
o = int(topo['file_offset'], 16)
head = bytes.fromhex(topo['header_hex'])
start = o + len(head) + topo['stride'] * topo['count']
topo['relations'] = []
for i in range(head[5]):
    at = start + i * head[4]
    topo['relations'].append({'file_offset': hex(at), 'kind': b[at],
                              'channel': b[at + 1], 'scale_q12': u32(at + 2),
                              'hex': b[at:at + head[4]].hex()})
dcb = base + u16(base + 0x36)
ext = base + u16(dcb + 18)
ver, hlen, count, stride = b[ext:ext + 4]
report['external_devices'] = []
for i in range(count):
    at = ext + hlen + i * stride
    raw = b[at:at + stride]
    report['external_devices'].append({'index': i, 'file_offset': hex(at),
        'hex': raw.hex(), 'type': hex(raw[0]), 'address_7bit': hex(raw[1] >> 1),
        'secondary_port': bool(raw[2] & 16)})
assert report['external_devices'][1]['type'] == '0x4e'
sense0 = bytes.fromhex(report['sense']['records'][0]['hex'])
assert sense0[:2] == bytes([1, 1])
report['sensor'] = {'type': 'INA3221', 'extdev_index': 1, 'address_7bit': '0x40',
                    'shunts_mohm': [sense0[i] for i in (5, 7, 9)]}
assert report['sensor']['shunts_mohm'] == [5, 5, 5]
live = json.loads((ROOT / 'experiments/current-limits-kepler-maxwell-20260908.json').read_text(encoding='utf-8'))['cards'][1]
info = next(x for x in live['calls']['PowerPolInfo']['captures']
            if x['command'] == '0x20802618')
assert info['policy_mask'] == 0xff
for i, row in enumerate(budget['records'][:8]):
    record = info['records'][i]
    assert record['policy'] == i
    words = record['words']
    assert row['channel'] == (words[1] >> 8) & 255
    assert [row[k] for k in ('minimum_mw', 'default_mw', 'maximum_mw')] == words[2:5]
report['first_eight_match_live_policies'] = True
samples = json.loads((HERE / 'channel-readbacks.json').read_text(encoding='utf-8'))['samples']
report['topology_formula_checks'] = []
for sample in samples:
    assert sample['ntstatus'] == sample['rm_status'] == 0
    values = [sample['params'][3 + 20 * i] for i in range(7)]
    predicted = [sum(values[:3]) + 4000, max(0, values[0] - 5000),
                 values[1] + values[2], (values[5] * 3113 + 2048) // 4096]
    assert values[3:] == predicted
    report['topology_formula_checks'].append({'channels_mw': values, 'predicted_channels_3_to_6_mw': predicted})
report['interpretation_limits'] = [
    'Connector names slot/6-pin/8-pin are inferred, not confirmed by PCB continuity.',
    '76% relation verified at idle; no loaded correlation was performed.',
    'Kind/flag semantics of duplicate and ninth budget records are not fully decoded.',
    'Shunt correction applies to modified sensed channels before calibration and modeled sums.'
]
args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
print('ROM hash:', report['sha256'])
print('All 8 live policies match ROM. All 3 samples satisfy the channel formulas.')
print('Sensor:', report['sensor'])
