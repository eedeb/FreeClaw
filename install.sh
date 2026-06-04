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

read -p "Enter your Groq API key: " api_key < /dev/tty
printf 'API_KEY=%s\n' "$api_key" > .env
chmod 600 .env
echo 'API key saved.'

read -s -p "Enter a password for the FreeClaw web UI: " fc_password < /dev/tty
echo ""
read -s -p "Confirm password: " fc_password_confirm < /dev/tty
echo ""

if [[ "$fc_password" != "$fc_password_confirm" ]]; then
    echo "Error: Passwords do not match. Please run the installer again."
    exit 1
fi

# Generate a random secret key for session signing
secret_key=$(python3 -c "import secrets; print(secrets.token_hex(32))")

printf 'FC_PASSWORD=%s\n' "$fc_password" >> .env
printf 'SECRET_KEY=%s\n' "$secret_key" >> .env
echo 'Password saved.'

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

sudo systemctl enable FreeClawAPI.service

chmod +x ha_setup.sh
chmod +x update.sh

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
echo "http://$IP:6767"
echo "======================================="