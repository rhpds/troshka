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


def build_export_job(name, namespace, temp_pvc_name, s3_path, s3_config):
    export_cmd = (
        "set -e; "
        "echo 'Converting raw to qcow2...'; "
        "qemu-img convert -f raw -O qcow2 -p /disk/disk.img /scratch/disk.qcow2; "
        "SIZE=$(stat -c%s /scratch/disk.qcow2); "
        "echo \"DISK_SIZE_BYTES=$SIZE\"; "
        "echo 'Uploading to S3...'; "
        f"aws s3 cp /scratch/disk.qcow2 s3://{s3_config.get('bucket', '')}/{s3_path} "
        f"--endpoint-url {s3_config.get('endpoint', 'https://s3.amazonaws.com')} "
        f"--region {s3_config.get('region', 'us-east-1')}; "
        "echo 'Done'"
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
            "backoffLimit": 2,
            "activeDeadlineSeconds": 3600,
            "template": {
                "spec": {
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
                                "readOnly": True,
                            },
                        },
                        {
                            "name": "scratch",
                            "emptyDir": {"sizeLimit": "500Gi"},
                        },
                    ],
                    "restartPolicy": "Never",
                },
            },
        },
    }
