# -*- coding: utf-8 -*-
"""Tester för WSJT-bron: routing (ren logik), WebSocket-servern och hela bron mot två
falska radioservrar (byggda på ws_server.py). Körs med: python -m unittest discover -s tests"""

import os
import queue
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websocket

import bridge_router as br
import tci_frames as frames
from tci_bridge import TciBridge
from tci_link import TciLink
from tx_timing import TxTiming
from ws_server import WsServer


def block(typ, rx, n=8, rate=48000):
    head = struct.pack("<8I", rx, rate, 3, 0, 0, n, typ, 2) + b"\0" * 32
    return head + b"\x01" * (n * 4)


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FakeRadio:
    def __init__(self, name, extra_hello=()):
        self.name = name
        self.extra_hello = list(extra_hello)
        self.texts = []
        self.binaries = []
        self.conns = []
        self.server = WsServer("127.0.0.1", 0, self._serve)
        self.server.start()
        self.port = self.server._srv.getsockname()[1]

    def _serve(self, conn):
        self.conns.append(conn)
        for t in ["protocol:ExpertSDR3,2.1;", "device:%s;" % self.name] + self.extra_hello + ["ready;"]:
            conn.send_text(t)
        try:
            while True:
                m = conn.recv()
                (self.binaries if isinstance(m, bytes) else self.texts).append(m)
        except (ConnectionError, OSError):
            pass

    def emit(self, data):
        for c in self.conns:
            (c.send_binary if isinstance(data, bytes) else c.send_text)(data)

    def stop(self):
        self.server.stop()


class RouterTest(unittest.TestCase):
    M = br.Mapping("A", 0, "B", 1)

    def test_client_commands_route_by_group(self):
        m = self.M
        self.assertEqual(br.client_command("audio_samplerate:48000;", m),
                         [("A", "audio_samplerate:48000;")])
        self.assertEqual(br.client_command("trx:0,true,tci;", m), [("B", "trx:1,true,tci;")])
        self.assertEqual(br.client_command("vfo:0,0,14074000;", m), [("A", "vfo:0,0,14074000;")])   # dial = RX-cellen
        self.assertEqual(br.client_command("vfo:0,1,14077000;", m), [("B", "vfo:1,1,14077000;")])   # VFO B = TX-cellen
        self.assertEqual(br.client_command("modulation:0,digu;", m),
                         [("A", "modulation:0,digu;"), ("B", "modulation:1,digu;")])
        self.assertEqual(br.client_command("start;", m), [("B", "start;")])
        self.assertEqual(br.client_command("somethingnew:0,1;", m), [("B", "somethingnew:0,1;")])

    def test_same_cell_not_duplicated(self):
        m = br.Mapping("A", 0, "A", 0)
        self.assertEqual(br.client_command("vfo:0,0,7074000;", m), [("A", "vfo:0,0,7074000;")])

    def test_radio_messages_filtered_and_renumbered(self):
        m = self.M
        self.assertEqual(br.radio_message("A", "rx_sensors:0,-71.5;", m), "rx_sensors:0,-71.5;")
        self.assertIsNone(br.radio_message("B", "rx_sensors:1,-71.5;", m))   # fel radio för RX
        self.assertEqual(br.radio_message("B", "trx:1,false;", m), "trx:0,false;")
        self.assertIsNone(br.radio_message("A", "trx:0,false;", m))
        self.assertIsNone(br.radio_message("B", "trx:0,false;", m))          # fel mottagare
        self.assertEqual(br.radio_message("A", "vfo:0,0,14074000;", m), "vfo:0,0,14074000;")   # dial från RX-cellen
        self.assertIsNone(br.radio_message("B", "vfo:1,0,14074000;", m))                       # inte TX-cellens VFO A
        self.assertEqual(br.radio_message("B", "vfo:1,1,14077000;", m), "vfo:0,1,14077000;")   # VFO B från TX-cellen
        self.assertIsNone(br.radio_message("A", "vfo:0,1,14077000;", m))


