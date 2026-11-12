# -*- coding: utf-8 -*-
"""Tester för TCI-proxyn (tci_proxy.py) mot en falsk radio. Körs med:
python -m unittest discover -s tests"""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import websocket

from tci_proxy import Rx1Filter, TciProxy, strip_header
from ws_server import WsServer
from test_tci_bridge import wait_for


class Radio:
    """Falsk ExpertSDR3: skickar en start med huvud och läge, svarar på `vfo:0,0;` bara till
    den anslutning som frågade och kan startas om på samma port (som en krasch)."""

    def __init__(self, freq="14074000"):
        self.freq = freq
        self.texts = []
        self.conns = []
        self.port = 0
        self.server = None
        self.start()

    def start(self):
        self.server = WsServer("127.0.0.1", self.port, self._serve)
        self.server.start()
        self.port = self.server._srv.getsockname()[1]

    def crash(self):
        self.server.stop()
        for c in list(self.conns):
            c.close()
        self.conns = []

    def _serve(self, conn):
        self.conns.append(conn)
        for t in ["protocol:ExpertSDR3,2.1;", "device:SunSDR2PRO;", "trx_count:2;",
                  "vfo:0,0,%s;" % self.freq, "modulation:0,cw;", "ready;"]:
            conn.send_text(t)
        try:
            while True:
                m = conn.recv()
                if isinstance(m, bytes):
                    continue
                self.texts.append(m)
                if m.strip().lower() == "vfo:0,0;":
                    conn.send_text("vfo:0,0,%s;" % self.freq)
        except (ConnectionError, OSError):
            pass

    def emit(self, text):
        for c in list(self.conns):
            try:
                c.send_text(text)
            except OSError:
                pass


class Client:
    def __init__(self, port):
        self.ws = websocket.create_connection("ws://127.0.0.1:%d" % port, timeout=3)
        self.got = []
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        self.ws.settimeout(0.2)
        while True:
            try:
                m = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                return
            if isinstance(m, str):
                self.got.append(m)

    def has(self, text):
        return any(text in g for g in self.got)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


class StripHeaderTest(unittest.TestCase):
    def test_header_removed_state_kept(self):
        self.assertEqual(strip_header("protocol:ExpertSDR3,2.1;"), "")
        self.assertEqual(strip_header("ready;"), "")
        self.assertEqual(strip_header("vfo:0,0,7074000;"), "vfo:0,0,7074000;")
        self.assertEqual(strip_header("device:X;vfo:1,0,3573000;READY;"), "vfo:1,0,3573000;")


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.radio = Radio()
        self.logs = []
        self.proxy = TciProxy("127.0.0.1", self.radio.port, port=0, log=self.logs.append)
        self.proxy.start()
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.proxy.stop()
        self.radio.crash()

    def client(self):
        c = Client(self.proxy.port)
        self.clients.append(c)
        return c

    def test_pass_through_both_ways(self):
        c = self.client()
        self.assertTrue(wait_for(lambda: c.has("ready;")))
        self.assertTrue(c.has("protocol:ExpertSDR3"))
        self.assertTrue(c.has("vfo:0,0,14074000;"))
        c.ws.send("vfo:1,0,3573000;")
        self.assertTrue(wait_for(lambda: "vfo:1,0,3573000;" in self.radio.texts))
        self.radio.emit("split_enable:1,true;")
        self.assertTrue(wait_for(lambda: c.has("split_enable:1,true;")))

    def test_each_client_has_its_own_radio_connection(self):
        a, b = self.client(), self.client()
        self.assertTrue(wait_for(lambda: a.has("ready;") and b.has("ready;")))
        self.assertEqual(len(self.radio.conns), 2)
        n_b = len(b.got)
        a.ws.send("vfo:0,0;")
        self.assertTrue(wait_for(lambda: sum(g.count("vfo:0,0,14074000;") for g in a.got) >= 2))
        time.sleep(0.2)
        self.assertEqual(len(b.got), n_b)           # svaret på a:s fråga gick bara till a
        self.assertEqual(self.proxy.status(), (2, 2))

    def test_client_survives_radio_crash(self):
        c = self.client()
        self.assertTrue(wait_for(lambda: c.has("ready;")))
        self.radio.crash()
        self.assertTrue(wait_for(lambda: self.proxy.status() == (1, 0)))
        c.ws.send("vfo:0,0,7000000;")               # radion borta: kastas
        time.sleep(0.3)
        n = len(c.got)
        self.radio.freq = "7074000"
        self.radio.start()                          # samma port, som efter en omstart av ExpertSDR3
        self.assertTrue(wait_for(lambda: c.has("vfo:0,0,7074000;"), timeout=6))
        new = "".join(c.got[n:])
        self.assertNotIn("protocol:", new)
        self.assertNotIn("device:", new)
        self.assertNotIn("ready;", new)
        self.assertIn("modulation:0,cw;", new)
        self.assertNotIn("vfo:0,0,7000000;", self.radio.texts)
        c.ws.send("vfo:0,0,7010000;")
        self.assertTrue(wait_for(lambda: "vfo:0,0,7010000;" in self.radio.texts))
        self.assertTrue(any("1 client commands dropped" in t for t in self.logs))

    def test_client_waits_when_radio_is_down_at_connect(self):
        self.radio.crash()
        c = self.client()
        time.sleep(0.5)
        self.assertFalse(c.has("ready;"))
        self.radio.start()
        self.assertTrue(wait_for(lambda: c.has("ready;"), timeout=6))
        self.assertTrue(c.has("protocol:ExpertSDR3"))  # första anslutningen: hela starten

    def test_client_disconnect_closes_radio_connection(self):
        c = self.client()
        self.assertTrue(wait_for(lambda: c.has("ready;")))
        c.close()
        self.assertTrue(wait_for(lambda: self.proxy.status() == (0, 0)))


