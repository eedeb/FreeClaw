#!/bin/bash
git clone https://github.com/eedeb/FreeClaw
cd FreeClaw
python3 -m venv venv
source venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install classy-ai
pip install sentence_transformers --no-deps
pip install flask
mkdir Flask/templates
mkdir Flask/static
