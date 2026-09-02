from live_config import LiveConfig


def test_unconfigured_when_no_url():
    cfg = LiveConfig.from_env({})
    assert cfg.configured is False
    assert cfg.troshkad_ready is False
    assert cfg.kubevirt_ready is False
    assert cfg.tier2_enabled is False
    assert cfg.timeout_s == 4200


def test_full_env_parsed():
    cfg = LiveConfig.from_env(
        {
            "TROSHKA_LIVE_URL": "http://localhost:8200",
            "TROSHKA_LIVE_TOKEN": "trk_abc",
            "TROSHKA_LIVE_TROSHKAD_HOST": "a1b2",
            "TROSHKA_LIVE_KUBECONFIG": "/tmp/kc",
            "TROSHKA_LIVE_KUBEVIRT_HOST": "c3d4",
            "TROSHKA_LIVE_TIER2": "1",
            "TROSHKA_LIVE_TIMEOUT_S": "600",
        }
    )
    assert cfg.configured
    assert cfg.token == "trk_abc"
    assert cfg.troshkad_ready
    assert cfg.kubevirt_ready
    assert cfg.tier2_enabled
    assert cfg.timeout_s == 600


def test_kubevirt_needs_both_kubeconfig_and_host():
    cfg = LiveConfig.from_env(
        {"TROSHKA_LIVE_URL": "u", "TROSHKA_LIVE_KUBECONFIG": "/tmp/kc"}
    )
    assert cfg.kubevirt_ready is False  # host missing


def test_empty_strings_treated_as_unset():
    cfg = LiveConfig.from_env({"TROSHKA_LIVE_URL": ""})
    assert cfg.configured is False
