#!/usr/bin/env bash
set -euo pipefail

# generate-release-notes.sh
#
# Generates customer-facing release notes from commit messages using Claude CLI.
# Component configuration lives in ci/scripts/release-notes-<component>.conf.
#
# Usage:
#   ./ci/scripts/generate-release-notes.sh <component> [--dry-run] [--force]                        # Add missing versions to changelog
#   ./ci/scripts/generate-release-notes.sh <component> <start_ver> <end_ver> [--dry-run] [--force]  # Generate for specific range
#   ./ci/scripts/generate-release-notes.sh --list                                                   # List configured components
#
# Requirements:
#   - claude CLI (claude.ai/code) must be installed and authenticated
#   - ci/scripts/release-notes-<component>.conf must exist for each component
#
# Output:
#   Prepends entries to component CHANGELOG.md
#   With --dry-run: prints to stdout only, no file changes
#
# NOTE: Generated notes are AI-drafted. Always review before publishing.

die() { echo "ERROR: $*" >&2; exit 1; }

REPO_ROOT="$(git rev-parse --show-toplevel)"
SCRIPT_DIR="${REPO_ROOT}/ci/scripts"

# Ensure we have the latest main so version history is up to date
git fetch origin main --quiet 2>/dev/null || true

# --- Config loading ---

# Component config variables (set by load_component_config)
CHANGELOG_PATH=""
DESCRIPTION=""
VERSION_FILE=""
VERSION_FIELD=""
SOURCE_PATHS=""  # space-separated directories to scope commits to (e.g. "vm/ common/")
DIFF_EXCLUDE=""
COMMIT_SKIP=""
MR_SOURCE=""  # "gitlab", "github", or "" (disabled)
COMMIT_URL_BASE=""  # Override commit link base URL (e.g. for a GitHub mirror)

load_component_config() {
  local component="$1"
  local config_file="${SCRIPT_DIR}/release-notes-${component}.conf"
  [[ -f "$config_file" ]] || die "No config for component '${component}'. Expected: ${config_file}
Run with --list to see configured components."

  # Reset to defaults before sourcing
  CHANGELOG_PATH=""
  DESCRIPTION=""
  VERSION_FILE=""
  VERSION_FIELD=""
  SOURCE_PATHS=""
  DIFF_EXCLUDE=""
  COMMIT_SKIP=""
  MR_SOURCE=""
  COMMIT_URL_BASE=""

  # shellcheck source=/dev/null
  source "$config_file"

  # Validate required fields
  [[ -n "$CHANGELOG_PATH" ]] || die "CHANGELOG_PATH not set in ${config_file}"
  [[ -n "$DESCRIPTION" ]] || die "DESCRIPTION not set in ${config_file}"
  [[ -n "$VERSION_FILE" ]] || die "VERSION_FILE not set in ${config_file}"
}

list_components() {
  if [[ ! -d "$SCRIPT_DIR" ]]; then
    die "No config directory found at ${SCRIPT_DIR}"
  fi
  local found=false
  for f in "${SCRIPT_DIR}"/release-notes-*.conf; do
    [[ -f "$f" ]] || continue
    found=true
    local name
    name=$(basename "$f" .conf)
    name="${name#release-notes-}"
    # Try to read description from config
    local desc=""
    desc=$(grep "^DESCRIPTION=" "$f" 2>/dev/null | head -1 | sed 's/^DESCRIPTION="//' | sed 's/"$//' || true)
    if [[ -n "$desc" ]]; then
      printf "  %-12s %s\n" "$name" "$desc"
    else
      echo "  $name"
    fi
  done
  if [[ "$found" == "false" ]]; then
    die "No release-notes-*.conf files found in ${SCRIPT_DIR}"
  fi
}

# --- Version resolution ---

# Cached version list (computed once per run).
_VERSION_LIST_CACHE=""

