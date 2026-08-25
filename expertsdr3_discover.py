#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""expertsdr3_discover.py - hittar aktiva ExpertSDR3-TCI-servrar på den här datorn
genom att läsa ExpertSDR3:s egna profilfiler, istället för hårdkodad IP/port.

Profilfilerna ligger i %LOCALAPPDATA%\\Expert Electronics\\ExpertSDR3\\ och det
finns TVÅ filer per enhetsserienummer:
    <serienummer>.json          - profilen för att ansluta LOKALT (LAN/USB)
    remote_<serienummer>.json   - profilen för att ansluta via ERS FJÄRRSTYRT

Verifierat 2026-08-03 mot en riktig tvåradio-uppsättning (en lokal, en fjärrstyrd)
genom att jämföra filernas ändringstid: den fil som faktiskt används just nu har
ett färskt mtime, den andra (samma enhet, andra anslutningssättet) är gammal.
`remote_`-varianten hade dessutom redan `transceivers=1` (ERS begränsar till
RX1), medan den lokala hade `transceivers=2` - fältet självt speglar alltså
korrekt vilket anslutningssätt filen gäller, inget separat runtime-hack behövs.

Relevanta nycklar:
    GlobalProfile_.transceivers  -> antal användbara mottagare för DEN HÄR
                                     anslutningsvägen (redan ERS-korrigerat
                                     för remote_-filer)
    Gui.TCI.open                 -> true om TCI-servern är påslagen
    Gui.TCI.port                 -> TCI-porten (alltid 127.0.0.1 lokalt)
    Gui.SoundCard.output         -> ljudenheten ExpertSDR3 SJÄLV spelar upp
                                     ljud på (t.ex. "MME: Hörlurar på
                                     headset (...)") - används för att låta
                                     vår egen sidoton hamna på SAMMA enhet
                                     som ESDR3 använder, i stället för att
                                     gissa via Windows systemstandard.
                                     `line_out_*`-fälten i GlobalProfile_ är
                                     separata, ofta avstängda hjälputgångar
                                     (extern mixer m.m.) - inte samma sak.
"""

import glob
import json
import os
import re

_SERIAL_RE = re.compile(r"^(remote_)?EED\d+$")

PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "Expert Electronics", "ExpertSDR3")


def _strip_hostapi_prefix(name):
    """'MME: Hörlurar på headset (...)' -> 'Hörlurar på headset (...)' -
    profilens namn har ett API-prefix vi inte bryr oss om vid matchning
    (vi letar upp enheten på valfri hostapi i sidetone.py)."""
    if name and ":" in name:
        return name.split(":", 1)[1].strip()
    return name


class DiscoveredRadio:
    def __init__(self, serial, role, port, transceivers, mtime, path, sound_output=None):
        self.serial = serial      # utan "remote_"-prefix
        self.role = role          # "local" eller "remote"
        self.port = port
        self.transceivers = transceivers
        self.mtime = mtime
        self.path = path
        self.sound_output = sound_output   # Gui.SoundCard.output, API-prefix borttaget

    def __repr__(self):
        return "DiscoveredRadio(serial=%r, role=%r, port=%r, transceivers=%r)" % (
            self.serial, self.role, self.port, self.transceivers)


def discover(profile_dir=PROFILE_DIR, role_overrides=None):
    """Returnerar en lista DiscoveredRadio, en per profilfil vars TCI-server
    är påslagen (Gui.TCI.open == true). Trasiga/oläsbara filer och gamla
    .backup/.corrupt/" - Copy"-varianter hoppas tyst över.

    `role_overrides`: valfri {serial: "local"/"remote"} från
    tcisync_settings.json (nyckel "role_overrides"). Profilfilernas "open"-
    flagga uppdateras inte vid frånkoppling och mtime kan bli missvisande
    (t.ex. om profilfilen redigerats direkt av en felsökningssession utan att
    ExpertSDR3 själv anslutit lokalt) - se DOCUMENTATION.md om Blecket-radion
    (alltid fjärransluten via ERS över 5G) som 2026-08-12 felaktigt visades
    som "local" eftersom lokal-profilfilen råkade ha färskare mtime än
    remote_-varianten. En override löser det manuellt tills en mer
    tillförlitlig live-signal finns."""
    found = []
    if not os.path.isdir(profile_dir):
        return found
    for path in glob.glob(os.path.join(profile_dir, "*.json")):
        name = os.path.basename(path)
        stem = name[:-5]   # ta bort ".json"
        if not _SERIAL_RE.match(stem):
            continue       # t.ex. bands.json, settings.json, backup-/corrupt-varianter
        if stem.startswith("remote_"):
            role, serial = "remote", stem[len("remote_"):]
        else:
            role, serial = "local", stem
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
        sound_output = _strip_hostapi_prefix(
            data.get("Gui", {}).get("SoundCard", {}).get("output"))
        found.append(DiscoveredRadio(serial, role, int(port), int(transceivers),
                                      os.path.getmtime(path), path, sound_output))
    # "open" i profilen uppdateras inte till false vid frånkoppling, den ligger
    # kvar från senaste gången just den filen begagnades - så samma serienummer
    # kan dyka upp i BÅDA rollerna om enheten någon gång körts både lokalt och
    # fjärrstyrt. Vid krock på samma serienummer vinner den med färskast mtime.
    best = {}
    for r in found:
        cur = best.get(r.serial)
        if cur is None or r.mtime > cur.mtime:
            best[r.serial] = r
    if role_overrides:
        for r in found:
            wanted = role_overrides.get(r.serial)
            if wanted and r.role == wanted:
                best[r.serial] = r
    return list(best.values())


def local_sound_output_name(discovered=None):
    """Namnet på ljudenheten den LOKALA ExpertSDR3-instansen spelar upp på
    (för att låta sidotonen hamna på samma enhet), eller None om ingen lokal
    radio hittades/profilen saknade fältet."""
    if discovered is None:
        discovered = discover()
    for r in discovered:
        if r.role == "local" and r.sound_output:
            return r.sound_output
    return None


if __name__ == "__main__":
    for r in discover():
        print(r)
