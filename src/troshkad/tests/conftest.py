import sys
import os
import time as _time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _cap_test_sleeps():
    """Unit tests must never incur real multi-second delays. troshkad's retry and
    poll loops call time.sleep with real second-scale intervals; cap every sleep
    to a tiny value so those loops finish fast while still yielding the GIL for any
    background threads. No test asserts on sleep timing. Tests that patch
    time.sleep themselves override this for their own duration.
    """
    real_sleep = _time.sleep

    def _capped(duration=0, *args, **kwargs):
        if isinstance(duration, (int, float)):
            return real_sleep(min(duration, 0.005))
        return real_sleep(0)

    _time.sleep = _capped
    try:
        yield
    finally:
        _time.sleep = real_sleep
