"""Unit tests for OVN nested-MAC port_security helpers."""

import asyncio
import importlib
import threading
from types import SimpleNamespace

import pytest
from helpers import ovn_nested_macs
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
    assert [
        "set",
        "logical_switch_port",
        "aaaa",
        "options:force_fdb_lookup=true",
    ] in calls
    assert ["lsp-set-port-security", "cccc"] in calls
    assert [
        "set",
        "logical_switch_port",
        "cccc",
        "options:force_fdb_lookup=true",
    ] in calls
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


@pytest.mark.parametrize("unavailable", [None, "node-one"])
def test_ensure_repairs_each_node_database(monkeypatch, unavailable):
    """A successful first database must not hide restricted ports elsewhere."""
    pods = [
        SimpleNamespace(
            metadata=SimpleNamespace(name=name),
            status=SimpleNamespace(phase=phase),
            spec=SimpleNamespace(containers=[SimpleNamespace(name=container)]),
        )
        for name, phase, container in [
            ("node-one", "Running", "nbdb"),
            ("node-two", "Running", "nbdb"),
            ("pending", "Pending", "nbdb"),
            ("no-db", "Running", "other"),
        ]
    ]
    api = SimpleNamespace(
        list_namespaced_pod=lambda *a, **kw: SimpleNamespace(items=pods)
    )
    settings = {name: {} for name in ("node-one", "node-two")}

    def exec_nbctl(core_api, pod_name, container, args):
        assert core_api is api
        assert container == "nbdb"
        if pod_name == unavailable:
            raise RuntimeError("database unavailable")
        if args == ["ls-list"]:
            return "switch (net.abc.nad_ovn_layer2_switch)"
        if args[0] == "lsp-list":
            return "port-uuid (troshka-x_virt-launcher-one)"
        settings[pod_name][args[0]] = args[1:]
        return ""

    monkeypatch.setattr(ovn_nested_macs, "_exec_nbctl", exec_nbctl)
    count = ensure_nested_mac_port_security(
        api, nad_name="net-abc-nad", namespace="troshka-x"
    )

    expected = {
        "lsp-set-port-security": ["port-uuid"],
        "lsp-set-addresses": ["port-uuid", "unknown"],
        "set": ["logical_switch_port", "port-uuid", "options:force_fdb_lookup=true"],
    }
    assert settings["node-two"] == expected
    assert settings["node-one"] == ({} if unavailable else expected)
    assert count == (1 if unavailable else 2)


def test_periodic_heal_does_not_block_operator_event_loop(monkeypatch):
    from handlers import network

    monkeypatch.setattr(
        network.kopf.timer, "side_effect", lambda *a, **kw: lambda fn: fn
    )
    importlib.reload(network)
    event_thread = threading.get_ident()
    worker_threads = []

    def heal(*args, **kwargs):
        worker_threads.append(threading.get_ident())

    monkeypatch.setattr(network, "ensure_nested_mac_port_security", heal)
    monkeypatch.setattr(network.client, "CoreV1Api", lambda: object())
    asyncio.run(
        network.network_nested_mac_ovn_timer(
            spec={"allowNestedMacs": True}, name="net-abc", namespace="troshka-x"
        )
    )
    assert len(worker_threads) == 1
    assert worker_threads[0] != event_thread
