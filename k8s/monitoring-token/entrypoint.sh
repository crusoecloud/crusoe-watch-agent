#!/bin/bash
set -ex

# --- CHECK AND EARLY EXIT ---
# Check if the secret already exists.
SECRET_NAME="${CRUSOE_MONITORING_TOKEN_SECRET_NAME}"
NAMESPACE="${CRUSOE_NAMESPACE}"

if kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo "Secret $SECRET_NAME already exists. Exiting job successfully."
  exit 0
fi

# Continue with installation and secret creation steps only if the secret is missing
echo "Secret $SECRET_NAME does not exist. Proceeding with token creation."

# Set up Crusoe credentials (loaded from envFrom secret)
export CRUSOE_ACCESS_KEY_ID="${CRUSOE_ACCESS_KEY}"

# Verify Crusoe CLI is working
echo "Verifying Crusoe CLI authentication..."
crusoe whoami

# Create monitoring token
export CRUSOE_MONITORING_TOKEN=$(crusoe monitoring tokens create | grep "monitor token:" | awk '{print $3}')

# Create the secret in Kubernetes
kubectl create secret generic "$SECRET_NAME" \
  --from-literal=CRUSOE_MONITORING_TOKEN="${CRUSOE_MONITORING_TOKEN}" \
  -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

echo "Secret $SECRET_NAME created successfully."
