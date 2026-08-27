{{/*
Image references are stored host-less (e.g. "crusoecloud/crusoe-watch-agent/
log-collector"); the registry host is prepended here from global.registry:
"ghcr" (default) uses ghcr.io / docker.io, "ccr" uses Crusoe Container
Registry's per-region pull-through caches. One cache project maps to one
upstream, hence separate ghcr/dockerhub projects per region.

These read only .Values.global -- the only scope Helm shares with subcharts,
since cwa.image is also called from the Vector subchart's tpl context.
*/}}

{{/*
Region for CCR endpoints. Uses global.region when set, otherwise reads the
standard topology.kubernetes.io/region label off any node (set by the cloud
controller manager).

The lookup only reaches a live cluster during install/upgrade -- `helm template`
and `--dry-run` render with no API access and return nothing, so GitOps-style
rendering must set global.region explicitly.
*/}}
{{- define "cwa.region" -}}
{{- $region := (.Values.global | default dict).region | default "" -}}
{{- if not $region -}}
  {{- $nodes := (lookup "v1" "Node" "" "") -}}
  {{- range ($nodes.items | default list) -}}
    {{- if not $region -}}
      {{- $region = index (.metadata.labels | default dict) "topology.kubernetes.io/region" | default "" -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $region -}}
{{- end -}}

{{/*
CCR prefix for an upstream, or "" when this region has no cache project (or the
region could not be determined). Callers fall back to the upstream registry.
Args: ctx, upstream.
*/}}
{{- define "cwa.ccrPrefix" -}}
{{- $g := .ctx.Values.global | default dict -}}
{{- $ccr := $g.ccr | default dict -}}
{{- $region := include "cwa.region" .ctx -}}
{{- if $region -}}
  {{- $regional := get ($ccr.projects | default dict) $region -}}
  {{- if $regional -}}
    {{- $project := get $regional .upstream -}}
    {{- if $project -}}
      {{- printf "%s/%s" (printf ($ccr.endpointFormat | default "registry.%s.ccr.crusoecloudcompute.com") $region) $project -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- end -}}

{{/* Resolve the registry host for an upstream. Args: ctx, upstream. */}}
{{- define "cwa.registry" -}}
{{- $g := .ctx.Values.global | default dict -}}
{{- $choice := $g.registry | default "ghcr" -}}
{{- $upstream := .upstream | default "ghcr" -}}
{{- if eq $choice "ghcr" -}}
  {{- if eq $upstream "dockerhub" -}}docker.io{{- else -}}ghcr.io{{- end -}}
{{- else if eq $choice "ccr" -}}
  {{- $prefix := include "cwa.ccrPrefix" (dict "ctx" .ctx "upstream" $upstream) -}}
  {{- if $prefix -}}
    {{- $prefix -}}
  {{- else if eq $upstream "dockerhub" -}}docker.io{{- else -}}ghcr.io{{- end -}}
{{- else -}}
  {{- fail (printf "unsupported global.registry %q -- valid values are \"ghcr\" and \"ccr\"" $choice) -}}
{{- end -}}
{{- end -}}

{{/*
Prepend the registry to a host-less repository. A repository that already
carries a host is passed through, so explicit overrides still work -- standard
OCI heuristic: first segment is a host only if it contains "." or ":".
Args: ctx, upstream, repository.
*/}}
{{- define "cwa.repository" -}}
{{- $first := splitList "/" .repository | first -}}
{{- if or (contains "." $first) (contains ":" $first) -}}
  {{- .repository -}}
{{- else -}}
  {{- printf "%s/%s" (include "cwa.registry" (dict "ctx" .ctx "upstream" .upstream)) .repository -}}
{{- end -}}
{{- end -}}

{{/* Build a full image reference. Args: ctx, upstream, repository, tag. */}}
{{- define "cwa.image" -}}
{{- $repo := include "cwa.repository" (dict "ctx" .ctx "upstream" (.upstream | default "ghcr") "repository" .repository) -}}
{{- if .tag -}}
  {{- printf "%s:%s" $repo (.tag | toString) -}}
{{- else -}}
  {{- $repo -}}
{{- end -}}
{{- end -}}

{{/*
OVERRIDE of the Vector subchart's image helper -- named templates share one
global namespace and the parent's definition wins.

Vector builds its image from .Values.image.repository verbatim with no registry
hook, and a parent chart cannot template a subchart's values. Overriding is what
lets a single --set global.registry=ccr cover Vector's image too.

Digest/tag composition is copied from vector-0.52.0 _helpers.tpl, so behaviour
is unchanged when global.registry is unset; only $repo differs.
RE-CHECK AGAINST UPSTREAM WHEN BUMPING THE VECTOR SUBCHART.
*/}}
{{- define "vector.image" -}}
{{- $repo := include "cwa.repository" (dict "ctx" . "upstream" "ghcr" "repository" (.Values.image.repository | default "timberio/vector")) -}}
{{- $tagRaw := include "vector.image.tag" . | default "" -}}
{{- $shaRaw := (coalesce .Values.image.sha .Values.image.digest) | default "" -}}
{{- $tag := trim $tagRaw -}}
{{- $sha := "" -}}
{{- if $shaRaw -}}
  {{- if hasPrefix "sha256:" $shaRaw -}}
    {{- $sha = $shaRaw -}}
  {{- else -}}
    {{- $sha = printf "sha256:%s" $shaRaw -}}
  {{- end -}}
{{- end -}}
{{- if $sha -}}
  {{- if $tag -}}
    {{- printf "%s:%s@%s" $repo $tag $sha -}}
  {{- else -}}
    {{- printf "%s@%s" $repo $sha -}}
  {{- end -}}
{{- else if hasPrefix "sha256:" $tag -}}
  {{- printf "%s@%s" $repo $tag -}}
{{- else if contains "@sha256:" $tag -}}
  {{- $parts := splitList "@" $tag -}}
  {{- printf "%s:%s@%s" $repo (index $parts 0) (index $parts 1) -}}
{{- else if $tag -}}
  {{- printf "%s:%s" $repo $tag -}}
{{- else -}}
  {{- printf "%s:%s" $repo .Chart.AppVersion -}}
{{- end -}}
{{- end -}}
