"""Offline paired INFO reads for power relationship-weight qualification."""

import copy
import struct
from unittest.mock import Mock

import pytest

from druta import power_weight_runtime as weight
from tests.test_linked_current_runtime import GPU, LAYOUT
from tests.test_power_weight_runtime import setup_case


@pytest.fixture
def paired_gpu():
    bios, report = setup_case()
    gpu = GPU()
    gpu._current_limit_abi = Mock(return_value=copy.deepcopy(LAYOUT))
    words = list(struct.unpack('<5128I', bytes.fromhex(report['info_hex'])))
    gpu._current_limit_rm = Mock(return_value=words)
    gpu._power_policy_table = Mock(side_effect=AssertionError('CONTROL/STATUS is not needed'))
    return gpu, bios, words


def test_two_matching_info_reads_validate_selected_target(paired_gpu):
    gpu, bios, _ = paired_gpu
    saved = weight.contracts(gpu, bios, {'power_relationship.2.weight': 4097 / 4096})
    assert saved['before']['identity']['slot'] == gpu.slot()
    selected = saved['selected']['power_relationship.2.weight']
    assert selected['before_raw'] == 4096 and selected['after_raw'] == 4097
    assert selected['binding']['target'] == 9
    assert gpu._current_limit_rm.call_args_list == [((0x2080A618,),), ((0x2080A618,),)]
    gpu._power_policy_table.assert_not_called()


def test_transient_first_info_failure_retries_whole_pair(paired_gpu):
    gpu, _, words = paired_gpu
    gpu._current_limit_rm.side_effect = [ValueError('temporary INFO failure'), words, words]
    report, error = weight.read(gpu)
    assert error is None and report['relationships'][0]['weight_raw'] == 4096
    assert gpu._current_limit_rm.call_count == 3
    assert all(call.args == (0x2080A618,) for call in gpu._current_limit_rm.call_args_list)
    gpu._power_policy_table.assert_not_called()


def test_unknown_geometry_keeps_weight_unavailable(paired_gpu):
    gpu, bios, _ = paired_gpu
    bad = copy.deepcopy(LAYOUT)
    bad['sizes'][0x2080A618] -= 4
    gpu._current_limit_abi.return_value = bad
    annotated, error = weight.annotate_fields(gpu, bios)
    row = next(f for f in annotated['fields'] if f['key'] == 'power_relationship.2.weight')
    assert error and row['capability'] == 'read-only'
    with pytest.raises(weight.PowerWeightError):
        weight.contracts(gpu, bios, {'power_relationship.2.weight': 1})


def test_absent_or_mismatched_target_never_qualifies(paired_gpu):
    gpu, bios, words = paired_gpu
    key = 'power_relationship.2.weight'
    missing = words.copy()
    missing[1] &= ~(1 << 9)
    gpu._current_limit_rm.return_value = missing
    annotated, _ = weight.annotate_fields(gpu, bios)
    assert next(f for f in annotated['fields'] if f['key'] == key)['capability'] == 'read-only'
    with pytest.raises(weight.PowerWeightError, match='target metadata'):
        weight.contracts(gpu, bios, {key: 1})
    wrong = bytearray(struct.pack('<5128I', *words))
    wrong[0x41D0 + 2 * 0x24 + 1] = 7
    gpu._current_limit_rm.return_value = list(struct.unpack('<5128I', wrong))
    with pytest.raises(weight.PowerWeightError, match='target metadata'):
        weight.contracts(gpu, bios, {key: 1})


def test_pair_disagreement_and_unstable_info_fail(paired_gpu):
    gpu, _, words = paired_gpu
    gpu.nvml.selected['slot'] = '0000:01:00.0'
    report, error = weight.read(gpu)
    assert report is None and 'different PCI addresses' in error
    gpu._current_limit_rm.assert_not_called()
    gpu.nvml.selected['slot'] = gpu.slot()
    changed = words.copy()
    changed[(0x41D0 + 2 * 0x24 + 4) // 4] ^= 1 << 16
    gpu._current_limit_rm.side_effect = [words, changed, words, changed]
    report, error = weight.read(gpu)
    assert report is None and 'changed between reads' in error
    assert gpu._current_limit_rm.call_count == 4
