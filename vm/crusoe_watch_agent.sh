#!/bin/bash

# --- Constants ---
UBUNTU_OS_VERSION=$(lsb_release -r -s)
CRUSOE_VM_ID=$(dmidecode -s system-uuid)

# GitHub branch (optional override via CLI, defaults to main)
GITHUB_BRANCH="main"

# Crusoe environment (optional override via CLI, defaults to main)
ENVIRONMENT="prod"

# Installation mode: "docker" (default) or "native" (--no-docker)
INSTALL_MODE="docker"

# Define paths for config files within the GitHub repository (vm subdir)
REMOTE_VECTOR_CONFIG_GPU_VM="vm/config/vector_gpu_vm.yaml"
REMOTE_VECTOR_CONFIG_CPU_VM="vm/config/vector_cpu_vm.yaml"
REMOTE_DCGM_EXPORTER_METRICS_CONFIG="vm/config/dcp-metrics-included.csv"
REMOTE_DCGM_EXPORTER_METRICS_CONFIG_NO_NVLINK="vm/config/dcp-metrics-included-no-nvlink.csv"
REMOTE_DOCKER_COMPOSE_DCGM_EXPORTER="vm/docker/docker-compose-dcgm-exporter.yaml"
REMOTE_DOCKER_COMPOSE_VECTOR="vm/docker/docker-compose-vector.yaml"
REMOTE_CRUSOE_WATCH_AGENT_SERVICE="vm/systemctl/crusoe-watch-agent.service"
REMOTE_CRUSOE_DCGM_EXPORTER_SERVICE="vm/systemctl/crusoe-dcgm-exporter.service"
REMOTE_CRUSOE_WATCH_AGENT_NATIVE_SERVICE="vm/systemctl/crusoe-watch-agent-native.service"
REMOTE_CRUSOE_DCGM_EXPORTER_NATIVE_SERVICE="vm/systemctl/crusoe-dcgm-exporter-native.service"
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
INSTALL_MODE_FILE="$CRUSOE_SECRETS_DIR/.install-mode"

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

LOGS_INGRESS_ENDPOINT="https://cms-monitoring.crusoecloud.com/logs/ingest"

# CLI args parsing
usage() {
  echo "Usage: $0 <command> [options]"
  echo "Commands: install | uninstall | refresh-token | upgrade | help"
  echo "Options:"
  echo "  --no-docker                               Install using native binaries instead of Docker (default: Docker)"
  echo "  --dcgm-exporter-service-name NAME         Specify custom DCGM exporter service name"
  echo "  --dcgm-exporter-service-port PORT         Specify custom DCGM exporter port"
  echo "  --replace-dcgm-exporter [SERVICE_NAME]    Replace pre-installed dcgm-exporter systemd service with Crusoe version for full metrics collection."
  echo "  --logs-endpoint URL                       Override the logs ingress endpoint"
  echo "                                            Optional SERVICE_NAME defaults to dcgm-exporter"
  echo "Defaults: NAME=crusoe-dcgm-exporter, PORT=9400, MODE=docker"
  echo "Examples:"
  echo "  $0 install --branch main"
  echo "  $0 install --no-docker"
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
      --no-docker)
        INSTALL_MODE="native"; shift ;;
      --logs-endpoint)
        if [[ -n "$2" ]]; then
          LOGS_INGRESS_ENDPOINT="$2"; shift 2
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

# --- Install Mode Persistence ---

write_install_mode() {
  echo "$INSTALL_MODE" > "$INSTALL_MODE_FILE"
}

read_install_mode() {
  if [[ -f "$INSTALL_MODE_FILE" ]]; then
    INSTALL_MODE=$(cat "$INSTALL_MODE_FILE")
  else
    echo "Warning: No install mode file found at $INSTALL_MODE_FILE. Defaulting to '$INSTALL_MODE'."
  fi
}

# --- Native Installation Functions ---

