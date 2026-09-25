{{/*
Chart name.
*/}}
{{- define "troshka.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name.
*/}}
{{- define "troshka.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Namespace name.
*/}}
{{- define "troshka.namespace" -}}
{{- .Values.namespace.name | default "troshka" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "troshka.labels" -}}
app.kubernetes.io/name: {{ include "troshka.name" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{/*
Backend image with tag.
*/}}
{{- define "troshka.backendImage" -}}
{{ .Values.backend.image.repository }}:{{ .Values.backend.image.tag | default .Chart.AppVersion }}
{{- end }}

{{/*
Frontend image with tag.
*/}}
{{- define "troshka.frontendImage" -}}
{{ .Values.frontend.image.repository }}:{{ .Values.frontend.image.tag | default .Chart.AppVersion }}
{{- end }}

{{/*
Soft scheduling preferences for control-plane pods (backend/frontend/postgres/redis).

- spreadControlPlane: prefer not sharing a node with another control-plane component
  (limits blast radius when one worker node flaps).
- avoidNodes: prefer not scheduling on listed hostnames (known-flaky nodes).

Workers are intentionally excluded — they should use every Ready node.
*/}}
{{- define "troshka.controlPlaneAffinity" -}}
{{- $spread := .Values.affinity.spreadControlPlane | default false -}}
{{- $avoid := .Values.affinity.avoidNodes | default list -}}
{{- if or $spread (gt (len $avoid) 0) }}
affinity:
  {{- if gt (len $avoid) 0 }}
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        preference:
          matchExpressions:
            - key: kubernetes.io/hostname
              operator: NotIn
              values:
                {{- range $avoid }}
                - {{ . | quote }}
                {{- end }}
  {{- end }}
  {{- if $spread }}
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          labelSelector:
            matchExpressions:
              - key: app.kubernetes.io/component
                operator: In
                values:
                  - backend
                  - frontend
                  - database
                  - redis
          topologyKey: kubernetes.io/hostname
  {{- end }}
{{- end }}
{{- end }}
