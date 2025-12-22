FROM ubuntu:22.04

# Install dependencies
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Download and install kubectl
RUN KUBE_LATEST=$(curl -L -s https://dl.k8s.io/release/stable.txt) && \
    curl -LO "https://dl.k8s.io/release/${KUBE_LATEST}/bin/linux/amd64/kubectl" && \
    install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && \
    rm kubectl

# Add Crusoe repository and install crusoe CLI
RUN echo "deb [trusted=yes] https://apt.fury.io/crusoe/ * *" > /etc/apt/sources.list.d/fury.list && \
    apt-get update && \
    apt-get install -y crusoe && \
    rm -rf /var/lib/apt/lists/*

# Create the entrypoint script
RUN mkdir -p /scripts
COPY entrypoint.sh /scripts/entrypoint.sh
RUN chmod +x /scripts/entrypoint.sh

ENTRYPOINT ["/scripts/entrypoint.sh"]
