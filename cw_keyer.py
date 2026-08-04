#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cw_keyer.py - Iambic-keyer med enkel-nivås paddel-memory för E-Coder-manipulatorn.

Lyssnar på paddelns dit-/dah-tillstånd (satta av EncoderRouter utifrån HID-
kontakterna cw_dit/cw_dah, byte 4 mask 0x20/0x10 i ecoder_map.json) och
genererar tidsriktiga TCI KEYER-kommandon till TX-radion, synkat med en
lokal sidoton (sidetone.py).

TCI-kommandot KEYER:radio,state,duration_ms; är byggt av Expert Electronics
för att motverka nätverksjitter: klienten mäter/beräknar elementets längd
lokalt och skickar den med vid release, så ExpertSDR3:s CW-kärna spelar
exakt rätt tecken oavsett fördröjning ("drunken sailor"-kompensation,
tci_protocol.txt). Det är alltså bara paddel-till-element-logiken (dit/dah-
växling vid squeeze) som saknades i protokollet och byggs här.

Enkel-nivås "memory": trycker man på en kontakt medan motsatt element
fortfarande låter (eller under mellanrummet), fastnar det trycket och spelas
upp i sin fulla längd så fort det blir dess tur - även om kontakten redan
hunnit släppas igen. Bara EN väntande nivå hålls (inte en full kö), precis
som en riktig keyer-krets. Ett kontinuerligt nedtryck fortsätter repetera
samma element tills det släpps.

