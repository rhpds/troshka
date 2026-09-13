# src/backend/tests/test_workload_run_service.py
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models.project import Project
from app.models.provider import Provider
from app.models.workload_run import WorkloadRun
from app.services.workloads import run_service
from tests.conftest import TestSession


def _active_project(db, name="rs"):
    prov = Provider(name=f"prov-{uuid.uuid4()}", type="troshka", credentials="{}")
    db.add(prov)
    db.flush()
    proj = Project(
        name=name,
        owner_id=str(uuid.uuid4()),
        provider_id=prov.id,
        state="active",
        topology={"nodes": []},
    )
    db.add(proj)
    db.flush()
    proj_id = proj.id
    db.commit()
    return db.get(Project, proj_id)


def test_start_workload_run_creates_row_and_enqueues(monkeypatch):
    db = TestSession()
    try:
        proj = _active_project(db)
        assert proj is not None
        enq = MagicMock()
        monkeypatch.setattr(run_service, "enqueue_job", enq)
        run = run_service.start_workload_run(
            db,
            project_id=proj.id,
            kind="catalog_item",
            catalog_item="agd-v2.x.prod",
            owner_id="u1",
        )
        assert run.status == "pending"
        assert run.catalog_item == "agd-v2.x.prod"
        assert enq.called
    finally:
        db.close()


def test_run_workload_job_happy_path(monkeypatch):
    db = TestSession()
    proj = _active_project(db, name="rs2")
    assert proj is not None
    run = WorkloadRun(
        project_id=proj.id,
        kind="catalog_item",
        catalog_item="agd-v2.x.prod",
        status="pending",
    )
    db.add(run)
    db.commit()
    rid = run.id
    db.close()

    monkeypatch.setattr(run_service, "SessionLocal", TestSession)
    monkeypatch.setattr(
        run_service,
        "_host_for_project",
        lambda db, p: SimpleNamespace(id="h1", host_type="shared"),
    )
    monkeypatch.setattr(
        run_service,
        "resolve_catalog_item",
        lambda db, cid: SimpleNamespace(
            extra_vars={"config": "openshift-workloads"},
            ee_image="ee:1",
            scm_ref="main",
            requirements_content=None,
        ),
    )
    monkeypatch.setattr(run_service, "mint_run_key", lambda db, p: "trk_k")
    monkeypatch.setattr(
        run_service, "validate_ansible_groups", lambda t, require_bastion: None
    )

    # Capture build_run_command calls to verify agnosticd_v2_url and scm_ref are passed
    build_run_calls = []

    def mock_build_run_command(
        item, paths, *, agnosticd_v2_url, scm_ref, kubeconfig=None, net_prelude=""
    ):
        build_run_calls.append(
            {
                "agnosticd_v2_url": agnosticd_v2_url,
                "scm_ref": scm_ref,
                "kubeconfig": kubeconfig,
                "net_prelude": net_prelude,
            }
        )
        return ["bash", "-lc", "echo test"]

    monkeypatch.setattr(run_service, "build_run_command", mock_build_run_command)
    # Isolate networking resolution (its own tests cover both providers).
    monkeypatch.setattr(
        run_service, "_resolve_pod_networks", lambda h, p, t: ([], "", "")
    )
    launched = MagicMock(return_value="job-1")
    monkeypatch.setattr(run_service, "launch_runner_pod", launched)
    monkeypatch.setattr(run_service, "_start_workload_monitor", lambda *a, **k: None)

    run_service.run_workload_job(rid)

    db = TestSession()
    row = db.get(WorkloadRun, rid)
    assert row is not None
    assert row.status == "running"
    assert row.started_at is not None
    assert launched.called
    # Verify build_run_command was called with agnosticd_v2_url and scm_ref
    assert len(build_run_calls) == 1
    assert (
        build_run_calls[0]["agnosticd_v2_url"]
        == "https://github.com/rhpds/agnosticd-v2.git"
    )
    assert build_run_calls[0]["scm_ref"] == "main"
    db.close()


