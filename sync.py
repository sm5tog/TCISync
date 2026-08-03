#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync.py - SunSync bidirektionell frekvenssynk mellan de två radiorna (steg 6).

Valfritt läge (`station.sync_mode`). När det är på reläas ett urval RX-parametrar
åt båda hållen mellan Ramnäs och Blecket, så de hålls på samma frekvens (och ev.
mode/RIT/filter). Portat ur SM5KRigSyncs bevisade relä-logik.

Dubbel-TX-skydd: allowlist, inte blocklist. Bara RX-parametrar reläas, ALDRIG
sändningskommandon (TRX, TUNE, DRIVE, CW_*, KEYER, MON_*, SPLIT). Synken sänder
alltså aldrig. Ett värde vi själva reläat ut skyddas av en eko-spärr så det inte
studsar tillbaka i en loop när radion bekräftar ändringen.

Spots (SPOT/SPOT_DELETE/SPOT_CLEAR, kommer via TCI från jtspots) reläas ALLTID mellan
radiorna, oberoende av synk-läget, så samma spots visas på båda panoramabilderna.

Vid lås-påslag snäpps remoten till RX1 direkt via `align()` (frågar RX1 om nuvarande
värden; svaren reläas), i stället för att bara reläa framtida ändringar.

RX-mappning: bara mottagare 0 (RX1) ↔ mottagare 0 reläas — remote-radion har bara
RX1, och Ramnäs RX2 (mottagare 1) hör inte till synken mellan QTH.
"""

import time

ECHO_WINDOW_S = 1.5     # hur länge ett nyss reläat värde skyddas mot att studsa tillbaka


class Sync:
    def __init__(self, station, links, log=None):
        self.station = station
        self.links = links                  # {radio_key: TciLink}
        self._log = log or (lambda _t: None)
        self._guard = {}                    # (radio_key, name, args) -> tidsstämpel
        # Per-parameter-väljare (defaults: frekvens + mode på, som SM5KRigSync).
        self.sync_vfo = True                # VFO A (kanal 0)
        self.sync_mod = True                # MODULATION
        self.sync_vfob = False              # VFO B (kanal 1) + kanalaktivering
        self.sync_rit = False               # RIT_ENABLE/RIT_OFFSET
        self.sync_filter = False            # RX_FILTER_BAND

    def _other(self, source_key):
        for r in self.station.radios:
            if r.key != source_key:
                return r.key
        return None

    def _send(self, target_key, name, args):
        """Reläa ett kommando och minns det kort (eko-spärr)."""
        self._guard[(target_key, name, tuple(args))] = time.time()
        link = self.links.get(target_key)
        if link is not None:
            link.send("%s:%s;" % (name, ",".join(args)))

    def relay(self, ev):
        """Ett inkommande TCI-kommando (dict med radio/name/args). Spots reläas ALLTID
        (oberoende av synk-läget); övriga parametrar bara när synk-låset är på och de är
        i allowlisten. Reläas till den andra radion."""
        if ev.get("kind") != "cmd":
            return
        source = ev["radio"]
        name = ev["name"]
        args = ev["args"]

        gkey = (source, name, tuple(args))
        ts = self._guard.pop(gkey, None)
        if ts is not None and time.time() - ts < ECHO_WINDOW_S:
            return                          # eko av vår egen relä, inte en ny ändring

        target = self._other(source)
        if target is None:
            return

        # Spots (från jtspots via TCI) reläas alltid, oberoende av synk-läget.
        if name in ("SPOT", "SPOT_DELETE", "SPOT_CLEAR"):
            self._send(target, name, args)
            return

        # Övriga parametrar bara när synk-låset är på.
        if not self.station.sync_mode:
            return

        if name == "VFO" and len(args) >= 3 and args[0] == "0":
            ch = args[1]
            if ch == "0" and self.sync_vfo:
                self._send(target, "VFO", ["0", "0", args[2]])
            elif ch == "1" and self.sync_vfob:
                self._send(target, "VFO", ["0", "1", args[2]])
        elif name == "MODULATION" and self.sync_mod and len(args) >= 2 and args[0] == "0":
            self._send(target, "MODULATION", ["0", args[1]])
        elif name == "RX_CHANNEL_ENABLE" and self.sync_vfob and len(args) >= 3 \
                and args[0] == "0" and args[1] == "1":
            self._send(target, "RX_CHANNEL_ENABLE", ["0", args[1], args[2]])
        elif name in ("RIT_ENABLE", "RIT_OFFSET") and self.sync_rit \
                and len(args) >= 2 and args[0] == "0":
            self._send(target, name, ["0"] + list(args[1:]))
        elif name == "RX_FILTER_BAND" and self.sync_filter and len(args) >= 3 and args[0] == "0":
            self._send(target, "RX_FILTER_BAND", ["0", args[1], args[2]])
        # Allt annat (TRX, TUNE, DRIVE, CW_*, KEYER, MON_*, SPLIT_ENABLE, ...) ignoreras alltid.

    def align(self):
        """Snäpp remoten till RX1 vid lås-påslag. Frågar RX1 (Ramnäs, mottagare 0) om
        nuvarande värden för de påslagna parametrarna; svaren kommer in som TCI-event och
        reläas av relay() till den andra radion (kräver att sync_mode redan är på). Så
        remoten hamnar rätt direkt i stället för att vänta på nästa manuella ändring."""
        master = self.station.radios[0]         # Ramnäs = referens (RX1)
        q = []
        if self.sync_vfo:
            q.append("VFO:0,0;")
        if self.sync_vfob:
            q.append("VFO:0,1;")
        if self.sync_mod:
            q.append("MODULATION:0;")
        if self.sync_rit:
            q += ["RIT_OFFSET:0;", "RIT_ENABLE:0;"]
        if self.sync_filter:
            q.append("RX_FILTER_BAND:0;")
        link = self.links.get(master.key)
        if link is not None:
            for c in q:
                link.send(c)
