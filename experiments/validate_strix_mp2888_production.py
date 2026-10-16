"""Local production-backend/UI-lifecycle validation; requests <=1125mV.

The GUI rendering/persistence hooks are replaced with journal hooks, while
controller methods, Apply normalization, clamp ownership, observed settling,
Auto and Release are the production implementations. No screenshot/UI claim.
Only the root agent runs this on the selected, previously identified adapter.
"""
import hashlib
import json
from pathlib import Path
from statistics import median
import time

import dearpygui.dearpygui as dpg
from druta import railctl
from druta.controllers.mp2888 import MP2888Candidate
from druta.druta import Druta
from druta.nvbackend import GPU


def main():
    root = Path(__file__).resolve().parent.parent
    sources = ('src/druta/controllers/mp2888.py', 'src/druta/druta.py',
               'src/druta/nvbackend.py', 'src/druta/profiles.py')
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sources}
    gpu = GPU()
    profile = next(p for p in railctl.load_profiles() if p.regulator == 'MPS MP2888A')
    candidate = MP2888Candidate(profile, gpu.nvapi, 1, 0x20)
    assert candidate.present()
    # Discovery retains the legacy offset method. Select the manual recipe
    # explicitly, just as the user does in the control-recipe combo.
    rail = candidate.for_control_method('pmbus')
    assert rail.present()
    assert gpu.read_vf_lock_status() == (None, None)
    assert gpu.read_clk_lock_status() == (None, None)
    original = rail.capture_control()
    assert not original['enabled'] and original['offset_word'] == 0 and original['fine_trim'] == 0
    assert rail.validate_control(original)
    fans = gpu.read_fan_control_state()
    assert fans
    result = {'scope': __doc__, 'source_sha256': hashes, 'entry': original,
              'fans_entry': fans, 'stages': [], 'events': []}
    journal = Path(__file__).with_suffix('.json')

    def save():
        journal.write_text(json.dumps(result, indent=2) + '\n')

    def log(message, ok=None):
        result['events'].append({'message': message, 'ok': ok})
        save()
        print(message, flush=True)

    def operating_point():
        point = app._manual_voltage_point(gpu)
        transitions = result.setdefault('operating_point_transitions', [])
        if not transitions or transitions[-1]['point'] != list(point):
            transitions.append({'time_monotonic': time.monotonic(), 'point': list(point),
                                'control': rail.capture_control()})
            save()
        return point

    dpg.create_context()
    app = Druta.__new__(Druta)
    app.gpu, app.rail = gpu, rail
    app._clk_lock = None
    app._i2c_busy = False
    app._profile_pending = None
    app._i2c_restore_failed = False
    app._i2c_manual_voltage = app._i2c_manual_clock_for = None
    app._i2c_verified = False
    app._i2c_verified_for = None
    app._gpu_gen = 0
    app.guard = lambda: True  # User explicitly authorized this local test.
    app.i2c_gate = lambda: (True, '')
    app.log = log
    app.autosave_before = lambda reason: log('journal snapshot before ' + reason)
    app.sync_profile_rail_sliders = lambda: None
    app.sync_lock_ui = lambda: None
    app.set_lock_state = lambda state, sync=True: setattr(app, '_clk_lock', state)

    def sample(label, expected_core=405):
        time.sleep(.3)
        rows = []
        for _ in range(15):
            control = rail.capture_control()
            state = gpu.read()
            value = rail.read_vout()
            assert value is not None and 300 <= value <= 1185
            assert 0 < state['core'] <= expected_core and state['temp_edge'] < 70
            assert gpu.read_clk_lock_status() == ((expected_core, expected_core), None)
            assert control['command'] * 6.25 <= 1125
            rows.append({'vout_mv': value, 'core_mhz': state['core'],
                         'gpu_vid_mv': state['vcore_mv'], 'temp_c': state['temp_edge']})
            time.sleep(.04)
        stage = {'label': label, 'median_mv': median(row['vout_mv'] for row in rows),
                 'control': rail.capture_control(), 'samples': rows}
        result['stages'].append(stage)
        save()
        print(json.dumps({k: v for k, v in stage.items() if k != 'samples'}), flush=True)
        return stage['median_mv']

    clean = False
    save()
    try:
        assert gpu.set_fan(100)[0]
        ok, message = app._prepare_manual_voltage()
        assert ok, message
        verified, message, rows = rail.verify(
            acknowledged=True, log=log, operating_point=operating_point)
        result['verification'] = {'ok': verified, 'message': message, 'rows': rows}
        save()
        assert verified, message
        assert rail.capture_control() == original
        ok, message = app._finish_manual_voltage('production Verify cleanup')
        assert ok, message
        assert gpu.read_clk_lock_status() == (None, None)
        app._i2c_verified = True
        app._i2c_verified_for = app.i2c_connection()
        volts = []
        for target in (1100, 1125, 1100):
            assert target <= 1125
            app.apply_i2c_rail(target)
            control = rail.capture_control()
            assert control['enabled'] and control['command'] * 6.25 == target
            assert app._i2c_manual_voltage['expected'] == control
            volts.append(sample(f'production Apply {target}mV'))
        assert min(volts[1] - volts[0], volts[1] - volts[2]) >= 15
        assert abs(volts[0] - volts[2]) <= 7
        # Deliberate user clock changes are permitted at the established
        # manual setpoint. The NEXT voltage Apply must re-observe the floor.
        assert gpu.lock_gpu_clocks(1500, 1500)[0]
        app.record_clock_lock()
        sample('manual1100mV with deliberate1500MHz clock', expected_core=1500)
        app.apply_i2c_rail(1125)
        sample('next Apply reacquired405MHz before voltage change')
        ok, message = app.release_current()
        assert ok, message
        app.set_lock_state(None)
        assert rail.capture_control() == original
        assert gpu.read_clk_lock_status() == (None, None)
        result['release_exact_entry'] = True
        app.apply_i2c_rail(1100)
        assert rail.capture_control()['enabled']
        ok, message = app.reset_i2c_rail(autosave=False)
        assert ok, message
        auto = rail.capture_control()
        assert not auto['enabled'] and gpu.read_clk_lock_status() == (None, None)
        result['auto'] = auto
        # Auto retains an inactive command. Put back the original inactive
        # command too, so this experiment leaves the exact entry state.
        ok, message = rail.restore_control(original, recovery=True, expected=auto)
        assert ok, message
        assert rail.capture_control() == original
        clean = True
    finally:
        try:
            if not clean:
                ok, message = app._finish_manual_voltage('production test recovery')
                log(message, ok)
                if ok and rail.capture_control() == original:
                    clean = True
            result['final'] = rail.capture_control()
            result['clock_final'] = gpu.read_clk_lock_status()
            result['vf_final'] = gpu.read_vf_lock_status()
            result['source_unchanged'] = all(
                hashlib.sha256((root / name).read_bytes()).hexdigest() == sha for name, sha in hashes.items())
            result['clean'] = clean and result['final'] == original and result['clock_final'] == (None, None)
            result['fans_restore'] = gpu.restore_fan_control_state(fans)
            save()
            print(json.dumps({k: result[k] for k in ('clean', 'final', 'clock_final',
                                                    'vf_final', 'source_unchanged', 'fans_restore')}), flush=True)
        finally:
            dpg.destroy_context()
    assert result['clean'] and result['source_unchanged']


if __name__ == '__main__':
    main()
