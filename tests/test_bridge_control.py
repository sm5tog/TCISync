# -*- coding: utf-8 -*-
"""Tester för styrgränssnittet (bridge_control.py) och bronas set_dial/rx_dial/switch_stats.
Körs med: python -m unittest discover -s tests"""

import json
import os
import queue
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import websocket

import bridge_router as br
import tci_frames as frames
from bridge_control import ControlServer
from tci_bridge import TciBridge
from tci_link import TciLink
from test_tci_bridge import FakeRadio, block, wait_for


class ControlServerTest(unittest.TestCase):
    def setUp(self):
        self.seen = []

        def handler(req):
            self.seen.append(req)
            if req["cmd"] == "boom":
                raise RuntimeError("boom")
            return {"ok": True, "echo": req}

        self.srv = ControlServer(handler, port=0)
        self.srv.start()
        self.port = self.srv._srv.getsockname()[1]
        self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.f = self.sock.makefile("rb")

    def tearDown(self):
        self.f.close()
        self.sock.close()
        self.srv.stop()

    def ask(self, raw):
        self.sock.sendall(raw)
        return json.loads(self.f.readline().decode("utf-8"))

    def test_request_reply(self):
        r = self.ask(b'{"cmd": "status"}\n')
        self.assertTrue(r["ok"])
        self.assertEqual(self.seen, [{"cmd": "status"}])

    def test_two_requests_in_one_packet_get_two_replies(self):
        self.sock.sendall(b'{"cmd": "a"}\n{"cmd": "b"}\n')
        self.assertEqual(json.loads(self.f.readline())["echo"]["cmd"], "a")
        self.assertEqual(json.loads(self.f.readline())["echo"]["cmd"], "b")

    def test_bad_json_and_missing_cmd_keep_the_connection(self):
        self.assertFalse(self.ask(b"not json\n")["ok"])
        self.assertFalse(self.ask(b'{"x": 1}\n')["ok"])
        self.assertFalse(self.ask(b"[1, 2]\n")["ok"])
        self.assertTrue(self.ask(b'{"cmd": "status"}\n')["ok"])

    def test_handler_exception_gives_error_reply_and_server_lives(self):
        r = self.ask(b'{"cmd": "boom"}\n')
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["error"])
        self.assertTrue(self.ask(b'{"cmd": "status"}\n')["ok"])

    def test_stop_closes_connections(self):
        self.srv.stop()
        try:
            self.assertEqual(self.f.readline(), b"")        # ren stängning
        except ConnectionResetError:
            pass                                            # Windows ger återställd anslutning
        self.assertFalse(self.srv.running)


