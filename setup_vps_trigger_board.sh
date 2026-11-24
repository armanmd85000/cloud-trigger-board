#!/bin/bash
# ==============================================================================
# UPCLOUD VPS TRIGGER BOARD DEPLOYMENT SCRIPT
# Deploy on Ubuntu 22.04 / 24.04 / 26.04 (Node 1: 209.151.147.187)
# ==============================================================================

set -e

echo "[*] Updating apt packages..."
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git curl

echo "[*] Creating trigger board directory..."
mkdir -p /opt/cloud-trigger-board
cd /opt/cloud-trigger-board

echo "[*] Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install fastapi uvicorn requests pycryptodome pydantic

echo "[*] Creating systemd service for Cloud Trigger Board..."
cat << 'EOF' > /etc/systemd/system/triggerboard.service
[Unit]
Description=Cloud Trigger Board - MEGA to GDrive Cloner
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/cloud-trigger-board
ExecStart=/opt/cloud-trigger-board/venv/bin/python /opt/cloud-trigger-board/cloud_trigger_board.py --port 8899
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[*] Reloading and starting systemd service..."
systemctl daemon-reload
systemctl enable triggerboard
systemctl restart triggerboard

echo "=============================================================================="
echo "  ⚡ TRIGGER BOARD DEPLOYMENT COMPLETE!"
echo "  Web Dashboard: http://209.151.147.187:8899"
echo "  API Docs:      http://209.151.147.187:8899/docs"
echo "=============================================================================="