# Get version releases as "sha version" lines, oldest first.
# Walks git history of VERSION_FILE to find each version bump. Cached after first call.
get_version_list() {
  if [[ -n "$_VERSION_LIST_CACHE" ]]; then
    echo "$_VERSION_LIST_CACHE"
    return
  fi
  _VERSION_LIST_CACHE=$(git log --first-parent origin/main --reverse --format="%H" -- "$VERSION_FILE" | while read -r sha; do
    local ver
    if [[ -n "$VERSION_FIELD" ]]; then
      ver=$(git show "${sha}:${VERSION_FILE}" 2>/dev/null | grep "$VERSION_FIELD" | awk '{print $2}' | head -1)
    else
      ver=$(git show "${sha}:${VERSION_FILE}" 2>/dev/null | tr -d '[:space:]')
    fi
    if [[ -n "$ver" ]]; then
      echo "${sha} ${ver}"
    fi
  done | awk '{if (seen[$2] == 0) { seen[$2]=1; print }}')
  echo "$_VERSION_LIST_CACHE"
}

# Resolve a version string to a commit SHA.
resolve_ref() {
  local version="$1"
  local sha
  sha=$(get_version_list | while IFS=' ' read -r s v; do
    if [[ "$v" == "$version" ]]; then
      echo "$s"
      break
    fi
  done)
  [[ -n "$sha" ]] || die "Unknown version: $version (not found in ${VERSION_FILE} history)"
  echo "$sha"
}

# --- Data collection ---

# Verify the current branch only touches version/changelog files.
# Prevents running the changelog script in an MR that also contains code changes,
# which would cause those unmerged changes to leak into the generated notes.
check_branch_clean() {
  local current_branch
  current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || return 0

  # Skip check on main — the script is meant to run there too
  [[ "$current_branch" != "main" && "$current_branch" != "master" ]] || return 0

  # Get all files changed vs main: committed on the branch, staged, and unstaged
  local changed_files
  changed_files=$(
    git diff --name-only origin/main...HEAD 2>/dev/null
    git diff --name-only 2>/dev/null
    git diff --name-only --cached 2>/dev/null
  ) || return 0
  # Deduplicate
  changed_files=$(echo "$changed_files" | sort -u)
  [[ -n "$changed_files" ]] || return 0

  local bad_files=()
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    # Allow version files, changelogs, and this script itself
    case "$file" in
      */VERSION|*/Chart.yaml|*/CHANGELOG.md|ci/scripts/release-notes*|ci/scripts/generate-release-notes.sh|CLAUDE.md) ;;
      *) bad_files+=("$file") ;;
    esac
  done <<< "$changed_files"

  if [[ ${#bad_files[@]} -gt 0 ]]; then
    echo "" >&2
    echo "WARNING: This branch contains non-release files changed vs main:" >&2
    for f in "${bad_files[@]}"; do
      echo "  - $f" >&2
    done
    echo "" >&2
    echo "Version bumps and changelog generation should be in a dedicated MR," >&2
    echo "separate from code changes, to avoid linking unmerged work in the notes." >&2
    echo "" >&2
    echo "To proceed anyway, re-run with --force." >&2
    exit 1
  fi
}

check_deps() {
  for cmd in git claude; do
    command -v "$cmd" >/dev/null || die "$cmd is required but not found"
  done
  if [[ "$MR_SOURCE" == "gitlab" ]]; then
    command -v glab >/dev/null || die "glab CLI is required for MR_SOURCE=gitlab but not found"
  elif [[ "$MR_SOURCE" == "github" ]]; then
    command -v gh >/dev/null || die "gh CLI is required for MR_SOURCE=github but not found"
  fi
}

# Extract the GitLab project path from git remote (URL-encoded).
gitlab_project_path() {
  local remote_url
  remote_url=$(git remote get-url origin 2>/dev/null) || die "No git remote 'origin' found"
  # Handle HTTPS: https://gitlab.com/group/subgroup/project.git
  # Handle SSH:   git@gitlab.com:group/subgroup/project.git
  local path
  path=$(echo "$remote_url" | sed -E 's#.*gitlab\.com[:/]##; s/\.git$//')
  # URL-encode slashes for API
  echo "${path//\//%2F}"
}

# Cached project path for API calls (computed once).
_PROJECT_PATH=""

# Base URL for commit links (computed once).
_COMMIT_BASE_URL=""

# Get the base URL for linking to commits (e.g. https://github.com/org/repo/commit/).
# Uses COMMIT_URL_BASE from config if set, otherwise derives from git remote.
commit_base_url() {
  if [[ -n "$_COMMIT_BASE_URL" ]]; then
    echo "$_COMMIT_BASE_URL"
    return
  fi
  if [[ -n "$COMMIT_URL_BASE" ]]; then
    _COMMIT_BASE_URL="$COMMIT_URL_BASE"
    echo "$_COMMIT_BASE_URL"
    return
  fi
  local remote_url
  remote_url=$(git remote get-url origin 2>/dev/null) || return 0
  # Normalize to HTTPS path
  local path
  path=$(echo "$remote_url" | sed -E 's#^git@([^:]+):#https://\1/#; s/\.git$//')
  case "$MR_SOURCE" in
    gitlab) _COMMIT_BASE_URL="${path}/-/commit/" ;;
    github) _COMMIT_BASE_URL="${path}/commit/" ;;
    *)      _COMMIT_BASE_URL="${path}/commit/" ;;
  esac
  echo "$_COMMIT_BASE_URL"
}

