{{- define "hermes-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "hermes-agent.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "hermes-agent.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "hermes-agent.tenantLabelKey" -}}
tenant.hermes.ai/id
{{- end -}}

{{- define "hermes-agent.tenantLabels" -}}
{{- if .Values.tenant.id }}
{{ include "hermes-agent.tenantLabelKey" . }}: {{ .Values.tenant.id | quote }}
{{- end }}
{{- with .Values.tenant.labels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "hermes-agent.labels" -}}
helm.sh/chart: {{ include "hermes-agent.chart" . }}
{{ include "hermes-agent.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: hermes-agent
{{- $tenantLabels := include "hermes-agent.tenantLabels" . }}
{{- if $tenantLabels }}
{{ $tenantLabels }}
{{- end }}
{{- end -}}

{{- define "hermes-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hermes-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "hermes-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "hermes-agent.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "hermes-agent.configMapName" -}}
{{- printf "%s-config" (include "hermes-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "hermes-agent.bootstrapConfigMapName" -}}
{{- default (include "hermes-agent.configMapName" .) .Values.bootstrap.existingConfigMap -}}
{{- end -}}

{{- define "hermes-agent.generatedSecretName" -}}
{{- printf "%s-secrets" (include "hermes-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "hermes-agent.secretName" -}}
{{- if .Values.externalSecret.enabled -}}
{{- default (include "hermes-agent.generatedSecretName" .) .Values.externalSecret.target.name -}}
{{- else if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- include "hermes-agent.generatedSecretName" . -}}
{{- end -}}
{{- end -}}

{{- define "hermes-agent.onboardingConfigMapName" -}}
{{- printf "%s-onboarding" (include "hermes-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "hermes-agent.onboardingMountPath" -}}
/opt/hermes-chart/onboarding
{{- end -}}

{{/*
Normalized onboarding requirements document, consumed by check.py.
Platform names are lowercased, de-duplicated and sorted so the requirements
hash (and therefore the completion latch) is order-insensitive.
*/}}
{{- define "hermes-agent.onboardingRequirements" -}}
{{- $platforms := list -}}
{{- range .Values.onboarding.requirements.platforms -}}
  {{- $name := . | toString | trim | lower -}}
  {{- if not (has $name $platforms) -}}
    {{- $platforms = append $platforms $name -}}
  {{- end -}}
{{- end -}}
{{- dict "schemaVersion" 1 "validatorVersion" 1 "provider" .Values.onboarding.requirements.provider "platforms" (sortAlpha $platforms) | toJson -}}
{{- end -}}

{{/*
Container environment shared by the gateway container and the onboarding
gate. Rendered at column 0; callers apply their own `nindent`. Keeping this
in one place prevents the gate from resolving a different credential set
than the gateway, which would make the validator report a false negative.
*/}}
{{- define "hermes-agent.containerEnv" -}}
{{- $mountPath := .Values.persistence.mountPath -}}
{{- $npmEnabled := gt (len .Values.npmPackages) 0 -}}
- name: S6_YES_I_WANT_A_WORLD_WRITABLE_RUN_BECAUSE_KUBERNETES
  value: "1"
- name: HERMES_HOME
  value: {{ $mountPath | quote }}
- name: HOME
  value: {{ printf "%s/home" $mountPath | quote }}
{{- if $npmEnabled }}
- name: PATH
  value: {{ printf "%s/npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" $mountPath | quote }}
- name: NODE_PATH
  value: {{ printf "%s/npm-global/lib/node_modules" $mountPath | quote }}
- name: NPM_CONFIG_PREFIX
  value: {{ printf "%s/npm-global" $mountPath | quote }}
{{- end }}
{{- range $key, $value := .Values.env }}
- name: {{ $key }}
  value: {{ printf "%v" $value | quote }}
{{- end }}
{{- if .Values.apiServer.enabled }}
- name: API_SERVER_ENABLED
  value: "true"
- name: API_SERVER_HOST
  value: {{ .Values.apiServer.host | quote }}
- name: API_SERVER_PORT
  value: {{ .Values.apiServer.port | quote }}
- name: API_SERVER_CORS_ORIGINS
  value: {{ .Values.apiServer.corsOrigins | quote }}
- name: API_SERVER_MODEL_NAME
  value: {{ .Values.apiServer.modelName | quote }}
{{- end }}
{{- if .Values.webhook.enabled }}
- name: WEBHOOK_ENABLED
  value: "true"
- name: WEBHOOK_PORT
  value: {{ .Values.webhook.port | quote }}
{{- end }}
{{- if .Values.telegramWebhook.enabled }}
- name: TELEGRAM_WEBHOOK_URL
  value: {{ .Values.telegramWebhook.url | quote }}
- name: TELEGRAM_WEBHOOK_PORT
  value: {{ .Values.telegramWebhook.port | quote }}
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
envFrom sources shared by the gateway container and the onboarding gate.
Renders nothing when no secret or extra source applies.
*/}}
{{- define "hermes-agent.containerEnvFrom" -}}
{{- $hasInlineSecretValues := false -}}
{{- range $key, $value := .Values.secrets }}
  {{- if and (ne $key "existingSecret") (ne $key "annotations") (ne (printf "%v" $value) "") -}}
    {{- $hasInlineSecretValues = true -}}
  {{- end -}}
{{- end -}}
{{- if or .Values.externalSecret.enabled .Values.secrets.existingSecret .Values.extraEnvFrom $hasInlineSecretValues }}
{{- if or .Values.externalSecret.enabled .Values.secrets.existingSecret }}
- secretRef:
    name: {{ include "hermes-agent.secretName" . }}
{{- else }}
- secretRef:
    name: {{ include "hermes-agent.secretName" . }}
    optional: true
{{- end }}
{{- with .Values.extraEnvFrom }}
{{ toYaml . }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "hermes-agent.pvcName" -}}
{{- printf "%s-data" (include "hermes-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "hermes-agent.servicePorts" -}}
{{- $ports := list -}}
{{- if gt (len .Values.service.ports) 0 -}}
  {{- $ports = .Values.service.ports -}}
{{- else -}}
  {{- if .Values.apiServer.enabled -}}
    {{- $ports = append $ports (dict "name" "api-server" "port" (.Values.apiServer.port | int) "targetPort" (.Values.apiServer.port | int) "containerPort" (.Values.apiServer.port | int) "protocol" "TCP") -}}
  {{- end -}}
  {{- if .Values.webhook.enabled -}}
    {{- $ports = append $ports (dict "name" "webhook" "port" (.Values.webhook.port | int) "targetPort" (.Values.webhook.port | int) "containerPort" (.Values.webhook.port | int) "protocol" "TCP") -}}
  {{- end -}}
  {{- if .Values.telegramWebhook.enabled -}}
    {{- $ports = append $ports (dict "name" "telegram-webhook" "port" (.Values.telegramWebhook.port | int) "targetPort" (.Values.telegramWebhook.port | int) "containerPort" (.Values.telegramWebhook.port | int) "protocol" "TCP") -}}
  {{- end -}}
{{- end -}}
{{- $ports | toJson -}}
{{- end -}}

{{- define "hermes-agent.primaryServicePortNumber" -}}
{{- $servicePorts := include "hermes-agent.servicePorts" . | fromJsonArray -}}
{{- if and .Values.service.enabled (gt (len $servicePorts) 0) -}}
{{- (index $servicePorts 0).port -}}
{{- else -}}
{{- fail "service.enabled=true with either explicit service.ports entries or enabled apiServer/webhook/telegramWebhook ports is required for ingress, httpRoute, or virtualService routing" -}}
{{- end -}}
{{- end -}}
