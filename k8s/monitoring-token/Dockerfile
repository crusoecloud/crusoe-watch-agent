FROM ubuntu:22.04

# Install dependencies.
#
# The trust store is settled here, in the first layer, so every curl and apt
# call below is already protected. The crusoe CLI installed further down is a Go
# binary that authenticates to api.crusoecloud.com with CRUSOE_ACCESS_KEY using
# this image's OS trust store and consults no revocation list, so the roots
# distrusted after the eMudhra intermediate CA key compromise (CVE-2026-113470)
# have to be gone before it runs.
#
# Upgrading ca-certificates is the real fix; the explicit distrust covers a base
# image whose package index has not picked it up yet, and is a no-op once it
# has. The two assertions fail the build rather than shipping an image that
# still trusts them.
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    && sed -i -E 's,^(mozilla/(emSign_ECC_Root_CA_-_G3|Comodo_AAA_Services_root)\.crt)$,!\1,' \
        /etc/ca-certificates.conf \
    && update-ca-certificates --fresh \
    && ! test -e /etc/ssl/certs/emSign_ECC_Root_CA_-_G3.pem \
    && ! test -e /etc/ssl/certs/Comodo_AAA_Services_root.pem \
    && rm -rf /var/lib/apt/lists/*

# Download and install kubectl
RUN KUBE_LATEST=$(curl -L -s https://dl.k8s.io/release/stable.txt) && \
    ARCH=$(dpkg --print-architecture) && \
    curl -LO "https://dl.k8s.io/release/${KUBE_LATEST}/bin/linux/${ARCH}/kubectl" && \
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
