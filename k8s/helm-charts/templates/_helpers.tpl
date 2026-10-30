{{/*
Prepend global.registry (default ghcr.io) to a host-less repository. A
repository whose first segment has "." or ":" already has a host and is kept.
Reads only .Values.global, the one scope shared with the Vector subchart.
Args: ctx, repository.
*/}}
{{- define "cwa.repository" -}}
{{- $first := splitList "/" .repository | first -}}
{{- if or (contains "." $first) (contains ":" $first) -}}
  {{- .repository -}}
{{- else -}}
  {{- $registry := (.ctx.Values.global | default dict).registry | default "" | trimSuffix "/" -}}
  {{- /* "ghcr" and "ccr" are the values before global.registry took a URL. */ -}}
  {{- if eq $registry "ccr" -}}
    {{- fail "global.registry no longer accepts \"ccr\"; set it to the registry URL" -}}
  {{- end -}}
  {{- if or (not $registry) (eq $registry "ghcr") -}}
    {{- $registry = "ghcr.io" -}}
  {{- end -}}
  {{- printf "%s/%s" $registry .repository -}}
{{- end -}}
{{- end -}}

{{/* Build a full image reference. Args: ctx, repository, tag. */}}
{{- define "cwa.image" -}}
{{- $repo := include "cwa.repository" (dict "ctx" .ctx "repository" .repository) -}}
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
lets global.registry cover Vector's image too.

Digest/tag composition is copied from vector-0.52.0 _helpers.tpl, so behaviour
is unchanged when global.registry is unset; only $repo differs.
RE-CHECK AGAINST UPSTREAM WHEN BUMPING THE VECTOR SUBCHART.
*/}}
{{- define "vector.image" -}}
{{- $repo := include "cwa.repository" (dict "ctx" . "repository" (.Values.image.repository | default "timberio/vector")) -}}
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
