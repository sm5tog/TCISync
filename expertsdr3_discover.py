#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""expertsdr3_discover.py - hittar aktiva ExpertSDR3-TCI-servrar på den här datorn
genom att läsa ExpertSDR3:s egna profilfiler, istället för hårdkodad IP/port.

Profilfilerna ligger i %LOCALAPPDATA%\\Expert Electronics\\ExpertSDR3\\ och heter
<enhetsserienummer>.json (t.ex. EED06511900116.json) för en LOKAL ExpertSDR3-
instans (även en instans som själv fjärrstyr en annan radio via ERS räknas som
"lokal" här - det är DEN INSTANSEN som kör en öppen TCI-server på den här datorn).

Filer prefixade "remote_<serienummer>.json" beskriver ett ANNAT scenario (den här
enhetens profil NÄR NÅGON ANNAN fjärransluter till DEN) och är irrelevanta för
detektion av vad vi själva kan ansluta till - de ignoreras.

Relevanta nycklar (verifierade mot riktiga profilfiler 2026-08-03):
    GlobalProfile_.transceivers  -> hårdvarans mottagarkapacitet (1 eller 2)
    Gui.TCI.open                 -> true om TCI-servern är påslagen
    Gui.TCI.port                 -> TCI-porten (alltid 127.0.0.1 lokalt)

OBS: `transceivers` speglar HÅRDVARANS kapacitet, inte en eventuell ERS-
begränsning till RX1 vid fjärrstyrning (se DESIGN.md §10 i sunsync) - antalet
faktiskt användbara mottagare kan vara lägre än vad profilen anger och bör
bekräftas mot radion själv (t.ex. via RX_CHANNEL_ENABLE) när den ansluts.
"""

import glob
import json
import os
import re

_SERIAL_RE = re.compile(r"^EED\d+$")

PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "Expert Electronics", "ExpertSDR3")


class DiscoveredRadio:
    def __init__(self, serial, port, transceivers, path):
        self.serial = serial
        self.port = port
        self.transceivers = transceivers
        self.path = path

    def __repr__(self):
        return "DiscoveredRadio(serial=%r, port=%r, transceivers=%r)" % (
            self.serial, self.port, self.transceivers)


def discover(profile_dir=PROFILE_DIR):
    """Returnerar en lista DiscoveredRadio, en per profilfil vars TCI-server
    är påslagen (Gui.TCI.open == true). Trasiga/oläsbara filer hoppas tyst
    över (profilkatalogen innehåller ofta gamla .backup/.corrupt-varianter)."""
    found = []
    if not os.path.isdir(profile_dir):
        return found
    for path in glob.glob(os.path.join(profile_dir, "*.json")):
        name = os.path.basename(path)
        if name.startswith("remote_"):
            continue
        serial = name[:-5]   # ta bort ".json"
        if not _SERIAL_RE.match(serial):
            continue         # t.ex. bands.json, settings.json, .backup/.corrupt/" - Copy"-varianter
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        tci = data.get("Gui", {}).get("TCI", {})
        if not tci.get("open"):
            continue
        port = tci.get("port")
        if port is None:
            continue
        transceivers = data.get("GlobalProfile_", {}).get("transceivers", 1)
        found.append(DiscoveredRadio(serial, int(port), int(transceivers), path))
    return found


if __name__ == "__main__":
    for r in discover():
        print(r)
