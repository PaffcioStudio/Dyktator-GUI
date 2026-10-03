#!/usr/bin/env bash
# Uruchamia Dyktator-GUI z dołączonym środowiskiem wirtualnym.
cd "$(dirname "$0")"
exec ./venv/bin/python main.py "$@"
