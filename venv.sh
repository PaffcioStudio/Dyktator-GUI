#!/usr/bin/env bash
# venv.sh - tworzy środowisko .venv/, instaluje zależności z requirements.txt
# i pobiera model Whispera do cache (~/.cache/huggingface), jeśli trzeba.
# Skrypt można uruchamiać wielokrotnie - pomija to, co już jest zrobione.
set -euo pipefail
cd "$(dirname "$0")"

# --- 1. środowisko wirtualne ---
if [ ! -x .venv/bin/python ]; then
    echo "==> Tworzę środowisko .venv/ ..."
    python3 -m venv .venv
else
    echo "==> .venv/ już istnieje - pomijam tworzenie."
fi

# --- 2. zależności ---
echo "==> Instaluję zależności z requirements.txt ..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# --- 3. model AI (ten wybrany w config.json, domyślnie "small") ---
MODEL=$(.venv/bin/python - <<'EOF'
import json, pathlib
model = "small"
cfg = pathlib.Path("config.json")
if cfg.exists():
    try:
        model = json.loads(cfg.read_text(encoding="utf-8")).get("model", model)
    except ValueError:
        pass
print(model)
EOF
)
echo "==> Pobieram/weryfikuję model Whispera „$MODEL” (cache: ~/.cache/huggingface) ..."
.venv/bin/python - "$MODEL" <<'EOF'
import sys
from faster_whisper import WhisperModel
WhisperModel(sys.argv[1], device="cpu", compute_type="int8")
print("    Model gotowy.")
EOF

echo
echo "✅ Środowisko gotowe. Aplikację uruchamiasz przez: ./run.sh"
