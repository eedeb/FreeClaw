#!/bin/bash
read -p "Enter the ip address for your home assistant: " api_key
echo "HA_URL=$api_key" > .env
echo "IP saved to .env"

read -p "Enter the API token for your home assistant: " api_key
echo "HA_TOKEN=$api_key" > .env
echo "Token saved to .env"