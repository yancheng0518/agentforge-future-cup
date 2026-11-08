#!/usr/bin/env bash
# Run only in the dedicated AgentForge WSL distribution, as root.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo 'Run as root in AgentForge WSL.' >&2; exit 1; fi
. /etc/os-release
if [ "$ID" != ubuntu ] || [ "$VERSION_ID" != 22.04 ]; then echo 'Expected dedicated Ubuntu 22.04.' >&2; exit 1; fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl --fail --silent --show-error --location https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: jammy
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
if [ "$(ps -p 1 -o comm=)" = systemd ]; then
  systemctl enable --now docker
else
  service docker start
fi
docker version
docker info --format 'os={{.OSType}} driver={{.Driver}} kernel={{.KernelVersion}} cgroup={{.CgroupVersion}}'
