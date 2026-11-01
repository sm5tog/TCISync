#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tci_bridge.py - WSJT-bron: en lokal TCI-server som WSJT-X/WSJT-K ansluter till.

Ett enda ansvar: förbinda EN extern TCI-klient (WSJT-X) med de två radiolänkarna som
TCISync redan har, så att RX-ljudet kommer från RX-cellen och PTT/TX-ljud går till
TX-cellen. Vilka två celler det är bestäms av `mapping_fn` (bron följer inte TCISyncs
öra-/TX-markörer). Vad som hör till vilken cell avgörs av bridge_router.py,
WebSocket-protokollet av ws_server.py, ströminnehållet av tci_frames.py.

Frekvens (WSJT-X kör alltid split): dialen (VFO A) är det man hör och går bara till RX-cellen,
sändfrekvensen (VFO B) och split går bara till TX-cellen. Till WSJT-X går RX-cellens VFO A
och TX-cellens VFO B. Bron minns WSJT-X:s dial, VFO B och split och skriver dem till rätt
cell vid lägesbyte, så en ny TX-cell aldrig sänder på en gammal VFO B. Ingen annan
frekvensautomatik finns; `copy_frequency` kopierar en cells frekvens till en annan på begäran.

Säkerhet: PTT (TRX) skickas BARA härifrån och bara från WSJT-X-klienten, till TX-cellen.
Medan PTT är på ligger TX-cellen fast (även om läget byts), så avnyckling alltid går till
samma radio som nycklades. Försvinner klienten under sändning skickas avnyckling.
"""

import threading

import bridge_router as router
import tci_frames as frames
from tx_timing import TxTiming
from ws_server import WsServer

DEFAULT_PORT = 60001
SPLIT_DELTA_LIMIT_HZ = 20000                  # större skillnad mellan VFO B och VFO A är ingen split-offset
CLIENT_LOG_LIMIT = 60                         # antal första klientkommandon per anslutning som loggas
LOCAL_AUDIO = {"AUDIO_START", "AUDIO_STOP"}   # hanteras av TciBridge._sync_audio, inte av routern


class TciBridge:
    def __init__(self, links, mapping_fn, port=DEFAULT_PORT, log=None, host="127.0.0.1",
                 echo_guard=None):
        """links: {radio_key: TciLink}. mapping_fn() -> bridge_router.Mapping (RX- och TX-cell).
        echo_guard(radio_key, NAME, args) anropas för varje kommando bron skickar till en radio,
        så att andra delar av programmet (Sync) kan ignorera radions eko av det."""
        self.links = links
        self._mapping_fn = mapping_fn
        self._echo_guard = echo_guard
        self._tx_timing = None          # TxTiming för pågående sändning (diagnostik)
        self._wsjt = self._empty_wsjt() # WSJT-X:s dial (a), TX-frekvens (b) och split, som text
        self._state = {}                # senast sedda VFO/MODULATION/SPLIT_ENABLE per radio och mottagare
        self.port = port
        self.host = host
        self._log = log or (lambda _t: None)
        self._server = None
        self._lock = threading.RLock()
        self._client = None
        self._ptt_map = None            # Mapping som gällde när PTT slogs på (None = ingen sändning)
        self._audio_on = False          # klienten har begärt ljudström
        self._audio_cells = set()       # (radio_key, rx) som just nu har audio_start
        self._audio_rate = None         # senaste AUDIO_SAMPLERATE från klienten
        self._both = {}                 # senaste BOTH-kommandon (för omspelning vid cellbyte)
        self._last_map = None

    # ----- livscykel -----------------------------------------------------------
    def start(self):
        if self._server is not None:
            return
        for key, link in self.links.items():
            for text in list(link.hello):
                self._note_radio_state(key, text, None)
            link.raw_listeners.append(self._on_radio)
        self._server = WsServer(self.host, self.port, self._serve_client, log=self._log)
        self._server.start()
        self._last_map = self._mapping_fn()
        self._log("WSJT bridge: listening on localhost:%d (IPv4 and IPv6 where available)" % self.port)

    def stop(self):
        if self._server is None:
            return
        self._server.stop()
        self._server = None
        for link in self.links.values():
            if self._on_radio in link.raw_listeners:
                link.raw_listeners.remove(self._on_radio)
        with self._lock:
            client = self._client
        if client is not None:
            client.close()
        self._log("WSJT bridge: stopped")

    @property
    def running(self):
        return self._server is not None

    @property
    def client_connected(self):
        return self._client is not None

    @property
    def ptt_active(self):
        return self._ptt_map is not None

    # ----- mapping --------------------------------------------------------------
    def _map(self):
        """Mappningen för trafik just nu. Under sändning ligger TX-cellen fast."""
        m = self._mapping_fn()
        p = self._ptt_map
        if p is not None:
            m = m._replace(tx_key=p.tx_key, tx_rx=p.tx_rx)
        return m

    @staticmethod
    def _empty_wsjt():
        return {"a": None, "b": None, "split": None}

    def on_mapping_changed(self):
        """Anropas när läget (RX-/TX-cell) bytts (GUI-tråden). Flyttar ljudströmmen, skriver
        WSJT-X:s dial till en ny RX-cell och WSJT-X:s VFO B och split till en ny TX-cell, och
        spelar om övriga delade parametrar (mode m.m.)."""
        with self._lock:
            if self._client is None:
                return
            m = self._map()
            self._sync_audio(m)
            old = self._last_map
            if old is not None and tuple(m) != tuple(old):
                if (m.ear_key, m.ear_rx) != (old.ear_key, old.ear_rx) and self._wsjt["a"]:
                    self._send_radio(m.ear_key, "VFO:%d,0,%s;" % (m.ear_rx, self._wsjt["a"]))
                if (m.tx_key, m.tx_rx) != (old.tx_key, old.tx_rx):
                    self._apply_tx_setup(m)
                for name, args in self._both.values():
                    self._to_radios(router.client_command(router.format_command(name, args), m))
            self._last_map = m

    def _apply_tx_setup(self, m):
        """Skriver WSJT-X:s sändfrekvens (VFO B) och split till TX-cellen. WSJT-X skickar dem inte
        om före varje sändning, så en TX-cell som just tagit över måste få dem här."""
        b, split = self._wsjt["b"], self._wsjt["split"]
        if b:
            self._send_radio(m.tx_key, "VFO:%d,1,%s;" % (m.tx_rx, b))
        else:
            self._log("WSJT bridge: TX cell has no VFO B yet (WSJT-X has not sent a TX frequency)")
        if split in ("true", "false"):
            self._send_radio(m.tx_key, "SPLIT_ENABLE:%d,%s;" % (m.tx_rx, split))

    # ----- kopiera frekvens -------------------------------------------------------
    def _split_delta(self, src_key, src_rx):
        """VFO B minus VFO A i Hz: WSJT-X:s senaste, annars källcellens egen, annars 0."""
        pairs = []
        if self._wsjt["a"] and self._wsjt["b"]:
            pairs.append((self._wsjt["a"], self._wsjt["b"]))
        sa = self._state.get((src_key, "VFO", str(src_rx), "0"))
        sb = self._state.get((src_key, "VFO", str(src_rx), "1"))
        if sa and sb:
            pairs.append((sa, sb))
        for a, b in pairs:
            try:
                delta = int(b) - int(a)
            except ValueError:
                continue
            if abs(delta) <= SPLIT_DELTA_LIMIT_HZ:
                return delta
        return 0

    def copy_frequency(self, src_key, src_rx, dst_key, dst_rx):
        """Kopierar källcellens VFO A och mode till målcellen. Är målcellen den aktuella TX-cellen
        sätts också VFO B (= ny VFO A + split-skillnaden) och split på, så sändningen hamnar
        på rätt frekvens. Returnerar (ok, text)."""
        with self._lock:
            if self._ptt_map is not None:
                return False, "not during transmission"
            if (src_key, src_rx) == (dst_key, dst_rx):
                return False, "source and target are the same cell"
            freq = self._state.get((src_key, "VFO", str(src_rx), "0"))
            if freq is None:
                return False, "source frequency not known yet"
            mode = self._state.get((src_key, "MODULATION", str(src_rx)))
            m = self._map()
            if mode:
                self._send_radio(dst_key, "MODULATION:%d,%s;" % (dst_rx, mode))
            self._send_radio(dst_key, "VFO:%d,0,%s;" % (dst_rx, freq))
            text = "%s Hz" % freq
            if (dst_key, dst_rx) == (m.tx_key, m.tx_rx):
                delta = self._split_delta(src_key, src_rx)
                self._send_radio(dst_key, "VFO:%d,1,%d;" % (dst_rx, int(freq) + delta))
                self._send_radio(dst_key, "SPLIT_ENABLE:%d,true;" % dst_rx)
                text += ", TX %+d Hz, split on" % delta
            return True, text

    # ----- ljud ---------------------------------------------------------------
    def _sync_audio(self, m):
        """Ser till att exakt öra-cellen och TX-cellen har ljudström när klienten vill ha ljud."""
        desired = {(m.ear_key, m.ear_rx), (m.tx_key, m.tx_rx)} if self._audio_on else set()
        for key, rx in self._audio_cells - desired:
            self._send_radio(key, "AUDIO_STOP:%d;" % rx)
        for key, rx in desired - self._audio_cells:
            if self._audio_rate:
                self._send_radio(key, "AUDIO_SAMPLERATE:%s;" % self._audio_rate)
            self._send_radio(key, "AUDIO_START:%d;" % rx)
        self._audio_cells = desired

    # ----- klientsidan ----------------------------------------------------------
    def _serve_client(self, conn):
        with self._lock:
            old, self._client = self._client, None
        if old is not None:
            self._log("WSJT bridge: new client replaces the previous one")
            old.close()
        m = self._mapping_fn()
        hello = self.links[m.tx_key].hello if m.tx_key in self.links else []
        if not hello:
            self._log("WSJT bridge: TX radio '%s' has no hello yet (not connected), refusing client" % m.tx_key)
            return
        with self._lock:
            self._client = conn
            self._last_map = m
            self._wsjt = {"a": self._state.get((m.ear_key, "VFO", str(m.ear_rx), "0")),
                          "b": self._state.get((m.tx_key, "VFO", str(m.tx_rx), "1")),
                          "split": self._state.get((m.tx_key, "SPLIT_ENABLE", str(m.tx_rx)))}
        self._log("WSJT bridge: client connected, sending %d hello messages from %s" % (len(hello), m.tx_key))
        cause = ""
        seen = 0
        tx_view = router.Mapping(m.tx_key, m.tx_rx, m.tx_key, m.tx_rx)   # hello = TX-cellens egen bild
        try:
            for text in hello:
                out = router.radio_message(m.tx_key, text, tx_view)
                if out:
                    conn.send_text(self._with_rx_dial(out, m))
            while True:
                msg = conn.recv()
                if isinstance(msg, bytes):
                    self._on_client_binary(msg)
                else:
                    if seen < CLIENT_LOG_LIMIT:
                        seen += 1
                        self._log("WSJT bridge: client sent %s" % msg.strip())
                    self._on_client_text(msg)
        except (ConnectionError, OSError) as e:
            cause = " (%s)" % e
        finally:
            self._log("WSJT bridge: client connection ended%s" % cause)
            self._client_gone(conn)

    def _with_rx_dial(self, out, m):
        """Hello kommer från TX-cellen, men dialen (VFO A) ska vara RX-cellens: det är den man hör."""
        dial = self._state.get((m.ear_key, "VFO", str(m.ear_rx), "0"))
        if dial is None or "vfo" not in out.lower():
            return out
        parts = []
        for name, args in router.split_commands(out):
            if name.upper() == "VFO" and len(args) >= 3 and args[0] == "0" and args[1] == "0":
                args = [args[0], args[1], dial]
            parts.append(router.format_command(name, args))
        return "".join(parts)

    def _client_gone(self, conn):
        with self._lock:
            if self._client is not conn:
                return
            self._client = None
            if self._ptt_map is not None:
                self._send_radio(self._ptt_map.tx_key, "TRX:%d,false;" % self._ptt_map.tx_rx)
                self._finish_tx_timing()
                self._ptt_map = None
            self._audio_on = False
            self._sync_audio(self._mapping_fn())
            self._both.clear()
            self._wsjt = self._empty_wsjt()
        self._log("WSJT bridge: client disconnected")

    def _on_client_text(self, text):
        with self._lock:
            for name, args in router.split_commands(text):
                self._track_state(name, args)
                m = self._map()
                if name.upper() not in LOCAL_AUDIO:       # ljudstart/stopp sköts av _sync_audio
                    self._to_radios(router.client_command(router.format_command(name, args), m))
                if name.upper() == "TRX" and len(args) >= 2 and args[1].lower() == "false":
                    self._finish_tx_timing()
                    self._ptt_map = None
            if self._audio_on or self._audio_cells:
                self._sync_audio(self._map())

    def _track_state(self, name, args):
        """Det bron behöver minnas om klienten: PTT, ljud, samplingsfrekvens, BOTH-värden,
        dial, sändfrekvens (VFO B) och split."""
        n = name.upper()
        if n == "TRX" and len(args) >= 2 and args[1].lower() == "true" and self._ptt_map is None:
            self._ptt_map = self._mapping_fn()
            self._tx_timing = TxTiming()
        elif n == "AUDIO_START":
            self._audio_on = True
        elif n == "AUDIO_STOP":
            self._audio_on = False
        elif n == "AUDIO_SAMPLERATE" and args:
            self._audio_rate = args[0]
        if router.group_of(name, args) == "BOTH" and len(args) >= 2:
            self._both[(n, args[1] if n in router.CHANNEL_ARG else "")] = (name, args)
        if n == "VFO" and len(args) >= 3 and args[0] == "0":
            if args[1] == "0":
                self._wsjt["a"] = args[2]
            elif args[1] == "1":
                self._wsjt["b"] = args[2]
        elif n == "SPLIT_ENABLE" and args:
            self._wsjt["split"] = args[-1].lower()

    def _on_client_binary(self, data):
        if frames.stream_type(data) != frames.TX_AUDIO_STREAM:
            return
        with self._lock:
            m = self._map()
        timing = self._tx_timing
        if timing is not None:
            timing.audio(frames.peak_level(data))
        link = self.links.get(m.tx_key)
        if link is not None:
            link.send_binary(frames.with_receiver(data, m.tx_rx))

    # ----- radiosidan -----------------------------------------------------------
    def _note_radio_state(self, radio_key, msg, m):
        """Minns radions senaste VFO/MODULATION/SPLIT_ENABLE (för hello och kopiera frekvens).
        Med mapping `m` och ansluten klient följer WSJT-X:s dial/VFO B/split radions egna värden."""
        if isinstance(msg, bytes):
            return
        for name, args in router.split_commands(msg):
            n = name.upper()
            if n == "VFO" and len(args) >= 3:
                self._state[(radio_key, "VFO", args[0], args[1])] = args[2]
                if m is not None and self._client is not None:
                    if args[1] == "0" and (radio_key, args[0]) == (m.ear_key, str(m.ear_rx)):
                        self._wsjt["a"] = args[2]
                    elif args[1] == "1" and (radio_key, args[0]) == (m.tx_key, str(m.tx_rx)):
                        self._wsjt["b"] = args[2]
            elif n == "MODULATION" and len(args) >= 2:
                self._state[(radio_key, "MODULATION", args[0])] = args[1]
            elif n == "SPLIT_ENABLE" and len(args) >= 2:
                self._state[(radio_key, "SPLIT_ENABLE", args[0])] = args[1].lower()
                if m is not None and self._client is not None \
                        and (radio_key, args[0]) == (m.tx_key, str(m.tx_rx)):
                    self._wsjt["split"] = args[1].lower()

    def _on_radio(self, radio_key, msg):
        m = self._map()
        with self._lock:
            self._note_radio_state(radio_key, msg, m)
        conn = self._client
        if conn is None:
            return
        try:
            if isinstance(msg, bytes):
                t = frames.stream_type(msg)
                rx = frames.receiver(msg) if t is not None else None
                if t == frames.RX_AUDIO_STREAM and (radio_key, rx) == (m.ear_key, m.ear_rx):
                    conn.send_binary(frames.with_receiver(msg, 0))
                elif t == frames.TX_CHRONO and (radio_key, rx) == (m.tx_key, m.tx_rx):
                    if self._tx_timing is not None:
                        self._tx_timing.chrono()
                    conn.send_binary(frames.with_receiver(msg, 0))
            else:
                self._note_radio_ptt(radio_key, msg, m)
                out = router.radio_message(radio_key, msg, m)
                if out:
                    conn.send_text(out)
        except (ConnectionError, OSError):
            pass

    def _note_radio_ptt(self, radio_key, msg, m):
        """Radions eget `trx:<rx>,true`-eko till en pågående sändning (för tidsloggen)."""
        timing = self._tx_timing
        if timing is None or radio_key != m.tx_key:
            return
        for name, args in router.split_commands(msg):
            if name.upper() == "TRX" and len(args) >= 2 and args[0] == str(m.tx_rx) and args[1].lower() == "true":
                timing.radio_echo()

    def _finish_tx_timing(self):
        timing, self._tx_timing = self._tx_timing, None
        if timing is not None:
            self._log("WSJT bridge: " + timing.summary())

    def _to_radios(self, items):
        for key, cmd in items:
            self._send_radio(key, cmd)

    def _send_radio(self, key, cmd):
        link = self.links.get(key)
        if link is None:
            return
        if self._echo_guard is not None:
            for name, args in router.split_commands(cmd):
                self._echo_guard(key, name.upper(), args)
        link.send(cmd)
