# Dyktator-GUI

Nagrywanie mowy z mikrofonu i **w pełni lokalna** transkrypcja (faster-whisper).
Jeden przycisk: klik → nagrywa, drugi klik → kończy i przepisuje tekst w oknie.

## Uruchomienie

```bash
./run.sh
```

Środowisko `venv/` jest dołączone (PyQt6 + faster-whisper). Po przeniesieniu
projektu na inny komputer wystarczy odtworzyć je poleceniem:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## Jak to działa

- Nagrywanie odbywa się przez PipeWire (`pw-record`, format 48 kHz / 16-bit / stereo),
  więc aplikacja widzi wszystkie źródła dźwięku systemowego i podąża za
  domyślnym mikrofonem ustawionym w KDE.
- Nagrania trafiają do `nagrania/RRRR-MM-DD_GG-MM-SS.wav`.
- Transkrypcja działa na CPU (int8) modelem faster-whisper; pierwszy start
  wybranego modelu pobiera go z Hugging Face do `~/.cache/huggingface/`,
  potem działa bez internetu.

## Opcje konfiguracji (w GUI)

| Opcja | Znaczenie |
|---|---|
| Model Whispera | `tiny` → `large-v3`; większy = dokładniejszy, ale wolniejszy |
| Język mowy | wymuszenie języka (np. polski) poprawia dokładność |
| Mikrofon | dowolne źródło z PipeWire lub domyślne systemowe |
| Precyzja | `int8` (szybko) / `float32` (dokładniej) |
| Kopiuj do schowka | wynik automatycznie ląduje w schowku |
| Zapisuj transkrypcje | dodatkowy plik `.txt` w `zapisy/` |

Ustawienia zapisują się w `config.json` obok skryptu.

## Struktura

```
Dyktator-GUI/
├── main.py           # cała aplikacja (PyQt6)
├── run.sh            # launcher
├── requirements.txt
├── config.json       # tworzony przy pierwszym uruchomieniu
├── nagrania/         # pliki WAV z nagrań
└── zapisy/           # transkrypcje .txt (jeśli włączone)
```
