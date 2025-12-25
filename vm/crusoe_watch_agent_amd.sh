#!/bin/bash

# --- Constants ---
UBUNTU_OS_VERSION=$(lsb_release -r -s)
CRUSOE_VM_ID=$(dmidecode -s system-uuid)

# GitHub branch (optional override via CLI, defaults to main)
GITHUB_BRANCH="main"

# Crusoe environment (optional override via CLI, defaults to main)
ENVIRONMENT="prod"

# Define paths for config files within the GitHub repository
REMOTE_VECTOR_CONFIG_AMD_GPU_VM="config/vector_amd_gpu_vm.yaml"
REMOTE_DOCKER_COMPOSE_VECTOR="docker/docker-compose-vector.yaml"
REMOTE_DOCKER_COMPOSE_AMD_EXPORTER="docker/docker-compose-amd-exporter.yaml"
REMOTE_CRUSOE_WATCH_AGENT_SERVICE="systemctl/crusoe-watch-agent.service"
REMOTE_CRUSOE_AMD_EXPORTER_SERVICE="systemctl/crusoe-amd-exporter.service"
REMOTE_AMD_METRICS_CONFIG="config/amd_metrics_config.json"
SYSTEMCTL_DIR="/etc/systemd/system"
CRUSOE_WATCH_AGENT_DIR="/etc/crusoe/crusoe_watch_agent"
CRUSOE_AUTH_TOKEN_LENGTH=82
ENV_FILE="$CRUSOE_WATCH_AGENT_DIR/.env" # Define the .env file path
# Secrets location for persisted monitoring token
CRUSOE_SECRETS_DIR="/etc/crusoe/secrets"
CRUSOE_MONITORING_TOKEN_FILE="$CRUSOE_SECRETS_DIR/.monitoring-token"

# Versioning and upgrade helpers
REMOTE_VERSION_FILE="vm/VERSION"
INSTALLED_VERSION_FILE="$CRUSOE_WATCH_AGENT_DIR/VERSION"

# Optional parameters with defaults
DEFAULT_AMD_EXPORTER_SERVICE_NAME="crusoe-amd-exporter.service"
AMD_EXPORTER_SERVICE_NAME=$DEFAULT_AMD_EXPORTER_SERVICE_NAME
AMD_EXPORTER_PORT="5000"

# environment to crusoe Ingress endpoint Map
declare -A -r TELEMETRY_INGRESS_MAP=(
  ["dev"]="https://cms-monitoring.crusoecloud.xyz/ingest"
  ["staging"]="https://cms-monitoring.crusoecloud.site/ingest"
  ["prod"]="https://cms-monitoring.crusoecloud.com/ingest"
)

# CLI args parsing
usage() {
  echo "Usage: $0 <command> [options]"
  echo "Commands: install | uninstall | refresh-token | upgrade | help"
  echo "Options:"
  echo "  --branch|-b BRANCH                        Specify GitHub branch (default: main)"
  echo "  --env|-e ENVIRONMENT                      Specify environment: dev|staging|prod (default: prod)"
  echo "  --amd-exporter-service-name NAME          Specify custom AMD exporter service name"
  echo "  --amd-exporter-port PORT                  Specify custom AMD exporter port (default: 5000)"
  echo "Examples:"
  echo "  $0 install --branch main"
  echo "  $0 uninstall"
  echo "  $0 refresh-token"
  echo "  $0 upgrade -b main"
}

parse_args() {
  COMMAND="install"  # default for backward compatibility
  while [[ $# -gt 0 ]]; do
    case "$1" in
      install|uninstall|refresh-token|upgrade|help)
        COMMAND="$1"; shift ;;
      --amd-exporter-service-name|-n)
        if [[ -n "$2" ]]; then
          AMD_EXPORTER_SERVICE_NAME=$(ensure_service_suffix "$2")
          shift 2
        else
          error_exit "Missing value for $1"
        fi
        ;;
      --amd-exporter-port|-p)
        if [[ -n "$2" ]]; then
          AMD_EXPORTER_PORT="$2"; shift 2
        else
          error_exit "Missing value for $1"
        fi
        ;;
      --branch|-b)
        if [[ -n "$2" ]]; then
          GITHUB_BRANCH="$2"; shift 2
        else
          error_exit "Missing value for $1"
        fi
        ;;
      --env|-e)
        if [[ -n "$2" ]]; then
          ENVIRONMENT="$2"; shift 2
        else
          error_exit "Missing value for $1"
        fi
        ;;
      --help|-h)
        usage; exit 0;;
      *)
        echo "Unknown option or command: $1"; usage; exit 1;;
    esac
  done
}

