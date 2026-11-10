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
import time

import bridge_router as router
import tci_frames as frames
from tx_timing import TxTiming
from ws_server import WsServer

DEFAULT_PORT = 60001
SPLIT_DELTA_LIMIT_HZ = 20000                  # större skillnad mellan VFO B och VFO A är ingen split-offset
AUDIO_WATCHDOG_S = 3.0                        # så länge får RX-ljudet utebli innan AUDIO_START skickas om
AUDIO_WATCHDOG_TICK_S = 1.0
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
        self._crx = "0"                 # klientens mottagarnummer: "0" = Rig 'TCI Client RX1', "1" = 'RX2'
        self._crx_known = False         # klienten har visat vilket nummer den använder
        self._ptt_map = None            # Mapping som gällde när PTT slogs på (None = ingen sändning)
        self._audio_on = False          # klienten har begärt ljudström
        self._audio_cells = set()       # (radio_key, rx) som just nu har audio_start
        self._audio_rate = None         # senaste AUDIO_SAMPLERATE från klienten
        self._both = {}                 # senaste BOTH-kommandon (för omspelning vid cellbyte)
        self._last_map = None
        self._switch_t = None           # monotonic-tid för senaste cellbyte (för mätning av byteslatens)
        self._switch_audio_ms = None    # ms från cellbyte till första RX-ljudblocket från nya RX-cellen
        self._audio_ref = time.monotonic()   # senaste RX-ljudblock från RX-cellen, eller senaste AUDIO_START
        self.watchdog_s = AUDIO_WATCHDOG_S
        self.watchdog_tick_s = AUDIO_WATCHDOG_TICK_S
        self._watchdog_stop = threading.Event()
        self._watchdog_restarts = 0     # antal omstarter i rad utan att ljud kommit (för att inte fylla loggen)

    # ----- livscykel -----------------------------------------------------------
    def start(self):
        if self._server is not None:
            return
        for key, link in self.links.items():
            for text in list(link.hello):
                self._note_radio_state(key, text, None)
            link.raw_listeners.append(self._on_radio)
            link.connect_listeners.append(self._on_radio_connected)
        self._server = WsServer(self.host, self.port, self._serve_client, log=self._log)
        self._server.start()
        self._last_map = self._mapping_fn()
        self._watchdog_stop.clear()
        threading.Thread(target=self._watchdog_run, daemon=True).start()
        self._log("WSJT bridge: listening on localhost:%d (IPv4 and IPv6 where available)" % self.port)

    def stop(self):
        if self._server is None:
            return
        self._watchdog_stop.set()
        self._server.stop()
        self._server = None
        for link in self.links.values():
            if self._on_radio in link.raw_listeners:
                link.raw_listeners.remove(self._on_radio)
            if self._on_radio_connected in link.connect_listeners:
                link.connect_listeners.remove(self._on_radio_connected)
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
                if (m.ear_key, m.ear_rx) != (old.ear_key, old.ear_rx):
                    self._switch_t, self._switch_audio_ms = time.monotonic(), None
                if (m.ear_key, m.ear_rx) != (old.ear_key, old.ear_rx) and self._wsjt["a"]:
                    self._send_radio(m.ear_key, "VFO:%d,0,%s;" % (m.ear_rx, self._wsjt["a"]))
                if (m.tx_key, m.tx_rx) != (old.tx_key, old.tx_rx):
                    self._apply_tx_setup(m)
                for name, args in self._both.values():
                    self._to_radios(router.client_command(router.format_command(name, args), m, self._crx))
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

    # ----- styrning utifrån (bridge_control.py) -------------------------------------
    def cell_dial(self, key, rx):
        """En cells senast kända VFO A i Hz (int), eller None."""
        with self._lock:
            v = self._state.get((key, "VFO", str(rx), "0"))
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def rx_dial(self):
        """RX-cellens senast kända VFO A i Hz (int), eller None."""
        m = self._map()
        return self.cell_dial(m.ear_key, m.ear_rx)

    def wsjt_dial(self):
        """WSJT-X:s dial i Hz (det RX-cellen får vid platsbyte), annars RX-cellens egen."""
        with self._lock:
            v = self._wsjt["a"]
        try:
            return int(v)
        except (TypeError, ValueError):
            return self.rx_dial()

    def set_companion_dial(self, key, rx, hz):
        """Sätter VFO A på en cell utanför bronas egna celler (Ramnäs RX1, som styr antennvalet).
        Bara på begäran från styrgränssnittet; routern släpper aldrig dess eko till WSJT-X och
        eko-spärren hindrar Sync från att reläa det. Returnerar (ok, text)."""
        with self._lock:
            if self._ptt_map is not None:
                return False, "not during transmission"
            self._send_radio(key, "VFO:%d,0,%d;" % (int(rx), int(hz)))
            return True, "%d Hz" % int(hz)

    def set_dial(self, hz):
        """Sätter RX-cellens VFO A. WSJT-X ser den nya dialen via radions eko, och en RX-cell som
        tar över vid ett platsbyte får WSJT-X:s dial. Returnerar (ok, text)."""
        with self._lock:
            if self._ptt_map is not None:
                return False, "not during transmission"
            m = self._map()
            self._send_radio(m.ear_key, "VFO:%d,0,%d;" % (m.ear_rx, int(hz)))
            return True, "%d Hz" % int(hz)

    def switch_stats(self):
        """ms från senaste RX-cellbyte till första RX-ljudblocket därifrån (None = ännu inget)."""
        return self._switch_audio_ms

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
            self._audio_ref = time.monotonic()
        self._audio_cells = desired

    def _on_radio_connected(self, radio_key, number):
        """En radiolänk har (åter)anslutit. Den nya anslutningen har ingen ljudström, så radions
        celler glöms ur `_audio_cells` och `_sync_audio` startar om dem (annars tror bron att
        ljudet redan går, medan kommandon fortsätter fungera men ljudet uteblir)."""
        with self._lock:
            lost = {c for c in self._audio_cells if c[0] == radio_key}
            self._log("WSJT bridge: radio '%s' connected (connection #%d), audio %s" % (
                radio_key, number, "restarted" if lost and self._client is not None else "not needed"))
            if not lost:
                return
            self._audio_cells -= lost
            if self._client is not None and self._audio_on:
                self._sync_audio(self._map())

    def _watchdog_run(self):
        while not self._watchdog_stop.wait(self.watchdog_tick_s):
            try:
                self._watchdog_check()
            except Exception as e:
                self._log("WSJT bridge: audio watchdog error: %s" % e)

    def _watchdog_check(self):
        """Vill klienten ha ljud men har inget RX-ljudblock kommit från RX-cellen på `watchdog_s`
        sekunder, skickas AUDIO_SAMPLERATE och AUDIO_START om. Täcker alla orsaker till en tyst
        ljudström (radion har glömt strömmen, kommandot gick förlorat), inte bara återanslutning.
        Loggar de första omstarterna i en följd och sedan var tionde."""
        with self._lock:
            if self._client is None or not self._audio_on:
                return
            silent = time.monotonic() - self._audio_ref
            if silent < self.watchdog_s:
                return
            m = self._map()
            self._watchdog_restarts += 1
            n = self._watchdog_restarts
            if n <= 3 or n % 10 == 0:
                self._log("WSJT bridge: no RX audio from '%s' for %.1f s, restarting audio (restart #%d in a row)"
                          % (m.ear_key, silent, n))
            self._audio_cells.discard((m.ear_key, m.ear_rx))
            self._sync_audio(m)

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
            self._crx, self._crx_known = "0", False
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
                if text.strip().lower().startswith("ready"):
                    conn.send_text("rx_enable:1,true;")      # bron har en mottagare 1 för Rig 'TCI Client RX2'
                for out in router.hello_message(m.tx_key, text, tx_view):
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
            if name.upper() == "VFO" and len(args) >= 3 and args[0] in ("0", "1") and args[1] == "0":
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
                self._learn_client_receiver(name, args)
                self._track_state(name, args)
                m = self._map()
                if name.upper() == "RX_ENABLE" and len(args) >= 2:
                    self._answer_client("rx_enable:%s,%s;" % (args[0], args[1].lower()))
                if name.upper() not in LOCAL_AUDIO:       # ljudstart/stopp sköts av _sync_audio
                    self._to_radios(router.client_command(router.format_command(name, args), m, self._crx))
                if name.upper() == "TRX" and len(args) >= 2 and args[1].lower() == "false":
                    self._finish_tx_timing()
                    self._ptt_map = None
            if self._audio_on or self._audio_cells:
                self._sync_audio(self._map())

    def _learn_client_receiver(self, name, args):
        """Första mottagarnumrerade kommandot (utom RX_ENABLE) visar om klienten är Rig 'TCI Client
        RX1' (nummer 0) eller 'RX2' (nummer 1). Därefter översätts detta nummer åt båda hållen."""
        if self._crx_known or name.upper() not in router.ARG0_RX or name.upper() == "RX_ENABLE":
            return
        if args and args[0] in ("0", "1"):
            self._crx, self._crx_known = args[0], True
            self._log("WSJT bridge: client uses receiver %s (Rig 'TCI Client RX%d')" % (args[0], int(args[0]) + 1))

    def _answer_client(self, text):
        conn = self._client
        if conn is None:
            return
        try:
            conn.send_text(text)
        except (ConnectionError, OSError):
            pass

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
        if n == "VFO" and len(args) >= 3 and args[0] == self._crx:
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
                    self._audio_ref = time.monotonic()
                    self._watchdog_restarts = 0
                    if self._switch_t is not None and self._switch_audio_ms is None:
                        self._switch_audio_ms = int((time.monotonic() - self._switch_t) * 1000)
                    conn.send_binary(frames.with_receiver(msg, int(self._crx)))
                elif t == frames.TX_CHRONO and (radio_key, rx) == (m.tx_key, m.tx_rx):
                    if self._tx_timing is not None:
                        self._tx_timing.chrono()
                    conn.send_binary(frames.with_receiver(msg, int(self._crx)))
            else:
                self._note_radio_ptt(radio_key, msg, m)
                out = router.radio_message(radio_key, msg, m, self._crx)
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
