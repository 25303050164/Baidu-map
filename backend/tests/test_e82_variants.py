"""Benchmark arms: names resolve to one loop configuration, and comparisons never mix arms."""
import pytest

from tools.e82_benchmark import pick_arm
from tools.e82_variants import SWITCHES, describe, resolve_arm
from tools.endpoint_refinement_loop import DEFAULT_CONFIG


def test_published_arms_run_the_defaults():
    assert resolve_arm('loop') == ('loop', None)
    assert resolve_arm('legacy') == ('legacy', None)
    assert describe(['loop'])['loop']['loopConfig']['gap_explore'] is False


def test_each_letter_turns_on_exactly_its_switch():
    refinement, config = resolve_arm('E1ca')
    assert refinement == 'loop'
    assert config.gap_explore and config.scan_extend
    on = {name for name in SWITCHES.values() if getattr(config, name)}
    assert on == {'gap_explore', 'scan_extend'}
    assert config.batch_attempts == DEFAULT_CONFIG.batch_attempts


def test_a_suffix_overrides_one_numeric_setting():
    _, config = resolve_arm('E1cfp:prefix_max_seconds=810')
    assert config.route_prefix and config.prefix_max_seconds == 810.0
    _, config = resolve_arm('E1c:batch_attempts=12')
    assert config.batch_attempts == 12 and type(config.batch_attempts) is int


def test_e822_is_a_versioned_refinement_with_its_own_switches():
    from app.algorithms.baidu_e82 import LOOP_CONFIGS, REFINEMENT_VERSIONS
    from app.engines.baidu_e82 import e82_request
    assert resolve_arm('loop2') == ('loop2', None)
    assert e82_request((121.5, 31.3), 400, refinement='loop2').config_version == REFINEMENT_VERSIONS['loop2']
    config = describe(['loop2'])['loop2']['loopConfig']
    assert config == {**config, **dict(gap_explore=True, stall_continue=True, route_prefix=True,
                                       prefix_max_seconds=810, scan_batch=True)}
    assert LOOP_CONFIGS['loop2'].crossing_hints is False and LOOP_CONFIGS['loop2'].ring_prefetch is False


def test_the_request_version_alone_selects_e822_in_compute_e82():
    import asyncio
    from life_circle.models import CancelToken
    from app.algorithms.baidu_e82 import compute_e82
    from app.engines.baidu_e82 import e82_request
    from tools.endpoint_multicross_experiment import RotatedSynthetic
    from tools.endpoint_radial_experiment import ORIGIN
    provider = RotatedSynthetic('narrow')
    result = asyncio.run(compute_e82(e82_request(ORIGIN, 200, refinement='loop2'), provider, CancelToken()))
    loop = result.metadata['evidence']['refinementLoop']
    assert provider.calls <= 200
    assert loop['parameters']['loopConfig']['gap_explore'] is True
    assert result.to_dict()['config']['config_version'] == 'local-multicross-e82.2'


@pytest.mark.parametrize('arm', ['E1', 'E1z', 'E1aa', 'loop3', 'e1a', 'E1c:route_prefix=1',
                                 'E1c:nothing=3', 'E1c:batch_attempts='])
def test_unknown_arms_fail_before_any_run(arm):
    with pytest.raises(ValueError):
        resolve_arm(arm)


def test_a_comparison_names_its_arm_when_a_run_holds_several():
    rows = [dict(arm='loop', case='a'), dict(arm='E1c', case='a')]
    with pytest.raises(SystemExit):
        pick_arm(rows, None, 'baseline')
    assert pick_arm(rows, 'E1c', 'candidate') == [dict(arm='E1c', case='a')]
    with pytest.raises(SystemExit):
        pick_arm(rows, 'E1a', 'candidate')
