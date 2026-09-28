import gc
import json
import threading
import time

import pytest

from app import bulk_load


def document():
    return json.dumps({"features": [{"id": i, "properties": {"name": f"n{i}", "tags": [i, None, True]}}
                                    for i in range(300_000)]})


def test_same_result_as_json():
    text = document()
    assert bulk_load.loads(text) == json.loads(text)


def test_other_threads_run_while_a_large_document_is_parsed():
    text = document()
    done, gaps = threading.Event(), []

    def parse():
        bulk_load.loads(text)
        done.set()

    worker = threading.Thread(target=parse)
    started = last = time.perf_counter()
    worker.start()
    while not done.is_set():
        time.sleep(0.001)
        now = time.perf_counter()
        gaps.append(now - last)
        last = now
    worker.join()
    elapsed = time.perf_counter() - started
    # With plain ``json.loads`` this thread waits out the whole parse in one gap.
    assert len(gaps) > 10
    assert max(gaps) < elapsed / 3


def test_nothing_built_to_last_sets_off_a_full_collection():
    building, full = [], []

    def watch(phase, info):
        if phase == "start" and info["generation"] == 2 and building:
            full.append(info)

    threshold = gc.get_threshold()
    gc.callbacks.append(watch)
    gc.freeze()
    gc.collect()
    gc.set_threshold(10, 1, 1)
    try:
        building.append(True)
        kept = [[i] for i in range(20_000)]
        # The same allocations anywhere else do.
        assert full
        building.clear()
        del kept
        full.clear()
        # Its own full collection before, while the heap is small, aside.
        with bulk_load.long_lived():
            building.append(True)
            with bulk_load.long_lived():
                first = [[i] for i in range(20_000)]
            # One build is over, the other still going.
            assert gc.get_threshold()[2] > 1
            second = [[i] for i in range(20_000)]
            building.clear()
        assert full == []
        assert gc.get_threshold() == (10, 1, 1)
        assert gc.get_freeze_count() >= len(first) + len(second)
    finally:
        gc.set_threshold(*threshold)
        gc.callbacks.remove(watch)


def test_a_failed_build_restores_the_thresholds_and_freezes_nothing():
    # A collection itself leaves a few hundred objects (the immortal ones) frozen.
    gc.collect()
    threshold, frozen = gc.get_threshold(), gc.get_freeze_count()
    with pytest.raises(ValueError):
        with bulk_load.long_lived():
            raise ValueError
    assert gc.get_threshold() == threshold
    assert gc.get_freeze_count() == frozen
