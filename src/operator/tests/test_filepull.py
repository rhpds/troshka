"""Tests for the snapshot-based VM file-pull helper (helpers/filepull)."""

import base64


class TestCandidatePaths:
    def test_direct_path_first(self):
        from helpers.filepull import candidate_paths

        c = candidate_paths("/etc/hostname")
        assert c[0] == "/etc/hostname"  # non-ostree / inspected root

    def test_etc_gets_ostree_deployment_candidate(self):
        from helpers.filepull import candidate_paths

        c = candidate_paths(
            "/etc/kubernetes/static-pod-resources/kube-apiserver-certs/"
            "secrets/node-kubeconfigs/lb-ext.kubeconfig"
        )
        assert (
            "/ostree/deploy/*/deploy/*/etc/kubernetes/static-pod-resources/"
            "kube-apiserver-certs/secrets/node-kubeconfigs/lb-ext.kubeconfig" in c
        )

    def test_var_uses_shared_stateroot_not_deployment(self):
        from helpers.filepull import candidate_paths

        c = candidate_paths("/var/lib/kubelet/kubeconfig")
        # /var is shared per-stateroot on ostree (NOT under deploy/<hash>)
        assert "/ostree/deploy/*/var/lib/kubelet/kubeconfig" in c
        # must NOT use the per-deployment (double-deploy) form for /var
        assert not any("/deploy/*/deploy/" in p for p in c)

    def test_relative_path_is_normalized_absolute(self):
        from helpers.filepull import candidate_paths

        c = candidate_paths("etc/hostname")
        assert c[0] == "/etc/hostname"


class TestParseFilepullOutput:
    def test_extracts_and_decodes_base64(self):
        from helpers.filepull import parse_filepull_output

        payload = b"hello kubeconfig\n"
        b64 = base64.b64encode(payload).decode()
        log = f"HASH=abc\nSIZE=16\nB64_BEGIN\n{b64}\nB64_END\ndone\n"
        assert parse_filepull_output(log) == payload

    def test_missing_markers_returns_none(self):
        from helpers.filepull import parse_filepull_output

        assert parse_filepull_output("no markers here") is None

    def test_empty_payload_returns_none(self):
        from helpers.filepull import parse_filepull_output

        assert parse_filepull_output("B64_BEGIN\n\nB64_END") is None


class TestBuildFilepullPod:
    def _pod(self, **kw):
        from helpers.filepull import build_filepull_pod

        return build_filepull_pod(
            "fp-abc",
            "ns1",
            "tmp-pvc-abc",
            "/etc/kubernetes/x/lb-ext.kubeconfig",
            **kw,
        )

    def test_mounts_temp_pvc_at_disk(self):
        pod = self._pod()
        vols = pod["spec"]["volumes"]
        assert vols[0]["persistentVolumeClaim"]["claimName"] == "tmp-pvc-abc"
        vm = pod["spec"]["containers"][0]["volumeMounts"][0]
        assert vm["mountPath"] == "/disk"

    def test_uses_recert_sa_and_privileged_by_default(self):
        pod = self._pod()
        assert pod["spec"]["serviceAccountName"] == "troshka-recert"
        assert pod["spec"]["containers"][0]["securityContext"]["privileged"] is True

    def test_never_restarts(self):
        pod = self._pod()
        assert pod["spec"]["restartPolicy"] == "Never"

    def test_command_references_path_markers_and_ostree_candidate(self):
        pod = self._pod()
        cmd = " ".join(pod["spec"]["containers"][0]["command"])
        assert "B64_BEGIN" in cmd and "B64_END" in cmd
        assert "/etc/kubernetes/x/lb-ext.kubeconfig" in cmd
        assert "/ostree/deploy/*/deploy/*/etc/kubernetes/x/lb-ext.kubeconfig" in cmd
        assert "LIBGUESTFS_BACKEND=direct" in cmd

    def test_custom_sa_override(self):
        pod = self._pod(service_account="troshka-export")
        assert pod["spec"]["serviceAccountName"] == "troshka-export"