class WsServerTest(unittest.TestCase):
    def test_roundtrip_and_large_frame(self):
        got = []

        def serve(conn):
            try:
                while True:
                    m = conn.recv()
                    got.append(m)
                    (conn.send_binary if isinstance(m, bytes) else conn.send_text)(m)
            except (ConnectionError, OSError):
                pass

        srv = WsServer("127.0.0.1", 0, serve)
        srv.start()
        try:
            ws = websocket.create_connection("ws://127.0.0.1:%d" % srv._srv.getsockname()[1], timeout=3)
            ws.send("hej;")
            self.assertEqual(ws.recv(), "hej;")
            big = bytes(range(256)) * 400          # 102 400 byte -> 64-bitars längdfält
            ws.send_binary(big)
            self.assertEqual(ws.recv(), big)
            ws.close()
            ws6 = websocket.create_connection("ws://[::1]:%d" % srv._srv.getsockname()[1], timeout=3)
            ws6.send("v6;")
            self.assertEqual(ws6.recv(), "v6;")      # localhost kan lösas upp till ::1
            ws6.close()
        finally:
            srv.stop()


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.a = FakeRadio("SunSDR2PRO")
        self.b = FakeRadio("SunSDR2DX")
        self.events = queue.Queue()
        self.links = {}
        for key, r in (("A", self.a), ("B", self.b)):
            link = TciLink(key, "127.0.0.1", r.port, self.events)
            self.links[key] = link
            link.start()
        self.assertTrue(wait_for(lambda: all(l.hello for l in self.links.values())))
        self.mapping = br.Mapping("A", 0, "B", 1)
        self.bridge = TciBridge(self.links, lambda: self.mapping, port=0)
        self.bridge.start()
        self.port = self.bridge._server._srv.getsockname()[1]
        self.client = websocket.create_connection("ws://127.0.0.1:%d" % self.port, timeout=3)
        self.hello = []
        while True:
            m = self.client.recv()
            self.hello.append(m)
            if "ready;" in m:
                break

    def tearDown(self):
        try:
            self.client.close()
        except Exception:
            pass
        self.bridge.stop()
        for l in self.links.values():
            l.stop()
        self.a.stop()
        self.b.stop()

    def test_hello_comes_from_tx_radio(self):
        self.assertIn("device:SunSDR2DX;", self.hello)

    def test_audio_and_chrono_routing(self):
        self.client.send("audio_samplerate:48000;")
        self.client.send("audio_start:0;")
        self.assertTrue(wait_for(lambda: "AUDIO_START:0;" in self.a.texts))
        self.assertTrue(wait_for(lambda: "AUDIO_START:1;" in self.b.texts))
        self.assertEqual(self.a.texts.count("AUDIO_START:0;"), 1)
        self.b.emit(block(frames.RX_AUDIO_STREAM, 1))         # TX-radions RX-ljud ska inte till klienten
        self.a.emit(block(frames.RX_AUDIO_STREAM, 0))
        got = self.client.recv()
        self.assertEqual((frames.stream_type(got), frames.receiver(got)), (frames.RX_AUDIO_STREAM, 0))
        self.a.emit(block(frames.TX_CHRONO, 0))               # fel radio för TX_CHRONO
        self.b.emit(block(frames.TX_CHRONO, 1))
        got = self.client.recv()
        self.assertEqual((frames.stream_type(got), frames.receiver(got)), (frames.TX_CHRONO, 0))

    def test_tx_audio_goes_to_tx_radio_with_its_receiver_number(self):
        self.client.send_binary(block(frames.TX_AUDIO_STREAM, 0))
        self.assertTrue(wait_for(lambda: self.b.binaries))
        self.assertEqual(frames.receiver(self.b.binaries[0]), 1)
        self.assertEqual(self.a.binaries, [])

    def test_dial_to_rx_cell_vfo_b_to_tx_cell_ptt_to_tx_only(self):
        self.client.send("vfo:0,0,14074000;")
        self.client.send("vfo:0,1,14073000;")
        self.client.send("trx:0,true,tci;")
        self.assertTrue(wait_for(lambda: "vfo:0,0,14074000;" in self.a.texts
                                 and "vfo:1,1,14073000;" in self.b.texts))
        self.assertTrue(wait_for(lambda: "trx:1,true,tci;" in self.b.texts))
        self.assertFalse([t for t in self.a.texts if t.lower().startswith("trx")])
        self.assertFalse([t for t in self.a.texts if t.lower().startswith("vfo:0,1")])    # RX-cellen får ingen VFO B
        self.assertFalse([t for t in self.b.texts if t.lower().startswith("vfo:1,0")])    # TX-cellen får ingen dial

    def test_tx_cell_latched_during_ptt(self):
        self.client.send("trx:0,true,tci;")
        self.assertTrue(wait_for(lambda: "trx:1,true,tci;" in self.b.texts))
        self.mapping = br.Mapping("A", 0, "A", 0)             # TX-markören flyttas mitt i sändningen
        self.client.send("trx:0,false,tci;")
        self.assertTrue(wait_for(lambda: "trx:1,false,tci;" in self.b.texts))
        self.assertFalse([t for t in self.a.texts if t.lower().startswith("trx")])

    def test_tx_timing_summary_is_logged_per_transmission(self):
        lines = []
        self.bridge._log = lines.append
        self.client.send("trx:0,true,tci;")
        self.assertTrue(wait_for(lambda: "trx:1,true,tci;" in self.b.texts))
        self.b.emit("trx:1,true;")                                  # radions eko
        self.b.emit(block(frames.TX_CHRONO, 1))
        time.sleep(0.2)
        silent = block(frames.TX_AUDIO_STREAM, 0)[:frames.HEADER_SIZE] + struct.pack("<8f", *([0.0] * 8))
        loud = block(frames.TX_AUDIO_STREAM, 0)[:frames.HEADER_SIZE] + struct.pack("<8f", *([0.25] * 7 + [0.5]))
        self.client.send_binary(silent)
        time.sleep(0.15)
        self.client.send_binary(loud)
        self.assertTrue(wait_for(lambda: len(self.b.binaries) >= 2))
        self.client.send("trx:0,false,tci;")
        self.assertTrue(wait_for(lambda: any("TX timing" in l for l in lines)))
        line = [l for l in lines if "TX timing" in l][0]
        self.assertIn("1 TX_CHRONO", line)
        self.assertIn("peak 0.500", line)
        self.assertIn("first TX audio +", line)
        self.assertIn("2 TX audio blocks", line)
        tone_ms = int(line.split("first non-silent audio +")[1].split(" ms")[0])
        audio_ms = int(line.split("first TX audio +")[1].split(" ms")[0])
        self.assertGreaterEqual(tone_ms, audio_ms)               # tonen kan inte börja före första blocket

    def test_client_drop_during_ptt_unkeys_tx_radio(self):
        self.client.send("trx:0,true,tci;")
        self.assertTrue(wait_for(lambda: "trx:1,true,tci;" in self.b.texts))
        self.client.close()
        self.assertTrue(wait_for(lambda: "TRX:1,false;" in self.b.texts))

    def test_ear_change_moves_audio_stream(self):
        self.client.send("audio_start:0;")
        self.assertTrue(wait_for(lambda: "AUDIO_START:0;" in self.a.texts))
        self.mapping = br.Mapping("B", 1, "B", 1)             # örat flyttas till TX-radion
        self.bridge.on_mapping_changed()
        self.assertTrue(wait_for(lambda: "AUDIO_STOP:0;" in self.a.texts))
        self.b.emit(block(frames.RX_AUDIO_STREAM, 1))
        got = self.client.recv()
        self.assertEqual(frames.stream_type(got), frames.RX_AUDIO_STREAM)