class BridgeControlSupportTest(unittest.TestCase):
    """Ramnäs = A (RX2 = mottagare 1), Blecket = B (RX1 = mottagare 0)."""

    def setUp(self):
        self.a = FakeRadio("SunSDR2PRO", extra_hello=["vfo:1,0,14074000;"])
        self.b = FakeRadio("SunSDR2DX", extra_hello=["vfo:0,0,7074000;"])
        self.events = queue.Queue()
        self.links = {}
        for key, r in (("A", self.a), ("B", self.b)):
            link = TciLink(key, "127.0.0.1", r.port, self.events)
            self.links[key] = link
            link.start()
        self.assertTrue(wait_for(lambda: all(l.hello for l in self.links.values())))
        self.mapping = br.Mapping("A", 1, "A", 1)
        self.guarded = []
        self.bridge = TciBridge(self.links, lambda: self.mapping, port=0,
                                echo_guard=lambda k, n, a: self.guarded.append((k, n, tuple(a))))
        self.bridge.start()
        port = self.bridge._server._srv.getsockname()[1]
        self.client = websocket.create_connection("ws://127.0.0.1:%d" % port, timeout=3)
        while "ready;" not in self.client.recv():
            pass

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

    def test_rx_dial_reads_the_rx_cells_vfo_a(self):
        self.assertEqual(self.bridge.rx_dial(), 14074000)           # Ramnäs RX2
        self.mapping = br.Mapping("B", 0, "B", 0)
        self.assertEqual(self.bridge.rx_dial(), 7074000)            # Blecket RX1

    def test_set_dial_goes_to_the_rx_cell_only(self):
        ok, text = self.bridge.set_dial(10136000)
        self.assertTrue(ok)
        self.assertEqual(text, "10136000 Hz")
        self.assertTrue(wait_for(lambda: "VFO:1,0,10136000;" in self.a.texts))
        self.assertEqual([t for t in self.b.texts if t.lower().startswith("vfo")], [])

    def test_set_dial_is_echoed_to_wsjtx_and_survives_a_place_switch(self):
        self.assertTrue(self.bridge.set_dial(10136000)[0])
        self.a.emit("vfo:1,0,10136000;")                            # radions eko
        self.assertEqual(self.client.recv(), "vfo:0,0,10136000;")
        self.mapping = br.Mapping("B", 0, "B", 0)
        self.bridge.on_mapping_changed()
        self.assertTrue(wait_for(lambda: "VFO:0,0,10136000;" in self.b.texts))   # Blecket får samma dial

    def test_set_dial_refused_during_ptt(self):
        self.client.send("trx:0,true,tci;")
        self.assertTrue(wait_for(lambda: self.bridge.ptt_active))
        ok, text = self.bridge.set_dial(10136000)
        self.assertFalse(ok)
        self.assertIn("transmission", text)
        self.assertEqual([t for t in self.a.texts if t.startswith("VFO:1,0,10136000")], [])

    def test_companion_dial_goes_to_ramnas_rx1_only_and_is_echo_guarded(self):
        ok, text = self.bridge.set_companion_dial("A", 0, 10136000)
        self.assertTrue(ok)
        self.assertEqual(text, "10136000 Hz")
        self.assertTrue(wait_for(lambda: "VFO:0,0,10136000;" in self.a.texts))      # Ramnäs RX1
        self.assertEqual([t for t in self.a.texts if t.startswith("VFO:1,0,10136000")], [])   # inte RX2
        self.assertEqual([t for t in self.b.texts if t.lower().startswith("vfo")], [])
        self.assertIn(("A", "VFO", ("0", "0", "10136000")), self.guarded)          # Sync ignorerar ekot

    def test_companion_echo_never_reaches_wsjtx(self):
        self.bridge.set_companion_dial("A", 0, 10136000)
        self.a.emit("vfo:0,0,10136000;")                         # radions eko på RX1
        self.client.settimeout(0.5)
        with self.assertRaises(websocket.WebSocketTimeoutException):
            self.client.recv()

    def test_companion_dial_refused_during_ptt(self):
        self.client.send("trx:0,true,tci;")
        self.assertTrue(wait_for(lambda: self.bridge.ptt_active))
        ok, _ = self.bridge.set_companion_dial("A", 0, 10136000)
        self.assertFalse(ok)
        self.assertEqual([t for t in self.a.texts if t.startswith("VFO:0,0,10136000")], [])

    def test_wsjt_dial_is_the_clients_dial_else_the_rx_cells_own(self):
        self.assertEqual(self.bridge.wsjt_dial(), 14074000)      # ännu inget från klienten: RX2:s egen
        self.client.send("vfo:0,0,10136000;")
        self.assertTrue(wait_for(lambda: self.bridge.wsjt_dial() == 10136000))

    def test_switch_stats_measures_time_to_first_rx_audio_from_new_cell(self):
        self.assertIsNone(self.bridge.switch_stats())
        self.client.send("audio_start:0;")
        self.assertTrue(wait_for(lambda: "AUDIO_START:1;" in self.a.texts))
        self.mapping = br.Mapping("B", 0, "B", 0)
        self.bridge.on_mapping_changed()
        self.a.emit(block(frames.RX_AUDIO_STREAM, 1))               # gammal cell: räknas inte
        self.assertIsNone(self.bridge.switch_stats())
        self.assertTrue(wait_for(lambda: "AUDIO_START:0;" in self.b.texts))
        self.b.emit(block(frames.RX_AUDIO_STREAM, 0))               # ny cell
        self.assertTrue(wait_for(lambda: self.bridge.switch_stats() is not None))
        self.assertLess(self.bridge.switch_stats(), 2000)


if __name__ == "__main__":
    unittest.main()
