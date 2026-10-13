"""Unit tests for OVN nested-MAC port_security helpers."""

from helpers.ovn_nested_macs import (
    ensure_nested_mac_port_security,
    nad_logical_switch_name,
    resolve_logical_switch,
    virt_launcher_lsp_uuids,
)


def test_nad_logical_switch_name():
    assert (
        nad_logical_switch_name("net-0ed04682-nad")
        == "net.0ed04682.nad_ovn_layer2_switch"
    )


def test_resolve_logical_switch_exact():
    out = (
        "c1771e2e-3676-4a91-8c85-29c617173802 "
        "(net.0ed04682.nad_ovn_layer2_switch)\n"
        "other-uuid (other.switch)\n"
    )
    assert (
        resolve_logical_switch(out, "net-0ed04682-nad")
        == "net.0ed04682.nad_ovn_layer2_switch"
    )


def test_resolve_logical_switch_missing():
    assert resolve_logical_switch("uuid (unrelated_switch)\n", "net-abc-nad") is None


def test_virt_launcher_lsp_uuids():
    out = """
9183eb6a-5ee4-4ac1-948b-2b121ed93f40 (troshka.9aff.net_dnsmasq-pod)
519e3baf-60b3-4932-894b-e7acbb13d775 (troshka.9aff.net_virt-launcher-vm-aaa)
2cca9400-c9d9-4a38-a30a-e203cc302a48 (troshka.9aff.net_virt-launcher-vm-bbb)
"""
    assert virt_launcher_lsp_uuids(out) == [
        "519e3baf-60b3-4932-894b-e7acbb13d775",
        "2cca9400-c9d9-4a38-a30a-e203cc302a48",
    ]


def test_ensure_clears_virt_launcher_lsps():
    calls: list[list[str]] = []

    def exec_fn(args: list[str]) -> str:
        calls.append(args)
        if args == ["ls-list"]:
            return "uuid (net.0ed04682.nad_ovn_layer2_switch)\n"
        if args[:2] == ["lsp-list", "net.0ed04682.nad_ovn_layer2_switch"]:
            return (
                "aaaa (x_virt-launcher-one)\n"
                "bbbb (x_dnsmasq)\n"
                "cccc (x_virt-launcher-two)\n"
            )
        return ""

    # core_api unused when exec_fn provided
    n = ensure_nested_mac_port_security(
        None,  # type: ignore[arg-type]
        nad_name="net-0ed04682-nad",
        namespace="troshka-x",
        exec_fn=exec_fn,
    )
    assert n == 2
    assert ["lsp-set-port-security", "aaaa"] in calls
    assert ["lsp-set-addresses", "aaaa", "unknown"] in calls
    assert ["lsp-set-port-security", "cccc"] in calls
    assert ["lsp-set-port-security", "bbbb"] not in calls


def test_ensure_noop_when_switch_missing():
    def exec_fn(args: list[str]) -> str:
        if args == ["ls-list"]:
            return "uuid (other_switch)\n"
        raise AssertionError(f"unexpected {args}")

    n = ensure_nested_mac_port_security(
        None,  # type: ignore[arg-type]
        nad_name="net-0ed04682-nad",
        namespace="troshka-x",
        exec_fn=exec_fn,
    )
    assert n == 0
