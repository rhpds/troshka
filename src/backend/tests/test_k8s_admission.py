"""Shared K8s admission canonicalize: numbers, SA projection, structural contains."""

from copy import deepcopy

import pytest

from app.services.k8s_admission import (
    canonical_equal,
    is_bound_service_account_volume,
    service_account_mount,
    structural_contains,
)


def _sa_volume(*, openshift=False, expiration=3607, name="kube-api-access-nwth7"):
    sources = [
        {"serviceAccountToken": {"expirationSeconds": expiration, "path": "token"}},
        {
            "configMap": {
                "name": "kube-root-ca.crt",
                "items": [{"key": "ca.crt", "path": "ca.crt"}],
            }
        },
        {
            "downwardAPI": {
                "items": [
                    {
                        "path": "namespace",
                        "fieldRef": {
                            "apiVersion": "v1",
                            "fieldPath": "metadata.namespace",
                        },
                    }
                ]
            }
        },
    ]
    if openshift:
        sources.append(
            {
                "configMap": {
                    "name": "openshift-service-ca.crt",
                    "items": [{"key": "service-ca.crt", "path": "service-ca.crt"}],
                }
            }
        )
    return {
        "name": name,
        "projected": {"defaultMode": 420, "sources": sources},
    }


@pytest.mark.parametrize("openshift", [False, True])
def test_bound_service_account_volume_accepts_k8s_and_openshift(openshift):
    assert is_bound_service_account_volume(_sa_volume(openshift=openshift))


def test_bound_service_account_volume_accepts_float_default_mode_and_expiration():
    volume = _sa_volume(openshift=True, expiration=3607.0)
    volume["projected"]["defaultMode"] = 420.0
    assert is_bound_service_account_volume(volume)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v.update(name="other-projection"),
        lambda v: v["projected"]["sources"].append({"secret": {"name": "x"}}),
        lambda v: v["projected"]["sources"][1]["configMap"].update(name="other"),
        lambda v: v["projected"].update(defaultMode=400),
    ],
)
def test_bound_service_account_volume_rejects_unexpected_shapes(mutate):
    volume = _sa_volume(openshift=True)
    mutate(volume)
    assert not is_bound_service_account_volume(volume)


def test_canonical_equal_equates_whole_number_floats():
    assert canonical_equal({"cpu": 2.0, "ram": 28.5}, {"cpu": 2, "ram": 28.5})
    assert not canonical_equal({"ram": 28.5}, {"ram": 28})


def test_structural_contains_allows_api_default_extras_and_number_noise():
    expected = {"containers": [{"name": "ops", "resources": {"cpu": 1.0}}]}
    observed = {
        "containers": [
            {
                "name": "ops",
                "resources": {"cpu": 1, "memory": "1Gi"},
                "imagePullPolicy": "IfNotPresent",
            }
        ],
        "dnsPolicy": "ClusterFirst",
    }
    assert structural_contains(expected, observed)
    assert not structural_contains(observed, expected)


def test_service_account_mount_shape():
    assert service_account_mount("kube-api-access-nwth7") == {
        "name": "kube-api-access-nwth7",
        "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
        "readOnly": True,
    }


def test_openshift_sa_admission_on_ops_scaffold():
    from app.schemas.quota import AccountingUnavailable
    from app.services import native_origin_ops as ops
    from app.services.ocp.ops_pod_scaffold import build_ops_pod_kubevirt_manifests

    requested, _ = build_ops_pod_kubevirt_manifests(
        namespace="project",
        project_id="12345678-project",
        command=["bash", "-c", "true"],
        env={"EXISTING": "unchanged"},
        config_files={"/workdir/install.yaml": "config"},
        cluster_nads=["lab-network"],
        bmc_nad=None,
    )
    observed = deepcopy(requested)
    volume = _sa_volume(openshift=True, name="kube-api-access-abcde")
    observed["spec"]["volumes"].append(volume)
    observed["spec"]["containers"][0]["volumeMounts"].append(
        service_account_mount(volume["name"])
    )
    ops._admitted(requested, observed)

    bad = deepcopy(observed)
    bad["spec"]["volumes"][-1]["projected"]["sources"][-1]["configMap"][
        "name"
    ] = "other-ca"
    with pytest.raises(AccountingUnavailable, match="service-account projection"):
        ops._admitted(requested, bad)
