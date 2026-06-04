#!/bin/bash
set -e

INSTALL_DIR=$(pwd)

# Make sure we're in the FreeClaw directory
if [[ ! -f "$INSTALL_DIR/.env" || ! -d "$INSTALL_DIR/src" ]]; then
    echo "Error: Run this script from your FreeClaw installation directory."
    exit 1
fi

echo "Fetching latest changes from GitHub..."
git fetch origin main

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [[ "$LOCAL" == "$REMOTE" ]]; then
    echo "Already up to date. Nothing to do."
    exit 0
fi

echo "Update available. Applying..."

# Stop the Flask service while we update
echo "Stopping FreeClaw..."
sudo systemctl stop FreeClaw.service

# Pull only src/ and Flask/ — overwrite repo files, leave user files alone
git checkout origin/main -- src/
git checkout origin/main -- Flask/templates/
git checkout origin/main -- Flask/main.py

# Restore Flask/static/ in case git checkout wiped it (user-uploaded files live here)
mkdir -p Flask/static

echo "Updating dependencies in case anything changed..."
venv/bin/pip install -q -r requirements.txt 2>/dev/null || true

echo "Restarting FreeClaw..."
sudo systemctl start FreeClaw.service

echo ""
echo "======================================="
echo "FreeClaw updated successfully!"
git log --oneline -5
echo "======================================="