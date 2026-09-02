from unittest.mock import patch

from live_config import LiveConfig
from live_scopedkey import client_for_key, scoped_key_from_pod


def test_client_for_key_sets_bearer():
    c = client_for_key("http://t", "trk_scoped")
    assert c.raw.headers.get("authorization") == "Bearer trk_scoped"


def test_scoped_key_from_pod_troshkad_uses_host_podman():
    cfg = LiveConfig.from_env(
        {"TROSHKA_LIVE_URL": "u", "TROSHKA_LIVE_TROSHKAD_HOST": "a1b2"}
    )
    with patch("live_scopedkey.host_podman", return_value="trk_frompod\n") as hs:
        key = scoped_key_from_pod(cfg, "abcd1234-xxxx", provider="troshkad")
    assert key == "trk_frompod"
    assert hs.call_args[0][0] == "a1b2"  # prefix
    assert "exec" in hs.call_args[0]
    assert "troshka-abcd1234-ops" in hs.call_args[0]  # container name


def test_scoped_key_from_pod_kubevirt_uses_oc_exec():
    cfg = LiveConfig.from_env(
        {
            "TROSHKA_LIVE_URL": "u",
            "TROSHKA_LIVE_KUBECONFIG": "/tmp/kc",
            "TROSHKA_LIVE_KUBEVIRT_HOST": "c3d4",
        }
    )
    with patch("live_scopedkey.oc", return_value="trk_kv\n") as oc_mock:
        key = scoped_key_from_pod(cfg, "abcd1234-xxxx", provider="kubevirt")
    assert key == "trk_kv"
    args = oc_mock.call_args
    assert args.kwargs["kubeconfig"] == "/tmp/kc"
    flat = " ".join(args[0])
    assert "exec" in flat and "troshka-abcd1234-ops" in flat
    assert "-n" in args[0] and "troshka-abcd1234" in args[0]  # namespace