# Strip noise from MR description: keep only the Description section,
# remove images, internal URLs, and template boilerplate.
strip_mr_noise() {
  sed -E '
    /^## (Testing Done|Risks|Related|Linked JIRA|AI Code Generation|Checklist)/,/^## /{ /^## (Testing Done|Risks|Related|Linked JIRA|AI Code Generation|Checklist)/d; /^## /!d; }
    /^!\[.*\]\(.*\)/d
    /\{width=[0-9]+ height=[0-9]+\}/d
    /^https?:\/\//d
    /^(What testing have you done|Any follow up issues|Did you use any AI|MRs related to this change)/d
  ' | sed '/^$/N;/^\n$/d'
}

# Fetch MR/PR title + description for a commit SHA. Returns empty if not found.
fetch_mr_description() {
  local sha="$1"
  local response=""

  case "$MR_SOURCE" in
    gitlab)
      if [[ -z "$_PROJECT_PATH" ]]; then
        _PROJECT_PATH=$(gitlab_project_path)
      fi
      response=$(glab api "projects/${_PROJECT_PATH}/repository/commits/${sha}/merge_requests" 2>/dev/null) || return 0
      ;;
    github)
      response=$(gh api "repos/{owner}/{repo}/commits/${sha}/pulls" 2>/dev/null) || return 0
      ;;
  esac

  # Extract title and description in a single python3 call
  local title_and_desc
  title_and_desc=$(echo "$response" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if not d: sys.exit(0)
mr = d[0]
title = mr.get('title', '')
desc = mr.get('description') or mr.get('body') or ''
if title: print(title + '\n---\n' + desc)
" 2>/dev/null) || return 0

  [[ -n "$title_and_desc" ]] || return 0

  local title="${title_and_desc%%$'\n---\n'*}"
  local desc="${title_and_desc#*$'\n---\n'}"

  echo "### ${title}"
  if [[ -n "$desc" ]]; then
    echo ""
    echo "$desc" | strip_mr_noise
  fi
}

# List non-CI commit SHAs between two refs (one per line).
list_commit_shas() {
  local start_ref="$1" end_ref="$2"
  local skip_pattern='\[skip ci\]'
  if [[ -n "${COMMIT_SKIP:-}" ]]; then
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      skip_pattern+="|${line}"
    done <<< "$COMMIT_SKIP"
  fi
  # shellcheck disable=SC2086
  git log --first-parent --format="%H %s" "${start_ref}..${end_ref}" -- ${SOURCE_PATHS:-.} \
    | grep -vE "$skip_pattern" || true
}