class TxTimingTest(unittest.TestCase):
    """Ren tidslogik med falsk klocka (inga sockets, inga trådar)."""

    def test_first_non_silent_block_is_timed_separately_from_first_block(self):
        now = [100.0]
        t = TxTiming(clock=lambda: now[0])
        now[0] = 100.002
        t.radio_echo()
        now[0] = 100.060
        t.chrono()
        now[0] = 100.115
        t.audio(0.0)                      # tyst block
        now[0] = 100.400
        t.audio(0.8)                      # tonen startar
        now[0] = 100.650
        t.audio(0.8)
        now[0] = 102.000
        line = t.summary()
        self.assertIn("radio echo +2 ms", line)
        self.assertIn("first TX_CHRONO +60 ms", line)
        self.assertIn("first TX audio +115 ms", line)
        self.assertIn("first non-silent audio +400 ms", line)
        self.assertIn("PTT off 2000 ms", line)
        self.assertIn("3 TX audio blocks", line)
        self.assertIn("peak 0.800", line)

    def test_unknown_peak_and_missing_events_are_shown_as_dashes(self):
        t = TxTiming(clock=lambda: 5.0)
        t.audio(None)
        line = t.summary()
        self.assertIn("radio echo -", line)
        self.assertIn("first non-silent audio -", line)
        self.assertIn("peak ?", line)