class Rx1FilterTest(unittest.TestCase):
    def test_to_client(self):
        f = Rx1Filter()
        self.assertEqual(f.to_client("trx_count:2;"), "trx_count:1;")
        self.assertEqual(f.to_client("vfo:1,0,7074000;"), "")
        self.assertEqual(f.to_client("vfo:0,0,7074000;vfo:1,1,6563000;modulation:1,DIGU;"),
                         "vfo:0,0,7074000;")
        self.assertEqual(f.to_client("if:1,1,1500;rx_enable:1,false;agc_gain:1,74;"), "")
        self.assertEqual(f.to_client("app_focus:false;volume:-15;"), "app_focus:false;volume:-15;")
        self.assertEqual(f.to_client("channels_count:2;"), "channels_count:2;")

    def test_tx_frequency_hidden_while_rx2_transmits(self):
        f = Rx1Filter()
        self.assertEqual(f.to_client("tx_frequency:7073400;"), "tx_frequency:7073400;")
        self.assertEqual(f.to_client("trx:1,true;tx_frequency:7054500;"), "")
        self.assertEqual(f.to_client("trx:1,false;"), "")
        self.assertEqual(f.to_client("trx:0,true;tx_frequency:5357000;"),
                         "trx:0,true;tx_frequency:5357000;")

    def test_to_radio(self):
        f = Rx1Filter()
        self.assertEqual(f.to_radio("VFO:1,0;"), "")
        self.assertEqual(f.to_radio("VFO:0,0;"), "VFO:0,0;")
        self.assertEqual(f.to_radio("VFO:0,0;SPLIT_ENABLE:1;MODULATION:0;"), "VFO:0,0;MODULATION:0;")


class OnlyRx1ProxyTest(unittest.TestCase):
    def test_client_sees_one_receiver(self):
        radio = Radio()
        proxy = TciProxy("127.0.0.1", radio.port, port=0, only_rx1=True)
        proxy.start()
        c = Client(proxy.port)
        try:
            self.assertTrue(wait_for(lambda: c.has("ready;")))
            self.assertTrue(c.has("trx_count:1;"))
            self.assertFalse(c.has("trx_count:2"))
            c.ws.send("VFO:1,0;")
            c.ws.send("vfo:0,0;")
            self.assertTrue(wait_for(lambda: "vfo:0,0;" in radio.texts))
            self.assertNotIn("VFO:1,0;", radio.texts)
            radio.emit("vfo:1,0,3573000;")
            radio.emit("vfo:0,0,5357000;")
            self.assertTrue(wait_for(lambda: c.has("vfo:0,0,5357000;")))
            self.assertFalse(c.has("vfo:1,0,3573000;"))
        finally:
            c.close()
            proxy.stop()
            radio.crash()


class TrafficLogTest(unittest.TestCase):
    def test_both_directions_logged(self):
        radio = Radio()
        path = os.path.join(tempfile.mkdtemp(), "traffic.log")
        proxy = TciProxy("127.0.0.1", radio.port, port=0, traffic_path=path)
        proxy.start()
        c = Client(proxy.port)
        try:
            self.assertTrue(wait_for(lambda: c.has("ready;")))
            c.ws.send("vfo:0,0;")
            def text():
                if not os.path.exists(path):
                    return ""
                with open(path, encoding="utf-8") as f:
                    return f.read()
            self.assertTrue(wait_for(lambda: "> vfo:0,0;" in text()))
            self.assertIn("< ready;", text())
        finally:
            c.close()
            proxy.stop()
            radio.crash()


if __name__ == "__main__":
    unittest.main()
