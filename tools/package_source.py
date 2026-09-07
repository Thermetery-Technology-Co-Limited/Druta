"""Package the exact public working-tree source used for a legacy EXE build."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone

FILES = (
    '.github/PULL_REQUEST_TEMPLATE/i2c_profile.md',
    'AGENTS.md',
    'COPYING',
    'DEBUG-SUMMARY-RTX5080.md',
    'DRIVER-COMPATIBILITY.md',
    'Druta-win7.spec',
    'Druta.spec',
    'MANUAL.md',
    'PORTABLE-WINDOWS7.md',
    'README.md',
    'RELEASE-NOTES-1.3.0.md',
    'TECHNICALDOCUMENTATION.md',
    'THIRD-PARTY-NOTICES.md',
    'VOLTAGE-RAILS-47212.md',
    'VOLTAGE-RAILS-TITAN.md',
    'WINDOWS7.md',
    'app.py',
    'build-win7.ps1',
    'build.ps1',
    'druta.py',
    'experiments/compatibility-validation-47212.json',
    'experiments/kepler-gtx690-clock-domains.json',
    'experiments/kepler-gtx690-clock-domains.md',
    'experiments/kepler-gtx690-i2c-47212.md',
    'experiments/kepler-gtx690-timing-sweep-47212.json',
    'experiments/kepler-gtx690-validation-47212.json',
    'experiments/kepler-ncp4206-control-47212.json',
    'experiments/kepler-ncp4206-identity-47212.json',
    'experiments/kepler-timing-sweep-47212.json',
    'experiments/kepler-timing-writes-47212.json',
    'experiments/kepler-validation-47212.json',
    'experiments/legacy-frequency-production-47212.json',
    'experiments/legacy-offsets-47212-0000-01-00.0.json',
    'experiments/legacy-offsets-47212-0000-02-00.0.json',
    'experiments/legacy-private-layout-47212.json',
    'experiments/maxwell-gtx745-clock-domains.json',
    'experiments/maxwell-gtx745-clock-domains.md',
    'experiments/maxwell-gtx745-p0-fan-ui-47212.json',
    'experiments/maxwell-gtx745-p0-paths-47212.json',
    'experiments/maxwell-gtx745-timing-sweep-47212.json',
    'experiments/maxwell-gtx745-timing-sweep-47212.md',
    'experiments/maxwell-gtx745-validation-47212.json',
    'experiments/mp2888a-discovery-47212.json',
    'experiments/voltage-rails-20260906.json',
    'gpuload.py',
    'mp2888.py',
    'ncp4206.py',
    'nvbackend.py',
    'profiles.py',
    'railctl.py',
    'requirements-win7.txt',
    'requirements.txt',
    'shuntmod.py',
    'startup.py',
    'timings.py',
    'timingwrite.py',
    'tools/build_win7_sfx.py',
    'tools/collect_win7_redist.py',
    'tools/decode_kepler_clocks.py',
    'tools/extract_win7_crt.py',
    'tools/i2c_discover.py',
    'tools/package_source.py',
    'tools/probe_volt_rails.py',
    'tools/probe_volt_rails_47212.py',
    'tools/smoke_full_ui.py',
    'wincompat.py',
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root):
    root = root.resolve(strict=True)
    paths = set(FILES)
    for pattern in ('test_*.py', 'tests/test_*.py', 'i2c/*.toml', 'i2c/*.md'):
        paths.update(path.relative_to(root).as_posix() for path in root.glob(pattern))
    result = []
    for relative in sorted(paths):
        path = root / relative
        if not path.is_file() or path.is_symlink() or root not in path.resolve().parents:
            raise RuntimeError('Source file missing or linked: ' + relative)
        result.append({'path': relative, 'bytes': path.stat().st_size,
                       'sha256': digest(path)})
    return result


def package(root, before, bundle):
    root = root.resolve(strict=True)
    after = snapshot(root)
    if after != before:
        raise RuntimeError('Source changed during compilation; rebuild before packaging.')
    exe = bundle / 'Druta.exe'
    if not exe.is_file():
        raise RuntimeError('Build did not produce Druta.exe')
    source = bundle / 'source'
    source.mkdir()  # Never mix a previous source snapshot with a new build.
    for entry in after:
        target = source / entry['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / entry['path'], target)
        if digest(target) != entry['sha256']:
            raise RuntimeError('Source changed while copying: ' + entry['path'])
    revision = dirty = None
    if (root / '.git').exists() and shutil.which('git'):
        revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                           universal_newlines=True).strip()
        dirty = bool(subprocess.check_output(['git', '-C', str(root), 'status',
                                              '--porcelain', '--untracked-files=normal']))
    manifest = {'format': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
                'source_kind': 'working-tree snapshot, including uncommitted files',
                'git_commit': revision, 'git_working_tree_changes': dirty,
                'executable': 'Druta.exe', 'executable_sha256': digest(exe),
                'files': after}
    (source / 'SOURCE-MANIFEST.json').write_text(
        json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('snapshot', 'package'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--bundle', type=Path)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if args.action == 'snapshot':
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(json.dumps(snapshot(root), indent=2) + '\n', encoding='utf-8')
    else:
        if args.bundle is None:
            parser.error('package requires --bundle')
        package(root, json.loads(args.snapshot.read_text(encoding='utf-8')), args.bundle)
