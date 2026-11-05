#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge_control.py - styrgränssnitt mot WSJT-bron för andra program på samma dator.

Ett enda ansvar: ta emot JSON-rader över TCP på localhost, skicka varje begäran till en
`handler(req) -> dict` och skriva tillbaka svaret som en JSON-rad. Ingen TCI-kunskap och ingen
GUI-kunskap här; handlern (i tcisync.py) avgör vad kommandona betyder och körs på rätt tråd.

Protokoll (en JSON-rad per begäran och per svar, UTF-8, avslutad med \\n):
    {"cmd": "status"}
    {"cmd": "set_place", "place": "ramnas" | "blecket"}
    {"cmd": "set_dial", "hz": 10136000}
Svar: {"ok": true, ...} eller {"ok": false, "error": "text"}. Okänd rad eller trasig JSON ger
{"ok": false, "error": ...} och anslutningen lever kvar.
"""

import json
import socket
import threading

DEFAULT_PORT = 60010
MAX_LINE = 4096


class ControlServer:
    def __init__(self, handler, port=DEFAULT_PORT, host="127.0.0.1", log=None):
        self._handler = handler
        self.port = port
        self.host = host
        self._log = log or (lambda _t: None)
        self._srv = None
        self._conns = set()
        self._lock = threading.Lock()

    @property
    def running(self):
        return self._srv is not None

    def start(self):
        if self._srv is not None:
            return
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(4)
        self._srv = srv
        threading.Thread(target=self._accept_loop, args=(srv,), daemon=True).start()
        self._log("Control interface: listening on %s:%d" % (self.host, self.port))

    def stop(self):
        srv, self._srv = self._srv, None
        if srv is None:
            return
        try:
            srv.close()
        except OSError:
            pass
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            try:
                c.close()
            except OSError:
                pass
        self._log("Control interface: stopped")

    def _accept_loop(self, srv):
        while True:
            try:
                conn, _addr = srv.accept()
            except OSError:
                return
            with self._lock:
                self._conns.add(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    conn.sendall(self._answer(line) + b"\n")
                if len(buf) > MAX_LINE:
                    conn.sendall(self._reply({"ok": False, "error": "line too long"}) + b"\n")
                    return
        except OSError:
            return
        finally:
            with self._lock:
                self._conns.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _answer(self, line):
        try:
            req = json.loads(line.decode("utf-8"))
            if not isinstance(req, dict) or "cmd" not in req:
                return self._reply({"ok": False, "error": "expected a JSON object with 'cmd'"})
        except (ValueError, UnicodeDecodeError):
            return self._reply({"ok": False, "error": "invalid JSON"})
        try:
            return self._reply(self._handler(req))
        except Exception as e:                        # handlern får aldrig fälla servern
            self._log("Control interface: handler error: %r" % (e,))
            return self._reply({"ok": False, "error": "internal error: %s" % e})

    @staticmethod
    def _reply(obj):
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")
