#!/usr/bin/env bash
# Install / update the systemd services on the Linux server (run from ~/app):
#   bash deploy/install.sh crouch-mm ml-copy ml-join crouch-v2
# Starts the server and the recorder, and enables + starts one pmtool-bot@<id> per bot id given.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/recordings data/bots
.venv/bin/pip install -q -r requirements.txt
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pmtool-server pmtool-recorder
for b in "$@"; do
  mkdir -p "data/bots/$b"
  sudo systemctl enable --now "pmtool-bot@$b"
done
systemctl --no-pager --plain list-units 'pmtool-*' | head -20
