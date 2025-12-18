# Crusoe Watch
Crusoe Watch is a vector.dev based agent for collecting telemetry data from Crusoe Cloud resources.


## Installation
Choose one of the following methods to get the Crusoe Watch manager onto your system.

### Simple Download 
Best for a quick, one-time installation.
```
wget https://raw.githubusercontent.com/crusoecloud/crusoe-watch/refs/heads/main/vm/crusoe_watch_manager.sh 
chmod +x crusoe_watch_manager.sh
sudo ./crusoe_watch_manager.sh install
```

### Symlink Installation
Sets up the script as a global command for easier upgrades and management.
```
wget https://raw.githubusercontent.com/crusoecloud/crusoe-watch/refs/heads/main/vm/crusoe_watch_manager.sh 
sudo mkdir -p /etc/crusoe/crusoe-watch
sudo mv crusoe_watch_manager.sh /etc/crusoe/crusoe-watch/.
sudo chmod +x /etc/crusoe/crusoe-watch/crusoe_watch_manager.sh
sudo ln -sf "/etc/crusoe/crusoe-watch/crusoe_watch_manager.sh" "/usr/bin/crusoe-watch" 
```

**Note for Slurm images:**
If you have a pre-installed dcgm-exporter systemd service, use `--replace-dcgm-exporter` to replace it with the Crusoe version for full metrics collection.
Optional `SERVICE_NAME` defaults to `dcgm-exporter`:
```
sudo crusoe-watch install --replace-dcgm-exporter [SERVICE_NAME]
```

## Configuration and Startup
Regardless of the installation method chosen above, follow these steps to authenticate and start the agent.

### Monitoring Token Injection
Generate a token via the Crusoe CLI.
```
crusoe monitoring tokens create
```
Save the token to the secrets directory to bypass manual prompts.
```
sudo mkdir -p /etc/crusoe/secrets
sudo tee /etc/crusoe/secrets/.monitoring-token <<'EOF'
CRUSOE_AUTH_TOKEN='<paste-your-monitoring-token-here>'
EOF
sudo chmod 600 /etc/crusoe/secrets/.monitoring-token
```

### Verification
Once the service starts, it will download and launch two Docker containers: `crusoe-dcgm-exporter` and `crusoe-vector`.  Check the Docker logs to verify there are no errors:
```
docker container logs crusoe-vector
```
TA-DA!! Crusoe Watch is now successfully pushing metrics.


## Maintenance
```
# Upgrade to latest version
sudo crusoe-watch upgrade

# Refresh auth token
sudo crusoe-watch refresh-token

# Uninstall
sudo crusoe-watch uninstall

# View help
sudo crusoe-watch help
```