install_vector_native() {
  if command_exists vector; then
    echo "Vector is already installed."
  else
    status "Installing Vector via APT."
    bash -c "$(curl -L https://setup.vector.dev)" || error_exit "Failed to add Vector APT repository."
    apt-get install -y vector || error_exit "Failed to install Vector."
  fi
  # Disable the default vector.service; we use our own custom unit
  systemctl disable vector.service 2>/dev/null || true
  systemctl stop vector.service 2>/dev/null || true
}

install_dcgm_exporter_native() {
  if command_exists dcgm-exporter; then
    echo "dcgm-exporter is already installed."
    return
  fi

  status "Building dcgm-exporter from source."
  local BUILD_DIR
  BUILD_DIR=$(mktemp -d)

  # Ensure Git is installed
  if ! command_exists git; then
    apt-get update && apt-get install -y git || error_exit "Failed to install git."
  fi

  # Ensure make is installed (needed for building dcgm-exporter)
  if ! command_exists make; then
    apt-get update && apt-get install -y make || error_exit "Failed to install make."
  fi

  # Ensure Go >= 1.24 is installed (Ubuntu apt packages are too old)
  local NEED_GO=false
  if ! command_exists go; then
    NEED_GO=true
  else
    local GO_VER
    GO_VER=$(go version | sed -E 's/.*go([0-9]+\.[0-9]+).*/\1/')
    if awk "BEGIN{exit !($GO_VER < 1.24)}"; then
      echo "Installed Go version ($GO_VER) is too old. Upgrading."
      NEED_GO=true
    fi
  fi
  if $NEED_GO; then
    status "Installing Go 1.24 from official tarball."
    local GO_TAR="go1.24.0.linux-amd64.tar.gz"
    wget -q -O "/tmp/$GO_TAR" "https://go.dev/dl/$GO_TAR" || error_exit "Failed to download Go."
    rm -rf /usr/local/go
    tar -C /usr/local -xzf "/tmp/$GO_TAR" || error_exit "Failed to extract Go."
    rm -f "/tmp/$GO_TAR"
    export PATH="/usr/local/go/bin:$PATH"
  fi

  git clone https://github.com/NVIDIA/dcgm-exporter.git "$BUILD_DIR" || error_exit "Failed to clone dcgm-exporter."
  make -C "$BUILD_DIR" binary || error_exit "Failed to build dcgm-exporter."
  make -C "$BUILD_DIR" install || error_exit "Failed to install dcgm-exporter."
  rm -rf "$BUILD_DIR"
  status "dcgm-exporter installed successfully."
}

uninstall_native() {
  status "Removing native packages."
  if command_exists vector; then
    apt-get remove -y vector || true
  fi
  if command_exists dcgm-exporter; then
    rm -f /usr/bin/dcgm-exporter || true
  fi
  # Remove DCGM if it was installed by this script
  if dpkg -l 'datacenter-gpu-manager-4-cuda*' 2>/dev/null | grep -q '^ii'; then
    status "Removing DCGM (datacenter-gpu-manager-4)."
    systemctl stop nvidia-dcgm || true
    systemctl disable nvidia-dcgm || true
    apt-get remove -y 'datacenter-gpu-manager-4-cuda*' || true
  fi
}

setup_nvidia_cuda_repo() {
  status "Setting up NVIDIA CUDA apt repository."

  local UBUNTU_VERSION
  UBUNTU_VERSION=$(echo "$UBUNTU_OS_VERSION" | sed 's/\.//')

  local KEYRING_URL="https://developer.download.nvidia.com/compute/cuda/repos/ubuntu${UBUNTU_VERSION}/x86_64/cuda-keyring_1.1-1_all.deb"
  local KEYRING_DEB="/tmp/cuda-keyring.deb"

  wget -q -O "$KEYRING_DEB" "$KEYRING_URL" || error_exit "Failed to download cuda-keyring from $KEYRING_URL"
  dpkg -i "$KEYRING_DEB" || error_exit "Failed to install cuda-keyring."
  rm -f "$KEYRING_DEB"

  apt-get update || error_exit "Failed to update package lists after adding NVIDIA repo."
  status "NVIDIA CUDA apt repository configured."
}

