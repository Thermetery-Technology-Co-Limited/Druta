"""Read only the captured GTX 770/R472 legacy channel-status interface."""
import ctypes as C
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "druta"))
import nvbackend as n
from tools.probe_volt_rails import rm_call

out = Path(__file__).with_name('channel-readbacks.json')
assert not out.exists(), 'Preserve earlier evidence'
g = n.GPU('0000:02:00.0')
assert g.static['name'] == 'NVIDIA GeForce GTX 770'
assert g.static['driver'] == '472.12'
assert not g.pairing_error
block = n._PwrTopo(version=g.nvapi.ver(n._PwrTopo, 1))
trace = rm_call(g, invoke=lambda: g.nvapi.PowerTopo(g.nvapi.gpu, C.byref(block)),
                capture_multiple=True)
capture = next(x for x in trace['captures'] if x['escape_header'][14] == 0x20802613)
info = next(x for x in trace['captures'] if x['escape_header'][14] == 0x20802612)
assert capture['escape_rm_status'] == info['escape_rm_status'] == 0
assert len(capture['escape_input_params']) == 707
assert info['escape_output_params'][2] == 0x7f
report = {'read_only': True, 'identity': g.static, 'info': info, 'samples': []}
for _ in range(3):
    packet = (n.u32 * (17 + 707))()
    packet[:17] = capture['escape_header']
    packet[16] = 0
    packet[17:] = capture['escape_input_params']
    packet[17] = 0x7f  # Select only the live channel mask, using the same GET.
    assert packet[14] == 0x20802613 and packet[15] == 2828
    status = g._legacy_clk_escape(packet, capture['escape_fields'])
    sample = {'ntstatus': status, 'rm_status': packet[16], 'params': list(packet[17:])}
    report['samples'].append(sample)
    out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    assert status == packet[16] == 0
    print('nonzero:', [(i, v) for i, v in enumerate(sample['params']) if v][:100])
    time.sleep(.2)
