def test_normalize_legacy_mapping_wraps_to_list():
    from app.services.template_loader import normalize_ocp_section

    legacy = {
        "cluster_name": "ocp",
        "base_domain": "ocp.local",
        "api_vip": "10.0.0.10",
        "ingress_vip": "10.0.0.11",
    }
    out = normalize_ocp_section(legacy)
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["name"] == "ocp"
    assert out[0]["base_domain"] == "ocp.local"
    assert out[0]["api_vip"] == "10.0.0.10"
    assert out[0]["ingress_vip"] == "10.0.0.11"


def test_normalize_new_list_passthrough_and_name_default():
    from app.services.template_loader import normalize_ocp_section

    out = normalize_ocp_section(
        [
            {"name": "prod", "type": "standard", "workers": 2},
            {"type": "sno", "base_domain": "dev.local"},
        ]
    )
    assert [c["name"] for c in out] == ["prod", "ocp"]  # 2nd defaults name
    assert out[0]["type"] == "standard" and out[0]["workers"] == 2


def test_normalize_none_returns_empty():
    from app.services.template_loader import normalize_ocp_section

    assert normalize_ocp_section(None) == []
    assert normalize_ocp_section({}) == []
