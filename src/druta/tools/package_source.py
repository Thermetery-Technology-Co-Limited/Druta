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
    'ADA-VALIDATION.md',
    'AGENTS.md',
    'BLACKWELL-VALIDATION.md',
    'COMPATIBILITY-GATING-AUDIT.md',
    'COPYING',
    'CURRENT-LIMITS-KEPLER-MAXWELL.md',
    'CURRENT-LIMITS-RTX5080.md',
    'DEBUG-SUMMARY-RTX5080.md',
    'DRIVER-COMPATIBILITY.md',
    'Druta-Diagnose.cmd',
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
    'RELEASE-NOTES-1.7.0.md',
    'RELEASE-NOTES-1.8.0.md',
    'RELEASE-NOTES-1.8.0a.md',
    'RELEASE-NOTES-1.8.0b.md',
    'RELEASE-NOTES-1.8.0c.md',
    'RELEASE-NOTES-1.8.1.md',
    'RELEASE-NOTES-1.9.03.md',
    'RELEASE-NOTES-1.9.04.md',
    'RELEASE-NOTES-1.9.05.md',
    'RELEASE-NOTES-1.9.06.md',
    'RELEASE-NOTES-1.9.07.md',
    'RELEASE-NOTES-1.9.08.md',
    'RELEASE-NOTES-1.9.0a.md',
    'RELEASE-NOTES-1.9.0b.md',
    'RELEASE-NOTES-1.9.1.md',
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
    'experiments/ada-ad103-validation.json',
    'experiments/ada-up9512r-product-2026-10-09.json',
    'experiments/ampere-four-digit-policy-summary-20261027.json',
    'experiments/ampere-ga102-kingpin-grpg-61742-20261105.json',
    'experiments/ampere-ga102-kingpin-grpg-61742-20261105.md',
    'experiments/ampere-ga102-kingpin-ms-61742-20261105.json',
    'experiments/ampere-ga102-kingpin-ms-61742-20261105.md',
    'experiments/ampere-ga102-kingpin-v30-policy-rows-61742-20261105.json',
    'experiments/ampere-ga102-kingpin-v30-policy-rows-61742-20261105.md',
    'experiments/ampere-ga104-3070ti-20260928.md',
    'experiments/ampere-live-client-power-20261027.md',
    'experiments/ampere-provenance-handoff-20261028.json',
    'experiments/ampere-provenance-qualified-20261029.json',
    'experiments/ampere-short-power-handoff-20261029.json',
    'experiments/capture_power_provenance.py',
    'experiments/clock-capability-retry-20260916.json',
    'experiments/clock-vf-profiles-20261102.json',
    'experiments/clock-vf-smoothing-profile-20261105.json',
    'experiments/combined-full-profiles-20261102.json',
    'experiments/combined-profiles-20261102.json',
    'experiments/compatibility-validation-47212.json',
    'experiments/correlate_mp29816.py',
    'experiments/current-limit-ceiling-titan-rtx-20260908.json',
    'experiments/current-limit-ceiling-titan-xp-20260908.json',
    'experiments/current-limits-kepler-maxwell-20260908.json',
    'experiments/current-limits-kepler-maxwell-modern-get-20260908.json',
    'experiments/current-limits-titan-20260908.json',
    'experiments/current-limits-titan-47212-20260908.json',
    'experiments/current-limits-titan-61088-20260908.json',
    'experiments/driver-transport-followup-20261028.json',
    'experiments/gating-refresh-gtx770-47212-20260909.json',
    'experiments/grpg-bit1-availability-qualified-20261029.json',
    'experiments/grpg-bit4-combined-qualified-20261029.json',
    'experiments/grpg-bit4-no-peer-qualified-20261029.json',
    'experiments/grpg-bit4-qualified-20261029.json',
    'experiments/grpg-main-complete-qualified-20261030.json',
    'experiments/hold-headroom-rtx-3070ti-59597.md',
    'experiments/hold-headroom-titan-rtx-61088-20260929.md',
    'experiments/i2c-direct-vmon-worker-gtx770-47212-20260909.json',
    'experiments/i2c-direct-vout-worker-titan-47212-20260909.json',
    'experiments/i2c-vmon-command-trace-gtx770-47212-20260909.json',
    'experiments/iddq-model-response-20261030.json',
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
    'experiments/linked-current-weight-composition-5080-61742-20261105.json',
    'experiments/linked-current-weight-composition-5080-61742-20261105.md',
    'experiments/lowpower-request-profiles-20261102.json',
    'experiments/lpwr-telemetry-provenance-20261029.json',
    'experiments/maxwell-gtx745-clock-domains.json',
    'experiments/maxwell-gtx745-clock-domains.md',
    'experiments/maxwell-gtx745-p0-fan-ui-47212.json',
    'experiments/maxwell-gtx745-p0-paths-47212.json',
    'experiments/maxwell-gtx745-timing-sweep-47212.json',
    'experiments/maxwell-gtx745-timing-sweep-47212.md',
    'experiments/maxwell-gtx745-validation-47212.json',
    'experiments/measured-clock-sources-titan-rtx-61088-20260929.md',
    'experiments/media-lpwr-configured-qualified-20261029.json',
    'experiments/media-type4-last-role-qualified-20261029.json',
    'experiments/meter_strix_mp2888_production.json',
    'experiments/mp2888a-discovery-47212.json',
    'experiments/ms-configured-qualified-20261029.json',
    'experiments/ms-loss20-qualified-20261101.json',
    'experiments/ms-mapping-loss04-qualified-20261030.json',
    'experiments/ms-mapping-neighbors-qualified-20261030.json',
    'experiments/ms-retained-class-qualified-20261101.json',
    'experiments/nvdec-configured-qualified-20261029.json',
    'experiments/nvvdd-offset-profiles-20261105.json',
    'experiments/nvvdd-offset-profiles-20261105.md',
    'experiments/ordinary-named-profiles-20261102.json',
    'experiments/ordinary-tuning-undo-20261102.json',
    'experiments/pascal-gp106-driver47212-20261102.json',
    'experiments/pascal-gp106-driver47212-20261102.md',
    'experiments/pascal-gp106-driver51694-20261102.json',
    'experiments/pascal-gp106-driver51694-20261102.md',
    'experiments/pascal-gp106-driver57283-20261102.json',
    'experiments/pascal-gp106-driver57283-20261102.md',
    'experiments/pascal-gp106-live-power-20261102.json',
    'experiments/pascal-gp106-live-power-20261102.md',
    'experiments/pcie-and-idle-boundaries-61742-20261105.json',
    'experiments/pcie-and-idle-boundaries-61742-20261105.md',
    'experiments/pcie-speed-5080-61742-20261105.json',
    'experiments/pcie-speed-5080-61742-20261105.md',
    'experiments/pcie-speed-profiles-20261105.json',
    'experiments/policy-intermediate-delay-5080-61742-20261105.json',
    'experiments/policy-intermediate-delay-5080-61742-20261105.md',
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
    'experiments/power-explicit-source-20261105.json',
    'experiments/power-explicit-source-20261105.md',
    'experiments/power-integral-count-failure-20261105.json',
    'experiments/power-integral-count-failure-20261105.md',
    'experiments/power-integral-lower-5080-61742-20261105.json',
    'experiments/power-integral-lower-5080-61742-20261105.md',
    'experiments/power-integral-lower-production-20261105.json',
    'experiments/power-integral-production-20261105.json',
    'experiments/power-integral-profiles-20261105.json',
    'experiments/power-policy-integral-upper-5080-61742-20261105.json',
    'experiments/power-policy-integral-upper-5080-61742-20261105.md',
    'experiments/power-relationship-formula-5080-20261105.json',
    'experiments/power-relationship-formula-5080-20261105.md',
    'experiments/power-relationship-info-20261105.json',
    'experiments/power-relationship-info-20261105.md',
    'experiments/power-type33-relationship-ranges-20261105.json',
    'experiments/power-type33-relationship-ranges-20261105.md',
    'experiments/power-v30-class1-20261028.json',
    'experiments/power-v30-short-row-20261029.json',
    'experiments/power-weight-override-5080-61742-20261105.json',
    'experiments/power-weight-override-5080-61742-20261105.md',
    'experiments/probe_5080_policy13_max.py',
    'experiments/probe_5080_policy13_write.py',
    'experiments/probe_5080_policy14_max.py',
    'experiments/probe_5080_policy14_write.py',
    'experiments/probe_5080_policy_series.py',
    'experiments/probe_5080_power_channels.py',
    'experiments/probe_kepler_maxwell_current.py',
    'experiments/probe_titan_current_ceiling.py',
    'experiments/rail-limit-held-intent-20261105.json',
    'experiments/rail-limit-profiles-20261105.json',
    'experiments/release-1.7.0-smoke-titan-rtx-61088.md',
    'experiments/runtime-i2c-ui-58097.json',
    'experiments/runtime-rails-47212.json',
    'experiments/runtime-rails-56636.json',
    'experiments/runtime-rails-58097.json',
    'experiments/runtime-rails-58266.json',
    'experiments/shared-power-profile-boundary-20261102.json',
    'experiments/strix-3070ti-mp2888-20261015.md',
    'experiments/titan-rtx-clock-sanity-56636-58097-20260916.json',
    'experiments/titan-rtx-clock-sanity-56636-58097-20260916.md',
    'experiments/turing-43160-clock-compat-20261028.json',
    'experiments/turing-43160-live-power-20261028.json',
    'experiments/turing-43160-static-layout-20261028.json',
    'experiments/turing-43160-static-layout-20261028.md',
    'experiments/turing-47212-clock-compat-20261028.json',
    'experiments/turing-47212-compatibility-20261028.md',
    'experiments/turing-47212-live-power-20261028.json',
    'experiments/turing-62101-live-power-20261028.json',
    'experiments/turing-driver-abi-review-20261028.json',
    'experiments/turing-driver-abi-review-20261028.md',
    'experiments/turing-driver-live-comparison-20261028.md',
    'experiments/turing-owner-furmark-ab-20261028.md',
    'experiments/turing-tu104-live-power-20261028.json',
    'experiments/turing-tu104-live-power-20261028.md',
    'experiments/ui-current-controls-capture-20261102.json',
    'experiments/ui-live-panels-capture-20261028.json',
    'experiments/ui-lpwr-configured-qualified-20261029.json',
    'experiments/ui-recovery-result-qualified-20261029.json',
    'experiments/ui-registry-power-qualified-20261029.json',
    'experiments/ui-render-capture-20261028.json',
    'experiments/validate_5080_current_limits_production.py',
    'experiments/validate_pascal_live_power.py',
    'experiments/validate_short_power_override.py',
    'experiments/validate_strix_mp2888_production.json',
    'experiments/validate_strix_mp2888_production.py',
    'experiments/validate_titan_current_limits_production.py',
    'experiments/validate_turing_clock_compat.py',
    'experiments/validate_turing_live_power.py',
    'experiments/voltage-61742-unblocked-objectives-20261101.json',
    'experiments/voltage-control-profiles-20261102.json',
    'experiments/voltage-live-bounds-20261029.json',
    'experiments/voltage-live-constants-20261028.json',
    'experiments/voltage-offset-undo-20261102.json',
    'experiments/voltage-rail-knobs-20261101.json',
    'experiments/voltage-rails-20260906.json',
    'experiments/voltage-replay-followup-20261028.json',
    'experiments/voltage-request-profiles-20261102.json',
    'experiments/voltage-vmin-live-20261028.json',
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
