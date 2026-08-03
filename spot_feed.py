#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spot_feed.py - Telnet-klient mot jtspots DX-klusterserver (127.0.0.1:7300).

Läser samma rader som JTAlert/Log4OM skulle fått ("DX de <av>: <freq> <call>
<comment> <utc>"). Kommentarens första ord är regelnamnet jtspots redan taggar
varje spot med (se global CLAUDE.md-regel om regelnamn i telnet-kommentaren).
Bara ATNO/MTH/DX/DXP-regelträffar är intressanta här — resten ignoreras tyst.

Eget litet TCP-klientmönster, samma stil som tci_link.TciLink: egen tråd,
återansluter var 2:e sekund vid tapp, lägger tolkade spots på den delade
händelsekön.
"""

import re
import socket
import threading
import time

RECONNECT_DELAY_S = 2.0

_LINE_RE = re.compile(
    r"^DX de \S+:\s+(?P<freq_khz>[\d.]+)\s+(?P<call>\S+)\s+"
    r"(?P<comment>.+?)\s+(?P<utc>\d{4}Z)\s*$"
)

WANTED_RULES = ("ATNO", "MTH", "DX", "DXP")


def _parse_line(line):
    """Rad -> dict (freq_hz, call, rule_name, digital) eller None om ointressant.

    Regelnamnet (ATNO/MTH) kan stå var som helst i kommentaren, inte bara som
    första ord (flera regler kan ha matchat samma spot, se _emit_common i
    jtspots som skriver in ALLA matchande regelnamn kommaseparerat)."""
    m = _LINE_RE.match(line.strip())
    if not m:
        return None
    comment = m.group("comment").strip()
    comment_upper = comment.upper()
    rule_name = next((r for r in WANTED_RULES
                       if re.search(r"\b%s\b" % re.escape(r), comment_upper)), None)
    if rule_name is None:
        return None
    try:
        freq_hz = int(round(float(m.group("freq_khz")) * 1000))
    except ValueError:
        return None
    return {
        "call": m.group("call"),
        "freq_hz": freq_hz,
        "rule_name": rule_name,
        "digital": "FT8" in comment_upper,
    }


class SpotFeed:
    def __init__(self, host, port, events):
        self.host = host
        self.port = port
        self.events = events        # delad queue.Queue (samma som TciLink)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._read_forever()
            except Exception:
                pass
            if self._stop.is_set():
                break
            time.sleep(RECONNECT_DELAY_S)

    def _read_forever(self):
        sock = socket.create_connection((self.host, self.port), timeout=3)
        try:
            sock.settimeout(1.0)
            buf = ""
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk.decode(errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    spot = _parse_line(line)
                    if spot is not None:
                        self.events.put({"kind": "spot", **spot})
        finally:
            try:
                sock.close()
            except Exception:
                pass