# Collect MR descriptions for commits between two refs.
# Returns concatenated descriptions, or empty if MR_SOURCE is not configured.
collect_mr_descriptions() {
  local start_ref="$1" end_ref="$2"
  [[ -n "$MR_SOURCE" ]] || return 0

  local shas
  shas=$(list_commit_shas "$start_ref" "$end_ref")
  [[ -n "$shas" ]] || return 0

  local result="" count=0
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    local sha="${line%% *}"
    local subject="${line#* }"
    echo "  - Fetching MR for ${sha:0:8} (${subject})" >&2
    local mr_desc
    mr_desc=$(fetch_mr_description "$sha")
    if [[ -n "$mr_desc" ]]; then
      [[ -n "$result" ]] && result+=$'\n\n---\n\n'
      result+="[commit: ${sha:0:7}]"$'\n'"$mr_desc"
      count=$((count + 1))
    fi
  done <<< "$shas"

  [[ $count -gt 0 ]] && echo "Found ${count} MR description(s)" >&2
  echo "$result"
}

# Collect commit subjects + diffstat between two refs (fallback when no MR descriptions).
collect_fallback_context() {
  local start_ref="$1" end_ref="$2"

  echo "Commits:"
  list_commit_shas "$start_ref" "$end_ref" | while read -r line; do
    local sha="${line%% *}"
    local subject="${line#* }"
    echo "- [${sha:0:7}] ${subject}"
  done

  # Build exclude pathspecs from config
  local -a exclude_args=()
  if [[ -n "${DIFF_EXCLUDE:-}" ]]; then
    for pattern in $DIFF_EXCLUDE; do
      exclude_args+=(":!${pattern}")
    done
  fi

  echo ""
  echo "Changed files:"
  # shellcheck disable=SC2086
  git diff --stat "${start_ref}..${end_ref}" -- ${SOURCE_PATHS:-.} "${exclude_args[@]}" 2>/dev/null || true
}

# --- Note generation ---

# Call Claude CLI to generate release notes from collected context.
generate_notes() {
  local version="$1" context="$2"

  local base_url
  base_url=$(commit_base_url)

  local prompt="You are writing public-facing release notes for ${DESCRIPTION}.

Version: ${version}
Commit URL base: ${base_url}

${context}

Rules:
- Write from the customer's perspective — what changed for them, not internal implementation details
- Commit/MR titles may follow conventional commit format: 'feat:', 'fix:', 'chore:', 'refactor:', 'perf:', 'docs:', 'ci:', 'test:'. Use these types to classify: feat→Features, fix→Bug Fixes, perf/refactor→Improvements. Skip chore/docs/ci/test unless customer-facing
- Group into categories only if there are enough items, using **bold** headings: **Features**, **Improvements**, **Bug Fixes**
- If there are only a few changes, a flat bullet list is fine
- Skip CI/infrastructure-only changes that don't affect the customer
- Skip commits that are reverts followed by reapplies (net zero change)
- Each bullet must be a single short line. No sub-bullets, no multi-sentence bullets
- Only include reasoning/justification for high-impact or surprising changes. Most bullets should just state what changed, not why
- Use imperative present tense like git commit messages (e.g. 'Add region selection to installer', 'Fix NFS metrics collection', 'Enable HTTP/2 for sink connections')
- Do not mention internal version numbers, image tags, or Docker tag prefixes
- Merge multiple MRs that fix the same thing into one bullet — do not repeat similar changes
- Do not repeat the version number as a heading — it is already in the changelog header
- End each bullet with the relevant commit hash as a markdown link using the commit URL base above, e.g. '- Fix foo ([abc1234](<commit_url_base>abc1234))'. If a bullet combines multiple commits, link each: '([abc1234](<url>), [def5678](<url>))'. If no commit URL base is provided, use plain text hashes: '(abc1234)'
- Do not include file paths or MR numbers in the output
- If the changes are mostly CI/infra noise with no customer-facing impact, say \"Internal improvements and maintenance.\"
- Output raw markdown only, no wrapping code fences
- Output ONLY the release notes content — no preamble, commentary, or explanation before or after"

  claude -p --output-format text --model "${CLAUDE_MODEL:-claude-sonnet-4-6}" "$prompt" || die "Claude CLI call failed"
}

