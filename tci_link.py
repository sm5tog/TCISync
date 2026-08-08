#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tci_link.py - TCI-WebSocket-klient, en instans per radio. Se DESIGN.md §2.

Lyssnar i egen tråd och lägger inkommande TCI-kommandon + statusändringar på en
delad kö (appen pumpar dem i GUI-tråden). `send()` är tråd-säker. Handskakning:
vänta in 'ready;' + 0.3s paus innan något skickas (bekräftat mot SunSDR3, mönster
från SM5KRigSync/ecoder). Återansluter var 2:e sekund vid tapp.

Utbruten ur ecoder_control.TciClient + SM5KRigSync.TciLink (som hade var sin kopia).
"""

import threading
import time

import websocket


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
                self._wait_ready(ws)
                if self._stop.is_set():
                    break
                self._emit(kind="status", connected=True)
                buf = ""
                while not self._stop.is_set():
                    ws.settimeout(1.0)
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not msg:
                        break
                    # En split per recv (inte en repeterad "in"-scan + split
                    # per hittat kommando) - sista elementet är ett ev.
                    # ofullständigt fragment, sparas till nästa recv.
                    parts = (buf + msg).split(";")
                    buf = parts.pop()
                    for cmd in parts:
                        cmd = cmd.strip()
                        if cmd:
                            name, _, rest = cmd.partition(":")
                            self._emit(kind="cmd", name=name.upper(),
                                       args=rest.split(",") if rest else [])
            except Exception as e:
                hint = ""
                if isinstance(e, ConnectionRefusedError) or "10061" in str(e):
                    hint = " - troligen är radion/fjärrstyrningen inte igång"
                self._emit(kind="log", text="TCI-fel (%s, %s:%d): %s%s" % (self.name, self.ip, self.port, e, hint))
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
                if "ready;" in ws.recv():
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
