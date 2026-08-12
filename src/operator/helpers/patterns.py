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

    raw_size_bytes = size_gb * 1073741824

    export_cmd = (
        "set -e; "
        "export HOME=/scratch; "
        'echo \'{"phase":"converting","percent":0}\' > /scratch/.progress; '
        # Background progress monitor — polls output file size every 5s
        "( while [ ! -f /scratch/.convert_done ]; do "
        "    if [ -f /scratch/disk.qcow2 ]; then "
        "      cur=$(stat -c%s /scratch/disk.qcow2 2>/dev/null || echo 0); "
        f"      pct=$((cur * 100 / {raw_size_bytes})); "
        "      [ $pct -gt 99 ] && pct=99; "
        '      echo "{\\"phase\\":\\"converting\\",\\"percent\\":$pct}" > /scratch/.progress; '
        "    fi; "
        "    sleep 5; "
        "  done ) & "
        "qemu-img convert -f raw -O qcow2 /disk/disk.img /scratch/disk.qcow2; "
        "touch /scratch/.convert_done; "
        "SIZE=$(stat -c%s /scratch/disk.qcow2); "
        'echo \'{"phase":"uploading","size":\'$SIZE\',"uploaded":0}\' > /scratch/.progress; '
        # rclone upload with progress via log file polling
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
        # Background upload progress monitor
        "( while [ ! -f /scratch/.upload_done ]; do "
        "  if [ -f /scratch/.rclone.log ]; then "
        "    last=$(grep -oP 'Transferred:.*?\\K[\\d.]+ [GgMm]iB' /scratch/.rclone.log | tail -1); "
        '    if [ -n "$last" ]; then '
        "      num=$(echo \"$last\" | grep -oP '[\\d.]+'); "
        "      unit=$(echo \"$last\" | grep -oP '[GM]'); "
        '      case "$unit" in '
        '        G) ub=$(echo "$num * 1073741824" | bc | cut -d. -f1);; '
        '        M) ub=$(echo "$num * 1048576" | bc | cut -d. -f1);; '
        "        *) ub=0;; "
        "      esac; "
        '      echo "{\\"phase\\":\\"uploading\\",\\"size\\":$SIZE,\\"uploaded\\":$ub}" > /scratch/.progress; '
        "    fi; "
        "  fi; "
        "  sleep 5; "
        "done ) & "
        f"rclone copyto /scratch/disk.qcow2 target:{s3_bucket}/{s3_path} "
        f"--s3-chunk-size {_EXPORT_MULTIPART_CHUNK} "
        f"--s3-upload-concurrency {_EXPORT_CONCURRENT_REQUESTS} "
        "--stats 5s --stats-one-line --log-file /scratch/.rclone.log --log-level INFO; "
        "touch /scratch/.upload_done; "
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
