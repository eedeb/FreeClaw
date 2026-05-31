#!/bin/bash
read -p "Enter the ip address for your home assistant: " url < /dev/tty
echo "HA_URL=$url" > .env
echo "IP saved to .env"

read -p "Enter the API token for your home assistant: " api_key < /dev/tty
echo "HA_TOKEN=$api_key" > .env
echo "Token saved to .env"