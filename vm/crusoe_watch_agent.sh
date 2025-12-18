#!/bin/bash

# --- Constants ---
UBUNTU_OS_VERSION=$(lsb_release -r -s)
CRUSOE_VM_ID=$(dmidecode -s system-uuid)

# GitHub branch (optional override via CLI, defaults to main)
GITHUB_BRANCH="main"

# Crusoe environment (optional override via CLI, defaults to main)
ENVIRONMENT="prod"

# Define paths for config files within the GitHub repository (vm subdir)
REMOTE_VECTOR_CONFIG_GPU_VM="vm/config/vector_gpu_vm.yaml"
REMOTE_VECTOR_CONFIG_CPU_VM="vm/config/vector_cpu_vm.yaml"
REMOTE_DCGM_EXPORTER_METRICS_CONFIG="vm/config/dcp-metrics-included.csv"
REMOTE_DOCKER_COMPOSE_DCGM_EXPORTER="vm/docker/docker-compose-dcgm-exporter.yaml"
REMOTE_DOCKER_COMPOSE_VECTOR="vm/docker/docker-compose-vector.yaml"
REMOTE_CRUSOE_WATCH_AGENT_SERVICE="vm/systemctl/crusoe-watch-agent.service"
REMOTE_CRUSOE_DCGM_EXPORTER_SERVICE="vm/systemctl/crusoe-dcgm-exporter.service"
SYSTEMCTL_DIR="/etc/systemd/system"
CRUSOE_WATCH_AGENT_DIR="/etc/crusoe/crusoe_watch_agent"
CRUSOE_AUTH_TOKEN_LENGTH=82
ENV_FILE="$CRUSOE_WATCH_AGENT_DIR/.env" # Define the .env file path
# Secrets location for persisted monitoring token
CRUSOE_SECRETS_DIR="/etc/crusoe/secrets"
CRUSOE_MONITORING_TOKEN_FILE="$CRUSOE_SECRETS_DIR/.monitoring-token"

# Optional parameters with defaults
DEFAULT_DCGM_EXPORTER_SERVICE_NAME="crusoe-dcgm-exporter.service"
DCGM_EXPORTER_SERVICE_NAME=$DEFAULT_DCGM_EXPORTER_SERVICE_NAME
DCGM_EXPORTER_SERVICE_PORT="9400"
REPLACE_DCGM_EXPORTER=false
EXISTING_DCGM_EXPORTER_SERVICE="dcgm-exporter"

# Versioning and upgrade helpers (use vm/VERSION)
REMOTE_VERSION_FILE="vm/VERSION"
INSTALLED_VERSION_FILE="$CRUSOE_WATCH_AGENT_DIR/VERSION"

# dcgm-exporter docker image version map
declare -A -r DCGM_EXPORTER_VERSION_MAP=(
  ["20.04"]="4.3.1-4.4.0-ubi9"
  ["22.04"]="4.3.1-4.4.0-ubuntu22.04"
  ["24.04"]="4.3.1-4.4.0-ubi9"
)

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
  echo "  --dcgm-exporter-service-name NAME         Specify custom DCGM exporter service name"
  echo "  --dcgm-exporter-service-port PORT         Specify custom DCGM exporter port"
  echo "  --replace-dcgm-exporter [SERVICE_NAME]    Replace pre-installed dcgm-exporter systemd service with Crusoe version for full metrics collection."
  echo "                                            Optional SERVICE_NAME defaults to dcgm-exporter"
  echo "Defaults: NAME=crusoe-dcgm-exporter, PORT=9400"
  echo "Examples:"
  echo "  $0 install --branch main"
  echo "  $0 install --replace-dcgm-exporter"
  echo "  $0 install --replace-dcgm-exporter my-dcgm-exporter"
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
      --dcgm-exporter-service-name|-n)
        if [[ -n "$2" ]]; then
          DCGM_EXPORTER_SERVICE_NAME=$(ensure_service_suffix "$2")
          shift 2
        else
          error_exit "Missing value for $1"
        fi
        ;;
      --dcgm-exporter-service-port|-p)
        if [[ -n "$2" ]]; then
          DCGM_EXPORTER_SERVICE_PORT="$2"; shift 2
        else
          error_exit "Missing value for $1"
        fi
        ;;
      # this is a hidden option to be used for internal testing
      --branch|-b)
        if [[ -n "$2" ]]; then
          GITHUB_BRANCH="$2"; shift 2
        else
          error_exit "Missing value for $1"
        fi
        ;;
      # this is a hidden option to be used for internal testing
      --env|-e)
        if [[ -n "$2" ]]; then
          ENVIRONMENT="$2"; shift 2
        else
          error_exit "Missing value for $1"
        fi
        ;;
      --replace-dcgm-exporter)
        REPLACE_DCGM_EXPORTER=true
        shift
        # Check if next argument is a service name (not a flag)
        if [[ -n "$1" && "$1" != --* ]]; then
          EXISTING_DCGM_EXPORTER_SERVICE="$1"
          shift
        fi
        EXISTING_DCGM_EXPORTER_SERVICE=$(ensure_service_suffix "$EXISTING_DCGM_EXPORTER_SERVICE")
        ;;
      --help|-h)
        usage; exit 0;;
      *)
        echo "Unknown option or command: $1"; usage; exit 1;;
    esac
  done
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

