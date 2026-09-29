#!/usr/bin/env bash
# One-time setup for an Oracle Cloud (OCI) Ubuntu 22.04/24.04 instance: Docker + open ports 80/443 in the host firewall.
# Run on the instance:  bash deploy/oracle_setup.sh   (then log out and back in so the docker group applies)
set -euo pipefail
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl git rsync netfilter-persistent
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER"
# OCI Ubuntu images ship iptables rules that reject everything except SSH; allow HTTP/HTTPS before that REJECT rule.
for port in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null \
    || sudo iptables -I INPUT 6 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
done
sudo netfilter-persistent save
echo "Done. Log out and back in, then: cd ~/sc_memory_extension && docker compose up -d --build"