class DialAndRx2Test(unittest.TestCase):
    """Ramnäs = A (RX2 = mottagare 1), Blecket = B (RX1 = mottagare 0). Frekvensen styrs av WSJT-X."""

    def setUp(self):
        self.a = FakeRadio("SunSDR2PRO", extra_hello=["vfo:0,0,3573000;", "vfo:1,0,14074000;",
                                                      "modulation:1,digu;"])
        self.b = FakeRadio("SunSDR2DX", extra_hello=["vfo:0,0,7074000;"])
        self.events = queue.Queue()
        self.links = {}
        for key, r in (("A", self.a), ("B", self.b)):
            link = TciLink(key, "127.0.0.1", r.port, self.events)
            self.links[key] = link
            link.start()
        self.assertTrue(wait_for(lambda: all(l.hello for l in self.links.values())))
        self.mapping = br.Mapping("A", 1, "A", 1)                 # läge 1: Ramnäs RX2 / Ramnäs RX2
        self.guarded = []
        self.bridge = TciBridge(self.links, lambda: self.mapping, port=0,
                                echo_guard=lambda k, n, a: self.guarded.append((k, n, tuple(a))))
        self.bridge.start()
        self.port = self.bridge._server._srv.getsockname()[1]
        self.client = websocket.create_connection("ws://127.0.0.1:%d" % self.port, timeout=3)
        self.hello = []
        while True:
            m = self.client.recv()
            self.hello.append(m)
            if "ready;" in m:
                break

    def tearDown(self):
        try:
            self.client.close()
        except Exception:
            pass
        self.bridge.stop()
        for l in self.links.values():
            l.stop()
        self.a.stop()
        self.b.stop()

    def test_hello_shows_the_tx_cells_own_frequency_as_receiver_0(self):
        self.assertIn("vfo:0,0,14074000;", self.hello)           # Ramnäs RX2, inte RX1:s 3573000
        self.assertNotIn("vfo:0,0,3573000;", self.hello)

    def test_ramnas_rx1_never_addressed(self):
        for cmd in ("vfo:0,0,14074000;", "modulation:0,digu;", "audio_samplerate:48000;", "audio_start:0;",
                    "trx:0,true,tci;", "drive:0,20;", "rx_mute:0,false;", "trx:0,false,tci;"):
            self.client.send(cmd)
        self.assertTrue(wait_for(lambda: any(t.lower().startswith("trx:1,false") for t in self.a.texts)))
        for t in self.a.texts:
            name, _, rest = t.partition(":")
            if name.upper() in br.ARG0_RX:
                self.assertEqual(rest.rstrip(";").split(",")[0], "1", t)       # aldrig mottagare 0 (RX1)

    def _reconnect(self):
        """Ny klient (ersätter den gamla); returnerar hello-meddelandena."""
        try:
            self.client.close()
        except Exception:
            pass
        self.client = websocket.create_connection("ws://127.0.0.1:%d" % self.port, timeout=3)
        hello = []
        while True:
            m = self.client.recv()
            hello.append(m)
            if "ready;" in m:
                return hello

    def _texts(self, radio, prefix):
        return [t for t in radio.texts if t.lower().startswith(prefix)]

    def test_mode_switch_writes_dial_to_new_rx_cell_and_vfo_b_split_to_new_tx_cell(self):
        for cmd in ("vfo:0,0,14074000;", "vfo:0,1,14073000;", "split_enable:0,true;"):
            self.client.send(cmd)
        self.assertTrue(wait_for(lambda: "split_enable:1,true;" in self.a.texts))
        self.mapping = br.Mapping("A", 1, "B", 0)                  # sänder nu Blecket, hör Ramnäs RX2
        self.bridge.on_mapping_changed()
        self.assertTrue(wait_for(lambda: "VFO:0,1,14073000;" in self.b.texts
                                 and "SPLIT_ENABLE:0,true;" in self.b.texts))
        self.assertEqual(self._texts(self.b, "vfo:0,0"), [])       # nya TX-cellen får ingen dial
        self.mapping = br.Mapping("B", 0, "B", 0)                  # hör nu Blecket också
        self.bridge.on_mapping_changed()
        self.assertTrue(wait_for(lambda: "VFO:0,0,14074000;" in self.b.texts))

    def test_dial_follows_rx_cell_and_vfo_b_follows_tx_cell_only(self):
        self.mapping = br.Mapping("A", 1, "B", 0)                  # RX Ramnäs RX2, TX Blecket
        self.bridge.on_mapping_changed()
        self.a.emit("vfo:1,0,5000000;")                            # ratt på RX-cellen: dialen i WSJT-X följer
        self.assertEqual(self.client.recv(), "vfo:0,0,5000000;")
        self.b.emit("vfo:0,0,7074000;")                            # TX-cellens VFO A hör inte till klientens bild
        time.sleep(0.2)
        self.b.emit("vfo:0,1,7073000;")
        self.assertEqual(self.client.recv(), "vfo:0,1,7073000;")   # TX-cellens VFO B gör det
        self.assertEqual(self._texts(self.b, "vfo"), [])           # och bron skickar inget tillbaka

    def test_hello_dial_is_the_rx_cells_vfo_a(self):
        self.mapping = br.Mapping("A", 1, "B", 0)
        hello = self._reconnect()
        self.assertIn("device:SunSDR2DX;", hello)                  # hello i övrigt från TX-cellen
        self.assertIn("vfo:0,0,14074000;", hello)                  # dialen: Ramnäs RX2, inte Blecket
        self.assertNotIn("vfo:0,0,7074000;", hello)

    def test_copy_to_tx_cell_sets_vfo_a_vfo_b_split_and_mode(self):
        self.mapping = br.Mapping("A", 1, "B", 0)
        self.bridge.on_mapping_changed()
        ok, _ = self.bridge.copy_frequency("A", 1, "B", 0)
        self.assertTrue(ok)
        self.assertTrue(wait_for(lambda: "SPLIT_ENABLE:0,true;" in self.b.texts))
        for cmd in ("MODULATION:0,digu;", "VFO:0,0,14074000;", "VFO:0,1,14074000;"):
            self.assertTrue(wait_for(lambda c=cmd: c in self.b.texts), cmd)

    def test_copy_keeps_wsjt_split_difference(self):
        self.mapping = br.Mapping("A", 1, "B", 0)
        self.bridge.on_mapping_changed()
        self.client.send("vfo:0,0,14074000;")
        self.client.send("vfo:0,1,14073000;")
        self.assertTrue(wait_for(lambda: "vfo:0,1,14073000;" in self.b.texts))
        self.assertTrue(self.bridge.copy_frequency("A", 1, "B", 0)[0])
        self.assertTrue(wait_for(lambda: "VFO:0,1,14073000;" in self.b.texts))   # 14074000 - 1000 Hz

    def test_copy_to_rx_only_cell_leaves_vfo_b_and_split_alone(self):
        self.mapping = br.Mapping("A", 1, "B", 0)
        self.bridge.on_mapping_changed()
        self.assertTrue(self.bridge.copy_frequency("B", 0, "A", 1)[0])
        self.assertTrue(wait_for(lambda: "VFO:1,0,7074000;" in self.a.texts))
        self.assertEqual(self._texts(self.a, "vfo:1,1"), [])
        self.assertEqual(self._texts(self.a, "split_enable"), [])

    def test_copy_refused_during_ptt_and_for_unknown_source(self):
        self.mapping = br.Mapping("A", 1, "B", 0)
        self.assertFalse(self.bridge.copy_frequency("B", 5, "A", 1)[0])      # okänd källcell
        self.client.send("trx:0,true,tci;")
        self.assertTrue(wait_for(lambda: "trx:0,true,tci;" in self.b.texts))
        self.assertFalse(self.bridge.copy_frequency("A", 1, "B", 0)[0])

    def test_echo_guard_called_for_every_command_the_bridge_sends(self):
        self.client.send("vfo:0,0,14074000;")
        self.assertTrue(wait_for(lambda: ("A", "VFO", ("1", "0", "14074000")) in self.guarded))


if __name__ == "__main__":
    unittest.main()
