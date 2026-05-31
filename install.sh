#!/bin/bash
set -e

# Verify required tools
for cmd in git python3 sudo; do
    command -v "$cmd" &>/dev/null || { echo "Error: $cmd is required but not found."; exit 1; }
done

echo 'Cloning repository...'
git clone https://github.com/eedeb/FreeClaw
cd FreeClaw || exit 1

echo 'Setting up virtual environment...'
python3 -m venv venv

echo 'Installing dependencies...'
venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
venv/bin/pip install classy-ai
venv/bin/pip install sentence_transformers --no-deps
venv/bin/pip install flask groq ddgs beautifulsoup4 json-repair python-dotenv uvicorn
venv/bin/pip install fastapi

echo 'Setting up directories...'
mkdir -p Flask/static

read -p "Enter your API key: " api_key < /dev/tty
printf 'API_KEY=%s\n' "$api_key" > .env
chmod 600 .env
echo 'API key saved to .env'

echo 'Setting up systemctl...'
INSTALL_DIR=$(pwd)
USER_NAME=$(whoami)

sudo tee /etc/systemd/system/FreeClaw.service > /dev/null <<EOF
[Unit]
Description=FreeClaw Flask Application
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 -m Flask.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/FreeClawAPI.service > /dev/null <<EOF
[Unit]
Description=FreeClaw API service
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/uvicorn src.api:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload

sudo systemctl enable FreeClaw.service
sudo systemctl start FreeClaw.service

chmod +x ha_setup.sh
if [[ -x ha_setup.sh ]]; then
    read -p "Do you want to set up Home Assistant integration? (y/n) " answer < /dev/tty
    if [[ "$answer" == "y" ]]; then
        ./ha_setup.sh
    else
        echo "Skipping Home Assistant setup."
    fi
else
    echo "Warning: ha_setup.sh not found or not executable, skipping Home Assistant setup."
fi

IP=$(hostname -I | awk '{print $1}')

echo ""
echo "======================================="
echo "Installation complete!"
echo ""
echo "Open the following URL in your browser:"
echo "http://$IP:8080"
echo "======================================="