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

echo 'settig up directories...'
mkdir Flask/static

read -p "Enter your API key: " api_key
echo "API_KEY=$api_key" > .env
echo "API key saved to .env"
