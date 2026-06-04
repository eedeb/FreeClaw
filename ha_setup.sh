#!/bin/bash

# ─────────────────────────────────────────────
#  FreeClaw — Home Assistant Setup
#  github.com/eedeb/FreeClaw
# ─────────────────────────────────────────────

RESET="\033[0m"
BOLD="\033[1m"
DIM="\033[2m"

LIME="\033[38;5;154m"
WHITE="\033[0;97m"
GRAY="\033[0;90m"
YELLOW="\033[0;33m"

BG_DARK="\033[48;5;234m"

# ── Helpers ──────────────────────────────────

info() {
    echo -e "     ${GRAY}→${RESET}  $1"
}

success() {
    echo -e "     ${LIME}✓${RESET}  $1"
}

divider() {
    echo -e "   ${DIM}${GRAY}────────────────────────────────────────────────────${RESET}"
}

section_gap() {
    echo ""
}

# ── Header ───────────────────────────────────

echo ""
echo -e "   ${LIME}${BOLD}FreeClaw${RESET} ${GRAY}·${RESET} ${BOLD}${WHITE}Home Assistant Setup${RESET}"
echo ""
divider
section_gap

echo -e "     ${GRAY}This connects FreeClaw to your Home Assistant instance,${RESET}"
echo -e "     ${GRAY}enabling smart home control and Alexa announcements.${RESET}"
section_gap
echo -e "     ${GRAY}You'll need a Long-Lived Access Token from:${RESET}"
echo -e "     ${LIME}  HA → Profile → Long-Lived Access Tokens${RESET}"
section_gap
divider
section_gap

# ── Inputs ───────────────────────────────────

read -p "$(echo -e "     ${LIME}?${RESET}  Home Assistant IP address: ")" url < /dev/tty
printf 'HA_URL=%s\n' "$url" >> .env
success "IP saved to .env"

section_gap

read -p "$(echo -e "     ${LIME}?${RESET}  Home Assistant API token: ")" ha_token < /dev/tty
printf 'HA_TOKEN=%s\n' "$ha_token" >> .env
success "Token saved to .env"

section_gap
divider
section_gap

# ── Restart ──────────────────────────────────

info "Restarting FreeClaw to apply changes..."
sudo systemctl restart FreeClaw.service
success "FreeClaw restarted"

section_gap
echo -e "   ${LIME}${BOLD}Home Assistant integration enabled!${RESET}"
echo -e "   ${GRAY}FreeClaw can now control your smart home devices.${RESET}"
echo ""
divider
echo ""