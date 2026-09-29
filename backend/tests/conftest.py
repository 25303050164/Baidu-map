import gc

import pytest


@pytest.fixture(autouse=True)
def _thaw_the_heap():
    """Loading the offline graph freezes the heap: in service it lives as long as the
    process. Tests load many small graphs; thawed after each, whatever a test leaves
    behind is still collected."""
    yield
    gc.unfreeze()
