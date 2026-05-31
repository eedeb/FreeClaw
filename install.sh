#!/bin/bash
echo 'cloning repository...'
git clone https://github.com/eedeb/FreeClaw
cd FreeClaw
echo 'setting up virtual environment...'
python3 -m venv venv
source venv/bin/activate
echo 'installing dependencies...'

pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install classy-ai
pip install sentence_transformers --no-deps

pip install flask
pip install groq
pip install ddgs
pip install beautifulsoup4
pip install json-repair
pip install python-dotenv
pip install uvicorn

echo 'settig up directories...'
mkdir Flask/static

read -p "Enter your API key: " api_key
echo "API_KEY=$api_key" > .env
echo "API key saved to .env"


echo 'settting up systemctl...'

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
read -p "Do you want to set up Home Assistant integration? (y/n) " answer
if [[ "$answer" == "y" ]]; then
    ./ha_setup.sh
else
    echo "Skipping Home Assistant setup."
fi