def test_synthesize_ad_hoc_minimal():
    """Test ad-hoc role synthesis with minimal FQCN."""
    db = TestSession()
    run = WorkloadRun(
        project_id=str(uuid.uuid4()),
        kind="ad_hoc",
        role_fqcn="redhat.openshift.install_operator",
        status="pending",
    )
    db.add(run)
    db.commit()

    item = run_service._synthesize_ad_hoc(db, run)
    assert item.extra_vars["config"] == "openshift-workloads"
    assert item.extra_vars["workloads"] == ["redhat.openshift.install_operator"]
    # No requirements_content provided → none is inferred (a bare Galaxy name is
    # wrong: agnosticd workload collections are git-hosted, not on Galaxy).
    assert item.requirements_content is None
    # Verify ad-hoc uses the configured default EE image (whatever it is set to),
    # not a hardcoded value — catalog items override via __meta__.deployer.
    from app.core.config import config as _cfg

    assert item.ee_image == getattr(_cfg.workloads, "default_ee_image", None)
    assert item.scm_ref is None
    db.close()


def test_synthesize_ad_hoc_with_requirements_content():
    """Ad-hoc synthesis passes caller-supplied requirements_content through
    verbatim (AgnosticD-compatible git-sourced collections)."""
    db = TestSession()
    reqs = {
        "collections": [
            {
                "name": "https://github.com/rhpds/core_workloads.git",
                "type": "git",
                "version": "main",
            }
        ]
    }
    run = WorkloadRun(
        project_id=str(uuid.uuid4()),
        kind="ad_hoc",
        role_fqcn="agnosticd.core_workloads.ocp4_workload_example",
        requirements_content=reqs,
        status="pending",
    )
    db.add(run)
    db.commit()

    item = run_service._synthesize_ad_hoc(db, run)
    assert item.extra_vars["workloads"] == [
        "agnosticd.core_workloads.ocp4_workload_example"
    ]
    # requirements_content threaded through unchanged (not inferred/rewritten).
    assert item.requirements_content == reqs
    db.close()


def test_synthesize_ad_hoc_invalid_fqcn():
    """Test ad-hoc synthesis rejects malformed FQCNs."""
    db = TestSession()
    run = WorkloadRun(
        project_id=str(uuid.uuid4()),
        kind="ad_hoc",
        role_fqcn="invalid.role",
        status="pending",
    )
    db.add(run)
    db.commit()

    try:
        run_service._synthesize_ad_hoc(db, run)
        assert False, "Expected RuntimeError for invalid FQCN"
    except RuntimeError as e:
        assert "Invalid role FQCN" in str(e)
    finally:
        db.close()


def test_infer_status_from_logs_multihost_recap_with_failure():
    """Log inference should NOT infer succeeded when multi-host recap has failures."""
    logs = """
PLAY RECAP *********************************************************************
host1.example.com          : ok=10   changed=3    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
host2.example.com          : ok=8    changed=2    unreachable=0    failed=2    skipped=0    rescued=0    ignored=0
"""
    status = run_service._infer_status_from_logs(logs)
    assert status == "error"  # NOT "succeeded" despite host1 having failed=0


def test_infer_status_from_logs_all_hosts_succeeded():
    """Log inference should infer succeeded when all hosts have failed=0."""
    logs = """
PLAY RECAP *********************************************************************
host1.example.com          : ok=10   changed=3    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
host2.example.com          : ok=8    changed=2    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
"""
    status = run_service._infer_status_from_logs(logs)
    assert status == "succeeded"


def test_infer_status_from_logs_no_recap():
    """Log inference without PLAY RECAP should infer error."""
    logs = "Some ansible output without recap"
    status = run_service._infer_status_from_logs(logs)
    assert status == "error"


