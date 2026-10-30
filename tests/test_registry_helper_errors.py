from unittest.mock import Mock

from druta import registry_overrides as ro


def test_missing_receipt_waits_then_relaunches_selected_adapter(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(ro.devicerecovery, 'wait_for_exit', lambda pid: events.append(('wait', pid)))
    def load(path):
        events.append(('load', path))
        raise FileNotFoundError('receipt missing')
    monkeypatch.setattr(ro, 'load_receipt', load)
    execute = Mock()
    launch = Mock()
    monkeypatch.setattr(ro, 'execute', execute)
    monkeypatch.setattr(ro.subprocess, 'Popen', launch)
    monkeypatch.setattr(ro.startup, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(ro.startup, 'application_command', lambda *args, **kwargs: list(args))
    notify = Mock()
    assert ro.cli(['missing', '--parent-pid', '42', '--expected-slot', '0000:01:00.0'], notify) == 1
    assert events == [('wait', 42), ('load', 'missing')]
    execute.assert_not_called()
    cmd = launch.call_args.args[0]
    assert cmd[cmd.index('--gpu') + 1] == '0000:01:00.0'
    assert '--recovery-result' in cmd
    assert ro.startup.read_json(cmd[cmd.index('--recovery-result') + 1])['slot'] == '0000:01:00.0'


def test_missing_receipt_still_waits_for_parent(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(ro.devicerecovery, 'wait_for_exit', lambda pid: events.append('wait'))
    def load(path):
        events.append('load')
        raise FileNotFoundError('receipt missing')
    monkeypatch.setattr(ro, 'load_receipt', load)
    monkeypatch.setattr(ro.startup, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(ro.startup, 'application_command', lambda *args, **kwargs: list(args))
    monkeypatch.setattr(ro.subprocess, 'Popen', Mock())
    assert ro.cli(['missing', '--parent-pid', '42'], Mock()) == 1
    assert events == ['wait', 'load']


def test_expected_slot_rejects_other_adapter_before_execute(monkeypatch, tmp_path):
    monkeypatch.setattr(ro, 'load_receipt', lambda _: {'target': {'slot': '0000:02:00.0'}})
    execute, launch = Mock(), Mock()
    monkeypatch.setattr(ro, 'execute', execute)
    monkeypatch.setattr(ro.subprocess, 'Popen', launch)
    monkeypatch.setattr(ro.startup, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(ro.startup, 'application_command', lambda *args, **kwargs: list(args))
    assert ro.cli(['saved', '--expected-slot', '0000:01:00.0'], Mock()) == 1
    execute.assert_not_called()
    cmd = launch.call_args.args[0]
    assert cmd[cmd.index('--gpu') + 1] == '0000:01:00.0'
    report = ro.startup.read_json(cmd[cmd.index('--recovery-result') + 1])
    assert 'does not match' in report['message'] and report['slot'] == '0000:01:00.0'


def test_unconfirmed_parent_does_not_load_execute_or_relaunch(monkeypatch):
    wait = Mock(side_effect=RuntimeError('timeout'))
    load, execute, launch, notify = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(ro.devicerecovery, 'wait_for_exit', wait)
    monkeypatch.setattr(ro, 'load_receipt', load)
    monkeypatch.setattr(ro, 'execute', execute)
    monkeypatch.setattr(ro.subprocess, 'Popen', launch)
    assert ro.cli(['saved', '--parent-pid', '42', '--expected-slot', '0000:01:00.0'], notify) == 1
    load.assert_not_called()
    execute.assert_not_called()
    launch.assert_not_called()
    assert 'still running' in notify.call_args.args[0]


def test_readonly_recovery_capture_is_forwarded_after_failed_load(monkeypatch, tmp_path):
    monkeypatch.setattr(ro, 'load_receipt', Mock(side_effect=FileNotFoundError('missing')))
    monkeypatch.setattr(ro.startup, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(ro.startup, 'application_command', lambda *args, **kwargs: list(args))
    launch, execute = Mock(), Mock()
    monkeypatch.setattr(ro.subprocess, 'Popen', launch)
    monkeypatch.setattr(ro, 'execute', execute)
    assert ro.cli(['saved', '--expected-slot', '0000:01:00.0', '--ui-capture-recovery', 'capture'], Mock()) == 1
    cmd = launch.call_args.args[0]
    assert cmd[cmd.index('--ui-capture-recovery') + 1] == 'capture'
    assert '--recovery-result' in cmd
    execute.assert_not_called()


def test_launch_and_recovery_forward_selected_slot(monkeypatch):
    launch = Mock()
    monkeypatch.setattr(ro.subprocess, 'Popen', launch)
    monkeypatch.setattr(ro.startup, 'application_command', lambda *args, **kwargs: list(args))
    ro.launch_helper({'path': 'saved', 'target': {'slot': '0000:02:00.0'}})
    assert launch.call_args.args[0][-2:] == ['--expected-slot', '0000:02:00.0']
    monkeypatch.setattr(ro, 'recovery_receipt', lambda slot: {'path': 'saved'})
    cli = Mock(return_value=0)
    monkeypatch.setattr(ro, 'cli', cli)
    assert ro.recovery_cli(['0000:02:00.0'], Mock()) == 0
    assert cli.call_args.args[0] == ['saved', '--action', 'restore', '--expected-slot', '0000:02:00.0']


def test_matching_receipt_executes_and_relaunches(monkeypatch, tmp_path):
    monkeypatch.setattr(ro, 'load_receipt', lambda _: {'target': {'slot': '0000:01:00.0'}})
    result = {'target': {'slot': '0000:01:00.0'}, 'ok': True, 'reboot_required': False,
              'path': 'saved', 'message': 'verified'}
    execute, launch = Mock(return_value=result), Mock()
    monkeypatch.setattr(ro, 'execute', execute)
    monkeypatch.setattr(ro.subprocess, 'Popen', launch)
    monkeypatch.setattr(ro.startup, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(ro.startup, 'application_command', lambda *args, **kwargs: list(args))
    assert ro.cli(['saved', '--expected-slot', '0000:01:00.0', '--action', 'restore'], Mock()) == 0
    assert execute.call_args.kwargs['restore'] is True
    assert launch.call_args.args[0][1] == '0000:01:00.0'


def test_legacy_missing_receipt_does_not_invent_adapter(monkeypatch, tmp_path):
    monkeypatch.setattr(ro, 'load_receipt', Mock(side_effect=FileNotFoundError('missing')))
    monkeypatch.setattr(ro.startup, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(ro.startup, 'application_command', lambda *args, **kwargs: list(args))
    launch = Mock()
    monkeypatch.setattr(ro.subprocess, 'Popen', launch)
    assert ro.cli(['saved'], Mock()) == 1
    cmd = launch.call_args.args[0]
    assert '--gpu' not in cmd
    assert ro.startup.read_json(cmd[cmd.index('--recovery-result') + 1])['slot'] == ''


def test_capture_options_reject_conflicts_before_execution(monkeypatch):
    import pytest
    load = Mock()
    monkeypatch.setattr(ro, 'load_receipt', load)
    with pytest.raises(SystemExit):
        ro.cli(['saved', '--expected-slot', '0000:01:00.0', '--ui-capture-recovery', 'capture',
                '--ui-pstates-trial-resume', 'trial'], Mock())
    with pytest.raises(SystemExit):
        ro.cli(['saved', '--ui-capture-recovery', 'capture'], Mock())
    with pytest.raises(SystemExit):
        ro.cli(['saved', '--expected-slot', 'not-a-pci-slot'], Mock())
    load.assert_not_called()


def test_capture_invalid_directory_stops_before_parent_and_receipt(monkeypatch, tmp_path):
    import pytest

    wait, load, execute, launch = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(ro.devicerecovery, 'wait_for_exit', wait)
    monkeypatch.setattr(ro, 'load_receipt', load)
    monkeypatch.setattr(ro, 'execute', execute)
    monkeypatch.setattr(ro.subprocess, 'Popen', launch)
    existing = tmp_path / 'existing'
    existing.mkdir()
    for path in (existing, tmp_path / 'missing-parent' / 'capture'):
        with pytest.raises(SystemExit):
            ro.cli(['saved', '--parent-pid', '42', '--expected-slot', '0000:01:00.0',
                    '--ui-capture-recovery', str(path)], Mock())
    wait.assert_not_called()
    load.assert_not_called()
    execute.assert_not_called()
    launch.assert_not_called()
