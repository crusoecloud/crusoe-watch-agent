#!/bin/bash
#
# AMD GPU Bug Report Collection Script
# Mimics nvidia-bug-report.sh for AMD GPUs with ROCm stack
#
# Usage: amd-bug-report.sh --output-file <path>
#

set -e

OUTPUT_FILE=""
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --output-file)
            OUTPUT_FILE="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 --output-file <path>"
            exit 1
            ;;
    esac
done

if [ -z "$OUTPUT_FILE" ]; then
    echo "Error: --output-file is required"
    echo "Usage: $0 --output-file <path>"
    exit 1
fi

LOG_FILE="${TEMP_DIR}/amd-bug-report.log"

echo "====================================" | tee -a "$LOG_FILE"
echo "AMD GPU Bug Report" | tee -a "$LOG_FILE"
echo "Generated: $(date)" | tee -a "$LOG_FILE"
echo "Hostname: $(hostname)" | tee -a "$LOG_FILE"
echo "====================================" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# Helper function to run command and capture output
run_command() {
    local section="$1"
    local cmd="$2"

    echo "### $section" | tee -a "$LOG_FILE"
    echo "Command: $cmd" | tee -a "$LOG_FILE"
    echo "---" | tee -a "$LOG_FILE"

    if eval "$cmd" >> "$LOG_FILE" 2>&1; then
        echo "" | tee -a "$LOG_FILE"
    else
        echo "Command failed or not available" | tee -a "$LOG_FILE"
        echo "" | tee -a "$LOG_FILE"
    fi
}

echo "=====================================" | tee -a "$LOG_FILE"
echo "BASIC INFO" | tee -a "$LOG_FILE"
echo "=====================================" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

run_command "OS Distribution and Version" "lsb_release -sd"
run_command "CPU Model and Architecture" "lshw -c cpu"
run_command "System Uptime and Load" "uptime"
run_command "System Memory" "free -h"
run_command "GPU Models and UUIDs" "amd-smi list"
run_command "ROCm and SMI Versions" "amd-smi version"

echo "" | tee -a "$LOG_FILE"
echo "=====================================" | tee -a "$LOG_FILE"
echo "HARDWARE & INSTALLATION" | tee -a "$LOG_FILE"
echo "=====================================" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

run_command "AMDGPU Driver Status (DKMS)" "dkms status"
run_command "AMDGPU Kernel Module" "lsmod | grep amdgpu"
run_command "AMDGPU Module Info" "modinfo amdgpu"
run_command "PCIe Bus Speeds and Device IDs" "lspci -vnn"
run_command "Linux Kernel Version" "uname -a"

echo "" | tee -a "$LOG_FILE"
echo "=====================================" | tee -a "$LOG_FILE"
echo "COMPUTE STACK" | tee -a "$LOG_FILE"
echo "=====================================" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

run_command "VBIOS and Power Limits" "amd-smi static"
run_command "ROCm GPU Visibility (rocminfo)" "rocminfo"
run_command "XGMI/P2P Interconnect Topology" "amd-smi topology"
run_command "ROCm Environment Variables" "env | grep -E 'ROCM|HSA|HIP'"

echo "" | tee -a "$LOG_FILE"
echo "=====================================" | tee -a "$LOG_FILE"
echo "PERFORMANCE & METRICS" | tee -a "$LOG_FILE"
echo "=====================================" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

run_command "GPU Processes" "amd-smi process"
run_command "GPU Memory Usage" "amd-smi metric -m memory_usage"
run_command "GPU Temperature and Power" "amd-smi metric -m temperature,power"
run_command "GPU Utilization" "amd-smi metric -m utilization"
run_command "GPU Clock Frequencies" "amd-smi metric -m clock"

echo "" | tee -a "$LOG_FILE"
echo "=====================================" | tee -a "$LOG_FILE"
echo "HEALTH & RELIABILITY" | tee -a "$LOG_FILE"
echo "=====================================" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

run_command "Hardware VRAM Defects" "amd-smi bad-pages"
run_command "GPU Error Counts" "amd-smi metric -m ecc"
run_command "Firmware Versions" "amd-smi firmware"
run_command "Recent GPU Errors from dmesg" "dmesg | grep -i -E 'amdgpu|amd-smi|rocm' | tail -n 100"

echo "" | tee -a "$LOG_FILE"
echo "=====================================" | tee -a "$LOG_FILE"
echo "Bug report collection complete" | tee -a "$LOG_FILE"
echo "=====================================" | tee -a "$LOG_FILE"

# Compress the log file
gzip -c "$LOG_FILE" > "${OUTPUT_FILE}.gz"

echo "Bug report saved to: ${OUTPUT_FILE}.gz"
echo "${OUTPUT_FILE}.gz"
