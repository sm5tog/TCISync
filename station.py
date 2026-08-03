#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""station.py - SunSync domänmodell ("stationen"). Enda sanningskällan.

Se DESIGN.md §1. Ren datamodell utan trådar: `tci_link` matar in ändringar (via
appens pump i GUI-tråden), GUI och moduler läser. `subscribe()`/`notify()` ger en
enkel observer så UI kan rita om vid ändring.
"""


class Receiver:
    """En mottagare (RX1/RX2). Har VFO A (0) och B (1)."""

    def __init__(self, index):
        self.index = index                 # 0 = RX1, 1 = RX2
        self.freq = {0: None, 1: None}      # VFO A = 0, B = 1 -> Hz
        self.active_vfo = 0                 # 0 = A, 1 = B
        self.mode = None                    # MODULATION
        self.rit_offset = 0
        self.rit_on = False
        self.split = False                 # SPLIT_ENABLE per mottagare (transceiver-index): TX på VFO B när på
        self.vfob_on = False               # RX_CHANNEL_ENABLE kanal 1 (dual-VFO/split-RX)
        self.listen = "A"                  # vilken VFO man hör: 'A' / 'B' / 'A,B'


class Radio:
    """En radioenhet med TCI-anslutning och 1-2 mottagare."""

    def __init__(self, key, name, ip, port, place, rx_count):
        self.key = key
        self.name = name
        self.ip = ip
        self.port = port
        self.place = place                  # 'local' (2 RX) / 'remote' (1 RX)
        self.rx_count = rx_count
        self.connected = False
        self.drive = None
        self.tune = False
        self.wpm = None
        self.rx = [Receiver(i) for i in range(rx_count)]


class Station:
    """Hela operatörsplatsen: radioapparater + öra/TX/encoder/lyssnings-tillstånd.

    Mus-modellen (DESIGN §9): två markörer, `ear` (öra, du hör) och `tx` (du sänder),
    var och en en cell (radio_idx, rx_idx, vfo). Encodern stämmer den ena i taget
    (`encoder_on`). `listen_tx`/`listen_both` styr tillfällig TX-lyssning (knapp 14/12).
    """

    def __init__(self, radios):
        self.radios = radios                # ordning: [Ramnäs (lokal), Blecket (remote)]
        self.ear = (0, 0, 0)                # (radio_idx, rx_idx, vfo) — hemvist, du hör
        self.tx = (0, 0, 0)                 # (radio_idx, rx_idx, vfo) — du sänder
        self.encoder_on = "ear"             # "ear" | "tx" — vad vredet stämmer
        self.listen_tx = False              # knapp 14: hör TX tillfälligt (öra följer TX)
        self.listen_both = False            # knapp 12: hör TX + RX (beständigt läge)
        self.sync_mode = False
        self._listeners = []

    def subscribe(self, fn):
        self._listeners.append(fn)

    def notify(self):
        for fn in self._listeners:
            fn()

    def by_key(self, key):
        for r in self.radios:
            if r.key == key:
                return r
        return None

    def index_of(self, key):
        """Radions plats i self.radios. -1 om okänd."""
        for i, r in enumerate(self.radios):
            if r.key == key:
                return i
        return -1

    # ----- cell-uppslag (cell = (radio_idx, rx_idx, vfo)) ----------------------
    def cell_radio(self, cell):
        return self.radios[cell[0]]

    def cell_rx(self, cell):
        return self.radios[cell[0]].rx[cell[1]]

    def encoder_cell(self):
        """Cellen vredet stämmer just nu (öra eller TX)."""
        return self.ear if self.encoder_on == "ear" else self.tx

    def tx_radio(self):
        return self.radios[self.tx[0]]

    def tx_rx(self):
        return self.radios[self.tx[0]].rx[self.tx[1]]

    def ear_rx(self):
        return self.radios[self.ear[0]].rx[self.ear[1]]

    def audible_cells(self):
        """Cellen/cellerna som hörs just nu enligt lyssningsläget."""
        if not self.listen_tx:
            return [self.ear]
        if self.listen_both:
            return [self.ear, self.tx]
        return [self.tx]


def default_station():
    """Fallback-uppsättning (samma som sunsyncs hårdkodade default) - används om
    auto_station() inte hittar några ExpertSDR3-profiler alls, t.ex. första
    körningen på en ny dator innan ExpertSDR3 ens anslutit till en radio."""
    return Station([
        Radio("ramnas", "Ramnäs", "127.0.0.1", 50001, "local", 2),
        Radio("blecket", "Blecket", "127.0.0.1", 40001, "remote", 1),
    ])


def auto_station(discovered=None):
    """Bygger en Station från auto-detekterade radioapparater (se
    expertsdr3_discover.discover()). Lokal radio läggs först (index 0) om den
    finns, sedan fjärrstyrd - matchar den tidigare hårdkodade ordningen så
    resten av modellen (t.ex. Station.ear/tx default (0,0,0) = första radion)
    beter sig som väntat. `Radio.place` sätts direkt från `role`
    ("local"/"remote" är redan samma värden).

    Faller tillbaka på default_station() om inget hittas alls.

    OBS: bara stationsmodellen byggs om av det här - E-Coder-knappmappningen
    och cockpitens paneluppställning (`tcisync.py: SLOTS`) är fortfarande
    hårdkodade mot nycklarna "ramnas"/"blecket" och behöver en egen ändring
    innan de fungerar med auto-detekterade radioapparater (nästa steg)."""
    if discovered is None:
        from expertsdr3_discover import discover
        discovered = discover()
    if not discovered:
        return default_station()
    order = {"local": 0, "remote": 1}
    discovered = sorted(discovered, key=lambda r: order.get(r.role, 2))
    names = {"local": "Lokal", "remote": "Fjärrstyrd"}
    radios = [Radio(r.serial, names.get(r.role, r.role), "127.0.0.1", r.port,
                     r.role, r.transceivers)
              for r in discovered]
    return Station(radios)
