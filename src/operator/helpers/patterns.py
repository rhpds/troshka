from helpers.k8s import TOOLS_IMAGE

SNAPSHOT_CLASS = "ocs-storagecluster-rbdplugin-snapclass"


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
    deadline = max(3600, size_gb * 15)
    scratch_pvc_name = f"scratch-{name}"

    export_cmd = (
        "set -e; "
        "aws configure set default.s3.multipart_chunksize 64MB; "
        "aws configure set default.s3.max_concurrent_requests 20; "
        "echo 'PHASE=converting'; "
        "qemu-img convert -f raw -O qcow2 -p /disk/disk.img /scratch/disk.qcow2; "
        "SIZE=$(stat -c%s /scratch/disk.qcow2); "
        'echo "DISK_SIZE_BYTES=$SIZE"; '
        "echo 'PHASE=uploading'; "
        f"aws s3 cp /scratch/disk.qcow2 s3://{s3_config.get('bucket', '')}/{s3_path} "
        f"--endpoint-url {s3_config.get('endpoint', 'https://s3.amazonaws.com')} "
        f"--region {s3_config.get('region', 'us-east-1')}; "
        "echo 'PHASE=done'"
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