# --- Changelog file operations ---

# Prepend an entry to an existing changelog, or create it.
prepend_to_changelog() {
  local changelog="$1" entry="$2"

  if [[ -f "$changelog" ]]; then
    local existing
    existing=$(cat "$changelog")
    # Strip the "# Changelog" header if present, we'll re-add it
    existing="${existing#"# Changelog"}"
    existing="${existing#$'\n'}"
    printf "# Changelog\n\n%s\n\n%s\n" "$entry" "$existing" > "$changelog"
  else
    # Create parent directory if needed
    mkdir -p "$(dirname "$changelog")"
    printf "# Changelog\n\n%s\n" "$entry" > "$changelog"
  fi
}

# --- Modes ---

# Collect context and generate notes for a version range.
generate_notes_for_range() {
  local version="$1" start_sha="$2" end_sha="$3"

  # Try MR descriptions first, fall back to commits + diffstat
  local context
  context=$(collect_mr_descriptions "$start_sha" "$end_sha")
  if [[ -z "$context" ]]; then
    context=$(collect_fallback_context "$start_sha" "$end_sha")
  fi

  if [[ -z "$context" ]]; then
    echo "Internal improvements and maintenance."
    return
  fi

  echo "Sending to Claude CLI..." >&2
  generate_notes "$version" "$context"
}

# Generate notes for a single version range
do_single() {
  local start_ver="$1" end_ver="$2" dry_run="$3"

  local start_sha end_sha
  start_sha=$(resolve_ref "$start_ver")
  end_sha=$(resolve_ref "$end_ver")

  echo "Collecting context for ${start_ver}..${end_ver}" >&2
  local notes
  notes=$(generate_notes_for_range "$end_ver" "$start_sha" "$end_sha")

  local date
  date=$(git log -1 --format="%cs" "$end_sha" 2>/dev/null || echo "unknown")
  local entry
  entry=$(printf "## %s (%s)\n\n%s" "$end_ver" "$date" "$notes")

  if [[ "$dry_run" == "true" ]]; then
    echo "$entry"
  else
    prepend_to_changelog "$CHANGELOG_PATH" "$entry"
    echo "Prepended entry for ${end_ver} to ${CHANGELOG_PATH}" >&2
  fi
}

