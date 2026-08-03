#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audio.py - SunSync ljudväxling: speglar den hörbara mängden celler till radiorna.

Mus-modellen (DESIGN §9). Vad du hör bestäms av `station.audible_cells()`:
  normalt      -> [öra]
  knapp 14     -> [TX]            (tillfällig TX-lyssning)
  knapp 14+12  -> [öra, TX]       (hör båda)

Speglas med (verifierat mot tci_protocol.txt):
  MUTE:bool;                    mutar hela radioinstansen (radior utan hörbar cell).
  RX_MUTE:rx,bool;              mutar en mottagare (RX:er utan hörbar cell på en hörbar radio).
  RX_CHANNEL_ENABLE:rx,1,true;  aktiverar VFO B:s ljudkanal (VFO A alltid på, bara B styrbar).
  RX_VOLUME:rx,vfo,dB;          per-VFO-nivå (hörbar VFO = 0 dB, tyst = -60 dB).

VFO B kräver att dess kanal aktiveras för att alls höras (spec: "Channel (VFO) A is always
on, only channel (VFO) B can be controlled"); utan det blir VFO B tyst trots RX_VOLUME.
"""

VOL_ON = 0
VOL_OFF = -60


class AudioSwitch:
    def __init__(self, station, links, log=None):
        self.station = station
        self.links = links
        self._log = log or (lambda _t: None)

    def _send(self, radio_key, cmd):
        link = self.links.get(radio_key)
        if link is not None:
            link.send(cmd)

    def apply(self):
        """Sätt MUTE/RX_MUTE/RX_VOLUME så exakt de hörbara cellerna hörs. Idempotent."""
        st = self.station
        audible = st.audible_cells()          # lista av (radio_idx, rx_idx, vfo)
        for ri, radio in enumerate(st.radios):
            rxs_here = {rxi for (r, rxi, _v) in audible if r == ri}
            if not rxs_here:
                self._send(radio.key, "MUTE:true;")
                continue
            self._send(radio.key, "MUTE:false;")
            for rx in radio.rx:
                if rx.index in rxs_here:
                    self._send(radio.key, "RX_MUTE:%d,false;" % rx.index)
                    vfos = {v for (r, rxi, v) in audible if r == ri and rxi == rx.index}
                    if 1 in vfos:
                        # VFO B hörs bara om dess kanal är aktiverad (VFO A är alltid på).
                        self._send(radio.key, "RX_CHANNEL_ENABLE:%d,1,true;" % rx.index)
                    for vfo in (0, 1):
                        db = VOL_ON if vfo in vfos else VOL_OFF
                        self._send(radio.key, "RX_VOLUME:%d,%d,%d;" % (rx.index, vfo, db))
                else:
                    self._send(radio.key, "RX_MUTE:%d,true;" % rx.index)
        names = ", ".join("%s RX%d VFO %s" % (st.radios[r].name, rxi + 1, "AB"[v])
                          for (r, rxi, v) in audible)
        self._log("Ljud: %s" % (names or "(inget)"))
