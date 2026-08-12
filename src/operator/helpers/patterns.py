from helpers.k8s import TOOLS_IMAGE

SNAPSHOT_CLASS = "ocs-storagecluster-rbdplugin-snapclass"
_EXPORT_MULTIPART_CHUNK = "64MB"
_EXPORT_CONCURRENT_REQUESTS = "20"


def build_volume_snapshot(name, namespace, pvc_name):
    return {
        "apiVersion": "snapshot.storage.k8s.io/v1",
        "kind": "VolumeSnapshot",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "volumeSnapshotClassName": SNAPSHOT_CLASS,
            "source": {"persistentVolumeClaimName": pvc_name},
        },
    }


def build_temp_pvc_from_snapshot(name, namespace, snapshot_name, size_gb):
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": f"{size_gb}Gi"}},
            "dataSource": {
                "name": snapshot_name,
                "kind": "VolumeSnapshot",
                "apiGroup": "snapshot.storage.k8s.io",
            },
        },
    }


def build_scratch_pvc(name, namespace, size_gb):
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": f"{size_gb}Gi"}},
            "storageClassName": "ocs-storagecluster-ceph-rbd",
        },
    }


def build_export_job(name, namespace, temp_pvc_name, s3_path, s3_config, size_gb):
    deadline = max(7200, size_gb * 90)
    scratch_pvc_name = f"scratch-{name}"

    export_cmd = (
        "set -e; "
        "export AWS_CONFIG_FILE=/tmp/.aws-config; "
        f"aws configure set default.s3.multipart_chunksize {_EXPORT_MULTIPART_CHUNK}; "
        f"aws configure set default.s3.max_concurrent_requests {_EXPORT_CONCURRENT_REQUESTS}; "
        # Progress file on scratch PVC — operator reads via exec
        "echo '{\"phase\":\"converting\",\"percent\":0}' > /scratch/.progress; "
        "qemu-img convert -f raw -O qcow2 -p /disk/disk.img /scratch/disk.qcow2 2>&1 | "
        "  while IFS= read -r line; do "
        "    pct=$(echo \"$line\" | grep -oP '\\d+\\.\\d+(?=/100%)' || true); "
        "    [ -n \"$pct\" ] && echo \"{\\\"phase\\\":\\\"converting\\\",\\\"percent\\\":${pct%%.*}}\" > /scratch/.progress; "
        "  done; "
        "SIZE=$(stat -c%s /scratch/disk.qcow2); "
        'echo "DISK_SIZE_BYTES=$SIZE"; '
        "echo '{\"phase\":\"uploading\",\"size\":'$SIZE'}' > /scratch/.progress; "
        f"aws s3 cp /scratch/disk.qcow2 s3://{s3_config.get('bucket', '')}/{s3_path} "
        f"--endpoint-url {s3_config.get('endpoint', 'https://s3.amazonaws.com')} "
        f"--region {s3_config.get('region', 'us-east-1')}; "
        "echo '{\"phase\":\"done\"}' > /scratch/.progress"
    )

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"export-{name}",
            "namespace": namespace,
            "labels": {"troshka-role": "pattern-export"},
        },
        "spec": {
            "backoffLimit": 6,
            "activeDeadlineSeconds": deadline,
            "template": {
                "spec": {
                    "serviceAccountName": "troshka-export",
                    "securityContext": {
                        "runAsUser": 107,
                        "runAsGroup": 107,
                        "fsGroup": 107,
                    },
                    "containers": [
                        {
                            "name": "export",
                            "image": TOOLS_IMAGE,
                            "command": ["sh", "-c", export_cmd],
                            "volumeMounts": [
                                {"name": "disk", "mountPath": "/disk"},
                                {"name": "scratch", "mountPath": "/scratch"},
                            ],
                            "envFrom": [
                                {
                                    "secretRef": {
                                        "name": s3_config.get(
                                            "credentialsSecret",
                                            "s3-credentials",
                                        )
                                    }
                                }
                            ],
                            "resources": {
                                "requests": {"cpu": "1", "memory": "1Gi"},
                                "limits": {"cpu": "4", "memory": "4Gi"},
                            },
                        }
                    ],
                    "volumes": [
                        {
                            "name": "disk",
                            "persistentVolumeClaim": {
                                "claimName": temp_pvc_name,
                            },
                        },
                        {
                            "name": "scratch",
                            "persistentVolumeClaim": {
                                "claimName": scratch_pvc_name,
                            },
                        },
                    ],
                    "restartPolicy": "Never",
                },
            },
        },
        "_deadline": deadline,
        "_scratchPvcName": scratch_pvc_name,
    }
