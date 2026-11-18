{{- define "troshka.nativeWorker" -}}
{{- $root := .root }}
{{- $worker := .worker }}
{{- $profile := .profile }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: troshka-{{ $profile }}-worker
  namespace: {{ include "troshka.namespace" $root }}
  labels:
    {{- include "troshka.labels" $root | nindent 4 }}
    app.kubernetes.io/component: {{ $profile }}-worker
spec:
  replicas: {{ $worker.replicas }}
  selector:
    matchLabels:
      app.kubernetes.io/name: troshka-{{ $profile }}-worker
  template:
    metadata:
      labels:
        app.kubernetes.io/name: troshka-{{ $profile }}-worker
        app.kubernetes.io/component: {{ $profile }}-worker
      annotations:
        checksum/config: {{ include (print $root.Template.BasePath "/backend-config.yaml") $root | sha256sum }}
    spec:
      terminationGracePeriodSeconds: {{ add .timeout 60 }}
      automountServiceAccountToken: false
      # Projected Secret/ConfigMap files are symlinks owned by root. Copy them
      # as the pod UID into private regular files for the protected-file reader.
      initContainers:
        {{- include "troshka.nativeRuntime.initContainers" (dict "root" $root "worker" $worker) | nindent 8 }}
      containers:
        - name: worker
          image: {{ include "troshka.backendImage" $root }}
          imagePullPolicy: Always
          command: ["python3", "-m", "app.workers.deploy_worker"]
          env:
            {{- include "troshka.nativeRuntime.env" (dict "worker" $worker) | nindent 12 }}
            - name: TROSHKA_WORKER_PROFILE
              value: {{ $profile }}
            - name: TROSHKA_DATABASE__URL
              valueFrom:
                secretKeyRef:
                  name: troshka-secrets
                  key: database-url
            - name: TROSHKA_DATABASE__POOL_SIZE
              value: {{ $worker.dbPoolSize | quote }}
            - name: TROSHKA_AUTH__ENCRYPTION_KEY
              valueFrom:
                secretKeyRef:
                  name: troshka-secrets
                  key: encryption-key
            - name: TROSHKA_DATABASE__MAX_OVERFLOW
              value: {{ $worker.dbMaxOverflow | quote }}
            - name: TROSHKA_DATABASE__POOL_RECYCLE
              value: {{ $root.Values.database.poolRecycle | quote }}
          volumeMounts:
            {{- include "troshka.nativeRuntime.volumeMounts" (dict "worker" $worker) | nindent 12 }}
          resources:
            {{- toYaml $worker.resources | nindent 12 }}
      volumes:
        {{- include "troshka.nativeRuntime.volumes" (dict "worker" $worker) | nindent 8 }}
{{- end }}