# --- Helper Functions ---

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

ensure_service_suffix() {
  local service_name="$1"
  if [[ "$service_name" != *.service ]]; then
    echo "${service_name}.service"
  else
    echo "$service_name"
  fi
}

file_exists() {
  [ -f "$1" ]
}

dir_exists() {
  [ -d "$1" ]
}

error_exit() {
  echo "Error: $1" >&2
  exit 1
}

status() {
  # Bold text for status messages
  echo -e "\n\033[1m$1\033[0m"
}

check_root() {
  if [[ $EUID -ne 0 ]]; then
      error_exit "This script must be run as root."
  fi
}

# Check if a systemd unit exists (anywhere on the systemd path)
service_exists() {
  systemctl cat "$1" >/dev/null 2>&1
}

# Stop and disable a systemd service if it exists
stop_and_disable_service() {
  local service_name="$1"
  if service_exists "$service_name"; then
    echo "Found $service_name."
    systemctl stop "$service_name" || echo "Warning: Failed to stop $service_name"
    systemctl disable "$service_name" || echo "Warning: Failed to disable $service_name"
    echo "$service_name has been stopped and disabled."
    return 0
  else
    echo "No $service_name found."
    return 1
  fi
}

# --- Token & Version/Lifecycle Helpers ---

write_token_to_secrets() {
  local token="$1"
  mkdir -p "$CRUSOE_SECRETS_DIR" || true
  # Escape dollar signs to prevent variable expansion in docker-compose/vector
  local escaped_token="${token//\$/\$\$}"
  echo "CRUSOE_AUTH_TOKEN=${escaped_token}" > "$CRUSOE_MONITORING_TOKEN_FILE"
  chmod 600 "$CRUSOE_MONITORING_TOKEN_FILE" || true
}