install_dcgm() {
  status "Installing DCGM (Data Center GPU Manager)."

  if ! command_exists nvidia-smi; then
    error_exit "nvidia-smi not found. Cannot determine CUDA version for DCGM installation."
  fi

  local CUDA_VERSION
  CUDA_VERSION=$(nvidia-smi | sed -E -n 's/.*CUDA Version: ([0-9]+)\..*/\1/p')

  if [[ -z "$CUDA_VERSION" ]]; then
    error_exit "Could not determine CUDA version. DCGM installation aborted."
  fi
  echo "Found CUDA Version: $CUDA_VERSION"

  # Remove any previous DCGM installations to avoid conflicts
  dpkg --list datacenter-gpu-manager &> /dev/null && apt purge --yes datacenter-gpu-manager
  dpkg --list datacenter-gpu-manager-config &> /dev/null && apt purge --yes datacenter-gpu-manager-config

  # Ensure the NVIDIA CUDA apt repository is configured
  setup_nvidia_cuda_repo

  apt-get install --yes --install-recommends "datacenter-gpu-manager-4-cuda${CUDA_VERSION}" || error_exit "Failed to install datacenter-gpu-manager-4-cuda${CUDA_VERSION}."

  systemctl --now enable nvidia-dcgm || error_exit "Failed to enable and start nvidia-dcgm service."

  status "DCGM installed and started successfully."
}

# --- Token & Version/Lifecycle Helpers ---

