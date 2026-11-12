#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tci_proxy.py - genomskinlig TCI-port mot EN radio, för program som Log4OM.

Ett enda ansvar: hålla klientens TCI-anslutning öppen även när radion (ExpertSDR3) försvinner,
så att klienten inte behöver starta om sin CAT. Varje klient får en egen TciLink mot radion,
så radion ser samma antal klienter som om de anslutit direkt och frågor (t.ex. `vfo:0,0;`)
besvaras bara till den klient som frågade. Allt skickas vidare oförändrat i båda riktningarna,
ingen mappning av mottagare och ingen PTT-spärr (det är WSJT-bron, tci_bridge.py).

Radion borta: klientens anslutning står kvar, kommandon från klienten kastas (räknas och loggas).
Radion tillbaka: TciLink återansluter (var 2:a s) och klienten får radions nya startmeddelanden
utom huvudet (protocol, device, gränser, ready), alltså bara lägesraderna (frekvens, mode,
split m.m.). Huvudet och `ready;` skickas bara vid första anslutningen.

Valfri trafiklogg (`traffic_path`): varje textmeddelande i båda riktningarna med tidsstämpel,
roteras vid TRAFFIC_MAX_BYTES till <fil>.1. Ljudblock (binärt) loggas inte.

Valfritt läge "bara RX1" (`only_rx1`, 0.24.0): klienten ser en radio med en enda mottagare.
`trx_count` skrivs om till 1, kommandon med mottagarnummer (TRX_INDEXED) för annan mottagare än
0 kastas åt båda hållen, och `tx_frequency` (som saknar mottagarnummer men följer den mottagare
som sänder) kastas medan RX2 sänder. Byggt för Log4OM, som blandar ihop RX1 och RX2 när den ser två.
"""

import os
import threading
import time

from tci_link import TciLink
from ws_server import WsServer

DEFAULT_PORT = 40011
TRAFFIC_MAX_BYTES = 20 * 1024 * 1024
# Startmeddelanden som beskriver radion och anslutningen, inte dess läge. Skickas bara till
# klienten vid första anslutningen; vid återanslutning skulle de se ut som en ny radio.
HEADER_NAMES = {"protocol", "device", "receive_only", "trx_count", "channels_count",
                "channel_count", "vfo_limits", "if_limits", "modulations_list", "ready"}


# Kommandon vars första argument är mottagarens/transceiverns nummer (TCI-specen 2.x plus
# det som ExpertSDR3 faktiskt skickar, t.ex. rx_enable och rx_clicked_on_spot).
TRX_INDEXED = {
    "dds", "if", "vfo", "vfo_lock", "lock", "modulation", "split_enable", "trx", "tune",
    "drive", "tune_drive", "tx_enable", "rit_enable", "rit_offset", "xit_enable", "xit_offset",
    "rx_enable", "rx_filter_band", "rx_channel_enable", "rx_nb_enable", "rx_nb_param",
    "rx_anc_enable", "rx_anf_enable", "rx_apf_enable", "rx_bin_enable", "rx_dse_enable",
    "rx_nr_enable", "rx_nf_enable", "rx_mute", "rx_volume", "rx_balance", "sql_enable",
    "sql_level", "agc_mode", "agc_gain", "rx_sensors", "rx_channel_sensors", "tx_sensors",
    "rx_smeter", "keyer", "tx_footswitch", "tx_footcwitch", "audio_start", "audio_stop",
    "iq_start", "iq_stop", "line_out_start", "line_out_stop", "line_out_recorder_start",
    "line_out_recorder_save", "line_out_recorder_break", "rx_clicked_on_spot",
}


def _split(cmd):
    """'vfo:1,0,7074000' -> ('vfo', ['1', '0', '7074000'])."""
    name, _, rest = cmd.partition(":")
    return name.strip().lower(), [a.strip() for a in rest.split(",")] if rest else []


class Rx1Filter:
    """Läget "bara RX1": filtrerar en klients trafik så att den ser en mottagare. Håller reda
    på om RX2 sänder, för att kunna kasta `tx_frequency` då."""

    def __init__(self):
        self.rx2_tx = False

    @staticmethod
    def _other_rx(name, args):
        return name in TRX_INDEXED and bool(args) and args[0].isdigit() and args[0] != "0"

    def to_client(self, text):
        keep = []
        for cmd in text.split(";"):
            c = cmd.strip()
            if not c:
                continue
            name, args = _split(c)
            if name == "trx" and len(args) >= 2 and args[0] == "1":
                self.rx2_tx = args[1].lower() == "true"
            if self._other_rx(name, args):
                continue
            if name == "tx_frequency" and self.rx2_tx:
                continue
            if name == "trx_count":
                c = "trx_count:1"
            keep.append(c)
        return "".join(c + ";" for c in keep)

    def to_radio(self, text):
        keep = [c.strip() for c in text.split(";")
                if c.strip() and not self._other_rx(*_split(c))]
        return "".join(c + ";" for c in keep)


def strip_header(text):
    """`text` utan huvudkommandona (HEADER_NAMES). Tom sträng om inget återstår."""
    keep = []
    for cmd in text.split(";"):
        c = cmd.strip()
        if c and c.partition(":")[0].strip().lower() not in HEADER_NAMES:
            keep.append(c)
    return "".join(c + ";" for c in keep)


class _Sink:
    """Tar emot TciLinks händelser (som annars går till appens kö) och loggar bara
    övergångarna ansluten/borta och det första felet i en följd."""

    def __init__(self, session):
        self._session = session

    def put(self, ev):
        self._session.on_link_event(ev)


class _TrafficLog:
    def __init__(self, path, max_bytes=TRAFFIC_MAX_BYTES):
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, client_id, direction, text):
        t = time.time()
        line = "%s.%03d #%d %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
                                       int(t * 1000) % 1000, client_id, direction, text.strip())
        with self._lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass


class _Session:
    """En klientanslutning och dess egen länk mot radion."""

    def __init__(self, proxy, conn, client_id):
        self.proxy = proxy
        self.conn = conn
        self.id = client_id
        self.radio_up = False
        self.dropped = 0            # kommandon från klienten medan radion var borta
        self._error_logged = False
        self.filter = Rx1Filter() if proxy.only_rx1 else None
        self.link = TciLink("proxy%d" % client_id, proxy.radio_ip, proxy.radio_port, _Sink(self),
                            name="TCI proxy #%d" % client_id)
        self.link.connect_listeners.append(self._on_connected)
        self.link.raw_listeners.append(self._on_radio)

    def _log(self, text):
        self.proxy._log("TCI proxy: client #%d %s" % (self.id, text))

    def on_link_event(self, ev):
        kind = ev.get("kind")
        if kind == "status" and not ev.get("connected") and self.radio_up:
            self.radio_up = False
            self._log("lost the radio, keeping the client connected")
        elif kind == "log" and not self._error_logged and not self.radio_up:
            self._error_logged = True
            self._log("radio not reachable (%s:%d), retrying every 2 s"
                      % (self.proxy.radio_ip, self.proxy.radio_port))

    def _on_connected(self, _key, number):
        """Länken har fått radions `ready;`. Första gången går hela starten till klienten,
        därefter bara lägesraderna."""
        first = number == 1
        sent = 0
        for text in list(self.link.hello):
            out = text if first else strip_header(text)
            if out and self._send_client(out):
                sent += 1
        self.radio_up = True
        self._error_logged = False
        if first:
            self._log("radio connected, %d start messages sent" % sent)
        else:
            self._log("radio back (connection #%d), %d state messages sent, %d client commands dropped "
                      "while it was away" % (number, sent, self.dropped))
        self.dropped = 0

    def _on_radio(self, _key, msg):
        if isinstance(msg, bytes):
            try:
                self.conn.send_binary(msg)
            except (ConnectionError, OSError):
                pass
            return
        self._send_client(msg)

    def _send_client(self, text):
        if self.filter is not None:
            text = self.filter.to_client(text)
            if not text:
                return False
        if self.proxy.traffic is not None:
            self.proxy.traffic.write(self.id, "<", text)
        try:
            self.conn.send_text(text)
            return True
        except (ConnectionError, OSError):
            return False

    def run(self):
        self.link.start()
        cause = ""
        try:
            while True:
                msg = self.conn.recv()
                if isinstance(msg, bytes):
                    ok = self.link.send_binary(msg)
                else:
                    if self.proxy.traffic is not None:
                        self.proxy.traffic.write(self.id, ">", msg)
                    if self.filter is not None:
                        msg = self.filter.to_radio(msg)
                        if not msg:
                            continue
                    ok = self.link.send(msg)
                if not ok:
                    self.dropped += 1
        except (ConnectionError, OSError) as e:
            cause = " (%s)" % e
        finally:
            self.radio_up = False           # länken stängs av oss, inget radiotapp att logga
            self.link.stop()
            self._log("disconnected%s" % cause)


class TciProxy:
    def __init__(self, radio_ip, radio_port, port=DEFAULT_PORT, log=None, host="127.0.0.1",
                 traffic_path=None, only_rx1=False):
        self.radio_ip = radio_ip
        self.only_rx1 = only_rx1
        self.radio_port = radio_port
        self.port = port
        self.host = host
        self._log = log or (lambda _t: None)
        self.traffic = _TrafficLog(traffic_path) if traffic_path else None
        self._server = None
        self._lock = threading.Lock()
        self._sessions = set()
        self._next_id = 1

    def start(self):
        if self._server is not None:
            return
        self._server = WsServer(self.host, self.port, self._serve_client, log=self._log)
        self._server.start()
        if self.port == 0:
            self.port = self._server._srv.getsockname()[1]
        self._log("TCI proxy: listening on localhost:%d for the radio at %s:%d%s%s" % (
            self.port, self.radio_ip, self.radio_port,
            ", traffic log on" if self.traffic is not None else "",
            ", clients see only RX1" if self.only_rx1 else ""))

    def stop(self):
        if self._server is None:
            return
        self._server.stop()
        self._server = None
        with self._lock:
            sessions = list(self._sessions)
        for s in sessions:
            s.conn.close()
            s.link.stop()
        self._log("TCI proxy: stopped")

    @property
    def running(self):
        return self._server is not None

    def status(self):
        """(antal klienter, antal av dem med radion ansluten)."""
        with self._lock:
            sessions = list(self._sessions)
        return len(sessions), sum(1 for s in sessions if s.radio_up)

    def _serve_client(self, conn):
        with self._lock:
            cid = self._next_id
            self._next_id += 1
            session = _Session(self, conn, cid)
            self._sessions.add(session)
        session._log("connected, sees only RX1" if self.only_rx1 else "connected")
        try:
            session.run()
        finally:
            with self._lock:
                self._sessions.discard(session)