# Find versions not yet in the changelog and generate notes for them.
do_update() {
  local dry_run="$1"

  # Get all known versions (oldest first)
  local shas=() versions=()
  while IFS=' ' read -r sha ver; do
    shas+=("$sha")
    versions+=("$ver")
  done < <(get_version_list)

  if [[ ${#shas[@]} -eq 0 ]]; then
    die "No versions found"
  fi

  # Find which versions are already in the changelog
  local documented=()
  if [[ -f "$CHANGELOG_PATH" ]]; then
    while IFS= read -r line; do
      if [[ "$line" =~ ^##\ (.+)\ \( ]]; then
        documented+=("${BASH_REMATCH[1]}")
      fi
    done < "$CHANGELOG_PATH"
  fi

  # Filter to only undocumented versions
  local new_shas=() new_versions=()
  for i in "${!versions[@]}"; do
    local ver="${versions[$i]}"
    local found=false
    for d in "${documented[@]+"${documented[@]}"}"; do
      if [[ "$d" == "$ver" ]]; then
        found=true
        break
      fi
    done
    if [[ "$found" == "false" ]]; then
      new_shas+=("${shas[$i]}")
      new_versions+=("$ver")
    fi
  done

  # Also check the working tree VERSION file for a bump not yet on main
  if [[ -f "$VERSION_FILE" ]]; then
    local current_ver
    if [[ -n "$VERSION_FIELD" ]]; then
      current_ver=$(grep "$VERSION_FIELD" "$VERSION_FILE" | awk '{print $2}' | head -1)
    else
      current_ver=$(tr -d '[:space:]' < "$VERSION_FILE")
    fi
    if [[ -n "$current_ver" ]]; then
      # Check if this version is already known or documented
      local known=false
      for v in "${versions[@]+"${versions[@]}"}"; do
        [[ "$v" == "$current_ver" ]] && known=true && break
      done
      for d in "${documented[@]+"${documented[@]}"}"; do
        [[ "$d" == "$current_ver" ]] && known=true && break
      done
      if [[ "$known" == "false" ]]; then
        echo "Detected version bump in working tree: ${current_ver}" >&2
        new_shas+=("main")
        new_versions+=("$current_ver")
        # Add to full version list so prev_ref lookup works
        shas+=("main")
        versions+=("$current_ver")
      fi
    fi
  fi

  if [[ ${#new_shas[@]} -eq 0 ]]; then
    echo "Changelog is up to date — no new versions to document." >&2
    return
  fi

  echo "Found ${#new_shas[@]} undocumented version(s): ${new_versions[*]}" >&2

  local count=0
  for i in "${!new_shas[@]}"; do
    local sha="${new_shas[$i]}"
    local ver="${new_versions[$i]}"

    # Find the previous version's SHA for the diff range
    local prev_ref=""
    for j in "${!versions[@]}"; do
      if [[ "${versions[$j]}" == "$ver" ]]; then
        if [[ "$j" -gt 0 ]]; then
          prev_ref="${shas[$((j-1))]}"
        else
          prev_ref=$(git rev-list --max-parents=0 HEAD | head -1)
        fi
        break
      fi
    done

    echo "" >&2
    echo "=== ${ver} ===" >&2

    local date
    if [[ "$sha" == "main" ]]; then
      date=$(date +%Y-%m-%d)
    else
      date=$(git log -1 --format="%cs" "$sha" 2>/dev/null || echo "unknown")
    fi

    local notes
    notes=$(generate_notes_for_range "$ver" "$prev_ref" "$sha")

    local entry
    entry=$(printf "## %s (%s)\n\n%s" "$ver" "$date" "$notes")

    # Write/print immediately so progress isn't lost on interrupt
    if [[ "$dry_run" == "true" ]]; then
      echo "$entry"
      echo ""
    else
      prepend_to_changelog "$CHANGELOG_PATH" "$entry"
      echo "Wrote ${ver} to ${CHANGELOG_PATH}" >&2
    fi
    count=$((count + 1))
  done

  echo "Done — ${count} entries added." >&2
}

# --- Main ---

usage() {
  echo "Usage:" >&2
  echo "  $0 <component> [--dry-run] [--force]                       Add missing versions to changelog" >&2
  echo "  $0 <component> <start_ver> <end_ver> [--dry-run] [--force] Generate notes for specific range" >&2
  echo "  $0 --list                                                  List configured components" >&2
  echo "" >&2
  echo "Components are configured via ci/scripts/release-notes-<name>.conf." >&2
  if [[ -d "$SCRIPT_DIR" ]]; then
    echo "" >&2
    echo "Available components:" >&2
    list_components
  fi
  exit 1
}

main() {
  check_deps

  local dry_run="false"
  local force="false"
  local args=()
  for arg in "$@"; do
    case "$arg" in
      --dry-run) dry_run="true" ;;
      --force)   force="true" ;;
      *)         args+=("$arg") ;;
    esac
  done

  if [[ ${#args[@]} -eq 0 ]]; then
    usage
  fi

  if [[ "${args[0]}" == "--list" ]]; then
    echo "Configured components:" >&2
    list_components
    exit 0
  fi

  load_component_config "${args[0]}"

  if [[ "$force" != "true" ]]; then
    check_branch_clean
  fi

  if [[ ${#args[@]} -ge 3 ]]; then
    do_single "${args[1]}" "${args[2]}" "$dry_run"
  elif [[ ${#args[@]} -eq 1 ]]; then
    do_update "$dry_run"
  else
    usage
  fi
}

main "$@"
