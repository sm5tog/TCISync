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
from ecoder_hid import EcoderHid
from ecoder_decode import Decoder, load_mapping
from ecoder_leds import LedController

VERSION = "0.12.0"

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
        self.slots = _build_slots(self.station)
        self.events = queue.Queue()
        self.links = {}
        self.cells = {}         # (radio_key, rx_idx, vfo) -> CTkLabel (frekvenstext)
        self.cellouter = {}     # -> CTkFrame (öra-ring + storlek)
        self.cellinner = {}     # -> CTkFrame (TX-ring + hörs-nu-fyllning)
        self.dots = []          # (dot_widget, radio_key) per panel

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
            link = TciLink(r.key, r.ip, r.port, self.events)
            self.links[r.key] = link
            link.start()

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

    def _reader_loop(self):
        while not self.stop_flag.is_set():
            try:
                r = self.hid.read(150)
            except OSError:
                break
            if r is not None:
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

    def _cell_click(self, key, which):
        radio_key, rxi, vfo = key
        cell = (self.station.index_of(radio_key), rxi, vfo)
        dirty = self.router.set_ear(cell) if which == "ear" else self.router.set_tx(cell)
        if dirty:
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
        self.update_btn = ctk.CTkButton(bar, text="", font=self.f_btn,
                                        height=th.METRICS["btn_height"],
                                        corner_radius=th.METRICS["radius"],
                                        fg_color=th.color("accent_blue"),
                                        hover_color=th.color("accent_blue_bright"),
                                        text_color=th.color("btn_text"),
                                        command=self._launch_updater)

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
        """Liveinställningar för keyer/sidoton (dot/dash/gap/pitch/ramp). 'Tillämpa'
        ändrar direkt i router.keyer/router.sidetone (inget omstartskrav, för att
        kunna modelleras); 'Spara' skriver dessutom till tcisync_settings.json så
        värdena finns kvar till nästa start."""
        if getattr(self, "_cw_win", None) is not None and self._cw_win.winfo_exists():
            self._cw_win.lift()
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

        def read_values():
            try:
                return {k: int(float(e.get())) for k, e in entries.items()}
            except ValueError:
                self._log("CW settings: invalid value, nothing changed")
                return None

        def apply_live():
            vals = read_values()
            if vals is None:
                return None
            sidetone.reconfigure(freq_hz=vals["pitch_hz"], ramp_ms=vals["ramp_ms"])
            keyer.debounce_ms = vals["debounce_ms"]
            keyer.break_in_ms = vals["break_in_ms"]
            if hasattr(keyer, "dot_pct"):    # bara IambicKeyer - se gv()
                keyer.dot_pct = vals["dot_pct"]
                keyer.dash_pct = vals["dash_pct"]
                keyer.gap_pct = vals["gap_pct"]
                keyer.ptt_cw_delay_ms = vals["ptt_cw_delay_ms"]
            self._log("CW settings: applied (live)")
            if key_type_var.get() != self.router.key_type:
                self._log("Key type: %s saved - restart to apply" % key_type_var.get())
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

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.grid(row=len(fields) + 1, column=0, columnspan=2, pady=(6, 8))
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

    def _on_close(self):
        self.stop_flag.set()
        self.router.stop()
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
