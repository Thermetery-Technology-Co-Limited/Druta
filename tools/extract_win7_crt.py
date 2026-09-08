"""Extract the pinned Microsoft VC++ 2019 x64 runtime without installing it.

Usage: python tools/extract_win7_crt.py vc_redist.x64.exe build/vc2019
Uses Windows' expand.exe. The hash is for the Microsoft-signed 14.29.30157.0
installer from https://aka.ms/vs/16/release/vc_redist.x64.exe.
"""
import argparse
import hashlib
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

SHA256 = '6afae68a783f11292149175844aed0e2ce3f247bc0250f6cb18c931295b3f399'
NS = {'b': 'http://schemas.microsoft.com/wix/2008/Burn'}


def extract(installer, destination):
    data = installer.read_bytes()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError('Expected Microsoft VC++ 2019 x64 14.29.30157.0 '
                         'installer; SHA256 does not match. No files extracted.')
    expand = shutil.which('expand.exe')
    if not expand:
        raise RuntimeError('Run this build helper on Windows (expand.exe required).')
    with tempfile.TemporaryDirectory(prefix='druta-vc2019-') as scratch:
        scratch = Path(scratch)
        cabinets = []
        offset = 0
        while True:
            offset = data.find(b'MSCF\0\0\0\0', offset)
            if offset < 0:
                break
            size = struct.unpack_from('<I', data, offset + 8)[0]
            if size < 36 or offset + size > len(data):
                raise ValueError('Invalid cabinet in verified installer')
            cabinet = scratch / ('container%d.cab' % len(cabinets))
            cabinet.write_bytes(data[offset:offset + size])
            directory = scratch / ('container%d' % len(cabinets))
            directory.mkdir()
            subprocess.run([expand, '-F:*', str(cabinet), str(directory)],
                           check=True, stdout=subprocess.DEVNULL)
            cabinets.append(directory)
            offset += size
        if len(cabinets) != 2:
            raise ValueError('Expected two embedded cabinets')
        manifest = ET.parse(str(cabinets[0] / '0')).getroot()
        payload = next(p for p in manifest.findall('b:Payload', NS)
                       if p.get('FilePath') ==
                       r'packages\vcRuntimeMinimum_amd64\cab1.cab')
        license_payload = next(p for p in manifest.findall('b:UX/b:Payload', NS)
                               if p.get('FilePath') == 'license.rtf')
        runtime_dir = scratch / 'runtime'
        runtime_dir.mkdir()
        subprocess.run([expand, '-F:*',
                        str(cabinets[1] / payload.get('SourcePath')),
                        str(runtime_dir)], check=True, stdout=subprocess.DEVNULL)
        destination.mkdir(parents=True, exist_ok=True)
        for name in ('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
            shutil.copy2(str(runtime_dir / name), str(destination / name))
        shutil.copy2(str(cabinets[0] / license_payload.get('SourcePath')),
                     str(destination / 'VC2019-LICENSE.rtf'))
    print('Extracted matched VC++ 2019 x64 runtime to', destination.resolve())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installer', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    extract(args.installer, args.destination)
