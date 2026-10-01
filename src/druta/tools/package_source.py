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
    'BLACKWELL-VALIDATION.md',
    'COMPATIBILITY-GATING-AUDIT.md',
    'COPYING',
    'CURRENT-LIMITS-KEPLER-MAXWELL.md',
    'CURRENT-LIMITS-RTX5080.md',
    'DEBUG-SUMMARY-RTX5080.md',
    'DRIVER-COMPATIBILITY.md',
    'Druta-win7.spec',
    'Druta.spec',
    'MANIFEST.in',
    'MANUAL.md',
    'MAXWELL-PASCAL-VALIDATION.md',
    'PORTABLE-WINDOWS7.md',
    'README.md',
    'RELEASE-NOTES-1.3.0.md',
    'RELEASE-NOTES-1.5.0a.md',
    'RELEASE-NOTES-1.5.1.md',
    'RELEASE-NOTES-1.6.0.md',
    'TECHNICALDOCUMENTATION.md',
    'THIRD-PARTY-NOTICES.md',
    'VOLTAGE-RAILS-47212.md',
    'VOLTAGE-RAILS-TITAN.md',
    'WINDOWS7.md',
    'build-win7.ps1',
    'build.ps1',
    'druta.py',
    'experiments/566-schema-static/README.md',
    'experiments/566-schema-static/getter-response-canary.json',
    'experiments/I2C-VOUT-VALIDATION.md',
    'experiments/clock-capability-retry-20260916.json',
    'experiments/compatibility-validation-47212.json',
    'experiments/correlate_mp29816.py',
    'experiments/current-limit-ceiling-titan-rtx-20260908.json',
    'experiments/current-limit-ceiling-titan-xp-20260908.json',
    'experiments/current-limits-kepler-maxwell-20260908.json',
    'experiments/current-limits-kepler-maxwell-modern-get-20260908.json',
    'experiments/current-limits-titan-20260908.json',
    'experiments/current-limits-titan-47212-20260908.json',
    'experiments/current-limits-titan-61088-20260908.json',
    'experiments/gating-refresh-gtx770-47212-20260909.json',
    'experiments/i2c-direct-vmon-worker-gtx770-47212-20260909.json',
    'experiments/i2c-direct-vout-worker-titan-47212-20260909.json',
    'experiments/i2c-vmon-command-trace-gtx770-47212-20260909.json',
    'experiments/kepler-gtx690-clock-domains.json',
    'experiments/kepler-gtx690-clock-domains.md',
    'experiments/kepler-gtx690-i2c-47212.md',
    'experiments/kepler-gtx690-timing-sweep-47212.json',
    'experiments/kepler-gtx690-validation-47212.json',
    'experiments/kepler-gtx770-clock-crosscheck.json',
    'experiments/kepler-gtx770-clock-crosscheck.md',
    'experiments/kepler-ncp4206-control-47212.json',
    'experiments/kepler-ncp4206-identity-47212.json',
    'experiments/kepler-p0-fan-ui-gtx770-47212-20260909.json',
    'experiments/kepler-rom-analysis/FINDINGS.md',
    'experiments/kepler-rom-analysis/channel-readbacks.json',
    'experiments/kepler-rom-analysis/decode_rom.py',
    'experiments/kepler-rom-analysis/decoded-rom.json',
    'experiments/kepler-rom-analysis/read_channels.py',
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
    'experiments/power-5080-20260908/MP29816-VALIDATION.md',
    'experiments/power-5080-20260908/POLICY13-MAXIMUM-RESULT.md',
    'experiments/power-5080-20260908/POLICY13-WRITE-RESULT.md',
    'experiments/power-5080-20260908/POLICY14-MAXIMUM-RESULT.md',
    'experiments/power-5080-20260908/POLICY14-WRITE-RESULT.md',
    'experiments/power-5080-20260908/mp29816-correlation.json',
    'experiments/power-5080-20260908/mp29816-nv40-i2c-post-reboot-100khz-buffer-same-value.json',
    'experiments/power-5080-20260908/mp29816-nv40-i2c-post-reboot-100khz-smbus-same-value.json',
    'experiments/power-5080-20260908/mp29816-nv40-i2c-read-matrix.json',
    'experiments/power-5080-20260908/mp29816-post-reboot-minimal.json',
    'experiments/power-5080-20260908/mp29816-production-writer.json',
    'experiments/power-5080-20260908/policy13-maximum/execution.json',
    'experiments/power-5080-20260908/policy13-write/execution.json',
    'experiments/power-5080-20260908/policy14-maximum/execution.json',
    'experiments/power-5080-20260908/policy14-write/execution.json',
    'experiments/power-5080-20260908/production-current-limits.json',
    'experiments/probe_5080_policy13_max.py',
    'experiments/probe_5080_policy13_write.py',
    'experiments/probe_5080_policy14_max.py',
    'experiments/probe_5080_policy14_write.py',
    'experiments/probe_5080_policy_series.py',
    'experiments/probe_5080_power_channels.py',
    'experiments/probe_kepler_maxwell_current.py',
    'experiments/probe_titan_current_ceiling.py',
    'experiments/runtime-i2c-ui-58097.json',
    'experiments/runtime-rails-47212.json',
    'experiments/runtime-rails-56636.json',
    'experiments/runtime-rails-58097.json',
    'experiments/runtime-rails-58266.json',
    'experiments/titan-rtx-clock-sanity-56636-58097-20260916.json',
    'experiments/titan-rtx-clock-sanity-56636-58097-20260916.md',
    'experiments/validate_5080_current_limits_production.py',
    'experiments/validate_titan_current_limits_production.py',
    'experiments/voltage-rails-20260906.json',
    'pyproject.toml',
    'requirements-win7.txt',
    'requirements.txt',
    'setup.py',
    'src/druta/__init__.py',
    'src/druta/__main__.py',
    'src/run_druta.py',
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root):
    root = root.resolve(strict=True)
    paths = set(FILES)
    # Match build.ps1: the complete package and regression suite, including
    # package initializers and test helpers, plus the public regulator recipes.
    for pattern in ('src/druta/**/*.py', 'tests/**/*.py', 'i2c/*.toml', 'i2c/*.md'):
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
