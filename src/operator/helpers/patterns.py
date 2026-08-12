from helpers.k8s import TOOLS_IMAGE

SNAPSHOT_CLASS = "ocs-storagecluster-rbdplugin-snapclass"
_EXPORT_MULTIPART_CHUNK = "256MB"
_EXPORT_CONCURRENT_REQUESTS = "7"


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

    s3_bucket = s3_config.get("bucket", "")
    s3_endpoint = s3_config.get("endpoint", "https://s3.amazonaws.com")

    export_cmd = (
        "set -e; "
        "export HOME=/scratch; "
        'echo \'{"phase":"converting","percent":0}\' > /scratch/.progress; '
        # qemu-img -p writes \r-delimited progress — tr splits into lines
        "qemu-img convert -f raw -O qcow2 -p /disk/disk.img /scratch/disk.qcow2 2>&1 | "
        "  tr '\\r' '\\n' | "
        "  while IFS= read -r line; do "
        "    pct=$(echo \"$line\" | grep -oP '\\d+\\.\\d+(?=/100%)' || true); "
        '    [ -n "$pct" ] && echo "{\\"phase\\":\\"converting\\",\\"percent\\":${pct%%.*}}" > /scratch/.progress; '
        "  done; "
        "SIZE=$(stat -c%s /scratch/disk.qcow2); "
        'echo \'{"phase":"uploading","size":\'$SIZE\',"uploaded":0}\' > /scratch/.progress; '
        # rclone uploads with --progress writing stats to stderr
        "export RCLONE_CONFIG=/scratch/rclone.conf; "
        "cat > $RCLONE_CONFIG <<REOF\n"
        "[target]\n"
        "type = s3\n"
        "provider = Ceph\n"
        "access_key_id = $AWS_ACCESS_KEY_ID\n"
        "secret_access_key = $AWS_SECRET_ACCESS_KEY\n"
        f"endpoint = {s3_endpoint}\n"
        "no_check_bucket = true\n"
        "no_verify_ssl = true\n"
        "REOF\n"
        f"rclone copyto /scratch/disk.qcow2 target:{s3_bucket}/{s3_path} "
        f"--s3-chunk-size {_EXPORT_MULTIPART_CHUNK} "
        f"--s3-upload-concurrency {_EXPORT_CONCURRENT_REQUESTS} "
        "--progress --stats 5s --stats-one-line 2>&1 | "
        "  tr '\\r' '\\n' | "
        "  while IFS= read -r line; do "
        "    bytes=$(echo \"$line\" | grep -oP 'Transferred:.*?\\K\\d+\\.\\d+ [GgMm]' | head -1 || true); "
        '    if [ -n "$bytes" ]; then '
        "      num=$(echo \"$bytes\" | grep -oP '[\\d.]+'); "
        "      unit=$(echo \"$bytes\" | grep -oP '[GgMm]'); "
        '      case "$unit" in '
        '        G|g) ub=$(echo "$num * 1073741824" | bc | cut -d. -f1);; '
        '        M|m) ub=$(echo "$num * 1048576" | bc | cut -d. -f1);; '
        "        *) ub=0;; "
        "      esac; "
        '      echo "{\\"phase\\":\\"uploading\\",\\"size\\":$SIZE,\\"uploaded\\":$ub}" > /scratch/.progress; '
        "    fi; "
        "  done; "
        'echo \'{"phase":"done"}\' > /scratch/.progress'
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
