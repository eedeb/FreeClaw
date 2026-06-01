#!/bin/bash
read -p "Enter the IP address for your Home Assistant: " url < /dev/tty
printf 'HA_URL=%s\n' "$url" >> .env
echo "IP saved to .env"

read -p "Enter the API token for your Home Assistant: " ha_token < /dev/tty
printf 'HA_TOKEN=%s\n' "$ha_token" >> .env
echo "Token saved to .env"

sudo systemctl restart FreeClaw.service