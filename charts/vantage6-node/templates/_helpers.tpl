{{/* Name of the release, used as prefix for all objects */}}
{{- define "node.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "node.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "node.labels" -}}
{{ include "node.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* Labels of the node pod, also used to co-locate algorithm pods */}}
{{- define "node.podSelectorLabels" -}}
{{ include "node.selectorLabels" . }}
app.kubernetes.io/component: node
{{- end -}}

{{/* Selector of the algorithm pods of this node (labels set by the node) */}}
{{- define "node.algorithmSelectorLabels" -}}
app.kubernetes.io/managed-by: vantage6-node
app.kubernetes.io/component: algorithm
{{- end -}}

{{/* "true" if running on OpenShift */}}
{{- define "node.openshift" -}}
{{- $enabled := .Values.openshift.enabled -}}
{{- if eq (toString $enabled) "auto" -}}
{{- .Capabilities.APIVersions.Has "security.openshift.io/v1" -}}
{{- else -}}
{{- $enabled -}}
{{- end -}}
{{- end -}}

{{- define "node.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- if ne (include "node.openshift" .) "true" }}
{{- with .Values.podSecurityContext }}
{{ toYaml . }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "node.taskPvcName" -}}
{{- default (printf "%s-tasks" (include "node.fullname" .)) .Values.taskVolume.existingClaim -}}
{{- end -}}

{{- define "node.algorithmServiceAccountName" -}}
{{- if .Values.algorithms.serviceAccount.create -}}
{{- default (printf "%s-algorithm" (include "node.fullname" .)) .Values.algorithms.serviceAccount.name -}}
{{- else -}}
{{- .Values.algorithms.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "node.registrySecretName" -}}
{{- printf "%s-registries" (include "node.fullname" .) -}}
{{- end -}}

{{- define "node.privateKeySecretName" -}}
{{- default (printf "%s-private-key" (include "node.fullname" .)) .Values.node.encryption.existingSecret.name -}}
{{- end -}}

{{- define "node.configSecretName" -}}
{{- default (printf "%s-config" (include "node.fullname" .)) .Values.node.existingConfigSecret -}}
{{- end -}}

{{- define "node.proxyServiceName" -}}
{{- printf "%s-proxy" (include "node.fullname" .) -}}
{{- end -}}

{{/*
Databases on persistent volume claims, which are mounted in the node pod. A claim
can only be used by one volume of a pod, so databases on the same claim share a
volume.
*/}}
{{- define "node.databaseVolumes" -}}
{{- $names := dict -}}
{{- range .Values.node.databases }}
{{- if and .volume .volume.claim_name }}
{{- if not (hasKey $names .volume.claim_name) }}
{{- $_ := set $names .volume.claim_name (printf "database-%d" (len $names)) }}
{{- end }}
- label: {{ .label | quote }}
  claim: {{ .volume.claim_name | quote }}
  volume: {{ get $names .volume.claim_name | quote }}
  subPath: {{ .volume.sub_path | default "" | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/* The node configuration file */}}
{{- define "node.config" -}}
{{- $openshift := eq (include "node.openshift" .) "true" -}}
{{- $pullSecrets := list -}}
{{- if .Values.node.registries -}}
{{- $pullSecrets = append $pullSecrets (include "node.registrySecretName" .) -}}
{{- end -}}
{{- $pullSecrets = concat $pullSecrets .Values.node.existingImagePullSecrets -}}
{{- $securityContext := dict -}}
{{- if not $openshift -}}
{{- $securityContext = .Values.podSecurityContext -}}
{{- end -}}
{{- $encryption := dict "enabled" .Values.node.encryption.enabled -}}
{{- if .Values.node.encryption.enabled -}}
{{- $_ := set $encryption "private_key" "/mnt/config/private_key.pem" -}}
{{- end -}}
{{- $config := dict
  "api_key" .Values.node.apiKey
  "server_url" .Values.node.serverUrl
  "port" .Values.node.port
  "api_path" .Values.node.apiPath
  "task_dir" "/mnt/data"
  "databases" .Values.node.databases
  "policies" .Values.node.policies
  "algorithm_env" .Values.node.algorithmEnv
  "share_algorithm_logs" .Values.node.shareAlgorithmLogs
  "share_config" .Values.node.shareConfig
  "run_context_file" .Values.node.runContextFile
  "docker_registries" .Values.node.registries
  "encryption" $encryption
  "logging" (dict
    "level" .Values.node.logging.level
    "use_console" true
    "backup_count" 5
    "max_size" 1024
    "format" "%(asctime)s - %(name)-14s - %(levelname)-8s - %(message)s"
    "datefmt" "%Y-%m-%d %H:%M:%S"
    "loggers" (list
      (dict "name" "urllib3" "level" "warning")
      (dict "name" "socketIO-client" "level" "warning")
      (dict "name" "engineio.client" "level" "warning")
      (dict "name" "kubernetes.client.rest" "level" "warning")))
  "runtime" "kubernetes"
  "kubernetes" (dict
    "namespace" .Release.Namespace
    "task_pvc" (include "node.taskPvcName" .)
    "task_volume_mount_path" "/mnt/data"
    "algorithm_service_account" (include "node.algorithmServiceAccountName" .)
    "image_pull_secrets" $pullSecrets
    "node_pod_labels" (include "node.podSelectorLabels" . | fromYaml)
    "job" (dict
      "ttl_seconds_after_finished" .Values.algorithms.job.ttlSecondsAfterFinished
      "active_deadline_seconds" .Values.algorithms.job.activeDeadlineSeconds
      "image_pull_timeout_seconds" .Values.algorithms.job.imagePullTimeoutSeconds
      "pending_timeout_seconds" .Values.algorithms.job.pendingTimeoutSeconds
      "keep_on_shutdown" .Values.algorithms.job.keepOnShutdown)
    "algorithm_pod" (dict
      "resources" .Values.algorithms.resources
      "node_selector" .Values.algorithms.nodeSelector
      "tolerations" .Values.algorithms.tolerations
      "affinity" .Values.algorithms.affinity
      "priority_class_name" (.Values.algorithms.priorityClassName | default nil)
      "security_context" $securityContext
      "read_only_root_filesystem" .Values.algorithms.readOnlyRootFilesystem
      "gpu_resource_name" .Values.algorithms.gpuResourceName
      "co_locate_with_node" (ne .Values.taskVolume.accessMode "ReadWriteMany")))
-}}
{{- toYaml (mergeOverwrite $config .Values.node.extraConfig) -}}
{{- end -}}
