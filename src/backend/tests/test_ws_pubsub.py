import asyncio
import json
from unittest.mock import AsyncMock
from app.services.ws_pubsub import subscribe, unsubscribe, get_active_project_ids, _subscribers


def _make_ws():
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


def test_subscribe_and_unsubscribe():
    _subscribers.clear()
    ws = _make_ws()
    subscribe("proj-1", ws)
    assert "proj-1" in get_active_project_ids()
    unsubscribe("proj-1", ws)
    assert "proj-1" not in get_active_project_ids()


def test_subscribe_multiple():
    _subscribers.clear()
    ws1 = _make_ws()
    ws2 = _make_ws()
    subscribe("proj-1", ws1)
    subscribe("proj-1", ws2)
    assert len(_subscribers["proj-1"]) == 2
    unsubscribe("proj-1", ws1)
    assert len(_subscribers["proj-1"]) == 1


def test_notify_project():
    _subscribers.clear()
    loop = asyncio.new_event_loop()

    from app.services.ws_pubsub import set_event_loop, notify_project
    set_event_loop(loop)

    ws = _make_ws()
    subscribe("proj-1", ws)

    msg = {"type": "vm-state", "states": {"vm-1": "running"}}
    notify_project("proj-1", msg)

    # Run the event loop briefly to process the coroutine
    loop.run_until_complete(asyncio.sleep(0.1))
    ws.send_text.assert_called_once()
    sent = json.loads(ws.send_text.call_args[0][0])
    assert sent["type"] == "vm-state"
    assert sent["states"]["vm-1"] == "running"
    loop.close()


def test_notify_dead_connection_removed():
    _subscribers.clear()
    loop = asyncio.new_event_loop()

    from app.services.ws_pubsub import set_event_loop, notify_project
    set_event_loop(loop)

    ws = _make_ws()
    ws.send_text.side_effect = RuntimeError("connection closed")
    subscribe("proj-1", ws)

    notify_project("proj-1", {"type": "test"})
    loop.run_until_complete(asyncio.sleep(0.1))

    assert "proj-1" not in get_active_project_ids()
    loop.close()
