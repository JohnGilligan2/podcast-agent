{{- define "pa.name" -}}{{ .Release.Name }}-podcast-agent{{- end -}}

{{- define "pa.labels" -}}
app.kubernetes.io/name: podcast-agent
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
{{- end -}}

{{- define "pa.image" -}}{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}{{- end -}}

{{/* Guard rails: refuse settings this release cannot honour, with a clear message. */}}
{{- define "pa.guards" -}}
{{- if .Values.mcp.enabled }}{{ fail "mcp.enabled: the MCP server ships in v1.0, see docs/ROADMAP.md" }}{{ end -}}
{{- if ne .Values.store.backend "sqlite" }}{{ fail "store.backend: only sqlite is available today (postgres in v1.0, see docs/ROADMAP.md)" }}{{ end -}}
{{- if or (gt (int .Values.gui.replicas) 1) .Values.gui.autoscaling.enabled (gt (int .Values.worker.replicas) 1) .Values.worker.autoscaling.enabled }}
{{- fail "more than one web or worker pod needs store.backend=postgres (v1.0): SQLite is one file on one volume" }}
{{- end -}}
{{- end -}}

{{- define "pa.env" -}}
- {name: PODCAST_AGENT_HOME, value: /app}
- {name: PODCAST_AGENT_DB, value: /app/var/podcast_agent.db}
- {name: PODCAST_AGENT_MODEL, value: {{ .Values.agent.model | quote }}}
- {name: PODCAST_AGENT_EFFORT, value: {{ .Values.agent.effort | quote }}}
- {name: PODCAST_AGENT_MAX_STEPS, value: {{ .Values.agent.maxSteps | quote }}}
- {name: PODCAST_AGENT_WEB_SEARCH, value: {{ .Values.agent.webSearch | quote }}}
- {name: PODCAST_AGENT_WEB_SEARCH_MAX_USES, value: {{ .Values.agent.webSearchMaxUses | quote }}}
- {name: PODCAST_AGENT_CONCURRENCY, value: {{ .Values.worker.concurrency | quote }}}
- {name: PODCAST_AGENT_OTEL, value: {{ .Values.otel.enabled | quote }}}
- {name: OTEL_EXPORTER_OTLP_ENDPOINT, value: {{ .Values.otel.endpoint | quote }}}
{{- end -}}

{{- define "pa.security" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: {drop: [ALL]}
{{- end -}}

{{- define "pa.volumes" -}}
- name: results
  persistentVolumeClaim: {claimName: {{ include "pa.name" . }}-results}
- name: tmp
  emptyDir: {}
{{- if .Values.clients.existingConfigMap }}
- name: clients
  configMap: {name: {{ .Values.clients.existingConfigMap }}}
{{- else if .Values.clients.seed }}
- name: clients
  configMap: {name: {{ include "pa.name" . }}-seed}
{{- end }}
{{- end -}}

{{- define "pa.mounts" -}}
- {name: results, mountPath: /app/var}
- {name: tmp, mountPath: /tmp}
{{- if or .Values.clients.existingConfigMap .Values.clients.seed }}
- {name: clients, mountPath: /app/data/clients, readOnly: true}
{{- end }}
{{- end -}}
