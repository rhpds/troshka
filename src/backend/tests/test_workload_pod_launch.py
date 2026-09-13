from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.workloads import pod_launch


def test_build_artifact_files_maps_paths():
    paths = pod_launch.RunPaths()
    files = pod_launch.build_artifact_files(
        extra_vars={"a": 1},
        inventory_yaml="plugin: troshka.cloud.troshka\n",
        cluster_access={"cl": {"api_url": "u", "api_token": "t"}},
        cloud_creds=None,
        paths=paths,
    )
    assert paths.extra_vars in files
    assert "troshka.cloud" in files[paths.inventory]
    assert "api_token" in files[paths.cluster_access]


def test_build_run_command_invokes_ansible_playbook():
    paths = pod_launch.RunPaths()
    item = SimpleNamespace(
        extra_vars={"config": "openshift-workloads"},
        ee_image="ee:1",
        scm_ref="main",
        requirements_content=None,
    )
    cmd = pod_launch.build_run_command(
        item,
        paths,
        agnosticd_v2_url="https://github.com/rhpds/agnosticd-v2.git",
        scm_ref="main",
    )
    joined = " ".join(cmd)
    assert "git clone" in joined
    assert "agnosticd-v2" in joined
    assert "--branch 'main'" in joined or "checkout 'main'" in joined
    assert "ansible-playbook" in joined
    assert "tee" in joined and paths.log in joined
    assert paths.inventory in joined
    assert paths.extra_vars in joined
    # Verify ANSIBLE_CONFIG points to repo root's ansible.cfg (before ansible-playbook)
    assert "export ANSIBLE_CONFIG=" in joined
    assert "agnosticd-v2/ansible.cfg" in joined
    # Verify git clone appears BEFORE ansible-playbook in the command
    git_pos = joined.find("git clone")
    ansible_pos = joined.find("ansible-playbook")
    assert git_pos < ansible_pos, "git clone must appear before ansible-playbook"
    # Verify ANSIBLE_CONFIG export appears BEFORE ansible-playbook
    config_pos = joined.find("export ANSIBLE_CONFIG=")
    assert (
        config_pos < ansible_pos
    ), "ANSIBLE_CONFIG must be set before ansible-playbook"


def test_launch_runner_pod_troshkad(monkeypatch):
    host = SimpleNamespace(id="h1", host_type="shared")
    project = SimpleNamespace(id="p1")
    fake_start = MagicMock(return_value="job-123")
    monkeypatch.setattr(pod_launch, "start_job", fake_start)
    job = pod_launch.launch_runner_pod(
        host,
        project,
        ee_image="ee:1",
        command=["bash", "-lc", "x"],
        files={"/run/x": "y"},
        networks=[],
    )
    assert job == "job-123"
    args, _kwargs = fake_start.call_args
    assert args[1] == "/pods/create"
    params = args[2]
    assert params["containers"][0]["image"] == "ee:1"
    assert params["files"] == {"/run/x": "y"}
    assert params["privileged"] is True


def test_launch_runner_pod_kubevirt(monkeypatch):
    host = SimpleNamespace(id="h1", host_type="kubevirt-cluster", provider_id="prov1")
    project = SimpleNamespace(id="p1234567890", provider_id="prov1")
    fake_provider = MagicMock()
    fake_create = MagicMock()
    fake_build_manifests = MagicMock(return_value=({"kind": "Pod"}, {"kind": "Secret"}))
    monkeypatch.setattr(
        pod_launch, "_provider_for_host", MagicMock(return_value=fake_provider)
    )
    monkeypatch.setattr(
        pod_launch, "_namespace_for_project", MagicMock(return_value="ns-p1234567")
    )
    with patch("app.services.providers.kubevirt.create_ops_pod", fake_create), patch(
        "app.services.ocp.ops_pod_scaffold.build_ops_pod_kubevirt_manifests",
        fake_build_manifests,
    ):
        job = pod_launch.launch_runner_pod(
            host,
            project,
            ee_image="ee:2",
            command=["bash", "-lc", "y"],
            files={"/run/a": "b"},
            networks=[],
        )
    assert job == "workload-runner-p1234567"
    args, _kwargs = fake_create.call_args
    assert args[1] == "p1234567890"  # project_id is 2nd positional arg
    # Assert EE image is passed to manifest builder
    assert fake_build_manifests.call_args.kwargs["image"] == "ee:2"
    # Assert runner Pod gets restart_policy="Never" (not "Always" like OCP ops pod)
    assert fake_build_manifests.call_args.kwargs["restart_policy"] == "Never"


def test_build_artifact_files_with_kubeconfig():
    """Verify kubeconfig lands in the files map when provided."""
    paths = pod_launch.RunPaths()
    files = pod_launch.build_artifact_files(
        extra_vars={"a": 1},
        inventory_yaml="plugin: troshka.cloud.troshka\n",
        cluster_access={},
        cloud_creds=None,
        kubeconfig="KC-CONTENTS",
        paths=paths,
    )
    assert paths.kubeconfig in files
    assert files[paths.kubeconfig] == "KC-CONTENTS"


def test_build_artifact_files_without_kubeconfig():
    """Verify kubeconfig is omitted from the files map when None."""
    paths = pod_launch.RunPaths()
    files = pod_launch.build_artifact_files(
        extra_vars={"a": 1},
        inventory_yaml="plugin: troshka.cloud.troshka\n",
        cluster_access={},
        cloud_creds=None,
        kubeconfig=None,
        paths=paths,
    )
    assert paths.kubeconfig not in files


def test_build_run_command_exports_kubeconfig():
    """Verify KUBECONFIG is exported before ansible-playbook invocation."""
    paths = pod_launch.RunPaths()
    item = SimpleNamespace(
        extra_vars={"config": "openshift-workloads"},
        ee_image="ee:1",
        scm_ref="main",
        requirements_content=None,
    )
    cmd = pod_launch.build_run_command(
        item,
        paths,
        agnosticd_v2_url="https://github.com/rhpds/agnosticd-v2.git",
        scm_ref="main",
    )
    joined = " ".join(cmd)
    # Verify KUBECONFIG is exported
    assert "export KUBECONFIG=" in joined
    assert paths.kubeconfig in joined
    # Verify KUBECONFIG export appears BEFORE ansible-playbook
    kubeconfig_pos = joined.find("export KUBECONFIG=")
    ansible_pos = joined.find("ansible-playbook")
    assert (
        kubeconfig_pos < ansible_pos
    ), "KUBECONFIG must be set before ansible-playbook"
