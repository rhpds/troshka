import httpx
import pytest
from live_api import LiveClient, poll_ocp


def _client_with(responses):
    """LiveClient whose transport replays queued (status_code, json) tuples."""
    seq = list(responses)

    def handler(request):
        code, payload = seq.pop(0)
        return httpx.Response(code, json=payload)

    c = LiveClient("http://t", token="trk_x")
    c.raw = httpx.Client(
        base_url="http://t",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer trk_x"},
    )
    return c


def test_client_sets_bearer_when_token():
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    c = LiveClient("http://t", token="trk_x")
    c.raw = httpx.Client(
        base_url="http://t",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer trk_x"},
    )
    c.get_json("/api/v1/projects/p1")
    assert captured["auth"] == "Bearer trk_x"


def test_poll_ocp_returns_on_ready():
    c = _client_with(
        [
            (200, {"ocp_status": None}),
            (200, {"ocp_status": "monitoring", "ocp_install_elapsed": 10}),
            (200, {"ocp_status": "ready", "ocp_install_elapsed": 42}),
        ]
    )
    final = poll_ocp(c, "p1", until={"ready", "warning"}, timeout_s=100, interval_s=0)
    assert final["ocp_status"] == "ready"
    assert final["ocp_install_elapsed"] == 42


def test_poll_ocp_raises_on_unexpected_error():
    c = _client_with(
        [(200, {"ocp_status": "monitoring"}), (200, {"ocp_status": "error"})]
    )
    with pytest.raises(AssertionError):
        poll_ocp(c, "p1", until={"ready", "warning"}, timeout_s=100, interval_s=0)


def test_poll_ocp_times_out():
    c = _client_with([(200, {"ocp_status": "monitoring"})] * 50)
    with pytest.raises(TimeoutError):
        poll_ocp(c, "p1", until={"ready"}, timeout_s=0, interval_s=0)
