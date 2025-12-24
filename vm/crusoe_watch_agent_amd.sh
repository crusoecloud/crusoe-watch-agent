#!/bin/bash

# --- Constants ---
UBUNTU_OS_VERSION=$(lsb_release -r -s)
CRUSOE_VM_ID=$(dmidecode -s system-uuid)

# GitHub branch (optional override via CLI, defaults to main)
GITHUB_BRANCH="main"

# Define paths for config files within the GitHub repository
REMOTE_VECTOR_CONFIG_AMD_GPU_VM="config/vector_amd_gpu_vm.yaml"
REMOTE_DOCKER_COMPOSE_VECTOR="docker/docker-compose-vector.yaml"
REMOTE_DOCKER_COMPOSE_AMD_EXPORTER="docker/docker-compose-amd-exporter.yaml"
REMOTE_CRUSOE_TELEMETRY_SERVICE="systemctl/crusoe-telemetry-agent.service"
REMOTE_CRUSOE_AMD_EXPORTER_SERVICE="systemctl/crusoe-amd-exporter.service"
REMOTE_AMD_METRICS_CONFIG="config/amd_metrics_config.json"
SYSTEMCTL_DIR="/etc/systemd/system"
CRUSOE_TELEMETRY_AGENT_DIR="/etc/crusoe/telemetry_agent"
CRUSOE_AUTH_TOKEN_LENGTH=82
ENV_FILE="$CRUSOE_TELEMETRY_AGENT_DIR/.env" # Define the .env file path
CRUSOE_AUTH_TOKEN_REFRESH_ALIAS_PATH="/usr/bin/crusoe_auth_token_refresh"

# CLI args parsing
usage() {
  echo "Usage: $0 [--branch BRANCH]"
  echo "Defaults: BRANCH=main"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --branch|-b)
        if [[ -n "$2" ]]; then
          GITHUB_BRANCH="$2"; shift 2
        else
          error_exit "Missing value for $1"
        fi
        ;;
      --help|-h)
        usage; exit 0;;
      *)
        echo "Unknown option: $1"; usage; exit 1;;
    esac
  done
}

# --- Helper Functions ---

command_exists() {
  command -v "$1" >/dev/null 2>&1
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
  status "Checking ROCm version (require 6.2.0 or newer)."
  local ver
  ver=$(get_rocm_version)
  if [[ -z "$ver" ]]; then
    error_exit "ROCm not detected. Please install ROCm 6.2.0 or newer and re-run this script."
  fi
  if version_ge "$ver" "6.2.0"; then
    echo "Detected ROCm version: $ver (OK)"
  else
    error_exit "Detected ROCm version $ver is older than required 6.2.0. Please upgrade ROCm and re-run."
  fi
}



# Parse command line arguments
parse_args "$@"

# Update base URL to reflect chosen branch
GITHUB_RAW_BASE_URL="https://raw.githubusercontent.com/crusoecloud/crusoe-telemetry-agent/${GITHUB_BRANCH}"

# --- Main Script ---

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
  apt-get update && apt-get install -y wget || error_exit "Failed to install wget."
fi

status "Create telemetry agent target directory."
if ! dir_exists "$CRUSOE_TELEMETRY_AGENT_DIR"; then
  mkdir -p "$CRUSOE_TELEMETRY_AGENT_DIR"
fi

# Validate ROCm installation and version
ensure_rocm_6_2_or_newer

# Download Vector config for AMD GPU VM (scrapes AMD exporter)
status "Download AMD GPU Vector config."
wget -q -O "$CRUSOE_TELEMETRY_AGENT_DIR/vector.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_VECTOR_CONFIG_AMD_GPU_VM" || error_exit "Failed to download $REMOTE_VECTOR_CONFIG_AMD_GPU_VM"

status "Download Vector docker-compose file."
wget -q -O "$CRUSOE_TELEMETRY_AGENT_DIR/docker-compose-vector.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_DOCKER_COMPOSE_VECTOR" || error_exit "Failed to download $REMOTE_DOCKER_COMPOSE_VECTOR"

# Download AMD Exporter docker-compose and systemd unit, then enable/start service
status "Prepare AMD metrics config directory."
mkdir -p "$CRUSOE_TELEMETRY_AGENT_DIR/config" || error_exit "Failed to create $CRUSOE_TELEMETRY_AGENT_DIR/config"
status "Download AMD metrics config.json."
wget -q -O "$CRUSOE_TELEMETRY_AGENT_DIR/config/config.json" "$GITHUB_RAW_BASE_URL/$REMOTE_AMD_METRICS_CONFIG" || error_exit "Failed to download $REMOTE_AMD_METRICS_CONFIG"

