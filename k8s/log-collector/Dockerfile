# --- Build stage: install Python dependencies ---
FROM nvcr.io/nvidia/cuda:12.8.0-base-ubuntu24.04 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# --- Runtime stage ---
FROM nvcr.io/nvidia/cuda:12.8.0-base-ubuntu24.04

WORKDIR /app

# Install Python, system tools needed by nvidia-bug-report.sh, and nvidia-utils
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    pciutils \
    usbutils \
    dmidecode \
    kmod \
    util-linux \
    procps \
    iproute2 \
    gzip \
    ca-certificates \
    acpica-tools \
    && echo "Installing nvidia-utils..." \
    && (apt-get install -y --no-install-recommends nvidia-utils-550 || \
        apt-get install -y --no-install-recommends nvidia-utils-545 || \
        apt-get install -y --no-install-recommends nvidia-utils-535 || \
        apt-get install -y --no-install-recommends nvidia-utils) \
    && dpkg --force-depends -r libnvidia-compute-550 cuda-compat-12-8 2>/dev/null || true \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /usr/lib/python3 /usr/lib/python3
COPY --from=builder /usr/local/lib/python3.12/dist-packages /usr/local/lib/python3.12/dist-packages

# Copy application code
COPY app/ .

# Create logs directory
RUN mkdir -p /logs && \
    chmod -R 755 /logs

# Run the application
CMD ["python3", "log_collector_app.py"]