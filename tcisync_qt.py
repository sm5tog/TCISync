#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tcisync_qt.py - TCISync (by SM5K), Qt-gränssnittet (PySide6).

Ersätter det tidigare customtkinter-fönstret (samma funktioner, samma inställningsfil). Cockpit
med öra/TX-mus-modell: vänsterklick på en frekvenscell = öra (grön ram, du hör), högerklick = TX
(orange ram, du sänder). Encodern stämmer den ena, E-Coder-knapp 13 växlar öra/TX. Knapp 14 =
tillfällig TX-lyssning, knapp 12 dess läge. "Hörs nu"-fyllning visar var ljudet kommer från.
Ljud (audio.py), styrning (encoder_router.py) och synk (sync.py) hänger på stationen.

Trådregel: allt som rör widgetar sker på GUI-tråden. Andra trådar (styrservern, uppdateringskollen)
skickar en signal via `Bus`, Qt köar den till GUI-tråden. Frekvenscellerna målas själva
(FreqCell.paintEvent), de uppdateras bara när text eller färg ändrats.

Trim-läge: `python tcisync.py --tweak` visar reglage för textstorlek och cellmått.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from PySide6.QtCore import QObject, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPalette, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
                               QSizePolicy, QSlider, QVBoxLayout, QWidget)

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
from tci_proxy import TciProxy, DEFAULT_PORT as PROXY_DEFAULT_PORT
from ecoder_hid import EcoderHid
from ecoder_decode import Decoder, load_mapping
from ecoder_leds import LedController
from tcisync import (VERSION, BRIDGE_PLACES, BRIDGE_TX_LIVE, BRIDGE_ROLE_TEXT, HID_RETRY_S,
                     HID_SILENCE_S, _BASE_DIR, DONATE_URL, HELP_URL, UPDATE_REPO,
                     UPDATE_CHECK_URL, UPDATE_CHECK_MS, UPDATER_NAME, load_settings,
                     save_settings, DEFAULT_CW_SETTINGS, FREQ_SCALE, HEAD_SCALE, STATUS_SCALE,
                     CELL_W, CELL_H, _build_slots, fmt_freq, fmt_split)

M = th.METRICS
WINDOW_H = 153      # fönsterhöjd (px), samma som rotorfönstret så att fönstren linjerar


def _px_font(role, scale=1.0, bold=False):
    """QFont i pixlar för en tema-roll (Consolas för frekvensrollerna, annars Segoe UI)."""
    base = th.FONT.get(role, th.FONT["body"])
    family = th.FONT["freq_family"] if role in ("freq_main", "freq_secondary") else th.FONT["family"]
    f = QFont(family)
    f.setPixelSize(max(1, round(base * scale)))
    f.setBold(bool(bold))
    return f


def _btn_qss(bg, hover=None, fg=None):
    return ("QPushButton{background:%s;color:%s;border:none;border-radius:%dpx;padding:0px 6px}"
            "QPushButton:hover{background:%s}" % (
                bg, fg or th.color("btn_text"), M["radius"], hover or bg))


def apply_theme(app):
    """Fusion med mörk palett härledd ur ExpertSDR3-paletten (ee_theme.PALETTE)."""
    app.setStyle("Fusion")
    pal = QPalette()
    for role, token in ((QPalette.Window, "bg_surface"), (QPalette.WindowText, "text_primary"),
                        (QPalette.Base, "bg_control"), (QPalette.AlternateBase, "bg_surface"),
                        (QPalette.Text, "text_primary"), (QPalette.Button, "btn_idle_bg"),
                        (QPalette.ButtonText, "btn_text"), (QPalette.Highlight, "accent_blue"),
                        (QPalette.HighlightedText, "btn_text_on_accent"),
                        (QPalette.ToolTipBase, "bg_control"), (QPalette.ToolTipText, "text_primary"),
                        (QPalette.PlaceholderText, "text_secondary")):
        pal.setColor(role, QColor(th.color(token)))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(th.color("text_disabled")))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(th.color("text_disabled")))
    app.setPalette(pal)


