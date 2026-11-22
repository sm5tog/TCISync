#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tcisync.py - TCISync (by SM5K): startpunkt, inställningar och delade konstanter.

Gränssnittet (Qt, PySide6) ligger i tcisync_qt.py. Den här filen håller inställningsfilen,
versionen, uppdateringskonstanterna och format-hjälpfunktionerna som både gränssnittet och
station.py läser.

Trim-läge: `python tcisync.py --tweak` visar reglage för textstorlek och cellmått.
"""

import sys
import os
import json

VERSION = "0.25.0"
# WSJT-bron: två platser. "ramnas" = lokala radions RX2, "blecket" = fjärrradions RX1. Varje plats är
# RX (grön), TX (röd), RX+TX (orange) eller av (grå); ett klick stegar platsen RX > TX > RX+TX.
BRIDGE_PLACES = ("ramnas", "blecket")
BRIDGE_TX_LIVE = "#ff5a5a"      # TX-knappen under pågående sändning (ljusare än vanlig TX-röd)
BRIDGE_ROLE_TEXT = {"rx": "RX", "tx": "TX", "both": "RX+TX", "off": "-"}
HID_RETRY_S = 2.0      # E-Coder reopen interval after a lost connection
HID_SILENCE_S = 3.0    # no HID report for this long = dead handle

# Inuti en PyInstaller-frozen .exe pekar __file__ INTE på .exe-filens egen
# mapp (bundlens interna extraktionsmapp) - settings/panel-mappning/LED-
# inställningar skulle tystnat läsas/skrivas fel och aldrig sparas mellan
# omstarter. sys.frozen sätts alltid av PyInstaller. Samma fix som jtspots
# (DOCUMENTATION.md §2.20 där), upptäckt vid TCISyncs första exe-build.
_BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(_BASE_DIR, "tcisync_settings.json")

DONATE_URL = "https://www.paypal.com/donate/?hosted_button_id=5VX8NE7LQEGF4"
HELP_URL = "https://github.com/sm5tog/TCISync/blob/master/README_TCISYNC.md"

# Auto-uppdatering: samma mönster som jtspots (jtspots_updater.py), men
# releaserna ligger i SAMMA repos Releases-flik (som rotor sedan 2026-08-01),
# inte i ett separat "-release"-repo - TCISync är publik källkod från start.
UPDATE_REPO = "sm5tog/TCISync"
UPDATE_CHECK_URL = "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO
UPDATE_CHECK_MS = 60 * 60 * 1000   # en gång i timmen, samma som jtspots
UPDATER_NAME = "TCISyncUpdater.exe"


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(d):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)

# CW-nyckelns modellparametrar (nyckel "cw" i tcisync_settings.json), för att kunna
# modelleras/experimenteras med utan kodändring. Defaultvärdena matchar Jonnys
# ExpertSDR3-profil (Dot 100%, Dash 300%, Dash-Dot pause 100%, Ramp 7ms, Pitch 600Hz).
DEFAULT_CW_SETTINGS = {
    "key_type": "paddle",  # "paddle" eller "straight", se cw_keyer.py
    "pitch_hz": 600,
    "ramp_ms": 7,
    "volume_pct": 35,
    "dot_pct": 100,
    "dash_pct": 300,
    "gap_pct": 100,
    "debounce_ms": 15,
    "break_in_ms": 400,
    "ptt_cw_delay_ms": 50,
}

UI_SCALE = 0.7            # fallback-default för per-roll-skalorna nedan
FREQ_SCALE = 0.62         # per-roll textskala (trimmat värde 2026-07-27)
HEAD_SCALE = 0.76
STATUS_SCALE = 0.83
CELL_W = 137      # cellens bredd (px)
CELL_H = 30       # cellens höjd (px)

def _build_slots(station):
    """Panelordning: (radio_key, rx_idx, rubrik), en panel per mottagare -
    byggd från stationens faktiska (auto-detekterade) radioapparater, inte
    hårdkodad mot Ramnäs/Blecket-namn."""
    slots = []
    for radio in station.radios:
        prefix = "" if radio.place == "local" else "Remote "
        for rxi in range(radio.rx_count):
            label = "%sRX%d" % (prefix, rxi + 1)
            slots.append((radio.key, rxi, label))
    return slots


def fmt_freq(hz):
    if hz is None:
        return "--- . --- . ---"
    return "{:,}".format(int(hz)).replace(",", ".")


def fmt_split(freq_a, freq_b):
    """Split-knappens text: VFO B minus VFO A i kHz med tecken, t.ex. "Split +1.07"."""
    if freq_a is None or freq_b is None:
        return "Split"
    khz = (int(freq_b) - int(freq_a)) / 1000.0
    return "Split %.2f" % khz if round(khz, 2) == 0 else "Split %+.2f" % khz


def main():
    from tcisync_qt import run
    run()


if __name__ == "__main__":
    main()
