#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tcisync.py - TCISync (by SM5K). Cockpit med öra/TX-mus-modell (DESIGN §9).

KOPIERAD FRÅN sunsync 2026-08-03 som startpunkt för generaliseringen (DESIGN.md §10 i sunsync).
Denna fil är ännu INTE generaliserad - hårdkodad Ramnäs/Blecket-modell, hårdkodad
knappmappning m.m. lever kvar tills auto-detektion/config-UI byggs.

Två markörer sätts med musen på frekvenscellerna: vänsterklick = öra (grön ram, du
hör), högerklick = TX (orange ram, du sänder). Encodern stämmer den ena, E-Coder-
knapp 13 växlar öra/TX. Knapp 14 = tillfällig TX-lyssning (öra följer TX live), knapp
12 dess läge (TX / TX+RX). "Hörs nu"-fyllning visar var ljudet kommer från just nu.
Ljud (audio.py), styrning (encoder_router.py) och synk (sync.py) hänger på stationen.

Trim-läge: `python sunsync.py --tweak` visar reglage för textstorlek/cellmått.
"""

import sys
import os
import json
import queue
import subprocess
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser

import customtkinter as ctk

import ee_theme as th
import win_snap
from station import auto_station
from tci_link import TciLink
from encoder_router import EncoderRouter
from audio import AudioSwitch
from sync import Sync
from tci_bridge import TciBridge, DEFAULT_PORT as BRIDGE_DEFAULT_PORT
from bridge_router import Mapping
from bridge_control import ControlServer, DEFAULT_PORT as CONTROL_DEFAULT_PORT
from ecoder_hid import EcoderHid
from ecoder_decode import Decoder, load_mapping
from ecoder_leds import LedController

VERSION = "0.22.0"
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


class Cockpit:
    def __init__(self, root, tweak=False, classic=False, ontop=True):
        self.root = root
        self.tweak = tweak
        self.classic = classic
        self.ontop = ontop
        root.title("TCISync (by SM5K)  v%s" % VERSION)
        root.configure(fg_color=th.color("bg_app"))

        self.station = auto_station()
        station_names = load_settings().get("station_names", {})
        for r in self.station.radios:
            if r.key in station_names:
                r.name = station_names[r.key]
        self.slots = _build_slots(self.station)
        self.events = queue.Queue()
        self.links = {}
        self.cells = {}         # (radio_key, rx_idx, vfo) -> CTkLabel (frekvenstext)
        self.cellouter = {}     # -> CTkFrame (öra-ring + storlek)
        self.cellinner = {}     # -> CTkFrame (TX-ring + hörs-nu-fyllning)
        self.dots = []          # (dot_widget, radio_key) per panel
        self._wheel_cell = {}    # widget (outer/inner/lbl) -> (radio_key, rx_idx, vfo), för scrollhjulet
        self._freq_char_w = None  # tecken-bredd (px) i f_freq - cachad vid första scrollet

        # Render-cache: senast satta värde per widget så .configure bara körs vid ändring
        # (ctk .configure ritar om en canvas varje gång -> annars omritningsstorm).
        self._cell_text = {}
        self._outer_bc = {}
        self._inner_bc = {}
        self._inner_fg = {}
        self._dot_col = {}
        # Färgtokens en gång (undvik dict-uppslag i heta render-loopen).
        self._c_inv = th.color("bg_control")
        self._c_green = th.color("accent_green")
        self._c_green_b = th.color("accent_green_bright")
        self._c_orange = th.color("accent_orange")
        self._c_orange_b = th.color("accent_orange_bright")
        self._c_txred = th.color("status_tx")
        self._c_heard = th.color("bg_heard")
        self._c_conn = th.color("status_connected")
        self._c_disc = th.color("status_disconnected")

        self.scales = {"freq": FREQ_SCALE, "head": HEAD_SCALE, "status": STATUS_SCALE}
        self.cell_w = CELL_W
        self.cell_h = CELL_H
        self._scalable_fonts = []   # (CTkFont, bas-px, grupp)

        self.f_freq = self._reg_font("freq_main", "freq")
        self.f_head = self._reg_font("body", "head", bold=True)
        # "status"-gruppen skalar nu synk-stripan (kryssrutor + knapp), statusraden borttagen.
        self.f_status = self._reg_font("label", "status")
        self.f_btn = self._reg_font("small", "status", bold=True)

        cw_cfg = dict(DEFAULT_CW_SETTINGS)
        cw_cfg.update(load_settings().get("cw", {}))
        panel_map_cfg = load_settings().get("panel_map", {})
        led_cfg = load_settings().get("leds", {})
        self.router = EncoderRouter(
            self.station, self.links, log=self._log,
            cw_settings=cw_cfg, panel_map=panel_map_cfg,
            led_map=led_cfg.get("map"), backlight_on=led_cfg.get("backlight"))
        self.leds = None   # sätts i _start_hid() om E-Codern öppnas
        self.audio = AudioSwitch(self.station, self.links, log=self._log)
        self.sync = Sync(self.station, self.links, log=self._log)
        self._last_audible = self._audible_key()

        self._build_ui()

        for r in self.station.radios:
            link = TciLink(r.key, r.ip, r.port, self.events, name=r.name)
            self.links[r.key] = link
            link.start()

        bridge_cfg = load_settings().get("wsjt_bridge", {})
        self._bridge_rx = bridge_cfg.get("rx", "ramnas")      # "ramnas" (lokala radions RX2) | "blecket" (fjärrradions RX1)
        self._bridge_tx = bridge_cfg.get("tx", "ramnas")
        self.bridge = TciBridge(self.links, self._bridge_mapping, log=self._bridge_log,
                                port=int(bridge_cfg.get("port", BRIDGE_DEFAULT_PORT)),
                                echo_guard=self.sync.ignore_echo)
        if bridge_cfg.get("enabled"):
            self._bridge_start()
        self.control = ControlServer(self._control_request, log=self._bridge_log,
                                     port=int(bridge_cfg.get("control_port", CONTROL_DEFAULT_PORT)))
        if bridge_cfg.get("control"):
            self._control_start()

        self._start_hid()

        self._update_available_tag = None
        root.after(20, self._pump)
        root.after(0, self._update_check_tick)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh()

    # ----- auto-uppdatering (samma mönster som jtspots) -------------------------
    def _update_check_tick(self):
        threading.Thread(target=self._check_for_update, daemon=True).start()
        self.root.after(UPDATE_CHECK_MS, self._update_check_tick)

    @staticmethod
    def _version_tuple(v):
        v = v.strip().lstrip("vV")
        parts = []
        for p in v.split("."):
            try:
                parts.append(int(p))
            except ValueError:
                break
        return tuple(parts)

    def _check_for_update(self):
        """Frågar bara GitHubs 'senaste release'-tagg, laddar aldrig ner något
        själv. Tyst vid nätverksfel/inget repo/inga releaser än."""
        try:
            req = urllib.request.Request(UPDATE_CHECK_URL, headers={"User-Agent": "TCISync"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            tag = data.get("tag_name", "")
            if self._version_tuple(tag) > self._version_tuple(VERSION):
                self.root.after(0, lambda t=tag: self._show_update_available(t))
        except Exception:
            pass

    def _show_update_available(self, tag):
        if self._update_available_tag == tag:
            return
        self._update_available_tag = tag
        self.update_btn.configure(text="Update: %s" % tag)
        self.update_btn.pack(side="right", padx=(0, 6), pady=4)
        self._log("New version %s available - click 'Update: %s' to install" % (tag, tag))

    def _launch_updater(self):
        """Bara meningsfullt i en fryst .exe (se tcisync_updater.py), loggar
        och avbryter annars (t.ex. vid `python tcisync.py` under utveckling)."""
        if not getattr(sys, "frozen", False):
            self._log("Update is only available in the .exe build")
            return
        updater = os.path.join(_BASE_DIR, UPDATER_NAME)
        if not os.path.exists(updater):
            self._log("%s not found in the program folder" % UPDATER_NAME)
            return
        try:
            subprocess.Popen([updater, "--update"], cwd=_BASE_DIR)
        except Exception as e:
            self._log("Could not launch updater: %s" % e)
            return
        self._on_close()

    def _log(self, text):
        print(text, flush=True)

    def _reg_font(self, role, group, bold=None):
        base = th.FONT.get(role, th.FONT["body"])
        fo = th.font(role, bold=bold, scale=self.scales[group])
        self._scalable_fonts.append((fo, base, group))
        return fo

    def _audible_key(self):
        return tuple(sorted(self.station.audible_cells()))

    def _key(self, cell):
        """(radio_idx, rx_idx, vfo) -> cell-nyckel (radio_key, rx_idx, vfo)."""
        return (self.station.radios[cell[0]].key, cell[1], cell[2])

    # ----- HID (E-Coder) -------------------------------------------------------
    def _start_hid(self):
        self.hid = None
        self.stop_flag = threading.Event()
        self.mapping, _ = load_mapping()
        self.decoder = Decoder(self.mapping)
        hid = EcoderHid()
        try:
            hid.open()
        except OSError as e:
            self._log("E-Coder not opened: %s" % e)
            return
        self.hid = hid
        self.leds = LedController(hid)
        threading.Thread(target=self._reader_loop, daemon=True).start()

    def _hid_log(self, text):
        """HID connection events go to tcisync_hid.log (pythonw has no console)."""
        line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
        self._log(line)
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "tcisync_hid.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _reconnect_hid(self):
        """Retry opening the E-Coder every HID_RETRY_S until it works or the
        app closes, then hand the new handle to the LED controller."""
        old = self.hid
        try:
            old.close()
        except Exception:
            pass
        while not self.stop_flag.wait(HID_RETRY_S):
            hid = EcoderHid()
            try:
                hid.open()
            except OSError as e:
                self._hid_log("E-Coder reopen failed: %s" % e)
                continue
            self.hid = hid
            if self.leds:
                self.leds.rebind(hid)
            self._hid_log("E-Coder reconnected")
            return

    def _reader_loop(self):
        # The panel streams ~500 reports/s even at rest, so HID_SILENCE_S
        # without a report means the handle is dead.
        last_rx = time.monotonic()
        while not self.stop_flag.is_set():
            try:
                r = self.hid.read(150)
                if r is None and time.monotonic() - last_rx > HID_SILENCE_S:
                    raise OSError("no reports for %d s" % HID_SILENCE_S)
            except OSError as e:
                if self.stop_flag.is_set():
                    break
                self._hid_log("E-Coder connection lost: %s" % e)
                self._reconnect_hid()
                last_rx = time.monotonic()
                continue
            if r is not None:
                last_rx = time.monotonic()
                for key, kind, value in self.decoder.feed(r):
                    self.events.put({"src": "panel", "key": key,
                                     "kind": kind, "value": value})

    # ----- UI ------------------------------------------------------------------
    def _build_ui(self):
        pad = th.METRICS["panel_pad"]
        wrap = ctk.CTkFrame(self.root, fg_color=th.color("bg_app"))
        wrap.pack(fill="both", expand=True, padx=pad, pady=(pad - 4, 0))
        self._bind_drag(wrap)
        # Högerklicksmenyn: EN bindning på rotfönstret i stället för en
        # per-widget-lista (se _bind_drag). Tkinter propagerar Button-3 till
        # root för alla widgetar som inte redan binder den själva, så
        # frekvenscellernas egen Button-3 (TX-markör, se _rx_panel) fortsätter
        # fungera som idag.
        self.root.bind("<Button-3>", self._on_win_menu)
        # Scrollhjul över en frekvenscell: EN global bindning (bind_all krävs
        # för tillförlitlig leverans på Windows oavsett vilken av de tre
        # nästlade cell-widgetsen som råkar ha fokus), cellen slås upp direkt
        # via event.widget i self._wheel_cell (se _rx_panel/_on_wheel).
        self.root.bind_all("<MouseWheel>", self._on_wheel)
        for i, slot in enumerate(self.slots):
            self._rx_panel(wrap, slot, i)
            wrap.columnconfigure(i, weight=1)
        wrap.rowconfigure(0, weight=1)

        self._sync_bar(pad)

        if self.tweak:
            self._tweak_bar(pad)

    def _rx_panel(self, parent, slot, col):
        M = th.METRICS
        radio_key, rxi, title = slot
        radio = self.station.by_key(radio_key)
        f = ctk.CTkFrame(parent, fg_color=th.color("bg_surface"),
                         corner_radius=M["radius"], border_width=1,
                         border_color=th.color("border"))
        f.grid(row=0, column=col, sticky="nsew", padx=M["gap"] + 2, pady=0)
        self._bind_drag(f)

        head = ctk.CTkFrame(f, fg_color="transparent")
        head.pack(fill="x", padx=M["panel_pad"], pady=(5, 2))
        self._bind_drag(head)
        dot = ctk.CTkLabel(head, text="●", text_color=th.color("status_disconnected"),
                           font=self.f_head)
        dot.pack(side="left")
        self._bind_drag(dot)
        self.dots.append((dot, radio_key))
        place = "local" if radio.place == "local" else "remote"
        titlbl = ctk.CTkLabel(head, text="%s   %s · %s" % (title, radio.name, place),
                              text_color=th.color("text_primary"), font=self.f_head)
        titlbl.pack(side="left", padx=5)
        self._bind_drag(titlbl)

        grid = ctk.CTkFrame(f, fg_color="transparent")
        grid.pack(fill="x", padx=M["panel_pad"], pady=(0, M["panel_pad"] - 4))
        # Storleken styrs av frekvens-ETIKETTENS width/height (customtkinter respekterar
        # det, till skillnad från CTkFrame + grid_propagate(False) som ignoreras här).
        # Ramarna krymper runt etiketten (ingen fyllning), så cellen blir exakt cell_w x
        # cell_h. RX-titeln bredvid borttagen; rubriken står kvar ovanför i panelhuvudet.
        for vfo in (0, 1):
            outer = ctk.CTkFrame(grid, fg_color=th.color("bg_control"),
                                 corner_radius=M["radius"], border_width=2,
                                 border_color=th.color("bg_control"))
            outer.grid(row=0, column=vfo, padx=M["gap"], pady=M["gap"])
            inner = ctk.CTkFrame(outer, fg_color=th.color("bg_control"),
                                 corner_radius=max(1, M["radius"] - 1), border_width=2,
                                 border_color=th.color("bg_control"))
            inner.pack(padx=2, pady=2)      # luft mellan öra-ringen (yttre) och TX-ringen (inre)
            # Etikettmarginal så inre ramens kant (TX-ring) + fg ("hörs nu") syns runt texten.
            lbl = ctk.CTkLabel(inner, text=fmt_freq(None),
                               width=self.cell_w, height=self.cell_h,
                               fg_color="transparent",
                               text_color=th.color("text_freq"), font=self.f_freq)
            lbl.pack(padx=6, pady=2)
            grid.columnconfigure(vfo, weight=1)
            key = (radio_key, rxi, vfo)
            self.cells[key] = lbl
            self.cellouter[key] = outer
            self.cellinner[key] = inner
            for w in (outer, inner, lbl):
                w.bind("<Button-1>", lambda e, k=key: self._cell_click(k, "ear"))
                # "break" hindrar eventet från att fortsätta till root-bindningen
                # (_on_win_menu, se _build_ui) - annars öppnas fönstermenyn OCKSÅ
                # varje gång en cells TX-markör sätts med högerklick.
                w.bind("<Button-3>", lambda e, k=key: self._cell_click(k, "tx") or "break")
                # Scrollhjul: widget -> cell-uppslagning (self._wheel_cell), läst
                # via event.widget i _on_wheel (en enda global <MouseWheel>-
                # bindning, se _build_ui). event.widget är ALLTID den widget som
                # faktiskt tog emot händelsen - ingen separat hover-state att
                # hålla synkad mellan Enter/Leave och scrollet.
                # OBS: CustomTkinters .bind() delegerar internt till en dold
                # self._canvas (CTkCanvas) - se ctk_frame.py/ctk_label.py -
                # så det ÄR _canvas-instansen som faktiskt tar emot händelsen,
                # inte outer/inner/lbl-wrapperobjekten. Nyckeln måste därför
                # vara w._canvas, annars missar uppslagningen alltid.
                self._wheel_cell[w._canvas] = key
            # CTkLabel lägger dessutom en egen, obunden tkinter.Label (self._label,
            # se ctk_label.py) OVANPÅ sin canvas, centrerad över exakt den yta där
            # siffrorna syns - precis där man naturligt scrollar. Ingen egen
            # .bind() behövs (root.bind_all fångar den ändå, se _build_ui) -
            # den behöver bara finnas i uppslagningstabellen, annars missar
            # scrollet varje gång pekaren står på själva siffrorna i stället
            # för cellens bakgrund/kant.
            self._wheel_cell[lbl._label] = key

        split_btn = ctk.CTkButton(f, text="Split", font=self.f_btn,
                                  height=th.METRICS["btn_height"],
                                  corner_radius=M["radius"],
                                  fg_color=th.color("btn_idle_bg"),
                                  hover_color=th.color("btn_hover_bg"),
                                  text_color=th.color("btn_text"),
                                  command=lambda rk=radio_key, i=rxi: self._on_split(rk, i))
        split_btn.pack(fill="x", padx=M["panel_pad"], pady=(0, M["panel_pad"] - 4))

    def _cell_click(self, key, which):
        radio_key, rxi, vfo = key
        cell = (self.station.index_of(radio_key), rxi, vfo)
        dirty = self.router.set_ear(cell) if which == "ear" else self.router.set_tx(cell)
        if dirty:
            self._refresh()

    def _digit_step_hz(self, key, lbl, event):
        """Räknar ut platsvärdet (i Hz) för siffran under muspekaren i en
        frekvenscell, t.ex. "14.074.500" -> 100 om pekaren står på "5".
        f_freq är Consolas (monospace, se ee_theme.py FONT["freq_family"]) -
        varje tecken (siffra ELLER punkt) har exakt samma bredd, vilket gör
        pixel->tecken-mappningen till en ren division i stället för en
        kumulativ font.measure()-sökning över delsträngar (hade krävts för
        ett proportionellt typsnitt).

        Läser text_root_x relativt lbl (inte event.widget) så resultatet blir
        korrekt oavsett vilken av de tre nästlade widgetsen (outer/inner/lbl)
        som faktiskt tog emot händelsen."""
        text = self._cell_text.get(key) or lbl.cget("text")
        if not text or not any(c.isdigit() for c in text):
            return None
        if self._freq_char_w is None:
            self._freq_char_w = self.f_freq.measure("0")
        char_w = self._freq_char_w
        if char_w <= 0:
            return None
        text_w = len(text) * char_w
        text_start = (lbl.winfo_width() - text_w) / 2.0
        rel_x = event.x_root - lbl.winfo_rootx()
        idx = int((rel_x - text_start) // char_w)
        idx = max(0, min(len(text) - 1, idx))
        while idx > 0 and not text[idx].isdigit():
            idx -= 1
        if not text[idx].isdigit():
            return None
        digits_right = sum(1 for c in text[idx + 1:] if c.isdigit())
        return 10 ** digits_right

    def _on_wheel(self, event):
        """Global <MouseWheel>-lyssnare (bunden en gång, se _build_ui).
        event.widget + self._wheel_cell ger cellen direkt, inget separat
        hover-tillstånd att hålla synkat. Positionsberoende steglängd, INGEN
        acceleration (designbeslut) - scrollriktningen avgör bara tecknet,
        siffran under pekaren avgör stegets storlek. Ear/tx/split rörs inte,
        egen tuningväg (tune_cell_by) skild från E-Coderns _tune()."""
        key = self._wheel_cell.get(event.widget)
        if key is None:
            return
        lbl = self.cells.get(key)
        if lbl is None:
            return
        step_hz = self._digit_step_hz(key, lbl, event)
        if step_hz is None:
            return
        radio_key, rxi, vfo = key
        cell = (self.station.index_of(radio_key), rxi, vfo)
        step = step_hz if event.delta > 0 else -step_hz
        if self.router.tune_cell_by(cell, step):
            self._refresh()

    def _on_split(self, radio_key, rxi):
        if self.router.split_offset(radio_key, rxi):
            self._refresh()

    # ----- synk-stripa ---------------------------------------------------------
    def _sync_bar(self, pad):
        bar = ctk.CTkFrame(self.root, fg_color=th.color("bg_surface"),
                           corner_radius=th.METRICS["radius"])
        bar.pack(fill="x", padx=pad, pady=(4, 0))
        self._bind_drag(bar)
        self.sync_btn = ctk.CTkButton(bar, text="Sync off", font=self.f_btn,
                                      height=th.METRICS["btn_height"], width=90,
                                      corner_radius=th.METRICS["radius"],
                                      fg_color=th.color("btn_idle_bg"),
                                      hover_color=th.color("btn_hover_bg"),
                                      text_color=th.color("btn_text"),
                                      command=self._on_sync_toggle)
        self.sync_btn.pack(side="left", padx=(8, 10), pady=4)
        params = [("vfo", "Frequency", "sync_vfo"), ("mod", "Mode", "sync_mod"),
                  ("vfob", "VFO B", "sync_vfob"), ("rit", "RIT", "sync_rit"),
                  ("filter", "Filter", "sync_filter")]
        self._sync_vars = {}
        for key, label, attr in params:
            var = ctk.BooleanVar(value=getattr(self.sync, attr))
            cb = ctk.CTkCheckBox(bar, text=label, variable=var,
                                 font=self.f_status, checkbox_width=16, checkbox_height=16,
                                 text_color=th.color("text_secondary"),
                                 fg_color=th.color("accent_blue"),
                                 hover_color=th.color("accent_blue_bright"),
                                 command=lambda k=key, a=attr: self._on_sync_param(k, a))
            cb.pack(side="left", padx=6, pady=4)
            self._sync_vars[key] = var

        # Rent ASCII-tecken ("alltid överst") i stället för emoji - emoji
        # ritas av Windows eget färgemoji-typsnitt och lyder inte text_color.
        self.ontop_btn = ctk.CTkButton(bar, text="^", font=self.f_btn,
                                       height=th.METRICS["btn_height"], width=28,
                                       corner_radius=th.METRICS["radius"],
                                       fg_color=th.color("btn_idle_bg") if self.ontop
                                       else th.color("accent_blue"),
                                       hover_color=th.color("accent_blue_bright"),
                                       text_color=th.color("btn_text"),
                                       command=self._on_ontop_toggle)
        self.ontop_btn.pack(side="right", padx=(0, 8), pady=4)

        # Skapas men packas INTE in - visas bara om _show_update_available
        # faktiskt hittar en nyare version (se _check_for_update), samma
        # mönster som jtspots "Uppdatering"-knapp.
        # WSJT-bron: en knapp per plats (grön RX, röd TX, orange RX+TX, grå av) och två knappar som
        # kopierar en plats frekvens till den andra. Visas när bron är påslagen, packas först av
        # _bridge_ui_tick. Pricken i etiketten: grå utan klient, blå när WSJT-X är ansluten.
        self.bridge_frame = ctk.CTkFrame(bar, fg_color="transparent")
        self.bridge_label = ctk.CTkLabel(self.bridge_frame, text="WSJT ●", font=self.f_status,
                                         text_color=th.color("text_secondary"))
        self.bridge_label.pack(side="left", padx=(0, 4))
        self.bridge_place_btns = {}
        for place in BRIDGE_PLACES:
            b = ctk.CTkButton(self.bridge_frame, text="", font=self.f_btn, width=64,
                              height=th.METRICS["btn_height"],
                              corner_radius=th.METRICS["radius"],
                              fg_color=th.color("btn_idle_bg"),
                              hover_color=th.color("btn_idle_bg"),
                              text_color=th.color("btn_text_on_accent"),
                              command=lambda p=place: self._on_bridge_place(p))
            b.pack(side="left", padx=2)
            self.bridge_place_btns[place] = b
        ctk.CTkLabel(self.bridge_frame, text="Freq", font=self.f_status,
                     text_color=th.color("text_secondary")).pack(side="left", padx=(8, 2))
        self.bridge_copy_btns = {}
        for src, dst in (("ramnas", "blecket"), ("blecket", "ramnas")):
            b = ctk.CTkButton(self.bridge_frame, text="", font=self.f_btn, width=64,
                              height=th.METRICS["btn_height"],
                              corner_radius=th.METRICS["radius"],
                              fg_color=th.color("btn_idle_bg"),
                              hover_color=th.color("btn_hover_bg"),
                              text_color=th.color("btn_text"),
                              command=lambda s=src, d=dst: self._on_bridge_copy(s, d))
            b.pack(side="left", padx=2)
            self.bridge_copy_btns[(src, dst)] = b
        self._bridge_btn_cache = {}

        self.update_btn = ctk.CTkButton(bar, text="", font=self.f_btn,
                                        height=th.METRICS["btn_height"],
                                        corner_radius=th.METRICS["radius"],
                                        fg_color=th.color("accent_blue"),
                                        hover_color=th.color("accent_blue_bright"),
                                        text_color=th.color("btn_text"),
                                        command=self._launch_updater)

    # ----- WSJT-bro -------------------------------------------------------------
    def _bridge_cells(self):
        """{'ramnas': (radio_key, rx_idx), 'blecket': (radio_key, rx_idx)} efter roll, inte namn:
        Ramnäs = lokala radions RX2, Blecket = fjärrradions RX1. Bron rör aldrig lokala RX1.
        Saknas en radio (eller lokala radions RX2) saknas dess post."""
        cells = {}
        for r in self.station.radios:
            if r.place == "local" and r.rx_count >= 2:
                cells["ramnas"] = (r.key, 1)
            elif r.place == "remote":
                cells["blecket"] = (r.key, 0)
        return cells

    def _bridge_mapping(self):
        c = self._bridge_cells()
        rx, tx = c[self._bridge_rx], c[self._bridge_tx]
        return Mapping(rx[0], rx[1], tx[0], tx[1])

    def _bridge_station_name(self, cell_id):
        key = self._bridge_cells().get(cell_id, (None,))[0]
        radio = self.station.by_key(key) if key else None
        return radio.name if radio else cell_id

    def _on_bridge_mode(self, rx_id, tx_id):
        if self.bridge.ptt_active:
            self._bridge_log("WSJT bridge: mode change ignored during transmission")
            return
        cells = self._bridge_cells()
        if rx_id not in cells or tx_id not in cells:
            return
        self._bridge_rx, self._bridge_tx = rx_id, tx_id
        d = load_settings()
        cfg = d.get("wsjt_bridge", {})
        cfg.update({"rx": rx_id, "tx": tx_id})
        d["wsjt_bridge"] = cfg
        save_settings(d)
        self._bridge_log("WSJT bridge: RX %s, TX %s" % (rx_id, tx_id))
        self.bridge.on_mapping_changed()
        self._bridge_ui_tick()

    def _bridge_role(self, place):
        rx, tx = self._bridge_rx == place, self._bridge_tx == place
        return "both" if rx and tx else "rx" if rx else "tx" if tx else "off"

    def _on_bridge_place(self, place):
        """Ett klick stegar platsen RX > TX > RX+TX; den andra platsen blir den som blir över."""
        other = BRIDGE_PLACES[1] if place == BRIDGE_PLACES[0] else BRIDGE_PLACES[0]
        rx, tx = {"rx": (other, place), "tx": (place, place),
                  "both": (place, other), "off": (place, place)}[self._bridge_role(place)]
        self._on_bridge_mode(rx, tx)

    def _on_bridge_copy(self, src_id, dst_id):
        """Kopierar frekvensen (och mode) från en plats cell till den andras."""
        if self.bridge.ptt_active:
            self._bridge_log("WSJT bridge: frequency copy ignored during transmission")
            return
        cells = self._bridge_cells()
        if src_id not in cells or dst_id not in cells:
            return
        (src_key, src_rx), (dst_key, dst_rx) = cells[src_id], cells[dst_id]
        ok, text = self.bridge.copy_frequency(src_key, src_rx, dst_key, dst_rx)
        self._bridge_log("WSJT bridge: copy frequency %s > %s: %s%s" % (
            self._bridge_station_name(src_id), self._bridge_station_name(dst_id),
            "" if ok else "refused, ", text))

    def _bridge_set(self, key, widget, **kw):
        """configure() bara när något ändrats (annars blinkar CTkButton vid varje tick)."""
        if self._bridge_btn_cache.get(key) != kw:
            self._bridge_btn_cache[key] = dict(kw)
            widget.configure(**kw)

    def _bridge_log(self, text):
        """Bryggans händelser till tcisync_bridge.log bredvid skriptet (pythonw har ingen konsol)."""
        line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
        self._log(line)
        try:
            with open(os.path.join(_BASE_DIR, "tcisync_bridge.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _bridge_start(self):
        cells = self._bridge_cells()
        if len(cells) < 2:
            self._bridge_log("WSJT bridge: needs a local radio with two receivers and a remote radio")
            return
        if self._bridge_rx not in cells or self._bridge_tx not in cells:
            self._bridge_rx = self._bridge_tx = "ramnas"
        try:
            self.bridge.start()
        except OSError as e:
            self._log("WSJT bridge: cannot listen on port %d: %s" % (self.bridge.port, e))
            return
        self._bridge_ui_tick()

    def _bridge_stop(self):
        self.bridge.stop()
        self._bridge_ui_tick()

    # ----- styrgränssnitt för andra program (bridge_control.py) ----------------------
    def _control_start(self):
        try:
            self.control.start()
        except OSError as e:
            self._bridge_log("Control interface: cannot listen on port %d: %s" % (self.control.port, e))

    def _control_request(self, req):
        """Anropas på servertråden. Själva åtgärden körs på GUI-tråden (Tk är inte trådsäkert):
        servertråden väntar på resultatet, högst 3 s."""
        done, box = threading.Event(), {}

        def run():
            try:
                box["r"] = self._control_exec(req)
            except Exception as e:
                box["r"] = {"ok": False, "error": "internal error: %s" % e}
            finally:
                done.set()

        self.root.after(0, run)
        if not done.wait(3.0):
            return {"ok": False, "error": "GUI busy, try again"}
        return box["r"]

    def _control_companion(self, place):
        """Ramnäs RX1 (mottagare 0 på lokala radion): den styr vilken antenn som är inkopplad, medan
        FT8 går på RX2. Styrgränssnittet håller den på samma frekvens som RX2. Blecket har ingen."""
        cell = self._bridge_cells().get(place)
        return (cell[0], 0) if place == "ramnas" and cell else None

    def _control_follow_companion(self, place, hz):
        comp = self._control_companion(place)
        if comp is None or not hz:
            return ""
        ok, _ = self.bridge.set_companion_dial(comp[0], comp[1], hz)
        return ", RX1 %s" % ("set" if ok else "refused")

    def _control_exec(self, req):
        """GUI-tråden. status | set_place | set_dial."""
        cmd = req.get("cmd")
        if cmd == "status":
            comp = self._control_companion("ramnas")
            return {"ok": True, "version": VERSION, "bridge_running": self.bridge.running,
                    "client_connected": self.bridge.client_connected, "ptt": self.bridge.ptt_active,
                    "rx": self._bridge_rx, "tx": self._bridge_tx, "dial_hz": self.bridge.rx_dial(),
                    "ramnas_rx1_hz": self.bridge.cell_dial(*comp) if comp else None,
                    "switch_audio_ms": self.bridge.switch_stats(),
                    "places": sorted(self._bridge_cells())}
        if not self.bridge.running:
            return {"ok": False, "error": "WSJT bridge is not running"}
        if self.bridge.ptt_active:
            return {"ok": False, "error": "not during transmission"}
        if cmd in ("set_place", "set_rx"):
            place = req.get("place")
            if place not in self._bridge_cells():
                return {"ok": False, "error": "unknown place %r" % (place,)}
            hz = req.get("hz")
            if hz is not None and (not isinstance(hz, int) or isinstance(hz, bool)
                                   or not 100000 <= hz <= 60000000):
                return {"ok": False, "error": "hz must be an integer between 100000 and 60000000"}
            # set_place: RX och TX till platsen. set_rx: bara RX, TX-platsen lämnas orörd.
            self._on_bridge_mode(place, place if cmd == "set_place" else self._bridge_tx)
            if hz is not None:
                self.bridge.set_dial(hz)          # nya RX-cellen, direkt efter bytet (ingen kapplöpning mot ekot)
            note = self._control_follow_companion(place, hz if hz is not None else self.bridge.wsjt_dial())
            if note or hz is not None:
                self._bridge_log("Control interface: %s %s%s%s" % (
                    cmd, place, " %d Hz" % hz if hz is not None else "", note))
            return {"ok": True, "rx": self._bridge_rx, "tx": self._bridge_tx}
        if cmd == "set_dial":
            hz = req.get("hz")
            if not isinstance(hz, int) or isinstance(hz, bool) or not 100000 <= hz <= 60000000:
                return {"ok": False, "error": "hz must be an integer between 100000 and 60000000"}
            ok, text = self.bridge.set_dial(hz)
            if ok:
                text += self._control_follow_companion(self._bridge_rx, hz)
            self._bridge_log("Control interface: set dial %s: %s%s" % (
                self._bridge_station_name(self._bridge_rx), "" if ok else "refused, ", text))
            return {"ok": ok, "text": text} if ok else {"ok": False, "error": text}
        return {"ok": False, "error": "unknown cmd %r" % (cmd,)}

    def _bridge_ui_tick(self):
        """Uppdaterar platsknapparna i synk-stripan, 4 ggr/s så länge bron är på."""
        if getattr(self, "_bridge_tick_id", None) is not None:
            self.root.after_cancel(self._bridge_tick_id)
            self._bridge_tick_id = None
        if not self.bridge.running:
            self.bridge_frame.pack_forget()
            return
        self._bridge_set("dot", self.bridge_label, text_color=th.color("accent_blue_bright")
                         if self.bridge.client_connected else th.color("text_secondary"))
        colors = {"rx": self._c_green, "tx": BRIDGE_TX_LIVE if self.bridge.ptt_active else self._c_txred,
                  "both": BRIDGE_TX_LIVE if self.bridge.ptt_active else self._c_orange,
                  "off": th.color("btn_idle_bg")}
        for place, b in self.bridge_place_btns.items():
            role = self._bridge_role(place)
            self._bridge_set(place, b, text="%s %s" % (self._bridge_station_name(place)[:3],
                                                       BRIDGE_ROLE_TEXT[role]),
                             fg_color=colors[role], hover_color=colors[role])
        for (src, dst), b in self.bridge_copy_btns.items():
            self._bridge_set((src, dst), b, text="%s>%s" % (self._bridge_station_name(src)[:3],
                                                            self._bridge_station_name(dst)[:3]))
        if not self.bridge_frame.winfo_ismapped():
            self.bridge_frame.pack(side="right", padx=(0, 8), pady=4)
        self._bridge_tick_id = self.root.after(250, self._bridge_ui_tick)

    def _on_ontop_toggle(self):
        self.ontop = not self.ontop
        self._apply_ontop()
        d = load_settings()
        d["ontop"] = self.ontop
        save_settings(d)

    def _apply_ontop(self):
        self.root.attributes("-topmost", self.ontop)
        self.ontop_btn.configure(
            fg_color=th.color("btn_idle_bg") if self.ontop else th.color("accent_blue"))

    def _on_sync_toggle(self):
        self.station.sync_mode = not self.station.sync_mode
        on = self.station.sync_mode
        self.sync_btn.configure(
            text="Sync on" if on else "Sync off",
            fg_color=th.color("accent_blue") if on else th.color("btn_idle_bg"),
            hover_color=th.color("accent_blue_bright") if on else th.color("btn_hover_bg"))
        self._log("Sync %s" % ("on" if on else "off"))
        if on:
            self.sync.align()          # snäpp remoten till RX1 direkt vid påslag

    def _on_sync_param(self, key, attr):
        setattr(self.sync, attr, self._sync_vars[key].get())

    # ----- trim-läge -----------------------------------------------------------
    def _tweak_bar(self, pad):
        bar = ctk.CTkFrame(self.root, fg_color=th.color("bg_control"),
                           corner_radius=th.METRICS["radius"])
        bar.pack(fill="x", padx=pad, pady=(4, 0))
        self._sliders = {}
        self._sl_val = {}

        def add(key, label, lo, hi, val, cb):
            row = ctk.CTkFrame(bar, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=2)
            ctk.CTkLabel(row, text=label, width=90, anchor="w",
                         text_color=th.color("text_primary"),
                         font=th.font("small")).pack(side="left")
            s = ctk.CTkSlider(row, from_=lo, to=hi, command=cb)
            s.set(val)
            s.pack(side="left", fill="x", expand=True, padx=6)
            v = ctk.CTkLabel(row, text="", width=52,
                             text_color=th.color("accent_green_bright"),
                             font=th.font("small"))
            v.pack(side="left")
            self._sliders[key] = s
            self._sl_val[key] = v

        add("freq", "Frequency", 0.4, 1.6, self.scales["freq"], lambda v: self._on_font("freq", v))
        add("head", "Panel title", 0.4, 1.6, self.scales["head"], lambda v: self._on_font("head", v))
        add("status", "Status", 0.4, 1.6, self.scales["status"], lambda v: self._on_font("status", v))
        add("cw", "Cell width", 70, 260, self.cell_w, self._on_cw)
        add("ch", "Cell height", 22, 80, self.cell_h, self._on_ch)

        btnrow = ctk.CTkFrame(bar, fg_color="transparent")
        btnrow.pack(fill="x", padx=8, pady=(2, 6))
        self.copy_btn = ctk.CTkButton(btnrow, text="Copy values",
                                      fg_color=th.color("accent_blue"),
                                      hover_color=th.color("accent_blue_bright"),
                                      font=th.font("small"), height=22, width=120,
                                      corner_radius=th.METRICS["radius"],
                                      command=self._copy_values)
        self.copy_btn.pack(side="left")
        self.copy_lbl = ctk.CTkLabel(btnrow, text="", text_color=th.color("text_secondary"),
                                     font=th.font("small"))
        self.copy_lbl.pack(side="left", padx=8)
        self._update_tweak_labels()

    def _rescale_fonts(self, group=None):
        for fo, base, g in self._scalable_fonts:
            if group is None or g == group:
                fo.configure(size=max(1, round(base * self.scales[g])))

    def _resize_cells(self):
        # Cellstorleken styrs av frekvens-etikettens width/height; ramarna krymper runt.
        for lbl in self.cells.values():
            lbl.configure(width=self.cell_w, height=self.cell_h)

    def _update_tweak_labels(self):
        for g in ("freq", "head", "status"):
            self._sl_val[g].configure(text="%.2f" % self.scales[g])
        self._sl_val["cw"].configure(text="%d px" % self.cell_w)
        self._sl_val["ch"].configure(text="%d px" % self.cell_h)

    def _on_font(self, group, v):
        self.scales[group] = round(float(v), 2)
        self._rescale_fonts(group)
        self._update_tweak_labels()

    def _on_cw(self, v):
        self.cell_w = int(round(float(v)))
        self._resize_cells()
        self._update_tweak_labels()

    def _on_ch(self, v):
        self.cell_h = int(round(float(v)))
        self._resize_cells()
        self._update_tweak_labels()

    def _copy_values(self):
        s = self.scales
        txt = ("FREQ_SCALE = %.2f   HEAD_SCALE = %.2f   STATUS_SCALE = %.2f   "
               "CELL_W = %d   CELL_H = %d") % (
            s["freq"], s["head"], s["status"], self.cell_w, self.cell_h)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)
        except Exception:
            pass
        print(txt, flush=True)
        self.copy_lbl.configure(text="Copied: " + txt)

    # ----- händelsepump --------------------------------------------------------
    def _pump(self):
        dirty = False
        try:
            while True:
                ev = self.events.get_nowait()
                if ev.get("src") == "panel":
                    if self.router.handle_panel(ev["key"], ev["kind"], ev["value"]):
                        dirty = True
                elif ev.get("kind") == "status":
                    self._on_status(ev)
                    dirty = True
                elif ev.get("kind") == "cmd":
                    if self.router.handle_tci(ev):
                        dirty = True
                    self.sync.relay(ev)
                elif ev.get("kind") == "log":
                    self._log(ev.get("text", ""))
        except queue.Empty:
            pass
        if self.router.flush():
            dirty = True
        audible = self._audible_key()
        if audible != self._last_audible:
            self._last_audible = audible
            self.audio.apply()
        if self.leds:
            left, right, center = self.router.led_states()
            self.leds.set(left=left, right=right, center=center,
                          backlight=self.router.backlight_on)
        if dirty:
            self._refresh()
        self.root.after(20, self._pump)

    def _on_status(self, ev):
        radio = self.station.by_key(ev["radio"])
        if radio is None:
            return
        radio.connected = ev["connected"]
        if ev["connected"]:
            self.router.prime(radio.key)
            self.router.apply_tx()          # sätt split efter TX-VFO:n
            self.audio.apply()
        self._refresh_status()

    def _refresh(self):
        cache = self._cell_text
        for key, lbl in self.cells.items():
            rk, rxi, vfo = key
            txt = fmt_freq(self.station.by_key(rk).rx[rxi].freq[vfo])
            if cache.get(key) != txt:
                cache[key] = txt
                lbl.configure(text=txt)
        self._refresh_markers()

    def _refresh_markers(self):
        st = self.station
        ear_key = self._key(st.ear)
        tx_key = self._key(st.tx)
        enc = st.encoder_on
        tx_tuning = st.tx_radio().tune
        audible = set(self._key(c) for c in st.audible_cells())
        inv = self._c_inv
        for key, outer in self.cellouter.items():
            inner = self.cellinner[key]
            # öra-ring (yttre) — configure bara vid ändrad färg
            if key == ear_key:
                obc = self._c_green_b if enc == "ear" else self._c_green
            else:
                obc = inv
            if self._outer_bc.get(key) != obc:
                self._outer_bc[key] = obc
                outer.configure(border_color=obc)
            # TX-ring (inre) + hörs-nu-fyllning — batcha till ett configure
            if key == tx_key:
                ibc = self._c_txred if tx_tuning else (
                    self._c_orange_b if enc == "tx" else self._c_orange)
            else:
                ibc = inv
            ifg = self._c_heard if key in audible else inv
            changed = {}
            if self._inner_bc.get(key) != ibc:
                self._inner_bc[key] = ibc
                changed["border_color"] = ibc
            if self._inner_fg.get(key) != ifg:
                self._inner_fg[key] = ifg
                changed["fg_color"] = ifg
            if changed:
                inner.configure(**changed)
        self._refresh_status()

    def _refresh_status(self):
        # Statusraden borttagen (redundant): anslutning visas av panelprickarna,
        # encoder öra/TX av den ljusare ringen, hör-läge av "hörs nu"-fyllningen.
        for dot, rk in self.dots:
            col = self._c_conn if self.station.by_key(rk).connected else self._c_disc
            if self._dot_col.get(dot) != col:
                self._dot_col[dot] = col
                dot.configure(text_color=col)

    # ----- ramlöst fönster: drag + avsluta-meny --------------------------------
    def _bind_drag(self, widget):
        # Endast i ramlöst normalläge (Compact). Tweak-läget och Classic-läget
        # behåller OS-ramen och flyttas med den vanliga titelraden, så
        # body-drag vore bara förvirrande där. Högerklicksmenyn binds INTE
        # här längre (se root.bind("<Button-3>", ...) i _build_ui) - en
        # per-widget-lista missade tidigare flera ytor (t.ex. cellernas
        # gridram, synk-stripans knapp/kryssrutor), vilket
        # gjorde att högerklick "strulade" beroende på VAR i fönstret man
        # klickade.
        if self.tweak or self.classic:
            return
        widget.bind("<Button-1>", self._on_press)
        widget.bind("<B1-Motion>", self._on_drag)
        widget.bind("<ButtonRelease-1>", self._on_release)

    def _on_press(self, event):
        self._drag_origin = (event.x_root, event.y_root)
        self._win_origin = (self.root.winfo_x(), self.root.winfo_y())
        self._moved = False

    def _on_drag(self, event):
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        if abs(dx) > 5 or abs(dy) > 5:
            self._moved = True
        if self._moved:
            x = self._win_origin[0] + dx
            y = self._win_origin[1] + dy
            # Magnetisk snapping mot andra cockpitfönster är kopplad till
            # ontop - samma knapp/kryssruta styr båda, ingen egen inställning.
            if self.ontop:
                x, y = win_snap.snap(self.root.title(), x, y,
                                      self.root.winfo_width(), self.root.winfo_height())
            self.root.geometry("+%d+%d" % (x, y))

    def _on_release(self, event):
        if self._moved:
            d = load_settings()
            d["x"] = self.root.winfo_x()
            d["y"] = self.root.winfo_y()
            save_settings(d)

    def _on_win_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="CW settings...", command=self._open_cw_settings)
        menu.add_command(label="Button mapping...", command=self._open_panel_map)
        menu.add_command(label="LEDs...", command=self._open_led_settings)
        menu.add_command(label="View...", command=self._open_ui_mode_settings)
        menu.add_command(label="Station names...", command=self._open_station_names)
        menu.add_command(label="WSJT bridge...", command=self._open_bridge_settings)
        menu.add_separator()
        menu.add_command(label="About...", command=self._open_about)
        menu.add_separator()
        menu.add_command(label="Help / ExpertSDR3 setup",
                          command=lambda: webbrowser.open(HELP_URL))
        menu.add_command(label="Support this project (PayPal)",
                          command=lambda: webbrowser.open(DONATE_URL))
        menu.add_separator()
        menu.add_command(label="Exit", command=self._on_close)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_cw_settings(self):
        """Liveinställningar för keyer/sidoton (dot/dash/gap/pitch/ramp/volym).
        'Apply' och 'Save' applicerar båda direkt i router.keyer/router.sidetone
        (inget omstartskrav, inklusive byte av Key type paddle<->straight, se
        EncoderRouter.set_key_type) - 'Save' skriver dessutom till
        tcisync_settings.json och döljer fönstret (withdraw, INTE destroy) efteråt.
        customtkinter-widgetar (~20 st i den här dialogen) är tunga att SKAPA
        (canvas-komposition per widget för rundade hörn) - withdraw/deiconify
        återanvänder samma widgetträd i stället för att bygga om det vid varje
        öppning, vilket annars kändes märkbart trögt varje gång man öppnat
        dialogen på nytt efter en Save (upptäckt/rapporterat 2026-08-06)."""
        win = getattr(self, "_cw_win", None)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            return
        keyer, sidetone = self.router.keyer, self.router.sidetone
        cw_cfg = dict(DEFAULT_CW_SETTINGS)
        cw_cfg.update(load_settings().get("cw", {}))
        win = ctk.CTkToplevel(self.root)
        win.title("CW settings")
        win.attributes("-topmost", True)
        self._cw_win = win

        ctk.CTkLabel(win, text="Key type").grid(row=0, column=0, padx=8, pady=4, sticky="w")
        key_type_var = tk.StringVar(value=self.router.key_type)
        ctk.CTkOptionMenu(win, values=["paddle", "straight"], variable=key_type_var,
                          width=120).grid(row=0, column=1, padx=8, pady=4)

        def gv(name):
            # dot_pct/dash_pct/ptt_cw_delay_ms finns bara på IambicKeyer -
            # om StraightKeyer är aktiv just nu visas ändå sparade/default-
            # värden, så de finns kvar oförändrade om man byter tillbaka.
            return getattr(keyer, name, cw_cfg[name])

        fields = [
            ("Pitch (Hz)", "pitch_hz", sidetone.freq_hz),
            ("Ramp (ms)", "ramp_ms", sidetone.ramp_ms),
            ("Dot (%)", "dot_pct", gv("dot_pct")),
            ("Dash (%)", "dash_pct", gv("dash_pct")),
            ("Gap (%)", "gap_pct", gv("gap_pct")),
            ("Debounce (ms)", "debounce_ms", keyer.debounce_ms),
            ("Break-in (ms)", "break_in_ms", keyer.break_in_ms),
            ("Ptt-CW delay (ms)", "ptt_cw_delay_ms", gv("ptt_cw_delay_ms")),
        ]
        entries = {}
        for i, (label, key, val) in enumerate(fields, start=1):
            ctk.CTkLabel(win, text=label).grid(row=i, column=0, padx=8, pady=4, sticky="w")
            e = ctk.CTkEntry(win, width=80)
            e.insert(0, str(val))
            e.grid(row=i, column=1, padx=8, pady=4)
            entries[key] = e

        # Sidetone-volym: egen rad med slider, verkar OMEDELBART vid drag -
        # inte gated bakom Apply/Save som fälten ovan.
        vol_row = len(fields) + 1
        ctk.CTkLabel(win, text="Sidetone volume (%)").grid(
            row=vol_row, column=0, padx=8, pady=4, sticky="w")
        volume_var = tk.IntVar(value=int(cw_cfg.get("volume_pct", 35)))
        vol_slider_frame = ctk.CTkFrame(win, fg_color="transparent")
        vol_slider_frame.grid(row=vol_row, column=1, padx=8, pady=4, sticky="ew")
        vol_val_lbl = ctk.CTkLabel(vol_slider_frame, text=str(volume_var.get()), width=30)

        def on_volume(v):
            pct = int(float(v))
            volume_var.set(pct)
            vol_val_lbl.configure(text=str(pct))
            sidetone.reconfigure(volume=pct / 100.0)

        vol_slider = ctk.CTkSlider(vol_slider_frame, from_=0, to=100, command=on_volume,
                                   width=110)
        vol_slider.set(volume_var.get())
        vol_slider.pack(side="left")
        vol_val_lbl.pack(side="left", padx=(6, 0))

        def read_values():
            try:
                vals = {k: int(float(e.get())) for k, e in entries.items()}
                vals["volume_pct"] = volume_var.get()
                return vals
            except ValueError:
                self._log("CW settings: invalid value, nothing changed")
                return None

        def apply_live():
            vals = read_values()
            if vals is None:
                return None
            sidetone.reconfigure(freq_hz=vals["pitch_hz"], ramp_ms=vals["ramp_ms"],
                                  volume=vals["volume_pct"] / 100.0)
            new_type = key_type_var.get()
            if new_type != self.router.key_type:
                self.router.set_key_type(new_type, vals)
            else:
                k = self.router.keyer
                k.debounce_ms = vals["debounce_ms"]
                k.break_in_ms = vals["break_in_ms"]
                if hasattr(k, "dot_pct"):    # bara IambicKeyer - se gv()
                    k.dot_pct = vals["dot_pct"]
                    k.dash_pct = vals["dash_pct"]
                    k.gap_pct = vals["gap_pct"]
                    k.ptt_cw_delay_ms = vals["ptt_cw_delay_ms"]
            self._log("CW settings: applied (live)")
            return vals

        def do_save():
            vals = apply_live()
            if vals is None:
                return
            vals["key_type"] = key_type_var.get()
            d = load_settings()
            d["cw"] = vals
            save_settings(d)
            self._log("CW settings: saved to tcisync_settings.json")
            win.withdraw()

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.grid(row=vol_row + 1, column=0, columnspan=2, pady=(6, 8))
        ctk.CTkButton(btns, text="Apply", width=90, command=apply_live).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Save", width=90, command=do_save).pack(side="left", padx=4)

    # (row, column) for each button in the panel image - same physical
    # placement as ESDR3's own E-Coder configuration image. The main knob
    # occupies the empty middle of rows 1-2, columns 1-2 (no button there).
    _PANEL_POSITIONS = {
        "zoom_up": (0, 0), "split": (0, 1), "switch_rx": (0, 2),
        "rit_reset": (0, 3), "listen_ab": (0, 4),
        "zoom_down": (1, 0), "step_down": (1, 1), "step_up": (1, 2),
        "if_as_rit": (1, 3), "listen_a": (1, 4),
        "tune": (2, 0), "listen_b": (2, 4),
        "change_vfo": (3, 0), "vfob_onoff": (3, 4),
    }

    def _open_panel_map(self):
        """Graphical button mapping: a clickable panel image in the same
        physical layout as ESDR3's own E-Coder configuration screen. Clicking
        a button opens a popup with a dropdown for its function (TCISync's
        own, see panel_functions.FUNCTION_CATALOG - NOT ESDR3's built-in
        functions, that panel is disabled in ESDR3) and a text field for an
        optional parameter (e.g. the CW macro text, the WPM value). The
        change is applied live to router.panel_map and saved to
        tcisync_settings.json as soon as the popup is closed with "OK".
        Buttons are labeled only by physical number - no invented names."""
        if getattr(self, "_panel_win", None) is not None and self._panel_win.winfo_exists():
            self._panel_win.lift()
            return
        from panel_functions import FUNCTION_CATALOG, BUTTON_LAYOUT, KNOB_PRESS_LAYOUT

        win = ctk.CTkToplevel(self.root)
        win.title("Button mapping")
        win.attributes("-topmost", True)
        self._panel_win = win

        knob_row = ctk.CTkFrame(win, fg_color="transparent")
        knob_row.pack(padx=12, pady=(12, 6))
        grid = ctk.CTkFrame(win, fg_color="transparent")
        grid.pack(padx=12, pady=(0, 12))

        self._panel_buttons = {}   # key -> CTkButton (to update its label after a change)

        def label_for(key, display_name):
            entry = self.router.panel_map.get(key, {"func": "unused"})
            func = entry.get("func", "unused")
            func_label = FUNCTION_CATALOG.get(func, FUNCTION_CATALOG["unused"])["label"]
            return "%s\n%s" % (display_name, func_label)

        def make_click(key, display_name):
            return lambda: self._open_function_popup(key, display_name)

        ctk.CTkLabel(knob_row, text="Knob press:", font=self.f_btn).pack(side="left", padx=(0, 8))
        for key, display_name in KNOB_PRESS_LAYOUT:
            btn = ctk.CTkButton(knob_row, text=label_for(key, display_name), width=140, height=40,
                                 font=self.f_status, command=make_click(key, display_name))
            btn.pack(side="left", padx=4)
            self._panel_buttons[key] = btn

        for num, key in BUTTON_LAYOUT:
            display_name = "Button %d" % num
            r, c = self._PANEL_POSITIONS[key]
            btn = ctk.CTkButton(grid, text=label_for(key, display_name), width=120, height=50,
                                 font=self.f_status, command=make_click(key, display_name))
            btn.grid(row=r, column=c, padx=4, pady=4)
            self._panel_buttons[key] = btn

    def _open_function_popup(self, key, name):
        """Popup for ONE button: dropdown (function) + text field (parameter).
        OK writes to router.panel_map (live) AND tcisync_settings.json
        immediately, then updates the panel button's label."""
        from panel_functions import FUNCTION_CATALOG

        func_ids = list(FUNCTION_CATALOG.keys())
        func_labels = [FUNCTION_CATALOG[f]["label"] for f in func_ids]
        label_to_id = dict(zip(func_labels, func_ids))
        id_to_label = dict(zip(func_ids, func_labels))

        entry = self.router.panel_map.get(key, {"func": "unused"})
        cur_func = entry.get("func", "unused")
        cur_param = entry.get("param", "")

        pop = ctk.CTkToplevel(self.root)
        pop.title(name)
        pop.attributes("-topmost", True)
        pop.grab_set()

        ctk.CTkLabel(pop, text=name, font=self.f_btn).grid(
            row=0, column=0, columnspan=2, padx=10, pady=(10, 6))
        ctk.CTkLabel(pop, text="Function").grid(row=1, column=0, padx=8, pady=4, sticky="w")
        var = tk.StringVar(value=id_to_label.get(cur_func, id_to_label["unused"]))
        ctk.CTkOptionMenu(pop, values=func_labels, variable=var, width=210).grid(
            row=1, column=1, padx=8, pady=4)
        ctk.CTkLabel(pop, text="Parameter").grid(row=2, column=0, padx=8, pady=4, sticky="w")
        pentry = ctk.CTkEntry(pop, width=120)
        if cur_param not in (None, ""):
            pentry.insert(0, str(cur_param))
        pentry.grid(row=2, column=1, padx=8, pady=4)

        def on_ok():
            func = label_to_id.get(var.get(), "unused")
            item = {"func": func}
            param_raw = pentry.get().strip()
            if param_raw:
                item["param"] = param_raw
            self.router.panel_map[key] = item
            d = load_settings()
            pm = d.get("panel_map", {})
            pm[key] = item
            d["panel_map"] = pm
            save_settings(d)
            self._log("Button mapping: %s -> %s" % (name, FUNCTION_CATALOG[func]["label"]))
            self._panel_buttons[key].configure(
                text="%s\n%s" % (name, FUNCTION_CATALOG[func]["label"]))
            pop.destroy()

        btns = ctk.CTkFrame(pop, fg_color="transparent")
        btns.grid(row=3, column=0, columnspan=2, pady=(6, 10))
        ctk.CTkButton(btns, text="OK", width=90, command=on_ok).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Cancel", width=90, command=pop.destroy).pack(side="left", padx=4)

    def _open_led_settings(self):
        """The 3 status LEDs (left/right/center knob) can each be assigned to
        mirror the ON/OFF state of any button's current function (see
        panel_functions.LED_TARGETS / EncoderRouter.led_states). The main-
        knob light + keypad backlight share a single on/off, they're not
        individually assignable (per Jonny's spec)."""
        from panel_functions import LED_TARGETS

        win = ctk.CTkToplevel(self.root)
        win.title("LEDs")
        win.attributes("-topmost", True)

        target_labels = [label for _key, label in LED_TARGETS]
        key_to_label = dict(LED_TARGETS)
        label_to_key = {label: key for key, label in LED_TARGETS}

        vars_ = {}
        for i, (slot, disp) in enumerate([("left", "Left knob LED"),
                                          ("right", "Right knob LED"),
                                          ("center", "Center knob LED")]):
            ctk.CTkLabel(win, text=disp).grid(row=i, column=0, padx=8, pady=4, sticky="w")
            cur = key_to_label.get(self.router.led_map.get(slot, "none"), "None")
            var = tk.StringVar(value=cur)
            ctk.CTkOptionMenu(win, values=target_labels, variable=var, width=200).grid(
                row=i, column=1, padx=8, pady=4)
            vars_[slot] = var

        bl_var = ctk.BooleanVar(value=self.router.backlight_on)
        ctk.CTkCheckBox(win, text="Backlight (main knob + keypad)", variable=bl_var).grid(
            row=3, column=0, columnspan=2, padx=8, pady=(8, 4), sticky="w")

        def on_ok():
            new_map = {slot: label_to_key.get(var.get(), "none") for slot, var in vars_.items()}
            self.router.led_map = new_map
            self.router.backlight_on = bool(bl_var.get())
            d = load_settings()
            d["leds"] = {"map": new_map, "backlight": self.router.backlight_on}
            save_settings(d)
            self._log("LEDs: saved")
            win.destroy()

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.grid(row=4, column=0, columnspan=2, pady=(6, 10))
        ctk.CTkButton(btns, text="OK", width=90, command=on_ok).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Cancel", width=90, command=win.destroy).pack(side="left", padx=4)

    def _open_ui_mode_settings(self):
        """Compact (current, frameless, body-drag) vs Classic (normal OS
        window frame/titlebar, no body-drag - the OS frame handles moving).
        Takes effect on next start; overrideredirect can't be toggled live
        without rebuilding the window."""
        win = ctk.CTkToplevel(self.root)
        win.title("View")
        win.attributes("-topmost", True)

        ctk.CTkLabel(win, text="UI mode").grid(row=0, column=0, padx=8, pady=(10, 4), sticky="w")
        cur = "classic" if self.classic else "compact"
        var = tk.StringVar(value=cur)
        ctk.CTkOptionMenu(win, values=["compact", "classic"], variable=var, width=160).grid(
            row=0, column=1, padx=8, pady=(10, 4))
        ctk.CTkLabel(win, text="Takes effect after restart.",
                     text_color=th.color("text_secondary")).grid(
            row=1, column=0, columnspan=2, padx=8, pady=(0, 8))

        ontop_var = ctk.BooleanVar(value=self.ontop)
        ctk.CTkCheckBox(win, text="Always on top", variable=ontop_var).grid(
            row=2, column=0, columnspan=2, padx=8, pady=(0, 8), sticky="w")

        def on_ok():
            d = load_settings()
            d["ui_mode"] = var.get()
            self.ontop = bool(ontop_var.get())
            self._apply_ontop()
            d["ontop"] = self.ontop
            save_settings(d)
            self._log("UI mode: saved (%s) - restart to apply" % var.get())
            win.destroy()

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.grid(row=3, column=0, columnspan=2, pady=(0, 6))
        ctk.CTkButton(btns, text="OK", width=90, command=on_ok).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Cancel", width=90, command=win.destroy).pack(side="left", padx=4)

        # Andra ingången till Help/Support, utöver högerklicksmenyn (skill
        # add-program-help-link: "two entry points beat one").
        def _link_label(row, text, url):
            lbl = ctk.CTkLabel(win, text=text, text_color=th.color("text_secondary"),
                               font=th.font("small"), cursor="hand2")
            lbl.grid(row=row, column=0, columnspan=2, pady=(0, 4 if row == 4 else 8))
            lbl.bind("<Button-1>", lambda e: webbrowser.open(url))
            lbl.bind("<Enter>", lambda e: lbl.configure(text_color=th.color("accent_orange_bright")))
            lbl.bind("<Leave>", lambda e: lbl.configure(text_color=th.color("text_secondary")))

        _link_label(4, "Help / ExpertSDR3 setup", HELP_URL)
        _link_label(5, "Support this project (PayPal)", DONATE_URL)

    def _open_station_names(self):
        """Eget namn per radio (t.ex. 'Ramnäs'/'Blecket') istället för auto-
        detekterade 'Local'/'Remote'. Sparas i tcisync_settings.json under
        station_names (nyckel = radio.key) och används i header-etiketten
        (radio.name) och i TCI-felmeddelanden (TciLink.name)."""
        win = ctk.CTkToplevel(self.root)
        win.title("Station names")
        win.attributes("-topmost", True)

        vars_by_key = {}
        role_vars_by_key = {}
        for i, r in enumerate(self.station.radios):
            ctk.CTkLabel(win, text="%s (%s)" % (r.place, r.key)).grid(
                row=i, column=0, padx=8, pady=(10 if i == 0 else 4, 4), sticky="w")
            var = tk.StringVar(value=r.name)
            vars_by_key[r.key] = var
            ctk.CTkEntry(win, textvariable=var, width=160).grid(
                row=i, column=1, padx=8, pady=(10 if i == 0 else 4, 4))
            role_var = tk.StringVar(value=r.place)
            role_vars_by_key[r.key] = role_var
            ctk.CTkOptionMenu(win, variable=role_var, values=["local", "remote"],
                               width=100).grid(
                row=i, column=2, padx=8, pady=(10 if i == 0 else 4, 4))

        ctk.CTkLabel(win, text=("Role: fix a wrong auto-detected Local/Remote "
                                 "(happens if ExpertSDR3's profile files are "
                                 "stale, see README). Takes effect after restart."),
                     text_color=th.color("text_secondary"), wraplength=320,
                     justify="left").grid(
            row=len(self.station.radios), column=0, columnspan=3, padx=8, pady=(0, 8))

        def on_ok():
            d = load_settings()
            names = d.get("station_names", {})
            overrides = d.get("role_overrides", {})
            for key, var in vars_by_key.items():
                val = var.get().strip()
                if val:
                    names[key] = val
            for key, role_var in role_vars_by_key.items():
                overrides[key] = role_var.get()
            d["station_names"] = names
            d["role_overrides"] = overrides
            save_settings(d)
            self._log("Station names/roles: saved - restart to apply")
            win.destroy()

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.grid(row=len(self.station.radios) + 1, column=0, columnspan=2, pady=(0, 6))
        ctk.CTkButton(btns, text="OK", width=90, command=on_ok).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Cancel", width=90, command=win.destroy).pack(side="left", padx=4)

    def _open_bridge_settings(self):
        """WSJT-bron: en lokal TCI-server som WSJT-X ansluter till (TCI Client RX1, localhost:<port>).
        Lägesknapparna i synk-stripan väljer vilken radio som hörs och vilken som sänder (Ramnäs
        RX2 eller Blecket RX1). Sparas i tcisync_settings.json under wsjt_bridge."""
        win = ctk.CTkToplevel(self.root)
        win.title("WSJT bridge")
        win.attributes("-topmost", True)
        cfg = load_settings().get("wsjt_bridge", {})
        en_var = tk.BooleanVar(value=self.bridge.running)
        port_var = tk.StringVar(value=str(self.bridge.port))
        ctk.CTkCheckBox(win, text="Enable WSJT bridge", variable=en_var).grid(
            row=0, column=0, columnspan=2, padx=12, pady=(12, 6), sticky="w")
        ctk.CTkLabel(win, text="Port").grid(row=1, column=0, padx=(12, 4), pady=4, sticky="w")
        ctk.CTkEntry(win, textvariable=port_var, width=80).grid(row=1, column=1, padx=4, pady=4, sticky="w")
        ctk.CTkLabel(win, text=("In WSJT-X: Settings > Radio > Rig = TCI Client RX1, "
                                 "network address localhost:<port>. The four mode buttons in the sync "
                                 "strip choose which radio you hear and which one transmits."),
                     text_color=th.color("text_secondary"), wraplength=320,
                     justify="left").grid(row=2, column=0, columnspan=2, padx=12, pady=(4, 8))
        ctl_var = tk.BooleanVar(value=self.control.running)
        cport_var = tk.StringVar(value=str(self.control.port))
        ctk.CTkCheckBox(win, text="Enable control interface (localhost, for other programs)",
                        variable=ctl_var).grid(row=3, column=0, columnspan=2, padx=12, pady=(4, 6), sticky="w")
        ctk.CTkLabel(win, text="Control port").grid(row=4, column=0, padx=(12, 4), pady=4, sticky="w")
        ctk.CTkEntry(win, textvariable=cport_var, width=80).grid(row=4, column=1, padx=4, pady=4, sticky="w")

        def on_ok():
            try:
                port = int(port_var.get())
            except ValueError:
                port = int(cfg.get("port", BRIDGE_DEFAULT_PORT))
            try:
                cport = int(cport_var.get())
            except ValueError:
                cport = int(cfg.get("control_port", CONTROL_DEFAULT_PORT))
            want = en_var.get()
            if self.bridge.running and (not want or port != self.bridge.port):
                self._bridge_stop()
            self.bridge.port = port
            if want and not self.bridge.running:
                self._bridge_start()
            want_ctl = ctl_var.get()
            if self.control.running and (not want_ctl or cport != self.control.port):
                self.control.stop()
            self.control.port = cport
            if want_ctl and not self.control.running:
                self._control_start()
            d = load_settings()
            cfg = d.get("wsjt_bridge", {})
            cfg.update({"enabled": self.bridge.running, "port": port,
                        "control": self.control.running, "control_port": cport})
            d["wsjt_bridge"] = cfg
            save_settings(d)
            win.destroy()

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.grid(row=5, column=0, columnspan=2, pady=(0, 10))
        ctk.CTkButton(btns, text="OK", width=90, command=on_ok).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Cancel", width=90, command=win.destroy).pack(side="left", padx=4)

    def _open_about(self):
        """Enda platsen i UI:t där versionsnumret syns i Compact-läge - root.title()
        innehåller det redan (rad ~124), men Compact kör overrideredirect(True)
        utan OS-titelrad så den texten är osynlig där (bara synlig i Classic-läge)."""
        if getattr(self, "_about_win", None) is not None and self._about_win.winfo_exists():
            self._about_win.lift()
            return
        win = ctk.CTkToplevel(self.root)
        win.title("About")
        win.attributes("-topmost", True)
        self._about_win = win

        ctk.CTkLabel(win, text="TCISync (by SM5K)", font=self.f_btn).grid(
            row=0, column=0, padx=16, pady=(14, 2))
        ctk.CTkLabel(win, text="Version %s" % VERSION,
                     text_color=th.color("text_secondary")).grid(
            row=1, column=0, padx=16, pady=(0, 10))

        def _link_label(row, text, url):
            lbl = ctk.CTkLabel(win, text=text, text_color=th.color("text_secondary"),
                               font=th.font("small"), cursor="hand2")
            lbl.grid(row=row, column=0, pady=(0, 4))
            lbl.bind("<Button-1>", lambda e: webbrowser.open(url))
            lbl.bind("<Enter>", lambda e: lbl.configure(text_color=th.color("accent_orange_bright")))
            lbl.bind("<Leave>", lambda e: lbl.configure(text_color=th.color("text_secondary")))

        _link_label(2, "github.com/sm5tog/TCISync", "https://github.com/%s" % UPDATE_REPO)
        _link_label(3, "Help / ExpertSDR3 setup", HELP_URL)
        _link_label(4, "Support this project (PayPal)", DONATE_URL)

        ctk.CTkButton(win, text="Close", width=90, command=win.destroy).grid(
            row=5, column=0, pady=(6, 14))

    def _on_close(self):
        self.stop_flag.set()
        self.router.stop()
        self.control.stop()
        self.bridge.stop()
        for link in self.links.values():
            link.stop()
        if self.leds:
            self.leds.off()
        if self.hid:
            try:
                self.hid.close()
            except Exception:
                pass
        self.root.destroy()