def set_topmost(widget, on):
    """Alltid överst via Windows-API (ingen omskapning av fönstret, till skillnad från
    setWindowFlag + show)."""
    try:
        import win32con
        import win32gui
        win32gui.SetWindowPos(int(widget.winId()),
                              win32con.HWND_TOPMOST if on else win32con.HWND_NOTOPMOST,
                              0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
    except Exception:
        pass


class Bus(QObject):
    """Signaler från andra trådar till GUI-tråden (Qt köar dem automatiskt)."""
    control = Signal(object)     # (req, box, done) från kontrollservern
    update = Signal(str)         # ny versionstagg från uppdateringskollen


class FreqCell(QWidget):
    """En frekvenscell: yttre ring = öra, inre ring = TX, fyllning = hörs nu. Målas själv."""

    def __init__(self, key, owner, font, cell_w, cell_h):
        super().__init__()
        self.key = key
        self.owner = owner
        self.text = fmt_freq(None)
        self._outer = th.color("bg_control")
        self._inner = th.color("bg_control")
        self._fill = th.color("bg_control")
        self.setFont(font)
        self.set_cell_size(cell_w, cell_h)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_cell_size(self, cell_w, cell_h):
        self.setFixedSize(cell_w + 24, cell_h + 16)

    def set_text(self, text):
        if text != self.text:
            self.text = text
            self.update()

    def set_rings(self, outer, inner, fill):
        if (outer, inner, fill) != (self._outer, self._inner, self._fill):
            self._outer, self._inner, self._fill = outer, inner, fill
            self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg = QColor(th.color("bg_control"))
        r = QRectF(self.rect())
        p.setBrush(bg)
        p.setPen(QPen(QColor(self._outer), 2))
        p.drawRoundedRect(r.adjusted(1, 1, -1, -1), M["radius"], M["radius"])
        p.setBrush(QColor(self._fill))
        p.setPen(QPen(QColor(self._inner), 2))
        p.drawRoundedRect(r.adjusted(5, 5, -5, -5), M["radius"] - 1, M["radius"] - 1)
        p.setPen(QColor(th.color("text_freq")))
        p.setFont(self.font())
        p.drawText(self.rect(), Qt.AlignCenter, self.text)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.owner._cell_click(self.key, "ear")
        elif event.button() == Qt.RightButton:
            self.owner._cell_click(self.key, "tx")
        event.accept()

    def contextMenuEvent(self, event):
        event.accept()      # högerklick är TX-markören, inte fönstermenyn

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta:
            self.owner._cell_wheel(self, event.position().x(), delta)
        event.accept()


class LinkLabel(QLabel):
    """Klickbar länktext (öppnar webbläsaren), orange vid hovring."""

    def __init__(self, text, url, parent=None):
        super().__init__(text, parent)
        self._url = url
        self.setCursor(Qt.PointingHandCursor)
        self.setAlignment(Qt.AlignCenter)
        self._paint(False)

    def _paint(self, hot):
        col = th.color("accent_orange_bright") if hot else th.color("text_secondary")
        self.setStyleSheet("color:%s" % col)

    def enterEvent(self, _event):
        self._paint(True)

    def leaveEvent(self, _event):
        self._paint(False)

    def mousePressEvent(self, _event):
        webbrowser.open(self._url)


def _dialog(parent, title):
    dlg = QDialog(parent, Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
    dlg.setWindowTitle(title)
    return dlg


def _button_row(buttons):
    """buttons = [(text, callback), ...] i en centrerad rad."""
    row = QHBoxLayout()
    row.addStretch(1)
    for text, cb in buttons:
        b = QPushButton(text)
        b.setFixedWidth(90)
        b.clicked.connect(lambda _=False, c=cb: c())
        row.addWidget(b)
    row.addStretch(1)
    return row


def _note(text):
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setMinimumWidth(320)
    lbl.setStyleSheet("color:%s" % th.color("text_secondary"))
    return lbl


class Cockpit(QWidget):
    def __init__(self, tweak=False, classic=False, ontop=True):
        flags = Qt.Window
        if not (tweak or classic):
            flags |= Qt.FramelessWindowHint
        super().__init__(None, flags)
        self.tweak = tweak
        self.classic = classic
        self.ontop = ontop
        self.setWindowTitle("TCISync (by SM5K)  v%s" % VERSION)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor(th.color("bg_app")))
        self.setPalette(pal)

        self.station = auto_station()
        station_names = load_settings().get("station_names", {})
        for r in self.station.radios:
            if r.key in station_names:
                r.name = station_names[r.key]
        self.slots = _build_slots(self.station)
        self.events = queue.Queue()
        self.links = {}
        self.cells = {}          # (radio_key, rx_idx, vfo) -> FreqCell
        self.dots = []           # (QLabel, radio_key) per panel
        self.split_btns = {}     # (radio_key, rx_idx) -> QPushButton (Split + diff)
        self._split_text = {}
        self._dot_col = {}
        self._cw_win = None
        self._panel_win = None
        self._about_win = None
        self._closed = False
        self._drag = None
        self._moved = False

        # Färgtokens en gång (heta uppdateringsloopen slår inte upp i dict).
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
        self._scalable = []      # (widget, roll, grupp, fet)

        self.bus = Bus()
        self.bus.control.connect(self._on_control)
        self.bus.update.connect(self._show_update_available)

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
        self._bridge_shown = False
        self._bridge_btn_cache = {}
        if bridge_cfg.get("enabled"):
            self._bridge_start()
        self.control = ControlServer(self._control_request, log=self._bridge_log,
                                     port=int(bridge_cfg.get("control_port", CONTROL_DEFAULT_PORT)))
        if bridge_cfg.get("control"):
            self._control_start()

        self.proxy = None
        if load_settings().get("tci_proxy", {}).get("enabled"):
            self._proxy_start()

        self._start_hid()

        self._update_available_tag = None
        self._pump_timer = QTimer(self)
        self._pump_timer.timeout.connect(self._pump)
        self._pump_timer.start(20)
        self._bridge_timer = QTimer(self)
        self._bridge_timer.timeout.connect(self._bridge_ui_tick)
        self._bridge_timer.start(250)
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._update_check_tick)
        self._update_timer.start(UPDATE_CHECK_MS)
        QTimer.singleShot(0, self._update_check_tick)
        self._refresh()

    # ----- auto-uppdatering (samma mönster som jtspots) -------------------------
    def _update_check_tick(self):
        threading.Thread(target=self._check_for_update, daemon=True).start()

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
        """Frågar bara GitHubs 'senaste release'-tagg, laddar aldrig ner något själv. Körs på en
        bakgrundstråd, resultatet går via signalen till GUI-tråden. Tyst vid nätverksfel."""
        try:
            req = urllib.request.Request(UPDATE_CHECK_URL, headers={"User-Agent": "TCISync"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            tag = data.get("tag_name", "")
            if self._version_tuple(tag) > self._version_tuple(VERSION):
                self.bus.update.emit(tag)
        except Exception:
            pass

    def _show_update_available(self, tag):
        if self._update_available_tag == tag:
            return
        self._update_available_tag = tag
        self.update_btn.setText("Update: %s" % tag)
        self.update_btn.show()
        self._log("New version %s available - click 'Update: %s' to install" % (tag, tag))

    def _launch_updater(self):
        """Bara meningsfullt i en fryst .exe (se tcisync_updater.py), loggar och avbryter annars."""
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
        self.close()

    def _log(self, text):
        print(text, flush=True)

    def _audible_key(self):
        return tuple(sorted(self.station.audible_cells()))

    def _key(self, cell):
        """(radio_idx, rx_idx, vfo) -> cell-nyckel (radio_key, rx_idx, vfo)."""
        return (self.station.radios[cell[0]].key, cell[1], cell[2])

    # ----- typsnitt -------------------------------------------------------------
    def _font(self, widget, role, group, bold=False):
        """Sätter ett skalat typsnitt och minns det så trim-läget kan skala om det."""
        widget.setFont(_px_font(role, self.scales[group], bold))
        self._scalable.append((widget, role, group, bold))

    def _rescale_fonts(self, group=None):
        for widget, role, g, bold in self._scalable:
            if group is None or g == group:
                widget.setFont(_px_font(role, self.scales[g], bold))
        self.adjustSize()

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
        pad = M["panel_pad"]
        root = QVBoxLayout(self)
        root.setContentsMargins(pad, pad - 4, pad, 0)
        root.setSpacing(4)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.setSpacing(M["gap"] * 2 + 4)
        for slot in self.slots:
            wrap.addWidget(self._rx_panel(slot), 1)
        root.addLayout(wrap, 1)
        root.addWidget(self._sync_bar())
        if self.tweak:
            root.addWidget(self._tweak_bar())

    def _rx_panel(self, slot):
        radio_key, rxi, title = slot
        radio = self.station.by_key(radio_key)
        f = QFrame()
        f.setObjectName("panel")
        f.setStyleSheet("QFrame#panel{background:%s;border:1px solid %s;border-radius:%dpx}" % (
            th.color("bg_surface"), th.color("border"), M["radius"]))
        lay = QVBoxLayout(f)
        lay.setContentsMargins(M["panel_pad"], 5, M["panel_pad"], M["panel_pad"] - 4)
        lay.setSpacing(2)

        head = QHBoxLayout()
        head.setSpacing(5)
        dot = QLabel("●")
        dot.setStyleSheet("color:%s" % th.color("status_disconnected"))
        self._font(dot, "body", "head", True)
        head.addWidget(dot)
        self.dots.append((dot, radio_key))
        place = "local" if radio.place == "local" else "remote"
        titlbl = QLabel("%s   %s · %s" % (title, radio.name, place))
        titlbl.setStyleSheet("color:%s" % th.color("text_primary"))
        self._font(titlbl, "body", "head", True)
        head.addWidget(titlbl)
        head.addStretch(1)
        lay.addLayout(head)

        cells = QHBoxLayout()
        cells.setSpacing(M["gap"] * 2)
        for vfo in (0, 1):
            key = (radio_key, rxi, vfo)
            cell = FreqCell(key, self, _px_font("freq_main", self.scales["freq"], True),
                            self.cell_w, self.cell_h)
            self._scalable.append((cell, "freq_main", "freq", True))
            self.cells[key] = cell
            cells.addWidget(cell, 0, Qt.AlignCenter)
        lay.addLayout(cells)

        split_btn = QPushButton("Split")
        split_btn.setFixedHeight(M["btn_height"])
        split_btn.setStyleSheet(_btn_qss(th.color("btn_idle_bg"), th.color("btn_hover_bg")))
        self._font(split_btn, "split", "status", True)
        split_btn.clicked.connect(lambda _=False, rk=radio_key, i=rxi: self._on_split(rk, i))
        lay.addWidget(split_btn)
        lay.addStretch(1)       # extra höjd hamnar under knappen, innehållet ligger överst
        self.split_btns[(radio_key, rxi)] = split_btn
        return f

    def _cell_click(self, key, which):
        radio_key, rxi, vfo = key
        cell = (self.station.index_of(radio_key), rxi, vfo)
        dirty = self.router.set_ear(cell) if which == "ear" else self.router.set_tx(cell)
        if dirty:
            self._refresh()

    @staticmethod
    def _digit_step_hz(cell, x):
        """Platsvärdet (i Hz) för siffran under pekaren i en frekvenscell, t.ex. "14.074.500"
        -> 100 om pekaren står på "5". Consolas är monospace, varje tecken (siffra eller punkt)
        är lika brett, så pixel -> tecken är en ren division."""
        text = cell.text
        if not text or not any(c.isdigit() for c in text):
            return None
        text_w = QFontMetrics(cell.font()).horizontalAdvance(text)
        char_w = text_w / float(len(text))
        if char_w <= 0:
            return None
        text_start = (cell.width() - text_w) / 2.0
        idx = int((x - text_start) // char_w)
        idx = max(0, min(len(text) - 1, idx))
        while idx > 0 and not text[idx].isdigit():
            idx -= 1
        if not text[idx].isdigit():
            return None
        digits_right = sum(1 for c in text[idx + 1:] if c.isdigit())
        return 10 ** digits_right

    def _cell_wheel(self, cell, x, delta):
        """Scrollhjul över en frekvenscell. Positionsberoende steglängd, ingen acceleration
        (designbeslut): riktningen avgör tecknet, siffran under pekaren stegets storlek.
        Öra/TX/split rörs inte, egen tuningväg (tune_cell_by) skild från E-Coderns."""
        step_hz = self._digit_step_hz(cell, x)
        if step_hz is None:
            return
        radio_key, rxi, vfo = cell.key
        idx = (self.station.index_of(radio_key), rxi, vfo)
        if self.router.tune_cell_by(idx, step_hz if delta > 0 else -step_hz):
            self._refresh()

    def _on_split(self, radio_key, rxi):
        if self.router.split_offset(radio_key, rxi):
            self._refresh()

    # ----- synk-stripa ---------------------------------------------------------
    def _sync_bar(self):
        bar = QFrame()
        bar.setObjectName("syncbar")
        bar.setStyleSheet("QFrame#syncbar{background:%s;border-radius:%dpx}" % (
            th.color("bg_surface"), M["radius"]))
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(6)

        self.sync_btn = QPushButton("Sync off")
        self.sync_btn.setFixedSize(90, M["btn_height"])
        self.sync_btn.setStyleSheet(_btn_qss(th.color("btn_idle_bg"), th.color("btn_hover_bg")))
        self._font(self.sync_btn, "small", "status", True)
        self.sync_btn.clicked.connect(self._on_sync_toggle)
        lay.addWidget(self.sync_btn)
        lay.addSpacing(4)

        params = [("vfo", "Frequency", "sync_vfo"), ("mod", "Mode", "sync_mod"),
                  ("vfob", "VFO B", "sync_vfob"), ("rit", "RIT", "sync_rit"),
                  ("filter", "Filter", "sync_filter")]
        self._sync_checks = {}
        check_qss = ("QCheckBox{color:%s}"
                     "QCheckBox::indicator{width:14px;height:14px;border:1px solid %s;"
                     "border-radius:2px;background:%s}"
                     "QCheckBox::indicator:checked{background:%s;border-color:%s}" % (
                         th.color("text_secondary"), th.color("btn_idle_border"),
                         th.color("bg_control"), th.color("accent_blue"),
                         th.color("accent_blue_bright")))
        for key, label, attr in params:
            cb = QCheckBox(label)
            cb.setChecked(bool(getattr(self.sync, attr)))
            cb.setStyleSheet(check_qss)
            self._font(cb, "label", "status", False)
            cb.clicked.connect(lambda checked, a=attr: setattr(self.sync, a, bool(checked)))
            lay.addWidget(cb)
            self._sync_checks[key] = cb
        lay.addStretch(1)

        # Visas bara om en nyare version hittats (se _show_update_available).
        self.update_btn = QPushButton("")
        self.update_btn.setFixedHeight(M["btn_height"])
        self.update_btn.setStyleSheet(_btn_qss(th.color("accent_blue"), th.color("accent_blue_bright")))
        self._font(self.update_btn, "small", "status", True)
        self.update_btn.clicked.connect(self._launch_updater)
        self.update_btn.hide()
        lay.addWidget(self.update_btn)

        # WSJT-bron: en knapp per plats (grön RX, röd TX, orange RX+TX, grå av) och två knappar som
        # kopierar en plats frekvens till den andra. Visas när bron är påslagen. Pricken i
        # etiketten: grå utan klient, blå när WSJT-X är ansluten.
        self.bridge_frame = QWidget()
        bl = QHBoxLayout(self.bridge_frame)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        self.bridge_label = QLabel("WSJT ●")
        self.bridge_label.setStyleSheet("color:%s" % th.color("text_secondary"))
        self._font(self.bridge_label, "label", "status", False)
        bl.addWidget(self.bridge_label)
        self.bridge_place_btns = {}
        for place in BRIDGE_PLACES:
            b = QPushButton("")
            b.setFixedSize(64, M["btn_height"])
            b.setStyleSheet(_btn_qss(th.color("btn_idle_bg"), fg=th.color("btn_text_on_accent")))
            self._font(b, "small", "status", True)
            b.clicked.connect(lambda _=False, p=place: self._on_bridge_place(p))
            bl.addWidget(b)
            self.bridge_place_btns[place] = b
        fl = QLabel("Freq")
        fl.setStyleSheet("color:%s" % th.color("text_secondary"))
        self._font(fl, "label", "status", False)
        bl.addSpacing(4)
        bl.addWidget(fl)
        self.bridge_copy_btns = {}
        for src, dst in (("ramnas", "blecket"), ("blecket", "ramnas")):
            b = QPushButton("")
            b.setFixedSize(64, M["btn_height"])
            b.setStyleSheet(_btn_qss(th.color("btn_idle_bg"), th.color("btn_hover_bg")))
            self._font(b, "small", "status", True)
            b.clicked.connect(lambda _=False, s=src, d=dst: self._on_bridge_copy(s, d))
            bl.addWidget(b)
            self.bridge_copy_btns[(src, dst)] = b
        self.bridge_frame.hide()
        lay.addWidget(self.bridge_frame)

        # Rent ASCII-tecken ("alltid överst") i stället för emoji.
        self.ontop_btn = QPushButton("^")
        self.ontop_btn.setFixedSize(28, M["btn_height"])
        self._font(self.ontop_btn, "small", "status", True)
        self.ontop_btn.clicked.connect(self._on_ontop_toggle)
        lay.addWidget(self.ontop_btn)
        self._paint_ontop_btn()
        return bar

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

    def _bridge_set(self, key, btn, text=None, bg=None, hover=None):
        """Sätter text och färg bara när något ändrats (undviker onödiga stilomräkningar)."""
        state = (text, bg, hover)
        if self._bridge_btn_cache.get(key) == state:
            return
        self._bridge_btn_cache[key] = state
        if text is not None:
            btn.setText(text)
        if bg is not None:
            btn.setStyleSheet(_btn_qss(bg, hover or bg, th.color("btn_text_on_accent")))

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

    def _bridge_ui_tick(self):
        """Uppdaterar platsknapparna i synk-stripan, 4 ggr/s."""
        running = self.bridge.running
        if running != self._bridge_shown:
            self._bridge_shown = running
            self.bridge_frame.setVisible(running)
        if not running:
            return
        dot = th.color("accent_blue_bright") if self.bridge.client_connected \
            else th.color("text_secondary")
        if self._bridge_btn_cache.get("dot") != dot:
            self._bridge_btn_cache["dot"] = dot
            self.bridge_label.setStyleSheet("color:%s" % dot)
        colors = {"rx": self._c_green, "tx": BRIDGE_TX_LIVE if self.bridge.ptt_active else self._c_txred,
                  "both": BRIDGE_TX_LIVE if self.bridge.ptt_active else self._c_orange,
                  "off": th.color("btn_idle_bg")}
        for place, b in self.bridge_place_btns.items():
            role = self._bridge_role(place)
            self._bridge_set(place, b, "%s %s" % (self._bridge_station_name(place)[:3],
                                                  BRIDGE_ROLE_TEXT[role]), colors[role])
        for (src, dst), b in self.bridge_copy_btns.items():
            self._bridge_set((src, dst), b, "%s>%s" % (self._bridge_station_name(src)[:3],
                                                       self._bridge_station_name(dst)[:3]))

    # ----- TCI-proxy för andra program, t.ex. Log4OM (tci_proxy.py) -------------------
    def _proxy_log(self, text):
        """Proxyns händelser till tcisync_proxy.log bredvid skriptet."""
        line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
        self._log(line)
        try:
            with open(os.path.join(_BASE_DIR, "tcisync_proxy.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _proxy_radio(self, cfg):
        """Radion proxyn går mot: inställningens nyckel, annars den lokala, annars den första."""
        r = self.station.by_key(cfg.get("radio"))
        if r is None:
            r = next((x for x in self.station.radios if x.place == "local"), None)
        return r or (self.station.radios[0] if self.station.radios else None)

    def _proxy_start(self):
        """Startar proxyn enligt tci_proxy i tcisync_settings.json (bara en socketbindning,
        anslutningarna går i egna trådar)."""
        cfg = load_settings().get("tci_proxy", {})
        r = self._proxy_radio(cfg)
        if r is None:
            self._proxy_log("TCI proxy: no radio found")
            return
        traffic = os.path.join(_BASE_DIR, "tcisync_proxy_traffic.log") if cfg.get("log_traffic") else None
        proxy = TciProxy(r.ip, r.port, port=int(cfg.get("port", PROXY_DEFAULT_PORT)),
                         log=self._proxy_log, traffic_path=traffic,
                         only_rx1=bool(cfg.get("only_rx1")))
        try:
            proxy.start()
        except OSError as e:
            self._proxy_log("TCI proxy: cannot listen on port %d: %s" % (proxy.port, e))
            return
        self.proxy = proxy

    def _proxy_stop(self):
        if self.proxy is not None:
            self.proxy.stop()
            self.proxy = None

    # ----- styrgränssnitt för andra program (bridge_control.py) ----------------------
    def _control_start(self):
        try:
            self.control.start()
        except OSError as e:
            self._bridge_log("Control interface: cannot listen on port %d: %s" % (self.control.port, e))

    def _control_request(self, req):
        """Anropas på servertråden. Själva åtgärden körs på GUI-tråden (widgetar är inte
        trådsäkra): servertråden väntar på resultatet, högst 3 s."""
        done, box = threading.Event(), {}
        self.bus.control.emit((req, box, done))
        if not done.wait(3.0):
            return {"ok": False, "error": "GUI busy, try again"}
        return box["r"]

    def _on_control(self, payload):
        req, box, done = payload
        try:
            box["r"] = self._control_exec(req)
        except Exception as e:
            box["r"] = {"ok": False, "error": "internal error: %s" % e}
        finally:
            done.set()

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
        """GUI-tråden. status | set_place | set_rx | set_dial."""
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

    # ----- ontop, synk ----------------------------------------------------------
    def _paint_ontop_btn(self):
        if self.ontop:
            self.ontop_btn.setStyleSheet(_btn_qss(th.color("btn_idle_bg"), th.color("accent_blue_bright")))
        else:
            self.ontop_btn.setStyleSheet(_btn_qss(th.color("accent_blue"), th.color("accent_blue_bright")))

    def _apply_ontop(self):
        set_topmost(self, self.ontop)
        self._paint_ontop_btn()

    def _on_ontop_toggle(self):
        self.ontop = not self.ontop
        self._apply_ontop()
        d = load_settings()
        d["ontop"] = self.ontop
        save_settings(d)

    def _on_sync_toggle(self):
        self.station.sync_mode = not self.station.sync_mode
        on = self.station.sync_mode
        self.sync_btn.setText("Sync on" if on else "Sync off")
        if on:
            self.sync_btn.setStyleSheet(_btn_qss(th.color("accent_blue"), th.color("accent_blue_bright")))
        else:
            self.sync_btn.setStyleSheet(_btn_qss(th.color("btn_idle_bg"), th.color("btn_hover_bg")))
        self._log("Sync %s" % ("on" if on else "off"))
        if on:
            self.sync.align()          # snäpp remoten till RX1 direkt vid påslag

    # ----- trim-läge -----------------------------------------------------------
    def _tweak_bar(self):
        bar = QFrame()
        bar.setObjectName("tweakbar")
        bar.setStyleSheet("QFrame#tweakbar{background:%s;border-radius:%dpx}" % (
            th.color("bg_control"), M["radius"]))
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(8, 4, 8, 6)
        lay.setSpacing(2)
        self._sl_val = {}

        def add(key, label, lo, hi, val, cb, factor):
            row = QHBoxLayout()
            name = QLabel(label)
            name.setFixedWidth(90)
            s = QSlider(Qt.Horizontal)
            s.setRange(int(round(lo * factor)), int(round(hi * factor)))
            s.setValue(int(round(val * factor)))
            v = QLabel("")
            v.setFixedWidth(60)
            v.setStyleSheet("color:%s" % th.color("accent_green_bright"))
            s.valueChanged.connect(lambda x, c=cb, f=factor: c(x / float(f)))
            row.addWidget(name)
            row.addWidget(s, 1)
            row.addWidget(v)
            lay.addLayout(row)
            self._sl_val[key] = v

        add("freq", "Frequency", 0.4, 1.6, self.scales["freq"], lambda v: self._on_font("freq", v), 100)
        add("head", "Panel title", 0.4, 1.6, self.scales["head"], lambda v: self._on_font("head", v), 100)
        add("status", "Status", 0.4, 1.6, self.scales["status"], lambda v: self._on_font("status", v), 100)
        add("cw", "Cell width", 70, 260, self.cell_w, self._on_cw, 1)
        add("ch", "Cell height", 22, 80, self.cell_h, self._on_ch, 1)

        row = QHBoxLayout()
        copy_btn = QPushButton("Copy values")
        copy_btn.setFixedWidth(120)
        copy_btn.clicked.connect(self._copy_values)
        self.copy_lbl = QLabel("")
        self.copy_lbl.setStyleSheet("color:%s" % th.color("text_secondary"))
        row.addWidget(copy_btn)
        row.addWidget(self.copy_lbl, 1)
        lay.addLayout(row)
        self._update_tweak_labels()
        return bar

    def _resize_cells(self):
        for cell in self.cells.values():
            cell.set_cell_size(self.cell_w, self.cell_h)
        self.adjustSize()

    def _update_tweak_labels(self):
        for g in ("freq", "head", "status"):
            self._sl_val[g].setText("%.2f" % self.scales[g])
        self._sl_val["cw"].setText("%d px" % self.cell_w)
        self._sl_val["ch"].setText("%d px" % self.cell_h)

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
        QGuiApplication.clipboard().setText(txt)
        print(txt, flush=True)
        self.copy_lbl.setText("Copied: " + txt)

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
        for key, cell in self.cells.items():
            rk, rxi, vfo = key
            cell.set_text(fmt_freq(self.station.by_key(rk).rx[rxi].freq[vfo]))
        for (rk, rxi), btn in self.split_btns.items():
            fr = self.station.by_key(rk).rx[rxi].freq
            txt = fmt_split(fr[0], fr[1])
            if self._split_text.get((rk, rxi)) != txt:
                self._split_text[(rk, rxi)] = txt
                btn.setText(txt)
        self._refresh_markers()

    def _refresh_markers(self):
        st = self.station
        ear_key = self._key(st.ear)
        tx_key = self._key(st.tx)
        enc = st.encoder_on
        tx_tuning = st.tx_radio().tune
        audible = set(self._key(c) for c in st.audible_cells())
        inv = self._c_inv
        for key, cell in self.cells.items():
            if key == ear_key:
                outer = self._c_green_b if enc == "ear" else self._c_green
            else:
                outer = inv
            if key == tx_key:
                inner = self._c_txred if tx_tuning else (
                    self._c_orange_b if enc == "tx" else self._c_orange)
            else:
                inner = inv
            cell.set_rings(outer, inner, self._c_heard if key in audible else inv)
        self._refresh_status()

    def _refresh_status(self):
        # Anslutning visas av panelprickarna, encoder öra/TX av den ljusare ringen,
        # hör-läge av "hörs nu"-fyllningen.
        for dot, rk in self.dots:
            col = self._c_conn if self.station.by_key(rk).connected else self._c_disc
            if self._dot_col.get(dot) != col:
                self._dot_col[dot] = col
                dot.setStyleSheet("color:%s" % col)

    # ----- ramlöst fönster: flytt + meny ---------------------------------------
    def mousePressEvent(self, event):
        # Bara i ramlöst normalläge (Compact). Classic och trim-läget har OS-ram.
        if event.button() == Qt.LeftButton and not (self.tweak or self.classic):
            self._drag = (event.globalPosition().toPoint(), self.pos())
            self._moved = False
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is None or not (event.buttons() & Qt.LeftButton):
            return
        start, origin = self._drag
        delta = event.globalPosition().toPoint() - start
        if abs(delta.x()) > 5 or abs(delta.y()) > 5:
            self._moved = True
        if self._moved:
            x, y = origin.x() + delta.x(), origin.y() + delta.y()
            # Magnetisk snapping mot andra cockpitfönster är kopplad till ontop.
            if self.ontop:
                x, y = win_snap.snap(self.windowTitle(), x, y, self.width(), self.height())
            self.move(x, y)

    def mouseReleaseEvent(self, _event):
        if self._drag is not None and self._moved:
            d = load_settings()
            d["x"] = self.x()
            d["y"] = self.y()
            save_settings(d)
        self._drag = None

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        for text, cb in (("CW settings...", self._open_cw_settings),
                         ("Button mapping...", self._open_panel_map),
                         ("LEDs...", self._open_led_settings),
                         ("View...", self._open_ui_mode_settings),
                         ("Station names...", self._open_station_names),
                         ("WSJT bridge...", self._open_bridge_settings),
                         ("TCI proxy...", self._open_proxy_settings)):
            menu.addAction(text, cb)
        menu.addSeparator()
        menu.addAction("About...", self._open_about)
        menu.addSeparator()
        menu.addAction("Help / ExpertSDR3 setup", lambda: webbrowser.open(HELP_URL))
        menu.addAction("Support this project (PayPal)", lambda: webbrowser.open(DONATE_URL))
        menu.addSeparator()
        menu.addAction("Exit", self.close)
        menu.exec(event.globalPos())

    # ----- dialoger -------------------------------------------------------------
    def _open_cw_settings(self):
        """Liveinställningar för keyer/sidoton. 'Apply' och 'Save' tillämpar båda direkt i
        router.keyer/router.sidetone (inget omstartskrav, även byte av Key type); 'Save' skriver
        dessutom till tcisync_settings.json och döljer fönstret. Dialogen återanvänds."""
        if self._cw_win is not None:
            self._cw_win.show()
            self._cw_win.raise_()
            return
        keyer, sidetone = self.router.keyer, self.router.sidetone
        cw_cfg = dict(DEFAULT_CW_SETTINGS)
        cw_cfg.update(load_settings().get("cw", {}))
        win = _dialog(self, "CW settings")
        self._cw_win = win
        form = QFormLayout()

        key_type = QComboBox()
        key_type.addItems(["paddle", "straight"])
        key_type.setCurrentText(self.router.key_type)
        form.addRow("Key type", key_type)

        def gv(name):
            # dot_pct/dash_pct/ptt_cw_delay_ms finns bara på IambicKeyer - om StraightKeyer är
            # aktiv visas ändå sparade/default-värden, så de finns kvar om man byter tillbaka.
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
        for label, key, val in fields:
            e = QLineEdit(str(val))
            e.setFixedWidth(80)
            form.addRow(label, e)
            entries[key] = e

        # Sidetone-volym: slider som verkar omedelbart, inte bakom Apply/Save.
        vol = QSlider(Qt.Horizontal)
        vol.setRange(0, 100)
        vol.setValue(int(cw_cfg.get("volume_pct", 35)))
        vol.setFixedWidth(110)
        vol_lbl = QLabel(str(vol.value()))
        vol_lbl.setFixedWidth(30)

        def on_volume(v):
            vol_lbl.setText(str(v))
            sidetone.reconfigure(volume=v / 100.0)

        vol.valueChanged.connect(on_volume)
        vrow = QHBoxLayout()
        vrow.addWidget(vol)
        vrow.addWidget(vol_lbl)
        form.addRow("Sidetone volume (%)", vrow)

        def read_values():
            try:
                vals = {k: int(float(e.text())) for k, e in entries.items()}
                vals["volume_pct"] = vol.value()
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
            new_type = key_type.currentText()
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
            vals["key_type"] = key_type.currentText()
            d = load_settings()
            d["cw"] = vals
            save_settings(d)
            self._log("CW settings: saved to tcisync_settings.json")
            win.hide()

        outer = QVBoxLayout(win)
        outer.addLayout(form)
        outer.addLayout(_button_row([("Apply", apply_live), ("Save", do_save)]))
        win.show()

    # (rad, kolumn) för varje knapp i panelbilden - samma fysiska placering som ESDR3:s egen
    # E-Coder-konfigurationsbild. Huvudvredet upptar den tomma mitten av rad 1-2, kolumn 1-2.
    _PANEL_POSITIONS = {
        "zoom_up": (0, 0), "split": (0, 1), "switch_rx": (0, 2),
        "rit_reset": (0, 3), "listen_ab": (0, 4),
        "zoom_down": (1, 0), "step_down": (1, 1), "step_up": (1, 2),
        "if_as_rit": (1, 3), "listen_a": (1, 4),
        "tune": (2, 0), "listen_b": (2, 4),
        "change_vfo": (3, 0), "vfob_onoff": (3, 4),
    }

    def _open_panel_map(self):
        """Grafisk knappmappning: en klickbar panelbild i samma fysiska layout som ESDR3:s egen
        E-Coder-konfiguration. Klick på en knapp öppnar en popup med funktionsval (TCISyncs egna,
        se panel_functions.FUNCTION_CATALOG) och en valfri parameter. Ändringen tillämpas live på
        router.panel_map och sparas direkt. Knapparna märks bara med fysiskt nummer."""
        if self._panel_win is not None:
            self._panel_win.show()
            self._panel_win.raise_()
            return
        from panel_functions import FUNCTION_CATALOG, BUTTON_LAYOUT, KNOB_PRESS_LAYOUT

        win = _dialog(self, "Button mapping")
        self._panel_win = win
        self._panel_buttons = {}   # key -> QPushButton (för att uppdatera etiketten efter ändring)

        def label_for(key, display_name):
            entry = self.router.panel_map.get(key, {"func": "unused"})
            func = entry.get("func", "unused")
            func_label = FUNCTION_CATALOG.get(func, FUNCTION_CATALOG["unused"])["label"]
            return "%s\n%s" % (display_name, func_label)

        outer = QVBoxLayout(win)
        knob_row = QHBoxLayout()
        knob_row.addStretch(1)
        knob_row.addWidget(QLabel("Knob press:"))
        for key, display_name in KNOB_PRESS_LAYOUT:
            btn = QPushButton(label_for(key, display_name))
            btn.setFixedSize(140, 40)
            btn.clicked.connect(lambda _=False, k=key, n=display_name: self._open_function_popup(k, n))
            knob_row.addWidget(btn)
            self._panel_buttons[key] = btn
        knob_row.addStretch(1)
        outer.addLayout(knob_row)

        grid = QGridLayout()
        for num, key in BUTTON_LAYOUT:
            display_name = "Button %d" % num
            r, c = self._PANEL_POSITIONS[key]
            btn = QPushButton(label_for(key, display_name))
            btn.setFixedSize(120, 50)
            btn.clicked.connect(lambda _=False, k=key, n=display_name: self._open_function_popup(k, n))
            grid.addWidget(btn, r, c)
            self._panel_buttons[key] = btn
        outer.addLayout(grid)
        win.show()

    def _open_function_popup(self, key, name):
        """Popup för EN knapp: funktionsval + parameter. OK skriver till router.panel_map (live)
        och tcisync_settings.json direkt och uppdaterar knappens etikett."""
        from panel_functions import FUNCTION_CATALOG

        func_ids = list(FUNCTION_CATALOG.keys())
        func_labels = [FUNCTION_CATALOG[f]["label"] for f in func_ids]
        label_to_id = dict(zip(func_labels, func_ids))
        id_to_label = dict(zip(func_ids, func_labels))

        entry = self.router.panel_map.get(key, {"func": "unused"})
        cur_func = entry.get("func", "unused")
        cur_param = entry.get("param", "")

        pop = _dialog(self._panel_win or self, name)
        pop.setWindowModality(Qt.WindowModal)
        form = QFormLayout()
        combo = QComboBox()
        combo.addItems(func_labels)
        combo.setCurrentText(id_to_label.get(cur_func, id_to_label["unused"]))
        combo.setMinimumWidth(210)
        form.addRow("Function", combo)
        param = QLineEdit("" if cur_param in (None, "") else str(cur_param))
        param.setFixedWidth(120)
        form.addRow("Parameter", param)

        def on_ok():
            func = label_to_id.get(combo.currentText(), "unused")
            item = {"func": func}
            param_raw = param.text().strip()
            if param_raw:
                item["param"] = param_raw
            self.router.panel_map[key] = item
            d = load_settings()
            pm = d.get("panel_map", {})
            pm[key] = item
            d["panel_map"] = pm
            save_settings(d)
            self._log("Button mapping: %s -> %s" % (name, FUNCTION_CATALOG[func]["label"]))
            self._panel_buttons[key].setText("%s\n%s" % (name, FUNCTION_CATALOG[func]["label"]))
            pop.accept()

        outer = QVBoxLayout(pop)
        outer.addLayout(form)
        outer.addLayout(_button_row([("OK", on_ok), ("Cancel", pop.reject)]))
        pop.exec()

    def _open_led_settings(self):
        """De tre status-LED:erna (vänster/höger/mitten) kan var och en spegla ON/OFF-läget för
        valfri knapps funktion (se panel_functions.LED_TARGETS / EncoderRouter.led_states).
        Huvudvredets ljus och tangentbordets bakgrundsbelysning delar en enda av/på."""
        from panel_functions import LED_TARGETS

        win = _dialog(self, "LEDs")
        win.setAttribute(Qt.WA_DeleteOnClose)
        target_labels = [label for _key, label in LED_TARGETS]
        key_to_label = dict(LED_TARGETS)
        label_to_key = {label: key for key, label in LED_TARGETS}

        form = QFormLayout()
        combos = {}
        for slot, disp in (("left", "Left knob LED"), ("right", "Right knob LED"),
                           ("center", "Center knob LED")):
            c = QComboBox()
            c.addItems(target_labels)
            c.setCurrentText(key_to_label.get(self.router.led_map.get(slot, "none"), "None"))
            c.setMinimumWidth(200)
            form.addRow(disp, c)
            combos[slot] = c
        backlight = QCheckBox("Backlight (main knob + keypad)")
        backlight.setChecked(bool(self.router.backlight_on))

        def on_ok():
            new_map = {slot: label_to_key.get(c.currentText(), "none") for slot, c in combos.items()}
            self.router.led_map = new_map
            self.router.backlight_on = backlight.isChecked()
            d = load_settings()
            d["leds"] = {"map": new_map, "backlight": self.router.backlight_on}
            save_settings(d)
            self._log("LEDs: saved")
            win.accept()

        outer = QVBoxLayout(win)
        outer.addLayout(form)
        outer.addWidget(backlight)
        outer.addLayout(_button_row([("OK", on_ok), ("Cancel", win.reject)]))
        win.show()

    def _open_ui_mode_settings(self):
        """Compact (ramlöst, flytt med musen) eller Classic (vanlig fönsterram). Gäller efter
        omstart, ramflaggor sätts när fönstret skapas. Alltid överst slår igenom direkt."""
        win = _dialog(self, "View")
        win.setAttribute(Qt.WA_DeleteOnClose)
        form = QFormLayout()
        mode = QComboBox()
        mode.addItems(["compact", "classic"])
        mode.setCurrentText("classic" if self.classic else "compact")
        mode.setMinimumWidth(160)
        form.addRow("UI mode", mode)
        ontop = QCheckBox("Always on top")
        ontop.setChecked(self.ontop)

        def on_ok():
            d = load_settings()
            d["ui_mode"] = mode.currentText()
            self.ontop = ontop.isChecked()
            self._apply_ontop()
            d["ontop"] = self.ontop
            save_settings(d)
            self._log("UI mode: saved (%s) - restart to apply" % mode.currentText())
            win.accept()

        outer = QVBoxLayout(win)
        outer.addLayout(form)
        outer.addWidget(_note("Takes effect after restart."))
        outer.addWidget(ontop)
        outer.addLayout(_button_row([("OK", on_ok), ("Cancel", win.reject)]))
        # Andra ingången till Help/Support, utöver högerklicksmenyn.
        outer.addWidget(LinkLabel("Help / ExpertSDR3 setup", HELP_URL))
        outer.addWidget(LinkLabel("Support this project (PayPal)", DONATE_URL))
        win.show()

    def _open_station_names(self):
        """Eget namn per radio i stället för auto-detekterade 'Local'/'Remote'. Sparas under
        station_names (nyckel = radio.key); rollen kan rättas om profilfilerna är inaktuella."""
        win = _dialog(self, "Station names")
        win.setAttribute(Qt.WA_DeleteOnClose)
        grid = QGridLayout()
        names, roles = {}, {}
        for i, r in enumerate(self.station.radios):
            grid.addWidget(QLabel("%s (%s)" % (r.place, r.key)), i, 0)
            e = QLineEdit(r.name)
            e.setFixedWidth(160)
            grid.addWidget(e, i, 1)
            names[r.key] = e
            c = QComboBox()
            c.addItems(["local", "remote"])
            c.setCurrentText(r.place)
            grid.addWidget(c, i, 2)
            roles[r.key] = c

        def on_ok():
            d = load_settings()
            saved_names = d.get("station_names", {})
            overrides = d.get("role_overrides", {})
            for key, e in names.items():
                val = e.text().strip()
                if val:
                    saved_names[key] = val
            for key, c in roles.items():
                overrides[key] = c.currentText()
            d["station_names"] = saved_names
            d["role_overrides"] = overrides
            save_settings(d)
            self._log("Station names/roles: saved - restart to apply")
            win.accept()

        outer = QVBoxLayout(win)
        outer.addLayout(grid)
        outer.addWidget(_note("Role: fix a wrong auto-detected Local/Remote (happens if "
                              "ExpertSDR3's profile files are stale, see README). Takes effect "
                              "after restart."))
        outer.addLayout(_button_row([("OK", on_ok), ("Cancel", win.reject)]))
        win.show()

    def _open_bridge_settings(self):
        """WSJT-bron: en lokal TCI-server som WSJT-X ansluter till (TCI Client RX1, localhost:<port>).
        Platsknapparna i synk-stripan väljer vilken radio som hörs och vilken som sänder. Sparas
        under wsjt_bridge. Här sätts också portarna och styrgränssnittet för andra program."""
        win = _dialog(self, "WSJT bridge")
        win.setAttribute(Qt.WA_DeleteOnClose)
        cfg = load_settings().get("wsjt_bridge", {})
        en = QCheckBox("Enable WSJT bridge")
        en.setChecked(self.bridge.running)
        port = QLineEdit(str(self.bridge.port))
        port.setFixedWidth(80)
        ctl = QCheckBox("Enable control interface (localhost, for other programs)")
        ctl.setChecked(self.control.running)
        cport = QLineEdit(str(self.control.port))
        cport.setFixedWidth(80)

        def on_ok():
            try:
                p = int(port.text())
            except ValueError:
                p = int(cfg.get("port", BRIDGE_DEFAULT_PORT))
            try:
                cp = int(cport.text())
            except ValueError:
                cp = int(cfg.get("control_port", CONTROL_DEFAULT_PORT))
            want = en.isChecked()
            if self.bridge.running and (not want or p != self.bridge.port):
                self._bridge_stop()
            self.bridge.port = p
            if want and not self.bridge.running:
                self._bridge_start()
            want_ctl = ctl.isChecked()
            if self.control.running and (not want_ctl or cp != self.control.port):
                self.control.stop()
            self.control.port = cp
            if want_ctl and not self.control.running:
                self._control_start()
            d = load_settings()
            saved = d.get("wsjt_bridge", {})
            saved.update({"enabled": self.bridge.running, "port": p,
                          "control": self.control.running, "control_port": cp})
            d["wsjt_bridge"] = saved
            save_settings(d)
            win.accept()

        form = QFormLayout()
        form.addRow("Port", port)
        form2 = QFormLayout()
        form2.addRow("Control port", cport)
        outer = QVBoxLayout(win)
        outer.addWidget(en)
        outer.addLayout(form)
        outer.addWidget(_note("In WSJT-X: Settings > Radio > Rig = TCI Client RX1, network address "
                              "localhost:<port>. The place buttons in the sync strip choose which "
                              "radio you hear and which one transmits."))
        outer.addWidget(ctl)
        outer.addLayout(form2)
        outer.addLayout(_button_row([("OK", on_ok), ("Cancel", win.reject)]))
        win.show()

    def _open_proxy_settings(self):
        """TCI-proxyn: en port som skickar allt oförändrat till en radio och håller klientens
        anslutning öppen när radion försvinner. Sparas i tcisync_settings.json under tci_proxy."""
        win = _dialog(self, "TCI proxy")
        win.setAttribute(Qt.WA_DeleteOnClose)
        cfg = load_settings().get("tci_proxy", {})
        radios = list(self.station.radios)
        labels = ["%s (port %d)" % (r.name, r.port) for r in radios]
        cur = self._proxy_radio(cfg)
        en = QCheckBox("Enable TCI proxy")
        en.setChecked(self.proxy is not None)
        port = QLineEdit(str(cfg.get("port", PROXY_DEFAULT_PORT)))
        port.setFixedWidth(80)
        radio = QComboBox()
        radio.addItems(labels)
        if cur in radios:
            radio.setCurrentIndex(radios.index(cur))
        log_traffic = QCheckBox("Log traffic (tcisync_proxy_traffic.log)")
        log_traffic.setChecked(bool(cfg.get("log_traffic")))
        rx1 = QCheckBox("Show only RX1 to the client (hides RX2)")
        rx1.setChecked(bool(cfg.get("only_rx1")))

        def on_ok():
            try:
                p = int(port.text())
            except ValueError:
                p = int(cfg.get("port", PROXY_DEFAULT_PORT))
            idx = radio.currentIndex()
            r = radios[idx] if 0 <= idx < len(radios) else cur
            wanted = {"port": p, "radio": r.key if r else None,
                      "log_traffic": log_traffic.isChecked(), "only_rx1": rx1.isChecked()}
            before = {"port": int(cfg.get("port", PROXY_DEFAULT_PORT)),
                      "radio": cur.key if cur else None, "log_traffic": bool(cfg.get("log_traffic")),
                      "only_rx1": bool(cfg.get("only_rx1"))}
            d = load_settings()
            d["tci_proxy"] = dict(d.get("tci_proxy", {}), **wanted)
            save_settings(d)
            # En omstart kopplar bort anslutna klienter, så den görs bara när något faktiskt ändrats.
            if self.proxy is not None and (not en.isChecked() or wanted != before):
                self._proxy_stop()
            if en.isChecked() and self.proxy is None:
                self._proxy_start()
            d["tci_proxy"]["enabled"] = self.proxy is not None
            save_settings(d)
            win.accept()

        form = QFormLayout()
        form.addRow("Port", port)
        form.addRow("Radio", radio)
        outer = QVBoxLayout(win)
        outer.addWidget(en)
        outer.addLayout(form)
        outer.addWidget(log_traffic)
        outer.addWidget(rx1)
        outer.addWidget(_note("For programs such as Log4OM: connect to localhost:<port> instead of "
                              "the radio's own TCI port. Everything is passed through, and the "
                              "connection stays open when ExpertSDR3 restarts. Changing a setting "
                              "disconnects the client, restart its CAT afterwards."))
        outer.addLayout(_button_row([("OK", on_ok), ("Cancel", win.reject)]))
        win.show()

    def _open_about(self):
        """Enda platsen där versionsnumret syns i Compact-läge (ingen titelrad där)."""
        if self._about_win is not None:
            self._about_win.show()
            self._about_win.raise_()
            return
        win = _dialog(self, "About")
        self._about_win = win
        outer = QVBoxLayout(win)
        title = QLabel("TCISync (by SM5K)")
        title.setAlignment(Qt.AlignCenter)
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        outer.addWidget(title)
        ver = QLabel("Version %s" % VERSION)
        ver.setAlignment(Qt.AlignCenter)
        ver.setStyleSheet("color:%s" % th.color("text_secondary"))
        outer.addWidget(ver)
        outer.addWidget(LinkLabel("github.com/sm5tog/TCISync", "https://github.com/%s" % UPDATE_REPO))
        outer.addWidget(LinkLabel("Help / ExpertSDR3 setup", HELP_URL))
        outer.addWidget(LinkLabel("Support this project (PayPal)", DONATE_URL))
        outer.addLayout(_button_row([("Close", win.hide)]))
        win.show()

    # ----- avslut ---------------------------------------------------------------
    def closeEvent(self, event):
        if not self._closed:
            self._closed = True
            self._pump_timer.stop()
            self._bridge_timer.stop()
            self._update_timer.stop()
            self.stop_flag.set()
            self.router.stop()
            self.control.stop()
            self.bridge.stop()
            self._proxy_stop()
            for link in self.links.values():
                link.stop()
            if self.leds:
                self.leds.off()
            if self.hid:
                try:
                    self.hid.close()
                except Exception:
                    pass
            for w in (self._cw_win, self._panel_win, self._about_win):
                if w is not None:
                    w.close()
        event.accept()
        QApplication.quit()

    # ----- start ----------------------------------------------------------------
    def present(self):
        """Visar fönstret: sparad position om den ligger på en skärm, trim-läget med eget mått."""
        self.adjustSize()
        if self.tweak:
            self.resize(680, 400)
        elif self.classic:
            self.resize(self.width(), WINDOW_H + 30)     # + OS-titelraden
            self.setMinimumHeight(WINDOW_H + 30)
        else:
            # Fast höjd som linjerar med rotorfönstret och de andra programmen på skärmen.
            self.setFixedHeight(WINDOW_H)
        cfg = load_settings()
        if "x" in cfg and "y" in cfg and not self.tweak:
            pt = QPoint(int(cfg["x"]), int(cfg["y"]))
            if QGuiApplication.screenAt(pt) is not None:
                self.move(pt)
        if self.tweak:
            self.setMinimumSize(self.minimumSizeHint())
        self.show()
        set_topmost(self, self.ontop)


def run():
    tweak = "--tweak" in sys.argv[1:]
    cfg = load_settings()
    app = QApplication(sys.argv)
    apply_theme(app)
    win = Cockpit(tweak=tweak, classic=cfg.get("ui_mode") == "classic", ontop=cfg.get("ontop", True))
    win.present()
    sys.exit(app.exec())
