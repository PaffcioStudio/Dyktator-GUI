#!/usr/bin/env bash
# Uruchamia Dyktator-GUI z dołączonym środowiskiem wirtualnym (.venv/).
# Brak środowiska? Uruchom najpierw: ./venv.sh
cd "$(dirname "$0")"
exec .venv/bin/python main.py "$@"
