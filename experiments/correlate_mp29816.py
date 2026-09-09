"""Read-only controller correlation with a bounded, PCI-selected CUDA load."""
import json
from pathlib import Path
import statistics
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "druta"))
import gpuload
import nvbackend
import railctl

OUT = ROOT / 'experiments/power-5080-20260908/mp29816-correlation.json'
assert not OUT.exists()
gpu = nvbackend.GPU('0000:01:00.0')
assert gpu.static['vbios'] == '98.03.3b.c0.6f'
data = tomllib.loads((ROOT / 'i2c/asus-rtx5080-astral-mp29816.toml').read_text(encoding='utf-8'))
data.pop('write', None)  # Discovery stays read-only as the production profile evolves.
p = railctl.Profile(data)
rail = railctl.Rail(p, gpu.nvapi)
assert rail.present() and p.read_only
report = dict(identity=gpu.static, i2c_writes=0, tuning_writes=0, samples=[])

def sample(phase):
    s = dict(phase=phase, time=time.time(), nvapi_before=gpu.read_vcore_mv())
    s['raw'] = {hex(r): rail.read(r, w) for r, w in
                ((0x99,4),(0x9A,7),(0x9B,3),(0x00,1),(0x29,2),(0x67,2),(0x8B,2),(0x8C,2))}
    s['page_after'] = rail.read(0x00,1)
    s['scale_after'] = rail.read(0x29,2)
    s['nvapi_after'] = gpu.read_vcore_mv()
    s['vout_mv'] = (s['raw']['0x8b'] & 0xfff) * 5
    report['samples'].append(s)
    time.sleep(.1)

load = gpuload.BandwidthLoad(max_seconds=10, slot='0000:01:00.0')
try:
    for _ in range(15): sample('idle_before')
    load.start()
    assert load.wait_started(), load.error
    time.sleep(1)
    for _ in range(40):
        assert load.running, load.error
        sample('loaded')
finally:
    load.stop()
    load.join(10)
    report['load'] = dict(error=load.error, running=load.running, stats=load.stats,
                          slot=load.device_slot)
    OUT.write_text(json.dumps(report, indent=2))
for _ in range(15): sample('idle_after')
report['summary'] = {}
for phase in ('idle_before','loaded','idle_after'):
    xs = [s for s in report['samples'] if s['phase'] == phase]
    report['summary'][phase] = dict(n=len(xs),
        vout_range=[min(s['vout_mv'] for s in xs),max(s['vout_mv'] for s in xs)],
        nvapi_range=[min(s['nvapi_after'] for s in xs),max(s['nvapi_after'] for s in xs)],
        mean_abs_difference_mv=statistics.mean(abs(s['vout_mv']-s['nvapi_after']) for s in xs),
        mean_difference_mv=statistics.mean(s['vout_mv']-s['nvapi_after'] for s in xs))
report['all_identity_gates_stable'] = all(s['raw']['0x99']==0x4d505303 and
    s['raw']['0x9a']==0x4d323a38313606 and s['raw']['0x9b']==0x102 and
    s['raw']['0x0']==s['page_after']==0 and
    s['raw']['0x29']&0x1c00==s['scale_after']&0x1c00==0x400 for s in report['samples'])
OUT.write_text(json.dumps(report, indent=2))
print(json.dumps({k:v for k,v in report.items() if k not in ('identity','samples')}, indent=2))
