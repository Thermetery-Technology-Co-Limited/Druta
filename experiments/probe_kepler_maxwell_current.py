"""Read-only capture of legacy cards' power/current policy interfaces."""
import ctypes as C
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "druta"))
import nvbackend as n
from tools.probe_volt_rails import rm_call

out=ROOT/'experiments/current-limits-kepler-maxwell-20260908.json'
assert not out.exists(), 'Preserve previous evidence'
report={'read_only':True,'cards':[]}
for slot in ('0000:01:00.0','0000:02:00.0'):
    g=n.GPU(slot)
    assert g.nvapi.ok and not g.pairing_error
    card={'identity':g.static,'architecture':g.arch(),'architecture_name':g.arch_name(),'calls':{}}
    report['cards'].append(card)
    for name,cls in [('PowerPolInfo',n._PwrPolInfo),('PowerPolStatus',n._PwrPolStatus),('PowerTopo',n._PwrTopo)]:
        fn=getattr(g.nvapi,name)
        block=cls(version=g.nvapi.ver(cls,1))
        if fn is None:
            card['calls'][name]={'unavailable':True}
            continue
        trace=rm_call(g,invoke=lambda:fn(g.nvapi.gpu,C.byref(block)),capture_multiple=True)
        card['calls'][name]={'trace':trace,'nvapi_bytes':bytes(block).hex()}
        out.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(g.static['name'],g.arch_name(),name,trace['nvapi_status'],[
            (hex(c['escape_header'][14]),c['escape_size'],c.get('escape_rm_status'))
            for c in trace.get('captures',[]) if len(c.get('escape_header',[]))>=17])
    card['production_current_limits']=g.get_current_limits()
    card['telemetry']=g.read()
    out.write_text(json.dumps(report,indent=2),encoding='utf-8')
