#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tx_timing.py - tidsmätning av en sändning genom WSJT-bron (bara diagnostik).

Ett enda ansvar: samla tidsstämplar och räknare för EN sändning (PTT på till PTT av) och ge en
sammanfattningsrad. Ingen nätverkskod, ingen TCI-tolkning. Syftet är att se om det finns en lucka
mellan PTT och att ljudet faktiskt når radion.
"""

import time

TONE_THRESHOLD = 0.02       # toppnivå (float32, 1.0 = full skala) över vilket ett ljudblock räknas som icke-tyst


class TxTiming:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.t_on = clock()
        self.t_echo = None          # radions eget `trx:..,true`-eko
        self.t_chrono = None        # första TX_CHRONO från radion
        self.t_audio = None         # första TX-ljudblocket från WSJT-X
        self.t_tone = None          # första icke-tysta TX-ljudblocket (topp över TONE_THRESHOLD)
        self.n_chrono = 0
        self.n_audio = 0
        self.peak = 0.0
        self.peak_known = False

    def _ms(self, t):
        return "-" if t is None else "+%d ms" % round((t - self.t_on) * 1000)

    def radio_echo(self):
        if self.t_echo is None:
            self.t_echo = self._clock()

    def chrono(self):
        self.n_chrono += 1
        if self.t_chrono is None:
            self.t_chrono = self._clock()

    def audio(self, peak=None):
        self.n_audio += 1
        if self.t_audio is None:
            self.t_audio = self._clock()
        if peak is not None:
            self.peak_known = True
            self.peak = max(self.peak, peak)
            if self.t_tone is None and peak >= TONE_THRESHOLD:
                self.t_tone = self._clock()

    def summary(self):
        dur = round((self._clock() - self.t_on) * 1000)
        return ("TX timing: PTT on, radio echo %s, first TX_CHRONO %s, first TX audio %s, "
                "first non-silent audio %s, PTT off %d ms, %d TX_CHRONO, %d TX audio blocks, peak %s"
                % (self._ms(self.t_echo), self._ms(self.t_chrono), self._ms(self.t_audio),
                   self._ms(self.t_tone), dur,
                   self.n_chrono, self.n_audio, "%.3f" % self.peak if self.peak_known else "?"))
