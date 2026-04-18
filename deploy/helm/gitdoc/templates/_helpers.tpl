{{/* Base name used by every resource in this release. */}}
{{- define "gitdoc.fullname" -}}
{{- if .Values.nameOverride -}}
{{- .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "gitdoc-%s" .Values.repo.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "gitdoc.labels" -}}
app.kubernetes.io/name: gitdoc
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
gitdoc.repo: {{ .Values.repo.name | quote }}
{{- end -}}

{{- define "gitdoc.selector.bot" -}}
app.kubernetes.io/name: gitdoc
app.kubernetes.io/component: discord-bot
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "gitdoc.selector.rag" -}}
app.kubernetes.io/name: gitdoc
app.kubernetes.io/component: rag
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Name of the Secret workloads envFrom. Resolves to `secrets.existingSecret`
when the operator has provided an out-of-band Secret (sealed-secrets /
external-secrets), otherwise falls back to the chart-managed Secret name.
Single source of truth — every envFrom / secretKeyRef references this.
*/}}
{{- define "gitdoc.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "gitdoc.fullname" .) -}}
{{- end -}}
{{- end -}}
