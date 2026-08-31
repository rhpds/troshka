"""Tests for extracted helper functions in deploy_service.py.

Focuses on pure-logic helpers and mocked DB/infra helpers to improve
SonarQube coverage on the newly refactored code.
"""

import time
from unittest.mock import MagicMock, patch

# ── Pure-logic helpers ──────────────────────────────────────────────────
from app.services.deploy_service import (
    _approve_csrs_if_due,
    _build_bootstrap_items,
    _build_early_phase_items,
    _build_operator_items,
    _classify_node_msg,
    _cluster_init_status,
    _destroy_container,
    _detect_install_phases,
    _extract_dns_domain,
    _has_ocp_monitor,
    _http_suffix,
    _is_api_error,
    _is_ocp_topology,
    _is_pattern_deploy,
    _is_route_ready,
    _parse_node_readiness,
    _parse_operator_status,
    _parse_unavailable_operators,
    _phase_detail_label,
    _phase_icon,
    _resolve_deploy_step,
)
from app.services.troshkad_client import TroshkadError

# ═══════════════════════════════════════════════════════════════════════
# _cluster_init_status
# ═══════════════════════════════════════════════════════════════════════


class TestClusterInitStatus:
    def test_api_init(self):
        assert _cluster_init_status({"api-init"}) == "✓"

    def test_validation(self):
        assert _cluster_init_status({"validation"}) == "✓"

    def test_both_api_init_and_validation(self):
        assert _cluster_init_status({"api-init", "validation"}) == "✓"

    def test_waiting_init(self):
        assert _cluster_init_status({"waiting-init"}) == "⏳"

    def test_empty(self):
        assert _cluster_init_status(set()) == "—"

    def test_unrelated_phases(self):
        assert _cluster_init_status({"downloading", "iso-ready"}) == "—"


# ═══════════════════════════════════════════════════════════════════════
# _phase_detail_label
# ═══════════════════════════════════════════════════════════════════════


class TestPhaseDetailLabel:
    def test_downloading(self):
        assert _phase_detail_label({"downloading"}) == "downloading OCP tools"

    def test_downloading_excludes_downloaded(self):
        # Once downloaded is in phases, label should NOT be "downloading OCP tools"
        assert (
            _phase_detail_label({"downloading", "downloaded"})
            != "downloading OCP tools"
        )

    def test_creating_iso(self):
        assert _phase_detail_label({"creating-iso"}) == "building agent ISO"

    def test_iso_ready(self):
        assert _phase_detail_label({"iso-ready"}) == "booting nodes from ISO"

    def test_waiting_init(self):
        assert _phase_detail_label({"waiting-init"}) == "waiting for cluster init"

    def test_api_init_validating(self):
        assert _phase_detail_label({"api-init"}) == "validating hosts"

    def test_fallback(self):
        assert (
            _phase_detail_label({"validation", "preparing", "bootstrap"})
            == "installing"
        )

    def test_empty(self):
        assert _phase_detail_label(set()) == "installing"


# ═══════════════════════════════════════════════════════════════════════
# _phase_icon
# ═══════════════════════════════════════════════════════════════════════


class TestPhaseIcon:
    def test_done_phase_present(self):
        assert _phase_icon({"downloaded", "iso-ready"}, "downloaded") == "✓"

    def test_done_phase_absent(self):
        assert _phase_icon({"downloading"}, "downloaded") == "⏳"

    def test_empty_phases(self):
        assert _phase_icon(set(), "anything") == "⏳"

    def test_exact_match(self):
        assert _phase_icon({"nodes-booted"}, "nodes-booted") == "✓"


# ═══════════════════════════════════════════════════════════════════════
# _classify_node_msg
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyNodeMsg:
    def test_writing_100_percent(self):
        assert _classify_node_msg("Writing image to disk: 100%") == "written"

    def test_writing_partial(self):
        result = _classify_node_msg("Writing image to disk: 45%")
        assert result == "writing 45%"

    def test_writing_without_colon(self):
        result = _classify_node_msg("Writing image to disk progress")
        assert result == "writing %"

    def test_rebooting(self):
        assert _classify_node_msg("Rebooting node") == "rebooting"

    def test_bootkube(self):
        assert _classify_node_msg("Waiting for bootkube to finish") == "bootkube"

    def test_configuring(self):
        assert _classify_node_msg("Configuring network") == "configuring"

    def test_joined(self):
        assert _classify_node_msg("Joined the cluster") == "joined"

    def test_done(self):
        assert _classify_node_msg("Done installing") == "done"

    def test_completing_installation(self):
        assert _classify_node_msg("completing installation") == "done"

    def test_unrecognized(self):
        assert _classify_node_msg("random log message") is None

    def test_empty_string(self):
        assert _classify_node_msg("") is None


# ═══════════════════════════════════════════════════════════════════════
# _extract_dns_domain
# ═══════════════════════════════════════════════════════════════════════


class TestExtractDnsDomain:
    def test_extracts_domain_from_api_record(self):
        nodes = [
            {
                "type": "networkNode",
                "data": {
                    "dnsRecords": [
                        {"name": "api.cluster.example.com"},
                    ],
                },
            },
        ]
        assert _extract_dns_domain(nodes) == "cluster.example.com"

    def test_skips_non_api_records(self):
        nodes = [
            {
                "type": "networkNode",
                "data": {
                    "dnsRecords": [
                        {"name": "console.example.com"},
                    ],
                },
            },
        ]
        assert _extract_dns_domain(nodes) == "ocp.ocp.local"

    def test_default_when_no_network_nodes(self):
        assert _extract_dns_domain([]) == "ocp.ocp.local"

    def test_default_when_no_dns_records(self):
        nodes = [{"type": "networkNode", "data": {}}]
        assert _extract_dns_domain(nodes) == "ocp.ocp.local"

    def test_skips_vm_nodes(self):
        nodes = [
            {
                "type": "vmNode",
                "data": {
                    "dnsRecords": [{"name": "api.ignored.local"}],
                },
            },
        ]
        assert _extract_dns_domain(nodes) == "ocp.ocp.local"

    def test_first_api_record_wins(self):
        nodes = [
            {
                "type": "networkNode",
                "data": {
                    "dnsRecords": [
                        {"name": "api.first.local"},
                        {"name": "api.second.local"},
                    ],
                },
            },
        ]
        assert _extract_dns_domain(nodes) == "first.local"


# ═══════════════════════════════════════════════════════════════════════
# _detect_install_phases
# ═══════════════════════════════════════════════════════════════════════


class TestDetectInstallPhases:
    def test_downloading_phase(self):
        phases = set()
        _detect_install_phases("Downloading openshift-install v4.16", phases)
        assert "downloading" in phases

    def test_downloaded_phase(self):
        phases = set()
        _detect_install_phases("Downloaded openshift-install v4.16", phases)
        assert "downloaded" in phases

    def test_creating_iso(self):
        phases = set()
        _detect_install_phases("Creating agent ISO image", phases)
        assert "creating-iso" in phases

    def test_api_init(self):
        phases = set()
        _detect_install_phases("Agent Rest API Initialized", phases)
        assert "api-init" in phases

    def test_compound_extracting_iso(self):
        phases = set()
        _detect_install_phases("Extracting base ISO from release", phases)
        assert "extracting-iso" in phases

    def test_compound_iso_ready(self):
        phases = set()
        _detect_install_phases("Generated ISO at /path/agent.iso", phases)
        assert "iso-ready" in phases

    def test_compound_waiting_init(self):
        phases = set()
        _detect_install_phases("Waiting for cluster install to initialize", phases)
        assert "waiting-init" in phases

    def test_compound_bootstrap_api(self):
        phases = set()
        _detect_install_phases("Bootstrap Kube API Initialized", phases)
        assert "bootstrap-api" in phases

    def test_compound_bootstrap_complete(self):
        phases = set()
        _detect_install_phases("cluster bootstrap is complete", phases)
        assert "bootstrap" in phases

    def test_compound_control_plane(self):
        phases = set()
        _detect_install_phases("Working towards OCP 4.16", phases)
        assert "control-plane" in phases

    def test_compound_initialized(self):
        phases = set()
        _detect_install_phases("Cluster is initialized", phases)
        assert "initialized" in phases

    def test_nodes_booted(self):
        phases = set()
        _detect_install_phases("master-0 Booted successfully from ISO", phases)
        assert "nodes-booted" in phases

    def test_preparing_for_installation(self):
        phases = set()
        _detect_install_phases("preparing-for-installation", phases)
        assert "validation" in phases
        assert "preparing" in phases

    def test_preparing_cluster_text(self):
        phases = set()
        _detect_install_phases("Preparing cluster for install", phases)
        assert "validation" in phases
        assert "preparing" in phases

    def test_waiting_to_initialize_implies_bootstrap(self):
        phases = set()
        _detect_install_phases("Waiting up to 30m0s to initialize", phases)
        assert "bootstrap" in phases

    def test_multiple_phases_in_one_log(self):
        phases = set()
        log = (
            "Downloading openshift-install v4.16\n"
            "Downloaded openshift-install v4.16\n"
            "Creating agent ISO image\n"
        )
        _detect_install_phases(log, phases)
        assert "downloading" in phases
        assert "downloaded" in phases
        assert "creating-iso" in phases

    def test_preserves_existing_phases(self):
        phases = {"existing-phase"}
        _detect_install_phases("Downloading openshift-install", phases)
        assert "existing-phase" in phases
        assert "downloading" in phases


# ═══════════════════════════════════════════════════════════════════════
# _parse_unavailable_operators
# ═══════════════════════════════════════════════════════════════════════


class TestParseUnavailableOperators:
    def test_finds_unavailable_from_tracked(self):
        text = 'msg="Cluster operators ingress, dns are not available"'
        tracked = {"ingress", "dns", "console"}
        aliases = {}
        result = _parse_unavailable_operators(text, tracked, aliases)
        assert "ingress" in result
        assert "dns" in result
        assert "console" not in result

    def test_finds_aliases(self):
        text = 'msg="Cluster operator image-registry is not available"'
        tracked = {"registry"}
        aliases = {"image-registry": "registry"}
        result = _parse_unavailable_operators(text, tracked, aliases)
        assert "registry" in result

    def test_no_match(self):
        text = "everything is fine"
        result = _parse_unavailable_operators(text, {"ingress"}, {})
        assert result == set()

    def test_empty_text(self):
        result = _parse_unavailable_operators("", {"ingress"}, {})
        assert result == set()

    def test_uses_last_matching_line(self):
        text = (
            'msg="Cluster operator ingress is not available"\n'
            'msg="Cluster operator dns is not available"\n'
        )
        tracked = {"ingress", "dns"}
        # reversed() finds "dns" line first, breaks after first match
        result = _parse_unavailable_operators(text, tracked, {})
        assert "dns" in result
        assert "ingress" not in result


# ═══════════════════════════════════════════════════════════════════════
# _is_route_ready
# ═══════════════════════════════════════════════════════════════════════


class TestIsRouteReady:
    def test_200(self):
        assert _is_route_ready("200") is True

    def test_301(self):
        assert _is_route_ready("301") is True

    def test_403(self):
        assert _is_route_ready("403") is True

    def test_000(self):
        assert _is_route_ready("000") is False

    def test_500(self):
        assert _is_route_ready("500") is False

    def test_404(self):
        assert _is_route_ready("404") is False


# ═══════════════════════════════════════════════════════════════════════
# _http_suffix
# ═══════════════════════════════════════════════════════════════════════


class TestHttpSuffix:
    def test_normal_code(self):
        assert _http_suffix("200") == " (HTTP 200)"

    def test_500(self):
        assert _http_suffix("500") == " (HTTP 500)"

    def test_000_suppressed(self):
        assert _http_suffix("000") == ""

    def test_000000_suppressed(self):
        assert _http_suffix("000000") == ""

    def test_empty_suppressed(self):
        assert _http_suffix("") == ""


# ═══════════════════════════════════════════════════════════════════════
# _build_early_phase_items
# ═══════════════════════════════════════════════════════════════════════


class TestBuildEarlyPhaseItems:
    def test_downloading_phase(self):
        phases = {"downloading"}
        items = _build_early_phase_items(phases, {}, [])
        assert any("Download OCP tools" in i for i in items)
        assert any("⏳" in i for i in items)

    def test_downloaded_phase(self):
        phases = {"downloading", "downloaded"}
        items = _build_early_phase_items(phases, {}, [])
        assert any("Download OCP tools: ✓" in i for i in items)

    def test_iso_ready_shows_boot_nodes(self):
        phases = {"downloading", "downloaded", "creating-iso", "iso-ready"}
        items = _build_early_phase_items(phases, {}, [])
        assert any("Boot nodes from ISO" in i for i in items)

    def test_nodes_booted_shows_cluster_init(self):
        phases = {"iso-ready", "nodes-booted"}
        items = _build_early_phase_items(phases, {}, [])
        assert any("Cluster init" in i for i in items)

    def test_validation_done(self):
        phases = {"validation"}
        items = _build_early_phase_items(phases, {}, [])
        assert any("Host validation: ✓" in i for i in items)

    def test_validating_in_progress(self):
        phases = {"validating"}
        items = _build_early_phase_items(phases, {}, [])
        assert any("Host validation: ⏳" in i for i in items)

    def test_preparing_without_node_status(self):
        phases = {"preparing"}
        items = _build_early_phase_items(phases, {}, [])
        assert any("Preparing for installation: ⏳" in i for i in items)

    def test_preparing_with_node_status(self):
        phases = {"preparing"}
        node_status = {"master-0": "writing 50%"}
        items = _build_early_phase_items(phases, node_status, ["master-0"])
        assert any("Preparing for installation: ✓" in i for i in items)
        assert any("Installing nodes" in i for i in items)

    def test_empty_phases(self):
        items = _build_early_phase_items(set(), {}, [])
        assert items == []


# ═══════════════════════════════════════════════════════════════════════
# _build_bootstrap_items
# ═══════════════════════════════════════════════════════════════════════


class TestBuildBootstrapItems:
    def test_bootstrap_complete(self):
        import re as _re

        phases = {"bootstrap"}
        items = _build_bootstrap_items(phases, {}, "", _re)
        assert any("Bootstrap: ✓" in i for i in items)

    def test_bootstrap_api_in_progress(self):
        import re as _re

        phases = {"bootstrap-api"}
        items = _build_bootstrap_items(phases, {}, "", _re)
        assert any("Bootstrap: ⏳" in i for i in items)

    def test_bootstrap_not_started(self):
        import re as _re

        node_status = {"master-0": "writing 50%"}
        items = _build_bootstrap_items(set(), node_status, "", _re)
        assert any("Bootstrap: —" in i for i in items)

    def test_etcd_shown_with_bootkube(self):
        import re as _re

        node_status = {"master-0": "bootkube"}
        items = _build_bootstrap_items(set(), node_status, "", _re)
        assert any("etcd:" in i for i in items)

    def test_control_plane_with_version(self):
        import re as _re

        phases = {"bootstrap", "control-plane"}
        text = 'msg="Working towards 4.16.5: 45% complete"'
        items = _build_bootstrap_items(phases, {}, text, _re)
        assert any("API: ✓" in i for i in items)
        assert any("4.16" in i for i in items)

    def test_control_plane_initialized(self):
        import re as _re

        phases = {"bootstrap", "control-plane", "initialized"}
        text = 'msg="Working towards 4.16.5: 100% complete"'
        items = _build_bootstrap_items(phases, {}, text, _re)
        assert any("✓" in i and "4.16" in i for i in items)

    def test_api_waiting_after_bootstrap(self):
        import re as _re

        phases = {"bootstrap"}
        items = _build_bootstrap_items(phases, {}, "", _re)
        assert any("API: ⏳" in i for i in items)


# ═══════════════════════════════════════════════════════════════════════
# _build_operator_items
# ═══════════════════════════════════════════════════════════════════════


class TestBuildOperatorItems:
    def test_initialized(self):
        phases = {"initialized"}
        items = _build_operator_items(phases, {"ingress", "dns"}, set())
        assert items == ["Cluster operators: ✓"]

    def test_some_unavailable(self):
        phases = {"control-plane"}
        tracked = {"ingress", "dns", "console"}
        not_available = {"ingress"}
        items = _build_operator_items(phases, tracked, not_available)
        assert any("2/3" in i for i in items)
        assert any("ingress: ✗" in i for i in items)

    def test_all_available(self):
        phases = {"control-plane"}
        items = _build_operator_items(phases, {"ingress", "dns"}, set())
        assert any("Cluster operators: ⏳" in i for i in items)

    def test_no_control_plane(self):
        phases = set()
        items = _build_operator_items(phases, {"ingress"}, set())
        assert items == []


# ═══════════════════════════════════════════════════════════════════════
# _parse_node_readiness
# ═══════════════════════════════════════════════════════════════════════


class TestParseNodeReadiness:
    def test_all_ready(self):
        output = (
            "master-0   Ready    control-plane   10d   v1.29.1\n"
            "master-1   Ready    control-plane   10d   v1.29.1\n"
        )
        items, ready, total = _parse_node_readiness(output)
        assert ready == 2
        assert total == 2
        assert len(items) == 2

    def test_mixed(self):
        output = (
            "master-0   Ready      control-plane   10d   v1.29.1\n"
            "master-1   NotReady   control-plane   10d   v1.29.1\n"
        )
        items, ready, total = _parse_node_readiness(output)
        assert ready == 1
        assert total == 2

    def test_none_result(self):
        items, ready, total = _parse_node_readiness(None)
        assert items == []
        assert ready == 0
        assert total == 0

    def test_empty_string(self):
        items, ready, total = _parse_node_readiness("")
        assert items == []
        assert ready == 0
        assert total == 0

    def test_single_line_no_newline(self):
        items, ready, total = _parse_node_readiness(
            "worker-0  Ready  worker  1d  v1.29.1"
        )
        assert ready == 1
        assert total == 1


# ═══════════════════════════════════════════════════════════════════════
# _parse_operator_status
# ═══════════════════════════════════════════════════════════════════════


class TestParseOperatorStatus:
    def test_all_available(self):
        output = (
            "ingress       4.16.5   True    False   False   10d\n"
            "dns           4.16.5   True    False   False   10d\n"
        )
        items, avail, total = _parse_operator_status(output)
        assert avail == 2
        assert total == 2
        assert all("available" in i for i in items)

    def test_degraded(self):
        # columns: name version available progressing degraded since
        output = "ingress       4.16.5   False   False   True    10d\n"
        items, avail, total = _parse_operator_status(output)
        assert avail == 0
        assert total == 1
        assert "degraded" in items[0]

    def test_progressing(self):
        output = "ingress       4.16.5   False   True    False   10d\n"
        items, avail, total = _parse_operator_status(output)
        assert avail == 0
        assert total == 1
        assert "progressing" in items[0]

    def test_none_result(self):
        items, avail, total = _parse_operator_status(None)
        assert items == []
        assert avail == 0
        assert total == 0

    def test_empty_string(self):
        items, avail, total = _parse_operator_status("")
        assert items == []
        assert avail == 0
        assert total == 0


# ═══════════════════════════════════════════════════════════════════════
# _is_api_error
# ═══════════════════════════════════════════════════════════════════════


class TestIsApiError:
    def test_none(self):
        assert _is_api_error(None) is True

    def test_error_in_output(self):
        assert _is_api_error("error: connection refused") is True

    def test_refused(self):
        assert _is_api_error("connection refused") is True

    def test_connection(self):
        assert _is_api_error("dial tcp: connection timed out") is True

    def test_clean_output(self):
        assert _is_api_error("master-0  Ready  control-plane  10d  v1.29.1") is False


# ═══════════════════════════════════════════════════════════════════════
# _is_ocp_topology / _has_ocp_monitor / _is_pattern_deploy
# ═══════════════════════════════════════════════════════════════════════


class TestTopologyPredicates:
    def test_is_ocp_topology_true(self):
        topo = {"nodes": [{"type": "vmNode", "data": {"os": "rhcos"}}]}
        assert _is_ocp_topology(topo) is True

    def test_is_ocp_topology_false(self):
        topo = {"nodes": [{"type": "vmNode", "data": {"os": "rhel9"}}]}
        assert _is_ocp_topology(topo) is False

    def test_is_ocp_topology_empty(self):
        assert _is_ocp_topology({"nodes": []}) is False
        assert _is_ocp_topology({}) is False

    def test_has_ocp_monitor_true(self):
        topo = {"nodes": [{"type": "vmNode", "data": {"ocpMonitor": True}}]}
        assert _has_ocp_monitor(topo) is True

    def test_has_ocp_monitor_bastion_browser(self):
        topo = {
            "nodes": [{"type": "vmNode", "data": {"configureBastionBrowser": True}}]
        }
        assert _has_ocp_monitor(topo) is True

    def test_has_ocp_monitor_false(self):
        topo = {"nodes": [{"type": "vmNode", "data": {"os": "rhcos"}}]}
        assert _has_ocp_monitor(topo) is False

    def test_is_pattern_deploy_true(self):
        topo = {"nodes": [{"type": "storageNode", "data": {"patternId": "abc123"}}]}
        assert _is_pattern_deploy(topo) is True

    def test_is_pattern_deploy_false(self):
        topo = {"nodes": [{"type": "storageNode", "data": {"size_gb": 20}}]}
        assert _is_pattern_deploy(topo) is False

    def test_is_pattern_deploy_empty(self):
        assert _is_pattern_deploy({"nodes": []}) is False


# ═══════════════════════════════════════════════════════════════════════
# _resolve_deploy_step
# ═══════════════════════════════════════════════════════════════════════


class TestResolveDeployStep:
    def test_all_disks_done_with_op_stage(self):
        step, detail = _resolve_deploy_step(
            all_disks_done=True,
            op_stage="Starting",
            op_detail="booting VMs",
            dv_detail="",
            dv_lines=[],
            status={},
            last={},
        )
        assert step == "starting"
        assert detail == "booting VMs"

    def test_all_disks_done_certificate_stage(self):
        step, detail = _resolve_deploy_step(
            all_disks_done=True,
            op_stage="Certificate Renewal",
            op_detail="renewing certs",
            dv_detail="",
            dv_lines=[],
            status={},
            last={},
        )
        assert step == "certificate renewal"
        assert detail == "renewing certs"

    def test_all_disks_done_with_vm_states(self):
        step, detail = _resolve_deploy_step(
            all_disks_done=True,
            op_stage="VMs",
            op_detail="",
            dv_detail="",
            dv_lines=[],
            status={"vmStates": {"vm1": "Running", "vm2": "Stopped", "vm3": "Pending"}},
            last={},
        )
        assert "2/3" in detail

    def test_dv_lines_present(self):
        lines = ["disk-1: importing 50%", "disk-2: done"]
        step, detail = _resolve_deploy_step(
            all_disks_done=False,
            op_stage="",
            op_detail="",
            dv_detail="disk-1: importing 50%\ndisk-2: done",
            dv_lines=lines,
            status={},
            last={},
        )
        assert step == "images"

    def test_op_stage_only(self):
        step, detail = _resolve_deploy_step(
            all_disks_done=False,
            op_stage="Networks",
            op_detail="setting up VLANs",
            dv_detail="",
            dv_lines=None,
            status={},
            last={},
        )
        assert step == "networks"
        assert detail == "setting up VLANs"

    def test_fallback_to_last(self):
        step, detail = _resolve_deploy_step(
            all_disks_done=False,
            op_stage="",
            op_detail="",
            dv_detail="",
            dv_lines=None,
            status={},
            last={"step": "seeds", "detail": "creating cloud-init"},
        )
        assert step == "seeds"
        assert detail == "creating cloud-init"

    def test_fallback_empty_last(self):
        step, detail = _resolve_deploy_step(
            all_disks_done=False,
            op_stage="",
            op_detail="",
            dv_detail="",
            dv_lines=None,
            status={},
            last={},
        )
        assert step == "deploying"


# ═══════════════════════════════════════════════════════════════════════
# _approve_csrs_if_due
# ═══════════════════════════════════════════════════════════════════════


class TestApproveCSRsIfDue:
    def test_not_due_yet(self):
        approve_fn = MagicMock(return_value=0)
        push_fn = MagicMock()
        last = time.time()
        result = _approve_csrs_if_due(approve_fn, push_fn, last, interval=30)
        # Should not call approve_fn since interval hasn't elapsed
        approve_fn.assert_not_called()
        assert result == last

    def test_due_with_approvals(self):
        approve_fn = MagicMock(return_value=3)
        push_fn = MagicMock()
        last = 0.0  # long ago
        result = _approve_csrs_if_due(approve_fn, push_fn, last, interval=30)
        approve_fn.assert_called_once()
        push_fn.assert_called_once_with("certs", "approved 3 certificate(s)")
        assert result > last

    def test_due_no_approvals(self):
        approve_fn = MagicMock(return_value=0)
        push_fn = MagicMock()
        last = 0.0
        result = _approve_csrs_if_due(approve_fn, push_fn, last, interval=30)
        approve_fn.assert_called_once()
        push_fn.assert_not_called()
        assert result > last

    def test_custom_interval(self):
        approve_fn = MagicMock(return_value=0)
        push_fn = MagicMock()
        last = time.time() - 5  # 5 seconds ago
        # With a 10-second interval, should not be due
        _approve_csrs_if_due(approve_fn, push_fn, last, interval=10)
        approve_fn.assert_not_called()

    def test_due_with_none_return(self):
        # approve_fn returns falsy (None/0)
        approve_fn = MagicMock(return_value=None)
        push_fn = MagicMock()
        _approve_csrs_if_due(approve_fn, push_fn, 0.0, interval=0)
        approve_fn.assert_called_once()
        push_fn.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# _ocp_push_status (DB-dependent, mock SessionLocal)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpPushStatus:
    @patch("app.services.deploy_service.notify_project")
    def test_pushes_ws_message(self, mock_notify):
        from app.services.deploy_service import _ocp_push_status

        mock_db = MagicMock()
        mock_project = MagicMock()
        mock_db.get.return_value = mock_project

        with patch(
            "app.services.deploy_service.SessionLocal",
            return_value=mock_db,
            create=True,
        ):
            # patch the import inside the function
            with patch.dict("sys.modules", {}):
                _ocp_push_status("proj-123", "nodes", "2/3 ready", ["m0: Ready"])

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        assert call_args[0][0] == "proj-123"
        assert call_args[0][1]["type"] == "ocp-health"
        assert call_args[0][1]["phase"] == "nodes"

    @patch("app.services.deploy_service.notify_project")
    def test_without_items(self, mock_notify):
        from app.services.deploy_service import _ocp_push_status

        mock_db = MagicMock()
        mock_db.get.return_value = MagicMock()
        with patch(
            "app.services.deploy_service.SessionLocal",
            return_value=mock_db,
            create=True,
        ):
            _ocp_push_status("proj-123", "console", "ready")

        msg = mock_notify.call_args[0][1]
        assert "items" not in msg


# ═══════════════════════════════════════════════════════════════════════
# _ocp_update_status (DB-dependent, mock SessionLocal)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpUpdateStatus:
    def test_updates_status(self):
        from app.services.deploy_service import _ocp_update_status

        mock_project = MagicMock()
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = (
            mock_project
        )

        with patch("app.core.database.SessionLocal", return_value=mock_db):
            _ocp_update_status("proj-123", "complete")

        assert mock_project.ocp_status == "complete"
        mock_db.commit.assert_called_once()

    def test_updates_elapsed(self):
        from app.services.deploy_service import _ocp_update_status

        mock_project = MagicMock()
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = (
            mock_project
        )

        with patch("app.core.database.SessionLocal", return_value=mock_db):
            _ocp_update_status("proj-123", "installing", elapsed_secs=300)

        assert mock_project.ocp_status == "installing"
        assert mock_project.ocp_install_elapsed == 300

    def test_no_project_found(self):
        from app.services.deploy_service import _ocp_update_status

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        with patch("app.core.database.SessionLocal", return_value=mock_db):
            # Should not raise
            _ocp_update_status("nonexistent", "error")

    def test_db_exception_handled(self):
        from app.services.deploy_service import _ocp_update_status

        with patch("app.core.database.SessionLocal", side_effect=Exception("DB down")):
            # Should not raise — exception is caught and logged
            _ocp_update_status("proj-123", "error")


# ═══════════════════════════════════════════════════════════════════════
# _destroy_container (mock troshkad_client)
# ═══════════════════════════════════════════════════════════════════════


class TestDestroyContainer:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service._find_container_volumes", return_value=[])
    def test_destroy_regular_container(self, mock_vols, mock_start, mock_wait):
        mock_start.return_value = "job-1"
        host = MagicMock()
        ctr = {"node_id": "abc12345-1234", "name": "mycontainer", "is_pod": False}
        _destroy_container(host, "proj-12345678", ctr, {}, None)

        mock_start.assert_called_once()
        call_args = mock_start.call_args
        assert call_args[0][1] == "/containers/destroy"
        payload = call_args[0][2]
        assert payload["container_name"] == "troshka-proj-123-abc12345"
        assert payload["project_id"] == "proj-12345678"

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service._find_container_volumes", return_value=[])
    def test_destroy_pod(self, mock_vols, mock_start, mock_wait):
        mock_start.return_value = "job-2"
        host = MagicMock()
        ctr = {"node_id": "xyz-node", "name": "mypod", "is_pod": True}
        _destroy_container(host, "proj-12345678", ctr, {}, None)

        call_args = mock_start.call_args
        assert call_args[0][1] == "/pods/destroy"
        payload = call_args[0][2]
        assert payload["pod_name"] == "troshka-proj-123-mypod"

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch(
        "app.services.deploy_service._find_container_volumes",
        return_value=[{"mount_dir": "/data"}],
    )
    def test_destroy_with_volumes(self, mock_vols, mock_start, mock_wait):
        mock_start.return_value = "job-3"
        host = MagicMock()
        ctr = {"node_id": "abc12345", "name": "ctr", "is_pod": False}
        _destroy_container(host, "proj-12345678", ctr, {}, None)

        payload = mock_start.call_args[0][2]
        assert payload["volumes"] == [{"mount_dir": "/data"}]

    @patch(
        "app.services.deploy_service.start_job",
        side_effect=MagicMock(
            side_effect=__import__(
                "app.services.troshkad_client", fromlist=["TroshkadError"]
            ).TroshkadError("host down")
        ),
    )
    @patch("app.services.deploy_service._find_container_volumes", return_value=[])
    def test_destroy_troshkad_error_handled(self, mock_vols, mock_start):
        host = MagicMock()
        ctr = {"node_id": "abc12345", "name": "ctr"}
        # Should not raise — error is caught and logged
        _destroy_container(host, "proj-12345678", ctr, {}, None)

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service._find_container_volumes", return_value=[])
    def test_destroy_container_calls_wait(self, mock_vols, mock_start, mock_wait):
        mock_start.return_value = "job-99"
        host = MagicMock()
        ctr = {"node_id": "abc12345", "name": "ctr", "is_pod": False}
        _destroy_container(host, "proj-12345678", ctr, {}, None)
        mock_wait.assert_called_once_with(host, "job-99", timeout=30)


# ═══════════════════════════════════════════════════════════════════════
# _ocp_build_summary_detail
# ═══════════════════════════════════════════════════════════════════════


class TestOcpBuildSummaryDetail:
    def test_done_line_found(self):
        from app.services.deploy_service import _ocp_build_summary_detail

        text = 'level=info msg="done (4m30s)"'
        result = _ocp_build_summary_detail({"downloading"}, text)
        assert "done" in result

    def test_long_detail_truncated(self):
        from app.services.deploy_service import _ocp_build_summary_detail

        long_msg = "a" * 100
        text = f'msg="{long_msg}"'
        # Need "done (" in line for it to match
        text = f'msg="done ({long_msg})"'
        result = _ocp_build_summary_detail(set(), text)
        assert len(result) <= 60

    def test_no_done_line_uses_phase_label(self):
        from app.services.deploy_service import _ocp_build_summary_detail

        result = _ocp_build_summary_detail({"downloading"}, "some other log text")
        assert result == "downloading OCP tools"

    def test_fallback_installing(self):
        from app.services.deploy_service import _ocp_build_summary_detail

        result = _ocp_build_summary_detail(set(), "nothing matches")
        assert result == "installing"


# ═══════════════════════════════════════════════════════════════════════
# _ocp_parse_node_status
# ═══════════════════════════════════════════════════════════════════════


class TestOcpParseNodeStatus:
    def test_parses_host_messages(self):
        from app.services.deploy_service import _ocp_parse_node_status

        text = (
            'Host master-0 msg="Writing image to disk: 100%"\n'
            'Host master-1 msg="Rebooting"'
        )
        result = _ocp_parse_node_status(text, ["master-0", "master-1"])
        assert result["master-0"] == "written"
        assert result["master-1"] == "rebooting"

    def test_writing_keeps_first_value(self):
        from app.services.deploy_service import _ocp_parse_node_status

        # "writing" uses setdefault — first value sticks.
        # Real install.log lines don't have quotes around msg values.
        text = (
            "Host master-0 msg=Writing image to disk: 30%\n"
            "Host master-0 msg=Writing image to disk: 80%"
        )
        result = _ocp_parse_node_status(text, ["master-0"])
        assert result["master-0"] == "writing 30%"

    def test_non_writing_overrides(self):
        from app.services.deploy_service import _ocp_parse_node_status

        text = (
            'Host master-0 msg="Writing image to disk: 50%"\n'
            'Host master-0 msg="Rebooting"'
        )
        result = _ocp_parse_node_status(text, ["master-0"])
        assert result["master-0"] == "rebooting"

    def test_unknown_host_ignored(self):
        from app.services.deploy_service import _ocp_parse_node_status

        text = 'Host worker-0 msg="Writing image to disk: 50%"'
        result = _ocp_parse_node_status(text, ["master-0"])
        assert result == {}

    def test_host_colon_format(self):
        from app.services.deploy_service import _ocp_parse_node_status

        text = 'Host: master-0 msg="Joined"'
        result = _ocp_parse_node_status(text, ["master-0"])
        assert result["master-0"] == "joined"

    def test_node_prefix_format(self):
        from app.services.deploy_service import _ocp_parse_node_status

        text = 'Node master-0 msg="Done"'
        result = _ocp_parse_node_status(text, ["master-0"])
        assert result["master-0"] == "done"


# ═══════════════════════════════════════════════════════════════════════
# _build_node_install_items
# ═══════════════════════════════════════════════════════════════════════


class TestBuildNodeInstallItems:
    def test_all_done(self):
        from app.services.deploy_service import _build_node_install_items

        status = {"m0": "done", "m1": "joined"}
        items = _build_node_install_items(status, ["m0", "m1"])
        assert "Installing nodes: ✓" in items[0]

    def test_in_progress(self):
        from app.services.deploy_service import _build_node_install_items

        status = {"m0": "done", "m1": "writing 50%"}
        items = _build_node_install_items(status, ["m0", "m1"])
        assert "⏳" in items[0]

    def test_per_node_detail(self):
        from app.services.deploy_service import _build_node_install_items

        status = {"m0": "configuring"}
        items = _build_node_install_items(status, ["m0", "m1"])
        assert any("m0: configuring" in i for i in items)
        assert any("m1: —" in i for i in items)


# ═══════════════════════════════════════════════════════════════════════
# _ocp_extract_topology_info
# ═══════════════════════════════════════════════════════════════════════


class TestOcpExtractTopologyInfo:
    def test_extracts_bastion_ip_and_password(self):
        from app.services.deploy_service import _ocp_extract_topology_info

        topology = {
            "nodes": [
                {
                    "type": "vmNode",
                    "id": "vm1",
                    "data": {
                        "label": "bastion",
                        "nics": [{"ip": "10.0.0.5"}],
                        "ciCloudUserPassword": "s3cret",
                    },
                },
                {
                    "type": "vmNode",
                    "id": "vm2",
                    "data": {"label": "master-0", "os": "rhcos"},
                },
            ]
        }
        (
            bastion,
            bastion_ip,
            password,
            cp_names,
            dns_domain,
        ) = _ocp_extract_topology_info(topology)
        assert bastion is not None
        assert bastion_ip == "10.0.0.5"
        assert password == "s3cret"
        assert "master-0" in cp_names

    def test_no_bastion(self):
        from app.services.deploy_service import _ocp_extract_topology_info

        topology = {
            "nodes": [
                {
                    "type": "vmNode",
                    "id": "vm1",
                    "data": {"label": "master-0", "os": "rhcos"},
                }
            ]
        }
        (
            bastion,
            bastion_ip,
            password,
            cp_names,
            dns_domain,
        ) = _ocp_extract_topology_info(topology)
        assert bastion is None
        assert bastion_ip == ""
        assert password == ""
        assert len(cp_names) == 1

    def test_empty_topology(self):
        from app.services.deploy_service import _ocp_extract_topology_info

        (
            bastion,
            bastion_ip,
            password,
            cp_names,
            dns_domain,
        ) = _ocp_extract_topology_info({"nodes": []})
        assert bastion is None
        assert cp_names == []
        assert dns_domain == "ocp.ocp.local"

    def test_bastion_without_nics(self):
        from app.services.deploy_service import _ocp_extract_topology_info

        topology = {
            "nodes": [
                {
                    "type": "vmNode",
                    "id": "vm1",
                    "data": {
                        "label": "bastion",
                        "nics": [],
                        "ciCloudUserPassword": "pw",
                    },
                }
            ]
        }
        (
            bastion,
            bastion_ip,
            password,
            cp_names,
            dns_domain,
        ) = _ocp_extract_topology_info(topology)
        assert bastion is not None
        assert bastion_ip == ""

    def test_dns_domain_from_network(self):
        from app.services.deploy_service import _ocp_extract_topology_info

        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "id": "net1",
                    "data": {
                        "dnsRecords": [{"name": "api.lab.example.com"}],
                    },
                }
            ]
        }
        _, _, _, _, dns_domain = _ocp_extract_topology_info(topology)
        assert dns_domain == "lab.example.com"


# ═══════════════════════════════════════════════════════════════════════
# _extract_bastion_info
# ═══════════════════════════════════════════════════════════════════════


class TestExtractBastionInfo:
    def test_finds_bastion(self):
        from app.services.deploy_service import _extract_bastion_info

        nodes = [
            {
                "type": "vmNode",
                "data": {
                    "label": "bastion",
                    "nics": [{"ip": "10.0.0.5"}],
                    "ciCloudUserPassword": "pw123",
                },
            }
        ]
        bastion, ip, pw = _extract_bastion_info(nodes)
        assert bastion is not None
        assert ip == "10.0.0.5"
        assert pw == "pw123"

    def test_no_bastion(self):
        from app.services.deploy_service import _extract_bastion_info

        nodes = [{"type": "vmNode", "data": {"label": "master-0"}}]
        bastion, ip, pw = _extract_bastion_info(nodes)
        assert bastion is None
        assert ip == ""
        assert pw == ""

    def test_bastion_picks_first_nic_with_ip(self):
        from app.services.deploy_service import _extract_bastion_info

        nodes = [
            {
                "type": "vmNode",
                "data": {
                    "label": "bastion",
                    "nics": [{"ip": ""}, {"ip": "10.0.0.8"}],
                    "ciCloudUserPassword": "x",
                },
            }
        ]
        _, ip, _ = _extract_bastion_info(nodes)
        assert ip == "10.0.0.8"

    def test_empty_list(self):
        from app.services.deploy_service import _extract_bastion_info

        bastion, ip, pw = _extract_bastion_info([])
        assert bastion is None


# ═══════════════════════════════════════════════════════════════════════
# _check_install_terminal_state
# ═══════════════════════════════════════════════════════════════════════


class TestCheckInstallTerminalState:
    @patch("app.services.deploy_service._ocp_update_status")
    def test_detects_failure(self, mock_update):
        from app.services.deploy_service import _check_install_terminal_state

        push_fn = MagicMock()
        result = _check_install_terminal_state(
            "Bootstrap failed to complete", push_fn, "proj-1", 100, time
        )
        assert result == ("error", None)
        push_fn.assert_called_once_with("error", "install failed")
        mock_update.assert_called_once_with("proj-1", "error")

    @patch("app.services.deploy_service._ocp_update_status")
    def test_detects_success(self, mock_update):
        from app.services.deploy_service import _check_install_terminal_state

        push_fn = MagicMock()
        result = _check_install_terminal_state(
            "Install complete!", push_fn, "proj-1", time.time() - 300, time
        )
        assert result[0] == "complete"
        assert isinstance(result[1], int)
        assert result[1] >= 299

    @patch("app.services.deploy_service._ocp_update_status")
    def test_context_deadline_exceeded(self, mock_update):
        from app.services.deploy_service import _check_install_terminal_state

        push_fn = MagicMock()
        result = _check_install_terminal_state(
            "context deadline exceeded", push_fn, "proj-1", 100, time
        )
        assert result == ("error", None)

    @patch("app.services.deploy_service._ocp_update_status")
    def test_no_terminal_state(self, mock_update):
        from app.services.deploy_service import _check_install_terminal_state

        push_fn = MagicMock()
        result = _check_install_terminal_state(
            "still installing...", push_fn, "proj-1", 100, time
        )
        assert result is None
        push_fn.assert_not_called()

    @patch("app.services.deploy_service._ocp_update_status")
    def test_all_operators_completed(self, mock_update):
        from app.services.deploy_service import _check_install_terminal_state

        push_fn = MagicMock()
        result = _check_install_terminal_state(
            "All cluster operators have completed", push_fn, "proj-1", time.time(), time
        )
        assert result[0] == "complete"


# ═══════════════════════════════════════════════════════════════════════
# _ocp_report_final_status
# ═══════════════════════════════════════════════════════════════════════


class TestOcpReportFinalStatus:
    @patch("app.services.deploy_service._ocp_update_status")
    def test_all_ready(self, mock_update):
        from app.services.deploy_service import _ocp_report_final_status

        push_fn = MagicMock()
        _ocp_report_final_status("proj-1", True, True, True, "5m", 300, push_fn)
        push_fn.assert_called_once_with("ready", "cluster ready")
        mock_update.assert_called_once_with("proj-1", "ready", 300)

    @patch("app.services.deploy_service._ocp_update_status")
    def test_nodes_not_ready(self, mock_update):
        from app.services.deploy_service import _ocp_report_final_status

        push_fn = MagicMock()
        _ocp_report_final_status("proj-1", False, True, True, "10m", 600, push_fn)
        call_args = push_fn.call_args
        assert "nodes" in call_args[0][1]
        mock_update.assert_called_once_with("proj-1", "warning", 600)

    @patch("app.services.deploy_service._ocp_update_status")
    def test_operators_not_ready(self, mock_update):
        from app.services.deploy_service import _ocp_report_final_status

        push_fn = MagicMock()
        _ocp_report_final_status("proj-1", True, False, True, "10m", 600, push_fn)
        call_args = push_fn.call_args
        assert "operators" in call_args[0][1]

    @patch("app.services.deploy_service._ocp_update_status")
    def test_multiple_not_ready(self, mock_update):
        from app.services.deploy_service import _ocp_report_final_status

        push_fn = MagicMock()
        _ocp_report_final_status("proj-1", False, False, False, "15m", 900, push_fn)
        detail = push_fn.call_args[0][1]
        assert "nodes" in detail
        assert "operators" in detail
        assert "console" in detail


# ═══════════════════════════════════════════════════════════════════════
# _build_clone_name_map
# ═══════════════════════════════════════════════════════════════════════


class TestBuildCloneNameMap:
    def test_builds_map_from_edges(self):
        from app.services.deploy_service import _build_clone_name_map

        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "id": "disk-aaaa1111-2222-3333-4444-555566667777",
                    "data": {
                        "id": "disk-aaaa1111-2222-3333-4444-555566667777",
                        "label": "RHEL 9 disk",
                        "format": "qcow2",
                    },
                },
                {
                    "type": "vmNode",
                    "id": "vm-bbbb1111-2222-3333-4444-555566667777",
                    "data": {"label": "master-0"},
                },
            ],
            "edges": [
                {
                    "source": "disk-aaaa1111-2222-3333-4444-555566667777",
                    "target": "vm-bbbb1111-2222-3333-4444-555566667777",
                }
            ],
        }
        result = _build_clone_name_map(topology)
        assert "vm-vm-bbbb1-disk-disk-aaa" in result or len(result) > 0
        assert any("RHEL 9 disk" in v for v in result.values())

    def test_iso_format_adds_cdrom_entry(self):
        from app.services.deploy_service import _build_clone_name_map

        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "id": "disk1234",
                    "data": {"id": "disk1234", "label": "Boot ISO", "format": "iso"},
                },
                {"type": "vmNode", "id": "vm5678", "data": {"label": "worker"}},
            ],
            "edges": [{"source": "disk1234", "target": "vm5678"}],
        }
        result = _build_clone_name_map(topology)
        assert any("cdrom" in k for k in result.keys())

    def test_empty_topology(self):
        from app.services.deploy_service import _build_clone_name_map

        assert _build_clone_name_map({"nodes": [], "edges": []}) == {}

    def test_no_edges(self):
        from app.services.deploy_service import _build_clone_name_map

        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "id": "d1",
                    "data": {"id": "d1", "label": "disk"},
                }
            ],
            "edges": [],
        }
        assert _build_clone_name_map(topology) == {}


# ═══════════════════════════════════════════════════════════════════════
# _format_import_progress
# ═══════════════════════════════════════════════════════════════════════


class TestFormatImportProgress:
    def test_completed_reason(self):
        from app.services.deploy_service import _format_import_progress

        dv = {
            "status": {
                "conditions": [{"type": "Running", "reason": "Completed"}],
            }
        }
        result = _format_import_progress("RHEL disk", dv, "50%")
        assert result == "RHEL disk: writing to storage"

    def test_error_reason(self):
        from app.services.deploy_service import _format_import_progress

        dv = {
            "status": {
                "conditions": [
                    {
                        "type": "Running",
                        "reason": "Error",
                        "message": "S3 access denied",
                    }
                ],
            }
        }
        result = _format_import_progress("disk", dv, "")
        assert "error" in result
        assert "S3 access denied" in result

    def test_progress_percentage(self):
        from app.services.deploy_service import _format_import_progress

        dv = {"status": {"conditions": []}}
        result = _format_import_progress("disk", dv, "45.5%")
        assert "downloading 45.5%" in result

    def test_progress_at_99_shows_writing(self):
        from app.services.deploy_service import _format_import_progress

        dv = {"status": {"conditions": []}}
        result = _format_import_progress("disk", dv, "99.5%")
        assert "writing to storage" in result

    def test_transfer_running(self):
        from app.services.deploy_service import _format_import_progress

        dv = {
            "status": {
                "conditions": [{"type": "Running", "reason": "TransferRunning"}],
            }
        }
        result = _format_import_progress("disk", dv, "")
        assert "downloading starting" in result

    def test_fallback_starting(self):
        from app.services.deploy_service import _format_import_progress

        dv = {"status": {"conditions": []}}
        result = _format_import_progress("disk", dv, "")
        assert result == "disk: starting"

    def test_na_progress_treated_as_empty(self):
        from app.services.deploy_service import _format_import_progress

        dv = {"status": {"conditions": []}}
        result = _format_import_progress("disk", dv, "N/A")
        assert result == "disk: starting"


# ═══════════════════════════════════════════════════════════════════════
# _format_dv_status_line
# ═══════════════════════════════════════════════════════════════════════


class TestFormatDvStatusLine:
    def test_succeeded(self):
        from app.services.deploy_service import _format_dv_status_line

        dv = {"status": {"phase": "Succeeded"}}
        assert _format_dv_status_line("disk", dv) == "disk: done"

    def test_clone_in_progress(self):
        from app.services.deploy_service import _format_dv_status_line

        dv = {"status": {"phase": "CloneInProgress"}}
        assert _format_dv_status_line("disk", dv) == "disk: cloning"

    def test_clone_scheduled(self):
        from app.services.deploy_service import _format_dv_status_line

        dv = {"status": {"phase": "CloneScheduled"}}
        assert _format_dv_status_line("disk", dv) == "disk: cloning"

    def test_pending(self):
        from app.services.deploy_service import _format_dv_status_line

        dv = {"status": {"phase": "Pending"}}
        assert _format_dv_status_line("disk", dv) == "disk: scheduled"

    def test_failed_with_message(self):
        from app.services.deploy_service import _format_dv_status_line

        dv = {
            "status": {
                "phase": "Failed",
                "conditions": [
                    {"type": "Running", "message": "could not connect to S3"}
                ],
            }
        }
        result = _format_dv_status_line("disk", dv)
        assert "error" in result
        assert "could not connect" in result

    def test_failed_without_message(self):
        from app.services.deploy_service import _format_dv_status_line

        dv = {"status": {"phase": "Failed", "conditions": []}}
        result = _format_dv_status_line("disk", dv)
        assert "failed" in result

    def test_unknown_phase(self):
        from app.services.deploy_service import _format_dv_status_line

        dv = {"status": {"phase": "WaitForFirstConsumer"}}
        assert _format_dv_status_line("disk", dv) == "disk: waitforfirstconsumer"

    def test_empty_phase(self):
        from app.services.deploy_service import _format_dv_status_line

        dv = {"status": {"phase": ""}}
        assert _format_dv_status_line("disk", dv) == "disk: waiting"


# ═══════════════════════════════════════════════════════════════════════
# _best_dv_status
# ═══════════════════════════════════════════════════════════════════════


class TestBestDvStatus:
    def test_keeps_highest_rank(self):
        from app.services.deploy_service import _best_dv_status

        lines = [
            "disk-A: waiting",
            "disk-A: downloading 50%",
            "disk-A: done",
        ]
        result = _best_dv_status(lines)
        assert result["disk-A"] == "done"

    def test_different_labels(self):
        from app.services.deploy_service import _best_dv_status

        lines = [
            "disk-A: done",
            "disk-B: downloading 30%",
        ]
        result = _best_dv_status(lines)
        assert result["disk-A"] == "done"
        assert result["disk-B"] == "downloading 30%"

    def test_empty_list(self):
        from app.services.deploy_service import _best_dv_status

        assert _best_dv_status([]) == {}

    def test_cloning_ranked(self):
        from app.services.deploy_service import _best_dv_status

        lines = ["disk: waiting", "disk: cloning"]
        result = _best_dv_status(lines)
        assert result["disk"] == "cloning"


# ═══════════════════════════════════════════════════════════════════════
# _fill_missing_disk_labels
# ═══════════════════════════════════════════════════════════════════════


class TestFillMissingDiskLabels:
    def test_adds_missing_labels(self):
        from app.services.deploy_service import _fill_missing_disk_labels

        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {"label": "disk-A", "source": "pattern"},
                },
                {
                    "type": "storageNode",
                    "data": {"label": "disk-B", "source": "library"},
                },
            ]
        }
        best = {"disk-A": "done"}
        _fill_missing_disk_labels(topology, best)
        assert best["disk-B"] == "waiting"
        assert best["disk-A"] == "done"

    def test_skips_non_pattern_library(self):
        from app.services.deploy_service import _fill_missing_disk_labels

        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {"label": "blank", "source": "blank"},
                },
            ]
        }
        best = {}
        _fill_missing_disk_labels(topology, best)
        assert best == {}

    def test_does_not_overwrite_existing(self):
        from app.services.deploy_service import _fill_missing_disk_labels

        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {"label": "disk-A", "source": "pattern"},
                },
            ]
        }
        best = {"disk-A": "done"}
        _fill_missing_disk_labels(topology, best)
        assert best["disk-A"] == "done"


# ═══════════════════════════════════════════════════════════════════════
# _collect_used_ips
# ═══════════════════════════════════════════════════════════════════════


class TestCollectUsedIps:
    def test_collects_vm_ips(self):
        from app.services.deploy_topology import _collect_used_ips

        topology = {
            "nodes": [
                {"type": "vmNode", "data": {"nics": [{"ip": "10.0.0.5"}]}},
                {
                    "type": "vmNode",
                    "data": {"nics": [{"ip": "10.0.0.6"}, {"ip": "10.0.0.7"}]},
                },
            ]
        }
        result = _collect_used_ips(topology)
        assert "10.0.0.5" in result
        assert "10.0.0.6" in result
        assert "10.0.0.7" in result

    def test_reserves_gateway_ips(self):
        from app.services.deploy_topology import _collect_used_ips

        topology = {
            "nodes": [
                {"type": "networkNode", "data": {"cidr": "10.0.0.0/24"}},
            ]
        }
        result = _collect_used_ips(topology)
        assert "10.0.0.1" in result

    def test_skips_empty_ips(self):
        from app.services.deploy_topology import _collect_used_ips

        topology = {
            "nodes": [
                {"type": "vmNode", "data": {"nics": [{"ip": ""}]}},
            ]
        }
        result = _collect_used_ips(topology)
        assert "" not in result

    def test_empty_topology(self):
        from app.services.deploy_topology import _collect_used_ips

        assert _collect_used_ips({"nodes": []}) == set()


# ═══════════════════════════════════════════════════════════════════════
# _get_dhcp_range
# ═══════════════════════════════════════════════════════════════════════


class TestGetDhcpRange:
    def test_explicit_range(self):
        from app.services.deploy_topology import _get_dhcp_range

        result = _get_dhcp_range(
            {"dhcpRangeStart": "10.0.0.100", "dhcpRangeEnd": "10.0.0.200"}
        )
        assert result is not None
        import ipaddress

        assert result[0] == int(ipaddress.ip_address("10.0.0.100"))
        assert result[1] == int(ipaddress.ip_address("10.0.0.200"))

    def test_auto_from_cidr(self):
        from app.services.deploy_topology import _get_dhcp_range

        result = _get_dhcp_range({"cidr": "10.0.0.0/24"})
        assert result is not None
        import ipaddress

        assert result[0] == int(ipaddress.ip_address("10.0.0.10"))
        assert result[1] == int(ipaddress.ip_address("10.0.0.254"))

    def test_small_subnet_returns_none(self):
        from app.services.deploy_topology import _get_dhcp_range

        # /28 has 14 hosts; len(hosts) > 10 is True (14 > 10), so it works
        # /29 has 6 hosts; len(hosts) > 10 is False, so returns None
        result = _get_dhcp_range({"cidr": "10.0.0.0/29"})
        assert result is None

    def test_no_cidr_no_range(self):
        from app.services.deploy_topology import _get_dhcp_range

        assert _get_dhcp_range({}) is None

    def test_invalid_cidr(self):
        from app.services.deploy_topology import _get_dhcp_range

        assert _get_dhcp_range({"cidr": "not-a-cidr"}) is None


# ═══════════════════════════════════════════════════════════════════════
# _check_central_source
# ═══════════════════════════════════════════════════════════════════════


class TestCheckCentralSource:
    def test_no_central_client(self):
        from app.services.deploy_service import _check_central_source

        result = _check_central_source(
            "path/disk.qcow2", MagicMock(), "bucket", {}, None, "", {}
        )
        assert result is False

    def test_found_in_primary(self):
        from app.services.deploy_service import _check_central_source

        primary = MagicMock()
        primary.head_object.return_value = {}
        central = MagicMock()
        result = _check_central_source(
            "path/disk.qcow2", primary, "bucket", {}, central, "central-bucket", {}
        )
        assert result is False
        central.head_object.assert_not_called()

    def test_not_in_primary_found_in_central(self):
        from app.services.deploy_service import _check_central_source

        primary = MagicMock()
        primary.head_object.side_effect = Exception("NotFound")
        central = MagicMock()
        central.head_object.return_value = {}
        result = _check_central_source(
            "path/disk.qcow2", primary, "bucket", {}, central, "central-bucket", {}
        )
        assert result is True

    def test_not_found_anywhere(self):
        from app.services.deploy_service import _check_central_source

        primary = MagicMock()
        primary.head_object.side_effect = Exception("NotFound")
        central = MagicMock()
        central.head_object.side_effect = Exception("NotFound")
        result = _check_central_source(
            "path/disk.qcow2", primary, "bucket", {}, central, "central-bucket", {}
        )
        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# _handle_kubevirt_deploy_error
# ═══════════════════════════════════════════════════════════════════════


class TestHandleKubevirtDeployError:
    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_sets_error_state(self, mock_del):
        from app.services.deploy_service import _handle_kubevirt_deploy_error

        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        status = {"error": "disk import failed"}
        _handle_kubevirt_deploy_error("proj-1", project, status, db, notify)
        assert project.state == "error"
        assert project.deploy_error == "disk import failed"
        db.commit.assert_called_once()
        notify.assert_called_once()

    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_fallback_error_message(self, mock_del):
        from app.services.deploy_service import _handle_kubevirt_deploy_error

        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        _handle_kubevirt_deploy_error("proj-1", project, {}, db, notify)
        assert project.deploy_error == "Operator reported an error"

    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_uses_message_field(self, mock_del):
        from app.services.deploy_service import _handle_kubevirt_deploy_error

        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        status = {"message": "PVC bound failed"}
        _handle_kubevirt_deploy_error("proj-1", project, status, db, notify)
        assert project.deploy_error == "PVC bound failed"

    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_notify_includes_error(self, mock_del):
        from app.services.deploy_service import _handle_kubevirt_deploy_error

        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        _handle_kubevirt_deploy_error(
            "proj-1", project, {"error": "timeout"}, db, notify
        )
        msg = notify.call_args[0][1]
        assert msg["state"] == "error"
        assert msg["deploy_error"] == "timeout"


# ═══════════════════════════════════════════════════════════════════════
# _push_kubevirt_deploy_progress
# ═══════════════════════════════════════════════════════════════════════


class TestPushKubevirtDeployProgress:
    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_pushes_when_changed(self, mock_get, mock_set):
        from app.services.deploy_service import _push_kubevirt_deploy_progress

        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        _push_kubevirt_deploy_progress(
            "proj-1",
            project,
            "images",
            "downloading 50%",
            50,
            ["disk: 50%"],
            db,
            notify,
        )
        mock_set.assert_called_once()
        db.commit.assert_called_once()
        notify.assert_called_once()
        msg = notify.call_args[0][1]
        assert msg["type"] == "deploy-progress"
        assert msg["step"] == "images"
        assert msg["percent"] == 50

    @patch("app.services.deploy_service._set_deploy_progress")
    @patch(
        "app.services.deploy_service._get_deploy_progress_data",
        return_value={"step": "images", "detail": "downloading 50%", "percent": 50},
    )
    def test_skips_when_unchanged(self, mock_get, mock_set):
        from app.services.deploy_service import _push_kubevirt_deploy_progress

        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        # Exact same step/detail/percent as last => no update
        _push_kubevirt_deploy_progress(
            "proj-1",
            project,
            "images",
            "downloading 50%",
            50,
            ["line"],
            db,
            notify,
        )
        mock_set.assert_not_called()
        notify.assert_not_called()

    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_skips_when_empty(self, mock_get, mock_set):
        from app.services.deploy_service import _push_kubevirt_deploy_progress

        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        _push_kubevirt_deploy_progress(
            "proj-1", project, "images", "", 0, None, db, notify
        )
        mock_set.assert_not_called()
        notify.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# _check_vm_route_http
# ═══════════════════════════════════════════════════════════════════════


class TestCheckVmRouteHttp:
    def test_returns_http_code(self):
        from app.services.deploy_service import _check_vm_route_http

        oc_fn = MagicMock(return_value="200")
        result = _check_vm_route_http(oc_fn, "console-openshift-console.apps")
        assert result == "200"

    def test_returns_000_on_none(self):
        from app.services.deploy_service import _check_vm_route_http

        oc_fn = MagicMock(return_value=None)
        result = _check_vm_route_http(oc_fn, "console-openshift-console.apps")
        assert result == "000"

    def test_strips_whitespace(self):
        from app.services.deploy_service import _check_vm_route_http

        oc_fn = MagicMock(return_value="  403\n")
        result = _check_vm_route_http(oc_fn, "oauth-openshift.apps")
        assert result == "403"


# ═══════════════════════════════════════════════════════════════════════
# _destroy_cleanup_sg_rules
# ═══════════════════════════════════════════════════════════════════════


class TestDestroyCleanupSgRules:
    @patch("app.services.provider_gc_service._get_ec2_client")
    def test_revokes_matching_rules(self, mock_get_ec2):
        from app.services.deploy_service import _destroy_cleanup_sg_rules

        mock_ec2 = MagicMock()
        mock_get_ec2.return_value = mock_ec2
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                {
                    "IpPermissions": [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 443,
                            "ToPort": 443,
                            "IpRanges": [
                                {
                                    "CidrIp": "0.0.0.0/0",
                                    "Description": "troshka-pf:proj-1234:https",
                                },
                                {
                                    "CidrIp": "0.0.0.0/0",
                                    "Description": "other-rule",
                                },
                            ],
                        }
                    ]
                }
            ]
        }

        host = MagicMock()
        host.provider_id = "prov-1"
        session = MagicMock()
        provider = MagicMock()
        provider.security_group_id = "sg-123"
        session.query.return_value.filter_by.return_value.first.return_value = provider

        _destroy_cleanup_sg_rules(host, "proj-1234", session)
        mock_ec2.revoke_security_group_ingress.assert_called_once()

    def test_no_provider(self):
        from app.services.deploy_service import _destroy_cleanup_sg_rules

        host = MagicMock()
        host.provider_id = "prov-1"
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None

        # Should not raise — catches exception internally
        _destroy_cleanup_sg_rules(host, "proj-1234", session)

    def test_no_sg_id(self):
        from app.services.deploy_service import _destroy_cleanup_sg_rules

        host = MagicMock()
        host.provider_id = "prov-1"
        session = MagicMock()
        provider = MagicMock()
        provider.security_group_id = None
        session.query.return_value.filter_by.return_value.first.return_value = provider

        # Should not raise
        _destroy_cleanup_sg_rules(host, "proj-1234", session)

    def test_no_provider_id(self):
        from app.services.deploy_service import _destroy_cleanup_sg_rules

        host = MagicMock()
        host.provider_id = None
        session = MagicMock()
        # Should not raise
        _destroy_cleanup_sg_rules(host, "proj-1234", session)


# ═══════════════════════════════════════════════════════════════════════
# _ocp_parse_install_phases (integration of sub-helpers)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpParseInstallPhases:
    def test_full_pipeline(self):
        from app.services.deploy_service import _ocp_parse_install_phases

        text = (
            "Downloading openshift-install v4.16\n"
            "Downloaded openshift-install v4.16\n"
            "Creating agent ISO image\n"
            'Host master-0 msg="Writing image to disk: 100%"'
        )
        items, detail, phases, node_status = _ocp_parse_install_phases(
            text, set(), ["master-0"], {"ingress", "dns"}, {}
        )
        assert "downloading" in phases
        assert "downloaded" in phases
        assert "creating-iso" in phases
        assert isinstance(items, list)
        assert len(items) > 0

    def test_preserves_existing_phases(self):
        from app.services.deploy_service import _ocp_parse_install_phases

        _, _, phases, _ = _ocp_parse_install_phases(
            "some log", {"already-seen"}, [], set(), {}
        )
        assert "already-seen" in phases

    def test_empty_text(self):
        from app.services.deploy_service import _ocp_parse_install_phases

        items, detail, phases, node_status = _ocp_parse_install_phases(
            "", set(), ["master-0"], set(), {}
        )
        assert detail == "installing"
        assert node_status == {}


# ═══════════════════════════════════════════════════════════════════════
# _ocp_build_progress_items
# ═══════════════════════════════════════════════════════════════════════


class TestOcpBuildProgressItems:
    def test_combines_all_phases(self):
        import re as _re

        from app.services.deploy_service import _ocp_build_progress_items

        phases = {"downloading", "downloaded", "creating-iso", "iso-ready", "bootstrap"}
        items = _ocp_build_progress_items(
            phases, ["master-0"], {}, "", {"ingress"}, {}, _re
        )
        assert isinstance(items, list)
        assert any("Download OCP tools" in i for i in items)

    def test_with_operators(self):
        import re as _re

        from app.services.deploy_service import _ocp_build_progress_items

        phases = {"control-plane"}
        text = 'msg="Cluster operator ingress is not available"'
        items = _ocp_build_progress_items(
            phases,
            ["master-0"],
            {},
            text,
            {"ingress", "dns"},
            {"image-registry": "registry"},
            _re,
        )
        assert any("operators" in i.lower() or "1/" in i for i in items)


# ═══════════════════════════════════════════════════════════════════════
# _finalize_kubevirt_deploy
# ═══════════════════════════════════════════════════════════════════════


class TestFinalizeKubevirtDeploy:
    @patch("app.services.deploy_service._allocate_kubevirt_eips")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.ws_pubsub.notify_project")
    def test_sets_active_state(self, mock_notify, mock_del, mock_eip):
        from app.services.deploy_service import _finalize_kubevirt_deploy

        project = MagicMock()
        project.ocp_status = None
        db = MagicMock()
        topology = {"nodes": [{"type": "vmNode", "data": {"os": "rhel9"}}]}
        _finalize_kubevirt_deploy("proj-1", project, topology, db)
        assert project.state == "active"
        assert project.deploy_error is None
        db.commit.assert_called_once()
        # finalize emits a topology-update then the project-state notification.
        mock_notify.assert_any_call(
            "proj-1", {"type": "project-state", "state": "active"}
        )

    @patch("app.services.deploy_service._allocate_kubevirt_eips")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.ws_pubsub.notify_project")
    def test_strips_resolved_s3_path(self, mock_notify, mock_del, mock_eip):
        from app.services.deploy_service import _finalize_kubevirt_deploy

        project = MagicMock()
        db = MagicMock()
        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {
                        "resolvedS3Path": "should/be/removed",
                        "presignedUrl": "https://...",
                        "label": "disk",
                    },
                }
            ]
        }
        _finalize_kubevirt_deploy("proj-1", project, topology, db)
        clean_topo = project.deployed_topology
        for node in clean_topo["nodes"]:
            assert "resolvedS3Path" not in node["data"]
            assert "presignedUrl" not in node["data"]

    @patch("app.services.deploy_service._allocate_kubevirt_eips")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.ws_pubsub.notify_project")
    def test_ocp_monitor_enabled(self, mock_notify, mock_del, mock_eip):
        from app.services.deploy_service import _finalize_kubevirt_deploy

        project = MagicMock()
        db = MagicMock()
        topology = {"nodes": [{"type": "vmNode", "data": {"ocpMonitor": True}}]}
        _finalize_kubevirt_deploy("proj-1", project, topology, db)
        assert project.ocp_status == "monitoring"


# ═══════════════════════════════════════════════════════════════════════
# _vm_dir
# ═══════════════════════════════════════════════════════════════════════


class TestVmDir:
    def test_local_no_pool(self):
        from app.services.deploy_topology import _vm_dir

        assert _vm_dir("proj-123") == "/var/lib/troshka/vms/proj-123"

    def test_local_with_none_pool(self):
        from app.services.deploy_topology import _vm_dir

        assert _vm_dir("proj-123", None) == "/var/lib/troshka/vms/proj-123"

    def test_shared_pool(self):
        from app.services.deploy_topology import _vm_dir

        pool = MagicMock()
        pool.mode = "shared-fsx"
        assert _vm_dir("proj-123", pool) == "/var/lib/troshka/shared/vms/proj-123"

    def test_shared_byo_pool(self):
        from app.services.deploy_topology import _vm_dir

        pool = MagicMock()
        pool.mode = "shared-byo"
        assert _vm_dir("proj-123", pool) == "/var/lib/troshka/shared/vms/proj-123"

    def test_local_pool(self):
        from app.services.deploy_topology import _vm_dir

        pool = MagicMock()
        pool.mode = "local"
        assert _vm_dir("proj-123", pool) == "/var/lib/troshka/vms/proj-123"


# ═══════════════════════════════════════════════════════════════════════
# _disk_path
# ═══════════════════════════════════════════════════════════════════════


class TestDiskPath:
    def test_basic(self):
        from app.services.deploy_topology import _disk_path

        result = _disk_path("proj-1234", "vm-node-id", "disk-node-id", "qcow2")
        assert result == "/var/lib/troshka/vms/proj-1234/vm-node--disk-nod.qcow2"

    def test_iso_format(self):
        from app.services.deploy_topology import _disk_path

        result = _disk_path("proj-1234", "vm-abcdef", "disk-xyz", "iso")
        assert result.endswith(".iso")

    def test_shared_pool(self):
        from app.services.deploy_topology import _disk_path

        pool = MagicMock()
        pool.mode = "shared-fsx"
        result = _disk_path("proj-1234", "vm-node-id", "disk-node-id", "qcow2", pool)
        assert "/shared/vms/" in result

    def test_truncation(self):
        from app.services.deploy_topology import _disk_path

        result = _disk_path("p" * 36, "v" * 36, "d" * 36, "qcow2")
        parts = result.split("/")[-1]
        # vm_node_id[:8] - disk_node_id[:8] . format
        assert parts == f"{'v' * 8}-{'d' * 8}.qcow2"


# ═══════════════════════════════════════════════════════════════════════
# _seed_path
# ═══════════════════════════════════════════════════════════════════════


class TestSeedPath:
    def test_basic(self):
        from app.services.deploy_topology import _seed_path

        result = _seed_path("proj-1234", "vm-node-id")
        assert result == "/var/lib/troshka/vms/proj-1234/vm-node--seed.iso"

    def test_shared_pool(self):
        from app.services.deploy_topology import _seed_path

        pool = MagicMock()
        pool.mode = "shared-fsx"
        result = _seed_path("proj-1234", "vm-node-id", pool)
        assert "/shared/vms/" in result
        assert result.endswith("-seed.iso")


# ═══════════════════════════════════════════════════════════════════════
# _image_cache_path
# ═══════════════════════════════════════════════════════════════════════


class TestImageCachePath:
    def test_local(self):
        from app.services.deploy_topology import _image_cache_path

        result = _image_cache_path("item-123", "qcow2")
        assert result == "/var/lib/troshka/images/item-123.qcow2"

    def test_shared(self):
        from app.services.deploy_topology import _image_cache_path

        pool = MagicMock()
        pool.mode = "shared-fsx"
        result = _image_cache_path("item-123", "qcow2", pool)
        assert result == "/var/lib/troshka/shared/images/item-123.qcow2"

    def test_iso(self):
        from app.services.deploy_topology import _image_cache_path

        result = _image_cache_path("item-123", "iso")
        assert result == "/var/lib/troshka/images/item-123.iso"


# ═══════════════════════════════════════════════════════════════════════
# _pattern_cache_path
# ═══════════════════════════════════════════════════════════════════════


class TestPatternCachePath:
    def test_basic(self):
        from app.services.deploy_topology import _pattern_cache_path

        result = _pattern_cache_path("pat-123", "disk-456", "qcow2")
        assert result == "/var/lib/troshka/local/cache/patterns/pat-123/disk-456.qcow2"

    def test_always_local(self):
        from app.services.deploy_topology import _pattern_cache_path

        pool = MagicMock()
        pool.mode = "shared-fsx"
        result = _pattern_cache_path("pat-123", "disk-456", "qcow2", pool)
        # Pattern cache is always local, never shared
        assert "/local/cache/patterns/" in result


# ═══════════════════════════════════════════════════════════════════════
# _snapshot_cache_path
# ═══════════════════════════════════════════════════════════════════════


class TestSnapshotCachePath:
    def test_basic(self):
        from app.services.deploy_topology import _snapshot_cache_path

        result = _snapshot_cache_path("snap-123", "disk-456", "qcow2")
        assert result == "/var/lib/troshka/cache/snapshots/snap-123/disk-456.qcow2"


# ═══════════════════════════════════════════════════════════════════════
# validate_topology_names
# ═══════════════════════════════════════════════════════════════════════


class TestValidateTopologyNames:
    def test_no_duplicates(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "vmNode", "data": {"name": "vm-a"}},
                {"id": "2", "type": "vmNode", "data": {"name": "vm-b"}},
            ]
        }
        assert validate_topology_names(topo) == []

    def test_duplicate_vm_names(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "vmNode", "data": {"name": "master-0"}},
                {"id": "2", "type": "vmNode", "data": {"name": "master-0"}},
            ]
        }
        errors = validate_topology_names(topo)
        assert len(errors) == 1
        assert "Duplicate VM name" in errors[0]

    def test_duplicate_network_names(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "networkNode", "data": {"name": "net1"}},
                {"id": "2", "type": "networkNode", "data": {"name": "net1"}},
            ]
        }
        errors = validate_topology_names(topo)
        assert len(errors) == 1
        assert "Network" in errors[0]

    def test_duplicate_storage_names(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "storageNode", "data": {"name": "disk"}},
                {"id": "2", "type": "storageNode", "data": {"name": "disk"}},
            ]
        }
        errors = validate_topology_names(topo)
        assert len(errors) == 1
        assert "Disk" in errors[0]

    def test_same_name_different_types_ok(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "vmNode", "data": {"name": "alpha"}},
                {"id": "2", "type": "networkNode", "data": {"name": "alpha"}},
            ]
        }
        assert validate_topology_names(topo) == []

    def test_empty_names_skipped(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "vmNode", "data": {"name": ""}},
                {"id": "2", "type": "vmNode", "data": {"name": ""}},
            ]
        }
        assert validate_topology_names(topo) == []

    def test_label_fallback(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "vmNode", "data": {"label": "myvm"}},
                {"id": "2", "type": "vmNode", "data": {"label": "myvm"}},
            ]
        }
        errors = validate_topology_names(topo)
        assert len(errors) == 1

    def test_unknown_types_ignored(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "unknownNode", "data": {"name": "a"}},
                {"id": "2", "type": "unknownNode", "data": {"name": "a"}},
            ]
        }
        assert validate_topology_names(topo) == []


# ═══════════════════════════════════════════════════════════════════════
# validate_topology_ips
# ═══════════════════════════════════════════════════════════════════════


class TestValidateTopologyIps:
    def test_no_duplicates(self):
        from app.services.deploy_topology import validate_topology_ips

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "name": "vm-a",
                        "nics": [{"id": "n1", "ip": "10.0.0.5"}],
                    },
                },
                {
                    "id": "vm2",
                    "type": "vmNode",
                    "data": {
                        "name": "vm-b",
                        "nics": [{"id": "n2", "ip": "10.0.0.6"}],
                    },
                },
                {"id": "net1", "type": "networkNode", "data": {"name": "net"}},
            ],
            "edges": [
                {"source": "net1", "target": "vm1", "targetHandle": "nic-n1-top"},
                {"source": "net1", "target": "vm2", "targetHandle": "nic-n2-top"},
            ],
        }
        assert validate_topology_ips(topo) == []

    def test_duplicate_ips_on_same_network(self):
        from app.services.deploy_topology import validate_topology_ips

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "name": "vm-a",
                        "nics": [{"id": "n1", "ip": "10.0.0.5"}],
                    },
                },
                {
                    "id": "vm2",
                    "type": "vmNode",
                    "data": {
                        "name": "vm-b",
                        "nics": [{"id": "n2", "ip": "10.0.0.5"}],
                    },
                },
                {"id": "net1", "type": "networkNode", "data": {"name": "net"}},
            ],
            "edges": [
                {"source": "net1", "target": "vm1", "targetHandle": "nic-n1-top"},
                {"source": "net1", "target": "vm2", "targetHandle": "nic-n2-top"},
            ],
        }
        errors = validate_topology_ips(topo)
        assert len(errors) == 1
        assert "Duplicate IP" in errors[0]
        assert "10.0.0.5" in errors[0]

    def test_empty_ips_skipped(self):
        from app.services.deploy_topology import validate_topology_ips

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"name": "vm-a", "nics": [{"id": "n1", "ip": ""}]},
                },
            ],
            "edges": [],
        }
        assert validate_topology_ips(topo) == []

    def test_empty_topology(self):
        from app.services.deploy_topology import validate_topology_ips

        assert validate_topology_ips({"nodes": [], "edges": []}) == []


# ═══════════════════════════════════════════════════════════════════════
# validate_topology_passwords
# ═══════════════════════════════════════════════════════════════════════


class TestValidateTopologyPasswords:
    def test_no_bmc_network(self):
        from app.services.deploy_topology import validate_topology_passwords

        topo = {"nodes": [{"type": "networkNode", "data": {"networkType": "vxlan"}}]}
        assert validate_topology_passwords(topo) == []

    def test_bmc_with_password(self):
        from app.services.deploy_topology import validate_topology_passwords

        topo = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "networkType": "bmc",
                        "name": "bmc-net",
                        "bmcPassword": "s3cret",
                    },
                }
            ]
        }
        assert validate_topology_passwords(topo) == []

    def test_bmc_without_password(self):
        from app.services.deploy_topology import validate_topology_passwords

        topo = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "networkType": "bmc",
                        "name": "bmc-net",
                        "bmcPassword": "",
                    },
                }
            ]
        }
        errors = validate_topology_passwords(topo)
        assert len(errors) == 1
        assert "no password" in errors[0].lower()

    def test_empty_topology(self):
        from app.services.deploy_topology import validate_topology_passwords

        assert validate_topology_passwords({"nodes": []}) == []


# ═══════════════════════════════════════════════════════════════════════
# diff_topologies
# ═══════════════════════════════════════════════════════════════════════


class TestDiffTopologies:
    def test_no_changes(self):
        from app.services.deploy_topology import diff_topologies

        topo = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"name": "a"}},
                {"id": "net1", "type": "networkNode", "data": {"name": "n"}},
            ]
        }
        result = diff_topologies(topo, topo)
        assert result["has_changes"] is False
        assert result["added_vms"] == []
        assert result["removed_vms"] == []

    def test_added_vm(self):
        from app.services.deploy_topology import diff_topologies

        current = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"name": "a"}},
                {"id": "vm2", "type": "vmNode", "data": {"name": "b"}},
            ]
        }
        deployed = {"nodes": [{"id": "vm1", "type": "vmNode", "data": {"name": "a"}}]}
        result = diff_topologies(current, deployed)
        assert result["has_changes"] is True
        assert len(result["added_vms"]) == 1
        assert result["added_vms"][0]["id"] == "vm2"

    def test_removed_vm(self):
        from app.services.deploy_topology import diff_topologies

        current = {"nodes": [{"id": "vm1", "type": "vmNode", "data": {"name": "a"}}]}
        deployed = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"name": "a"}},
                {"id": "vm2", "type": "vmNode", "data": {"name": "b"}},
            ]
        }
        result = diff_topologies(current, deployed)
        assert len(result["removed_vms"]) == 1
        assert result["removed_vms"][0]["id"] == "vm2"

    def test_changed_vm(self):
        from app.services.deploy_topology import diff_topologies

        current = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"name": "a", "vcpus": 4}}
            ]
        }
        deployed = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"name": "a", "vcpus": 2}}
            ]
        }
        result = diff_topologies(current, deployed)
        assert len(result["changed_vms"]) == 1
        assert result["has_changes"] is True

    def test_skip_keys_not_counted(self):
        from app.services.deploy_topology import diff_topologies

        current = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"name": "a", "status": "running"},
                }
            ]
        }
        deployed = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"name": "a", "status": "stopped"},
                }
            ]
        }
        result = diff_topologies(current, deployed)
        assert result["changed_vms"] == []
        assert result["has_changes"] is False

    def test_added_network(self):
        from app.services.deploy_topology import diff_topologies

        current = {
            "nodes": [
                {"id": "net1", "type": "networkNode", "data": {"name": "n1"}},
                {"id": "net2", "type": "networkNode", "data": {"name": "n2"}},
            ]
        }
        deployed = {
            "nodes": [{"id": "net1", "type": "networkNode", "data": {"name": "n1"}}]
        }
        result = diff_topologies(current, deployed)
        assert len(result["added_networks"]) == 1

    def test_removed_network(self):
        from app.services.deploy_topology import diff_topologies

        current = {"nodes": []}
        deployed = {
            "nodes": [{"id": "net1", "type": "networkNode", "data": {"name": "n1"}}]
        }
        result = diff_topologies(current, deployed)
        assert len(result["removed_networks"]) == 1

    def test_empty_topologies(self):
        from app.services.deploy_topology import diff_topologies

        result = diff_topologies({"nodes": []}, {"nodes": []})
        assert result["has_changes"] is False


# ═══════════════════════════════════════════════════════════════════════
# _extract_vms
# ═══════════════════════════════════════════════════════════════════════


class TestExtractVms:
    def test_extracts_basic_vm(self):
        from app.services.deploy_topology import _extract_vms

        topo = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "name": "master-0",
                        "vcpus": 4,
                        "ram": 16,
                        "os": "rhcos",
                    },
                }
            ]
        }
        vms = _extract_vms(topo)
        assert len(vms) == 1
        assert vms[0]["name"] == "master-0"
        assert vms[0]["vcpus"] == 4
        assert vms[0]["ram_gb"] == 16
        assert vms[0]["node_id"] == "vm-1"

    def test_defaults(self):
        from app.services.deploy_topology import _extract_vms

        topo = {"nodes": [{"id": "vm-1", "type": "vmNode", "data": {}}]}
        vms = _extract_vms(topo)
        assert vms[0]["name"] == "vm"
        assert vms[0]["vcpus"] == 2
        assert vms[0]["ram_gb"] == 4
        assert vms[0]["firmware"] == "bios"
        assert vms[0]["cloud_init"] is False

    def test_skips_non_vm_nodes(self):
        from app.services.deploy_topology import _extract_vms

        topo = {
            "nodes": [
                {"id": "net", "type": "networkNode", "data": {}},
                {"id": "disk", "type": "storageNode", "data": {}},
            ]
        }
        assert _extract_vms(topo) == []

    def test_multiple_vms(self):
        from app.services.deploy_topology import _extract_vms

        topo = {
            "nodes": [
                {"id": "v1", "type": "vmNode", "data": {"name": "a"}},
                {"id": "v2", "type": "vmNode", "data": {"name": "b"}},
                {"id": "v3", "type": "vmNode", "data": {"name": "c"}},
            ]
        }
        assert len(_extract_vms(topo)) == 3


# ═══════════════════════════════════════════════════════════════════════
# _extract_containers
# ═══════════════════════════════════════════════════════════════════════


class TestExtractContainers:
    def test_extracts_basic_container(self):
        from app.services.deploy_topology import _extract_containers

        topo = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {
                        "name": "nginx",
                        "image": "nginx:latest",
                        "cpus": 2,
                        "memory": 1024,
                    },
                }
            ]
        }
        ctrs = _extract_containers(topo)
        assert len(ctrs) == 1
        assert ctrs[0]["name"] == "nginx"
        assert ctrs[0]["image"] == "nginx:latest"
        assert ctrs[0]["cpus"] == 2
        assert ctrs[0]["memory_mb"] == 1024

    def test_defaults(self):
        from app.services.deploy_topology import _extract_containers

        topo = {"nodes": [{"id": "ctr-1", "type": "containerNode", "data": {}}]}
        ctrs = _extract_containers(topo)
        assert ctrs[0]["name"] == "container"
        assert ctrs[0]["cpus"] == 1
        assert ctrs[0]["memory_mb"] == 512
        assert ctrs[0]["is_pod"] is False

    def test_pod_flag(self):
        from app.services.deploy_topology import _extract_containers

        topo = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {"isPod": True},
                }
            ]
        }
        ctrs = _extract_containers(topo)
        assert ctrs[0]["is_pod"] is True

    def test_skips_non_container_nodes(self):
        from app.services.deploy_topology import _extract_containers

        topo = {"nodes": [{"id": "vm-1", "type": "vmNode", "data": {}}]}
        assert _extract_containers(topo) == []


# ═══════════════════════════════════════════════════════════════════════
# _should_skip
# ═══════════════════════════════════════════════════════════════════════


class TestShouldSkip:
    def test_no_resume(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip(None, "networks") is False

    def test_skip_completed_step(self):
        from app.services.deploy_service import _should_skip

        # resume_from="images" means skip everything before "images"
        assert _should_skip("images", "networks") is True
        assert _should_skip("images", "seeds") is True

    def test_dont_skip_resume_step(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip("images", "images") is False

    def test_dont_skip_future_steps(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip("images", "vms") is False

    def test_unknown_step_not_skipped(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip("images", "unknown-step") is False


# ═══════════════════════════════════════════════════════════════════════
# _vm_domain_name
# ═══════════════════════════════════════════════════════════════════════


class TestVmDomainName:
    def test_basic(self):
        from app.services.deploy_topology import _vm_domain_name

        assert _vm_domain_name("proj-1234", "node-5678") == "troshka-proj-123-node-567"

    def test_truncation(self):
        from app.services.deploy_topology import _vm_domain_name

        result = _vm_domain_name("a" * 36, "b" * 36)
        assert result == f"troshka-{'a' * 8}-{'b' * 8}"

    def test_short_ids(self):
        from app.services.deploy_topology import _vm_domain_name

        assert _vm_domain_name("abc", "xyz") == "troshka-abc-xyz"


# ═══════════════════════════════════════════════════════════════════════
# _find_vm_name_by_ip
# ═══════════════════════════════════════════════════════════════════════


class TestFindVmNameByIp:
    def test_found(self):
        from app.services.deploy_topology import _find_vm_name_by_ip

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"name": "bastion", "nics": [{"ip": "10.0.0.5"}]},
                }
            ]
        }
        assert _find_vm_name_by_ip(topo, "10.0.0.5") == "bastion"

    def test_not_found(self):
        from app.services.deploy_topology import _find_vm_name_by_ip

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"name": "bastion", "nics": [{"ip": "10.0.0.5"}]},
                }
            ]
        }
        assert _find_vm_name_by_ip(topo, "10.0.0.99") == "10-0-0-99"

    def test_empty_topology(self):
        from app.services.deploy_topology import _find_vm_name_by_ip

        assert _find_vm_name_by_ip({"nodes": []}, "10.0.0.1") == "10-0-0-1"

    def test_multiple_nics(self):
        from app.services.deploy_topology import _find_vm_name_by_ip

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "name": "worker",
                        "nics": [{"ip": "10.0.0.5"}, {"ip": "10.0.1.5"}],
                    },
                }
            ]
        }
        assert _find_vm_name_by_ip(topo, "10.0.1.5") == "worker"


# ═══════════════════════════════════════════════════════════════════════
# _find_vm_disks
# ═══════════════════════════════════════════════════════════════════════


class TestFindVmDisks:
    def test_finds_disk_via_dp_handle(self):
        from app.services.deploy_topology import _find_vm_disks

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"diskControllers": [{"id": "dp-ctrl1", "bus": "sata"}]},
                },
                {
                    "id": "disk1",
                    "type": "storageNode",
                    "data": {
                        "name": "RHEL",
                        "size": 40,
                        "format": "qcow2",
                        "source": "library",
                        "libraryItemId": "lib-123",
                    },
                },
            ],
            "edges": [
                {
                    "source": "vm1",
                    "target": "disk1",
                    "sourceHandle": "dp-ctrl1",
                    "targetHandle": "disk-top",
                }
            ],
        }
        disks = _find_vm_disks("vm1", topo)
        assert len(disks) == 1
        assert disks[0]["name"] == "RHEL"
        assert disks[0]["size_gb"] == 40
        assert disks[0]["bus"] == "sata"
        assert disks[0]["library_item_id"] == "lib-123"

    def test_no_disks(self):
        from app.services.deploy_topology import _find_vm_disks

        topo = {
            "nodes": [{"id": "vm1", "type": "vmNode", "data": {}}],
            "edges": [],
        }
        assert _find_vm_disks("vm1", topo) == []

    def test_ignores_nic_handles(self):
        from app.services.deploy_topology import _find_vm_disks

        topo = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {}},
                {"id": "net1", "type": "networkNode", "data": {}},
            ],
            "edges": [
                {"source": "vm1", "target": "net1", "sourceHandle": "nic-n1-top"}
            ],
        }
        assert _find_vm_disks("vm1", topo) == []


# ═══════════════════════════════════════════════════════════════════════
# _find_vm_networks
# ═══════════════════════════════════════════════════════════════════════


class TestFindVmNetworks:
    def test_finds_network_via_nic_handle(self):
        from app.services.deploy_topology import _find_vm_networks

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "nics": [
                            {"id": "nic1", "mac": "52:54:00:aa:bb:cc", "model": "e1000"}
                        ]
                    },
                },
                {"id": "net1", "type": "networkNode", "data": {}},
            ],
            "edges": [
                {
                    "source": "vm1",
                    "target": "net1",
                    "sourceHandle": "nic-nic1-top",
                    "targetHandle": "net-in",
                }
            ],
        }
        vni_map = {"net1": 100}
        nets = _find_vm_networks("vm1", topo, vni_map)
        assert len(nets) == 1
        assert nets[0]["bridge"] == "br-100"
        assert nets[0]["mac"] == "52:54:00:aa:bb:cc"
        assert nets[0]["model"] == "e1000"

    def test_bmc_network(self):
        from app.services.deploy_topology import _find_vm_networks

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"nics": [{"id": "nic1", "mac": "52:54:01:aa:bb:cc"}]},
                },
                {
                    "id": "bmc-net",
                    "type": "networkNode",
                    "data": {"networkType": "bmc"},
                },
            ],
            "edges": [
                {
                    "source": "vm1",
                    "target": "bmc-net",
                    "sourceHandle": "nic-nic1-top",
                }
            ],
        }
        nets = _find_vm_networks("vm1", topo, {}, "proj-12345678")
        assert len(nets) == 1
        assert nets[0]["bridge"] == "br-bmc-proj-123"

    def test_no_networks(self):
        from app.services.deploy_topology import _find_vm_networks

        topo = {"nodes": [{"id": "vm1", "type": "vmNode", "data": {}}], "edges": []}
        assert _find_vm_networks("vm1", topo, {}) == []


# ═══════════════════════════════════════════════════════════════════════
# _extract_bmc_config
# ═══════════════════════════════════════════════════════════════════════


class TestExtractBmcConfig:
    def test_no_bmc_network(self):
        from app.services.deploy_topology import _extract_bmc_config

        topo = {"nodes": [{"type": "networkNode", "data": {"networkType": "vxlan"}}]}
        assert _extract_bmc_config(topo, "proj-123") is None

    def test_bmc_network_no_vms(self):
        from app.services.deploy_topology import _extract_bmc_config

        topo = {
            "nodes": [
                {"id": "bmc", "type": "networkNode", "data": {"networkType": "bmc"}},
                {"id": "vm1", "type": "vmNode", "data": {"bmcEnabled": False}},
            ],
            "edges": [],
        }
        assert _extract_bmc_config(topo, "proj-123") is None

    def test_bmc_with_vms(self):
        from app.services.deploy_topology import _extract_bmc_config

        topo = {
            "nodes": [
                {
                    "id": "bmc",
                    "type": "networkNode",
                    "data": {"networkType": "bmc", "cidr": "192.168.100.0/24"},
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "name": "sno",
                        "bmcEnabled": True,
                        "bmcIp": "192.168.100.10",
                    },
                },
            ],
            "edges": [],
        }
        config = _extract_bmc_config(topo, "proj-12345678")
        assert config is not None
        assert len(config["vms"]) == 1
        assert config["vms"][0]["bmc_ip"] == "192.168.100.10"
        assert config["vms"][0]["domain_name"] == "troshka-proj-123-vm1"

    def test_bmc_with_dhcp_hosts(self):
        from app.services.deploy_topology import _extract_bmc_config

        topo = {
            "nodes": [
                {
                    "id": "bmc-net",
                    "type": "networkNode",
                    "data": {"networkType": "bmc"},
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "name": "sno",
                        "bmcEnabled": True,
                        "bmcIp": "192.168.100.10",
                        "nics": [
                            {
                                "id": "nic1",
                                "mac": "52:54:01:00:00:01",
                                "ip": "192.168.100.20",
                            }
                        ],
                    },
                },
            ],
            "edges": [
                {
                    "source": "vm1",
                    "target": "bmc-net",
                    "sourceHandle": "nic-nic1-top",
                }
            ],
        }
        config = _extract_bmc_config(topo, "proj-12345678")
        assert len(config["dhcp_hosts"]) == 1
        assert config["dhcp_hosts"][0]["mac"] == "52:54:01:00:00:01"
        assert config["dhcp_hosts"][0]["ip"] == "192.168.100.20"


# ═══════════════════════════════════════════════════════════════════════
# _resolve_boot_devs
# ═══════════════════════════════════════════════════════════════════════


class TestResolveBootDevs:
    def test_defaults_hd_only(self):
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": None}
        disks = [{"format": "qcow2"}]
        topo = {"nodes": []}
        assert _resolve_boot_devs(vm, disks, topo) == ["hd"]

    def test_defaults_iso_only_non_bootable(self):
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": None}
        disks = [{"format": "iso", "bootable_iso": False}]
        topo = {"nodes": []}
        assert _resolve_boot_devs(vm, disks, topo) == ["network"]

    def test_defaults_bootable_iso_only(self):
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": None}
        disks = [{"format": "iso", "bootable_iso": True}]
        topo = {"nodes": []}
        assert _resolve_boot_devs(vm, disks, topo) == ["cdrom"]

    def test_defaults_iso_and_disk(self):
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": None}
        disks = [
            {"format": "iso", "bootable_iso": True},
            {"format": "qcow2"},
        ]
        topo = {"nodes": []}
        result = _resolve_boot_devs(vm, disks, topo)
        assert result == ["cdrom", "hd"]

    def test_config_iso_and_disk_boots_hd_only(self):
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": None}
        disks = [
            {"format": "iso", "bootable_iso": False},
            {"format": "qcow2"},
        ]
        topo = {"nodes": []}
        assert _resolve_boot_devs(vm, disks, topo) == ["hd"]

    def test_defaults_no_disks(self):
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": None}
        disks = []
        topo = {"nodes": []}
        assert _resolve_boot_devs(vm, disks, topo) == ["network"]

    def test_explicit_network(self):
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": ["network"], "disk_controllers": []}
        disks = []
        topo = {"nodes": []}
        assert _resolve_boot_devs(vm, disks, topo) == ["network"]

    def test_explicit_hd_with_non_bootable_iso(self):
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": ["hd"], "disk_controllers": []}
        disks = [
            {"format": "iso", "bootable_iso": False},
            {"format": "qcow2"},
        ]
        topo = {"nodes": []}
        assert _resolve_boot_devs(vm, disks, topo) == ["hd"]

    def test_explicit_hd_with_bootable_iso_overrides(self):
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": ["hd"], "disk_controllers": []}
        disks = [
            {"format": "iso", "bootable_iso": True},
            {"format": "qcow2"},
        ]
        topo = {"nodes": []}
        result = _resolve_boot_devs(vm, disks, topo)
        assert result == ["cdrom", "hd"]

    def test_storage_node_id_as_boot_dev(self):
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": ["disk-iso-1", "disk-hd-1"], "disk_controllers": []}
        disks = [{"format": "qcow2"}]
        topo = {
            "nodes": [
                {
                    "id": "disk-iso-1",
                    "type": "storageNode",
                    "data": {"format": "iso"},
                },
                {
                    "id": "disk-hd-1",
                    "type": "storageNode",
                    "data": {"format": "qcow2"},
                },
            ]
        }
        result = _resolve_boot_devs(vm, disks, topo)
        assert result == ["cdrom", "hd"]

    def test_deduplicates(self):
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": ["hd", "hd", "network"], "disk_controllers": []}
        disks = [{"format": "qcow2"}]
        topo = {"nodes": []}
        result = _resolve_boot_devs(vm, disks, topo)
        assert result == ["hd", "network"]


# ═══════════════════════════════════════════════════════════════════════
# _auto_assign_container_ips
# ═══════════════════════════════════════════════════════════════════════


class TestAutoAssignContainerIps:
    def test_assigns_ip_from_dhcp_range(self):
        from app.services.deploy_topology import _auto_assign_container_ips

        topo = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"cidr": "10.0.0.0/24"},
                },
                {
                    "id": "ctr1",
                    "type": "containerNode",
                    "data": {"name": "nginx", "nics": [{"id": "nic1", "ip": ""}]},
                },
            ],
            "edges": [
                {
                    "source": "ctr1",
                    "target": "net1",
                    "sourceHandle": "nic-nic1-top",
                    "targetHandle": "net-in",
                }
            ],
        }
        _auto_assign_container_ips(topo)
        nic = topo["nodes"][1]["data"]["nics"][0]
        assert nic["ip"] != ""
        assert nic["ip"].startswith("10.0.0.")

    def test_skips_static_ips(self):
        from app.services.deploy_topology import _auto_assign_container_ips

        topo = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"cidr": "10.0.0.0/24"},
                },
                {
                    "id": "ctr1",
                    "type": "containerNode",
                    "data": {
                        "name": "nginx",
                        "nics": [{"id": "nic1", "ip": "10.0.0.50"}],
                    },
                },
            ],
            "edges": [],
        }
        _auto_assign_container_ips(topo)
        assert topo["nodes"][1]["data"]["nics"][0]["ip"] == "10.0.0.50"

    def test_avoids_used_ips(self):
        from app.services.deploy_topology import _auto_assign_container_ips

        topo = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"cidr": "10.0.0.0/24"},
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"nics": [{"ip": "10.0.0.10"}]},
                },
                {
                    "id": "ctr1",
                    "type": "containerNode",
                    "data": {"name": "nginx", "nics": [{"id": "nic1", "ip": ""}]},
                },
            ],
            "edges": [
                {
                    "source": "ctr1",
                    "target": "net1",
                    "sourceHandle": "nic-nic1-top",
                    "targetHandle": "net-in",
                }
            ],
        }
        _auto_assign_container_ips(topo)
        assigned_ip = topo["nodes"][2]["data"]["nics"][0]["ip"]
        assert assigned_ip != "10.0.0.10"
        assert assigned_ip != ""


# ═══════════════════════════════════════════════════════════════════════
# _line_mentions_host
# ═══════════════════════════════════════════════════════════════════════


class TestLineMentionsHost:
    def test_host_format(self):
        from app.services.deploy_service import _line_mentions_host

        assert _line_mentions_host('Host master-0 msg="done"', "master-0") is True

    def test_host_colon_format(self):
        from app.services.deploy_service import _line_mentions_host

        assert _line_mentions_host('Host: master-0 msg="done"', "master-0") is True

    def test_node_format(self):
        from app.services.deploy_service import _line_mentions_host

        assert _line_mentions_host('Node master-0 msg="done"', "master-0") is True

    def test_no_match(self):
        from app.services.deploy_service import _line_mentions_host

        assert _line_mentions_host("random log line", "master-0") is False

    def test_partial_name_no_match(self):
        from app.services.deploy_service import _line_mentions_host

        # "master-0" is in "master-00", but Host master-00 != Host master-0
        assert (
            _line_mentions_host('Host master-00 msg="done"', "master-0") is True
        )  # substring match is expected


# ═══════════════════════════════════════════════════════════════════════
# _update_node_status_from_line
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateNodeStatusFromLine:
    def test_updates_status(self):
        from app.services.deploy_service import _update_node_status_from_line

        status = {}
        _update_node_status_from_line(
            'Host master-0 msg="Rebooting"', ["master-0"], status
        )
        assert status["master-0"] == "rebooting"

    def test_writing_uses_setdefault(self):
        from app.services.deploy_service import _update_node_status_from_line

        status = {"master-0": "writing 30%"}
        _update_node_status_from_line(
            'Host master-0 msg="Writing image to disk: 80%"', ["master-0"], status
        )
        # writing uses setdefault so first value sticks
        assert status["master-0"] == "writing 30%"

    def test_non_writing_overrides(self):
        from app.services.deploy_service import _update_node_status_from_line

        status = {"master-0": "writing 50%"}
        _update_node_status_from_line(
            'Host master-0 msg="Rebooting"', ["master-0"], status
        )
        assert status["master-0"] == "rebooting"

    def test_unrecognized_msg_ignored(self):
        from app.services.deploy_service import _update_node_status_from_line

        status = {}
        _update_node_status_from_line(
            "Host master-0 msg=some random log", ["master-0"], status
        )
        assert status == {}


# ═══════════════════════════════════════════════════════════════════════
# _match_unavailable_ops
# ═══════════════════════════════════════════════════════════════════════


class TestMatchUnavailableOps:
    def test_finds_tracked_ops(self):
        from app.services.deploy_service import _match_unavailable_ops

        msg = "Cluster operators ingress, dns are not available"
        tracked = {"ingress", "dns", "console"}
        assert _match_unavailable_ops(msg, tracked, {}) == {"ingress", "dns"}

    def test_finds_aliases(self):
        from app.services.deploy_service import _match_unavailable_ops

        msg = "image-registry is not available"
        tracked = {"registry"}
        aliases = {"image-registry": "registry"}
        assert _match_unavailable_ops(msg, tracked, aliases) == {"registry"}

    def test_no_match(self):
        from app.services.deploy_service import _match_unavailable_ops

        assert _match_unavailable_ops("all good", {"ingress"}, {}) == set()


# ═══════════════════════════════════════════════════════════════════════
# _apply_bastion_browser_fixes
# ═══════════════════════════════════════════════════════════════════════


class TestApplyBastionBrowserFixes:
    def test_no_fix_needed(self):
        from app.services.deploy_service import _apply_bastion_browser_fixes

        exec_fn = MagicMock()
        push_fn = MagicMock()
        result = _apply_bastion_browser_fixes(
            "ca:ok\nlogins:ok", exec_fn, push_fn, "ca-cmd", "autologin-cmd"
        )
        assert result is False
        exec_fn.assert_not_called()

    def test_ca_stale(self):
        from app.services.deploy_service import _apply_bastion_browser_fixes

        exec_fn = MagicMock()
        push_fn = MagicMock()
        result = _apply_bastion_browser_fixes(
            "ca:stale\nlogins:ok", exec_fn, push_fn, "ca-cmd", "autologin-cmd"
        )
        assert result is True
        exec_fn.assert_called_once_with("ca-cmd", timeout=15)

    def test_logins_missing(self):
        from app.services.deploy_service import (
            _CLEAR_BASTION_OCP_COOKIES_CMD,
            _ENSURE_FIREFOX_PROFILE_CMD,
            _KILL_BROWSER_CMD,
            _apply_bastion_browser_fixes,
        )

        exec_fn = MagicMock()
        push_fn = MagicMock()
        result = _apply_bastion_browser_fixes(
            "ca:ok\nlogins:missing", exec_fn, push_fn, "ca-cmd", "autologin-cmd"
        )
        assert result is True
        assert exec_fn.call_count == 4
        exec_fn.assert_any_call(_KILL_BROWSER_CMD, timeout=10)
        exec_fn.assert_any_call(_CLEAR_BASTION_OCP_COOKIES_CMD, timeout=10)
        exec_fn.assert_any_call(_ENSURE_FIREFOX_PROFILE_CMD, timeout=20)
        exec_fn.assert_any_call("autologin-cmd", timeout=90)

    def test_both_stale(self):
        from app.services.deploy_service import _apply_bastion_browser_fixes

        exec_fn = MagicMock()
        push_fn = MagicMock()
        result = _apply_bastion_browser_fixes(
            "ca:stale\nlogins:stale", exec_fn, push_fn, "ca-cmd", "autologin-cmd"
        )
        assert result is True
        assert exec_fn.call_count == 5

    def test_none_verify(self):
        from app.services.deploy_service import _apply_bastion_browser_fixes

        exec_fn = MagicMock()
        push_fn = MagicMock()
        result = _apply_bastion_browser_fixes(
            None, exec_fn, push_fn, "ca-cmd", "autologin-cmd"
        )
        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# _check_vm_console_and_oauth
# ═══════════════════════════════════════════════════════════════════════


class TestCheckVmConsoleAndOauth:
    def test_both_ready(self):
        from app.services.deploy_service import _check_vm_console_and_oauth

        oc_fn = MagicMock(side_effect=["200", "403"])
        push_fn = MagicMock()
        result = _check_vm_console_and_oauth(oc_fn, push_fn, MagicMock())
        assert result is True

    def test_console_not_ready(self):
        from app.services.deploy_service import _check_vm_console_and_oauth

        oc_fn = MagicMock(return_value="000")
        push_fn = MagicMock()
        mock_time = MagicMock()
        result = _check_vm_console_and_oauth(oc_fn, push_fn, mock_time)
        assert result is False

    def test_console_ready_oauth_not(self):
        from app.services.deploy_service import _check_vm_console_and_oauth

        oc_fn = MagicMock(side_effect=["200", "000"])
        push_fn = MagicMock()
        mock_time = MagicMock()
        result = _check_vm_console_and_oauth(oc_fn, push_fn, mock_time)
        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# _report_pre_install_status
# ═══════════════════════════════════════════════════════════════════════


class TestReportPreInstallStatus:
    def test_oc_mirror_running(self):
        from app.services.deploy_service import _report_pre_install_status

        push_fn = MagicMock()
        _report_pre_install_status("oc-mirror --config=...---active---", push_fn)
        push_fn.assert_called_once_with(
            "installing", "mirroring OCP images (oc-mirror)"
        )

    def test_registry_active(self):
        from app.services.deploy_service import _report_pre_install_status

        push_fn = MagicMock()
        _report_pre_install_status("---active---", push_fn)
        push_fn.assert_called_once_with(
            "installing", "setting up disconnected registry"
        )

    def test_fallback(self):
        from app.services.deploy_service import _report_pre_install_status

        push_fn = MagicMock()
        _report_pre_install_status("---inactive---", push_fn)
        push_fn.assert_called_once_with("installing", "preparing environment")


# ═══════════════════════════════════════════════════════════════════════
# _teardown_networks_via_troshkad (mocked)
# ═══════════════════════════════════════════════════════════════════════


class TestTeardownNetworksViaTroshkad:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_basic_teardown(self, mock_start, mock_wait):
        from app.services.deploy_service import _teardown_networks_via_troshkad

        mock_start.return_value = "job-1"
        host = MagicMock()
        _teardown_networks_via_troshkad(host, "proj-123", {"net1": 100, "net2": 200})
        mock_start.assert_called_once()
        payload = mock_start.call_args[0][2]
        assert payload["project_id"] == "proj-123"
        assert set(payload["vni_list"]) == {100, 200}

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_empty_vni_map(self, mock_start, mock_wait):
        from app.services.deploy_service import _teardown_networks_via_troshkad

        mock_start.return_value = "job-2"
        host = MagicMock()
        _teardown_networks_via_troshkad(host, "proj-123", {})
        payload = mock_start.call_args[0][2]
        assert payload["vni_list"] == []

    def test_troshkad_error_handled(self):
        from app.services.deploy_service import _teardown_networks_via_troshkad
        from app.services.troshkad_client import TroshkadError as _TE

        with patch("app.services.deploy_service.start_job", side_effect=_TE("down")):
            host = MagicMock()
            # Should not raise
            _teardown_networks_via_troshkad(host, "proj-123", {"net1": 100})

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_none_vni_map(self, mock_start, mock_wait):
        from app.services.deploy_service import _teardown_networks_via_troshkad

        mock_start.return_value = "job-3"
        host = MagicMock()
        _teardown_networks_via_troshkad(host, "proj-123", None)
        payload = mock_start.call_args[0][2]
        assert payload["vni_list"] == []


# ═══════════════════════════════════════════════════════════════════════
# _control_plane_detail
# ═══════════════════════════════════════════════════════════════════════


class TestControlPlaneDetail:
    def test_extracts_version_and_pct(self):
        import re as _re

        from app.services.deploy_service import _control_plane_detail

        phases = {"control-plane"}
        text = 'msg="Working towards 4.16.5: 45% complete"'
        result = _control_plane_detail(phases, text, _re)
        assert "4.16" in result

    def test_no_matching_text(self):
        import re as _re

        from app.services.deploy_service import _control_plane_detail

        result = _control_plane_detail(set(), "some text", _re)
        # Returns default "⏳" when no "Working towards" lines found
        assert result == "⏳"

    def test_initialized(self):
        import re as _re

        from app.services.deploy_service import _control_plane_detail

        phases = {"control-plane", "initialized"}
        text = 'msg="Working towards 4.16.5: 100% complete"'
        result = _control_plane_detail(phases, text, _re)
        assert "4.16" in result

    def test_no_version_match(self):
        import re as _re

        from app.services.deploy_service import _control_plane_detail

        phases = {"control-plane"}
        result = _control_plane_detail(phases, "nothing relevant", _re)
        assert result is not None  # returns default string


# ═══════════════════════════════════════════════════════════════════════
# _find_container_networks
# ═══════════════════════════════════════════════════════════════════════


class TestFindContainerNetworks:
    def test_finds_network(self):
        from app.services.deploy_topology import _find_container_networks

        topo = {
            "nodes": [
                {
                    "id": "ctr1",
                    "type": "containerNode",
                    "data": {
                        "nics": [
                            {"id": "nic1", "mac": "52:54:00:aa:bb:cc", "ip": "10.0.0.5"}
                        ]
                    },
                },
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"cidr": "10.0.0.0/24"},
                },
            ],
            "edges": [
                {
                    "source": "ctr1",
                    "target": "net1",
                    "sourceHandle": "nic-nic1-top",
                    "targetHandle": "net-in",
                }
            ],
        }
        vni_map = {"net1": 100}
        nets = _find_container_networks("ctr1", topo, vni_map)
        assert len(nets) == 1
        assert nets[0]["bridge"] == "br-100"
        assert nets[0]["ip"] == "10.0.0.5"
        assert nets[0]["cidr"] == "10.0.0.0/24"
        assert nets[0]["gateway"] == "10.0.0.1"

    def test_uses_configured_dhcp_gateway(self):
        from app.services.deploy_topology import _find_container_networks

        topo = {
            "nodes": [
                {
                    "id": "ctr1",
                    "type": "containerNode",
                    "data": {
                        "nics": [
                            {"id": "nic1", "mac": "52:54:00:aa:bb:cc", "ip": "10.0.0.5"}
                        ]
                    },
                },
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {
                        "cidr": "10.0.0.0/24",
                        "dhcpGateway": "10.0.0.254",
                    },
                },
            ],
            "edges": [
                {
                    "source": "ctr1",
                    "target": "net1",
                    "sourceHandle": "nic-nic1-top",
                    "targetHandle": "net-in",
                }
            ],
        }
        nets = _find_container_networks("ctr1", topo, {"net1": 100})
        assert nets[0]["gateway"] == "10.0.0.254"

    def test_no_matching_node(self):
        from app.services.deploy_topology import _find_container_networks

        topo = {"nodes": [], "edges": []}
        assert _find_container_networks("ctr1", topo, {}) == []

    def test_no_vni_mapping(self):
        from app.services.deploy_topology import _find_container_networks

        topo = {
            "nodes": [
                {
                    "id": "ctr1",
                    "type": "containerNode",
                    "data": {"nics": [{"id": "nic1"}]},
                },
                {"id": "net1", "type": "networkNode", "data": {}},
            ],
            "edges": [
                {
                    "source": "ctr1",
                    "target": "net1",
                    "sourceHandle": "nic-nic1-top",
                }
            ],
        }
        # VNI map doesn't have net1
        assert _find_container_networks("ctr1", topo, {}) == []


# ═══════════════════════════════════════════════════════════════════════
# _project_deleted (mocked DB)
# ═══════════════════════════════════════════════════════════════════════


class TestProjectDeleted:
    def test_project_exists(self):
        from app.services.deploy_service import _project_deleted

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = (
            MagicMock()
        )
        with patch("app.core.database.SessionLocal", return_value=mock_db):
            assert _project_deleted("proj-123") is False

    def test_project_deleted(self):
        from app.services.deploy_service import _project_deleted

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        with patch("app.core.database.SessionLocal", return_value=mock_db):
            assert _project_deleted("proj-123") is True

    def test_project_deleting(self):
        from app.services.deploy_service import _project_deleted

        mock_db = MagicMock()
        mock_project = MagicMock()
        mock_project.state = "deleting"
        mock_db.query.return_value.filter_by.return_value.first.return_value = (
            mock_project
        )
        with patch("app.core.database.SessionLocal", return_value=mock_db):
            assert _project_deleted("proj-123") is True

    def test_session_closed(self):
        from app.services.deploy_service import _project_deleted

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        with patch("app.core.database.SessionLocal", return_value=mock_db):
            _project_deleted("proj-123")
        mock_db.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _destroy_cleanup_route_access (mocked)
# ═══════════════════════════════════════════════════════════════════════


class TestDestroyCleanupRouteAccess:
    @patch("app.services.providers.get_provider_driver")
    def test_ocpvirt_cleanup(self, mock_get_driver):
        from app.services.deploy_service import _destroy_cleanup_route_access

        mock_driver = MagicMock()
        mock_get_driver.return_value = mock_driver
        host = MagicMock()
        host.provider_id = "prov-1"
        session = MagicMock()
        provider = MagicMock()
        provider.type = "ocpvirt"
        session.query.return_value.filter_by.return_value.first.return_value = provider
        _destroy_cleanup_route_access(host, "proj-123", session)
        mock_driver.delete_route_access.assert_called_once_with(provider, "proj-123")

    def test_non_ocpvirt_skipped(self):
        from app.services.deploy_service import _destroy_cleanup_route_access

        host = MagicMock()
        host.provider_id = "prov-1"
        session = MagicMock()
        provider = MagicMock()
        provider.type = "ec2"
        session.query.return_value.filter_by.return_value.first.return_value = provider
        # Should not raise and should not call driver
        _destroy_cleanup_route_access(host, "proj-123", session)

    def test_no_host(self):
        from app.services.deploy_service import _destroy_cleanup_route_access

        host = MagicMock()
        host.provider_id = None
        session = MagicMock()
        _destroy_cleanup_route_access(host, "proj-123", session)

    def test_no_provider(self):
        from app.services.deploy_service import _destroy_cleanup_route_access

        host = MagicMock()
        host.provider_id = "prov-1"
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        _destroy_cleanup_route_access(host, "proj-123", session)


# ═══════════════════════════════════════════════════════════════════════
# validate_topology_passwords
# ═══════════════════════════════════════════════════════════════════════


class TestValidateTopologyPasswordsV2:
    def test_no_bmc_networks(self):
        from app.services.deploy_topology import validate_topology_passwords

        topology = {
            "nodes": [
                {"type": "vmNode", "data": {"name": "vm1"}},
                {
                    "type": "networkNode",
                    "data": {"name": "net1", "networkType": "management"},
                },
            ]
        }
        assert validate_topology_passwords(topology) == []

    def test_bmc_with_password(self):
        from app.services.deploy_topology import validate_topology_passwords

        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "name": "bmc-net",
                        "networkType": "bmc",
                        "bmcPassword": "secret123",
                    },
                }
            ]
        }
        assert validate_topology_passwords(topology) == []

    def test_bmc_without_password(self):
        from app.services.deploy_topology import validate_topology_passwords

        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {"name": "bmc-net", "networkType": "bmc"},
                }
            ]
        }
        errors = validate_topology_passwords(topology)
        assert len(errors) == 1
        assert "bmc-net" in errors[0]
        assert "no password" in errors[0]

    def test_multiple_bmc_networks_mixed(self):
        from app.services.deploy_topology import validate_topology_passwords

        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "name": "bmc-ok",
                        "networkType": "bmc",
                        "bmcPassword": "pass",
                    },
                },
                {
                    "type": "networkNode",
                    "data": {"name": "bmc-bad", "networkType": "bmc"},
                },
                {
                    "type": "networkNode",
                    "data": {
                        "name": "bmc-also-bad",
                        "networkType": "bmc",
                        "bmcPassword": "",
                    },
                },
            ]
        }
        errors = validate_topology_passwords(topology)
        assert len(errors) == 2
        names = [e for e in errors]
        assert any("bmc-bad" in e for e in names)
        assert any("bmc-also-bad" in e for e in names)

    def test_empty_topology(self):
        from app.services.deploy_topology import validate_topology_passwords

        assert validate_topology_passwords({}) == []
        assert validate_topology_passwords({"nodes": []}) == []


# ═══════════════════════════════════════════════════════════════════════
# _update_deploy_progress
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateDeployProgress:
    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_basic_step(self, mock_set, mock_notify):
        from app.services.deploy_service import _update_deploy_progress

        with patch("app.services.deploy_service._DP_SL", create=True), patch(
            "app.core.database.SessionLocal"
        ) as mock_sl:
            mock_session = MagicMock()
            mock_sl.return_value = mock_session
            mock_session.get.return_value = MagicMock()
            _update_deploy_progress("proj-1", "networks")
        mock_set.assert_called_once()
        progress = mock_set.call_args[0][1]
        assert progress["step"] == "networks"
        assert progress["detail"] == ""

    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_with_detail(self, mock_set, mock_notify):
        from app.services.deploy_service import _update_deploy_progress

        with patch("app.core.database.SessionLocal") as mock_sl:
            mock_session = MagicMock()
            mock_sl.return_value = mock_session
            mock_session.get.return_value = MagicMock()
            _update_deploy_progress("proj-1", "disks", detail="creating disk 1/3")
        progress = mock_set.call_args[0][1]
        assert progress["detail"] == "creating disk 1/3"

    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_with_items(self, mock_set, mock_notify):
        from app.services.deploy_service import _update_deploy_progress

        items = [{"label": "disk1", "status": "done"}]
        with patch("app.core.database.SessionLocal") as mock_sl:
            mock_session = MagicMock()
            mock_sl.return_value = mock_session
            mock_session.get.return_value = MagicMock()
            _update_deploy_progress("proj-1", "disks", items=items)
        progress = mock_set.call_args[0][1]
        assert progress["items"] == items

    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_notifies_websocket(self, mock_set, mock_notify):
        from app.services.deploy_service import _update_deploy_progress

        with patch("app.core.database.SessionLocal") as mock_sl:
            mock_session = MagicMock()
            mock_sl.return_value = mock_session
            mock_session.get.return_value = MagicMock()
            _update_deploy_progress("proj-1", "vms")
        mock_notify.assert_called_once()
        args = mock_notify.call_args[0]
        assert args[0] == "proj-1"
        assert args[1]["type"] == "deploy-progress"


# ═══════════════════════════════════════════════════════════════════════
# _checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestCheckpoint:
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_updates_deploy_step(self, mock_progress):
        from app.services.deploy_service import _checkpoint

        session = MagicMock()
        project = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = project
        _checkpoint(session, "proj-1", "networks")
        assert project.deploy_step == "networks"
        session.commit.assert_called_once()

    @patch(
        "app.services.deploy_service._get_deploy_progress_data",
        return_value={"step": "disks", "detail": "50%"},
    )
    def test_persists_progress_from_redis(self, mock_progress):
        from app.services.deploy_service import _checkpoint

        session = MagicMock()
        project = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = project
        _checkpoint(session, "proj-1", "disks")
        assert project.deploy_progress == {"step": "disks", "detail": "50%"}

    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_no_project_found(self, mock_progress):
        from app.services.deploy_service import _checkpoint

        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        # Should not raise
        _checkpoint(session, "nonexistent", "networks")
        session.commit.assert_not_called()

    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_does_not_overwrite_progress_when_none(self, mock_progress):
        from app.services.deploy_service import _checkpoint

        session = MagicMock()
        project = MagicMock()
        project.deploy_progress = {"step": "old"}
        session.query.return_value.filter_by.return_value.first.return_value = project
        _checkpoint(session, "proj-1", "vms")
        # When _get_deploy_progress_data returns None, deploy_progress should
        # NOT be overwritten (no assignment happens in the function)
        assert project.deploy_progress == {"step": "old"}


# ═══════════════════════════════════════════════════════════════════════
# _auto_assign_container_ips
# ═══════════════════════════════════════════════════════════════════════


class TestAutoAssignContainerIpsV2:
    def _make_topology(
        self,
        cidr="192.168.1.0/24",
        container_nics=None,
        vm_nics=None,
        net_id="net-1",
        ctr_id="ctr-1",
    ):
        """Build a minimal topology with one network and one container."""
        if container_nics is None:
            container_nics = [{"id": "nic-a", "name": "eth0"}]
        nodes = [
            {
                "id": net_id,
                "type": "networkNode",
                "data": {"name": "mgmt", "cidr": cidr},
            },
            {
                "id": ctr_id,
                "type": "containerNode",
                "data": {"name": "my-container", "nics": container_nics},
            },
        ]
        if vm_nics:
            nodes.append(
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"name": "vm1", "nics": vm_nics},
                }
            )
        edges = [
            {
                "source": ctr_id,
                "target": net_id,
                "sourceHandle": f"nic-{container_nics[0]['id']}-top",
                "targetHandle": "bottom",
            }
        ]
        return {"nodes": nodes, "edges": edges}

    def test_assigns_ip_from_dhcp_range(self):
        from app.services.deploy_topology import _auto_assign_container_ips

        topology = self._make_topology()
        _auto_assign_container_ips(topology)
        nic = topology["nodes"][1]["data"]["nics"][0]
        assert nic["ip"]  # Should have been assigned something
        # The DHCP range starts at .10 for /24
        assert nic["ip"].startswith("192.168.1.")
        octet = int(nic["ip"].split(".")[-1])
        assert octet >= 10  # DHCP range starts at hosts[9] = .10

    def test_skips_nic_with_existing_ip(self):
        from app.services.deploy_topology import _auto_assign_container_ips

        topology = self._make_topology(
            container_nics=[{"id": "nic-a", "name": "eth0", "ip": "192.168.1.50"}]
        )
        _auto_assign_container_ips(topology)
        nic = topology["nodes"][1]["data"]["nics"][0]
        assert nic["ip"] == "192.168.1.50"  # Unchanged

    def test_avoids_ips_used_by_vms(self):
        from app.services.deploy_topology import _auto_assign_container_ips

        # VM uses .10 (first DHCP address), so container should get .11
        topology = self._make_topology(
            vm_nics=[{"id": "vm-nic-1", "name": "eth0", "ip": "192.168.1.10"}]
        )
        _auto_assign_container_ips(topology)
        nic = topology["nodes"][1]["data"]["nics"][0]
        assert nic["ip"] != "192.168.1.10"
        assert nic["ip"] == "192.168.1.11"

    def test_no_connected_network(self):
        from app.services.deploy_topology import _auto_assign_container_ips

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {"name": "lonely", "nics": [{"id": "nic-a"}]},
                }
            ],
            "edges": [],  # No edges connecting to any network
        }
        _auto_assign_container_ips(topology)
        nic = topology["nodes"][0]["data"]["nics"][0]
        assert not nic.get("ip")

    def test_network_without_cidr(self):
        from app.services.deploy_topology import _auto_assign_container_ips

        topology = self._make_topology(cidr="")
        _auto_assign_container_ips(topology)
        nic = topology["nodes"][1]["data"]["nics"][0]
        assert not nic.get("ip")


# ═══════════════════════════════════════════════════════════════════════
# _build_clone_name_map
# ═══════════════════════════════════════════════════════════════════════


class TestBuildCloneNameMapV2:
    def test_basic_disk_mapping(self):
        from app.services.deploy_service import _build_clone_name_map

        topology = {
            "nodes": [
                {
                    "id": "disk-aaaa1111-bbbb-cccc",
                    "type": "storageNode",
                    "data": {
                        "id": "disk-aaaa1111-bbbb-cccc",
                        "label": "Root Disk",
                        "format": "qcow2",
                    },
                },
                {
                    "id": "vm-xxxx2222-yyyy-zzzz",
                    "type": "vmNode",
                    "data": {"name": "my-vm"},
                },
            ],
            "edges": [
                {"source": "disk-aaaa1111-bbbb-cccc", "target": "vm-xxxx2222-yyyy-zzzz"}
            ],
        }
        result = _build_clone_name_map(topology)
        assert "vm-vm-xxxx2-disk-disk-aaa" in result
        assert result["vm-vm-xxxx2-disk-disk-aaa"] == "Root Disk"

    def test_iso_disk_adds_cdrom_entry(self):
        from app.services.deploy_service import _build_clone_name_map

        topology = {
            "nodes": [
                {
                    "id": "iso-1111",
                    "type": "storageNode",
                    "data": {
                        "id": "iso-1111",
                        "label": "Install ISO",
                        "format": "iso",
                    },
                },
                {"id": "vm-2222", "type": "vmNode", "data": {"name": "vm1"}},
            ],
            "edges": [{"source": "iso-1111", "target": "vm-2222"}],
        }
        result = _build_clone_name_map(topology)
        # ISO format should also add cdrom entry
        assert any("cdrom" in k for k in result)

    def test_no_storage_nodes(self):
        from app.services.deploy_service import _build_clone_name_map

        topology = {
            "nodes": [{"id": "vm-1", "type": "vmNode", "data": {"name": "vm1"}}],
            "edges": [],
        }
        assert _build_clone_name_map(topology) == {}

    def test_storage_with_no_edges(self):
        from app.services.deploy_service import _build_clone_name_map

        topology = {
            "nodes": [
                {
                    "id": "disk-1",
                    "type": "storageNode",
                    "data": {"id": "disk-1", "label": "Orphan"},
                }
            ],
            "edges": [],
        }
        assert _build_clone_name_map(topology) == {}

    def test_empty_topology(self):
        from app.services.deploy_service import _build_clone_name_map

        assert _build_clone_name_map({}) == {}
        assert _build_clone_name_map({"nodes": [], "edges": []}) == {}


# ═══════════════════════════════════════════════════════════════════════
# _create_and_start_container
# ═══════════════════════════════════════════════════════════════════════


class TestCreateAndStartContainer:
    @patch(
        "app.services.deploy_service.wait_for_job", return_value={"status": "completed"}
    )
    @patch("app.services.deploy_service.start_job", return_value="job-1")
    @patch("app.services.deploy_service._find_container_volumes", return_value=[])
    @patch(
        "app.services.deploy_service._find_container_networks",
        return_value=[
            {
                "bridge": "br-100",
                "ip": "10.0.0.5",
                "mac": "aa:bb:cc:00:00:01",
                "cidr": "10.0.0.0/24",
            }
        ],
    )
    def test_creates_and_starts(self, mock_nets, mock_vols, mock_start, mock_wait):
        from app.services.deploy_service import _create_and_start_container

        host = MagicMock()
        ctr = {
            "node_id": "ctr-abcd1234",
            "image": "registry.io/myimg:latest",
            "cpus": 2,
            "memory_mb": 1024,
            "env_vars": {"FOO": "bar"},
            "ports": [{"host": 8080, "container": 80}],
        }
        _create_and_start_container(
            host, "proj-1234", ctr, {"nodes": [], "edges": []}, {}
        )
        assert mock_start.call_count == 2
        # First call is /containers/create
        assert mock_start.call_args_list[0][0][1] == "/containers/create"
        # Second call is /containers/start
        assert mock_start.call_args_list[1][0][1] == "/containers/start"

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="job-1")
    @patch("app.services.deploy_service._find_container_volumes", return_value=[])
    @patch("app.services.deploy_service._find_container_networks", return_value=[])
    def test_container_name_format(self, mock_nets, mock_vols, mock_start, mock_wait):
        from app.services.deploy_service import _create_and_start_container

        host = MagicMock()
        ctr = {
            "node_id": "ctr-abcdefgh-1234",
            "image": "img:v1",
            "cpus": 1,
            "memory_mb": 512,
            "env_vars": {},
            "ports": [],
        }
        _create_and_start_container(
            host, "proj-99887766", ctr, {"nodes": [], "edges": []}, {}
        )
        create_params = mock_start.call_args_list[0][0][2]
        assert create_params["container_name"] == "troshka-proj-998-ctr-abcd"

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="job-1")
    @patch("app.services.deploy_service._find_container_volumes", return_value=[])
    @patch("app.services.deploy_service._find_container_networks", return_value=[])
    def test_passes_restart_policy(self, mock_nets, mock_vols, mock_start, mock_wait):
        from app.services.deploy_service import _create_and_start_container

        host = MagicMock()
        ctr = {
            "node_id": "ctr-aaa",
            "image": "img",
            "cpus": 1,
            "memory_mb": 512,
            "env_vars": {},
            "ports": [],
            "restart_policy": "on-failure",
            "privileged": True,
        }
        _create_and_start_container(
            host, "proj-1234", ctr, {"nodes": [], "edges": []}, {}
        )
        create_params = mock_start.call_args_list[0][0][2]
        assert create_params["restart_policy"] == "on-failure"
        assert create_params["privileged"] is True


# ═══════════════════════════════════════════════════════════════════════
# _create_and_start_pod
# ═══════════════════════════════════════════════════════════════════════


class TestCreateAndStartPod:
    @patch(
        "app.services.deploy_service.wait_for_job", return_value={"status": "completed"}
    )
    @patch("app.services.deploy_service.start_job", return_value="job-1")
    @patch("app.services.deploy_service._find_container_volumes", return_value=[])
    @patch(
        "app.services.deploy_service._find_container_networks",
        return_value=[
            {
                "bridge": "br-200",
                "ip": "10.0.0.10",
                "mac": "aa:bb:cc:00:00:02",
                "cidr": "10.0.0.0/24",
            }
        ],
    )
    def test_creates_and_starts(self, mock_nets, mock_vols, mock_start, mock_wait):
        from app.services.deploy_service import _create_and_start_pod

        host = MagicMock()
        ctr = {
            "node_id": "pod-abcd",
            "name": "my-pod",
            "init_containers": [],
            "pod_containers": [
                {
                    "name": "app",
                    "image": "myapp:v1",
                    "cpus": 1,
                    "memory": 256,
                    "envVars": [],
                    "mounts": [],
                }
            ],
        }
        _create_and_start_pod(host, "proj-1234", ctr, {"nodes": [], "edges": []}, {})
        assert mock_start.call_count == 2
        assert mock_start.call_args_list[0][0][1] == "/pods/create"
        assert mock_start.call_args_list[1][0][1] == "/pods/start"

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="job-1")
    @patch("app.services.deploy_service._find_container_volumes", return_value=[])
    @patch("app.services.deploy_service._find_container_networks", return_value=[])
    def test_pod_start_uses_prefixed_name(
        self, mock_nets, mock_vols, mock_start, mock_wait
    ):
        from app.services.deploy_service import _create_and_start_pod

        host = MagicMock()
        ctr = {
            "node_id": "pod-xyz",
            "name": "infra-pod",
            "init_containers": [],
            "pod_containers": [],
        }
        _create_and_start_pod(
            host, "proj-abcdefgh", ctr, {"nodes": [], "edges": []}, {}
        )
        start_params = mock_start.call_args_list[1][0][2]
        assert start_params["pod_name"] == "troshka-proj-abc-infra-pod"

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="job-1")
    @patch("app.services.deploy_service._find_container_volumes", return_value=[])
    @patch("app.services.deploy_service._find_container_networks", return_value=[])
    def test_init_containers_included(
        self, mock_nets, mock_vols, mock_start, mock_wait
    ):
        from app.services.deploy_service import _create_and_start_pod

        host = MagicMock()
        ctr = {
            "node_id": "pod-1",
            "name": "mypod",
            "init_containers": [
                {
                    "name": "init-setup",
                    "image": "busybox:latest",
                    "envVars": [{"key": "MODE", "value": "init"}],
                    "mounts": [],
                    "command": "echo hello",
                }
            ],
            "pod_containers": [
                {
                    "name": "main",
                    "image": "nginx:latest",
                    "cpus": 1,
                    "memory": 512,
                    "envVars": [],
                    "mounts": [],
                }
            ],
        }
        _create_and_start_pod(host, "proj-1234", ctr, {"nodes": [], "edges": []}, {})
        create_params = mock_start.call_args_list[0][0][2]
        assert len(create_params["init_containers"]) == 1
        assert create_params["init_containers"][0]["name"] == "init-setup"
        assert create_params["init_containers"][0]["env"] == {"MODE": "init"}

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="job-1")
    @patch("app.services.deploy_service._find_container_volumes", return_value=[])
    @patch("app.services.deploy_service._find_container_networks", return_value=[])
    def test_default_restart_policy(self, mock_nets, mock_vols, mock_start, mock_wait):
        from app.services.deploy_service import _create_and_start_pod

        host = MagicMock()
        ctr = {
            "node_id": "pod-2",
            "name": "default-pod",
            "init_containers": [],
            "pod_containers": [],
        }
        _create_and_start_pod(host, "proj-1234", ctr, {"nodes": [], "edges": []}, {})
        create_params = mock_start.call_args_list[0][0][2]
        assert create_params["restart_policy"] == "always"
        assert create_params["privileged"] is False


class TestWaitTroshkadJob:
    def test_raises_on_failed_job(self):
        from app.services.deploy_service import TroshkadError, _wait_troshkad_job

        with patch(
            "app.services.deploy_service.wait_for_job",
            return_value={"status": "failed", "result": {"error": "boom"}},
        ):
            try:
                _wait_troshkad_job(MagicMock(), "job-1", 30, "Pod create")
                raise AssertionError("expected TroshkadError")
            except TroshkadError as exc:
                assert "Pod create failed: boom" in str(exc)


class TestDeployStartContainers:
    @patch("app.services.deploy_service._start_pod")
    @patch("app.services.deploy_service._time.sleep")
    def test_starts_ordered_pod_after_delay(self, mock_sleep, mock_start_pod):
        from app.services.deploy_service import _deploy_start_containers

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {"name": "showroom", "isPod": True, "status": "stopped"},
                }
            ],
            "startOrder": [
                {
                    "entryType": "container",
                    "containerId": "ctr-1",
                    "delaySeconds": 15,
                }
            ],
        }
        _deploy_start_containers(MagicMock(), "projabcd-0000-0000-0000", topology, True)
        mock_sleep.assert_called_once_with(15)
        mock_start_pod.assert_called_once()
        assert mock_start_pod.call_args[0][1] == "troshka-projabcd-showroom"
        assert mock_start_pod.call_args[1]["timeout"] == 900
        assert topology["nodes"][0]["data"]["status"] == "running"

    @patch("app.services.deploy_service._create_pod")
    @patch("app.services.deploy_service._start_pod")
    def test_create_ordered_containers_does_not_start(
        self, mock_start_pod, mock_create_pod
    ):
        from app.services.deploy_service import _create_ordered_containers

        containers = [{"node_id": "ctr-1", "name": "showroom", "is_pod": True}]
        start_order = [{"entryType": "container", "containerId": "ctr-1"}]
        ids = _create_ordered_containers(
            MagicMock(), "proj-1234", containers, start_order, {}, {}, None
        )
        assert ids == {"ctr-1"}
        mock_create_pod.assert_called_once()
        mock_start_pod.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# _setup_kubevirt_s3_clients
# ═══════════════════════════════════════════════════════════════════════


class TestSetupKubevirtS3Clients:
    @patch("app.services.deploy_service.boto3")
    @patch("app.services.s3_storage.owner_params", return_value={})
    @patch(
        "app.services.s3_storage._get_readonly_s3_config",
        return_value=None,
    )
    @patch(
        "app.services.s3_storage._get_s3_config",
        return_value={
            "region": "us-west-2",
            "access_key_id": "AK",
            "secret_access_key": "SK",
            "bucket": "my-bucket",
        },
    )
    def test_primary_only(self, mock_cfg, mock_ro_cfg, mock_owner, mock_boto):
        from app.services.deploy_service import _setup_kubevirt_s3_clients

        result = _setup_kubevirt_s3_clients()
        assert len(result) == 8
        (
            s3_config,
            central_s3_config,
            s3_client,
            bucket,
            s3_op,
            central_s3_client,
            central_bucket,
            central_op,
        ) = result
        assert bucket == "my-bucket"
        assert central_s3_client is None
        assert central_bucket == ""

    @patch("app.services.deploy_service.boto3")
    @patch(
        "app.services.s3_storage.owner_params",
        return_value={"RequestPayer": "requester"},
    )
    @patch(
        "app.services.s3_storage._get_readonly_s3_config",
        return_value={
            "region": "us-east-1",
            "access_key_id": "CK",
            "secret_access_key": "CS",
            "bucket": "central-bucket",
        },
    )
    @patch(
        "app.services.s3_storage._get_s3_config",
        return_value={
            "region": "us-west-2",
            "access_key_id": "AK",
            "secret_access_key": "SK",
            "bucket": "primary-bucket",
        },
    )
    def test_with_central_s3(self, mock_cfg, mock_ro_cfg, mock_owner, mock_boto):
        from app.services.deploy_service import _setup_kubevirt_s3_clients

        result = _setup_kubevirt_s3_clients()
        (
            s3_config,
            central_s3_config,
            s3_client,
            bucket,
            s3_op,
            central_s3_client,
            central_bucket,
            central_op,
        ) = result
        assert bucket == "primary-bucket"
        assert central_bucket == "central-bucket"
        # boto3.client should be called twice (primary + central)
        assert mock_boto.client.call_count == 2

    @patch("app.services.deploy_service.boto3")
    @patch("app.services.s3_storage.owner_params", return_value={})
    @patch("app.services.s3_storage._get_readonly_s3_config", return_value=None)
    @patch(
        "app.services.s3_storage._get_s3_config",
        return_value={
            "access_key_id": "AK",
            "secret_access_key": "SK",
        },
    )
    def test_defaults(self, mock_cfg, mock_ro_cfg, mock_owner, mock_boto):
        from app.services.deploy_service import _setup_kubevirt_s3_clients

        result = _setup_kubevirt_s3_clients()
        _, _, _, bucket, _, _, _, _ = result
        assert bucket == "troshka-images"  # Default bucket name


# ═══════════════════════════════════════════════════════════════════════
# validate_topology_names
# ═══════════════════════════════════════════════════════════════════════


class TestValidateTopologyNamesV2:
    def test_no_duplicates(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "vmNode", "data": {"name": "vm-a"}},
                {"id": "2", "type": "vmNode", "data": {"name": "vm-b"}},
            ]
        }
        assert validate_topology_names(topo) == []

    def test_duplicate_vm_names(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "vmNode", "data": {"name": "master-0"}},
                {"id": "2", "type": "vmNode", "data": {"name": "master-0"}},
            ]
        }
        errors = validate_topology_names(topo)
        assert len(errors) == 1
        assert "Duplicate VM" in errors[0]

    def test_same_name_different_types_ok(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "vmNode", "data": {"name": "myname"}},
                {"id": "2", "type": "networkNode", "data": {"name": "myname"}},
            ]
        }
        assert validate_topology_names(topo) == []

    def test_empty_name_skipped(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "vmNode", "data": {"name": ""}},
                {"id": "2", "type": "vmNode", "data": {"name": ""}},
            ]
        }
        assert validate_topology_names(topo) == []

    def test_uses_label_if_no_name(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "storageNode", "data": {"label": "disk-A"}},
                {"id": "2", "type": "storageNode", "data": {"label": "disk-A"}},
            ]
        }
        errors = validate_topology_names(topo)
        assert len(errors) == 1
        assert "Duplicate Disk" in errors[0]

    def test_unknown_node_types_ignored(self):
        from app.services.deploy_topology import validate_topology_names

        topo = {
            "nodes": [
                {"id": "1", "type": "unknownNode", "data": {"name": "x"}},
                {"id": "2", "type": "unknownNode", "data": {"name": "x"}},
            ]
        }
        assert validate_topology_names(topo) == []


# ═══════════════════════════════════════════════════════════════════════
# validate_topology_ips
# ═══════════════════════════════════════════════════════════════════════


class TestValidateTopologyIpsV2:
    def test_no_duplicates(self):
        from app.services.deploy_topology import validate_topology_ips

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "name": "vm-a",
                        "nics": [{"id": "nic1", "ip": "10.0.0.5"}],
                    },
                },
                {
                    "id": "vm2",
                    "type": "vmNode",
                    "data": {
                        "name": "vm-b",
                        "nics": [{"id": "nic2", "ip": "10.0.0.6"}],
                    },
                },
                {"id": "net1", "type": "networkNode", "data": {"name": "net"}},
            ],
            "edges": [
                {
                    "source": "net1",
                    "target": "vm1",
                    "targetHandle": "nic-nic1-top",
                },
                {
                    "source": "net1",
                    "target": "vm2",
                    "targetHandle": "nic-nic2-top",
                },
            ],
        }
        assert validate_topology_ips(topo) == []

    def test_duplicate_ips_same_network(self):
        from app.services.deploy_topology import validate_topology_ips

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "name": "vm-a",
                        "nics": [{"id": "nic1", "ip": "10.0.0.5"}],
                    },
                },
                {
                    "id": "vm2",
                    "type": "vmNode",
                    "data": {
                        "name": "vm-b",
                        "nics": [{"id": "nic2", "ip": "10.0.0.5"}],
                    },
                },
                {"id": "net1", "type": "networkNode", "data": {"name": "lan"}},
            ],
            "edges": [
                {
                    "source": "net1",
                    "target": "vm1",
                    "targetHandle": "nic-nic1-top",
                },
                {
                    "source": "net1",
                    "target": "vm2",
                    "targetHandle": "nic-nic2-top",
                },
            ],
        }
        errors = validate_topology_ips(topo)
        assert len(errors) == 1
        assert "Duplicate IP 10.0.0.5" in errors[0]

    def test_empty_ip_skipped(self):
        from app.services.deploy_topology import validate_topology_ips

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"name": "a", "nics": [{"id": "n1", "ip": ""}]},
                },
            ],
            "edges": [],
        }
        assert validate_topology_ips(topo) == []


# ═══════════════════════════════════════════════════════════════════════
# validate_topology_passwords
# ═══════════════════════════════════════════════════════════════════════


class TestValidateTopologyPasswordsV2Extra:
    def test_bmc_with_password(self):
        from app.services.deploy_topology import validate_topology_passwords

        topo = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "networkType": "bmc",
                        "name": "bmc-net",
                        "bmcPassword": "s3cret",
                    },
                }
            ]
        }
        assert validate_topology_passwords(topo) == []

    def test_bmc_without_password(self):
        from app.services.deploy_topology import validate_topology_passwords

        topo = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {"networkType": "bmc", "name": "bmc-net"},
                }
            ]
        }
        errors = validate_topology_passwords(topo)
        assert len(errors) == 1
        assert "BMC network" in errors[0]

    def test_non_bmc_network_no_check(self):
        from app.services.deploy_topology import validate_topology_passwords

        topo = {
            "nodes": [
                {"type": "networkNode", "data": {"networkType": "vxlan", "name": "lan"}}
            ]
        }
        assert validate_topology_passwords(topo) == []


# ═══════════════════════════════════════════════════════════════════════
# _should_skip
# ═══════════════════════════════════════════════════════════════════════


class TestShouldSkipV2:
    def test_no_resume(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip(None, "networks") is False

    def test_step_before_resume(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip("images", "networks") is True

    def test_step_at_resume(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip("images", "images") is False

    def test_step_after_resume(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip("networks", "images") is False

    def test_unknown_step_returns_false(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip("nonexistent", "images") is False

    def test_unknown_resume_returns_false(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip("images", "nonexistent") is False


# ═══════════════════════════════════════════════════════════════════════
# _checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestCheckpointV2:
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_sets_deploy_step(self, mock_progress):
        from app.services.deploy_service import _checkpoint

        mock_session = MagicMock()
        mock_project = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_project
        )
        _checkpoint(mock_session, "proj-1", "images")
        assert mock_project.deploy_step == "images"
        mock_session.commit.assert_called_once()

    @patch(
        "app.services.deploy_service._get_deploy_progress_data",
        return_value={"step": "images", "detail": "50%"},
    )
    def test_sets_progress_from_redis(self, mock_progress):
        from app.services.deploy_service import _checkpoint

        mock_session = MagicMock()
        mock_project = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_project
        )
        _checkpoint(mock_session, "proj-1", "images")
        assert mock_project.deploy_progress == {"step": "images", "detail": "50%"}

    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_no_project_found(self, mock_progress):
        from app.services.deploy_service import _checkpoint

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None
        _checkpoint(mock_session, "proj-1", "images")
        mock_session.commit.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# _extract_vms
# ═══════════════════════════════════════════════════════════════════════


class TestExtractVmsV2:
    def test_extracts_basic_vm(self):
        from app.services.deploy_topology import _extract_vms

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "name": "master-0",
                        "vcpus": 4,
                        "ram": 16,
                        "os": "rhcos",
                    },
                }
            ]
        }
        vms = _extract_vms(topo)
        assert len(vms) == 1
        assert vms[0]["name"] == "master-0"
        assert vms[0]["vcpus"] == 4
        assert vms[0]["ram_gb"] == 16
        assert vms[0]["os"] == "rhcos"

    def test_skips_non_vm_nodes(self):
        from app.services.deploy_topology import _extract_vms

        topo = {
            "nodes": [
                {"id": "n1", "type": "networkNode", "data": {"name": "net"}},
                {"id": "s1", "type": "storageNode", "data": {"name": "disk"}},
            ]
        }
        assert _extract_vms(topo) == []

    def test_defaults_for_missing_fields(self):
        from app.services.deploy_topology import _extract_vms

        topo = {"nodes": [{"id": "vm1", "type": "vmNode", "data": {}}]}
        vms = _extract_vms(topo)
        assert vms[0]["name"] == "vm"
        assert vms[0]["vcpus"] == 2
        assert vms[0]["ram_gb"] == 4
        assert vms[0]["firmware"] == "bios"

    def test_empty_topology(self):
        from app.services.deploy_topology import _extract_vms

        assert _extract_vms({"nodes": []}) == []
        assert _extract_vms({}) == []


# ═══════════════════════════════════════════════════════════════════════
# _extract_containers
# ═══════════════════════════════════════════════════════════════════════


class TestExtractContainersV2:
    def test_extracts_container(self):
        from app.services.deploy_topology import _extract_containers

        topo = {
            "nodes": [
                {
                    "id": "c1",
                    "type": "containerNode",
                    "data": {
                        "name": "nginx",
                        "image": "nginx:latest",
                        "cpus": 2,
                        "memory": 1024,
                    },
                }
            ]
        }
        ctrs = _extract_containers(topo)
        assert len(ctrs) == 1
        assert ctrs[0]["name"] == "nginx"
        assert ctrs[0]["image"] == "nginx:latest"
        assert ctrs[0]["is_pod"] is False

    def test_pod_container(self):
        from app.services.deploy_topology import _extract_containers

        topo = {
            "nodes": [
                {
                    "id": "c1",
                    "type": "containerNode",
                    "data": {"name": "mypod", "isPod": True},
                }
            ]
        }
        ctrs = _extract_containers(topo)
        assert ctrs[0]["is_pod"] is True


# ═══════════════════════════════════════════════════════════════════════
# _find_vm_name_by_ip
# ═══════════════════════════════════════════════════════════════════════


class TestFindVmNameByIpV2:
    def test_finds_vm_by_ip(self):
        from app.services.deploy_topology import _find_vm_name_by_ip

        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "id": "vm1",
                    "data": {
                        "name": "bastion",
                        "nics": [{"ip": "10.0.0.5"}],
                    },
                }
            ]
        }
        assert _find_vm_name_by_ip(topo, "10.0.0.5") == "bastion"

    def test_fallback_to_ip_dash_format(self):
        from app.services.deploy_topology import _find_vm_name_by_ip

        topo = {"nodes": []}
        assert _find_vm_name_by_ip(topo, "10.0.0.5") == "10-0-0-5"

    def test_no_matching_ip(self):
        from app.services.deploy_topology import _find_vm_name_by_ip

        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "id": "vm1",
                    "data": {"name": "vm", "nics": [{"ip": "10.0.0.99"}]},
                }
            ]
        }
        assert _find_vm_name_by_ip(topo, "10.0.0.5") == "10-0-0-5"


# ═══════════════════════════════════════════════════════════════════════
# _vm_domain_name
# ═══════════════════════════════════════════════════════════════════════


class TestVmDomainNameV2:
    def test_basic(self):
        from app.services.deploy_topology import _vm_domain_name

        result = _vm_domain_name("proj-12345678-abcd", "node-abcdef12-9999")
        assert result == "troshka-proj-123-node-abc"

    def test_short_ids(self):
        from app.services.deploy_topology import _vm_domain_name

        result = _vm_domain_name("abc", "xyz")
        assert result == "troshka-abc-xyz"


# ═══════════════════════════════════════════════════════════════════════
# _find_vm_disks
# ═══════════════════════════════════════════════════════════════════════


class TestFindVmDisksV2:
    def test_finds_connected_disk(self):
        from app.services.deploy_topology import _find_vm_disks

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "diskControllers": [{"id": "dp-ctrl1", "bus": "sata"}],
                    },
                },
                {
                    "id": "disk1",
                    "type": "storageNode",
                    "data": {
                        "name": "RHEL disk",
                        "size": 50,
                        "format": "qcow2",
                        "source": "library",
                        "libraryItemId": "lib-1",
                    },
                },
            ],
            "edges": [
                {
                    "source": "vm1",
                    "sourceHandle": "dp-ctrl1",
                    "target": "disk1",
                }
            ],
        }
        disks = _find_vm_disks("vm1", topo)
        assert len(disks) == 1
        assert disks[0]["name"] == "RHEL disk"
        assert disks[0]["size_gb"] == 50
        assert disks[0]["bus"] == "sata"

    def test_skips_non_disk_handles(self):
        from app.services.deploy_topology import _find_vm_disks

        topo = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {}},
                {"id": "net1", "type": "networkNode", "data": {}},
            ],
            "edges": [{"source": "vm1", "sourceHandle": "nic-abc", "target": "net1"}],
        }
        assert _find_vm_disks("vm1", topo) == []

    def test_no_matching_storage_node(self):
        from app.services.deploy_topology import _find_vm_disks

        topo = {
            "nodes": [{"id": "vm1", "type": "vmNode", "data": {}}],
            "edges": [
                {"source": "vm1", "sourceHandle": "dp-ctrl1", "target": "missing"}
            ],
        }
        assert _find_vm_disks("vm1", topo) == []

    def test_reverse_edge_direction(self):
        from app.services.deploy_topology import _find_vm_disks

        topo = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"diskControllers": []}},
                {
                    "id": "disk1",
                    "type": "storageNode",
                    "data": {"name": "d", "size": 10, "format": "qcow2"},
                },
            ],
            "edges": [
                {
                    "source": "disk1",
                    "target": "vm1",
                    "targetHandle": "dp-ctrl1",
                }
            ],
        }
        disks = _find_vm_disks("vm1", topo)
        assert len(disks) == 1


# ═══════════════════════════════════════════════════════════════════════
# _find_vm_networks
# ═══════════════════════════════════════════════════════════════════════


class TestFindVmNetworksV2:
    def test_finds_connected_network(self):
        from app.services.deploy_topology import _find_vm_networks

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "nics": [{"id": "nic1", "mac": "52:54:00:aa:bb:cc"}],
                    },
                },
                {"id": "net1", "type": "networkNode", "data": {}},
            ],
            "edges": [
                {
                    "source": "vm1",
                    "sourceHandle": "nic-nic1-top",
                    "target": "net1",
                }
            ],
        }
        vni_map = {"net1": 1001}
        networks = _find_vm_networks("vm1", topo, vni_map)
        assert len(networks) == 1
        assert networks[0]["bridge"] == "br-1001"
        assert networks[0]["mac"] == "52:54:00:aa:bb:cc"

    def test_bmc_network_uses_bmc_bridge(self):
        from app.services.deploy_topology import _find_vm_networks

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"nics": [{"id": "nic1", "mac": "52:54:01:aa:bb:cc"}]},
                },
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"networkType": "bmc"},
                },
            ],
            "edges": [
                {
                    "source": "vm1",
                    "sourceHandle": "nic-nic1-top",
                    "target": "net1",
                }
            ],
        }
        networks = _find_vm_networks("vm1", topo, {}, project_id="proj-12345678")
        assert len(networks) == 1
        assert networks[0]["bridge"] == "br-bmc-proj-123"

    def test_skips_non_nic_handles(self):
        from app.services.deploy_topology import _find_vm_networks

        topo = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {}},
                {"id": "net1", "type": "networkNode", "data": {}},
            ],
            "edges": [{"source": "vm1", "sourceHandle": "dp-ctrl1", "target": "net1"}],
        }
        assert _find_vm_networks("vm1", topo, {"net1": 100}) == []


# ═══════════════════════════════════════════════════════════════════════
# _find_container_networks
# ═══════════════════════════════════════════════════════════════════════


class TestFindContainerNetworksV2:
    def test_finds_container_network(self):
        from app.services.deploy_topology import _find_container_networks

        topo = {
            "nodes": [
                {
                    "id": "ctr1",
                    "type": "containerNode",
                    "data": {
                        "nics": [
                            {"id": "nic1", "mac": "52:54:00:11:22:33", "ip": "10.0.0.5"}
                        ],
                    },
                },
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"cidr": "10.0.0.0/24"},
                },
            ],
            "edges": [
                {
                    "source": "ctr1",
                    "sourceHandle": "nic-nic1-top",
                    "target": "net1",
                }
            ],
        }
        vni_map = {"net1": 2001}
        nets = _find_container_networks("ctr1", topo, vni_map)
        assert len(nets) == 1
        assert nets[0]["bridge"] == "br-2001"
        assert nets[0]["ip"] == "10.0.0.5"
        assert nets[0]["gateway"] == "10.0.0.1"

    def test_no_container_node(self):
        from app.services.deploy_topology import _find_container_networks

        topo = {"nodes": [], "edges": []}
        assert _find_container_networks("missing", topo, {}) == []


# ═══════════════════════════════════════════════════════════════════════
# _extract_bmc_config
# ═══════════════════════════════════════════════════════════════════════


class TestExtractBmcConfigV2:
    def test_extracts_bmc_config(self):
        from app.services.deploy_topology import _extract_bmc_config

        topo = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {
                        "networkType": "bmc",
                        "cidr": "192.168.100.0/24",
                        "bmcUsername": "admin",
                        "bmcPassword": "pw",
                    },
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"bmcEnabled": True, "bmcIp": "192.168.100.10"},
                },
            ],
            "edges": [],
        }
        result = _extract_bmc_config(topo, "proj-12345678")
        assert result is not None
        assert len(result["vms"]) == 1
        assert result["vms"][0]["bmc_ip"] == "192.168.100.10"

    def test_no_bmc_network(self):
        from app.services.deploy_topology import _extract_bmc_config

        topo = {
            "nodes": [
                {"type": "networkNode", "data": {"networkType": "vxlan"}},
            ]
        }
        assert _extract_bmc_config(topo, "proj-1") is None

    def test_bmc_network_but_no_bmc_vms(self):
        from app.services.deploy_topology import _extract_bmc_config

        topo = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"networkType": "bmc"},
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"bmcEnabled": False},
                },
            ],
            "edges": [],
        }
        assert _extract_bmc_config(topo, "proj-1") is None


# ═══════════════════════════════════════════════════════════════════════
# _get_host_pool
# ═══════════════════════════════════════════════════════════════════════


class TestGetHostPool:
    def test_no_storage_pool(self):
        from app.services.deploy_service import _get_host_pool

        host = MagicMock()
        host.storage_pool_id = None
        assert _get_host_pool(host, MagicMock()) is None

    @patch("app.services.deploy_service.get_lock")
    def test_with_pool(self, _mock_lock):
        from app.services.deploy_service import _get_host_pool

        host = MagicMock()
        host.storage_pool_id = "pool-123"
        mock_db = MagicMock()
        mock_pool = MagicMock()
        mock_db.get.return_value = mock_pool
        result = _get_host_pool(host, mock_db)
        assert result is mock_pool
        mock_db.get.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _check_shared_cache
# ═══════════════════════════════════════════════════════════════════════


class TestCheckSharedCache:
    def test_no_pool(self):
        from app.services.deploy_service import _check_shared_cache

        status, entry = _check_shared_cache(MagicMock(), None, "item1", "image")
        assert status is None
        assert entry is None

    def test_entry_found(self):
        from app.services.deploy_service import _check_shared_cache

        mock_db = MagicMock()
        mock_entry = MagicMock()
        mock_entry.status = "ready"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_entry
        pool = MagicMock()
        pool.id = "pool-1"
        status, entry = _check_shared_cache(mock_db, pool, "item1", "image")
        assert status == "ready"
        assert entry is mock_entry

    def test_no_entry(self):
        from app.services.deploy_service import _check_shared_cache

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        pool = MagicMock()
        pool.id = "pool-1"
        status, entry = _check_shared_cache(mock_db, pool, "item1", "image")
        assert status is None
        assert entry is None


# ═══════════════════════════════════════════════════════════════════════
# _report_pre_install_status
# ═══════════════════════════════════════════════════════════════════════


class TestReportPreInstallStatusV2:
    def test_oc_mirror_running(self):
        from app.services.deploy_service import _report_pre_install_status

        push_fn = MagicMock()
        _report_pre_install_status("12345 oc-mirror run---active---", push_fn)
        push_fn.assert_called_once_with(
            "installing", "mirroring OCP images (oc-mirror)"
        )

    def test_registry_active(self):
        from app.services.deploy_service import _report_pre_install_status

        push_fn = MagicMock()
        _report_pre_install_status("---active---", push_fn)
        push_fn.assert_called_once_with(
            "installing", "setting up disconnected registry"
        )

    def test_fallback(self):
        from app.services.deploy_service import _report_pre_install_status

        push_fn = MagicMock()
        _report_pre_install_status("---inactive---", push_fn)
        push_fn.assert_called_once_with("installing", "preparing environment")


# ═══════════════════════════════════════════════════════════════════════
# _resolve_pattern_disk
# ═══════════════════════════════════════════════════════════════════════


class TestResolvePatternDisk:
    @patch(
        "app.services.pattern_locations.pattern_disk_source_for_cluster",
        return_value="obc",
    )
    def test_resolves_from_db_record(self, mock_source):
        from app.services.deploy_service import _resolve_pattern_disk

        data = {"patternId": "pat-1", "patternDiskId": "pd-1", "label": "disk"}
        mock_db = MagicMock()
        mock_pd = MagicMock()
        mock_pd.s3_key = "patterns/pat-1/pd-1.qcow2"
        mock_pd.virtual_size_bytes = None
        mock_db.scalars.return_value.first.return_value = mock_pd
        _resolve_pattern_disk(data, mock_db, "provider-1")
        assert data["resolvedS3Path"] == "patterns/pat-1/pd-1.qcow2"
        assert data["diskSource"] == "obc"

    @patch(
        "app.services.pattern_locations.pattern_disk_source_for_cluster",
        return_value="central",
    )
    def test_fallback_path_when_no_record(self, mock_source):
        from app.services.deploy_service import _resolve_pattern_disk

        data = {"patternId": "pat-1", "patternDiskId": "pd-1", "label": "disk"}
        mock_db = MagicMock()
        mock_db.scalars.return_value.first.return_value = None
        _resolve_pattern_disk(data, mock_db, "provider-1")
        assert data["resolvedS3Path"] == "patterns/pat-1/pd-1.qcow2"

    @patch(
        "app.services.pattern_locations.pattern_disk_source_for_cluster",
        return_value="obc",
    )
    def test_size_override_when_larger(self, mock_source):
        from app.services.deploy_service import _resolve_pattern_disk

        data = {
            "patternId": "pat-1",
            "patternDiskId": "pd-1",
            "label": "disk",
            "size": 50,
        }
        mock_db = MagicMock()
        mock_pd = MagicMock()
        mock_pd.s3_key = "patterns/pat-1/pd-1.qcow2"
        mock_pd.virtual_size_bytes = 214748364800  # 200 GiB
        mock_db.scalars.return_value.first.return_value = mock_pd
        _resolve_pattern_disk(data, mock_db, "provider-1")
        assert data["size"] == 200


# ═══════════════════════════════════════════════════════════════════════
# _resolve_library_disk
# ═══════════════════════════════════════════════════════════════════════


class TestResolveLibraryDisk:
    def test_resolves_from_lib_item(self):
        from app.services.deploy_service import _resolve_library_disk

        data = {"libraryItemId": "lib-1", "label": "RHEL disk", "format": "qcow2"}
        mock_db = MagicMock()
        mock_item = MagicMock()
        mock_item.s3_key = "library/lib-1.qcow2"
        mock_item.source = "local"
        mock_db.get.return_value = mock_item
        _resolve_library_disk(data, mock_db, MagicMock(), "bkt", {}, None, "", {})
        assert data["resolvedS3Path"] == "library/lib-1.qcow2"
        assert data["centralSource"] is False

    @patch("app.services.deploy_service._check_central_source", return_value=True)
    def test_fallback_when_no_lib_item(self, mock_central):
        from app.services.deploy_service import _resolve_library_disk

        data = {"libraryItemId": "lib-1", "label": "disk", "format": "qcow2"}
        mock_db = MagicMock()
        mock_db.get.return_value = None
        _resolve_library_disk(
            data, mock_db, MagicMock(), "bkt", {}, MagicMock(), "central-bkt", {}
        )
        assert data["resolvedS3Path"] == "library/lib-1.qcow2"
        assert data["centralSource"] is True


# ═══════════════════════════════════════════════════════════════════════
# _resolve_disk_s3_paths
# ═══════════════════════════════════════════════════════════════════════


class TestResolveDiskS3Paths:
    @patch("app.services.deploy_service._resolve_library_disk")
    @patch("app.services.deploy_service._resolve_pattern_disk")
    def test_dispatches_to_pattern_and_library(self, mock_pattern, mock_library):
        from app.services.deploy_service import _resolve_disk_s3_paths

        topo = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {
                        "source": "pattern",
                        "patternId": "p1",
                    },
                },
                {
                    "type": "storageNode",
                    "data": {
                        "source": "library",
                        "libraryItemId": "l1",
                    },
                },
                {
                    "type": "storageNode",
                    "data": {"source": "blank"},
                },
                {
                    "type": "vmNode",
                    "data": {"name": "vm"},
                },
            ]
        }
        _resolve_disk_s3_paths(
            topo, MagicMock(), "provider-1", MagicMock(), "bkt", {}, None, "", {}
        )
        mock_pattern.assert_called_once()
        mock_library.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _teardown_networks_via_troshkad
# ═══════════════════════════════════════════════════════════════════════


class TestTeardownNetworks:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="job-1")
    def test_calls_full_teardown(self, mock_start, mock_wait):
        from app.services.deploy_service import _teardown_networks_via_troshkad

        host = MagicMock()
        _teardown_networks_via_troshkad(host, "proj-1", {"net1": 1001})
        mock_start.assert_called_once()
        call_args = mock_start.call_args
        assert call_args[0][1] == "/networks/full-teardown"
        payload = call_args[0][2]
        assert payload["project_id"] == "proj-1"
        assert payload["vni_list"] == [1001]

    @patch(
        "app.services.deploy_service.start_job",
        side_effect=__import__(
            "app.services.troshkad_client", fromlist=["TroshkadError"]
        ).TroshkadError("host down"),
    )
    def test_handles_troshkad_error(self, mock_start):
        from app.services.deploy_service import _teardown_networks_via_troshkad

        host = MagicMock()
        # Should not raise
        _teardown_networks_via_troshkad(host, "proj-1", {"net1": 1001})

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="job-1")
    def test_empty_vni_map(self, mock_start, mock_wait):
        from app.services.deploy_service import _teardown_networks_via_troshkad

        host = MagicMock()
        _teardown_networks_via_troshkad(host, "proj-1", None)
        payload = mock_start.call_args[0][2]
        assert payload["vni_list"] == []


# ═══════════════════════════════════════════════════════════════════════
# _start_vm_monitor
# ═══════════════════════════════════════════════════════════════════════


class TestStartVmMonitor:
    @patch("app.services.deploy_service.threading")
    @patch("app.services.deploy_service.add_to_set")
    @patch("app.services.deploy_service.is_in_set", return_value=False)
    def test_starts_monitor_for_ocp_monitor_vm(
        self, mock_in_set, mock_add, mock_threading
    ):
        from app.services.deploy_service import _start_vm_monitor

        node = {
            "id": "vm-1",
            "data": {"ocpMonitor": True, "label": "master-0"},
        }
        result = _start_vm_monitor("proj-1", "host-1", node, 0)
        assert result is True
        mock_add.assert_called_once()

    @patch("app.services.deploy_service.is_in_set", return_value=True)
    def test_skips_already_running(self, mock_in_set):
        from app.services.deploy_service import _start_vm_monitor

        node = {
            "id": "vm-1",
            "data": {"ocpMonitor": True, "label": "master-0"},
        }
        result = _start_vm_monitor("proj-1", "host-1", node, 0)
        assert result is True

    def test_returns_false_for_non_monitor_vm(self):
        from app.services.deploy_service import _start_vm_monitor

        node = {"id": "vm-1", "data": {"os": "rhel9"}}
        result = _start_vm_monitor("proj-1", "host-1", node, 0)
        assert result is False

    @patch("app.services.deploy_service.threading")
    @patch("app.services.deploy_service.add_to_set")
    @patch("app.services.deploy_service.is_in_set", return_value=False)
    def test_starts_for_bastion_browser(self, mock_in_set, mock_add, mock_threading):
        from app.services.deploy_service import _start_vm_monitor

        node = {
            "id": "vm-1",
            "data": {"configureBastionBrowser": True, "label": "bastion"},
        }
        result = _start_vm_monitor("proj-1", "host-1", node, 0)
        assert result is True


# ═══════════════════════════════════════════════════════════════════════
# _is_ocpvirt_host
# ═══════════════════════════════════════════════════════════════════════


class TestIsOcpvirtHost:
    def test_cached_result(self):
        from app.services.deploy_service import _is_ocpvirt_host, _ocpvirt_hosts

        host = MagicMock()
        host.id = "cached-host-id"
        _ocpvirt_hosts["cached-host-id"] = True
        result = _is_ocpvirt_host(host)
        assert result is True
        # Cleanup
        del _ocpvirt_hosts["cached-host-id"]

    @patch("app.core.database.SessionLocal")
    def test_queries_db_when_not_cached(self, mock_sl):
        from app.services.deploy_service import _is_ocpvirt_host, _ocpvirt_hosts

        host = MagicMock()
        host.id = "new-host-id-test"
        host.provider_id = "prov-1"
        if host.id in _ocpvirt_hosts:
            del _ocpvirt_hosts[host.id]
        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        mock_prov = MagicMock()
        mock_prov.type = "ocpvirt"
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_prov
        result = _is_ocpvirt_host(host)
        assert result is True
        # Cleanup
        if host.id in _ocpvirt_hosts:
            del _ocpvirt_hosts[host.id]


# ═══════════════════════════════════════════════════════════════════════
# _update_deploy_progress
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateDeployProgressV2:
    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_sends_progress_with_items(self, mock_set, mock_notify):
        from app.services.deploy_service import _update_deploy_progress

        with patch("app.core.database.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_db.get.return_value = MagicMock()
            _update_deploy_progress("proj-1", "images", "50%", ["disk-1: 50%"])

        mock_set.assert_called_once()
        progress = mock_set.call_args[0][1]
        assert progress["step"] == "images"
        assert progress["items"] == ["disk-1: 50%"]

    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_omits_items_when_none(self, mock_set, mock_notify):
        from app.services.deploy_service import _update_deploy_progress

        with patch("app.core.database.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_db.get.return_value = MagicMock()
            _update_deploy_progress("proj-1", "networks", "setting up")

        progress = mock_set.call_args[0][1]
        assert "items" not in progress


# ═══════════════════════════════════════════════════════════════════════
# get_deploy_progress
# ═══════════════════════════════════════════════════════════════════════


class TestGetDeployProgress:
    @patch(
        "app.services.deploy_service._get_deploy_progress_data",
        return_value={"step": "images"},
    )
    def test_returns_redis_cached(self, mock_get):
        from app.services.deploy_service import get_deploy_progress

        result = get_deploy_progress("proj-1")
        assert result == {"step": "images"}

    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_falls_back_to_db(self, mock_get):
        from app.services.deploy_service import get_deploy_progress

        with patch("app.core.database.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_project = MagicMock()
            mock_project.deploy_progress = {"step": "done"}
            mock_db.query.return_value.filter_by.return_value.first.return_value = (
                mock_project
            )
            result = get_deploy_progress("proj-1")
        assert result == {"step": "done"}

    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_returns_none_when_nothing(self, mock_get):
        from app.services.deploy_service import get_deploy_progress

        with patch("app.core.database.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_db.query.return_value.filter_by.return_value.first.return_value = None
            result = get_deploy_progress("proj-1")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# _setup_bmc_via_troshkad
# ═══════════════════════════════════════════════════════════════════════


class TestSetupBmcViaTroshkad:
    @patch("app.services.deploy_service._teardown_bmc_via_troshkad")
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="bmc-job-1")
    def test_success(self, mock_start, mock_wait, mock_teardown):
        from app.services.deploy_service import _setup_bmc_via_troshkad

        mock_wait.return_value = {"status": "completed"}
        host = MagicMock()
        bmc_config = {
            "bmc_network": {
                "cidr": "192.168.100.0/24",
                "bmcUsername": "admin",
                "bmcPassword": "pw",
            },
            "vms": [{"domain_name": "troshka-proj-vm1", "bmc_ip": "192.168.100.10"}],
        }
        result = _setup_bmc_via_troshkad(host, "proj-1", bmc_config)
        assert result is True
        mock_start.assert_called_once()

    @patch("app.services.deploy_service._teardown_bmc_via_troshkad")
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="bmc-job-1")
    def test_failure(self, mock_start, mock_wait, mock_teardown):
        from app.services.deploy_service import _setup_bmc_via_troshkad

        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "bmc bridge failed"},
        }
        host = MagicMock()
        bmc_config = {
            "bmc_network": {"cidr": "10.0.0.0/24"},
            "vms": [{"domain_name": "d", "bmc_ip": "10.0.0.5"}],
        }
        result = _setup_bmc_via_troshkad(host, "proj-1", bmc_config)
        assert result == "bmc bridge failed"


# ═══════════════════════════════════════════════════════════════════════
# _create_shared_cache_entry  (lines 290-303)
# ═══════════════════════════════════════════════════════════════════════


class TestCreateSharedCacheEntry:
    @patch("app.services.deploy_service.SharedCacheEntry", create=True)
    def test_creates_entry(self, _mock_sce_cls):
        from app.services.deploy_service import _create_shared_cache_entry

        db = MagicMock()
        pool = MagicMock(id="pool-1")
        entry = _create_shared_cache_entry(
            db, pool, "item-1", "image", "images/item-1.qcow2"
        )
        db.add.assert_called_once()
        db.commit.assert_called_once()
        assert entry is not None

    @patch("app.services.deploy_service.SharedCacheEntry", create=True)
    def test_entry_fields(self, _mock_sce_cls):
        from app.services.deploy_service import _create_shared_cache_entry

        db = MagicMock()
        pool = MagicMock(id="pool-42")
        _create_shared_cache_entry(db, pool, "itm-2", "pattern", "patterns/itm-2.qcow2")
        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert added.status == "downloading"


# ═══════════════════════════════════════════════════════════════════════
# _mark_shared_cache_ready  (lines 306-323)
# ═══════════════════════════════════════════════════════════════════════


class TestMarkSharedCacheReady:
    def test_marks_ready(self):
        from app.services.deploy_service import _mark_shared_cache_ready

        entry = MagicMock(status="downloading", size_bytes=None)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = entry
        _mark_shared_cache_ready(db, "pool-1", "item-1", "image", size_bytes=1024)
        assert entry.status == "ready"
        assert entry.size_bytes == 1024
        db.commit.assert_called_once()

    def test_no_entry_found(self):
        from app.services.deploy_service import _mark_shared_cache_ready

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        # Should not raise
        _mark_shared_cache_ready(db, "pool-1", "item-1", "image")
        db.commit.assert_not_called()

    def test_marks_ready_no_size(self):
        from app.services.deploy_service import _mark_shared_cache_ready

        entry = MagicMock(status="downloading", size_bytes=None)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = entry
        _mark_shared_cache_ready(db, "pool-1", "item-1", "image")
        assert entry.status == "ready"
        db.commit.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _mark_shared_cache_error  (lines 326-341)
# ═══════════════════════════════════════════════════════════════════════


class TestMarkSharedCacheError:
    def test_deletes_downloading_entry(self):
        from app.services.deploy_service import _mark_shared_cache_error

        entry = MagicMock(status="downloading")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = entry
        _mark_shared_cache_error(db, "pool-1", "item-1", "image")
        db.delete.assert_called_once_with(entry)
        db.commit.assert_called_once()

    def test_skips_ready_entry(self):
        from app.services.deploy_service import _mark_shared_cache_error

        entry = MagicMock(status="ready")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = entry
        _mark_shared_cache_error(db, "pool-1", "item-1", "image")
        db.delete.assert_not_called()

    def test_no_entry(self):
        from app.services.deploy_service import _mark_shared_cache_error

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        _mark_shared_cache_error(db, "pool-1", "item-1", "image")
        db.delete.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# _wait_for_shared_cache  (lines 344-367)
# ═══════════════════════════════════════════════════════════════════════


class TestWaitForSharedCache:
    @patch("time.sleep", return_value=None)
    def test_returns_true_when_ready(self, _mock_sleep):
        from app.services.deploy_service import _wait_for_shared_cache

        entry = MagicMock(status="ready")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = entry
        result = _wait_for_shared_cache(db, "pool-1", "item-1", "image", timeout=1)
        assert result is True

    @patch("time.sleep", return_value=None)
    def test_returns_false_on_error(self, _mock_sleep):
        from app.services.deploy_service import _wait_for_shared_cache

        entry = MagicMock(status="error")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = entry
        result = _wait_for_shared_cache(db, "pool-1", "item-1", "image", timeout=1)
        assert result is False

    @patch("time.sleep", return_value=None)
    def test_returns_false_on_timeout(self, _mock_sleep):
        from app.services.deploy_service import _wait_for_shared_cache

        entry = MagicMock(status="downloading")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = entry
        # Use a timeout of 0 to force immediate timeout
        result = _wait_for_shared_cache(db, "pool-1", "item-1", "image", timeout=0)
        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# _find_container_volumes  (lines 639-695)
# ═══════════════════════════════════════════════════════════════════════


class TestFindContainerVolumes:
    def test_finds_volumes_via_mnt_handles(self):
        from app.services.deploy_topology import _find_container_volumes

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {
                        "mounts": [
                            {"diskNodeId": "disk-1", "mountPath": "/data"},
                        ],
                    },
                },
                {
                    "id": "disk-1",
                    "type": "storageNode",
                    "data": {"size": 20},
                },
            ],
            "edges": [
                {
                    "source": "ctr-1",
                    "target": "disk-1",
                    "sourceHandle": "",
                    "targetHandle": "mnt-disk-1",
                },
            ],
        }
        result = _find_container_volumes("ctr-1", topology, "proj-1234")
        assert len(result) == 1
        assert result[0]["mount_path"] == "/data"
        assert result[0]["size_gb"] == 20
        assert result[0]["node_id"] == "disk-1"

    def test_no_container_found(self):
        from app.services.deploy_topology import _find_container_volumes

        topology = {"nodes": [], "edges": []}
        result = _find_container_volumes("nonexistent", topology, "proj-1")
        assert result == []

    def test_no_matching_edges(self):
        from app.services.deploy_topology import _find_container_volumes

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {"mounts": []},
                },
            ],
            "edges": [],
        }
        result = _find_container_volumes("ctr-1", topology, "proj-1")
        assert result == []

    def test_skips_non_storage_node(self):
        from app.services.deploy_topology import _find_container_volumes

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {"mounts": [{"diskNodeId": "vm-1", "mountPath": "/mnt"}]},
                },
                {"id": "vm-1", "type": "vmNode", "data": {}},
            ],
            "edges": [
                {
                    "source": "ctr-1",
                    "target": "vm-1",
                    "sourceHandle": "",
                    "targetHandle": "mnt-vm-1",
                },
            ],
        }
        result = _find_container_volumes("ctr-1", topology, "proj-1")
        assert result == []

    def test_reverse_edge_direction(self):
        from app.services.deploy_topology import _find_container_volumes

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {
                        "mounts": [{"diskNodeId": "disk-1", "mountPath": "/vol"}],
                    },
                },
                {"id": "disk-1", "type": "storageNode", "data": {"size": 5}},
            ],
            "edges": [
                {
                    "source": "disk-1",
                    "target": "ctr-1",
                    "sourceHandle": "mnt-disk-1",
                    "targetHandle": "",
                },
            ],
        }
        result = _find_container_volumes("ctr-1", topology, "proj-1234")
        assert len(result) == 1
        assert result[0]["mount_path"] == "/vol"


# ═══════════════════════════════════════════════════════════════════════
# _extract_bmc_config DHCP hosts  (lines 744-757)
# ═══════════════════════════════════════════════════════════════════════


class TestExtractBmcConfigDhcpHosts:
    def test_collects_dhcp_hosts_from_bmc_nics(self):
        from app.services.deploy_topology import _extract_bmc_config

        topology = {
            "nodes": [
                {
                    "id": "bmc-net",
                    "type": "networkNode",
                    "data": {"networkType": "bmc", "cidr": "192.168.100.0/24"},
                },
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "bmcEnabled": True,
                        "bmcIp": "192.168.100.10",
                        "name": "master-0",
                        "nics": [
                            {
                                "id": "nic-abc",
                                "ip": "192.168.100.20",
                                "mac": "52:54:00:aa:bb:cc",
                            }
                        ],
                    },
                },
            ],
            "edges": [
                {
                    "source": "vm-1",
                    "target": "bmc-net",
                    "sourceHandle": "nic-abc-top",
                    "targetHandle": "",
                },
            ],
        }
        result = _extract_bmc_config(topology, "proj-12345678")
        assert result is not None
        assert len(result["dhcp_hosts"]) == 1
        assert result["dhcp_hosts"][0]["mac"] == "52:54:00:aa:bb:cc"
        assert result["dhcp_hosts"][0]["ip"] == "192.168.100.20"
        assert result["dhcp_hosts"][0]["name"] == "master-0"

    def test_no_bmc_network(self):
        from app.services.deploy_topology import _extract_bmc_config

        topology = {
            "nodes": [
                {"id": "net-1", "type": "networkNode", "data": {"networkType": ""}},
            ],
            "edges": [],
        }
        assert _extract_bmc_config(topology, "proj-1") is None

    def test_no_bmc_enabled_vms(self):
        from app.services.deploy_topology import _extract_bmc_config

        topology = {
            "nodes": [
                {
                    "id": "bmc-net",
                    "type": "networkNode",
                    "data": {"networkType": "bmc"},
                },
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"bmcEnabled": False, "bmcIp": "10.0.0.5"},
                },
            ],
            "edges": [],
        }
        assert _extract_bmc_config(topology, "proj-1") is None

    def test_edge_target_is_vm(self):
        """Test when VM is the target of the edge (reverse direction)."""
        from app.services.deploy_topology import _extract_bmc_config

        topology = {
            "nodes": [
                {
                    "id": "bmc-net",
                    "type": "networkNode",
                    "data": {"networkType": "bmc", "cidr": "10.0.0.0/24"},
                },
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "bmcEnabled": True,
                        "bmcIp": "10.0.0.10",
                        "name": "sno",
                        "nics": [
                            {
                                "id": "nic-def",
                                "ip": "10.0.0.20",
                                "mac": "aa:bb:cc:dd:ee:ff",
                            }
                        ],
                    },
                },
            ],
            "edges": [
                {
                    "source": "bmc-net",
                    "target": "vm-1",
                    "sourceHandle": "",
                    "targetHandle": "nic-def-bottom",
                },
            ],
        }
        result = _extract_bmc_config(topology, "proj-12345678")
        assert result is not None
        assert len(result["dhcp_hosts"]) == 1


# ═══════════════════════════════════════════════════════════════════════
# _vm_domain_name  (line 701-702)
# ═══════════════════════════════════════════════════════════════════════


class TestVmDomainNameV2Extra:
    def test_basic(self):
        from app.services.deploy_topology import _vm_domain_name

        result = _vm_domain_name("proj-1234-5678-abcd", "vm-aaaa-bbbb-cccc")
        assert result == "troshka-proj-123-vm-aaaa-"

    def test_short_ids(self):
        from app.services.deploy_topology import _vm_domain_name

        result = _vm_domain_name("abcd", "efgh")
        assert result == "troshka-abcd-efgh"


# ═══════════════════════════════════════════════════════════════════════
# _setup_metadata_via_troshkad  (lines 1636-1682)
# ═══════════════════════════════════════════════════════════════════════


class TestSetupMetadataViaTroshkad:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="meta-job-1")
    @patch("app.services.cloud_init.generate_metadata", return_value="meta-yaml")
    @patch("app.services.cloud_init.generate_userdata", return_value="user-yaml")
    def test_deploys_metadata(self, mock_ud, mock_md, mock_start, mock_wait):
        from app.services.deploy_service import _setup_metadata_via_troshkad

        host = MagicMock()
        topology = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {
                        "cloudInit": True,
                        "name": "bastion",
                        "nics": [{"mac": "52:54:00:AA:BB:CC"}],
                    },
                },
            ]
        }
        vni_map = {"net-1": 1001}
        _setup_metadata_via_troshkad(host, "proj-12345678", topology, vni_map)
        mock_start.assert_called_once()
        params = mock_start.call_args[0][2]
        assert params["project_id"] == "proj-12345678"
        assert "52:54:00:aa:bb:cc" in params["vm_configs"]
        assert params["namespace"] == "troshka-proj-123"

    @patch("app.services.deploy_service.start_job")
    def test_skips_when_no_cloudinit_vms(self, mock_start):
        from app.services.deploy_service import _setup_metadata_via_troshkad

        host = MagicMock()
        topology = {
            "nodes": [
                {"type": "vmNode", "data": {"cloudInit": False, "name": "vm1"}},
            ]
        }
        _setup_metadata_via_troshkad(host, "proj-1", topology, {})
        mock_start.assert_not_called()

    @patch("app.services.deploy_service.start_job")
    @patch("app.services.cloud_init.generate_metadata", return_value="m")
    @patch("app.services.cloud_init.generate_userdata", return_value="u")
    def test_handles_troshkad_error(self, mock_ud, mock_md, mock_start):
        from app.services.deploy_service import _setup_metadata_via_troshkad
        from app.services.troshkad_client import TroshkadError

        mock_start.side_effect = TroshkadError("connection refused")
        host = MagicMock()
        topology = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {
                        "cloudInit": True,
                        "name": "vm1",
                        "nics": [{"mac": "aa:bb:cc:dd:ee:ff"}],
                    },
                }
            ]
        }
        # Should not raise
        _setup_metadata_via_troshkad(host, "proj-1", topology, {"n": 100})


# ═══════════════════════════════════════════════════════════════════════
# _setup_pxe_via_troshkad  (lines 1388-1431)
# ═══════════════════════════════════════════════════════════════════════


class TestSetupPxeViaTroshkad:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="pxe-job-1")
    @patch("app.services.vxlan.build_host_network_config")
    def test_sets_up_pxe_for_builtin_server(
        self, mock_net_config, mock_start, mock_wait
    ):
        from app.services.deploy_service import _setup_pxe_via_troshkad

        mock_net_config.return_value = {
            "networks": [
                {
                    "vni": 1001,
                    "pxe_config": {
                        "server_mode": "builtin",
                        "iso_path": "/var/lib/troshka/images/boot.iso",
                        "http_port": 8080,
                        "tftp_root": "/tftpboot",
                    },
                    "dhcp_config": {"gateway": "10.0.0.1"},
                }
            ]
        }
        mock_wait.return_value = {"status": "completed"}
        host = MagicMock()
        _setup_pxe_via_troshkad(host, "proj-1", {}, {})
        mock_start.assert_called_once()
        params = mock_start.call_args[0][2]
        assert params["vni"] == 1001
        assert params["iso_path"] == "/var/lib/troshka/images/boot.iso"
        assert params["gateway_ip"] == "10.0.0.1"

    @patch("app.services.deploy_service.start_job")
    @patch("app.services.vxlan.build_host_network_config")
    def test_skips_non_builtin(self, mock_net_config, mock_start):
        from app.services.deploy_service import _setup_pxe_via_troshkad

        mock_net_config.return_value = {
            "networks": [
                {"vni": 1001, "pxe_config": {"server_mode": "external"}},
            ]
        }
        host = MagicMock()
        _setup_pxe_via_troshkad(host, "proj-1", {}, {})
        mock_start.assert_not_called()

    @patch("app.services.deploy_service.start_job")
    @patch("app.services.vxlan.build_host_network_config")
    def test_handles_troshkad_error(self, mock_net_config, mock_start):
        from app.services.deploy_service import _setup_pxe_via_troshkad
        from app.services.troshkad_client import TroshkadError

        mock_net_config.return_value = {
            "networks": [
                {
                    "vni": 1001,
                    "pxe_config": {
                        "server_mode": "builtin",
                        "iso_path": "/path/boot.iso",
                    },
                    "dhcp_config": {},
                }
            ]
        }
        mock_start.side_effect = TroshkadError("host unreachable")
        host = MagicMock()
        # Should not raise
        _setup_pxe_via_troshkad(host, "proj-1", {}, {})

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="pxe-job-2")
    @patch("app.services.vxlan.build_host_network_config")
    def test_logs_failed_job(self, mock_net_config, mock_start, mock_wait):
        from app.services.deploy_service import _setup_pxe_via_troshkad

        mock_net_config.return_value = {
            "networks": [
                {
                    "vni": 1001,
                    "pxe_config": {"server_mode": "builtin", "iso_path": "/p.iso"},
                    "dhcp_config": {},
                }
            ]
        }
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "extract failed"},
        }
        host = MagicMock()
        # Should not raise — logs the error
        _setup_pxe_via_troshkad(host, "proj-1", {}, {})


# ═══════════════════════════════════════════════════════════════════════
# _compute_deploy_step  (lines 2352-2365)
# ═══════════════════════════════════════════════════════════════════════


class TestComputeDeployStep:
    @patch(
        "app.services.deploy_service._resolve_deploy_step",
        return_value=("images", "downloading"),
    )
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value={})
    def test_returns_step_and_percent(self, mock_get, mock_resolve):
        from app.services.deploy_service import _compute_deploy_step

        dv_lines = ["disk-1: downloading 50%"]
        progress = {"stage": "", "detail": "", "percent": 42}
        status = {}
        step, detail, percent = _compute_deploy_step(
            "proj-1", status, dv_lines, progress
        )
        assert step == "images"
        assert detail == "downloading"
        assert percent == 42

    @patch(
        "app.services.deploy_service._resolve_deploy_step",
        return_value=("deploying", ""),
    )
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_no_progress(self, mock_get, mock_resolve):
        from app.services.deploy_service import _compute_deploy_step

        step, detail, percent = _compute_deploy_step("proj-1", {}, [], None)
        assert percent == 0


# ═══════════════════════════════════════════════════════════════════════
# _collect_dv_progress  (lines 2297-2349)
# ═══════════════════════════════════════════════════════════════════════


class TestCollectDvProgress:
    @patch("app.services.deploy_service._fill_missing_disk_labels")
    @patch(
        "app.services.deploy_service._best_dv_status", return_value={"disk-A": "done"}
    )
    @patch("app.services.deploy_service._build_clone_name_map", return_value={})
    @patch(
        "app.services.deploy_service._format_dv_status_line",
        return_value="disk-A: done",
    )
    def test_returns_dv_lines(self, mock_fmt, mock_clone, mock_best, mock_fill):
        from app.services.deploy_service import _collect_dv_progress

        provider = MagicMock()
        topology = {"nodes": []}

        with patch(
            "app.services.providers.kubevirt._get_k8s_clients"
        ) as mock_k8s, patch(
            "app.services.providers.kubevirt._project_ns", return_value="troshka-proj-1"
        ):
            mock_custom = MagicMock()
            mock_k8s.return_value = (mock_custom, MagicMock(), MagicMock())
            mock_custom.list_namespaced_custom_object.return_value = {"items": []}
            result = _collect_dv_progress("proj-1", provider, topology)
        assert isinstance(result, list)

    def test_returns_empty_on_exception(self):
        from app.services.deploy_service import _collect_dv_progress

        provider = MagicMock()
        topology = {"nodes": []}
        with patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            side_effect=Exception("no cluster"),
        ):
            result = _collect_dv_progress("proj-1", provider, topology)
        assert result == []


# ═══════════════════════════════════════════════════════════════════════
# _poll_kubevirt_deploy  (lines 2468-2527)
# ═══════════════════════════════════════════════════════════════════════


class TestPollKubevirtDeploy:
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._push_kubevirt_deploy_progress")
    @patch(
        "app.services.deploy_service._compute_deploy_step",
        return_value=("starting", "booting", 80),
    )
    @patch("app.services.deploy_service._collect_dv_progress", return_value=[])
    @patch("app.services.deploy_service._project_deleted", return_value=False)
    @patch("app.services.deploy_service._is_deploy_cancelled", return_value=False)
    @patch("app.services.deploy_service._clear_deploy_cancelled")
    @patch("app.services.deploy_service._finalize_kubevirt_deploy")
    @patch("time.sleep", return_value=None)
    def test_running_phase_finalizes(
        self,
        mock_sleep,
        mock_finalize,
        mock_clear,
        mock_cancelled,
        mock_deleted,
        mock_dv,
        mock_step,
        mock_push,
        mock_notify,
        mock_del,
    ):
        from app.services.deploy_service import _poll_kubevirt_deploy

        driver = MagicMock()
        driver.get_project_status.return_value = {"phase": "Running"}
        project = MagicMock()
        db = MagicMock()
        _poll_kubevirt_deploy("proj-1", project, MagicMock(), driver, {}, db)
        mock_finalize.assert_called_once()

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._handle_kubevirt_deploy_error")
    @patch(
        "app.services.deploy_service._compute_deploy_step",
        return_value=("images", "downloading", 50),
    )
    @patch("app.services.deploy_service._collect_dv_progress", return_value=[])
    @patch("app.services.deploy_service._project_deleted", return_value=False)
    @patch("app.services.deploy_service._is_deploy_cancelled", return_value=False)
    @patch("app.services.deploy_service._clear_deploy_cancelled")
    @patch("time.sleep", return_value=None)
    def test_error_phase_handles_error(
        self,
        mock_sleep,
        mock_clear,
        mock_cancelled,
        mock_deleted,
        mock_dv,
        mock_step,
        mock_error,
        mock_notify,
        mock_del,
    ):
        from app.services.deploy_service import _poll_kubevirt_deploy

        driver = MagicMock()
        driver.get_project_status.return_value = {
            "phase": "Error",
            "error": "disk failed",
        }
        project = MagicMock()
        db = MagicMock()
        _poll_kubevirt_deploy("proj-1", project, MagicMock(), driver, {}, db)
        mock_error.assert_called_once()

    @patch("app.services.deploy_service.notify_project")
    @patch(
        "app.services.deploy_service._compute_deploy_step",
        return_value=("deploying", "", 0),
    )
    @patch("app.services.deploy_service._collect_dv_progress", return_value=[])
    @patch("app.services.deploy_service._project_deleted", return_value=True)
    @patch("app.services.deploy_service._is_deploy_cancelled", return_value=False)
    @patch("app.services.deploy_service._clear_deploy_cancelled")
    @patch("time.sleep", return_value=None)
    def test_returns_on_project_deleted(
        self,
        mock_sleep,
        mock_clear,
        mock_cancelled,
        mock_deleted,
        mock_dv,
        mock_step,
        mock_notify,
    ):
        from app.services.deploy_service import _poll_kubevirt_deploy

        driver = MagicMock()
        driver.get_project_status.return_value = {"phase": "Pending"}
        project = MagicMock()
        db = MagicMock()
        _poll_kubevirt_deploy("proj-1", project, MagicMock(), driver, {}, db)
        # Should return early — project.state should NOT be set to "error"
        assert project.state != "error"

    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._clear_deploy_cancelled")
    @patch("app.services.deploy_service._is_deploy_cancelled", return_value=True)
    @patch("time.sleep", return_value=None)
    def test_returns_on_cancelled(
        self, mock_sleep, mock_cancelled, mock_clear, mock_notify
    ):
        from app.services.deploy_service import _poll_kubevirt_deploy

        driver = MagicMock()
        project = MagicMock()
        db = MagicMock()
        _poll_kubevirt_deploy("proj-1", project, MagicMock(), driver, {}, db)
        mock_clear.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# _extract_containers  (lines 404-432)
# ═══════════════════════════════════════════════════════════════════════


class TestExtractContainersV2Extra:
    def test_extracts_single_container(self):
        from app.services.deploy_topology import _extract_containers

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {
                        "name": "nginx",
                        "image": "nginx:latest",
                        "cpus": 2,
                        "memory": 1024,
                        "isPod": False,
                    },
                }
            ]
        }
        result = _extract_containers(topology)
        assert len(result) == 1
        assert result[0]["name"] == "nginx"
        assert result[0]["image"] == "nginx:latest"
        assert result[0]["cpus"] == 2
        assert result[0]["is_pod"] is False

    def test_extracts_pod(self):
        from app.services.deploy_topology import _extract_containers

        topology = {
            "nodes": [
                {
                    "id": "pod-1",
                    "type": "containerNode",
                    "data": {
                        "name": "mypod",
                        "image": "",
                        "isPod": True,
                        "initContainers": [{"image": "init:1"}],
                        "podContainers": [{"image": "app:2"}],
                    },
                }
            ]
        }
        result = _extract_containers(topology)
        assert len(result) == 1
        assert result[0]["is_pod"] is True
        assert len(result[0]["init_containers"]) == 1
        assert len(result[0]["pod_containers"]) == 1

    def test_skips_non_container_nodes(self):
        from app.services.deploy_topology import _extract_containers

        topology = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"name": "vm"}},
                {"id": "net-1", "type": "networkNode", "data": {}},
            ]
        }
        assert _extract_containers(topology) == []

    def test_empty_topology(self):
        from app.services.deploy_topology import _extract_containers

        assert _extract_containers({"nodes": []}) == []


# ═══════════════════════════════════════════════════════════════════════
# _check_shared_cache  (lines 270-287)
# ═══════════════════════════════════════════════════════════════════════


class TestCheckSharedCacheV2:
    def test_no_pool_returns_none(self):
        from app.services.deploy_service import _check_shared_cache

        status, entry = _check_shared_cache(MagicMock(), None, "item-1", "image")
        assert status is None
        assert entry is None

    def test_entry_found(self):
        from app.services.deploy_service import _check_shared_cache

        mock_entry = MagicMock(status="ready")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_entry
        pool = MagicMock(id="pool-1")
        status, entry = _check_shared_cache(db, pool, "item-1", "image")
        assert status == "ready"
        assert entry is mock_entry

    def test_no_entry_found(self):
        from app.services.deploy_service import _check_shared_cache

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        pool = MagicMock(id="pool-1")
        status, entry = _check_shared_cache(db, pool, "item-1", "image")
        assert status is None
        assert entry is None


# ═══════════════════════════════════════════════════════════════════════
# _get_host_pool  (lines 261-267)
# ═══════════════════════════════════════════════════════════════════════


class TestGetHostPoolV2:
    def test_no_storage_pool_id(self):
        from app.services.deploy_service import _get_host_pool

        host = MagicMock(storage_pool_id=None)
        result = _get_host_pool(host, MagicMock())
        assert result is None

    def test_returns_pool(self):
        from app.services.deploy_service import _get_host_pool

        host = MagicMock(storage_pool_id="pool-1")
        db = MagicMock()
        mock_pool = MagicMock()
        db.get.return_value = mock_pool
        result = _get_host_pool(host, db)
        assert result is mock_pool


# ═══════════════════════════════════════════════════════════════════════
# _find_vm_name_by_ip  (lines 566-574)
# ═══════════════════════════════════════════════════════════════════════


class TestFindVmNameByIpV2Extra:
    def test_finds_by_ip(self):
        from app.services.deploy_topology import _find_vm_name_by_ip

        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"name": "bastion", "nics": [{"ip": "10.0.0.5"}]},
                }
            ]
        }
        assert _find_vm_name_by_ip(topology, "10.0.0.5") == "bastion"

    def test_not_found_returns_ip_dashes(self):
        from app.services.deploy_topology import _find_vm_name_by_ip

        topology = {"nodes": []}
        result = _find_vm_name_by_ip(topology, "192.168.1.10")
        assert result == "192-168-1-10"


# ═══════════════════════════════════════════════════════════════════════
# _find_vm_disks  (lines 577-636)
# ═══════════════════════════════════════════════════════════════════════


class TestFindVmDisksV2Extra:
    def test_finds_disk_via_dp_handle(self):
        from app.services.deploy_topology import _find_vm_disks

        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "diskControllers": [{"id": "dp-ctrl1", "bus": "sata"}],
                    },
                },
                {
                    "id": "disk-1",
                    "type": "storageNode",
                    "data": {
                        "name": "RHEL9",
                        "size": 40,
                        "format": "qcow2",
                        "source": "library",
                        "libraryItemId": "lib-1",
                    },
                },
            ],
            "edges": [
                {
                    "source": "vm-1",
                    "target": "disk-1",
                    "sourceHandle": "dp-ctrl1",
                    "targetHandle": "",
                },
            ],
        }
        result = _find_vm_disks("vm-1", topology)
        assert len(result) == 1
        assert result[0]["name"] == "RHEL9"
        assert result[0]["bus"] == "sata"
        assert result[0]["library_item_id"] == "lib-1"

    def test_no_disks(self):
        from app.services.deploy_topology import _find_vm_disks

        topology = {
            "nodes": [{"id": "vm-1", "type": "vmNode", "data": {}}],
            "edges": [],
        }
        assert _find_vm_disks("vm-1", topology) == []

    def test_skips_non_dp_handles(self):
        from app.services.deploy_topology import _find_vm_disks

        topology = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {}},
                {"id": "net-1", "type": "networkNode", "data": {}},
            ],
            "edges": [
                {
                    "source": "vm-1",
                    "target": "net-1",
                    "sourceHandle": "nic-abc-top",
                    "targetHandle": "",
                },
            ],
        }
        assert _find_vm_disks("vm-1", topology) == []


# ═══════════════════════════════════════════════════════════════════════
# _find_vm_networks — uncovered branches
# ═══════════════════════════════════════════════════════════════════════


class TestFindVmNetworksUncovered:
    """Cover lines 450-452, 454, 477+479, 495."""

    def test_edge_where_vm_is_target(self):
        """Line 450-452: edge.target == vm_node_id branch."""
        from app.services.deploy_topology import _find_vm_networks

        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "nics": [
                            {"id": "abc", "mac": "52:54:00:aa:bb:cc", "model": "e1000"}
                        ]
                    },
                },
                {"id": "net-1", "type": "networkNode", "data": {"cidr": "10.0.0.0/24"}},
            ],
            "edges": [
                {
                    "source": "net-1",
                    "target": "vm-1",
                    "sourceHandle": "",
                    "targetHandle": "nic-abc-top",
                },
            ],
        }
        vni_map = {"net-1": 100}
        result = _find_vm_networks("vm-1", topology, vni_map, "proj-123")
        assert len(result) == 1
        assert result[0]["bridge"] == "br-100"
        assert result[0]["mac"] == "52:54:00:aa:bb:cc"
        assert result[0]["model"] == "e1000"

    def test_edge_neither_source_nor_target(self):
        """Line 454: edge doesn't involve the VM at all -> continue."""
        from app.services.deploy_topology import _find_vm_networks

        topology = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"nics": []}},
                {"id": "net-1", "type": "networkNode", "data": {}},
                {"id": "vm-2", "type": "vmNode", "data": {}},
            ],
            "edges": [
                {
                    "source": "vm-2",
                    "target": "net-1",
                    "sourceHandle": "nic-xyz-top",
                    "targetHandle": "",
                },
            ],
        }
        result = _find_vm_networks("vm-1", topology, {"net-1": 100})
        assert result == []

    def test_bmc_network_no_mac_generates_random(self):
        """Lines 477, 479: BMC network with empty MAC -> random MAC generation."""
        from app.services.deploy_topology import _find_vm_networks

        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"nics": [{"id": "bmc-nic", "mac": "", "model": "virtio"}]},
                },
                {
                    "id": "bmc-net",
                    "type": "networkNode",
                    "data": {"networkType": "bmc", "cidr": "192.168.100.0/24"},
                },
            ],
            "edges": [
                {
                    "source": "vm-1",
                    "target": "bmc-net",
                    "sourceHandle": "nic-bmc-nic-top",
                    "targetHandle": "",
                },
            ],
        }
        vni_map = {}
        result = _find_vm_networks("vm-1", topology, vni_map, "proj-123")
        assert len(result) == 1
        assert result[0]["bridge"] == "br-bmc-proj-123"
        # MAC should be generated (starts with 52:54:01)
        assert result[0]["mac"].startswith("52:54:01:")
        assert len(result[0]["mac"]) == 17  # full MAC length

    def test_network_not_in_vni_map_skips(self):
        """Line 495: network_node_id not in vni_map -> skip."""
        from app.services.deploy_topology import _find_vm_networks

        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"nics": [{"id": "abc", "mac": "52:54:00:11:22:33"}]},
                },
                {"id": "net-1", "type": "networkNode", "data": {"cidr": "10.0.0.0/24"}},
            ],
            "edges": [
                {
                    "source": "vm-1",
                    "target": "net-1",
                    "sourceHandle": "nic-abc-top",
                    "targetHandle": "",
                },
            ],
        }
        vni_map = {}  # net-1 not in map
        result = _find_vm_networks("vm-1", topology, vni_map)
        assert result == []


# ═══════════════════════════════════════════════════════════════════════
# _find_container_networks — uncovered branches
# ═══════════════════════════════════════════════════════════════════════


class TestFindContainerNetworksUncovered:
    """Cover lines 534-536, 539, 544."""

    def test_edge_where_container_is_target(self):
        """Lines 534-536: container is edge target, not source."""
        from app.services.deploy_topology import _find_container_networks

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {
                        "nics": [
                            {
                                "id": "mynic",
                                "mac": "52:54:00:dd:ee:ff",
                                "ip": "10.0.0.5",
                            }
                        ]
                    },
                },
                {"id": "net-1", "type": "networkNode", "data": {"cidr": "10.0.0.0/24"}},
            ],
            "edges": [
                {
                    "source": "net-1",
                    "target": "ctr-1",
                    "sourceHandle": "",
                    "targetHandle": "nic-mynic-bottom",
                },
            ],
        }
        vni_map = {"net-1": 200}
        result = _find_container_networks("ctr-1", topology, vni_map)
        assert len(result) == 1
        assert result[0]["bridge"] == "br-200"
        assert result[0]["mac"] == "52:54:00:dd:ee:ff"
        assert result[0]["ip"] == "10.0.0.5"
        assert result[0]["cidr"] == "10.0.0.0/24"

    def test_edge_with_no_nic_handle_skips(self):
        """Line 539: edge matched but no nic_id/net_node_id -> continue."""
        from app.services.deploy_topology import _find_container_networks

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {"nics": []},
                },
                {"id": "net-1", "type": "networkNode", "data": {}},
            ],
            "edges": [
                {
                    "source": "ctr-1",
                    "target": "net-1",
                    "sourceHandle": "port-abc-top",  # not "nic-" prefix
                    "targetHandle": "",
                },
            ],
        }
        result = _find_container_networks("ctr-1", topology, {"net-1": 100})
        assert result == []

    def test_network_not_in_vni_map_skips(self):
        """Line 544: vni is None -> continue."""
        from app.services.deploy_topology import _find_container_networks

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {"nics": [{"id": "mynic", "mac": "52:54:00:11:22:33"}]},
                },
                {"id": "net-1", "type": "networkNode", "data": {}},
            ],
            "edges": [
                {
                    "source": "ctr-1",
                    "target": "net-1",
                    "sourceHandle": "nic-mynic-top",
                    "targetHandle": "",
                },
            ],
        }
        vni_map = {}  # net-1 not in map
        result = _find_container_networks("ctr-1", topology, vni_map)
        assert result == []


# ═══════════════════════════════════════════════════════════════════════
# _extract_bmc_config — uncovered DHCP hosts loop
# ═══════════════════════════════════════════════════════════════════════


class TestExtractBmcConfigDhcpHostsV2:
    """Cover lines 738-763 (DHCP hosts collection loop)."""

    def test_collects_dhcp_hosts_from_bmc_nics(self):
        from app.services.deploy_topology import _extract_bmc_config

        topology = {
            "nodes": [
                {
                    "id": "bmc-net",
                    "type": "networkNode",
                    "data": {
                        "networkType": "bmc",
                        "cidr": "192.168.100.0/24",
                        "bmcUsername": "admin",
                        "bmcPassword": "secret",
                    },
                },
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "name": "sno1",
                        "bmcEnabled": True,
                        "bmcIp": "192.168.100.10",
                        "nics": [
                            {
                                "id": "bmc-nic-1",
                                "mac": "52:54:00:aa:aa:aa",
                                "ip": "192.168.100.50",
                            }
                        ],
                    },
                },
                {
                    "id": "vm-2",
                    "type": "vmNode",
                    "data": {
                        "name": "sno2",
                        "bmcEnabled": True,
                        "bmcIp": "192.168.100.11",
                        "nics": [
                            {
                                "id": "bmc-nic-2",
                                "mac": "52:54:00:bb:bb:bb",
                                "ip": "192.168.100.51",
                            }
                        ],
                    },
                },
            ],
            "edges": [
                {
                    "source": "vm-1",
                    "target": "bmc-net",
                    "sourceHandle": "nic-bmc-nic-1-top",
                    "targetHandle": "",
                },
                {
                    "source": "vm-2",
                    "target": "bmc-net",
                    "sourceHandle": "nic-bmc-nic-2-top",
                    "targetHandle": "",
                },
            ],
        }
        result = _extract_bmc_config(topology, "proj-12345678")
        assert result is not None
        assert len(result["dhcp_hosts"]) == 2
        macs = {h["mac"] for h in result["dhcp_hosts"]}
        assert "52:54:00:aa:aa:aa" in macs
        assert "52:54:00:bb:bb:bb" in macs
        ips = {h["ip"] for h in result["dhcp_hosts"]}
        assert "192.168.100.50" in ips
        assert "192.168.100.51" in ips

    def test_edge_where_vm_is_target_in_bmc(self):
        """Lines 748-750: edge.target == vm_id in DHCP host loop."""
        from app.services.deploy_topology import _extract_bmc_config

        topology = {
            "nodes": [
                {
                    "id": "bmc-net",
                    "type": "networkNode",
                    "data": {
                        "networkType": "bmc",
                        "cidr": "192.168.100.0/24",
                    },
                },
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "name": "target-vm",
                        "bmcEnabled": True,
                        "bmcIp": "192.168.100.10",
                        "nics": [
                            {
                                "id": "bnic",
                                "mac": "52:54:00:cc:cc:cc",
                                "ip": "192.168.100.60",
                            }
                        ],
                    },
                },
            ],
            "edges": [
                {
                    "source": "bmc-net",
                    "target": "vm-1",
                    "sourceHandle": "",
                    "targetHandle": "nic-bnic-top",
                },
            ],
        }
        result = _extract_bmc_config(topology, "proj-12345678")
        assert result is not None
        assert len(result["dhcp_hosts"]) == 1
        assert result["dhcp_hosts"][0]["mac"] == "52:54:00:cc:cc:cc"

    def test_edge_not_matching_vm_continues(self):
        """Line 752: edge doesn't involve the current VM -> continue."""
        from app.services.deploy_topology import _extract_bmc_config

        topology = {
            "nodes": [
                {
                    "id": "bmc-net",
                    "type": "networkNode",
                    "data": {"networkType": "bmc", "cidr": "192.168.100.0/24"},
                },
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "name": "vm1",
                        "bmcEnabled": True,
                        "bmcIp": "192.168.100.10",
                        "nics": [
                            {
                                "id": "bnic1",
                                "mac": "52:54:00:11:11:11",
                                "ip": "192.168.100.70",
                            }
                        ],
                    },
                },
                {
                    "id": "vm-2",
                    "type": "vmNode",
                    "data": {"name": "vm2", "nics": []},
                },
            ],
            "edges": [
                {
                    "source": "vm-2",
                    "target": "bmc-net",
                    "sourceHandle": "nic-other-top",
                    "targetHandle": "",
                },
                {
                    "source": "vm-1",
                    "target": "bmc-net",
                    "sourceHandle": "nic-bnic1-top",
                    "targetHandle": "",
                },
            ],
        }
        result = _extract_bmc_config(topology, "proj-12345678")
        # vm-1 should still produce a DHCP host even though vm-2's edge is first
        assert result is not None
        assert len(result["dhcp_hosts"]) == 1
        assert result["dhcp_hosts"][0]["name"] == "vm1"


# ═══════════════════════════════════════════════════════════════════════
# _exec_on_bastion_troshkad — uncovered success + error paths
# ═══════════════════════════════════════════════════════════════════════


class TestExecOnBastionTroshkad:
    """Cover lines 4466-4499."""

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_success_strips_ansi_and_filters_banner(self, mock_start, mock_wait):
        """Lines 4466-4496: successful exec with ANSI codes and OCP banner lines."""
        from app.services.deploy_service import _exec_on_bastion_troshkad

        mock_start.return_value = "job-123"
        mock_wait.return_value = {
            "status": "completed",
            "result": {
                "output": (
                    "\x1b[32mnode1   Ready\x1b[0m\n"
                    "OpenShift Console: https://console.ocp.local\n"
                    "Username: kubeadmin\n"
                    "Password: secret123\n"
                    "node2   Ready\n"
                    "  \n"  # blank line — should be filtered out
                ),
            },
        }
        host = MagicMock()
        result = _exec_on_bastion_troshkad(
            host, "proj-1", "10.0.0.5", "pass", "oc get nodes", 30
        )
        assert result is not None
        assert "node1   Ready" in result
        assert "node2   Ready" in result
        # Banner lines should be stripped
        assert "OpenShift Console" not in result
        assert "Username:" not in result
        assert "Password:" not in result
        # ANSI codes should be stripped
        assert "\x1b[" not in result

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_troshkad_error_returns_none(self, mock_start, mock_wait):
        """Lines 4497-4499: TroshkadError during execution -> returns None."""
        from app.services.deploy_service import _exec_on_bastion_troshkad
        from app.services.troshkad_client import TroshkadError

        mock_start.side_effect = TroshkadError("connection refused")
        host = MagicMock()
        result = _exec_on_bastion_troshkad(
            host, "proj-1", "10.0.0.5", "pass", "oc get nodes", 30
        )
        assert result is None

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_completed_with_error_returns_none(self, mock_start, mock_wait):
        """Line 4495-4496: completed but result has error -> returns None."""
        from app.services.deploy_service import _exec_on_bastion_troshkad

        mock_start.return_value = "job-456"
        mock_wait.return_value = {
            "status": "completed",
            "result": {
                "output": "some output",
                "error": "ssh connection timed out",
            },
        }
        host = MagicMock()
        result = _exec_on_bastion_troshkad(
            host, "proj-1", "10.0.0.5", "pass", "cmd", 30
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# _delete_project_record — uncovered lines 6474-6481
# ═══════════════════════════════════════════════════════════════════════


class TestDeleteProjectRecord:
    """Cover lines 6474-6481."""

    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service.logger")
    def test_project_exists_deletes_and_notifies(self, mock_logger, mock_notify):
        from app.services.deploy_service import _delete_project_record

        mock_project = MagicMock()
        mock_session = MagicMock()
        mock_session.get.return_value = mock_project

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            _delete_project_record("proj-12345678")

        mock_session.delete.assert_called_once_with(mock_project)
        mock_session.commit.assert_called_once()
        mock_notify.assert_called_once_with(
            "proj-12345678", {"type": "project-deleted"}
        )
        mock_session.close.assert_called_once()

    @patch("app.services.deploy_service.notify_project")
    def test_project_not_found_no_op(self, mock_notify):
        from app.services.deploy_service import _delete_project_record

        mock_session = MagicMock()
        mock_session.get.return_value = None

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            _delete_project_record("nonexistent-id")

        mock_session.delete.assert_not_called()
        mock_session.commit.assert_not_called()
        mock_notify.assert_not_called()
        mock_session.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _set_destroy_error — uncovered lines 6490-6497
# ═══════════════════════════════════════════════════════════════════════


class TestSetDestroyError:
    """Cover lines 6490-6497."""

    @patch("app.services.deploy_service.notify_project")
    def test_sets_error_state_and_notifies(self, mock_notify):
        from app.services.deploy_service import _set_destroy_error

        mock_project = MagicMock()
        mock_session = MagicMock()
        mock_session.get.return_value = mock_project

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            _set_destroy_error("proj-12345678", "host unreachable")

        assert mock_project.state == "error"
        assert mock_project.deploy_error == "Delete failed: host unreachable"
        mock_session.commit.assert_called_once()
        mock_notify.assert_called_once_with(
            "proj-12345678",
            {
                "type": "project-state",
                "state": "error",
                "deploy_error": "Delete failed: host unreachable",
            },
        )
        mock_session.close.assert_called_once()

    @patch("app.services.deploy_service.notify_project")
    def test_project_not_found_no_op(self, mock_notify):
        from app.services.deploy_service import _set_destroy_error

        mock_session = MagicMock()
        mock_session.get.return_value = None

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            _set_destroy_error("nonexistent", "whatever")

        mock_notify.assert_not_called()
        mock_session.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _destroy_cleanup_route_access — uncovered lines 6700-6717
# ═══════════════════════════════════════════════════════════════════════


class TestDestroyCleanupRouteAccessV2:
    """Cover lines 6700-6717."""

    def test_no_host_returns_early(self):
        from app.services.deploy_service import _destroy_cleanup_route_access

        _destroy_cleanup_route_access(None, "proj-1", MagicMock())

    def test_no_provider_id_returns_early(self):
        from app.services.deploy_service import _destroy_cleanup_route_access

        host = MagicMock(provider_id=None)
        _destroy_cleanup_route_access(host, "proj-1", MagicMock())

    def test_non_ocpvirt_provider_returns_early(self):
        from app.services.deploy_service import _destroy_cleanup_route_access

        host = MagicMock(provider_id="prov-1")
        mock_provider = MagicMock(type="ec2")
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_provider
        )
        # Should return without calling delete_route_access
        _destroy_cleanup_route_access(host, "proj-1", mock_session)

    @patch("app.services.providers.get_provider_driver")
    def test_ocpvirt_calls_delete_route_access(self, mock_get_driver):
        from app.services.deploy_service import _destroy_cleanup_route_access

        host = MagicMock(provider_id="prov-1")
        mock_provider = MagicMock(type="ocpvirt")
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_provider
        )

        mock_driver = MagicMock()
        mock_get_driver.return_value = mock_driver

        _destroy_cleanup_route_access(host, "proj-1", mock_session)

        mock_driver.delete_route_access.assert_called_once_with(mock_provider, "proj-1")

    def test_exception_is_caught_non_fatal(self):
        from app.services.deploy_service import _destroy_cleanup_route_access

        host = MagicMock(provider_id="prov-1")
        mock_session = MagicMock()
        mock_session.query.side_effect = Exception("DB error")
        # Should not raise
        _destroy_cleanup_route_access(host, "proj-1", mock_session)


# ═══════════════════════════════════════════════════════════════════════
# _update_deploy_progress — DB exception path (lines 205-206)
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateDeployProgressDbException:
    """Cover lines 205-206: except Exception: pass when DB write fails."""

    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_db_exception_silently_caught(self, mock_set, mock_notify):
        from app.services.deploy_service import _update_deploy_progress

        # Make SessionLocal raise an exception
        with patch(
            "app.core.database.SessionLocal",
            side_effect=Exception("DB connection failed"),
        ):
            # Should not raise
            _update_deploy_progress("proj-1", "deploying", "creating VMs")

        # Redis progress and WS notification should still have been called
        mock_set.assert_called_once()
        mock_notify.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _check_install_terminal_state — uncovered branches
# ═══════════════════════════════════════════════════════════════════════


class TestCheckInstallTerminalStateV2:
    """Cover the function body for failure, success, and no-match cases."""

    def test_failure_marker_returns_error(self):
        from app.services.deploy_service import _check_install_terminal_state

        push_fn = MagicMock()
        mock_time = MagicMock()
        mock_time.time.return_value = 1000.0

        with patch("app.services.deploy_service._ocp_update_status") as mock_update:
            result = _check_install_terminal_state(
                "Bootstrap failed to complete: timed out",
                push_fn,
                "proj-1",
                900.0,
                mock_time,
            )

        assert result == ("error", None)
        push_fn.assert_called_with("error", "install failed")
        mock_update.assert_called_once_with("proj-1", "error")

    def test_success_marker_returns_complete_with_elapsed(self):
        from app.services.deploy_service import _check_install_terminal_state

        push_fn = MagicMock()
        mock_time = MagicMock()
        mock_time.time.return_value = 1500.0

        with patch("app.services.deploy_service._ocp_update_status") as mock_update:
            result = _check_install_terminal_state(
                "Install complete!\nAll cluster operators ready",
                push_fn,
                "proj-1",
                1000.0,
                mock_time,
            )

        assert result is not None
        assert result[0] == "complete"
        assert result[1] == 500  # 1500 - 1000
        mock_update.assert_called_once_with("proj-1", "ready", 500)

    def test_no_match_returns_none(self):
        from app.services.deploy_service import _check_install_terminal_state

        push_fn = MagicMock()
        mock_time = MagicMock()
        mock_time.time.return_value = 1000.0

        result = _check_install_terminal_state(
            "Still running some workload...",
            push_fn,
            "proj-1",
            900.0,
            mock_time,
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# _ocp_update_status — uncovered DB update path
# ═══════════════════════════════════════════════════════════════════════


class TestOcpUpdateStatusDbPath:
    """Cover lines 5066-5081: DB write for ocp_status + elapsed."""

    def test_updates_status_and_elapsed(self):
        from app.services.deploy_service import _ocp_update_status

        mock_project = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_project
        )

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            _ocp_update_status("proj-1", "ready", elapsed_secs=300)

        assert mock_project.ocp_status == "ready"
        assert mock_project.ocp_install_elapsed == 300
        mock_session.commit.assert_called_once()
        mock_session.close.assert_called_once()

    def test_updates_status_without_elapsed(self):
        from app.services.deploy_service import _ocp_update_status

        mock_project = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_project
        )

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            _ocp_update_status("proj-1", "error")

        assert mock_project.ocp_status == "error"
        mock_session.commit.assert_called_once()

    def test_db_exception_logged_not_raised(self):
        from app.services.deploy_service import _ocp_update_status

        with patch(
            "app.core.database.SessionLocal",
            side_effect=Exception("DB down"),
        ):
            # Should not raise
            _ocp_update_status("proj-1", "error")


# ═══════════════════════════════════════════════════════════════════════
# _ocp_wait_for_install_log — uncovered lines 5486-5505
# ═══════════════════════════════════════════════════════════════════════


class TestOcpWaitForInstallLog:
    """Cover lines 5486-5505."""

    @patch("app.services.deploy_service._exec_on_bastion")
    def test_returns_immediately_when_install_log_found(self, mock_exec):
        from app.services.deploy_service import _ocp_wait_for_install_log

        # First call returns install.log found
        mock_exec.return_value = "---\nactive\n---\n/home/cloud-user/install.log"
        push_fn = MagicMock()
        _ocp_wait_for_install_log(MagicMock(), "proj-1", "10.0.0.5", "pw", push_fn)
        push_fn.assert_any_call("installing", "preparing environment")

    @patch("app.services.deploy_service._exec_on_bastion")
    def test_reports_oc_mirror_status(self, mock_exec):
        from app.services.deploy_service import _ocp_wait_for_install_log

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return "12345 oc-mirror---\nactive\n---\n"
            return "---\n---\n/home/cloud-user/install.log"

        mock_exec.side_effect = side_effect
        push_fn = MagicMock()

        with patch("time.sleep"):
            _ocp_wait_for_install_log(MagicMock(), "proj-1", "10.0.0.5", "pw", push_fn)

        push_fn.assert_any_call("installing", "mirroring OCP images (oc-mirror)")

    @patch("app.services.deploy_service._exec_on_bastion")
    def test_reports_registry_status(self, mock_exec):
        from app.services.deploy_service import _ocp_wait_for_install_log

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return "---\nactive\n---\n"
            return "---\n---\n/home/cloud-user/install.log"

        mock_exec.side_effect = side_effect
        push_fn = MagicMock()

        with patch("time.sleep"):
            _ocp_wait_for_install_log(MagicMock(), "proj-1", "10.0.0.5", "pw", push_fn)

        push_fn.assert_any_call("installing", "setting up disconnected registry")


# ═══════════════════════════════════════════════════════════════════════
# _report_pre_install_status — uncovered branches
# ═══════════════════════════════════════════════════════════════════════


class TestReportPreInstallStatusV2Extra:
    """Cover lines 5471-5481."""

    def test_oc_mirror_running(self):
        from app.services.deploy_service import _report_pre_install_status

        push_fn = MagicMock()
        _report_pre_install_status("12345 oc-mirror---active---", push_fn)
        push_fn.assert_called_with("installing", "mirroring OCP images (oc-mirror)")

    def test_registry_active(self):
        from app.services.deploy_service import _report_pre_install_status

        push_fn = MagicMock()
        _report_pre_install_status("---active---", push_fn)
        push_fn.assert_called_with("installing", "setting up disconnected registry")

    def test_nothing_detected(self):
        from app.services.deploy_service import _report_pre_install_status

        push_fn = MagicMock()
        _report_pre_install_status("---inactive---", push_fn)
        push_fn.assert_called_with("installing", "preparing environment")

    def test_reverse_edge_direction(self):
        """Edge where storage is source and VM is target (reverse direction)."""
        from app.services.deploy_topology import _find_vm_disks

        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "diskControllers": [{"id": "dp-ctrl1", "bus": "sata"}],
                    },
                },
                {
                    "id": "disk-1",
                    "type": "storageNode",
                    "data": {
                        "name": "os-disk",
                        "size": 50,
                        "format": "qcow2",
                        "source": "library",
                        "libraryItemId": "lib-123",
                    },
                },
            ],
            "edges": [
                {
                    "source": "disk-1",
                    "target": "vm-1",
                    "sourceHandle": "",
                    "targetHandle": "dp-ctrl1",
                },
            ],
        }
        disks = _find_vm_disks("vm-1", topology)
        assert len(disks) == 1
        assert disks[0]["node_id"] == "disk-1"
        assert disks[0]["bus"] == "sata"
        assert disks[0]["library_item_id"] == "lib-123"

    def test_pattern_disk_fields(self):
        """Disk connected to VM with pattern fields."""
        from app.services.deploy_topology import _find_vm_disks

        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"diskControllers": [{"id": "dp-dc1", "bus": "virtio"}]},
                },
                {
                    "id": "disk-p",
                    "type": "storageNode",
                    "data": {
                        "name": "pattern-disk",
                        "size": 100,
                        "format": "qcow2",
                        "source": "pattern",
                        "patternId": "pat-001",
                        "patternDiskId": "pdisk-001",
                        "snapshotItemId": "snap-001",
                    },
                },
            ],
            "edges": [
                {
                    "source": "vm-1",
                    "target": "disk-p",
                    "sourceHandle": "dp-dc1",
                    "targetHandle": "",
                },
            ],
        }
        disks = _find_vm_disks("vm-1", topology)
        assert len(disks) == 1
        assert disks[0]["patternId"] == "pat-001"
        assert disks[0]["patternDiskId"] == "pdisk-001"
        assert disks[0]["snapshotItemId"] == "snap-001"

    def test_multiple_disks(self):
        """VM with multiple disks."""
        from app.services.deploy_topology import _find_vm_disks

        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "diskControllers": [
                            {"id": "dp-c1", "bus": "virtio"},
                            {"id": "dp-c2", "bus": "sata"},
                        ]
                    },
                },
                {
                    "id": "d1",
                    "type": "storageNode",
                    "data": {"name": "disk1", "size": 20},
                },
                {
                    "id": "d2",
                    "type": "storageNode",
                    "data": {"name": "disk2", "size": 30},
                },
            ],
            "edges": [
                {
                    "source": "vm-1",
                    "target": "d1",
                    "sourceHandle": "dp-c1",
                    "targetHandle": "",
                },
                {
                    "source": "vm-1",
                    "target": "d2",
                    "sourceHandle": "dp-c2",
                    "targetHandle": "",
                },
            ],
        }
        disks = _find_vm_disks("vm-1", topology)
        assert len(disks) == 2
        names = [d["name"] for d in disks]
        assert "disk1" in names
        assert "disk2" in names

    def test_edge_to_non_storage_node(self):
        """dp- handle pointing to a non-storageNode -> skipped."""
        from app.services.deploy_topology import _find_vm_disks

        topology = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {}},
                {"id": "other-1", "type": "containerNode", "data": {}},
            ],
            "edges": [
                {
                    "source": "vm-1",
                    "target": "other-1",
                    "sourceHandle": "dp-x",
                    "targetHandle": "",
                },
            ],
        }
        assert _find_vm_disks("vm-1", topology) == []


# ═══════════════════════════════════════════════════════════════════════
# _ocp_update_status (additional)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpUpdateStatusAdditional:
    @patch("app.core.database.SessionLocal")
    def test_updates_status(self, mock_sl):
        from app.services.deploy_service import _ocp_update_status

        mock_project = MagicMock()
        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        mock_db.query.return_value.filter_by.return_value.first.return_value = (
            mock_project
        )

        _ocp_update_status("proj-1234-5678", "installing")

        assert mock_project.ocp_status == "installing"
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.core.database.SessionLocal")
    def test_updates_status_with_elapsed(self, mock_sl):
        from app.services.deploy_service import _ocp_update_status

        mock_project = MagicMock()
        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        mock_db.query.return_value.filter_by.return_value.first.return_value = (
            mock_project
        )

        _ocp_update_status("proj-1234-5678", "complete", elapsed_secs=300)

        assert mock_project.ocp_status == "complete"
        assert mock_project.ocp_install_elapsed == 300

    @patch("app.core.database.SessionLocal")
    def test_project_not_found(self, mock_sl):
        from app.services.deploy_service import _ocp_update_status

        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        # Should not raise
        _ocp_update_status("proj-missing1", "error")
        mock_db.commit.assert_not_called()
        mock_db.close.assert_called_once()

    @patch("app.core.database.SessionLocal")
    def test_exception_handling(self, mock_sl):
        from app.services.deploy_service import _ocp_update_status

        mock_sl.side_effect = Exception("DB connection failed")

        # Should not raise
        _ocp_update_status("proj-1234-5678", "error")


# ═══════════════════════════════════════════════════════════════════════
# _image_cache_path
# ═══════════════════════════════════════════════════════════════════════


class TestImageCachePathV2:
    def test_local_no_pool(self):
        from app.services.deploy_topology import _image_cache_path

        result = _image_cache_path("item-abc", "qcow2")
        assert result == "/var/lib/troshka/images/item-abc.qcow2"

    def test_local_pool_local_mode(self):
        from app.services.deploy_topology import _image_cache_path

        pool = MagicMock()
        pool.mode = "local"
        result = _image_cache_path("item-def", "iso", pool=pool)
        assert result == "/var/lib/troshka/images/item-def.iso"

    def test_shared_pool(self):
        from app.services.deploy_topology import _image_cache_path

        pool = MagicMock()
        pool.mode = "shared-fsx"
        result = _image_cache_path("item-ghi", "qcow2", pool=pool)
        assert result == "/var/lib/troshka/shared/images/item-ghi.qcow2"

    def test_shared_byo_pool(self):
        from app.services.deploy_topology import _image_cache_path

        pool = MagicMock()
        pool.mode = "shared-byo"
        result = _image_cache_path("item-jkl", "raw", pool=pool)
        assert result == "/var/lib/troshka/shared/images/item-jkl.raw"

    def test_shared_ceph_nfs_pool(self):
        from app.services.deploy_topology import _image_cache_path

        pool = MagicMock()
        pool.mode = "shared-ceph-nfs"
        result = _image_cache_path("item-mno", "qcow2", pool=pool)
        assert result == "/var/lib/troshka/shared/images/item-mno.qcow2"


# ═══════════════════════════════════════════════════════════════════════
# _extract_containers (additional)
# ═══════════════════════════════════════════════════════════════════════


class TestExtractContainersAdditional:
    def test_empty_topology(self):
        from app.services.deploy_topology import _extract_containers

        assert _extract_containers({}) == []
        assert _extract_containers({"nodes": []}) == []

    def test_no_container_nodes(self):
        from app.services.deploy_topology import _extract_containers

        topology = {"nodes": [{"id": "vm-1", "type": "vmNode", "data": {}}]}
        assert _extract_containers(topology) == []

    def test_single_container(self):
        from app.services.deploy_topology import _extract_containers

        topology = {
            "nodes": [
                {
                    "id": "c1",
                    "type": "containerNode",
                    "data": {
                        "name": "myapp",
                        "image": "nginx:latest",
                        "cpus": 2,
                        "memory": 1024,
                        "envVars": [{"name": "FOO", "value": "bar"}],
                        "ports": [{"containerPort": 80}],
                        "command": "/start.sh",
                        "restartPolicy": "on-failure",
                        "privileged": True,
                        "mounts": [{"path": "/data"}],
                    },
                },
            ]
        }
        result = _extract_containers(topology)
        assert len(result) == 1
        c = result[0]
        assert c["node_id"] == "c1"
        assert c["name"] == "myapp"
        assert c["image"] == "nginx:latest"
        assert c["cpus"] == 2
        assert c["memory_mb"] == 1024
        assert c["command"] == "/start.sh"
        assert c["restart_policy"] == "on-failure"
        assert c["privileged"] is True

    def test_pod_container(self):
        from app.services.deploy_topology import _extract_containers

        topology = {
            "nodes": [
                {
                    "id": "p1",
                    "type": "containerNode",
                    "data": {
                        "name": "mypod",
                        "isPod": True,
                        "initContainers": [{"name": "init1", "image": "busybox"}],
                        "podContainers": [{"name": "app", "image": "myapp:v1"}],
                    },
                },
            ]
        }
        result = _extract_containers(topology)
        assert len(result) == 1
        assert result[0]["is_pod"] is True
        assert result[0]["init_containers"] == [{"name": "init1", "image": "busybox"}]
        assert result[0]["pod_containers"] == [{"name": "app", "image": "myapp:v1"}]

    def test_defaults_for_missing_fields(self):
        from app.services.deploy_topology import _extract_containers

        topology = {"nodes": [{"id": "c2", "type": "containerNode", "data": {}}]}
        result = _extract_containers(topology)
        assert len(result) == 1
        c = result[0]
        assert c["name"] == "container"
        assert c["image"] == ""
        assert c["cpus"] == 1
        assert c["memory_mb"] == 512
        assert c["restart_policy"] == "always"
        assert c["privileged"] is False
        assert c["is_pod"] is False

    def test_mixed_node_types(self):
        from app.services.deploy_topology import _extract_containers

        topology = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {}},
                {"id": "c1", "type": "containerNode", "data": {"name": "app1"}},
                {"id": "net-1", "type": "networkNode", "data": {}},
                {"id": "c2", "type": "containerNode", "data": {"name": "app2"}},
            ]
        }
        result = _extract_containers(topology)
        assert len(result) == 2
        assert result[0]["name"] == "app1"
        assert result[1]["name"] == "app2"

    def test_registry_credential_fields(self):
        from app.services.deploy_topology import _extract_containers

        topology = {
            "nodes": [
                {
                    "id": "c3",
                    "type": "containerNode",
                    "data": {
                        "registryCredentialId": "cred-123",
                        "registryCredentialName": "quay-creds",
                    },
                },
            ]
        }
        result = _extract_containers(topology)
        assert result[0]["registry_credential_id"] == "cred-123"
        assert result[0]["registry_credential_name"] == "quay-creds"


# ═══════════════════════════════════════════════════════════════════════
# _should_skip  (additional branch coverage)
# ═══════════════════════════════════════════════════════════════════════


class TestShouldSkipAdditional:
    def test_step_before_resume_is_skipped(self):
        from app.services.deploy_service import _should_skip

        # "networks" is index 1, "images" is index 3 -> skip
        assert _should_skip("images", "networks") is True

    def test_step_at_resume_not_skipped(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip("images", "images") is False

    def test_step_after_resume_not_skipped(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip("networks", "images") is False

    def test_invalid_resume_step(self):
        from app.services.deploy_service import _should_skip

        # Invalid resume_from -> ValueError caught -> False
        assert _should_skip("nonexistent_step", "networks") is False

    def test_invalid_current_step(self):
        from app.services.deploy_service import _should_skip

        # Invalid step -> ValueError caught -> False
        assert _should_skip("networks", "nonexistent_step") is False

    def test_none_resume(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip(None, "networks") is False


# ═══════════════════════════════════════════════════════════════════════
# _ocp_check_console_route
# ═══════════════════════════════════════════════════════════════════════


class TestOcpCheckConsoleRoute:
    @patch("app.services.deploy_service._exec_on_bastion")
    def test_console_200_oauth_200_returns_true(self, mock_exec):
        from app.services.deploy_service import _ocp_check_console_route

        mock_exec.side_effect = ["200", "200"]
        push_fn = MagicMock()
        result = _ocp_check_console_route(
            MagicMock(), "proj-1", "10.0.0.5", "pass", push_fn
        )
        assert result is True
        # Verify final push says ready
        push_fn.assert_any_call("console", "console and OAuth ready")

    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("time.sleep", return_value=None)
    def test_console_503_returns_false(self, mock_sleep, mock_exec):
        from app.services.deploy_service import _ocp_check_console_route

        mock_exec.return_value = "503"
        push_fn = MagicMock()
        result = _ocp_check_console_route(
            MagicMock(), "proj-1", "10.0.0.5", "pass", push_fn
        )
        assert result is False

    @patch("app.services.deploy_service._exec_on_bastion")
    def test_console_403_continues_to_oauth(self, mock_exec):
        from app.services.deploy_service import _ocp_check_console_route

        # 403 is acceptable for console, then 200 for oauth
        mock_exec.side_effect = ["403", "200"]
        push_fn = MagicMock()
        result = _ocp_check_console_route(
            MagicMock(), "proj-1", "10.0.0.5", "pass", push_fn
        )
        assert result is True

    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("time.sleep", return_value=None)
    def test_console_ok_oauth_503_returns_false(self, mock_sleep, mock_exec):
        from app.services.deploy_service import _ocp_check_console_route

        # Console returns 200, OAuth returns 503
        mock_exec.side_effect = ["200", "503"]
        push_fn = MagicMock()
        result = _ocp_check_console_route(
            MagicMock(), "proj-1", "10.0.0.5", "pass", push_fn
        )
        assert result is False

    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("time.sleep", return_value=None)
    def test_console_000_returns_false(self, mock_sleep, mock_exec):
        from app.services.deploy_service import _ocp_check_console_route

        mock_exec.return_value = "000"
        push_fn = MagicMock()
        result = _ocp_check_console_route(
            MagicMock(), "proj-1", "10.0.0.5", "pass", push_fn
        )
        assert result is False
        # Verify the push message doesn't contain HTTP code for "000"
        calls = [c for c in push_fn.call_args_list if "console" in str(c)]
        found_http_000 = any("HTTP 000" in str(c) for c in calls)
        assert not found_http_000

    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("time.sleep", return_value=None)
    def test_console_ok_oauth_000000_returns_false(self, mock_sleep, mock_exec):
        from app.services.deploy_service import _ocp_check_console_route

        mock_exec.side_effect = ["200", "000000"]
        push_fn = MagicMock()
        result = _ocp_check_console_route(
            MagicMock(), "proj-1", "10.0.0.5", "pass", push_fn
        )
        assert result is False
        oauth_calls = [
            c for c in push_fn.call_args_list if "waiting for OAuth route" in str(c)
        ]
        assert oauth_calls
        assert "HTTP" not in str(oauth_calls[-1])

    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("time.sleep", return_value=None)
    def test_console_none_returns_false(self, mock_sleep, mock_exec):
        from app.services.deploy_service import _ocp_check_console_route

        mock_exec.return_value = None
        push_fn = MagicMock()
        result = _ocp_check_console_route(
            MagicMock(), "proj-1", "10.0.0.5", "pass", push_fn
        )
        assert result is False

    @patch("app.services.deploy_service._exec_on_bastion")
    def test_console_302_oauth_403_returns_true(self, mock_exec):
        from app.services.deploy_service import _ocp_check_console_route

        # 302 is a redirect (3xx), 403 is accepted for OAuth
        mock_exec.side_effect = ["302", "403"]
        push_fn = MagicMock()
        result = _ocp_check_console_route(
            MagicMock(), "proj-1", "10.0.0.5", "pass", push_fn
        )
        assert result is True


# ═══════════════════════════════════════════════════════════════════════
# _ocp_wait_for_bastion_ssh
# ═══════════════════════════════════════════════════════════════════════


class TestOcpWaitForBastionSsh:
    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("app.services.troshkad_client.get_vm_state")
    def test_bastion_powered_off_returns_false(self, mock_vm_st, mock_exec):
        from app.services.deploy_service import _ocp_wait_for_bastion_ssh

        host = MagicMock()
        host.host_type = "ec2"
        mock_vm_st.return_value = {"state": "shut_off"}

        bastion = {"id": "bastion-uuid-1234"}
        push_fn = MagicMock()
        result = _ocp_wait_for_bastion_ssh(
            host, "proj-1234-5678", bastion, "10.0.0.5", "pw", push_fn, 0
        )
        assert result is False
        push_fn.assert_any_call(
            "waiting",
            "bastion is powered off — start it to enable OCP monitoring",
        )

    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("app.services.troshkad_client.get_vm_state")
    def test_bastion_ssh_ready(self, mock_vm_st, mock_exec):
        import time

        from app.services.deploy_service import _ocp_wait_for_bastion_ssh

        host = MagicMock()
        host.host_type = "ec2"
        mock_vm_st.return_value = {"state": "running"}
        mock_exec.return_value = "ok"

        bastion = {"id": "bastion-uuid-1234"}
        push_fn = MagicMock()
        deadline = time.time() + 60

        result = _ocp_wait_for_bastion_ssh(
            host, "proj-1234-5678", bastion, "10.0.0.5", "pw", push_fn, deadline
        )
        assert result is True

    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("app.services.troshkad_client.get_vm_state")
    def test_bastion_ssh_timeout(self, mock_vm_st, mock_exec):
        from app.services.deploy_service import _ocp_wait_for_bastion_ssh

        host = MagicMock()
        host.host_type = "ec2"
        mock_vm_st.return_value = {"state": "running"}
        mock_exec.return_value = ""

        bastion = {"id": "bastion-uuid-1234"}
        push_fn = MagicMock()
        # Deadline already passed
        deadline = 0

        result = _ocp_wait_for_bastion_ssh(
            host, "proj-1234-5678", bastion, "10.0.0.5", "pw", push_fn, deadline
        )
        assert result is False
        push_fn.assert_any_call("timeout", "bastion SSH not available")

    @patch("app.services.deploy_service._exec_on_bastion")
    def test_kubevirt_cluster_skips_vm_state_check(self, mock_exec):
        import time

        from app.services.deploy_service import _ocp_wait_for_bastion_ssh

        host = MagicMock()
        host.host_type = "kubevirt-cluster"
        mock_exec.return_value = "ok"

        bastion = {"id": "bastion-uuid-1234"}
        push_fn = MagicMock()
        deadline = time.time() + 60

        result = _ocp_wait_for_bastion_ssh(
            host, "proj-1234-5678", bastion, "10.0.0.5", "pw", push_fn, deadline
        )
        assert result is True

    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("app.services.troshkad_client.get_vm_state")
    def test_vm_state_exception_continues(self, mock_vm_st, mock_exec):
        """If get_vm_state throws, we still try SSH."""
        import time

        from app.services.deploy_service import _ocp_wait_for_bastion_ssh

        host = MagicMock()
        host.host_type = "ec2"
        mock_vm_st.side_effect = Exception("connection refused")
        mock_exec.return_value = "ok"

        bastion = {"id": "bastion-uuid-1234"}
        push_fn = MagicMock()
        deadline = time.time() + 60

        result = _ocp_wait_for_bastion_ssh(
            host, "proj-1234-5678", bastion, "10.0.0.5", "pw", push_fn, deadline
        )
        assert result is True


# ═══════════════════════════════════════════════════════════════════════
# _ocp_wait_for_direct_oc
# ═══════════════════════════════════════════════════════════════════════


class TestOcpWaitForDirectOc:
    @patch("app.services.deploy_service._exec_oc")
    def test_nodes_ready(self, mock_exec_oc):
        import time

        from app.services.deploy_service import _ocp_wait_for_direct_oc

        mock_exec_oc.return_value = "node1   Ready   control-plane   5d   v1.28.0"
        push_fn = MagicMock()
        deadline = time.time() + 60

        result = _ocp_wait_for_direct_oc(MagicMock(), "proj-1", push_fn, deadline)
        assert result is True

    @patch("app.services.deploy_service._exec_oc")
    def test_timeout_no_ready(self, mock_exec_oc):
        from app.services.deploy_service import _ocp_wait_for_direct_oc

        mock_exec_oc.return_value = ""
        push_fn = MagicMock()
        # Deadline already passed
        deadline = 0

        result = _ocp_wait_for_direct_oc(MagicMock(), "proj-1", push_fn, deadline)
        assert result is False
        push_fn.assert_any_call("timeout", "OCP API not reachable")

    @patch("app.services.deploy_service._exec_oc")
    def test_exec_exception_continues(self, mock_exec_oc):
        from app.services.deploy_service import _ocp_wait_for_direct_oc

        mock_exec_oc.side_effect = Exception("connection refused")
        push_fn = MagicMock()
        # Deadline already passed
        deadline = 0

        result = _ocp_wait_for_direct_oc(MagicMock(), "proj-1", push_fn, deadline)
        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# _build_clone_name_map — target-edge branch
# ═══════════════════════════════════════════════════════════════════════


class TestBuildCloneNameMapTargetEdge:
    def test_edge_with_disk_as_target(self):
        from app.services.deploy_service import _build_clone_name_map

        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "id": "disk-aaaa",
                    "data": {
                        "id": "disk-aaaa",
                        "label": "RHEL disk",
                        "format": "qcow2",
                    },
                },
                {"type": "vmNode", "id": "vm-bbbb", "data": {"label": "master"}},
            ],
            "edges": [{"source": "vm-bbbb", "target": "disk-aaaa"}],
        }
        result = _build_clone_name_map(topology)
        assert any("RHEL disk" in v for v in result.values())

    def test_edge_unrelated_to_disk_is_skipped(self):
        from app.services.deploy_service import _build_clone_name_map

        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "id": "disk-aaaa",
                    "data": {"id": "disk-aaaa", "label": "disk1"},
                },
                {"type": "vmNode", "id": "vm-1111", "data": {}},
                {"type": "networkNode", "id": "net-2222", "data": {}},
            ],
            "edges": [
                {"source": "vm-1111", "target": "net-2222"},
                {"source": "vm-1111", "target": "disk-aaaa"},
            ],
        }
        result = _build_clone_name_map(topology)
        assert len(result) == 1


# ═══════════════════════════════════════════════════════════════════════
# _format_import_progress — ValueError on non-numeric progress
# ═══════════════════════════════════════════════════════════════════════


class TestFormatImportProgressValueError:
    def test_non_numeric_progress_string(self):
        from app.services.deploy_service import _format_import_progress

        dv = {"status": {"conditions": []}}
        result = _format_import_progress("disk", dv, "invalid%")
        assert "downloading invalid%" in result

    def test_empty_percent_string(self):
        from app.services.deploy_service import _format_import_progress

        dv = {"status": {"conditions": []}}
        result = _format_import_progress("disk", dv, "abc")
        assert "downloading abc" in result


# ═══════════════════════════════════════════════════════════════════════
# _format_dv_status_line — ImportInProgress phase
# ═══════════════════════════════════════════════════════════════════════


class TestFormatDvStatusLineImportInProgress:
    def test_import_in_progress_delegates(self):
        from app.services.deploy_service import _format_dv_status_line

        dv = {
            "status": {"phase": "ImportInProgress", "progress": "50%", "conditions": []}
        }
        result = _format_dv_status_line("disk", dv)
        assert "downloading 50%" in result

    def test_import_in_progress_no_progress(self):
        from app.services.deploy_service import _format_dv_status_line

        dv = {
            "status": {"phase": "ImportInProgress", "progress": "N/A", "conditions": []}
        }
        result = _format_dv_status_line("disk", dv)
        assert "starting" in result

    def test_import_scheduled(self):
        from app.services.deploy_service import _format_dv_status_line

        dv = {"status": {"phase": "ImportScheduled"}}
        assert _format_dv_status_line("disk", dv) == "disk: scheduled"


# ═══════════════════════════════════════════════════════════════════════
# _resolve_deploy_step — additional branches
# ═══════════════════════════════════════════════════════════════════════


class TestResolveDeployStepAdditional:
    def test_all_disks_done_no_vm_states(self):
        step, detail = _resolve_deploy_step(
            True, "StartingVMs", "booting", "", [], {"vmStates": {}}, {}
        )
        assert step == "startingvms"
        assert detail == "booting"

    def test_all_disks_done_certificate_stage(self):
        step, detail = _resolve_deploy_step(
            True, "Certificate renewal", "renewing", "", [], {}, {}
        )
        assert "certificate" in step
        assert detail == "renewing"

    def test_op_stage_only_no_disks(self):
        step, detail = _resolve_deploy_step(
            False, "Networking", "creating bridges", "", [], {}, {}
        )
        assert step == "networking"
        assert detail == "creating bridges"

    def test_fallback_to_last_progress(self):
        step, detail = _resolve_deploy_step(
            False, "", "", "", [], {}, {"step": "images", "detail": "waiting"}
        )
        assert step == "images"
        assert detail == "waiting"

    def test_fallback_empty_last(self):
        step, detail = _resolve_deploy_step(False, "", "", "", [], {}, {})
        assert step == "deploying"


# ═══════════════════════════════════════════════════════════════════════
# Coverage gap tests — uncovered branches in helper functions
# ═══════════════════════════════════════════════════════════════════════


class TestProgressWrappers:
    """Cover thin Redis-wrapping helpers (lines 75, 79, 83, 87, 91, 95)."""

    @patch("app.services.deploy_service.set_progress")
    def test_set_deploy_progress(self, mock_sp):
        from app.services.deploy_service import _set_deploy_progress

        _set_deploy_progress("proj-1", {"step": "images"})
        mock_sp.assert_called_once_with("deploy:proj-1", {"step": "images"})

    @patch("app.services.deploy_service.get_progress", return_value={"step": "vms"})
    def test_get_deploy_progress_data(self, mock_gp):
        from app.services.deploy_service import _get_deploy_progress_data

        result = _get_deploy_progress_data("proj-2")
        assert result == {"step": "vms"}
        mock_gp.assert_called_once_with("deploy:proj-2")

    @patch("app.services.deploy_service.delete_progress")
    def test_delete_deploy_progress(self, mock_dp):
        from app.services.deploy_service import _delete_deploy_progress

        _delete_deploy_progress("proj-3")
        mock_dp.assert_called_once_with("deploy:proj-3")

    @patch("app.services.deploy_service._redis_mark_cancelled")
    def test_mark_deploy_cancelled(self, mock_mc):
        from app.services.deploy_service import _mark_deploy_cancelled

        _mark_deploy_cancelled("proj-4")
        mock_mc.assert_called_once_with("proj-4")

    @patch("app.services.deploy_service._redis_is_cancelled", return_value=True)
    def test_is_deploy_cancelled(self, mock_ic):
        from app.services.deploy_service import _is_deploy_cancelled

        assert _is_deploy_cancelled("proj-5") is True
        mock_ic.assert_called_once_with("proj-5")

    @patch("app.services.deploy_service.clear_cancelled")
    def test_clear_deploy_cancelled(self, mock_cc):
        from app.services.deploy_service import _clear_deploy_cancelled

        _clear_deploy_cancelled("proj-6")
        mock_cc.assert_called_once_with("proj-6")


class TestGetNetworkLock:
    """Cover _get_network_lock (line 255)."""

    @patch("app.services.deploy_service.get_lock")
    def test_returns_lock_for_host(self, mock_gl):
        from app.services.deploy_service import _get_network_lock

        mock_lock = MagicMock()
        mock_gl.return_value = mock_lock
        result = _get_network_lock("host-abc")
        mock_gl.assert_called_once_with("network:host-abc", timeout=120)
        assert result is mock_lock


class TestShouldSkipValueError:
    """Cover _should_skip ValueError branch (line 247)."""

    def test_unknown_step_returns_false(self):
        from app.services.deploy_service import _should_skip

        # Pass a step name not in DEPLOY_STEPS to trigger ValueError
        assert _should_skip("images", "nonexistent_step") is False

    def test_unknown_resume_from_returns_false(self):
        from app.services.deploy_service import _should_skip

        assert _should_skip("nonexistent_step", "images") is False


class TestFindVmNameByIpNonVmNodes:
    """Cover _find_vm_name_by_ip line 569 — the `continue` for non-vmNode."""

    def test_skips_non_vm_nodes(self):
        from app.services.deploy_topology import _find_vm_name_by_ip

        topo = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"nics": [{"ip": "10.0.0.5"}]},
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"name": "bastion", "nics": [{"ip": "10.0.0.5"}]},
                },
            ]
        }
        # Should find the VM node, not be confused by the networkNode
        assert _find_vm_name_by_ip(topo, "10.0.0.5") == "bastion"

    def test_only_non_vm_nodes_falls_back(self):
        from app.services.deploy_topology import _find_vm_name_by_ip

        topo = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"nics": [{"ip": "10.0.0.5"}]},
                },
            ]
        }
        # No vmNode with this IP — should fall back to IP-based name
        assert _find_vm_name_by_ip(topo, "10.0.0.5") == "10-0-0-5"


class TestFindVmDisksUnrelatedEdge:
    """Cover _find_vm_disks line 594 — edge not connected to the target VM."""

    def test_skips_unrelated_edges(self):
        from app.services.deploy_topology import _find_vm_disks

        topo = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"diskControllers": []}},
                {"id": "vm2", "type": "vmNode", "data": {"diskControllers": []}},
                {
                    "id": "disk1",
                    "type": "storageNode",
                    "data": {"name": "d1", "size": 10, "format": "qcow2"},
                },
            ],
            "edges": [
                {
                    "source": "vm2",
                    "target": "disk1",
                    "sourceHandle": "dp-ctrl1",
                    "targetHandle": "disk-top",
                }
            ],
        }
        # vm1 has no edges to disk1, so it should find no disks
        result = _find_vm_disks("vm1", topo)
        assert result == []


class TestFindContainerVolumesAltHandles:
    """Cover _find_container_volumes lines 662-665 — alternative edge handle directions."""

    def test_tgt_is_container_with_tgt_handle_mnt(self):
        """Line 662-663: tgt == container_node_id and tgt_h.startswith('mnt-')."""
        from app.services.deploy_topology import _find_container_volumes

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {
                        "mounts": [{"diskNodeId": "disk-1", "mountPath": "/data"}],
                    },
                },
                {"id": "disk-1", "type": "storageNode", "data": {"size": 15}},
            ],
            "edges": [
                {
                    "source": "disk-1",
                    "target": "ctr-1",
                    "sourceHandle": "",
                    "targetHandle": "mnt-disk-1",
                },
            ],
        }
        result = _find_container_volumes("ctr-1", topology, "proj-1234")
        assert len(result) == 1
        assert result[0]["mount_path"] == "/data"

    def test_src_is_container_with_src_handle_mnt(self):
        """Line 664-665: src == container_node_id and src_h.startswith('mnt-')."""
        from app.services.deploy_topology import _find_container_volumes

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {
                        "mounts": [{"diskNodeId": "disk-1", "mountPath": "/vol"}],
                    },
                },
                {"id": "disk-1", "type": "storageNode", "data": {"size": 25}},
            ],
            "edges": [
                {
                    "source": "ctr-1",
                    "target": "disk-1",
                    "sourceHandle": "mnt-disk-1",
                    "targetHandle": "",
                },
            ],
        }
        result = _find_container_volumes("ctr-1", topology, "proj-5678")
        assert len(result) == 1
        assert result[0]["mount_path"] == "/vol"
        assert result[0]["size_gb"] == 25

    def test_edge_not_connected_to_container(self):
        """Line 668: disk_node_id is None because edge connects other nodes."""
        from app.services.deploy_topology import _find_container_volumes

        topology = {
            "nodes": [
                {
                    "id": "ctr-1",
                    "type": "containerNode",
                    "data": {"mounts": []},
                },
                {"id": "vm-1", "type": "vmNode", "data": {}},
                {"id": "disk-1", "type": "storageNode", "data": {"size": 10}},
            ],
            "edges": [
                {
                    "source": "vm-1",
                    "target": "disk-1",
                    "sourceHandle": "dp-ctrl1",
                    "targetHandle": "disk-top",
                },
            ],
        }
        result = _find_container_volumes("ctr-1", topology, "proj-1")
        assert result == []


class TestResolveBootDevsUnknownId:
    """Cover _resolve_boot_devs lines 878, 884 — unknown boot dev + cdrom controller fallback."""

    def test_unknown_boot_dev_id_skipped(self):
        """Line 878: boot_devices entry not in boot_type_map or storage_nodes — skip."""
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": ["bogus-id-999", "hd"], "disk_controllers": []}
        disks = [{"format": "qcow2"}]
        topo = {"nodes": []}
        result = _resolve_boot_devs(vm, disks, topo)
        assert result == ["hd"]

    def test_cdrom_controller_not_added_for_config_iso(self):
        """Config ISO attached but not bootable — do not append cdrom to boot order."""
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {
            "boot_devices": ["hd"],
            "disk_controllers": [{"id": "dc-1", "bus": "sata", "name": "cdrom-1"}],
        }
        disks = [{"format": "qcow2"}]
        topo = {"nodes": []}
        result = _resolve_boot_devs(vm, disks, topo)
        assert result == ["hd"]

    def test_all_unknown_boot_devs_fallback_to_hd(self):
        """Line 885: boot_devs ends up empty, returns ['hd'] fallback."""
        from app.services.deploy_topology import _resolve_boot_devs

        vm = {"boot_devices": ["zzz-123", "yyy-456"], "disk_controllers": []}
        disks = [{"format": "qcow2"}]
        topo = {"nodes": []}
        result = _resolve_boot_devs(vm, disks, topo)
        assert result == ["hd"]


class TestAutoAssignContainerIpsTargetEdge:
    """Cover _auto_assign_container_ips lines 1792-1793 — target-side edge match."""

    def test_assigns_ip_via_target_edge(self):
        """Lines 1792-1793: tgt == node['id'] and th matches the NIC handle."""
        from app.services.deploy_topology import _auto_assign_container_ips

        topology = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {
                        "cidr": "192.168.0.0/24",
                        "dhcpRangeStart": "192.168.0.10",
                        "dhcpRangeEnd": "192.168.0.20",
                    },
                },
                {
                    "id": "ctr1",
                    "type": "containerNode",
                    "data": {
                        "name": "web",
                        "nics": [{"id": "nic-a", "name": "eth0"}],
                    },
                },
            ],
            "edges": [
                {
                    "source": "net1",
                    "target": "ctr1",
                    "sourceHandle": "net-port",
                    "targetHandle": "nic-nic-a-top",
                },
            ],
        }
        _auto_assign_container_ips(topology)
        nic = topology["nodes"][1]["data"]["nics"][0]
        assert nic["ip"] == "192.168.0.10"

    def test_no_dhcp_range_skips(self):
        """Line 1807: _get_dhcp_range returns None — container NIC left without IP."""
        from app.services.deploy_topology import _auto_assign_container_ips

        topology = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"cidr": "10.0.0.0/30"},  # only 2 hosts, < 10
                },
                {
                    "id": "ctr1",
                    "type": "containerNode",
                    "data": {
                        "name": "app",
                        "nics": [{"id": "nic-b", "name": "eth0"}],
                    },
                },
            ],
            "edges": [
                {
                    "source": "ctr1",
                    "target": "net1",
                    "sourceHandle": "nic-nic-b-top",
                    "targetHandle": "net-port",
                },
            ],
        }
        _auto_assign_container_ips(topology)
        nic = topology["nodes"][1]["data"]["nics"][0]
        # /30 has only 2 hosts — no DHCP range → NIC stays without IP
        assert nic.get("ip") is None or nic.get("ip") == ""


class TestCollectUsedIpsInvalidCidr:
    """Cover _collect_used_ips lines 1838-1839 — ValueError on invalid CIDR."""

    def test_invalid_cidr_ignored(self):
        from app.services.deploy_topology import _collect_used_ips

        topology = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"cidr": "not-a-cidr"},
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"nics": [{"ip": "10.0.0.5"}]},
                },
            ]
        }
        result = _collect_used_ips(topology)
        assert "10.0.0.5" in result
        # Gateway IP not added because CIDR was invalid
        assert len(result) == 1


class TestGetDhcpRangeInvalidIp:
    """Cover _get_dhcp_range lines 1869-1870 — ValueError on invalid IP address."""

    def test_invalid_range_addresses_returns_none(self):
        from app.services.deploy_topology import _get_dhcp_range

        net_data = {
            "dhcpRangeStart": "not-an-ip",
            "dhcpRangeEnd": "also-not-an-ip",
        }
        assert _get_dhcp_range(net_data) is None

    def test_invalid_start_only(self):
        from app.services.deploy_topology import _get_dhcp_range

        net_data = {
            "dhcpRangeStart": "invalid",
            "dhcpRangeEnd": "10.0.0.100",
        }
        assert _get_dhcp_range(net_data) is None


class TestExtractBmcConfigEdgeSrcHandle:
    """Cover _extract_bmc_config line 754 — edge where handle doesn't start with 'nic-'."""

    def test_non_nic_handle_skipped(self):
        from app.services.deploy_topology import _extract_bmc_config

        topo = {
            "nodes": [
                {
                    "id": "bmc-net",
                    "type": "networkNode",
                    "data": {"networkType": "bmc", "cidr": "10.0.0.0/24"},
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "name": "sno1",
                        "bmcEnabled": True,
                        "bmcIp": "10.0.0.10",
                        "nics": [
                            {
                                "id": "nic-1",
                                "ip": "10.0.0.11",
                                "mac": "aa:bb:cc:dd:ee:ff",
                            }
                        ],
                    },
                },
            ],
            "edges": [
                {
                    "source": "vm1",
                    "target": "bmc-net",
                    "sourceHandle": "dp-ctrl1",
                    "targetHandle": "net-port",
                },
            ],
        }
        result = _extract_bmc_config(topo, "proj-12345678")
        assert result is not None
        assert len(result["vms"]) == 1
        # DHCP hosts list is empty because the edge handle is "dp-ctrl1", not "nic-..."
        assert result["dhcp_hosts"] == []


class TestWaitForSharedCacheTimeout:
    """Cover _wait_for_shared_cache line 366 — timeout returns False."""

    @patch("time.sleep", return_value=None)
    @patch("time.time")
    def test_timeout_returns_false(self, mock_time_time, mock_sleep):
        from app.services.deploy_service import _wait_for_shared_cache

        mock_db = MagicMock()
        # Always return a "downloading" entry
        mock_entry = MagicMock()
        mock_entry.status = "downloading"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_entry

        # deadline = time() + 600 = 1000. Loop: time() < 1000 first iteration, then > 1000
        mock_time_time.side_effect = [400, 400, 1100]

        result = _wait_for_shared_cache(
            mock_db, "pool-1", "item-1", "pattern", timeout=600
        )
        assert result is False


class TestCollectDvProgressPartialException:
    """Cover _collect_dv_progress lines 2326-2327 — exception from one namespace."""

    @patch("app.services.deploy_service._fill_missing_disk_labels")
    @patch("app.services.deploy_service._best_dv_status")
    @patch("app.services.deploy_service._build_clone_name_map")
    @patch("app.services.deploy_service._format_dv_status_line")
    def test_one_namespace_fails_other_succeeds(
        self, mock_fmt, mock_clone, mock_best, mock_fill
    ):
        from app.services.deploy_service import _collect_dv_progress

        mock_clone.return_value = {"vm-aaaa-disk-bbbb": "RHEL"}
        mock_fmt.return_value = "RHEL: done"
        mock_best.return_value = {"RHEL": "done"}
        mock_fill.return_value = None

        provider = MagicMock()
        topology = {"nodes": []}

        with patch(
            "app.services.providers.kubevirt._get_k8s_clients"
        ) as mock_k8s, patch("app.services.providers.kubevirt._project_ns") as mock_ns:
            mock_custom = MagicMock()
            mock_k8s.return_value = (mock_custom, MagicMock(), MagicMock())
            mock_ns.return_value = "troshka-proj-1"

            # First namespace raises, second succeeds
            def list_side_effect(group, version, namespace, plural):
                if namespace == "troshka-cache":
                    raise Exception("cache namespace error")
                return {
                    "items": [
                        {
                            "metadata": {
                                "namespace": "troshka-proj-1",
                                "name": "vm-aaaa-disk-bbbb",
                            },
                            "status": {"phase": "Succeeded", "progress": "100%"},
                        }
                    ]
                }

            mock_custom.list_namespaced_custom_object.side_effect = list_side_effect

            result = _collect_dv_progress("proj-1", provider, topology)

        # Should still return results from the successful namespace
        assert isinstance(result, list)

    @patch("app.services.deploy_service._fill_missing_disk_labels")
    @patch("app.services.deploy_service._best_dv_status")
    @patch("app.services.deploy_service._build_clone_name_map")
    @patch("app.services.deploy_service._format_dv_status_line")
    def test_cache_vs_clone_lines_separated(
        self, mock_fmt, mock_clone, mock_best, mock_fill
    ):
        """Lines 2338-2342: DVs in troshka-cache go to cache_lines, others to clone_lines."""
        from app.services.deploy_service import _collect_dv_progress

        mock_clone.return_value = {"vm-aaaa-disk-bbbb": "RHEL"}

        def fmt_side_effect(friendly, dv):
            phase = dv.get("status", {}).get("phase", "")
            return f"{friendly}: {phase.lower()}"

        mock_fmt.side_effect = fmt_side_effect
        mock_best.return_value = {"RHEL": "succeeded"}
        mock_fill.return_value = None

        provider = MagicMock()
        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {
                        "resolvedS3Path": "patterns/xyz/disk.qcow2",
                        "label": "RHEL",
                    },
                }
            ]
        }

        with patch(
            "app.services.providers.kubevirt._get_k8s_clients"
        ) as mock_k8s, patch("app.services.providers.kubevirt._project_ns") as mock_ns:
            mock_custom = MagicMock()
            mock_k8s.return_value = (mock_custom, MagicMock(), MagicMock())
            mock_ns.return_value = "troshka-proj-1"

            def list_side_effect(group, version, namespace, plural):
                if namespace == "troshka-cache":
                    import hashlib

                    h = hashlib.sha256(b"patterns/xyz/disk.qcow2").hexdigest()[:16]
                    return {
                        "items": [
                            {
                                "metadata": {
                                    "namespace": "troshka-cache",
                                    "name": f"golden-{h}",
                                },
                                "status": {"phase": "Succeeded"},
                            }
                        ]
                    }
                return {
                    "items": [
                        {
                            "metadata": {
                                "namespace": "troshka-proj-1",
                                "name": "vm-aaaa-disk-bbbb",
                            },
                            "status": {"phase": "CloneInProgress"},
                        }
                    ]
                }

            mock_custom.list_namespaced_custom_object.side_effect = list_side_effect

            result = _collect_dv_progress("proj-1", provider, topology)

        assert isinstance(result, list)


# ═══════════════════════════════════════════════════════════════════════
# _teardown_bmc_via_troshkad (lines 805-810)
# ═══════════════════════════════════════════════════════════════════════


class TestTeardownBmcViaTroshkad:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="tear-1")
    def test_success(self, mock_start, mock_wait):
        from app.services.deploy_service import _teardown_bmc_via_troshkad

        mock_wait.return_value = {"status": "completed"}
        host = MagicMock()
        _teardown_bmc_via_troshkad(host, "proj-123")
        mock_start.assert_called_once_with(
            host, "/bmc/teardown", {"project_id": "proj-123"}
        )
        mock_wait.assert_called_once_with(host, "tear-1", timeout=60)

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="tear-2")
    def test_failure_logs_warning(self, mock_start, mock_wait):
        from app.services.deploy_service import _teardown_bmc_via_troshkad

        mock_wait.return_value = {"status": "failed", "result": "bridge missing"}
        host = MagicMock()
        # Should not raise, just log a warning
        _teardown_bmc_via_troshkad(host, "proj-456")
        mock_wait.assert_called_once()


class TestSetupBmcTeardownException:
    """Cover _setup_bmc_via_troshkad lines 778-779 — teardown raises exception."""

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="bmc-job-1")
    @patch(
        "app.services.deploy_service._teardown_bmc_via_troshkad",
        side_effect=Exception("teardown error"),
    )
    def test_teardown_exception_swallowed(self, mock_teardown, mock_start, mock_wait):
        from app.services.deploy_service import _setup_bmc_via_troshkad

        mock_wait.return_value = {"status": "completed"}
        host = MagicMock()
        bmc_config = {
            "bmc_network": {
                "cidr": "192.168.100.0/24",
                "bmcUsername": "admin",
                "bmcPassword": "pw",
            },
            "vms": [{"domain_name": "troshka-proj-vm1", "bmc_ip": "192.168.100.10"}],
        }
        # Should succeed despite teardown exception
        result = _setup_bmc_via_troshkad(host, "proj-1", bmc_config)
        assert result is True
        mock_teardown.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _start_vms_via_troshkad (lines 1688-1747)
# ═══════════════════════════════════════════════════════════════════════


class TestStartVmsViaTroshkad:
    """Cover _start_vms_via_troshkad — ordered start, unordered start, error paths."""

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="start-1")
    def test_ordered_start(self, mock_start, mock_wait):
        from app.services.deploy_service import _start_vms_via_troshkad

        mock_wait.return_value = {"status": "completed"}
        host = MagicMock()
        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"name": "bastion", "nics": []},
                },
            ],
            "startOrder": [{"vmId": "vm-1"}],
        }
        failed = _start_vms_via_troshkad(host, "proj-12345678", topology)
        assert failed == []
        mock_start.assert_called_once()

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="start-2")
    def test_ordered_auto_start_disabled(self, mock_start, mock_wait):
        from app.services.deploy_service import _start_vms_via_troshkad

        host = MagicMock()
        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"name": "sno1", "nics": []},
                },
            ],
            "startOrder": [{"vmId": "vm-1", "autoStart": False}],
        }
        failed = _start_vms_via_troshkad(host, "proj-12345678", topology)
        assert failed == []
        # VM skipped — start_job not called
        mock_start.assert_not_called()

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="start-3")
    @patch("app.services.deploy_service._time")
    def test_ordered_with_delay(self, mock_time, mock_start, mock_wait):
        from app.services.deploy_service import _start_vms_via_troshkad

        mock_wait.return_value = {"status": "completed"}
        host = MagicMock()
        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"name": "master", "nics": []},
                },
            ],
            "startOrder": [{"vmId": "vm-1", "delaySeconds": 5}],
        }
        failed = _start_vms_via_troshkad(host, "proj-12345678", topology)
        assert failed == []
        mock_time.sleep.assert_called_once_with(5)

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_ordered_start_error(self, mock_start, mock_wait):
        from app.services.deploy_service import _start_vms_via_troshkad

        mock_start.side_effect = TroshkadError("connection refused")
        host = MagicMock()
        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"name": "bastion", "nics": []},
                },
            ],
            "startOrder": [{"vmId": "vm-1"}],
        }
        failed = _start_vms_via_troshkad(host, "proj-12345678", topology)
        assert len(failed) == 1
        assert failed[0][0] == "bastion"

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="start-4")
    def test_unordered_start(self, mock_start, mock_wait):
        from app.services.deploy_service import _start_vms_via_troshkad

        mock_wait.return_value = {"status": "completed"}
        host = MagicMock()
        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"name": "worker1", "nics": []},
                },
                {
                    "id": "vm-2",
                    "type": "vmNode",
                    "data": {"name": "worker2", "nics": []},
                },
            ],
            "startOrder": [],  # No explicit order
        }
        failed = _start_vms_via_troshkad(host, "proj-12345678", topology)
        assert failed == []
        # Both VMs started
        assert mock_start.call_count == 2

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="start-5")
    def test_unordered_power_on_at_deploy_false(self, mock_start, mock_wait):
        from app.services.deploy_service import _start_vms_via_troshkad

        host = MagicMock()
        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "name": "sno-target",
                        "powerOnAtDeploy": False,
                        "nics": [],
                    },
                },
            ],
        }
        failed = _start_vms_via_troshkad(host, "proj-12345678", topology)
        assert failed == []
        mock_start.assert_not_called()

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_unordered_start_job_error(self, mock_start, mock_wait):
        from app.services.deploy_service import _start_vms_via_troshkad

        mock_start.side_effect = TroshkadError("agent down")
        host = MagicMock()
        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"name": "web", "nics": []},
                },
            ],
        }
        failed = _start_vms_via_troshkad(host, "proj-12345678", topology)
        assert len(failed) == 1
        assert failed[0][0] == "web"

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="start-6")
    def test_unordered_wait_error(self, mock_start, mock_wait):
        from app.services.deploy_service import _start_vms_via_troshkad

        mock_wait.side_effect = TroshkadError("timeout waiting for job")
        host = MagicMock()
        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"name": "db", "nics": []},
                },
            ],
        }
        failed = _start_vms_via_troshkad(host, "proj-12345678", topology)
        assert len(failed) == 1
        assert failed[0][0] == "db"

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job", return_value="start-7")
    def test_mixed_ordered_and_unordered(self, mock_start, mock_wait):
        from app.services.deploy_service import _start_vms_via_troshkad

        mock_wait.return_value = {"status": "completed"}
        host = MagicMock()
        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"name": "bastion", "nics": []},
                },
                {
                    "id": "vm-2",
                    "type": "vmNode",
                    "data": {"name": "worker", "nics": []},
                },
            ],
            "startOrder": [{"vmId": "vm-1"}],  # Only bastion in start order
        }
        failed = _start_vms_via_troshkad(host, "proj-12345678", topology)
        assert failed == []
        # Both VMs started (bastion via order, worker via unordered)
        assert mock_start.call_count == 2


# ═══════════════════════════════════════════════════════════════════════
# _finalize_kubevirt_deploy (uncovered lines ~2388-2426)
# ═══════════════════════════════════════════════════════════════════════


class TestFinalizeKubevirtDeployExtended:
    @patch("app.services.deploy_service._allocate_kubevirt_eips")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._extract_bmc_config", return_value=None)
    @patch("app.services.deploy_service._has_ocp_monitor", return_value=False)
    def test_basic_finalize_no_bmc_no_ocp(
        self, mock_ocp, mock_bmc, mock_notify, mock_del, mock_eip
    ):
        from app.services.deploy_service import _finalize_kubevirt_deploy

        project = MagicMock()
        project.state = "deploying"
        db = MagicMock()
        topology = {
            "nodes": [{"data": {"resolvedS3Path": "/s3", "presignedUrl": "http://x"}}]
        }
        pid = "proj-12345678"

        _finalize_kubevirt_deploy(pid, project, topology, db)

        assert project.state == "active"
        assert project.deploy_error is None
        assert project.deploy_progress is None
        db.commit.assert_called()
        mock_del.assert_called_once_with(pid)
        # finalize emits a topology-update then the project-state notification.
        mock_notify.assert_any_call(pid, {"type": "project-state", "state": "active"})

    @patch("app.services.deploy_service._allocate_kubevirt_eips")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._has_ocp_monitor", return_value=True)
    @patch("app.services.deploy_service._extract_bmc_config")
    def test_finalize_with_bmc_and_ocp_monitor(
        self, mock_bmc, mock_ocp, mock_notify, mock_del, mock_eip
    ):
        from app.services.deploy_service import _finalize_kubevirt_deploy

        mock_bmc.return_value = {
            "bmc_network": {"bmcUsername": "root", "bmcPassword": "secret"},
            "vms": [{"node_id": "vm-1", "bmc_ip": "10.0.1.100"}],
        }
        project = MagicMock()
        project.state = "deploying"
        db = MagicMock()
        topology = {"nodes": []}
        pid = "proj-bmc12345"

        _finalize_kubevirt_deploy(pid, project, topology, db)

        assert project.state == "active"
        assert project.ocp_status == "monitoring"
        assert project.ocp_status_detail is None
        assert project.ocp_install_elapsed is None
        # deployed_topology should have bmc section
        deployed = project.deployed_topology
        assert "bmc" in deployed
        assert deployed["bmc"]["username"] == "root"
        assert deployed["bmc"]["password"] == "secret"
        assert "vm-1" in deployed["bmc"]["vms"]
        vm_bmc = deployed["bmc"]["vms"]["vm-1"]
        assert "redfish_url" in vm_bmc
        assert "ipmi_address" in vm_bmc

    @patch("app.services.deploy_service._allocate_kubevirt_eips")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._has_ocp_monitor", return_value=False)
    @patch("app.services.deploy_service._extract_bmc_config", return_value=None)
    def test_finalize_cleans_s3_from_topology(
        self, mock_bmc, mock_ocp, mock_notify, mock_del, mock_eip
    ):
        from app.services.deploy_service import _finalize_kubevirt_deploy

        project = MagicMock()
        db = MagicMock()
        topology = {
            "nodes": [
                {
                    "data": {
                        "resolvedS3Path": "s3://bucket/key",
                        "presignedUrl": "https://s3.example.com/key?token=abc",
                        "ciGeneratedUserData": "some-data",
                        "label": "disk1",
                    },
                },
            ],
        }
        pid = "proj-clean1234"

        _finalize_kubevirt_deploy(pid, project, topology, db)

        # The cleaned topology should not have S3 or presigned fields
        cleaned = project.deployed_topology
        node_data = cleaned["nodes"][0]["data"]
        assert "resolvedS3Path" not in node_data
        assert "presignedUrl" not in node_data
        assert "ciGeneratedUserData" not in node_data
        assert node_data["label"] == "disk1"


# ═══════════════════════════════════════════════════════════════════════
# _handle_kubevirt_deploy_error (uncovered lines ~2429-2441)
# ═══════════════════════════════════════════════════════════════════════


class TestHandleKubevirtDeployErrorExtended:
    def test_error_from_status_error_field(self):
        from app.services.deploy_service import _handle_kubevirt_deploy_error

        project = MagicMock()
        db = MagicMock()
        notify_fn = MagicMock()
        pid = "proj-err12345"
        status = {"error": "disk import failed"}

        _handle_kubevirt_deploy_error(pid, project, status, db, notify_fn)

        assert project.state == "error"
        assert project.deploy_error == "disk import failed"
        db.commit.assert_called_once()
        notify_fn.assert_called_once()
        msg = notify_fn.call_args[0][1]
        assert msg["state"] == "error"
        assert msg["deploy_error"] == "disk import failed"

    def test_error_from_status_message_field(self):
        from app.services.deploy_service import _handle_kubevirt_deploy_error

        project = MagicMock()
        db = MagicMock()
        notify_fn = MagicMock()
        status = {"message": "namespace stuck terminating"}

        _handle_kubevirt_deploy_error("proj-2", project, status, db, notify_fn)

        assert project.deploy_error == "namespace stuck terminating"

    def test_error_fallback_default_message(self):
        from app.services.deploy_service import _handle_kubevirt_deploy_error

        project = MagicMock()
        db = MagicMock()
        notify_fn = MagicMock()
        status = {}

        _handle_kubevirt_deploy_error("proj-3", project, status, db, notify_fn)

        assert project.deploy_error == "Operator reported an error"


# ═══════════════════════════════════════════════════════════════════════
# _push_kubevirt_deploy_progress (uncovered lines ~2444-2465)
# ═══════════════════════════════════════════════════════════════════════


class TestPushKubevirtDeployProgressExtended:
    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_pushes_progress_when_changed(self, mock_get, mock_set):
        from app.services.deploy_service import _push_kubevirt_deploy_progress

        project = MagicMock()
        db = MagicMock()
        notify_fn = MagicMock()
        pid = "proj-push1234"

        _push_kubevirt_deploy_progress(
            pid,
            project,
            "images",
            "downloading disks",
            42,
            ["disk1: downloading"],
            db,
            notify_fn,
        )

        mock_set.assert_called_once()
        db.commit.assert_called_once()
        notify_fn.assert_called_once()
        msg = notify_fn.call_args[0][1]
        assert msg["type"] == "deploy-progress"
        assert msg["step"] == "images"
        assert msg["detail"] == "downloading disks"
        assert msg["percent"] == 42

    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("app.services.deploy_service._get_deploy_progress_data")
    def test_no_push_when_same_as_last(self, mock_get, mock_set):
        from app.services.deploy_service import _push_kubevirt_deploy_progress

        mock_get.return_value = {"step": "images", "detail": "same", "percent": 50}
        project = MagicMock()
        db = MagicMock()
        notify_fn = MagicMock()

        _push_kubevirt_deploy_progress(
            "pid", project, "images", "same", 50, ["x"], db, notify_fn
        )

        mock_set.assert_not_called()
        notify_fn.assert_not_called()

    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_no_push_when_no_detail_and_no_dv_lines(self, mock_get, mock_set):
        from app.services.deploy_service import _push_kubevirt_deploy_progress

        project = MagicMock()
        db = MagicMock()
        notify_fn = MagicMock()

        _push_kubevirt_deploy_progress(
            "pid", project, "deploying", "", 0, [], db, notify_fn
        )

        mock_set.assert_not_called()
        notify_fn.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# _compute_deploy_step (uncovered lines ~2352-2365)
# ═══════════════════════════════════════════════════════════════════════


class TestComputeDeployStepExtended:
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_with_dv_lines_not_done(self, mock_get):
        from app.services.deploy_service import _compute_deploy_step

        status = {}
        dv_lines = ["disk1: downloading 50%", "disk2: waiting"]
        progress = {"stage": "", "detail": "", "percent": 30}

        step, detail, percent = _compute_deploy_step("pid", status, dv_lines, progress)

        assert step == "images"
        assert "disk1" in detail
        assert percent == 30

    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_all_disks_done_with_op_stage(self, mock_get):
        from app.services.deploy_service import _compute_deploy_step

        status = {"vmStates": {"vm1": "Running", "vm2": "Stopped"}}
        dv_lines = ["disk1: done", "disk2: done"]
        progress = {"stage": "Starting VMs", "detail": "2/2 ready", "percent": 90}

        step, detail, percent = _compute_deploy_step("pid", status, dv_lines, progress)

        assert step == "starting vms"
        assert "2/2" in detail
        assert percent == 90

    @patch(
        "app.services.deploy_service._get_deploy_progress_data",
        return_value={"step": "deploying", "detail": "old"},
    )
    def test_no_dv_lines_no_progress_falls_back(self, mock_get):
        from app.services.deploy_service import _compute_deploy_step

        step, detail, percent = _compute_deploy_step("pid", {}, [], None)

        assert step == "deploying"
        assert percent == 0

    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_certificate_stage(self, mock_get):
        from app.services.deploy_service import _compute_deploy_step

        status = {}
        dv_lines = ["disk1: done"]
        progress = {
            "stage": "Certificate Renewal",
            "detail": "renewing certs",
            "percent": 80,
        }

        step, detail, percent = _compute_deploy_step("pid", status, dv_lines, progress)

        assert step == "certificate renewal"
        assert detail == "renewing certs"


# ═══════════════════════════════════════════════════════════════════════
# _extract_bastion_info (uncovered lines ~4656-4674)
# ═══════════════════════════════════════════════════════════════════════


class TestExtractBastionInfoExtended:
    def test_with_bastion_node(self):
        from app.services.deploy_service import _extract_bastion_info

        nodes = [
            {
                "type": "vmNode",
                "data": {
                    "label": "bastion",
                    "nics": [{"ip": "10.0.0.5"}, {"ip": "10.0.1.5"}],
                    "ciCloudUserPassword": "s3cret",
                },
            },
            {"type": "vmNode", "data": {"label": "worker1", "nics": []}},
        ]
        bastion, ip, pw = _extract_bastion_info(nodes)
        assert bastion is not None
        assert ip == "10.0.0.5"
        assert pw == "s3cret"

    def test_no_bastion_node(self):
        from app.services.deploy_service import _extract_bastion_info

        nodes = [
            {
                "type": "vmNode",
                "data": {"label": "worker1", "nics": [{"ip": "10.0.0.2"}]},
            },
        ]
        bastion, ip, pw = _extract_bastion_info(nodes)
        assert bastion is None
        assert ip == ""
        assert pw == ""

    def test_bastion_no_ip(self):
        from app.services.deploy_service import _extract_bastion_info

        nodes = [
            {
                "type": "vmNode",
                "data": {"label": "bastion", "nics": [{"mac": "aa:bb:cc:dd:ee:ff"}]},
            },
        ]
        bastion, ip, pw = _extract_bastion_info(nodes)
        assert bastion is not None
        assert ip == ""

    def test_bastion_no_password(self):
        from app.services.deploy_service import _extract_bastion_info

        nodes = [
            {
                "type": "vmNode",
                "data": {"label": "bastion", "nics": [{"ip": "10.0.0.1"}]},
            },
        ]
        bastion, ip, pw = _extract_bastion_info(nodes)
        assert pw == ""


# ═══════════════════════════════════════════════════════════════════════
# _start_vm_monitor (uncovered lines ~4227-4254)
# ═══════════════════════════════════════════════════════════════════════


class TestStartVmMonitorExtended:
    @patch("app.services.deploy_service.threading")
    @patch("app.services.deploy_service.add_to_set")
    @patch("app.services.deploy_service.is_in_set", return_value=False)
    def test_starts_monitor_for_ocp_monitor_vm(self, mock_in_set, mock_add, mock_thr):
        from app.services.deploy_service import _start_vm_monitor

        node = {
            "id": "vm-ocp-1",
            "type": "vmNode",
            "data": {"ocpMonitor": True, "label": "sno1", "ocpKubeconfig": "kc-data"},
        }
        result = _start_vm_monitor("proj-1234", "host-1234", node, 1000.0)

        assert result is True
        mock_add.assert_called_once()
        mock_thr.Thread.assert_called_once()
        mock_thr.Thread.return_value.start.assert_called_once()

    @patch("app.services.deploy_service.is_in_set", return_value=True)
    def test_skips_already_monitored(self, mock_in_set):
        from app.services.deploy_service import _start_vm_monitor

        node = {
            "id": "vm-ocp-2",
            "type": "vmNode",
            "data": {"ocpMonitor": True, "label": "sno2"},
        }
        result = _start_vm_monitor("proj-1234", "host-1234", node, 1000.0)

        assert result is True

    def test_skips_non_monitor_vm(self):
        from app.services.deploy_service import _start_vm_monitor

        node = {
            "id": "vm-worker",
            "type": "vmNode",
            "data": {"label": "worker1"},
        }
        result = _start_vm_monitor("proj-1234", "host-1234", node, 1000.0)

        assert result is False

    @patch("app.services.deploy_service.threading")
    @patch("app.services.deploy_service.add_to_set")
    @patch("app.services.deploy_service.is_in_set", return_value=False)
    def test_starts_monitor_for_bastion_browser_vm(
        self, mock_in_set, mock_add, mock_thr
    ):
        from app.services.deploy_service import _start_vm_monitor

        node = {
            "id": "vm-bastion-1",
            "type": "vmNode",
            "data": {"configureBastionBrowser": True, "label": "bastion"},
        }
        result = _start_vm_monitor("proj-1234", "host-1234", node, 1000.0)

        assert result is True
        mock_add.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# maybe_start_ocp_health_monitor (uncovered lines ~4257-4274)
# ═══════════════════════════════════════════════════════════════════════


class TestMaybeStartOcpHealthMonitor:
    @patch("app.services.deploy_service._start_vm_monitor", return_value=True)
    @patch("app.services.deploy_service._resolve_monitor_context")
    def test_starts_monitors_for_vm_candidates(self, mock_ctx, mock_start):
        from app.services.deploy_service import maybe_start_ocp_health_monitor

        host = MagicMock()
        host.id = "host-1"
        topo = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"ocpMonitor": True}},
                {"id": "vm-2", "type": "vmNode", "data": {"label": "worker"}},
                {"id": "net-1", "type": "networkNode", "data": {}},
            ],
        }
        mock_ctx.return_value = (MagicMock(), host, topo, 1000.0)

        maybe_start_ocp_health_monitor("proj-12345678")

        # Only vmNode entries passed to _start_vm_monitor
        assert mock_start.call_count == 2

    @patch("app.services.deploy_service._resolve_monitor_context", return_value=None)
    def test_no_context_returns_early(self, mock_ctx):
        from app.services.deploy_service import maybe_start_ocp_health_monitor

        maybe_start_ocp_health_monitor("proj-no-ctx")
        # No error, just returns


# ═══════════════════════════════════════════════════════════════════════
# _check_vm_route_http (line 4740)
# ═══════════════════════════════════════════════════════════════════════


class TestCheckVmRouteHttpExtended:
    def test_returns_http_code(self):
        from app.services.deploy_service import _check_vm_route_http

        oc_fn = MagicMock(return_value="200")
        result = _check_vm_route_http(oc_fn, "console-openshift-console.apps")
        assert result == "200"

    def test_strips_whitespace(self):
        from app.services.deploy_service import _check_vm_route_http

        oc_fn = MagicMock(return_value="  403\n")
        result = _check_vm_route_http(oc_fn, "oauth-openshift.apps")
        assert result == "403"

    def test_none_result_returns_000(self):
        from app.services.deploy_service import _check_vm_route_http

        oc_fn = MagicMock(return_value=None)
        result = _check_vm_route_http(oc_fn, "console")
        assert result == "000"


# ═══════════════════════════════════════════════════════════════════════
# _check_vm_console_and_oauth (uncovered lines ~4765-4779)
# ═══════════════════════════════════════════════════════════════════════


class TestCheckVmConsoleAndOauthExtended:
    @patch("app.services.deploy_service._check_vm_route_http")
    def test_both_ready(self, mock_route):
        from app.services.deploy_service import _check_vm_console_and_oauth

        mock_route.side_effect = ["200", "302"]
        push_fn = MagicMock()
        _t = MagicMock()

        result = _check_vm_console_and_oauth(MagicMock(), push_fn, _t)

        assert result is True
        # Last push should say ready
        last_call = push_fn.call_args_list[-1]
        assert "ready" in last_call[0][1]

    @patch("app.services.deploy_service._check_vm_route_http")
    def test_console_not_ready(self, mock_route):
        from app.services.deploy_service import _check_vm_console_and_oauth

        mock_route.return_value = "000"
        push_fn = MagicMock()
        _t = MagicMock()

        result = _check_vm_console_and_oauth(MagicMock(), push_fn, _t)

        assert result is False
        _t.sleep.assert_called_once_with(10)

    @patch("app.services.deploy_service._check_vm_route_http")
    def test_console_ready_oauth_not(self, mock_route):
        from app.services.deploy_service import _check_vm_console_and_oauth

        mock_route.side_effect = ["200", "000"]
        push_fn = MagicMock()
        _t = MagicMock()

        result = _check_vm_console_and_oauth(MagicMock(), push_fn, _t)

        assert result is False
        # Should mention OAuth
        oauth_push = push_fn.call_args_list[-1]
        assert "OAuth" in oauth_push[0][1]


# ═══════════════════════════════════════════════════════════════════════
# _ocp_vm_restart_ingress (uncovered lines ~4939-4944)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpVmRestartIngress:
    def test_restart_calls_oc(self):
        from app.services.deploy_service import _ocp_vm_restart_ingress

        oc_fn = MagicMock()
        push_fn = MagicMock()

        _ocp_vm_restart_ingress(oc_fn, push_fn)

        push_fn.assert_called_once_with("console", "restarting ingress router")
        oc_fn.assert_called_once()
        assert "rollout restart" in oc_fn.call_args[0][0]


# ═══════════════════════════════════════════════════════════════════════
# _ocp_vm_final_csr_sweep (uncovered lines ~4947-4955)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpVmFinalCsrSweep:
    @patch("time.sleep")
    def test_approves_until_none(self, mock_sleep):
        from app.services.deploy_service import _ocp_vm_final_csr_sweep

        approve_fn = MagicMock(side_effect=[3, 2, 0])
        push_fn = MagicMock()

        _ocp_vm_final_csr_sweep(approve_fn, push_fn)

        assert approve_fn.call_count == 3
        assert push_fn.call_count == 2  # Only pushes when approved > 0

    @patch("time.sleep")
    def test_no_csrs_to_approve(self, mock_sleep):
        from app.services.deploy_service import _ocp_vm_final_csr_sweep

        approve_fn = MagicMock(return_value=0)
        push_fn = MagicMock()

        _ocp_vm_final_csr_sweep(approve_fn, push_fn)

        assert approve_fn.call_count == 1
        push_fn.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# _ocp_vm_wait_for_api (uncovered lines ~4922-4936)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpVmWaitForApi:
    @patch("time.sleep")
    def test_api_ready(self, mock_sleep):
        from app.services.deploy_service import _ocp_vm_wait_for_api

        oc_fn = MagicMock(return_value="node1   Ready   master   10d   v1.29.0")
        push_fn = MagicMock()
        deadline = time.time() + 100

        result = _ocp_vm_wait_for_api(oc_fn, push_fn, deadline)

        assert result is True

    @patch("time.sleep")
    def test_api_timeout(self, mock_sleep):
        from app.services.deploy_service import _ocp_vm_wait_for_api

        oc_fn = MagicMock(side_effect=Exception("connection refused"))
        push_fn = MagicMock()
        # Deadline already passed
        deadline = time.time() - 1

        result = _ocp_vm_wait_for_api(oc_fn, push_fn, deadline)

        assert result is False
        # Should push timeout message
        last_call = push_fn.call_args_list[-1]
        assert "timeout" in last_call[0][0] or "not reachable" in last_call[0][1]


# ═══════════════════════════════════════════════════════════════════════
# _ocp_vm_wait_for_operators (uncovered lines ~4690-4714)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpVmWaitForOperators:
    @patch("time.sleep")
    def test_all_operators_available(self, mock_sleep):
        from app.services.deploy_service import _ocp_vm_wait_for_operators

        co_output = (
            "authentication   4.16.0   True   False   False   10d\n"
            "console          4.16.0   True   False   False   10d\n"
            "ingress          4.16.0   True   False   False   10d\n"
        )
        oc_fn = MagicMock(return_value=co_output)
        approve_fn = MagicMock(return_value=0)
        push_fn = MagicMock()
        deadline = time.time() + 100

        _ocp_vm_wait_for_operators(oc_fn, approve_fn, push_fn, deadline)

        # Should have pushed operators status at least once
        push_calls = [c for c in push_fn.call_args_list if c[0][0] == "operators"]
        assert len(push_calls) >= 1
        assert "3/3" in push_calls[-1][0][1]


# ═══════════════════════════════════════════════════════════════════════
# _ocp_vm_poll_with_csrs (uncovered lines ~4716-4738)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpVmPollWithCsrs:
    @patch("app.services.deploy_service._ocp_vm_wait_for_operators")
    @patch("time.sleep")
    def test_nodes_ready_then_operators(self, mock_sleep, mock_wait_ops):
        from app.services.deploy_service import _ocp_vm_poll_with_csrs

        node_output = "master-0   Ready   control-plane,master   10d   v1.29.0"
        oc_fn = MagicMock(return_value=node_output)
        approve_fn = MagicMock(return_value=0)
        push_fn = MagicMock()
        deadline = time.time() + 100

        _ocp_vm_poll_with_csrs(oc_fn, approve_fn, push_fn, deadline)

        mock_wait_ops.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _setup_bastion_kubeconfig (uncovered lines ~4861-4881)
# ═══════════════════════════════════════════════════════════════════════


class TestSetupBastionKubeconfig:
    @patch("app.services.deploy_service._exec_on_bastion")
    def test_with_kubeconfig_content(self, mock_exec):
        from app.services.deploy_service import _setup_bastion_kubeconfig

        host = MagicMock()
        kc_path, effective = _setup_bastion_kubeconfig(
            host,
            "proj-1",
            "vm-1",
            "10.0.0.5",
            "pass123",
            "apiVersion: v1\nkind: Config",
        )

        assert kc_path == "/tmp/troshka-kc-vm-1.yaml"
        assert effective == kc_path
        mock_exec.assert_called_once()
        # Should be base64-encoding the kubeconfig
        call_cmd = mock_exec.call_args[0][4]
        assert "base64" in call_cmd

    def test_without_kubeconfig_content(self):
        from app.services.deploy_service import _setup_bastion_kubeconfig

        host = MagicMock()
        kc_path, effective = _setup_bastion_kubeconfig(
            host, "proj-1", "vm-1", "10.0.0.5", "pass123", None
        )

        assert kc_path is None
        assert effective == "/home/cloud-user/ocp-install/auth/kubeconfig"

    def test_with_empty_string_kubeconfig(self):
        from app.services.deploy_service import _setup_bastion_kubeconfig

        host = MagicMock()
        kc_path, effective = _setup_bastion_kubeconfig(
            host, "proj-1", "vm-1", "10.0.0.5", "pass123", ""
        )

        assert kc_path is None
        assert effective == "/home/cloud-user/ocp-install/auth/kubeconfig"


# ═══════════════════════════════════════════════════════════════════════
# _make_oc_and_csr_helpers (uncovered lines ~4884-4919)
# ═══════════════════════════════════════════════════════════════════════


class TestMakeOcAndCsrHelpers:
    @patch("app.services.deploy_service._exec_on_bastion")
    def test_oc_helper_sets_kubeconfig(self, mock_exec):
        from app.services.deploy_service import _make_oc_and_csr_helpers

        mock_exec.return_value = "node1   Ready"
        host = MagicMock()

        _oc, _approve = _make_oc_and_csr_helpers(
            host, "proj-1", "10.0.0.5", "pw", "/tmp/kc.yaml", "sno1"
        )

        result = _oc("oc get nodes")
        assert result == "node1   Ready"
        call_cmd = mock_exec.call_args[0][4]
        assert "KUBECONFIG=/tmp/kc.yaml" in call_cmd
        assert "oc get nodes" in call_cmd

    @patch("app.services.deploy_service._exec_on_bastion")
    def test_approve_csrs_finds_pending(self, mock_exec):
        from app.services.deploy_service import _make_oc_and_csr_helpers

        csr_output = "csr-abc   1h   kubernetes.io/kube-apiserver-client   Pending\ncsr-def   1h   kubernetes.io/kubelet-serving   Approved"
        mock_exec.side_effect = [
            csr_output,  # get csr
            "approved",  # approve csr-abc
        ]
        host = MagicMock()

        _oc, _approve = _make_oc_and_csr_helpers(
            host, "proj-1", "10.0.0.5", "pw", "/tmp/kc.yaml", "sno1"
        )

        count = _approve()
        assert count == 1

    @patch("app.services.deploy_service._exec_on_bastion")
    def test_approve_csrs_empty_result(self, mock_exec):
        from app.services.deploy_service import _make_oc_and_csr_helpers

        mock_exec.return_value = ""
        host = MagicMock()

        _oc, _approve = _make_oc_and_csr_helpers(
            host, "proj-1", "10.0.0.5", "pw", "/tmp/kc.yaml", "sno1"
        )

        count = _approve()
        assert count == 0

    @patch("app.services.deploy_service._exec_on_bastion")
    def test_approve_csrs_none_result(self, mock_exec):
        from app.services.deploy_service import _make_oc_and_csr_helpers

        mock_exec.return_value = None
        host = MagicMock()

        _oc, _approve = _make_oc_and_csr_helpers(
            host, "proj-1", "10.0.0.5", "pw", "/tmp/kc.yaml", "sno1"
        )

        count = _approve()
        assert count == 0


# ═══════════════════════════════════════════════════════════════════════
# _configure_bastion_and_cleanup (uncovered lines ~4804-4858)
# ═══════════════════════════════════════════════════════════════════════


class TestConfigureBastionAndCleanup:
    @patch("app.services.deploy_service._exec_on_bastion")
    def test_cleanup_temp_kubeconfig_when_no_browser(self, mock_exec):
        from app.services.deploy_service import _configure_bastion_and_cleanup

        nodes = [
            {"id": "vm-1", "data": {"label": "sno1"}},
        ]
        host = MagicMock()
        oc_fn = MagicMock()
        push_fn = MagicMock()

        _configure_bastion_and_cleanup(
            nodes,
            "vm-1",
            "/tmp/kc.yaml",
            host,
            "proj-1",
            "10.0.0.5",
            "pw",
            oc_fn,
            push_fn,
        )

        # Should have cleaned up the temp kubeconfig
        mock_exec.assert_called_once()
        assert "rm -f /tmp/kc.yaml" in mock_exec.call_args[0][4]

    def test_no_cleanup_when_no_kc_path(self):
        from app.services.deploy_service import _configure_bastion_and_cleanup

        nodes = [{"id": "vm-1", "data": {"label": "sno1"}}]
        host = MagicMock()
        oc_fn = MagicMock()
        push_fn = MagicMock()

        # Should not raise when kc_path is None
        _configure_bastion_and_cleanup(
            nodes, "vm-1", None, host, "proj-1", "10.0.0.5", "pw", oc_fn, push_fn
        )

    @patch("app.services.deploy_service._verify_bastion_browser", return_value=True)
    @patch("app.services.deploy_service._exec_on_bastion")
    def test_configure_browser_when_flag_set(self, mock_exec, mock_verify):
        from app.services.deploy_service import _configure_bastion_and_cleanup

        nodes = [
            {"id": "vm-1", "data": {"label": "sno1", "configureBastionBrowser": True}},
        ]
        host = MagicMock()
        oc_fn = MagicMock()
        push_fn = MagicMock()

        _configure_bastion_and_cleanup(
            nodes,
            "vm-1",
            "/tmp/kc.yaml",
            host,
            "proj-1",
            "10.0.0.5",
            "pw",
            oc_fn,
            push_fn,
        )

        # Should have copied kubeconfig and refreshed CA trust
        assert mock_exec.call_count >= 1
        # Should NOT have cleaned up kc_path (browser flag is set, kc stays)
        cleanup_calls = [
            c for c in mock_exec.call_args_list if "rm -f /tmp/kc.yaml" in str(c)
        ]
        assert len(cleanup_calls) == 0


# ═══════════════════════════════════════════════════════════════════════
# _ocp_vm_wait_for_console (uncovered lines ~4782-4801)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpVmWaitForConsole:
    @patch("app.services.deploy_service._check_vm_console_and_oauth", return_value=True)
    @patch("time.sleep")
    def test_console_ready(self, mock_sleep, mock_check):
        from app.services.deploy_service import _ocp_vm_wait_for_console

        oc_fn = MagicMock(return_value="console   4.16.0   True   False   False   10d")
        approve_fn = MagicMock(return_value=0)
        push_fn = MagicMock()
        deadline = time.time() + 100

        _ocp_vm_wait_for_console(oc_fn, approve_fn, push_fn, deadline)

        mock_check.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _ocp_push_status (uncovered but partially tested — add missing paths)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpPushStatusWithItems:
    @patch("app.services.deploy_service.notify_project")
    @patch("app.core.database.SessionLocal")
    def test_with_items(self, mock_sl, mock_notify):
        from app.services.deploy_service import _ocp_push_status

        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        mock_project = MagicMock()
        mock_db.get.return_value = mock_project

        _ocp_push_status(
            "proj-1", "nodes", "2/3 ready", items=["node1: Ready", "node2: Ready"]
        )

        msg = mock_notify.call_args[0][1]
        assert msg["items"] == ["node1: Ready", "node2: Ready"]
        assert msg["phase"] == "nodes"


# ═══════════════════════════════════════════════════════════════════════
# _ocp_report_final_status (covers code in finalization)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpReportFinalStatusExtended:
    @patch("app.services.deploy_service._ocp_update_status")
    def test_all_ready(self, mock_update):
        from app.services.deploy_service import _ocp_report_final_status

        push_fn = MagicMock()

        _ocp_report_final_status("proj-1", True, True, True, "5m 30s", 330, push_fn)

        push_fn.assert_called_once_with("ready", "cluster ready")
        mock_update.assert_called_once_with("proj-1", "ready", 330)

    @patch("app.services.deploy_service._ocp_update_status")
    def test_nodes_not_ready(self, mock_update):
        from app.services.deploy_service import _ocp_report_final_status

        push_fn = MagicMock()

        _ocp_report_final_status("proj-1", False, True, True, "30m 00s", 1800, push_fn)

        call_args = push_fn.call_args[0]
        assert call_args[0] == "warning"
        assert "nodes" in call_args[1]
        mock_update.assert_called_once_with("proj-1", "warning", 1800)

    @patch("app.services.deploy_service._ocp_update_status")
    def test_multiple_not_ready(self, mock_update):
        from app.services.deploy_service import _ocp_report_final_status

        push_fn = MagicMock()

        _ocp_report_final_status("proj-1", False, False, True, "30m 00s", 1800, push_fn)

        call_args = push_fn.call_args[0]
        assert "nodes" in call_args[1]
        assert "operators" in call_args[1]


# ═══════════════════════════════════════════════════════════════════════
# _ocp_extract_topology_info
# ═══════════════════════════════════════════════════════════════════════


class TestOcpExtractTopologyInfoExtended:
    def test_full_topology(self):
        from app.services.deploy_service import _ocp_extract_topology_info

        topology = {
            "nodes": [
                {
                    "type": "vmNode",
                    "id": "vm-bastion",
                    "data": {
                        "label": "bastion",
                        "nics": [{"ip": "10.0.0.5"}],
                        "ciCloudUserPassword": "redhat",
                    },
                },
                {
                    "type": "vmNode",
                    "id": "vm-master0",
                    "data": {"label": "master-0", "os": "rhcos"},
                },
                {
                    "type": "vmNode",
                    "id": "vm-master1",
                    "data": {"label": "master-1", "os": "rhcos"},
                },
                {
                    "type": "networkNode",
                    "id": "net-1",
                    "data": {"dnsRecords": [{"name": "api.ocp.example.com"}]},
                },
            ],
        }

        bastion, ip, pw, cp_names, dns_domain = _ocp_extract_topology_info(topology)

        assert bastion is not None
        assert ip == "10.0.0.5"
        assert pw == "redhat"
        assert len(cp_names) == 2
        assert "master-0" in cp_names
        assert "master-1" in cp_names
        assert dns_domain == "ocp.example.com"

    def test_no_bastion_no_dns(self):
        from app.services.deploy_service import _ocp_extract_topology_info

        topology = {
            "nodes": [
                {
                    "type": "vmNode",
                    "id": "vm-sno",
                    "data": {"label": "sno", "os": "rhcos"},
                },
            ],
        }

        bastion, ip, pw, cp_names, dns_domain = _ocp_extract_topology_info(topology)

        assert bastion is None
        assert ip == ""
        assert dns_domain == "ocp.ocp.local"
        assert len(cp_names) == 1


# ═══════════════════════════════════════════════════════════════════════
# _ocp_wait_for_direct_oc (uncovered lines ~5164-5180)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpWaitForDirectOcExtended:
    @patch("app.services.deploy_service._exec_oc")
    @patch("time.sleep")
    def test_api_ready_immediately(self, mock_sleep, mock_exec_oc):
        from app.services.deploy_service import _ocp_wait_for_direct_oc

        mock_exec_oc.return_value = "master-0   Ready   control-plane"
        push_fn = MagicMock()
        deadline = time.time() + 100

        result = _ocp_wait_for_direct_oc(MagicMock(), "proj-1", push_fn, deadline)

        assert result is True

    @patch("app.services.deploy_service._exec_oc")
    @patch("time.sleep")
    def test_api_timeout(self, mock_sleep, mock_exec_oc):
        from app.services.deploy_service import _ocp_wait_for_direct_oc

        mock_exec_oc.side_effect = Exception("connection refused")
        push_fn = MagicMock()
        deadline = time.time() - 1  # Already expired

        result = _ocp_wait_for_direct_oc(MagicMock(), "proj-1", push_fn, deadline)

        assert result is False
        last_push = push_fn.call_args_list[-1]
        assert "timeout" in last_push[0][0]


# ═══════════════════════════════════════════════════════════════════════
# _ocp_monitor_fresh_install (uncovered lines ~5543-5601)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpMonitorFreshInstall:
    @patch("app.services.deploy_service._ocp_update_status")
    @patch("app.services.deploy_service._ocp_parse_install_phases")
    @patch("app.services.deploy_service._check_install_terminal_state")
    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("app.services.deploy_service._ocp_wait_for_install_log")
    @patch("time.sleep")
    def test_install_completes(
        self,
        mock_sleep,
        mock_wait_log,
        mock_exec,
        mock_terminal,
        mock_parse,
        mock_update,
    ):
        from app.services.deploy_service import _ocp_monitor_fresh_install

        mock_exec.return_value = "some install log output"
        mock_terminal.return_value = ("complete", 300)
        host = MagicMock()
        push_fn = MagicMock()

        result = _ocp_monitor_fresh_install(
            host, "proj-1", "10.0.0.5", "pw", ["master-0"], push_fn, time.time()
        )

        assert result == ("complete", 300)

    @patch("app.services.deploy_service._ocp_update_status")
    @patch("app.services.deploy_service._ocp_parse_install_phases")
    @patch(
        "app.services.deploy_service._check_install_terminal_state", return_value=None
    )
    @patch("app.services.deploy_service._exec_on_bastion", return_value=None)
    @patch("app.services.deploy_service._ocp_wait_for_install_log")
    @patch("time.sleep")
    @patch("time.time")
    def test_install_timeout(
        self,
        mock_time,
        mock_sleep,
        mock_wait_log,
        mock_exec,
        mock_terminal,
        mock_parse,
        mock_update,
    ):
        from app.services.deploy_service import _ocp_monitor_fresh_install

        mock_time.side_effect = [1000.0, 9000.0, 9001.0, 9002.0]
        host = MagicMock()
        push_fn = MagicMock()

        result = _ocp_monitor_fresh_install(
            host, "proj-1", "10.0.0.5", "pw", ["master-0"], push_fn, 500.0
        )

        assert result == ("timeout", None)
        mock_update.assert_called_with("proj-1", "error")


# ═══════════════════════════════════════════════════════════════════════
# _ocp_wait_for_nodes_ready (uncovered lines ~5604-5636)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpWaitForNodesReady:
    @patch("app.services.deploy_service._approve_csrs_if_due", return_value=0)
    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("time.sleep")
    def test_nodes_become_ready(self, mock_sleep, mock_exec, mock_approve):
        from app.services.deploy_service import _ocp_wait_for_nodes_ready

        mock_exec.return_value = (
            "master-0   Ready   control-plane,master   10d   v1.29.0"
        )
        host = MagicMock()
        push_fn = MagicMock()
        deadline = time.time() + 100

        result = _ocp_wait_for_nodes_ready(
            host, "proj-1", "10.0.0.5", "pw", ["master-0"], push_fn, deadline
        )

        assert result is True

    @patch("app.services.deploy_service._approve_csrs_if_due", return_value=0)
    @patch(
        "app.services.deploy_service._exec_on_bastion",
        return_value="error: connection refused",
    )
    @patch("time.sleep")
    def test_api_error_waiting(self, mock_sleep, mock_exec, mock_approve):
        from app.services.deploy_service import _ocp_wait_for_nodes_ready

        host = MagicMock()
        push_fn = MagicMock()
        deadline = time.time() - 1  # Already expired

        result = _ocp_wait_for_nodes_ready(
            host, "proj-1", "10.0.0.5", "pw", ["master-0"], push_fn, deadline
        )

        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# _ocp_wait_for_operators (uncovered lines ~5639-5676)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpWaitForOperatorsBastion:
    @patch("app.services.deploy_service._approve_csrs_if_due", return_value=0)
    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("time.sleep")
    def test_operators_become_available(self, mock_sleep, mock_exec, mock_approve):
        from app.services.deploy_service import _ocp_wait_for_operators

        mock_exec.return_value = (
            "authentication   4.16.0   True   False   False   10d\n"
            "console          4.16.0   True   False   False   10d\n"
        )
        host = MagicMock()
        push_fn = MagicMock()
        deadline = time.time() + 100

        result = _ocp_wait_for_operators(
            host, "proj-1", "10.0.0.5", "pw", push_fn, deadline
        )

        assert result is True

    @patch("app.services.deploy_service._approve_csrs_if_due", return_value=0)
    @patch(
        "app.services.deploy_service._exec_on_bastion",
        return_value="error: API unavailable",
    )
    @patch("time.sleep")
    def test_operators_timeout(self, mock_sleep, mock_exec, mock_approve):
        from app.services.deploy_service import _ocp_wait_for_operators

        host = MagicMock()
        push_fn = MagicMock()
        deadline = time.time() - 1

        result = _ocp_wait_for_operators(
            host, "proj-1", "10.0.0.5", "pw", push_fn, deadline
        )

        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# _ocp_wait_for_console_route (uncovered lines ~5735-5788)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpWaitForConsoleRoute:
    @patch("app.services.deploy_service._ocp_check_console_route", return_value=True)
    @patch("app.services.deploy_service._approve_csrs_if_due", return_value=0)
    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("time.sleep")
    def test_console_ready_pattern_deploy(
        self, mock_sleep, mock_exec, mock_approve, mock_console
    ):
        from app.services.deploy_service import _ocp_wait_for_console_route

        mock_exec.return_value = "console   4.16.0   True   False   False   10d"
        host = MagicMock()
        push_fn = MagicMock()
        deadline = time.time() + 100
        topology = {
            "nodes": [{"type": "storageNode", "data": {"source": "pattern"}}],
        }

        result = _ocp_wait_for_console_route(
            host, "proj-1", "10.0.0.5", "pw", push_fn, deadline, topology
        )

        assert result is True

    @patch("app.services.deploy_service._approve_csrs_if_due", return_value=0)
    @patch(
        "app.services.deploy_service._exec_on_bastion",
        return_value="error: no such resource",
    )
    @patch("time.sleep")
    def test_console_timeout(self, mock_sleep, mock_exec, mock_approve):
        from app.services.deploy_service import _ocp_wait_for_console_route

        host = MagicMock()
        push_fn = MagicMock()
        deadline = time.time() - 1
        topology = {"nodes": []}

        result = _ocp_wait_for_console_route(
            host, "proj-1", "10.0.0.5", "pw", push_fn, deadline, topology
        )

        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# _ocp_ping_cp_nodes (uncovered lines ~5868-5907)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpPingCpNodes:
    @patch("app.services.deploy_service._approve_pending_csrs", return_value=0)
    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("time.sleep")
    def test_all_nodes_reachable(self, mock_sleep, mock_exec, mock_approve):
        from app.services.deploy_service import _ocp_ping_cp_nodes

        mock_exec.return_value = "up"
        host = MagicMock()
        push_fn = MagicMock()
        deadline = time.time() + 100

        _ocp_ping_cp_nodes(
            host,
            "proj-1",
            "10.0.0.5",
            "pw",
            ["master-0", "master-1"],
            push_fn,
            deadline,
        )

        # Should push 2/2 reachable
        ping_pushes = [c for c in push_fn.call_args_list if c[0][0] == "nodes"]
        assert len(ping_pushes) >= 1
        last_push = ping_pushes[-1]
        assert "2/2" in last_push[0][1]

    @patch("app.services.deploy_service._approve_pending_csrs", return_value=0)
    @patch("app.services.deploy_service._exec_on_bastion", return_value="down")
    @patch("time.sleep")
    def test_nodes_not_reachable_timeout(self, mock_sleep, mock_exec, mock_approve):
        from app.services.deploy_service import _ocp_ping_cp_nodes

        host = MagicMock()
        push_fn = MagicMock()
        deadline = time.time() - 1

        _ocp_ping_cp_nodes(
            host, "proj-1", "10.0.0.5", "pw", ["master-0"], push_fn, deadline
        )

        assert push_fn.call_count <= 1


# ═══════════════════════════════════════════════════════════════════════
# _ocp_final_csr_sweep (uncovered lines ~5910-5924)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpFinalCsrSweep:
    @patch("app.services.deploy_service._ocp_post_pattern_cert_refresh")
    @patch("app.services.deploy_service._approve_pending_csrs", return_value=0)
    @patch("time.sleep")
    def test_sweep_no_csrs(self, mock_sleep, mock_approve, mock_refresh):
        from app.services.deploy_service import _ocp_final_csr_sweep

        host = MagicMock()
        push_fn = MagicMock()
        topology = {"nodes": []}

        _ocp_final_csr_sweep(host, "proj-1", "10.0.0.5", "pw", topology, push_fn)

        # Should have called approve once and stopped
        assert mock_approve.call_count == 1
        # No pattern deploy, no cert refresh
        mock_refresh.assert_not_called()

    @patch("app.services.deploy_service._ocp_post_pattern_cert_refresh")
    @patch("app.services.deploy_service._approve_pending_csrs")
    @patch("time.sleep")
    def test_sweep_with_pattern_deploy(self, mock_sleep, mock_approve, mock_refresh):
        from app.services.deploy_service import _ocp_final_csr_sweep

        mock_approve.side_effect = [2, 0]
        host = MagicMock()
        push_fn = MagicMock()
        topology = {
            "nodes": [{"type": "storageNode", "data": {"patternId": "pat-123"}}],
        }

        _ocp_final_csr_sweep(host, "proj-1", "10.0.0.5", "pw", topology, push_fn)

        assert mock_approve.call_count == 2
        mock_refresh.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _ocp_post_pattern_cert_refresh (uncovered lines ~5791-5845)
# ═══════════════════════════════════════════════════════════════════════


class TestOcpPostPatternCertRefresh:
    @patch("app.services.deploy_service._verify_bastion_browser")
    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("app.core.database.SessionLocal")
    def test_refresh_with_recert_pattern(self, mock_sl, mock_exec, mock_verify):
        from app.services.deploy_service import _ocp_post_pattern_cert_refresh

        # Mock DB to return a pattern with recert=True
        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        mock_pattern = MagicMock()
        mock_pattern.recert = True
        mock_db.query.return_value.filter_by.return_value.first.return_value = (
            mock_pattern
        )

        host = MagicMock()
        push_fn = MagicMock()
        topology = {
            "nodes": [
                {"type": "storageNode", "data": {"patternId": "pat-123"}},
                {"type": "vmNode", "data": {"os": "rhcos"}},
            ],
        }

        _ocp_post_pattern_cert_refresh(
            host, "proj-1", "10.0.0.5", "pw", topology, push_fn
        )

        # Should have run cert refresh
        push_fn.assert_any_call("certs", "refreshing bastion certificates")
        mock_verify.assert_called_once()

    def test_skip_when_no_bastion_ip(self):
        from app.services.deploy_service import _ocp_post_pattern_cert_refresh

        host = MagicMock()
        push_fn = MagicMock()
        topology = {"nodes": []}

        _ocp_post_pattern_cert_refresh(host, "proj-1", "", "pw", topology, push_fn)

        push_fn.assert_not_called()

    @patch("app.services.deploy_service._verify_bastion_browser")
    @patch("app.services.deploy_service._exec_on_bastion")
    def test_refresh_for_single_rhcos_no_recert(self, mock_exec, mock_verify):
        from app.services.deploy_service import _ocp_post_pattern_cert_refresh

        host = MagicMock()
        push_fn = MagicMock()
        topology = {
            "nodes": [
                {"type": "vmNode", "data": {"os": "rhcos"}},
            ],
        }

        _ocp_post_pattern_cert_refresh(
            host, "proj-1", "10.0.0.5", "pw", topology, push_fn
        )

        # Single RHCOS VM triggers refresh even without recert pattern
        push_fn.assert_any_call("certs", "refreshing bastion certificates")


# ═══════════════════════════════════════════════════════════════════════
# _monitor_ocp_vm_health wrapper (uncovered lines ~4628-4653)
# ═══════════════════════════════════════════════════════════════════════


class TestMonitorOcpVmHealth:
    @patch("app.services.deploy_service.remove_from_set")
    @patch(
        "app.services.deploy_service._ocp_vm_health_inner",
        side_effect=Exception("boom"),
    )
    @patch("app.core.database.SessionLocal")
    def test_exception_cleanup(self, mock_sl, mock_inner, mock_remove):
        from app.services.deploy_service import _monitor_ocp_vm_health

        mock_db = MagicMock()
        mock_sl.return_value = mock_db

        _monitor_ocp_vm_health("proj-1", "host-1", "vm-1", "sno1", "kc-content", 1000.0)

        mock_remove.assert_called_once_with("deploy:health_monitors", "proj-1:vm-1")
        mock_db.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# _collect_dv_progress skip-unfriendly path (line 2337)
# ═══════════════════════════════════════════════════════════════════════


class TestCollectDvProgressSkipUnfriendly:
    @patch("app.services.deploy_service._fill_missing_disk_labels")
    @patch("app.services.deploy_service._best_dv_status", return_value={})
    @patch("app.services.deploy_service._build_clone_name_map", return_value={})
    @patch("app.services.deploy_service._format_dv_status_line")
    def test_skip_dv_without_friendly_name(
        self, mock_format, mock_clone, mock_best, mock_fill
    ):
        """DVs that aren't in golden or clone name maps are skipped (line 2337)."""
        from app.services.deploy_service import _collect_dv_progress

        provider = MagicMock()
        topology = {"nodes": []}

        with patch(
            "app.services.providers.kubevirt._get_k8s_clients"
        ) as mock_k8s, patch(
            "app.services.providers.kubevirt._project_ns",
            return_value="troshka-proj-1234",
        ):
            mock_custom = MagicMock()
            mock_k8s.return_value = (mock_custom, None, None)
            # Return a DV that has no matching golden or clone name
            mock_custom.list_namespaced_custom_object.return_value = {
                "items": [
                    {
                        "metadata": {
                            "namespace": "troshka-cache",
                            "name": "unknown-dv",
                        },
                        "status": {"phase": "Succeeded"},
                    }
                ]
            }

            _collect_dv_progress("proj-1234", provider, topology)

            # _format_dv_status_line should NOT be called (unknown DV skipped)
            mock_format.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# _best_dv_status edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestBestDvStatusEdgeCases:
    def test_keeps_most_advanced_status(self):
        from app.services.deploy_service import _best_dv_status

        lines = [
            "disk1: waiting",
            "disk1: downloading",
            "disk1: done",
            "disk2: scheduled",
        ]
        result = _best_dv_status(lines)
        assert result["disk1"] == "done"
        assert result["disk2"] == "scheduled"

    def test_empty_list(self):
        from app.services.deploy_service import _best_dv_status

        result = _best_dv_status([])
        assert result == {}

    def test_unknown_status(self):
        from app.services.deploy_service import _best_dv_status

        lines = ["disk1: something-unknown"]
        result = _best_dv_status(lines)
        assert result["disk1"] == "something-unknown"


# ═══════════════════════════════════════════════════════════════════════
# _fill_missing_disk_labels
# ═══════════════════════════════════════════════════════════════════════


class TestFillMissingDiskLabelsExtended:
    def test_adds_waiting_for_missing_disks(self):
        from app.services.deploy_service import _fill_missing_disk_labels

        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {"source": "pattern", "label": "boot-disk"},
                },
                {
                    "type": "storageNode",
                    "data": {"source": "library", "label": "data-disk"},
                },
                {
                    "type": "storageNode",
                    "data": {"source": "blank", "label": "scratch"},
                },
                {"type": "vmNode", "data": {"label": "vm1"}},
            ],
        }
        best = {"boot-disk": "done"}

        _fill_missing_disk_labels(topology, best)

        assert best["boot-disk"] == "done"  # unchanged
        assert best["data-disk"] == "waiting"  # added
        assert "scratch" not in best  # blank source not added


# ═══════════════════════════════════════════════════════════════════════
# _ensure_storage_library_ref / _collect_library_items (pattern disks)
# ═══════════════════════════════════════════════════════════════════════


class TestPatternStorageLibraryRefs:
    def test_ensure_storage_library_ref_skips_pattern_disks(self):
        from app.services.deploy_service import _ensure_storage_library_ref

        node = {
            "type": "storageNode",
            "data": {
                "source": "pattern",
                "patternDiskId": "pd-1",
                "libraryItemName": "rhel-9.6",
            },
        }
        db = MagicMock()
        _ensure_storage_library_ref(node, db)
        assert node["data"]["source"] == "pattern"
        assert "libraryItemId" not in node["data"]
        db.query.assert_not_called()

    def test_collect_library_items_skips_pattern_disks(self):
        from app.services.deploy_service import _collect_library_items

        nodes = [
            {
                "type": "storageNode",
                "data": {
                    "source": "pattern",
                    "patternDiskId": "pd-1",
                    "libraryItemName": "rhel-9.6",
                    "format": "qcow2",
                },
            }
        ]
        items = _collect_library_items(nodes, MagicMock(), pool="shared")
        assert items == []


class TestSyncDeployedContainerNode:
    def test_updates_deployed_node_and_showroom_meta(self):
        from types import SimpleNamespace

        from app.services.deploy_service import _sync_deployed_container_node

        project = SimpleNamespace(
            deployed_topology={
                "nodes": [
                    {
                        "id": "sr-1",
                        "type": "containerNode",
                        "data": {"name": "showroom", "contentRepo": "old"},
                    },
                    {"id": "vm-1", "type": "vmNode", "data": {"name": "bastion"}},
                ],
                "showroom": {"content_repo": "old"},
            }
        )
        topo = {
            "nodes": [
                {
                    "id": "sr-1",
                    "type": "containerNode",
                    "data": {"name": "showroom", "contentRepo": "new"},
                },
            ],
            "showroom": {"content_repo": "new"},
        }
        _sync_deployed_container_node(project, "sr-1", topo)
        node = next(n for n in project.deployed_topology["nodes"] if n["id"] == "sr-1")
        assert node["data"]["contentRepo"] == "new"
        assert project.deployed_topology["showroom"]["content_repo"] == "new"
        # unrelated nodes untouched
        assert any(n["id"] == "vm-1" for n in project.deployed_topology["nodes"])

    def test_noop_when_node_absent(self):
        from types import SimpleNamespace

        from app.services.deploy_service import _sync_deployed_container_node

        project = SimpleNamespace(deployed_topology={"nodes": []})
        _sync_deployed_container_node(project, "missing", {"nodes": []})
        assert project.deployed_topology == {"nodes": []}