def test_run_workload_job_ocp_no_kubeconfig_fails(monkeypatch):
    """OCP project with no resolvable kubeconfig should fail the run."""
    db = TestSession()
    proj = _active_project(db, name="rs-ocp-bad")
    assert proj is not None
    # Topology has a node with ocpKubeconfig (so _has_ocp returns True),
    # but NOT a vmNode with clusterId (so _stored_cluster_creds yields nothing)
    proj.topology = {
        "nodes": [{"type": "networkNode", "data": {"ocpKubeconfig": True}}]
    }
    # deployed_topology is None so _has_ocp falls back to topology
    proj.deployed_topology = None
    db.commit()
    run = WorkloadRun(
        project_id=proj.id,
        kind="catalog_item",
        catalog_item="agd-v2.x.prod",
        status="pending",
    )
    db.add(run)
    db.commit()
    rid = run.id
    db.close()

    monkeypatch.setattr(run_service, "SessionLocal", TestSession)
    monkeypatch.setattr(
        run_service,
        "_host_for_project",
        lambda db, p: SimpleNamespace(id="h1", host_type="shared"),
    )
    monkeypatch.setattr(
        run_service,
        "resolve_catalog_item",
        lambda db, cid: SimpleNamespace(
            extra_vars={"config": "openshift-workloads"},
            ee_image="ee:1",
            scm_ref="main",
            requirements_content=None,
        ),
    )
    monkeypatch.setattr(run_service, "mint_run_key", lambda db, p: "trk_k")
    monkeypatch.setattr(
        run_service, "validate_ansible_groups", lambda t, require_bastion: None
    )
    monkeypatch.setattr(run_service, "build_inventory_yaml", lambda *a, **k: "inv")

    # The job should raise before reaching launch_runner_pod
    try:
        run_service.run_workload_job(rid)
        assert False, "Expected RuntimeError for OCP project with no kubeconfig"
    except RuntimeError as e:
        assert "targets OCP but no admin kubeconfig" in str(e)

    # Verify the run was failed
    db = TestSession()
    row = db.get(WorkloadRun, rid)
    assert row is not None
    assert row.status == "error"
    assert row.error is not None
    assert "kubeconfig" in row.error.lower()
    db.close()


def test_resolve_pod_networks_kubevirt(monkeypatch):
    """KubeVirt: cluster NADs + dnsmasq (.2) nameserver + self-assign-IP prelude."""
    import app.services.deploy_service as ds
    import app.services.ocp.ops_pod_install as opi
    import app.services.ocp.ops_pod_scaffold as ops

    monkeypatch.setattr(ops, "ops_pod_network_nads", lambda t: (["net-abc-nad"], None))
    monkeypatch.setattr(
        ds,
        "_kubevirt_ops_pod_net_ips",
        lambda t: ([("net1", "192.168.47.50/24")], None),
    )
    monkeypatch.setattr(ds, "_kubevirt_ops_pod_dns", lambda a: "192.168.47.2")
    monkeypatch.setattr(opi, "_self_assign_net_ips", lambda a: "ip addr add x\n")

    host = SimpleNamespace(host_type="kubevirt-cluster")
    project = SimpleNamespace(id="p1", vni_map={})
    networks, dns, prelude = run_service._resolve_pod_networks(host, project, {})
    assert networks == ["net-abc-nad"]
    assert dns == "192.168.47.2"
    assert "ip addr add" in prelude


def test_resolve_pod_networks_troshkad(monkeypatch):
    """troshkad: runner transit network (.5, NOT ops .4) + gateway dnsmasq, no prelude."""
    import app.services.deploy_topology as dt
    import app.services.ocp.ops_pod_scaffold as ops

    monkeypatch.setattr(dt, "_gateway_connected_dns_nameserver", lambda t: "10.0.0.1")
    monkeypatch.setattr(
        ops, "runner_pod_infra_network", lambda vni, dns_nameserver="": [{"vni": 1}]
    )

    host = SimpleNamespace(host_type="shared")
    project = SimpleNamespace(id="p1", vni_map={"n1": 1})
    networks, dns, prelude = run_service._resolve_pod_networks(host, project, {})
    assert networks == [{"vni": 1}]
    assert dns == "10.0.0.1"
    assert prelude == ""


def test_runner_pod_infra_network_distinct_ip():
    """Runner transit IP is .5 — distinct from showroom (.3) and ops (.4)."""
    from app.services.ocp.ops_pod_scaffold import (
        ops_pod_infra_network,
        runner_pod_infra_network,
    )

    vni_map = {"39483e1f-2bdb-420c-bd7c-67c1da6c21e1": 2078}
    runner = runner_pod_infra_network(vni_map, dns_nameserver="10.0.0.1")
    ops = ops_pod_infra_network(vni_map, dns_nameserver="10.0.0.1")
    assert runner and ops
    assert runner[0]["ip"].endswith(".5")
    assert ops[0]["ip"].endswith(".4")
    assert runner[0]["ip"] != ops[0]["ip"]
    # Same subnet + gateway + dns as the ops pod (only the host IP differs).
    assert runner[0]["cidr"] == ops[0]["cidr"]
    assert runner[0]["gateway"] == ops[0]["gateway"]
    assert runner[0]["dns_nameserver"] == "10.0.0.1"
