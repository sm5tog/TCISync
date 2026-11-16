#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tci_link.py - TCI-WebSocket-klient, en instans per radio. Se DESIGN.md §2.

Lyssnar i egen tråd och lägger inkommande TCI-kommandon + statusändringar på en
delad kö (appen pumpar dem i GUI-tråden). `send()` är tråd-säker. Handskakning:
vänta in 'ready;' + 0.3s paus innan något skickas (bekräftat mot SunSDR3, mönster
från SM5KRigSync/ecoder). Återansluter var 2:e sekund vid tapp.

Utbruten ur ecoder_control.TciClient + SM5KRigSync.TciLink (som hade var sin kopia).
"""

import re
import threading
import time

import websocket


_POWER_RE = re.compile(r"(^|;)\s*(start|stop)\s*;", re.IGNORECASE)


def hello_with_power(hello, running):
    """Returns a copy of the saved hello where the radio's power message (start; or stop;) matches the radio's
    current state. The radio sends it once in the hello and again live when the device is stopped or started
    (the on/off button in ExpertSDR). WSJT-X reads it in the hello to decide whether the radio is switched on, so a
    stale 'stop;' makes it give up. If the hello has no power message, one is inserted before 'ready;'."""
    word = "start;" if running else "stop;"
    out, found = [], False
    for text in hello:
        new = _POWER_RE.sub(lambda m: m.group(1) + word, text)
        found = found or new != text or bool(_POWER_RE.search(text))
        out.append(new)
    if not found:
        pos = next((i for i, t in enumerate(out) if t.strip().lower().startswith("ready")), len(out))
        out.insert(pos, word)
    return out


class TciLink:
    def __init__(self, radio_key, ip, port, events, name=None):
        self.key = radio_key
        self.ip = ip
        self.port = port
        self.name = name or radio_key
        self.events = events                # delad queue.Queue
        self._ws = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.raw_listeners = []             # callbacks (radio_key, msg) för varje mottaget meddelande (str eller bytes)
        self.hello = []                     # textmeddelanden fram till och med 'ready;' vid senaste anslutningen
        self.connect_listeners = []         # callbacks (radio_key, anslutningsnummer) efter varje lyckad (åter)anslutning
        self.connections = 0                # antal lyckade anslutningar sedan start

    def _notify_connected(self):
        for cb in list(self.connect_listeners):
            try:
                cb(self.key, self.connections)
            except Exception:
                pass

    def _notify_raw(self, msg):
        for cb in list(self.raw_listeners):
            try:
                cb(self.key, msg)
            except Exception:
                pass

    def start(self):
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None

    def _emit(self, **kw):
        self.events.put({"radio": self.key, **kw})

    def _run(self):
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection("ws://%s:%d" % (self.ip, self.port),
                                                 timeout=3)
                with self._lock:
                    self._ws = ws
                self.hello = []
                self._wait_ready(ws)
                if self._stop.is_set():
                    break
                self.connections += 1
                self._emit(kind="status", connected=True)
                self._notify_connected()
                buf = ""
                while not self._stop.is_set():
                    ws.settimeout(1.0)
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not msg:
                        break
                    self._notify_raw(msg)
                    if isinstance(msg, bytes):
                        continue            # binärt strömblock (ljud), inget kommando att tolka
                    # En split per recv (inte en repeterad "in"-scan + split
                    # per hittat kommando) - sista elementet är ett ev.
                    # ofullständigt fragment, sparas till nästa recv.
                    parts = (buf + msg).split(";")
                    buf = parts.pop()
                    for cmd in parts:
                        cmd = cmd.strip()
                        if cmd:
                            name, _, rest = cmd.partition(":")
                            if name.lower() in ("start", "stop") and not rest:
                                # Keep the saved hello in step with the radio (replaced as a whole, the
                                # bridge and proxy read the list from other threads).
                                self.hello = hello_with_power(self.hello, name.lower() == "start")
                            self._emit(kind="cmd", name=name.upper(),
                                       args=rest.split(",") if rest else [])
            except Exception as e:
                hint = ""
                if isinstance(e, ConnectionRefusedError) or "10061" in str(e):
                    hint = " - the radio or remote control is probably not running"
                self._emit(kind="log", text="TCI error (%s, %s:%d): %s%s" % (self.name, self.ip, self.port, e, hint))
            with self._lock:
                self._ws = None
            self._emit(kind="status", connected=False)
            if self._stop.is_set():
                break
            time.sleep(2)

    def _wait_ready(self, ws, timeout=3):
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                ws.settimeout(max(0.05, deadline - time.time()))
                msg = ws.recv()
                if isinstance(msg, bytes):
                    continue
                self.hello.append(msg)
                if "ready;" in msg.lower():
                    break
        except Exception:
            pass
        time.sleep(0.3)

    def send(self, cmd):
        """Skickar ett kommando. Håller låset genom HELA sändningen (inte bara
        vid läsning av self._ws) - annars kan två trådar (t.ex. cw_keyer.py:s
        keyer-tråd, som sänder mycket tätt under manuell nyckling, och GUI-
        tråden) råka anropa ws.send() samtidigt på samma socket okontrollerat.
        Upptäckt 2026-08-03: manipulator-nyckling (många snabba send() per
        sekund) gav opålitlig TX, medan sällanskickade CW_MACROS alltid
        fungerade - just den typen av race hade annars kunnat tappa/korrumpera
        enstaka kommandon utan att synas som fel någonstans."""
        with self._lock:
            ws = self._ws
            if ws is None:
                return False
            try:
                ws.send(cmd)
                return True
            except Exception:
                self._ws = None
        self._emit(kind="status", connected=False)
        return False

    def send_binary(self, data):
        """Skickar ett binärt strömblock (TX-ljud). Samma lås som send()."""
        with self._lock:
            ws = self._ws
            if ws is None:
                return False
            try:
                ws.send_binary(data)
                return True
            except Exception:
                self._ws = None
        self._emit(kind="status", connected=False)
        return False
