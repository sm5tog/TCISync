#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""encoder_router.py - SunSync controller: E-Coder-panel + mus -> öra/TX-modellen.

Mus-modellen (DESIGN §9). Två markörer sätts med musen: vänsterklick = öra, höger-
klick = TX. Encodern stämmer den ena (öra/TX), knapp 13 växlar vilken. Knapp 14
togglar tillfällig TX-lyssning, knapp 12 dess läge (TX / TX+RX). Övrig panelstyrning
(TUNE, CW-makron, split, WPM/DRIVE/RIT) riktas mot rätt radio.

Knappnumrering (fysisk, live-kartlagd 2026-07-26) -> roll:
   1 zoom_up      CW "SM5K"        8  step_up     (ledig)
   2 split        (ledig)          9  if_as_rit   (ledig)
   3 switch_rx    TUNE (TX-radio)  10 listen_a    (ledig)
   4 rit_reset    (ledig)          11 tune        CW "TU"
   5 listen_ab    (ledig)          12 listen_b    toggla listen_both
   6 zoom_down    CW "UR 5NN"      13 change_vfo  växla encoder öra/TX
   7 step_down    (ledig)          14 vfob_onoff  toggla listen_tx

Två riktningar med eko-spärr: handle_panel/flush (panel -> TCI + modell) och
handle_tci (TCI -> modell).
"""

import time

from cw_keyer import IambicKeyer, StraightKeyer
from sidetone import Sidetone
from panel_functions import DEFAULT_PANEL_MAP, DEFAULT_LED_MAP, DEFAULT_BACKLIGHT


def _auto_sidetone_output_name():
    """Läser vilken ljudenhet den LOKALA ExpertSDR3-instansen själv spelar
    upp på (Gui.SoundCard.output i profilen), så sidotonen hamnar på samma
    enhet oavsett vad Windows systemstandard råkar stå på. Faller tyst
    tillbaka på None (sidetone.py matchar då mot systemstandarden i stället)
    om ingen profil hittas eller något går fel."""
    try:
        from expertsdr3_discover import local_sound_output_name
        return local_sound_output_name()
    except Exception:
        return None

# --- VFO-varvtalsacceleration (porterad ur ecoder_control) ---------------------
BASE_STEP = 1
ACCEL_N0 = 3.0
ACCEL_SCALE = 6.0
ACCEL_EXP = 2.0
ACCEL_PER_MAX = 2000

DRIVE_STEP = 1
WPM_STEP = 1
VOLUME_STEP = 2
WPM_MIN = 10
WPM_MAX = 50
WPM_PRESET = 20
ECHO_WINDOW = 0.8

def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class EncoderRouter:
    def __init__(self, station, links, log=None, cw_settings=None, panel_map=None,
                 led_map=None, backlight_on=None):
        """cw_settings (valfri dict) -> pitch_hz/ramp_ms (Sidetone) och
        dot_pct/dash_pct/gap_pct (IambicKeyer), se cw_keyer.DEFAULT_CW_SETTINGS
        och sunsync.py:s inläsning från sunsync_settings.json (nyckel "cw").
        panel_map (valfri dict) -> knapp-nyckel -> {"func": ..., "param": ...},
        se panel_functions.py. None/saknad nyckel = DEFAULT_PANEL_MAP:s värde.
        led_map (valfri dict) -> "left"/"right"/"center" -> panel-nyckel (eller
        "none"), se panel_functions.LED_TARGETS. backlight_on (bool) -> stor-
        vredets ljus + tangentbordsbelysning, ett gemensamt på/av."""
        self.station = station
        self.links = links
        self._log = log or (lambda _t: None)
        self.panel_map = dict(DEFAULT_PANEL_MAP)
        if panel_map:
            self.panel_map.update(panel_map)
        self.led_map = dict(DEFAULT_LED_MAP)
        if led_map:
            self.led_map.update(led_map)
        self.backlight_on = DEFAULT_BACKLIGHT if backlight_on is None else backlight_on
        self._acc = {"vfo_main": 0, "knob_left": 0, "knob_center": 0, "knob_right": 0}
        self._ts = {}
        self._sent = {}   # tag -> senast skickade värde (för äkta eko-jämförelse, se _guard/_echoed)
        self._mon_volume = {}   # radio_key -> senast kända MON_VOLUME (dB), från prime()/eko
        self._mon_restore = {}  # radio_key -> nivå att återställa till efter nyckling
        cw = cw_settings or {}
        output_name = cw.get("sidetone_output_name")
        if output_name is None:
            output_name = _auto_sidetone_output_name()
        self.sidetone = Sidetone(
            freq_hz=cw.get("pitch_hz", 700),
            ramp_ms=cw.get("ramp_ms", 7),
            volume=cw.get("volume_pct", 35) / 100.0,
            output_name=output_name,
            log=self._log)
        self.key_type = cw.get("key_type", "paddle")
        if self.key_type == "straight":
            self.keyer = StraightKeyer(
                send_keyer=self._send_keyer,
                send_trx=self._send_keyer_trx,
                sidetone=self.sidetone,
                log=self._log,
                debounce_ms=cw.get("debounce_ms"),
                break_in_ms=cw.get("break_in_ms"))
        else:
            self.keyer = IambicKeyer(
                get_wpm=lambda: self.station.tx_radio().wpm,
                send_keyer=self._send_keyer,
                send_trx=self._send_keyer_trx,
                sidetone=self.sidetone,
                log=self._log,
                dot_pct=cw.get("dot_pct"),
                dash_pct=cw.get("dash_pct"),
                gap_pct=cw.get("gap_pct"),
                debounce_ms=cw.get("debounce_ms"),
                break_in_ms=cw.get("break_in_ms"),
                ptt_cw_delay_ms=cw.get("ptt_cw_delay_ms"))

    def stop(self):
        self.keyer.stop()
        self.sidetone.stop()

    def set_key_type(self, key_type, cw_vals=None):
        """Byter keyer-objekt (IambicKeyer <-> StraightKeyer) live, utan
        omstart av appen. cw_vals (valfri dict, se DEFAULT_CW_SETTINGS) ->
        nya parametervärden att bygga den nya keyern med, annars behålls
        gamla keyerns nuvarande debounce/break_in."""
        if key_type == self.key_type:
            return
        cw_vals = cw_vals or {}
        debounce_ms = cw_vals.get("debounce_ms", self.keyer.debounce_ms)
        break_in_ms = cw_vals.get("break_in_ms", self.keyer.break_in_ms)
        self.keyer.stop()
        self.key_type = key_type
        if key_type == "straight":
            self.keyer = StraightKeyer(
                send_keyer=self._send_keyer,
                send_trx=self._send_keyer_trx,
                sidetone=self.sidetone,
                log=self._log,
                debounce_ms=debounce_ms,
                break_in_ms=break_in_ms)
        else:
            self.keyer = IambicKeyer(
                get_wpm=lambda: self.station.tx_radio().wpm,
                send_keyer=self._send_keyer,
                send_trx=self._send_keyer_trx,
                sidetone=self.sidetone,
                log=self._log,
                dot_pct=cw_vals.get("dot_pct"),
                dash_pct=cw_vals.get("dash_pct"),
                gap_pct=cw_vals.get("gap_pct"),
                debounce_ms=debounce_ms,
                break_in_ms=break_in_ms,
                ptt_cw_delay_ms=cw_vals.get("ptt_cw_delay_ms"))
        self._log("Key type: %s (live)" % key_type)

    def _send(self, radio_key, cmd):
        link = self.links.get(radio_key)
        if link is not None:
            link.send(cmd)

    # ----- eko-spärr -----------------------------------------------------------
    _BLOCK_ANY = object()   # se _guard(block_any=True)/_echoed

    def _guard(self, tag, value=None, block_any=False):
        """value = det vi just skickade för tag, sparas för att kunna skilja
        på ett äkta eko (radion bekräftar SAMMA värde - ignoreras) och en
        legitim korrigering (radion självmant ändrade till ett ANNAT värde,
        t.ex. dess egna VFO B-auto-snap vid split på/av - ska alltid släppas
        igenom, se _echoed). block_any=True (bara `apply_tx`s VFO B-korrigering
        vid split-på) blockerar MEDVETET allt inom fönstret oavsett värde,
        eftersom radions auto-hopp-eko där är känt och oönskat, inte en
        legitim korrigering vi vill se."""
        self._ts[tag] = time.time()
        self._sent[tag] = self._BLOCK_ANY if block_any else value

    def _echoed(self, tag, incoming):
        """True om incoming ska ignoreras: antingen `block_any`-läge (se
        _guard), eller ett äkta eko (incoming matchar exakt det VI skickade).
        Skiljer sig från den gamla logiken som blockerade ALLA inkommande
        värden i fönstret oavsett om de matchade, vilket lät radions egna
        korrigeringar tystas och lämnade appens modell felsynkad mot radion."""
        if time.time() - self._ts.get(tag, 0) >= ECHO_WINDOW:
            return False
        if tag not in self._sent:
            return False
        sent = self._sent[tag]
        return sent is self._BLOCK_ANY or sent == incoming

    # ----- panel in ------------------------------------------------------------
    def handle_panel(self, key, kind, value):
        if kind == "turn":
            if key in self._acc:
                self._acc[key] += value
            return False
        if key in ("cw_dit", "cw_dah"):         # manipulator, kort/lång kontakt (byte 4, mask 0x20/0x10)
            if kind in ("press", "release"):
                if self.key_type == "straight":
                    # Straight-läge: en riktig rak nyckel kan vara inkopplad på
                    # vilken som helst av de två pinnarna - båda nyckling samma sak.
                    self.keyer.set_key(kind == "press")
                elif key == "cw_dit":
                    self.keyer.set_dit(kind == "press")
                else:
                    self.keyer.set_dah(kind == "press")
            return False
        if key == "ptt_pedal":                 # PTT-fotpedal (byte 4, mask 0x08)
            if kind in ("press", "release"):
                return self._ptt(kind == "press")
            return False
        if kind != "press":
            return False
        return self._press(key)

    def _send_keyer(self, state, duration_ms):
        """Skickar TCI KEYER:rx,state,duration; till TX-radion åt IambicKeyer.
        duration_ms är 0 vid nedtryck, uppmätt elementlängd vid släpp (se
        cw_keyer.py och tci_protocol.txt om KEYER-kommandots jitterkompensation)."""
        radio = self.station.tx_radio()
        rxi = self.station.tx[1]
        self._send(radio.key, "KEYER:%d,%s,%d;" % (rxi, "true" if state else "false", duration_ms))

    def _send_keyer_trx(self, state):
        """Drar den RIKTIGA PTT/TX-motorn (inklusive interlock mot andra radion)
        åt IambicKeyer. KEYER ensamt sätter bara TCI-statusflaggan/TX-lampan hos
        ExpertSDR3, inte den riktiga sändningskedjan - upptäckt via TCI-loggen
        2026-08-03 (KEYER-nyckling saknade tx_enable-interlocket som ett riktigt
        makro/TRX-anrop har)."""
        radio = self.station.tx_radio()
        rxi = self.station.tx[1]
        self._send(radio.key, "TRX:%d,%s;" % (rxi, "true" if state else "false"))
        # Radions egen TX-medhörning (MON_VOLUME) tystas under manuell manipulator-
        # nyckling - annars hörs både radions nätverksfördröjda medhörning och vår
        # lokala sidoton samtidigt, förvirrande. MON_ENABLE visade sig INTE tysta
        # ljudet på riktigt (TCI-loggen 2026-08-03 visade att den ekades korrekt men
        # utan hörbar effekt), medan MON_VOLUME gör det (verifierat manuellt av Jonny,
        # -60 = tyst). Nivån minns vi innan vi sänker, och återställer EXAKT samma
        # nivå efteråt istället för att bara slå på igen på en okänd/förvald nivå.
        if state:
            self._mon_restore[radio.key] = self._mon_volume.get(radio.key, 0)
            self._guard(("monvol", radio.key), -60.0)
            self._send(radio.key, "MON_VOLUME:-60;")
        else:
            restore = self._mon_restore.pop(radio.key, 0)
            self._guard(("monvol", radio.key), float(restore))
            self._send(radio.key, "MON_VOLUME:%g;" % restore)

    def _ptt(self, on):
        """PTT-pedal (byte 4, mask 0x08) -> TRX-kommando till TX-radion, härlett
        ur station.tx (DESIGN.md §1/§4 steg 4). Foljer TX-valet dynamiskt istallet
        for att vara hardwired mot en enda radio."""
        radio = self.station.tx_radio()
        rxi = self.station.tx[1]
        self._send(radio.key, "TRX:%d,%s;" % (rxi, "true" if on else "false"))
        self._log("PTT %s (%s RX%d)" % ("ON" if on else "off", radio.name, rxi + 1))
        return False

    # ----- LED-status -----------------------------------------------------------
    def _button_active_state(self, key):
        """ON/OFF-tillstånd för VILKEN FUNKTION SOM HELST just nu sitter på
        `key` (samma panel_map-uppslag som _press använder), för LED-
        speglingen (se led_states). Funktioner utan ett meningsfullt persist-
        ent tillstånd (cw_macro, wpm_preset, unused) ger alltid False."""
        entry = self.panel_map.get(key, {"func": "unused"})
        func = entry.get("func", "unused")
        if func == "toggle_listen_both":
            return self.station.listen_both
        if func == "toggle_listen_tx":
            return self.station.listen_tx
        if func == "change_vfo":
            return self.station.encoder_on == "tx"
        if func == "toggle_rit":
            ri, rxi, _ = self.station.ear
            return self.station.radios[ri].rx[rxi].rit_on
        if func == "split_offset":
            ri, rxi, _ = self.station.ear
            return self.station.radios[ri].rx[rxi].split
        if func == "toggle_volume_mute":
            ri, rxi, _ = self.station.ear
            return self.station.radios[ri].volume_muted
        if func == "tune":
            return self.station.tx_radio().tune
        return False

    def led_states(self):
        """(left, right, center) -> bool, utifrån self.led_map. En LED speglar
        tillståndet hos VAD SOM HELST just nu sitter på den valda knappen -
        håller sig alltså rätt även om knappens funktion ändras senare."""
        def val(k):
            return self._button_active_state(k) if k and k != "none" else False
        return (val(self.led_map.get("left")), val(self.led_map.get("right")),
                val(self.led_map.get("center")))

    def _press(self, key):
        """Datadriven: slår upp knappens funktion i self.panel_map (se
        panel_functions.py) i stället för en hårdkodad if/elif-kedja, så
        knapptilldelningen kan ändras via ett grafiskt konfigurationsverktyg
        utan kodändring."""
        entry = self.panel_map.get(key, {"func": "unused"})
        func = entry.get("func", "unused")
        param = entry.get("param")

        if func == "change_vfo":
            self.station.encoder_on = "tx" if self.station.encoder_on == "ear" else "ear"
            self._log("Encoder: %s" % ("TX" if self.station.encoder_on == "tx" else "Ear"))
            return True
        if func == "toggle_listen_tx":
            self.station.listen_tx = not self.station.listen_tx
            self._log("Listening to %s" % ("TX" if self.station.listen_tx else "ear"))
            return True
        if func == "toggle_listen_both":
            self.station.listen_both = not self.station.listen_both
            self._log("TX listening: %s" % ("TX + RX" if self.station.listen_both else "TX only"))
            return True
        if func == "tune":
            return self._toggle_tune()
        if func == "cw_macro":
            self._send_macro_text(param or "")
            return False
        if func == "wpm_preset":
            return self._set_wpm(int(param) if param else WPM_PRESET)
        if func == "toggle_rit":
            return self._toggle_rit_enable()
        if func == "split_offset":
            ri, rxi, _ = self.station.ear
            return self.split_offset(self.station.radios[ri].key, rxi)
        if func == "toggle_volume_mute":
            return self._toggle_volume_mute()
        if func != "unused":
            self._log("(unknown function %r) %s" % (func, key))
            return False
        self._log("(unused) %s" % key)
        return False

    def flush(self):
        dirty = False
        a = self._acc
        if a["vfo_main"]:
            dirty |= self._tune(a["vfo_main"])
        if a["knob_left"]:
            dirty |= self._adj_wpm(a["knob_left"])
        if a["knob_center"]:
            dirty |= self._adj_volume(a["knob_center"])
        if a["knob_right"]:
            dirty |= self._adj_drive(a["knob_right"])
        for k in a:
            a[k] = 0
        return dirty

    # ----- mus (öra/TX-placering) ----------------------------------------------
    def set_ear(self, cell):
        """Vänsterklick: sätt örats hemvist till cellen (radio_idx, rx_idx, vfo)."""
        if not self._valid(cell):
            return False
        self.station.ear = cell
        self._log("Ear: %s" % self._cellname(cell))
        return True

    def set_tx(self, cell):
        """Högerklick: sätt TX till cellen. TX-säkerhet: byter vi TX-mottagare (radio
        eller RX1<->RX2 på samma radio), städa den vi lämnar: slå av dess TUNE (på det
        gamla transceiver-indexet) så bara en mottagare kan sända, och slå av dess split
        så split bara ligger kvar på den RX som faktiskt är TX. Sätter sedan split efter
        nya TX-VFO:n (VFO B = split på, radion nycklar på B)."""
        if not self._valid(cell):
            return False
        old = self.station.tx
        if (cell[0], cell[1]) != (old[0], old[1]):
            old_radio = self.station.radios[old[0]]
            old_rx = old_radio.rx[old[1]]
            if old_radio.tune:
                old_radio.tune = False
                self._send(old_radio.key, "TUNE:%d,false;" % old[1])
                self._log("TUNE off on %s RX%d (TX moved)" % (old_radio.name, old[1] + 1))
            if old_rx.split:
                old_rx.split = False
                self._send(old_radio.key, "SPLIT_ENABLE:%d,false;" % old[1])
                self._log("Split off on %s RX%d (TX moved)" % (old_radio.name, old[1] + 1))
        self.station.tx = cell
        self.apply_tx()
        self._log("TX: %s" % self._cellname(cell))
        return True

    def apply_tx(self):
        """Sätt sändnings-VFO på TX-mottagaren via SPLIT_ENABLE. arg1 = transceiver-index,
        som på SunSDR2DX är mottagar-index (transceiver 0 = RX1, 1 = RX2). TX på VFO B ->
        split på, TX på VFO A -> split av. Idempotent (körs vid TX-byte och anslutning).

        Vid av->på-övergången flyttar ExpertSDR3 själv VFO B till VFO A + offset (1 kHz CW
        / 5 kHz SSB). Vi bokför VFO B före split och återställer den direkt efteråt så
        operatörens satta TX-frekvens bevaras. Eko-spärr gör att radions auto-hopp-eko
        ignoreras (ingen flimmer i displayen)."""
        radio = self.station.tx_radio()
        rxi = self.station.tx[1]
        rx = radio.rx[rxi]
        want = self.station.tx[2] == 1
        was = rx.split
        rx.split = want
        if want and not was:
            stored = rx.freq[1]
            self._send(radio.key, "SPLIT_ENABLE:%d,true;" % rxi)
            if stored is not None:
                self._guard(("vfo", radio.key, rxi, 1), block_any=True)
                self._send(radio.key, "VFO:%d,1,%d;" % (rxi, stored))
                rx.freq[1] = stored
        else:
            self._send(radio.key, "SPLIT_ENABLE:%d,%s;" % (rxi, "true" if want else "false"))

    def split_offset(self, radio_key, rxi):
        """Splitknapp (under frekvenserna i RX-panelen): flyttar TX-markören
        till mottagarens VFO B (annars sänder radion inte där trots
        SPLIT_ENABLE - TX-cellen är sanningskällan, set_tx() sköter
        TX-säkerhet/städning av föregående TX-cell och slår på split via
        apply_tx()), och sätter sedan VFO B = VFO A + mode-beroende offset
        (5 kHz SSB, annars 1 kHz), som skriver över det värde apply_tx precis
        återställde. Öra/encoder-fokus rörs inte."""
        ri = self.station.index_of(radio_key)
        if ri < 0 or rxi >= self.station.radios[ri].rx_count:
            return False
        radio = self.station.radios[ri]
        rx = radio.rx[rxi]
        # Andra trycket (split redan på FÖR DEN HÄR knappens TX-cell) återställer:
        # TX tillbaka till VFO A, split av, via samma TX-säkerhet som set_tx().
        if rx.split and self.station.tx == (ri, rxi, 1):
            self.set_tx((ri, rxi, 0))
            self._log("Split off: %s RX%d" % (radio.name, rxi + 1))
            return True
        base = rx.freq[0]
        if base is None:
            return False
        offset = 5000 if (rx.mode or "") in ("USB", "LSB") else 1000
        new_b = int(base) + offset
        self.set_tx((ri, rxi, 1))
        rx.freq[1] = new_b
        self._guard(("vfo", radio.key, rxi, 1), new_b)
        self._send(radio.key, "VFO:%d,1,%d;" % (rxi, new_b))
        self._log("Split: %s RX%d VFO B = %s (+%d Hz)" % (
            radio.name, rxi + 1, new_b, offset))
        return True

    def _valid(self, cell):
        ri, rxi, vfo = cell
        return 0 <= ri < len(self.station.radios) and rxi < self.station.radios[ri].rx_count \
            and vfo in (0, 1)

    def _cellname(self, cell):
        r = self.station.radios[cell[0]]
        return "%s RX%d VFO %s" % (r.name, cell[1] + 1, "AB"[cell[2]])

    # ----- utgående ------------------------------------------------------------
    def _step_per_detent(self, n):
        if n <= ACCEL_N0:
            return 1.0
        return 1.0 + ((n - ACCEL_N0) / ACCEL_SCALE) ** ACCEL_EXP

    def _tune(self, delta):
        """Stäm den cell encodern står på (öra eller TX)."""
        ri, rxi, vfo = self.station.encoder_cell()
        radio = self.station.radios[ri]
        rx = radio.rx[rxi]
        base = rx.freq[vfo]
        if base is None:
            return False
        n = abs(delta)
        sign = 1 if delta > 0 else -1
        per = min(BASE_STEP * self._step_per_detent(n), ACCEL_PER_MAX)
        new = int(base) + sign * int(round(n * per))
        rx.freq[vfo] = new
        self._guard(("vfo", radio.key, rxi, vfo), new)
        self._send(radio.key, "VFO:%d,%d,%d;" % (rxi, vfo, new))
        return True

    def _adj_wpm(self, delta):
        radio = self.station.tx_radio()
        cur = radio.wpm if radio.wpm is not None else 25
        radio.wpm = clamp(int(cur) + delta * WPM_STEP, WPM_MIN, WPM_MAX)
        self._guard(("wpm", radio.key), radio.wpm)
        self._send(radio.key, "CW_KEYER_SPEED:%d;" % radio.wpm)
        self._send(radio.key, "CW_MACROS_SPEED:%d;" % radio.wpm)
        return True

    def _set_wpm(self, value):
        radio = self.station.tx_radio()
        radio.wpm = value
        self._guard(("wpm", radio.key), value)
        self._send(radio.key, "CW_KEYER_SPEED:%d;" % value)
        self._send(radio.key, "CW_MACROS_SPEED:%d;" % value)
        self._log("WPM = %d" % value)
        return True

    def _adj_drive(self, delta):
        radio = self.station.tx_radio()
        if radio.drive is None:
            return False
        radio.drive = clamp(int(radio.drive) + delta * DRIVE_STEP, 0, 100)
        self._guard(("drive", radio.key), radio.drive)
        self._send(radio.key, "DRIVE:%d,%d;" % (self.station.tx[1], radio.drive))
        return True

    def _adj_volume(self, delta):
        ri, rxi, _ = self.station.ear
        radio = self.station.radios[ri]
        if radio.volume_muted:
            return False
        radio.volume = clamp(int(radio.volume) + delta * VOLUME_STEP, -60, 0)
        self._guard(("volume", radio.key), radio.volume)
        self._send(radio.key, "VOLUME:%d;" % radio.volume)
        return True

    def _toggle_volume_mute(self):
        ri, rxi, _ = self.station.ear
        radio = self.station.radios[ri]
        if radio.volume_muted:
            radio.volume_muted = False
            radio.volume = radio._volume_before_mute
            self._send(radio.key, "VOLUME:%d;" % radio.volume)
            self._log("Volume unmute: %s (%d dB)" % (radio.name, radio.volume))
        else:
            radio._volume_before_mute = radio.volume
            radio.volume_muted = True
            self._send(radio.key, "VOLUME:-60;")
            self._log("Volume mute: %s" % radio.name)
        return True

    def _send_macro_text(self, text):
        radio = self.station.tx_radio()
        self._send(radio.key, "CW_MACROS:%d,%s;" % (self.station.tx[1], text))
        self._log("CW: %s (sending on %s RX%d)" % (text, radio.name, self.station.tx[1] + 1))

    def _toggle_tune(self):
        radio = self.station.tx_radio()
        rxi = self.station.tx[1]
        radio.tune = not radio.tune
        self._send(radio.key, "TUNE:%d,%s;" % (rxi, "true" if radio.tune else "false"))
        self._log("TUNE %s%s" % ("ON" if radio.tune else "off",
                                 " - %s RX%d SENDING" % (radio.name, rxi + 1) if radio.tune else ""))
        return True

    def _toggle_rit_enable(self):
        ri, rxi, _ = self.station.ear
        radio = self.station.radios[ri]
        rx = radio.rx[rxi]
        rx.rit_on = not rx.rit_on
        self._send(radio.key, "RIT_ENABLE:%d,%s;" % (rxi, "true" if rx.rit_on else "false"))
        return True

    # ----- prime (fråga radion vid anslutning) ---------------------------------
    def prime(self, radio_key):
        radio = self.station.by_key(radio_key)
        if radio is None:
            return
        cmds = ["CW_KEYER_SPEED;", "CW_MACROS_SPEED;", "MON_VOLUME;", "VOLUME;"]
        for rxi in range(radio.rx_count):
            cmds += ["VFO:%d,0;" % rxi, "VFO:%d,1;" % rxi,
                     "RIT_OFFSET:%d;" % rxi, "RIT_ENABLE:%d;" % rxi,
                     "RX_CHANNEL_ENABLE:%d,1;" % rxi,
                     "DRIVE:%d;" % rxi, "TUNE:%d;" % rxi, "SPLIT_ENABLE:%d;" % rxi,
                     "MODULATION:%d;" % rxi]
        for c in cmds:
            self._send(radio_key, c)

    # ----- inkommande TCI (modell + eko-spärr) ---------------------------------
    def handle_tci(self, ev):
        radio = self.station.by_key(ev["radio"])
        if radio is None or ev.get("kind") != "cmd":
            return False
        name, args = ev["name"], ev["args"]
        try:
            if name == "VFO" and len(args) >= 3 and args[0] in ("0", "1") and args[1] in ("0", "1"):
                rxi, vfo = int(args[0]), int(args[1])
                incoming = int(args[2])
                if rxi < radio.rx_count and not self._echoed(
                        ("vfo", radio.key, rxi, vfo), incoming):
                    radio.rx[rxi].freq[vfo] = incoming
                    return True
            elif name in ("CW_KEYER_SPEED", "CW_MACROS_SPEED") and len(args) >= 1:
                incoming = int(float(args[0]))
                if not self._echoed(("wpm", radio.key), incoming):
                    radio.wpm = incoming
                    return True
            elif name == "MON_VOLUME" and len(args) >= 1:
                incoming = float(args[0])
                if not self._echoed(("monvol", radio.key), incoming):
                    self._mon_volume[radio.key] = incoming
                    return True
            elif name == "VOLUME" and len(args) >= 1:
                incoming = int(float(args[0]))
                if not self._echoed(("volume", radio.key), incoming):
                    if not radio.volume_muted:
                        radio.volume = incoming
                    return True
            elif name == "DRIVE" and len(args) >= 2 and args[0] in ("0", "1"):
                incoming = int(float(args[1]))
                if not self._echoed(("drive", radio.key), incoming):
                    radio.drive = incoming
                    return True
            elif name == "RIT_OFFSET" and len(args) >= 2 and args[0] in ("0", "1"):
                rxi = int(args[0])
                incoming = int(float(args[1]))
                if rxi < radio.rx_count and not self._echoed(
                        ("rit", radio.key, rxi), incoming):
                    radio.rx[rxi].rit_offset = incoming
                    return True
            elif name == "RIT_ENABLE" and len(args) >= 2 and args[0] in ("0", "1"):
                rxi = int(args[0])
                if rxi < radio.rx_count:
                    radio.rx[rxi].rit_on = args[1].lower() == "true"
                    return True
            elif name == "SPLIT_ENABLE" and len(args) >= 2 and args[0] in ("0", "1"):
                rxi = int(args[0])
                if rxi < radio.rx_count:
                    on = args[1].lower() == "true"
                    radio.rx[rxi].split = on  # per mottagare
                    # Håll TX-cellens VFO i synk om split ändras utifrån (t.ex.
                    # ExpertSDR3s egen splitknapp på radion) - bara om den här
                    # mottagaren redan ÄR TX-målet, annars rör vi inte TX-fokus.
                    ri = self.station.index_of(radio.key)
                    if (self.station.tx[0], self.station.tx[1]) == (ri, rxi):
                        self.station.tx = (ri, rxi, 1 if on else 0)
                    return True
            elif name == "RX_CHANNEL_ENABLE" and len(args) >= 3 and args[0] in ("0", "1") and args[1] == "1":
                rxi = int(args[0])
                if rxi < radio.rx_count:
                    radio.rx[rxi].vfob_on = args[2].lower() == "true"
                    return True
            elif name == "TUNE" and len(args) >= 2 and args[0] in ("0", "1"):
                radio.tune = args[1].lower() == "true"
                return True
            elif name == "MODULATION" and len(args) >= 2 and args[0] in ("0", "1"):
                rxi = int(args[0])
                if rxi < radio.rx_count:
                    radio.rx[rxi].mode = args[1].upper()
                    return True
        except (ValueError, IndexError, KeyError):
            return False
        return False
