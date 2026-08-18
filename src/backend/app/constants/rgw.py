"""Cluster-local ODF RGW endpoint constants."""

# NOSONAR — in-cluster service URL; HTTP on port 80 inside the mesh is expected
RGW_IN_CLUSTER_ENDPOINT = (
    "http://rook-ceph-rgw-ocs-storagecluster-cephobjectstore"
    ".openshift-storage.svc:80"
)
