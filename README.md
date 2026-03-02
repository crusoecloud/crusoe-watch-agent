# Crusoe Watch Agent
crusoe-watch-agent is a vector.dev based agent for collecting telemetry data from Crusoe Cloud resources.

## Installation
Choose one of the following methods to install crusoe-watch-agent on your VM.

### Simple Download
Best for a quick, one-time installation.

**For NVIDIA GPUs or CPU-only VMs:**
```
wget https://raw.githubusercontent.com/crusoecloud/crusoe-watch-agent/refs/heads/main/vm/crusoe_watch_agent.sh
chmod +x crusoe_watch_agent.sh
sudo ./crusoe_watch_agent.sh install
```

**For AMD GPUs:**
```
wget https://raw.githubusercontent.com/crusoecloud/crusoe-watch-agent/refs/heads/main/vm/crusoe_watch_agent_amd.sh
chmod +x crusoe_watch_agent_amd.sh
sudo ./crusoe_watch_agent_amd.sh install
```

> **Note:** For AMD GPU VMs, use `crusoe_watch_agent_amd.sh` script. All commands (`install`, `uninstall`, `upgrade`, `refresh-token`, `help`) work the same way for both scripts.

### Symlink Installation
Sets up the script as a global command for easier upgrades and management.

**For NVIDIA GPUs or CPU-only VMs:**
```
wget https://raw.githubusercontent.com/crusoecloud/crusoe-watch-agent/refs/heads/main/vm/crusoe_watch_agent.sh
sudo mkdir -p /etc/crusoe/crusoe_watch_agent
sudo mv crusoe_watch_agent.sh /etc/crusoe/crusoe_watch_agent/.
sudo chmod +x /etc/crusoe/crusoe_watch_agent/crusoe_watch_agent.sh
sudo ln -sf "/etc/crusoe/crusoe_watch_agent/crusoe_watch_agent.sh" "/usr/bin/crusoe-watch-agent"
```

**For AMD GPUs:**
```
wget https://raw.githubusercontent.com/crusoecloud/crusoe-watch-agent/refs/heads/main/vm/crusoe_watch_agent_amd.sh
sudo mkdir -p /etc/crusoe/crusoe_watch_agent
sudo mv crusoe_watch_agent_amd.sh /etc/crusoe/crusoe_watch_agent/.
sudo chmod +x /etc/crusoe/crusoe_watch_agent/crusoe_watch_agent_amd.sh
sudo ln -sf "/etc/crusoe/crusoe_watch_agent/crusoe_watch_agent_amd.sh" "/usr/bin/crusoe-watch-agent"
```

### Installation Modes

By default, the agent uses **Docker** to run Vector and DCGM Exporter as containers. If you prefer to install them as native binaries (no Docker dependency), pass the `--no-docker` flag:

```
sudo ./crusoe_watch_agent.sh install --no-docker
```

In native mode:
- **Vector** is installed via APT (`apt-get install vector`)
- **DCGM Exporter** is built from source (requires Go, installed automatically if missing)
- Services run as native systemd units instead of Docker containers

The chosen mode is persisted so that `upgrade`, `uninstall`, and `refresh-token` automatically use the same mode without needing to re-specify the flag.

> **Note:** The `--no-docker` flag is currently supported for the NVIDIA/CPU script (`crusoe_watch_agent.sh`) only.

## Configuration and Startup
Regardless of the installation method chosen above, follow these steps to authenticate and start the agent.

### Monitoring Token Injection
Generate a token via the Crusoe CLI.
```
crusoe monitoring tokens create
```
Optional: Save the token to the secrets directory to bypass manual prompts.
```
sudo mkdir -p /etc/crusoe/secrets
sudo tee /etc/crusoe/secrets/.monitoring-token <<'EOF'
CRUSOE_AUTH_TOKEN='<paste-your-monitoring-token-here>'
EOF
sudo chmod 600 /etc/crusoe/secrets/.monitoring-token
```

### Agent Installation
If you used the Simple Download method, run `sudo ./crusoe_watch_agent.sh install`. Otherwise, run:
```
sudo crusoe-watch-agent install
```

> **Note for Slurm images:**
> If you have a pre-installed use `--replace-dcgm-exporter` to replace it with the Crusoe version for full metrics collection. 
> Optional `SERVICE_NAME` defaults to `dcgm-exporter`:
> ```bash
> sudo crusoe-watch-agent install --replace-dcgm-exporter [SERVICE_NAME]
> ```

### Verification

**Docker mode (default):**

Once the service starts, it will download and launch two Docker containers: `crusoe-dcgm-exporter` and `crusoe-vector`. Check the Docker logs to verify there are no errors:
```
docker container logs crusoe-vector
```

**Native mode (`--no-docker`):**

Check the systemd service status and logs:
```
sudo systemctl status crusoe-watch-agent.service
sudo journalctl -u crusoe-watch-agent.service -f
```

For GPU VMs, also verify the DCGM exporter:
```
sudo systemctl status crusoe-dcgm-exporter.service
curl localhost:9400/metrics
```


## Maintenance
```
# Upgrade to latest version (automatically uses the same mode as the original install)
sudo ./crusoe_watch_agent.sh upgrade

# Refresh auth token
sudo ./crusoe_watch_agent.sh refresh-token

# Uninstall (automatically cleans up based on the original install mode)
sudo ./crusoe_watch_agent.sh uninstall

# View help
sudo ./crusoe_watch_agent.sh help
```