"""troshka-oc reconnect helpers (imported from scripts/)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_troshka_oc():
    path = Path(__file__).resolve().parents[3] / "scripts" / "troshka_oc.py"
    spec = importlib.util.spec_from_file_location("troshka_oc", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ws_connection_open_states():
    oc = _load_troshka_oc()
    assert oc._ws_connection_open(None) is False
    assert oc._ws_connection_open(SimpleNamespace(state=SimpleNamespace(name="OPEN")))
    assert not oc._ws_connection_open(
        SimpleNamespace(state=SimpleNamespace(name="CLOSED"))
    )
    assert oc._ws_connection_open(SimpleNamespace(open=True))
    assert not oc._ws_connection_open(SimpleNamespace(open=False))
    assert oc._ws_connection_open(SimpleNamespace(closed=False))
    assert not oc._ws_connection_open(SimpleNamespace(closed=True))