status "Download AMD Exporter docker-compose file."
wget -q -O "$CRUSOE_TELEMETRY_AGENT_DIR/docker-compose-amd-exporter.yaml" "$GITHUB_RAW_BASE_URL/$REMOTE_DOCKER_COMPOSE_AMD_EXPORTER" || error_exit "Failed to download $REMOTE_DOCKER_COMPOSE_AMD_EXPORTER"

status "Install crusoe-amd-exporter systemd unit."
wget -q -O "$SYSTEMCTL_DIR/crusoe-amd-exporter.service" "$GITHUB_RAW_BASE_URL/$REMOTE_CRUSOE_AMD_EXPORTER_SERVICE" || error_exit "Failed to download $REMOTE_CRUSOE_AMD_EXPORTER_SERVICE"

status "Enable and start systemd services for crusoe-amd-exporter."
echo "systemctl daemon-reload"
systemctl daemon-reload
echo "systemctl enable crusoe-amd-exporter.service"
systemctl enable crusoe-amd-exporter.service
echo "systemctl start crusoe-amd-exporter.service"
systemctl start crusoe-amd-exporter.service

status "Fetching crusoe auth token."
if [[ -z "$CRUSOE_AUTH_TOKEN" ]]; then
  echo "Command: crusoe monitoring tokens create"
  echo "Please enter the crusoe monitoring token:"
  read -s CRUSOE_AUTH_TOKEN # -s for silent input (no echo)
  echo "" # Add a newline after the silent input for better readability

  if [ "${#CRUSOE_AUTH_TOKEN}" -ne $CRUSOE_AUTH_TOKEN_LENGTH ]; then
    echo "CRUSOE_AUTH_TOKEN should be $CRUSOE_AUTH_TOKEN_LENGTH characters long."
    echo "Use Crusoe CLI to generate a new token:"
    echo "Command: crusoe monitoring tokens create"
    error_exit "CRUSOE_AUTH_TOKEN is invalid. "
  fi
fi

status "Creating .env file with CRUSOE_AUTH_TOKEN and VM_ID."
cat <<EOF > "$ENV_FILE"
CRUSOE_AUTH_TOKEN='${CRUSOE_AUTH_TOKEN}'
VM_ID='${CRUSOE_VM_ID}'
AMD_EXPORTER_PORT='${AMD_EXPORTER_PORT:-5000}'
EOF

echo ".env file created at $ENV_FILE"

status "Download crusoe-telemetry-agent.service."
wget -q -O "$SYSTEMCTL_DIR/crusoe-telemetry-agent.service" "$GITHUB_RAW_BASE_URL/$REMOTE_CRUSOE_TELEMETRY_SERVICE" || error_exit "Failed to download $REMOTE_CRUSOE_TELEMETRY_SERVICE"

status "Download crusoe_auth_token_refresh.sh and make it executable command."
wget -q -O "$CRUSOE_TELEMETRY_AGENT_DIR/crusoe_auth_token_refresh.sh" "$GITHUB_RAW_BASE_URL/crusoe_auth_token_refresh.sh" || error_exit "Failed to download crusoe_auth_token_refresh.sh"
chmod +x "$CRUSOE_TELEMETRY_AGENT_DIR/crusoe_auth_token_refresh.sh"
# Create a symbolic link from /usr/bin to the actual script location.
ln -sf "$CRUSOE_TELEMETRY_AGENT_DIR/crusoe_auth_token_refresh.sh" "$CRUSOE_AUTH_TOKEN_REFRESH_ALIAS_PATH"

status "Enable and start systemd services for crusoe-telemetry-agent."
echo "systemctl daemon-reload"
systemctl daemon-reload
echo "systemctl enable crusoe-telemetry-agent.service"
systemctl enable crusoe-telemetry-agent.service
echo "systemctl start crusoe-telemetry-agent.service"
systemctl start crusoe-telemetry-agent

status "Setup Complete!"
echo "Check status of crusoe-telemetry-agent service: 'sudo systemctl status crusoe-telemetry-agent.service'"
echo "Setup finished successfully!"
