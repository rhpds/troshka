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