write_token_to_secrets() {
  local token="$1"
  mkdir -p "$CRUSOE_SECRETS_DIR" || true
  if [[ "$INSTALL_MODE" == "docker" ]]; then
    # Escape dollar signs to prevent variable expansion in docker-compose
    local escaped_token="${token//\$/\$\$}"
    echo "CRUSOE_AUTH_TOKEN=${escaped_token}" > "$CRUSOE_MONITORING_TOKEN_FILE"
  else
    # Native mode: systemd EnvironmentFile reads raw values
    echo "CRUSOE_AUTH_TOKEN=${token}" > "$CRUSOE_MONITORING_TOKEN_FILE"
  fi
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

  # Install runtime dependencies based on mode
  if [[ "$INSTALL_MODE" == "docker" ]]; then
    status "Ensure docker installation."
    if command_exists docker; then
      echo "Docker is already installed."
    else
      echo "Installing Docker."
      install_docker
    fi
  else
    status "Installing Vector natively via APT."
    install_vector_native
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
  elif lspci 2>/dev/null | grep -qi 'NVIDIA'; then
    # GPU hardware detected but nvidia-smi not available — drivers not installed
    error_exit "NVIDIA GPU detected but GPU drivers are not installed. Please install NVIDIA drivers and try again."
  fi

  if $HAS_NVIDIA_GPUS; then
    if [[ "$INSTALL_MODE" == "docker" ]]; then
      # Docker mode: require both dcgmi and nvidia-ctk pre-installed
      status "Ensure NVIDIA dependencies exist."
      if command_exists dcgmi && command_exists nvidia-ctk; then
        echo "Required NVIDIA dependencies are already installed."
        upgrade_dcgm
      else
        error_exit "Please make sure NVIDIA dependencies (dcgm & nvidia-ctk) are installed and try again."
      fi
      # OS version check only applies to Docker mode (selects image tag)
      check_os_support
    else
      # Native mode: install DCGM if missing, nvidia-ctk not needed
      if command_exists dcgmi; then
        echo "DCGM is already installed."
        upgrade_dcgm
      else
        install_dcgm
      fi
      # Build dcgm-exporter from source
      install_dcgm_exporter_native
    fi

    status "Checking NVLink status."
    local METRICS_CONFIG_URL="$GITHUB_RAW_BASE_URL/$REMOTE_DCGM_EXPORTER_METRICS_CONFIG"  # default to standard config
    NVLINK_STATUS=$(nvidia-smi nvlink --status 2>&1 | xargs)
    if [[ -z "$NVLINK_STATUS" || "$NVLINK_STATUS" == *"all links are inActive"* ]]; then
      echo "NVLink is inactive or unavailable. Using no-nvlink metrics config."
      METRICS_CONFIG_URL="$GITHUB_RAW_BASE_URL/$REMOTE_DCGM_EXPORTER_METRICS_CONFIG_NO_NVLINK"
    else
      echo "NVLink is active. Using standard metrics config."
    fi

    status "Download DCGM exporter metrics config."
    wget -q -O "$CRUSOE_WATCH_AGENT_DIR/dcp-metrics-included.csv" "$METRICS_CONFIG_URL" || error_exit "Failed to download $METRICS_CONFIG_URL"

    status "Download GPU Vector config."
    wget -q -O "$CRUSOE_WATCH_AGENT_DIR/vector.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_VECTOR_CONFIG_GPU_VM" || error_exit "Failed to download $REMOTE_VECTOR_CONFIG_GPU_VM"

    if $REPLACE_DCGM_EXPORTER; then
      status "Checking for pre-installed dcgm-exporter service: $EXISTING_DCGM_EXPORTER_SERVICE"
      stop_and_disable_service "$EXISTING_DCGM_EXPORTER_SERVICE"
    fi

    # Unmask the DCGM exporter service if it was previously masked
    systemctl unmask "$DCGM_EXPORTER_SERVICE_NAME" 2>/dev/null || true

    # Download DCGM Exporter artifacts if service does not exist or replace flag is set
    if ! service_exists "$DCGM_EXPORTER_SERVICE_NAME" || $REPLACE_DCGM_EXPORTER; then
      if [[ "$INSTALL_MODE" == "docker" ]]; then
        status "Download DCGM Exporter docker-compose file."
        wget -q -O "$CRUSOE_WATCH_AGENT_DIR/docker-compose-dcgm-exporter.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_DOCKER_COMPOSE_DCGM_EXPORTER" || error_exit "Failed to download $REMOTE_DOCKER_COMPOSE_DCGM_EXPORTER"

        status "Download $DCGM_EXPORTER_SERVICE_NAME systemd unit."
        wget -q -O "$SYSTEMCTL_DIR/$DCGM_EXPORTER_SERVICE_NAME" "$GITHUB_RAW_BASE_URL/$REMOTE_CRUSOE_DCGM_EXPORTER_SERVICE" || error_exit "Failed to download $REMOTE_CRUSOE_DCGM_EXPORTER_SERVICE"
      else
        status "Download $DCGM_EXPORTER_SERVICE_NAME native systemd unit."
        wget -q -O "$SYSTEMCTL_DIR/$DCGM_EXPORTER_SERVICE_NAME" "$GITHUB_RAW_BASE_URL/$REMOTE_CRUSOE_DCGM_EXPORTER_NATIVE_SERVICE" || error_exit "Failed to download $REMOTE_CRUSOE_DCGM_EXPORTER_NATIVE_SERVICE"
      fi
    fi
  else
     status "Copy CPU Vector config."
     wget -q -O "$CRUSOE_WATCH_AGENT_DIR/vector.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_VECTOR_CONFIG_CPU_VM" || error_exit "Failed to download $REMOTE_VECTOR_CONFIG_CPU_VM"
  fi

  # Download Vector docker-compose file (Docker mode only)
  if [[ "$INSTALL_MODE" == "docker" ]]; then
    status "Download Vector docker-compose file."
    wget -q -O "$CRUSOE_WATCH_AGENT_DIR/docker-compose-vector.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_DOCKER_COMPOSE_VECTOR" || error_exit "Failed to download $REMOTE_DOCKER_COMPOSE_VECTOR"
  fi

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
  if [[ "$INSTALL_MODE" == "docker" ]]; then
    cat <<EOF > "$ENV_FILE"
VM_ID='${CRUSOE_VM_ID}'
DCGM_EXPORTER_PORT='${DCGM_EXPORTER_SERVICE_PORT}'
DCGM_EXPORTER_IMAGE_VERSION='${DCGM_EXPORTER_VERSION_MAP[$UBUNTU_OS_VERSION]}'
TELEMETRY_INGRESS_ENDPOINT='${TELEMETRY_INGRESS_MAP[$ENVIRONMENT]}'
LOGS_INGRESS_ENDPOINT='${LOGS_INGRESS_ENDPOINT}'
AGENT_VERSION='${AGENT_VERSION}'
EOF
  else
    cat <<EOF > "$ENV_FILE"
VM_ID='${CRUSOE_VM_ID}'
DCGM_EXPORTER_PORT='${DCGM_EXPORTER_SERVICE_PORT}'
TELEMETRY_INGRESS_ENDPOINT='${TELEMETRY_INGRESS_MAP[$ENVIRONMENT]}'
LOGS_INGRESS_ENDPOINT='${LOGS_INGRESS_ENDPOINT}'
AGENT_VERSION='${AGENT_VERSION}'
EOF
  fi
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

  # Download the appropriate crusoe-watch-agent systemd unit
  if [[ "$INSTALL_MODE" == "docker" ]]; then
    status "Download crusoe-watch-agent.service."
    wget -q -O "$SYSTEMCTL_DIR/crusoe-watch-agent.service" "$GITHUB_RAW_BASE_URL/$REMOTE_CRUSOE_WATCH_AGENT_SERVICE" || error_exit "Failed to download $REMOTE_CRUSOE_WATCH_AGENT_SERVICE"
  else
    status "Download crusoe-watch-agent.service (native)."
    wget -q -O "$SYSTEMCTL_DIR/crusoe-watch-agent.service" "$GITHUB_RAW_BASE_URL/$REMOTE_CRUSOE_WATCH_AGENT_NATIVE_SERVICE" || error_exit "Failed to download $REMOTE_CRUSOE_WATCH_AGENT_NATIVE_SERVICE"
  fi

  status "Enable and start systemd services for crusoe-watch-agent."
  echo "systemctl daemon-reload"
  systemctl daemon-reload
  echo "systemctl enable crusoe-watch-agent.service"
  systemctl enable crusoe-watch-agent.service
  echo "systemctl start crusoe-watch-agent.service"
  systemctl start crusoe-watch-agent.service

  # Persist the install mode for upgrade/uninstall
  write_install_mode

  status "Setup Complete! (mode: $INSTALL_MODE)"
  if $HAS_NVIDIA_GPUS; then
    echo "Check status of $DCGM_EXPORTER_SERVICE_NAME: 'sudo systemctl status $DCGM_EXPORTER_SERVICE_NAME'"
  fi
  echo "Check status of crusoe-watch-agent service: 'sudo systemctl status crusoe-watch-agent.service'"
  echo "Setup finished successfully!"
}

do_uninstall() {
  check_root

  # Read persisted install mode so we clean up correctly
  read_install_mode

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

  # Remove native packages if installed in native mode
  if [[ "$INSTALL_MODE" == "native" ]]; then
    uninstall_native
  fi

  status "Removing crusoe_watch_agent directory."
  rm -rf "$CRUSOE_WATCH_AGENT_DIR" || true
  rm -f "$INSTALL_MODE_FILE" || true

  status "Uninstall complete."
}

do_refresh_token() {
  check_root

  # Read persisted install mode for correct token escaping
  read_install_mode

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
  write_token_to_secrets "$NEW_CRUSOE_AUTH_TOKEN"
  status "Token refresh complete."
  echo "CRUSOE_AUTH_TOKEN has been updated in $CRUSOE_MONITORING_TOKEN_FILE."
  echo "For the changes to take effect, you may need to restart the crusoe-watch-agent service:"
  echo "  sudo systemctl restart crusoe-watch-agent"
}

do_upgrade() {
  check_root

  # Read persisted install mode so upgrade preserves the mode
  read_install_mode

  status "Checking for available upgrade (mode: $INSTALL_MODE)."
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