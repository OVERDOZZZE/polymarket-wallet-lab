#!/usr/bin/env bash
# Latency test of a fresh Ubuntu 24.04 EC2 box before choosing it (no keys, no orders):
#   bash az_probe.sh [minutes]        (run from the unpacked probe bundle: backend/config.py + scripts/latency_probe.py)
# Labels the result with the AWS zone id (same across accounts, unlike eu-west-2a/b/c):
#   data/latency/probe-london-<zone id>-<utc>.json
set -euo pipefail
cd "$(dirname "$0")"
T=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
md() { curl -s -H "X-aws-ec2-metadata-token: $T" "http://169.254.169.254/latest/meta-data/$1"; }
echo "zone $(md placement/availability-zone) = $(md placement/availability-zone-id), type $(md instance-type)"
sudo apt-get -qq update && sudo DEBIAN_FRONTEND=noninteractive apt-get -qq install -y python3-venv >/dev/null
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip -q install requests 'websockets<16' uvloop python-dotenv
.venv/bin/python -m scripts.latency_probe --site "london-$(md placement/availability-zone-id)" --minutes "${1:-10}"