validate_token() {
  local token="$1"
  [[ ${#token} -eq $CRUSOE_AUTH_TOKEN_LENGTH ]]
}

# --- Version & Lifecycle Helpers ---
normalize_version() {
  # strip spaces and trailing non-numeric/dot chars, and any trailing dot
  echo "$1" | tr -d ' \t\r' | sed -E 's/[^0-9.].*$//' | sed -E 's/[.]+$//'
}

get_remote_version() {
  normalize_version "$(curl -fsSL "$GITHUB_RAW_BASE_URL/$REMOTE_VERSION_FILE")"
}

get_installed_version() {
  if [ -f "$INSTALLED_VERSION_FILE" ]; then
    normalize_version "$(cat "$INSTALLED_VERSION_FILE")"
  fi
}

# Returns 0 (true) if $1 < $2 using version sort
version_lt() {
  local a="$1" b="$2"
  [ "$a" != "$b" ] && [ "$(printf '%s\n%s\n' "$a" "$b" | sort -V | tail -n1)" = "$b" ]
}

write_installed_version() {
  local ver
  ver=$(normalize_version "$1")
  if [[ -n "$ver" ]]; then
    echo "$ver" > "$INSTALLED_VERSION_FILE"
  fi
}

check_os_support() {
  # Require Ubuntu 22.04 or later
  if ! version_ge "$UBUNTU_OS_VERSION" "22.04"; then
    error_exit "Ubuntu version $UBUNTU_OS_VERSION is not supported. Require 22.04 or later."
  fi
}

install_docker() {
  curl -fsSL https://get.docker.com | sh
}

# Compare semantic versions a.b.c >= x.y.z
version_ge() {
  # returns 0 (true) if $1 >= $2
  [ "$1" = "$2" ] && return 0
  local IFS=.
  local i ver1=($1) ver2=($2)
  # fill empty fields in ver1 with zeros
  for ((i=${#ver1[@]}; i<3; i++)); do ver1[i]=0; done
  for ((i=${#ver2[@]}; i<3; i++)); do ver2[i]=0; done
  for ((i=0; i<3; i++)); do
    if ((10#${ver1[i]} > 10#${ver2[i]})); then return 0; fi
    if ((10#${ver1[i]} < 10#${ver2[i]})); then return 1; fi
  done
  return 0
}

# Detect ROCm version using apt metadata (no dpkg) or version file
get_rocm_version() {
  local ver=""
  if command_exists apt; then
    # Prefer installed version if present
    ver=$(apt show rocm-libs -a 2>/dev/null | sed -En 's/^Installed: ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p' | head -n1)
    # If not installed or missing Installed field, take the first Version line
    if [[ -z "$ver" ]]; then
      ver=$(apt show rocm-libs -a 2>/dev/null | sed -En 's/^Version: ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p' | head -n1)
    fi
  fi
  if [[ -z "$ver" ]] && [ -f "/opt/rocm/.info/version" ]; then
    ver=$(sed -En 's/^ROCM_VERSION=([0-9]+\.[0-9]+\.[0-9]+).*/\1/p' /opt/rocm/.info/version | head -n1)
  fi
  echo "$ver"
}

ensure_rocm_6_2_or_newer() {
  status "Checking ROCm version (require 6.2.0 or newer for GPU monitoring)."
  local ver
  ver=$(get_rocm_version)
  if [[ -z "$ver" ]]; then
    echo "ROCm not detected. Will proceed with CPU-only monitoring."
    return 1
  fi
  if version_ge "$ver" "6.2.0"; then
    echo "Detected ROCm version: $ver (OK)"
    return 0
  else
    echo "Warning: Detected ROCm version $ver is older than required 6.2.0."
    echo "GPU monitoring may not work properly. Consider upgrading ROCm."
    return 1
  fi
}



do_install() {
  # Ensure the script is run as root.
  check_root
  check_os_support

  status "Ensure docker installation."
  if command_exists docker; then
    echo "Docker is already installed."
  else
    echo "Installing Docker."
    install_docker
  fi

  # Ensure wget is installed
  status "Ensuring wget is installed."
  if ! command_exists wget; then
    (apt-get update && apt-get install -y wget) || error_exit "Failed to install wget."
  fi

  status "Create crusoe_watch_agent target directory."
  if ! dir_exists "$CRUSOE_WATCH_AGENT_DIR"; then
    mkdir -p "$CRUSOE_WATCH_AGENT_DIR"
  fi

  # Detect AMD GPUs
  local HAS_AMD_GPUS=false
  if ensure_rocm_6_2_or_newer; then
    HAS_AMD_GPUS=true
  fi

  if $HAS_AMD_GPUS; then
    # Download Vector config for AMD GPU VM (scrapes AMD exporter)
    status "Download AMD GPU Vector config."
    wget -q -O "$CRUSOE_WATCH_AGENT_DIR/vector.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_VECTOR_CONFIG_AMD_GPU_VM" || error_exit "Failed to download $REMOTE_VECTOR_CONFIG_AMD_GPU_VM"

    # Download AMD Exporter docker-compose and systemd unit, then enable/start service
    status "Prepare AMD metrics config directory."
    mkdir -p "$CRUSOE_WATCH_AGENT_DIR/config" || error_exit "Failed to create $CRUSOE_WATCH_AGENT_DIR/config"
    status "Download AMD metrics config.json."
    wget -q -O "$CRUSOE_WATCH_AGENT_DIR/config/config.json" "$GITHUB_RAW_BASE_URL/$REMOTE_AMD_METRICS_CONFIG" || error_exit "Failed to download $REMOTE_AMD_METRICS_CONFIG"

    status "Download AMD Exporter docker-compose file."
    wget -q -O "$CRUSOE_WATCH_AGENT_DIR/docker-compose-amd-exporter.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_DOCKER_COMPOSE_AMD_EXPORTER" || error_exit "Failed to download $REMOTE_DOCKER_COMPOSE_AMD_EXPORTER"

    status "Install $AMD_EXPORTER_SERVICE_NAME systemd unit."
    wget -q -O "$SYSTEMCTL_DIR/$AMD_EXPORTER_SERVICE_NAME" "$GITHUB_RAW_BASE_URL/$REMOTE_CRUSOE_AMD_EXPORTER_SERVICE" || error_exit "Failed to download $REMOTE_CRUSOE_AMD_EXPORTER_SERVICE"
  else
    # CPU-only VM - use minimal vector config
    status "No AMD GPUs detected. Using CPU-only monitoring configuration."
    # Note: You'll need to create a vector_cpu_vm.yaml config similar to NVIDIA version
    # For now, we'll use the AMD config but it won't collect GPU metrics
    status "Download CPU Vector config."
    wget -q -O "$CRUSOE_WATCH_AGENT_DIR/vector.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_VECTOR_CONFIG_AMD_GPU_VM" || error_exit "Failed to download vector config"
  fi

  status "Download Vector docker-compose file."
  wget -q -O "$CRUSOE_WATCH_AGENT_DIR/docker-compose-vector.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_DOCKER_COMPOSE_VECTOR" || error_exit "Failed to download $REMOTE_DOCKER_COMPOSE_VECTOR"

  status "Ensuring Crusoe auth token in secrets."
  if [[ -n "$CRUSOE_AUTH_TOKEN" ]] && validate_token "$CRUSOE_AUTH_TOKEN"; then
    # Env var provided; write/overwrite secrets store
    write_token_to_secrets "$CRUSOE_AUTH_TOKEN"
  elif [[ -s "$CRUSOE_MONITORING_TOKEN_FILE" ]]; then
    echo "Detected existing token file at $CRUSOE_MONITORING_TOKEN_FILE"
  else
    echo "Command: crusoe monitoring tokens create"
    echo "Please enter the crusoe monitoring token:"
    read -s CRUSOE_AUTH_TOKEN
    echo ""
    if ! validate_token "$CRUSOE_AUTH_TOKEN"; then
      echo "CRUSOE_AUTH_TOKEN should be $CRUSOE_AUTH_TOKEN_LENGTH characters long."
      echo "Use Crusoe CLI to generate a new token:"
      echo "Command: crusoe monitoring tokens create"
      error_exit "CRUSOE_AUTH_TOKEN is invalid."
    fi
    write_token_to_secrets "$CRUSOE_AUTH_TOKEN"
  fi

  # Download version file first so we can read it
  status "Download VERSION file."
  wget -q -O "$INSTALLED_VERSION_FILE" "$GITHUB_RAW_BASE_URL/$REMOTE_VERSION_FILE" || error_exit "Failed to download $GITHUB_RAW_BASE_URL/$REMOTE_VERSION_FILE"

  # Read agent version from VERSION file
  AGENT_VERSION=$(tr -d '[:space:]' < "$INSTALLED_VERSION_FILE")

  # Create .env file
  status "Creating .env file with VM_ID and AMD_EXPORTER_PORT."
  cat <<EOF > "$ENV_FILE"
VM_ID='${CRUSOE_VM_ID}'
AMD_EXPORTER_PORT='${AMD_EXPORTER_PORT}'
TELEMETRY_INGRESS_ENDPOINT='${TELEMETRY_INGRESS_MAP[$ENVIRONMENT]}'
AGENT_VERSION='${AGENT_VERSION}'
EOF
  echo ".env file created at $ENV_FILE"

  # Start AMD exporter after .env is ready
  if $HAS_AMD_GPUS; then
    status "Enable and start systemd services for $AMD_EXPORTER_SERVICE_NAME."
    echo "systemctl daemon-reload"
    systemctl daemon-reload
    echo "systemctl enable $AMD_EXPORTER_SERVICE_NAME"
    systemctl enable "$AMD_EXPORTER_SERVICE_NAME"
    echo "systemctl start $AMD_EXPORTER_SERVICE_NAME"
    systemctl start "$AMD_EXPORTER_SERVICE_NAME"
  fi

  status "Download crusoe-watch-agent.service."
  wget -q -O "$SYSTEMCTL_DIR/crusoe-watch-agent.service" "$GITHUB_RAW_BASE_URL/$REMOTE_CRUSOE_WATCH_AGENT_SERVICE" || error_exit "Failed to download $REMOTE_CRUSOE_WATCH_AGENT_SERVICE"

  status "Enable and start systemd services for crusoe-watch-agent."
  echo "systemctl daemon-reload"
  systemctl daemon-reload
  echo "systemctl enable crusoe-watch-agent.service"
  systemctl enable crusoe-watch-agent.service
  echo "systemctl start crusoe-watch-agent.service"
  systemctl start crusoe-watch-agent.service

  status "Setup Complete!"
  if $HAS_AMD_GPUS; then
    echo "Check status of $AMD_EXPORTER_SERVICE_NAME: 'sudo systemctl status $AMD_EXPORTER_SERVICE_NAME'"
  fi
  echo "Check status of crusoe-watch-agent service: 'sudo systemctl status crusoe-watch-agent.service'"
  echo "Setup finished successfully!"
}

do_uninstall() {
  check_root
  status "Stopping and disabling crusoe-watch-agent service."
  if service_exists "crusoe-watch-agent.service"; then
    systemctl stop crusoe-watch-agent.service || true
    systemctl disable crusoe-watch-agent.service || true
  fi

  status "Stopping and disabling AMD Exporter service if installed by this script."
  if service_exists "$DEFAULT_AMD_EXPORTER_SERVICE_NAME"; then
    systemctl stop "$DEFAULT_AMD_EXPORTER_SERVICE_NAME" || true
    systemctl disable "$DEFAULT_AMD_EXPORTER_SERVICE_NAME" || true
  fi

  status "Removing systemd unit files."
  rm -f "$SYSTEMCTL_DIR/crusoe-watch-agent.service" || true
  rm -f "$SYSTEMCTL_DIR/$AMD_EXPORTER_SERVICE_NAME" || true
  systemctl daemon-reload || true

  status "Removing crusoe_watch_agent directory."
  rm -rf "$CRUSOE_WATCH_AGENT_DIR" || true

  status "Uninstall complete."
}

do_refresh_token() {
  check_root
  status "Refreshing Crusoe Auth Token."
  echo "Command: crusoe monitoring tokens create"
  echo "Please enter the new Crusoe monitoring token:"
  read -s NEW_CRUSOE_AUTH_TOKEN
  echo ""
  if [ "${#NEW_CRUSOE_AUTH_TOKEN}" -ne $CRUSOE_AUTH_TOKEN_LENGTH ]; then
    echo "NEW_CRUSOE_AUTH_TOKEN should be $CRUSOE_AUTH_TOKEN_LENGTH characters long."
    echo "Use Crusoe CLI to generate a new token:"
    echo "Command: crusoe monitoring tokens create"
    error_exit "NEW_CRUSOE_AUTH_TOKEN is invalid. Please provide a valid token."
  fi
  status "Writing token to secrets store at $CRUSOE_MONITORING_TOKEN_FILE..."
  mkdir -p "$CRUSOE_SECRETS_DIR" || true
  # Escape dollar signs to prevent variable expansion in docker-compose/vector
  local escaped_token="${NEW_CRUSOE_AUTH_TOKEN//\$/\$\$}"
  echo "CRUSOE_AUTH_TOKEN=${escaped_token}" > "$CRUSOE_MONITORING_TOKEN_FILE"
  chmod 600 "$CRUSOE_MONITORING_TOKEN_FILE" || true
  status "Token refresh complete."
  echo "CRUSOE_AUTH_TOKEN has been updated in $CRUSOE_MONITORING_TOKEN_FILE."
  echo "For the changes to take effect, you may need to restart the crusoe-watch-agent service:"
  echo "  sudo systemctl restart crusoe-watch-agent"
}

do_upgrade() {
  check_root
  status "Checking for available upgrade."
  local remote_ver installed_ver
  remote_ver=$(get_remote_version)
  if [[ -z "$remote_ver" ]]; then
    error_exit "Failed to determine remote version from $REMOTE_VERSION_FILE on branch $GITHUB_BRANCH."
  fi
  installed_ver=$(get_installed_version)

  if [[ -z "$installed_ver" ]]; then
    status "No installed version detected. Performing clean install of $remote_ver."
    do_uninstall
    do_install
    return
  elif version_lt "$installed_ver" "$remote_ver"; then
    status "Upgrading agent from $installed_ver to $remote_ver."
    do_uninstall
    do_install
  else
    echo "Installed version ($installed_ver) is up-to-date (remote: $remote_ver). No upgrade performed."
  fi
}

# Parse command line arguments
parse_args "$@"

# Update base URL to reflect chosen branch
GITHUB_RAW_BASE_URL="https://raw.githubusercontent.com/crusoecloud/crusoe-watch-agent/${GITHUB_BRANCH}"

# --- Main Script ---

if [[ "$COMMAND" == "help" ]]; then
  usage
  exit 0
fi

case "$COMMAND" in
  install)
    do_install
    ;;
  uninstall)
    do_uninstall
    ;;
  refresh-token)
    do_refresh_token
    ;;
  upgrade)
    do_upgrade
    ;;
  *)
    echo "Unknown command: $COMMAND"; usage; exit 1
    ;;
esac
