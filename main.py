#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dyktator-GUI — nagrywanie mowy z mikrofonu i lokalna transkrypcja (faster-whisper).

Przycisk Start/Stop nagrywa przez PipeWire (pw-record), a po zatrzymaniu
nagranie jest przepisywane lokalnie modelem Whisper. Nic nie wychodzi
do internetu (poza jednorazowym pobraniem wybranego modelu z Hugging Face).
"""

import json
import os
import subprocess
import sys
import time
import traceback
import wave
from array import array
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
NAGRANIA_DIR = APP_DIR / "nagrania"
ZAPISY_DIR = APP_DIR / "zapisy"

MODELE = ["tiny", "base", "small", "medium", "large-v3"]
JEZYKI = {
    "Automatyczny": None,
    "polski": "pl",
    "angielski": "en",
    "niemiecki": "de",
    "ukraiński": "uk",
    "czeski": "cs",
    "hiszpański": "es",
    "francuski": "fr",
}
PRECYZJE = {
    "int8 — szybko (CPU)": "int8",
    "float32 — dokładniej, wolniej": "float32",
}

DOMYSLNA_KONFIG = {
    "model": "small",
    "jezyk": "Automatyczny",
    "mikrofon": "",  # puste = domyślne źródło systemowe
    "precyzja": "int8",
    "kopiuj": True,
    "zapisuj": False,
}


def wczytaj_konfig() -> dict:
    konfig = dict(DOMYSLNA_KONFIG)
    try:
        konfig.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return konfig


def zapisz_konfig(konfig: dict) -> None:
    CONFIG_FILE.write_text(
        json.dumps(konfig, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def lista_zrodel() -> list[tuple[str, str]]:
    """Źródła nagrywania z PipeWire jako pary (nazwa, opis); bez monitorów."""
    env = {**os.environ, "LC_ALL": "C"}
    try:
        out = subprocess.run(
            ["pactl", "list", "sources"],
            capture_output=True, text=True, env=env, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    zrodla = []
    for sekcja in out.split("Source #")[1:]:
        nazwa = opis = None
        for linia in sekcja.splitlines():
            t = linia.strip()
            if t.startswith("Name:"):
                nazwa = t.split(":", 1)[1].strip()
            elif t.startswith("Description:"):
                opis = t.split(":", 1)[1].strip()
        if nazwa and not nazwa.endswith(".monitor"):
            zrodla.append((nazwa, opis or nazwa))
    return zrodla


def _pik(chunk: bytes) -> int:
    """Chwilowa głośność fragmentu PCM s16le jako 0-100."""
    probki = array("h", chunk[: len(chunk) // 2 * 2])
    if not probki:
        return 0
    return min(100, max(abs(p) for p in probki) * 100 // 32768)


class Nagrywarka(QThread):
    """Czyta PCM z pw-record (PipeWire) i po zatrzymaniu zapisuje plik WAV."""

    poziom = pyqtSignal(int)         # głośność 0-100
    zatrzymano = pyqtSignal(object)  # Path do pliku WAV
    blad = pyqtSignal(str)

    def __init__(self, zrodlo: str):
        super().__init__()
        self._zrodlo = zrodlo
        self._proc = None
        self._bufor = bytearray()
        self._aktywny = True

    def run(self):
        cmd = ["pw-record", "--format", "s16", "--rate", "48000", "--channels", "2", "-"]
        if self._zrodlo:
            cmd[1:1] = ["--target", self._zrodlo]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as e:
            self.blad.emit(f"Nie udało się uruchomić pw-record: {e}")
            return

        licznik = 0
        while self._aktywny:
            chunk = self._proc.stdout.read(9600)  # 50 ms stereo 48 kHz
            if not chunk:
                break
            self._bufor += chunk
            licznik += 1
            if licznik % 2 == 0:
                self.poziom.emit(_pik(chunk))

        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc.stderr.close()

        if not self._bufor:
            komunikat = ""
            try:
                komunikat = self._proc.stderr.read().decode(errors="replace")
            except (OSError, ValueError):
                pass
            self.blad.emit(f"Nie doszły żadne dane z mikrofonu. {komunikat.strip()}")
            return

        NAGRANIA_DIR.mkdir(exist_ok=True)
        plik = NAGRANIA_DIR / (datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".wav")
        with wave.open(str(plik), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(bytes(self._bufor))
        self.zatrzymano.emit(plik)

    def zatrzymaj(self):
        self._aktywny = False


_MODELE_CACHE: dict = {}


class Transkrybent(QThread):
    """Przepisuje plik WAV lokalnym modelem faster-whisper."""

    gotowe = pyqtSignal(str)
    blad = pyqtSignal(str)
    postep = pyqtSignal(str)

    def __init__(self, plik: Path, model: str, jezyk: str, precyzja: str):
        super().__init__()
        self.plik = plik
        self.model = model
        self.jezyk = jezyk
        self.precyzja = precyzja

    def run(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            self.blad.emit(
                "Brak biblioteki faster-whisper — zainstaluj: venv/bin/pip install faster-whisper"
            )
            return

        klucz = (self.model, self.precyzja)
        if klucz not in _MODELE_CACHE:
            self.postep.emit(
                f"Ładuję model „{self.model}” (pierwsze użycie może go pobrać)…"
            )
            try:
                _MODELE_CACHE[klucz] = WhisperModel(
                    self.model, device="cpu", compute_type=self.precyzja
                )
            except Exception as e:
                self.blad.emit(f"Nie udało się załadować modelu: {e}")
                return
        model = _MODELE_CACHE[klucz]

        # Whisper przyjmuje float32 @ 16 kHz mono. Konwertujemy ffmpegem
        # i czytamy modułem wave — transkrypcja z wątku roboczego omija
        # w ten sposób wadliwy resampler PyAV.
        audio = None
        wav16 = self.plik.with_suffix(".16k.wav")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(self.plik),
                 "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav16)],
                check=True, capture_output=True,
            )
            with wave.open(str(wav16), "rb") as w:
                import numpy as np
                audio = (
                    np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
                    .astype(np.float32) / 32768.0
                )
        except (OSError, subprocess.SubprocessError, wave.Error):
            pass  # brak ffmpeg — transkrybujemy plik bezpośrednio
        finally:
            wav16.unlink(missing_ok=True)

        self.postep.emit("Przepisuję nagranie…")
        try:
            segmenty, _info = model.transcribe(
                audio if audio is not None else str(self.plik),
                language=JEZYKI.get(self.jezyk),
                vad_filter=True,
            )
            tekst = " ".join(s.text.strip() for s in segmenty).strip()
        except Exception as e:
            traceback.print_exc()
            self.blad.emit(f"Błąd transkrypcji: {e}")
            return
        self.gotowe.emit(tekst)


class OknoGlowne(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Dyktator-GUI — mikrofon → tekst")
        self.resize(720, 580)

        self.konfig = wczytaj_konfig()
        self.nagrywarka = None
        self.transkrybent = None
        self._start_czasu = 0.0

        self._zbuduj_gui()
        self._wczytaj_ustawienia()
        self.statusBar().showMessage("Gotowy. Wybierz mikrofon i naciśnij nagrywanie.")

    # ---------- budowa interfejsu ----------

    def _zbuduj_gui(self):
        centralny = QWidget()
        uklad = QVBoxLayout(centralny)
        uklad.setSpacing(10)

        # --- nagrywanie ---
        grupa_nagrywania = QGroupBox("Nagrywanie")
        uklad_n = QVBoxLayout(grupa_nagrywania)

        wiersz = QHBoxLayout()
        self.przycisk_nagrywaj = QPushButton("🎙  Zacznij nagrywać")
        self.przycisk_nagrywaj.setMinimumHeight(52)
        self.przycisk_nagrywaj.setStyleSheet("font-size: 16pt; font-weight: bold;")
        self.przycisk_nagrywaj.setCheckable(True)
        self.przycisk_nagrywaj.toggled.connect(self._przelacz_nagrywanie)
        wiersz.addWidget(self.przycisk_nagrywaj, 1)

        self.etykieta_czas = QLabel("0:00")
        self.etykieta_czas.setStyleSheet("font-size: 16pt; font-family: monospace;")
        wiersz.addWidget(self.etykieta_czas)
        uklad_n.addLayout(wiersz)

        self.pasek_glosnosci = QProgressBar()
        self.pasek_glosnosci.setRange(0, 100)
        self.pasek_glosnosci.setTextVisible(False)
        uklad_n.addWidget(self.pasek_glosnosci)
        uklad.addWidget(grupa_nagrywania)

        # --- transkrypcja ---
        grupa_tekstu = QGroupBox("Transkrypcja")
        uklad_t = QVBoxLayout(grupa_tekstu)
        self.pole_tekstu = QTextEdit()
        self.pole_tekstu.setPlaceholderText(
            "Tutaj pojawi się przepisany tekst. Możesz go poprawić przed skopiowaniem."
        )
        uklad_t.addWidget(self.pole_tekstu)
        uklad.addWidget(grupa_tekstu, 1)

        # --- konfiguracja ---
        grupa_konfig = QGroupBox("Konfiguracja")
        siatka = QGridLayout(grupa_konfig)
        siatka.setVerticalSpacing(6)

        siatka.addWidget(QLabel("Model Whispera:"), 0, 0)
        self.combo_model = QComboBox()
        self.combo_model.addItems(MODELE)
        self.combo_model.setToolTip(
            "Większy model = lepsza dokładność, ale wolniej.\n"
            "medium/large przy pierwszym użyciu pobierają 1,5–3 GB."
        )
        self.combo_model.currentTextChanged.connect(self._zapisz_ustawienia)
        siatka.addWidget(self.combo_model, 0, 1)

        siatka.addWidget(QLabel("Język mowy:"), 0, 2)
        self.combo_jezyk = QComboBox()
        self.combo_jezyk.addItems(JEZYKI.keys())
        self.combo_jezyk.currentTextChanged.connect(self._zapisz_ustawienia)
        siatka.addWidget(self.combo_jezyk, 0, 3)

        siatka.addWidget(QLabel("Mikrofon:"), 1, 0)
        self.combo_mikrofon = QComboBox()
        self.combo_mikrofon.currentIndexChanged.connect(self._zapisz_ustawienia)
        siatka.addWidget(self.combo_mikrofon, 1, 1)
        self.przycisk_odswiez = QPushButton("⟳ Odśwież")
        self.przycisk_odswiez.setToolTip("Ponownie przeskanuj źródła dźwięku w systemie")
        self.przycisk_odswiez.clicked.connect(self._odswiez_mikrofony)
        siatka.addWidget(self.przycisk_odswiez, 1, 2)

        siatka.addWidget(QLabel("Precyzja:"), 1, 3)
        self.combo_precyzja = QComboBox()
        self.combo_precyzja.addItems(PRECYZJE.keys())
        self.combo_precyzja.currentTextChanged.connect(self._zapisz_ustawienia)
        siatka.addWidget(self.combo_precyzja, 2, 0, 1, 2)

        self.checkbox_kopiuj = QCheckBox("Kopiuj wynik do schowka")
        self.checkbox_kopiuj.toggled.connect(self._zapisz_ustawienia)
        siatka.addWidget(self.checkbox_kopiuj, 2, 2)

        self.checkbox_zapisuj = QCheckBox("Zapisuj transkrypcje do plików .txt")
        self.checkbox_zapisuj.toggled.connect(self._zapisz_ustawienia)
        siatka.addWidget(self.checkbox_zapisuj, 2, 3)

        self._odswiez_mikrofony()
        uklad.addWidget(grupa_konfig)

        self.setCentralWidget(centralny)

        self.zegar = QTimer(self)
        self.zegar.setInterval(200)
        self.zegar.timeout.connect(self._aktualizuj_czas)

    def _wczytaj_ustawienia(self):
        # blokada sygnałów: zmiany widgetów nie mogą wywołać zapisu konfiguracji
        # z domyślnymi (jeszcze nieustawionymi) wartościami pozostałych pól
        widgety = (self.combo_model, self.combo_jezyk, self.combo_precyzja,
                   self.combo_mikrofon, self.checkbox_kopiuj, self.checkbox_zapisuj)
        for widget in widgety:
            widget.blockSignals(True)

        self.combo_model.setCurrentText(self.konfig["model"])
        self.combo_jezyk.setCurrentText(self.konfig["jezyk"])
        self.combo_precyzja.setCurrentText(
            next((k for k, v in PRECYZJE.items() if v == self.konfig["precyzja"]),
                 next(iter(PRECYZJE)))
        )
        self.checkbox_kopiuj.setChecked(self.konfig["kopiuj"])
        self.checkbox_zapisuj.setChecked(self.konfig["zapisuj"])
        indeks = self.combo_mikrofon.findData(self.konfig["mikrofon"])
        self.combo_mikrofon.setCurrentIndex(max(0, indeks))

        for widget in widgety:
            widget.blockSignals(False)

    def _zapisz_ustawienia(self, *_args):
        self.konfig.update(
            model=self.combo_model.currentText(),
            jezyk=self.combo_jezyk.currentText(),
            mikrofon=self.combo_mikrofon.currentData() or "",
            precyzja=PRECYZJE.get(self.combo_precyzja.currentText(), "int8"),
            kopiuj=self.checkbox_kopiuj.isChecked(),
            zapisuj=self.checkbox_zapisuj.isChecked(),
        )
        zapisz_konfig(self.konfig)

    def _odswiez_mikrofony(self):
        wybrane = self.combo_mikrofon.currentData() or self.konfig["mikrofon"]
        self.combo_mikrofon.blockSignals(True)
        self.combo_mikrofon.clear()
        self.combo_mikrofon.addItem("Domyślny (systemowy)", "")
        for nazwa, opis in lista_zrodel():
            self.combo_mikrofon.addItem(opis, nazwa)
        indeks = self.combo_mikrofon.findData(wybrane)
        self.combo_mikrofon.setCurrentIndex(max(0, indeks))
        self.combo_mikrofon.blockSignals(False)

    # ---------- nagrywanie ----------

    def _przelacz_nagrywanie(self, wlaczone: bool):
        if wlaczone:
            self.przycisk_nagrywaj.setText("⏹  Zatrzymaj i przepisz")
            self.przycisk_nagrywaj.setStyleSheet(
                "font-size: 16pt; font-weight: bold; background: #b3474a; color: white;"
            )
            self.pasek_glosnosci.setValue(0)
            self.nagrywarka = Nagrywarka(self.combo_mikrofon.currentData() or "")
            self.nagrywarka.poziom.connect(self.pasek_glosnosci.setValue)
            self.nagrywarka.zatrzymano.connect(self._nagranie_gotowe)
            self.nagrywarka.blad.connect(self._blad)
            self._start_czasu = time.monotonic()
            self.zegar.start()
            self.nagrywarka.start()
            self.statusBar().showMessage("Nagrywam…")
        else:
            self.zegar.stop()
            self.przycisk_nagrywaj.setEnabled(False)
            if self.nagrywarka:
                self.nagrywarka.zatrzymaj()

    def _aktualizuj_czas(self):
        sekundy = int(time.monotonic() - self._start_czasu)
        self.etykieta_czas.setText(f"{sekundy // 60}:{sekundy % 60:02d}")

    def _nagranie_gotowe(self, plik):
        self.nagrywarka = None
        self.pasek_glosnosci.setValue(0)
        self.przycisk_nagrywaj.setText("🎙  Zacznij nagrywać")
        self.przycisk_nagrywaj.setStyleSheet("font-size: 16pt; font-weight: bold;")
        self.statusBar().showMessage(f"Nagrano: {plik.name} — przygotowuję transkrypcję…")
        self.transkrybent = Transkrybent(
            plik,
            self.combo_model.currentText(),
            self.combo_jezyk.currentText(),
            PRECYZJE.get(self.combo_precyzja.currentText(), "int8"),
        )
        self.transkrybent.postep.connect(lambda m: self.statusBar().showMessage(m))
        self.transkrybent.gotowe.connect(lambda t: self._transkrypcja_gotowa(t, plik))
        self.transkrybent.blad.connect(self._blad)
        self.transkrybent.start()

    def _transkrypcja_gotowa(self, tekst, plik):
        self.transkrybent = None
        self.przycisk_nagrywaj.setEnabled(True)
        if not tekst:
            self.statusBar().showMessage("Nic nie usłyszałem — sprawdź mikrofon i głośność.")
            return
        self.pole_tekstu.setPlainText(tekst)
        if self.checkbox_kopiuj.isChecked():
            QApplication.clipboard().setText(tekst)
        if self.checkbox_zapisuj.isChecked():
            ZAPISY_DIR.mkdir(exist_ok=True)
            cel = ZAPISY_DIR / (plik.stem + ".txt")
            cel.write_text(tekst + "\n", encoding="utf-8")
            self.statusBar().showMessage(f"Gotowe — zapisano też do {cel}")
        else:
            self.statusBar().showMessage("Gotowe.")

    def _blad(self, komunikat):
        self.transkrybent = None
        self.przycisk_nagrywaj.setEnabled(True)
        self.przycisk_nagrywaj.setChecked(False)
        self.przycisk_nagrywaj.setText("🎙  Zacznij nagrywać")
        self.przycisk_nagrywaj.setStyleSheet("font-size: 16pt; font-weight: bold;")
        self.statusBar().showMessage("Błąd")
        QMessageBox.critical(self, "Dyktator-GUI", komunikat)

    def closeEvent(self, zdarzenie):
        if self.nagrywarka and self.nagrywarka.isRunning():
            self.nagrywarka.zatrzymaj()
            self.nagrywarka.wait(3000)
        if self.transkrybent and self.transkrybent.isRunning():
            self.transkrybent.wait(5000)
        super().closeEvent(zdarzenie)


def main():
    aplikacja = QApplication(sys.argv)
    okno = OknoGlowne()
    okno.show()
    sys.exit(aplikacja.exec())


if __name__ == "__main__":
    main()
