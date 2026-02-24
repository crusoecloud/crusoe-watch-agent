FROM nvcr.io/nvidia/cuda:12.8.0-base-ubuntu24.04

WORKDIR /app

# Install Python and system dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    pciutils \
    usbutils \
    dmidecode \
    kmod \
    util-linux \
    procps \
    iproute2 \
    gzip \
    wget \
    gnupg \
    ca-certificates \
    acpica-tools \
    && rm -rf /var/lib/apt/lists/*

# Install nvidia-utils from NVIDIA repository
# This provides nvidia-bug-report.sh, nvidia-smi, and nvidia-debugdump
# Try to match GB200 driver version (580.95.05) first, then fall back
RUN apt-get update \
    && echo "Searching for available nvidia-utils packages:" \
    && apt-cache search nvidia-utils \
    && echo "Installing nvidia-utils..." \
    && (apt-get install -y --no-install-recommends nvidia-utils-550 || \
        apt-get install -y --no-install-recommends nvidia-utils-545 || \
        apt-get install -y --no-install-recommends nvidia-utils-535 || \
        apt-get install -y --no-install-recommends nvidia-utils) \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# Copy application code
COPY app/ .

# Create logs directory
RUN mkdir -p /logs && \
    chmod -R 755 /logs

# Run the application
CMD ["python3", "log_collector_app.py"]