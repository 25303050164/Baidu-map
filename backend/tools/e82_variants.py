"""Named E8.2 arms for the offline benchmarks: a refinement plus a loop configuration.

``legacy`` and ``loop`` are the published refinements. An arm ``E1`` followed by
letters turns on those switches of the loop, e.g. ``E1ca`` is gap exploration plus
open-ended scans; everything else keeps its published default. Letters a-e are
coverage switches, f keeps going after a batch that only hit the cache, p and x
use the returned routes (prefix evidence, 900 s crossing hints), s and r are
throughput switches (batched scans, ring prefetch).
"""
from dataclasses import asdict, replace

from tools.endpoint_refinement_loop import DEFAULT_CONFIG

SWITCHES = dict(a='scan_extend', b='densify_in_patch', c='gap_explore', d='interior_adaptive',
                e='explore_beyond_reserve', f='stall_continue', p='route_prefix', x='crossing_hints',
                s='scan_batch', r='ring_prefetch')


def resolve_arm(arm):
    """(refinement, loop configuration or None for the published defaults).

    ``:name=value`` suffixes override numeric settings, e.g. ``E1cfp:prefix_max_seconds=810``.
    """
    name, *overrides = arm.split(':')
    if name in ('legacy', 'loop', 'loop2') and not overrides:
        # loop2 (E8.2.2) carries its own switches through its config_version.
        return name, None
    letters = name[2:]
    if not (name.startswith('E1') and letters and set(letters) <= set(SWITCHES) and len(set(letters)) == len(letters)):
        raise ValueError(f'Unknown arm {arm!r}')
    config = replace(DEFAULT_CONFIG, **{SWITCHES[letter]: True for letter in letters})
    for override in overrides:
        key, _, value = override.partition('=')
        current = getattr(config, key, None)
        if type(current) not in (int, float) or not value:
            raise ValueError(f'Unknown numeric setting in arm {arm!r}')
        config = replace(config, **{key: type(current)(float(value))})
    return 'loop', config


def describe(arms):
    """What each arm ran with, for a benchmark's protocol."""
    from app.algorithms.baidu_e82 import LOOP_CONFIGS
    described = {}
    for arm in arms:
        refinement, config = resolve_arm(arm)
        config = config or LOOP_CONFIGS.get(refinement, DEFAULT_CONFIG)
        described[arm] = dict(refinement=refinement, loopConfig=asdict(config))
    return described