do_install() {
  # Ensure the script is run as root.
  check_root

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

  # Detect NVIDIA GPUs
  local HAS_NVIDIA_GPUS=false
  if command_exists nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then
    HAS_NVIDIA_GPUS=true
  fi

  if $HAS_NVIDIA_GPUS; then
    status "Ensure NVIDIA dependencies exist."
    if command_exists dcgmi && command_exists nvidia-ctk; then
      echo "Required NVIDIA dependencies are already installed."
      # Check and upgrade DCGM here
      upgrade_dcgm
    else
      error_exit "Please make sure NVIDIA dependencies (dcgm & nvidia-ctk) are installed and try again."
    fi

    check_os_support

    status "Download DCGM exporter metrics config."
    wget -q -O "$CRUSOE_WATCH_AGENT_DIR/dcp-metrics-included.csv" "$GITHUB_RAW_BASE_URL/$REMOTE_DCGM_EXPORTER_METRICS_CONFIG" || error_exit "Failed to download $GITHUB_RAW_BASE_URL/$REMOTE_DCGM_EXPORTER_METRICS_CONFIG"

    status "Download GPU Vector config."
    wget -q -O "$CRUSOE_WATCH_AGENT_DIR/vector.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_VECTOR_CONFIG_GPU_VM" || error_exit "Failed to download $REMOTE_VECTOR_CONFIG_GPU_VM"

    if $REPLACE_DCGM_EXPORTER; then
      status "Checking for pre-installed dcgm-exporter service: $EXISTING_DCGM_EXPORTER_SERVICE"
      stop_and_disable_service "$EXISTING_DCGM_EXPORTER_SERVICE"
    fi

    # Download DCGM Exporter artifacts if service does not exist or replace flag is set
    if ! service_exists "$DCGM_EXPORTER_SERVICE_NAME" || $REPLACE_DCGM_EXPORTER; then
      status "Download DCGM Exporter docker-compose file."
      wget -q -O "$CRUSOE_WATCH_AGENT_DIR/docker-compose-dcgm-exporter.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_DOCKER_COMPOSE_DCGM_EXPORTER" || error_exit "Failed to download $REMOTE_DOCKER_COMPOSE_DCGM_EXPORTER"

      status "Download $DCGM_EXPORTER_SERVICE_NAME systemd unit."
      wget -q -O "$SYSTEMCTL_DIR/$DCGM_EXPORTER_SERVICE_NAME" "$GITHUB_RAW_BASE_URL/$REMOTE_CRUSOE_DCGM_EXPORTER_SERVICE" || error_exit "Failed to download $REMOTE_CRUSOE_DCGM_EXPORTER_SERVICE"
    fi
  else
     status "Copy CPU Vector config."
     wget -q -O "$CRUSOE_WATCH_AGENT_DIR/vector.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_VECTOR_CONFIG_CPU_VM" || error_exit "Failed to download $REMOTE_VECTOR_CONFIG_CPU_VM"
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
  status "Creating .env file with VM_ID and DCGM_EXPORTER_PORT."
  cat <<EOF > "$ENV_FILE"
VM_ID='${CRUSOE_VM_ID}'
DCGM_EXPORTER_PORT='${DCGM_EXPORTER_SERVICE_PORT}'
DCGM_EXPORTER_IMAGE_VERSION='${DCGM_EXPORTER_VERSION_MAP[$UBUNTU_OS_VERSION]}'
TELEMETRY_INGRESS_ENDPOINT='${TELEMETRY_INGRESS_MAP[$ENVIRONMENT]}'
AGENT_VERSION='${AGENT_VERSION}'
EOF
  echo ".env file created at $ENV_FILE"

  # Start DCGM exporter after .env is ready
  if $HAS_NVIDIA_GPUS; then
    status "Enable and start systemd services for $DCGM_EXPORTER_SERVICE_NAME."
    echo "systemctl daemon-reload"
    systemctl daemon-reload
    echo "systemctl enable $DCGM_EXPORTER_SERVICE_NAME"
    systemctl enable "$DCGM_EXPORTER_SERVICE_NAME"
    echo "systemctl start $DCGM_EXPORTER_SERVICE_NAME"
    systemctl start "$DCGM_EXPORTER_SERVICE_NAME"
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
  if $HAS_NVIDIA_GPUS; then
    echo "Check status of $DCGM_EXPORTER_SERVICE_NAME: 'sudo systemctl status $DCGM_EXPORTER_SERVICE_NAME'"
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

  status "Stopping and disabling DCGM Exporter service if installed by this script."
  if service_exists "$DEFAULT_DCGM_EXPORTER_SERVICE_NAME"; then
    systemctl stop "$DEFAULT_DCGM_EXPORTER_SERVICE_NAME" || true
    systemctl disable "$DEFAULT_DCGM_EXPORTER_SERVICE_NAME" || true
  fi

  status "Removing systemd unit files."
  rm -f "$SYSTEMCTL_DIR/crusoe-watch-agent.service" || true
  rm -f "$SYSTEMCTL_DIR/$DCGM_EXPORTER_SERVICE_NAME" || true
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

check_os_support() {
  local os_version_supported=0 # Flag to check if version is found

  for supported_version in "${!DCGM_EXPORTER_VERSION_MAP[@]}"; do
    if [[ "$UBUNTU_OS_VERSION" == "$supported_version" ]]; then
      os_version_supported=1
      break # Exit the loop once a match is found
    fi
  done

  if [[ $os_version_supported -eq 0 ]]; then
    error_exit "Ubuntu version $UBUNTU_OS_VERSION is not supported."
  fi
}

install_docker() {
  curl -fsSL https://get.docker.com | sh
}

# Function to check and upgrade DCGM version
upgrade_dcgm() {
  status "Checking DCGM version for upgrade."

  local dcgm_version_raw=$(dcgmi --version | grep 'version:' | awk '{print $3}' | cut -d'.' -f1)
  local dcgm_version_major=${dcgm_version_raw:0:1}

  if [[ "$dcgm_version_major" -lt 4 ]]; then
    status "Current DCGM version ($dcgm_version_major.x.x) is older than 4.x.x. Upgrading DCGM."

    # Stop DCGM service
    systemctl --now disable nvidia-dcgm || error_exit "Failed to disable and stop nvidia-dcgm service."

    # Purge old packages
    dpkg --list datacenter-gpu-manager &> /dev/null && apt purge --yes datacenter-gpu-manager
    dpkg --list datacenter-gpu-manager-config &> /dev/null && apt purge --yes datacenter-gpu-manager-config

    # Update package lists
    apt-get update || error_exit "Failed to update package lists."

    # Get CUDA version
    if ! command_exists nvidia-smi; then
      error_exit "nvidia-smi not found. Cannot determine CUDA version for DCGM upgrade."
    fi
    local CUDA_VERSION=$(nvidia-smi -q | sed -E -n 's/CUDA Version[ :]+([0-9]+)[.].*/\1/p')

    if [[ -z "$CUDA_VERSION" ]]; then
      error_exit "Could not determine CUDA version. DCGM upgrade aborted."
    fi
    echo "Found CUDA Version: $CUDA_VERSION"

    # Install new DCGM package
    apt-get install --yes --install-recommends "datacenter-gpu-manager-4-cuda${CUDA_VERSION}" || error_exit "Failed to install datacenter-gpu-manager-4-cuda${CUDA_VERSION}."

    # Enable and start the new service
    systemctl --now enable nvidia-dcgm || error_exit "Failed to enable and start nvidia-dcgm service."

    status "DCGM upgrade complete."
  else
    echo "DCGM version is already 4.x.x or newer. No upgrade needed."
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