def main():
    tweak = "--tweak" in sys.argv[1:]
    startup_cfg = load_settings()
    classic = startup_cfg.get("ui_mode") == "classic"
    ontop = startup_cfg.get("ontop", True)
    th.setup()
    root = ctk.CTk()
    Cockpit(root, tweak=tweak, classic=classic, ontop=ontop)
    root.update_idletasks()               # låt widgetarna räkna ut sina begärda storlekar
    if tweak:
        root.geometry("680x400")
        root.minsize(520, 370)
    elif classic:
        # Classic: vanlig OS-fönsterram/titelrad (minimera/stäng), ingen
        # body-drag - OS-ramen sköter flytt. Samma innehållslayout som Compact.
        w = root.winfo_reqwidth()
        h = 153 + 30                      # + lite marginal för OS-titelraden
        cfg = load_settings()
        if "x" in cfg and "y" in cfg:
            root.geometry("%dx%d+%d+%d" % (w, h, cfg["x"], cfg["y"]))
        else:
            root.geometry("%dx%d" % (w, h))
        root.minsize(w, h)
        root.attributes("-topmost", ontop)
    else:
        # Compact: ramlöst (som rotor), ingen OS-titelrad. Flytta med body-drag,
        # avsluta via högerklicksmenyn. Auto-anpassa fönstret till innehållets
        # faktiska storlek: tunn stripa utan svart yta och utan klippning,
        # oavsett trimmade cell-/textmått.
        root.overrideredirect(True)
        w = root.winfo_reqwidth()
        h = 153                           # samma höjd som rotorfönstret (kompass-storleken)
        cfg = load_settings()
        if "x" in cfg and "y" in cfg:
            root.geometry("%dx%d+%d+%d" % (w, h, cfg["x"], cfg["y"]))
        else:
            root.geometry("%dx%d" % (w, h))
        root.minsize(w, h)
        root.attributes("-topmost", ontop)
    root.mainloop()


if __name__ == "__main__":
    main()