Förhållandet mellan elementen (dot_pct/dash_pct/gap_pct, i procent av en
dit-längd) och sidotonens ramp/tonhöjd är konfigurerbara (se
DEFAULT_CW_SETTINGS och sunsync.py:s CW_SETTINGS-inläsning från
sunsync_settings.json, nyckel "cw") - för att kunna modelleras/experimenteras
med utan kodändring. Defaultvärdena matchar Jonnys ExpertSDR3-profil
(Dot 100%, Dash 300%, Dash-Dot pause 100%, Ramp 7ms, Pitch 600Hz).
"""

import threading
import time

DIT_UNIT_MS = 1200.0   # 1200/WPM = en dit-längd i ms (PARIS-standarden)

DEFAULT_CW_SETTINGS = {
    "key_type": "paddle",  # "paddle" (IambicKeyer) eller "straight" (StraightKeyer)
    "dot_pct": 100,      # dit-längd i % av en enhet (u)
    "dash_pct": 300,     # dah-längd i % av en enhet
    "gap_pct": 100,      # mellanrum mellan element i % av en enhet
    "debounce_ms": 15,   # kontaktstuds kortare än så ignoreras (se set_dit/set_dah)
    "break_in_ms": 400,  # hur länge TX hålls kvar efter sista elementet (Jonnys profil: 400ms)
    "ptt_cw_delay_ms": 50,  # paus mellan TRX:true och FÖRSTA elementet (Jonnys profil: Ptt-CW delay 50ms)
}


class IambicKeyer:
    def __init__(self, get_wpm, send_keyer, send_trx=None, sidetone=None, log=None,
                 dot_pct=None, dash_pct=None, gap_pct=None, debounce_ms=None, break_in_ms=None,
                 ptt_cw_delay_ms=None):
        """get_wpm() -> aktuellt WPM för TX-radion (int/None).
        send_keyer(state: bool, duration_ms: int) -> skickar KEYER-kommandot.
        send_trx(state: bool) -> skickar TRX-kommandot som faktiskt drar PTT (se
        _run: KEYER:true ensamt sätter bara TCI-statusflaggan/TX-lampan hos
        ExpertSDR3, inte den riktiga TX-motorn/interlocket mot andra radion -
        TRX måste skickas separat, precis som PTT-pedalen redan gör).
        sidetone -> valfritt objekt med on()/off(), synkas med varje element.
        dot_pct/dash_pct/gap_pct/debounce_ms/break_in_ms -> se DEFAULT_CW_SETTINGS,
        None = defaultvärdet."""
        self._get_wpm = get_wpm
        self._send_keyer = send_keyer
        self._send_trx = send_trx or (lambda _state: None)
        self._sidetone = sidetone
        self._log = log or (lambda _t: None)
        d = DEFAULT_CW_SETTINGS
        self.dot_pct = dot_pct if dot_pct is not None else d["dot_pct"]
        self.dash_pct = dash_pct if dash_pct is not None else d["dash_pct"]
        self.gap_pct = gap_pct if gap_pct is not None else d["gap_pct"]
        self.debounce_ms = debounce_ms if debounce_ms is not None else d["debounce_ms"]
        self.break_in_ms = break_in_ms if break_in_ms is not None else d["break_in_ms"]
        self.ptt_cw_delay_ms = ptt_cw_delay_ms if ptt_cw_delay_ms is not None else d["ptt_cw_delay_ms"]
        self.dit_down = False
        self.dah_down = False
        self._dit_latch = False
        self._dah_latch = False
        self._last = "dah"     # föregående sända element, för alternering vid squeeze
        self._t_dit = 0.0
        self._t_dah = 0.0
        self._tx_active = False
        self._idle_since = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_dit(self, on):
        now = time.time()
        if on == self.dit_down or now - self._t_dit < self.debounce_ms / 1000.0:
            return
        self._t_dit = now
        self.dit_down = on
        if on:
            self._dit_latch = True   # fastnar tills konsumerad, oavsett hur kort trycket är

    def set_dah(self, on):
        now = time.time()
        if on == self.dah_down or now - self._t_dah < self.debounce_ms / 1000.0:
            return
        self._t_dah = now
        self.dah_down = on
        if on:
            self._dah_latch = True

    def stop(self):
        self._stop.set()
        if self._tx_active:
            self._send_trx(False)
            self._tx_active = False

    def _unit_ms(self):
        wpm = self._get_wpm() or 20
        return DIT_UNIT_MS / max(int(wpm), 1)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._run_step()
            except Exception as e:
                # Ett undantag här får ALDRIG lämna riktig PTT pådragen - släpp
                # den ovillkorligen innan loopen fortsätter (stuck-TX är en
                # allvarligare risk än att tappa ett CW-element).
                self._log("CW keyer error: %s" % e)
                if self._tx_active:
                    try:
                        self._send_trx(False)
                    except Exception:
                        pass
                    self._tx_active = False
                self._idle_since = None

    def _run_step(self):
        if self._dit_latch or self._dah_latch:
            if not self._tx_active:
                self._send_trx(True)   # drar riktig PTT/interlock, inte bara KEYER
                self._tx_active = True
                # paus innan FÖRSTA elementet - annars klipps dess början bort
                # medan sändarkedjan (relä/PA) fortfarande kopplar om till TX
                time.sleep(self.ptt_cw_delay_ms / 1000.0)
            self._idle_since = None
            if self._dit_latch and self._dah_latch:
                elem = "dah" if self._last == "dit" else "dit"
            else:
                elem = "dit" if self._dit_latch else "dah"
            self._last = elem
            self._send_element(elem)
            # konsumera EFTER att elementet (+ mellanrum) spelats klart - ett kort
            # tryck hinner släppas på riktigt under den tiden. Läser vi av detta
            # innan sändningen ser vi alltid "fortfarande nedtryckt" (ett släpp
            # hinner aldrig ske på den bråkdels millisekund det tar loopen att
            # reagera), vilket ovillkorligen skulle köa ett extra element.
            if elem == "dit":
                self._dit_latch = self.dit_down
            else:
                self._dah_latch = self.dah_down
        else:
            if self._tx_active:
                now = time.time()
                if self._idle_since is None:
                    self._idle_since = now
                elif (now - self._idle_since) * 1000 >= self.break_in_ms:
                    self._send_trx(False)   # break-in slut, släpp riktig PTT
                    self._tx_active = False
                    self._idle_since = None
            time.sleep(0.005)

    def _send_element(self, elem):
        """Ett påbörjat element spelas alltid klart i sin fulla längd, oavsett
        om paddeln släpps under tiden (så en keyer ska fungera, en kort
        knapptryckning räcker för en fullständig dit). Latcharna sätts
        redan vid tryck (set_dit/set_dah), så ett tryck som sker medan
        detta element/mellanrum pågår fångas upp och spelas nästa varv."""
        u = self._unit_ms()
        target = u * (self.dot_pct if elem == "dit" else self.dash_pct) / 100.0
        gap = u * self.gap_pct / 100.0

        if self._sidetone:
            self._sidetone.on()   # kontinuerlig ström, ingen ny uppspelning startas
        self._send_keyer(True, 0)
        time.sleep(target / 1000.0)
        self._send_keyer(False, int(target))
        if self._sidetone:
            self._sidetone.off()
        time.sleep(gap / 1000.0)         # mellanrum mellan element


class StraightKeyer:
    """Rak nyckling för en enkel manipulator/straight key, i stället för
    IambicKeyers paddel-timing (dit/dah-element, squeeze-memory). Här finns
    inga element att beräkna - kontaktens egen nedtryckta tid ÄR tecknet,
    operatören formar det själv för hand.

    set_key(on) matar BÅDA E-Coderns kontakter (cw_dit och cw_dah) hit i
    straight-läge (se encoder_router.py) - vilken av de två pinnarna en
    riktig rak nyckel råkar vara inkopplad på spelar ingen roll.

    Samma break-in-mönster mot riktig PTT (TRX) som IambicKeyer: TX dras vid
    första nedtryck, släpps `break_in_ms` efter sista släppet - inte direkt
    vid varje enskilt tecken (skulle knattra PTT-relät)."""

    def __init__(self, send_keyer, send_trx=None, sidetone=None, log=None,
                 debounce_ms=None, break_in_ms=None):
        self._send_keyer = send_keyer
        self._send_trx = send_trx or (lambda _state: None)
        self._sidetone = sidetone
        self._log = log or (lambda _t: None)
        d = DEFAULT_CW_SETTINGS
        self.debounce_ms = debounce_ms if debounce_ms is not None else d["debounce_ms"]
        self.break_in_ms = break_in_ms if break_in_ms is not None else d["break_in_ms"]
        self._down = False
        self._down_ts = 0.0
        self._key_since = 0.0
        self._tx_active = False
        self._idle_since = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_key(self, on):
        now = time.time()
        if on == self._down or now - self._down_ts < self.debounce_ms / 1000.0:
            return
        self._down_ts = now
        self._down = on
        if on:
            if not self._tx_active:
                self._send_trx(True)   # drar riktig PTT/interlock
                self._tx_active = True
            self._idle_since = None
            self._key_since = now
            if self._sidetone:
                self._sidetone.on()
            self._send_keyer(True, 0)
        else:
            duration_ms = int((now - self._key_since) * 1000)
            if self._sidetone:
                self._sidetone.off()
            self._send_keyer(False, duration_ms)

    def stop(self):
        self._stop.set()
        if self._tx_active:
            self._send_trx(False)
            self._tx_active = False

    def _run(self):
        while not self._stop.is_set():
            try:
                self._run_step()
            except Exception as e:
                # Samma princip som IambicKeyer._run: ett undantag får aldrig
                # lämna riktig PTT pådragen.
                self._log("CW keyer error: %s" % e)
                if self._tx_active:
                    try:
                        self._send_trx(False)
                    except Exception:
                        pass
                    self._tx_active = False
                self._idle_since = None

    def _run_step(self):
        if not self._down and self._tx_active:
            now = time.time()
            if self._idle_since is None:
                self._idle_since = now
            elif (now - self._idle_since) * 1000 >= self.break_in_ms:
                self._send_trx(False)   # break-in slut, släpp riktig PTT
                self._tx_active = False
                self._idle_since = None
        time.sleep(0.005)
