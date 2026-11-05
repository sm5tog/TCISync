#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""control_probe.py - mäter styrgränssnittet mot en körande TCISync (inte en del av programmet).

Kräver: TCISync körs, WSJT bridge och "Enable control interface" är påslagna i
"WSJT bridge...", WSJT-X/WSJT-K är ansluten till bron och står på FT8 med mottagning på.
Endast standardbibliotek. Skriver också allt till control_probe.log bredvid skriptet.

    python control_probe.py status
    python control_probe.py latency [N]          N platsbyten (förval 10), mäter svarstid och tid till första ljudblock
    python control_probe.py dial HZ [HZ ...]     sätter dialen och mäter tiden tills radion ekar den
    python control_probe.py schedule [RUNDOR]    byter plats var 30:e sekund, linjerat mot UTC :00/:30,
                                                 loggar varje byte i UTC (jämförs sedan mot WSJT-X ALL.TXT)
"""

import json
import os
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 60010
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control_probe.log")


class Probe:
    def __init__(self):
        self.sock = socket.create_connection((HOST, PORT), timeout=5)
        self.f = self.sock.makefile("rb")

    def ask(self, **req):
        t0 = time.monotonic()
        self.sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        reply = json.loads(self.f.readline().decode("utf-8"))
        return reply, (time.monotonic() - t0) * 1000


def say(text):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + "Z", text)
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def other(place, places):
    return [p for p in places if p != place][0]


def cmd_status(p):
    r, ms = p.ask(cmd="status")
    say("status (%.0f ms): %s" % (ms, r))


def cmd_latency(p, n):
    st, _ = p.ask(cmd="status")
    if not st.get("client_connected"):
        say("WSJT-X is not connected to the bridge; switch_audio_ms stays empty without it")
    places = st["places"]
    place = st["rx"]
    for i in range(n):
        place = other(place, places)
        r, ms = p.ask(cmd="set_place", place=place)
        t0 = time.monotonic()
        audio = None
        while time.monotonic() - t0 < 5:
            s, _ = p.ask(cmd="status")
            if s.get("switch_audio_ms") is not None:
                audio = s["switch_audio_ms"]
                break
            time.sleep(0.02)
        say("switch %d to %s: reply %.0f ms, ok=%s, first audio block %s ms" % (
            i + 1, place, ms, r.get("ok"), audio if audio is not None else "none within 5 s"))
        time.sleep(2)


def cmd_dial(p, hzs):
    for hz in hzs:
        r, ms = p.ask(cmd="set_dial", hz=hz)
        t0 = time.monotonic()
        seen = None
        while time.monotonic() - t0 < 5:
            s, _ = p.ask(cmd="status")
            if s.get("dial_hz") == hz:
                seen = (time.monotonic() - t0) * 1000
                break
            time.sleep(0.02)
        say("set_dial %d: reply %.0f ms, ok=%s, radio echoed after %s" % (
            hz, ms, r.get("ok"), "%.0f ms" % seen if seen is not None else "no echo within 5 s"))
        time.sleep(1)


def cmd_schedule(p, rounds):
    st, _ = p.ask(cmd="status")
    places = st["places"]
    place = st["rx"]
    say("schedule start, places %s, current %s" % (places, place))
    for i in range(rounds):
        boundary = (int(time.time() // 30) + 1) * 30          # nästa hel eller halv minut, UTC
        time.sleep(max(0.0, boundary - time.time() - 0.05))   # sov till 50 ms före
        while time.time() < boundary:                         # vänta ut resten aktivt (timerfelet på Windows är ~15 ms)
            pass
        t_send = time.time()
        place = other(place, places)
        r, ms = p.ask(cmd="set_place", place=place)
        say("SWITCH to %s: sent %+.0f ms from %s boundary, reply %.0f ms, ok=%s" % (
            place, (t_send - boundary) * 1000,
            time.strftime("%H:%M:%S", time.gmtime(boundary)) + "Z", ms, r.get("ok")))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    p = Probe()
    cmd = argv[1]
    if cmd == "status":
        cmd_status(p)
    elif cmd == "latency":
        cmd_latency(p, int(argv[2]) if len(argv) > 2 else 10)
    elif cmd == "dial":
        cmd_dial(p, [int(a) for a in argv[2:]])
    elif cmd == "schedule":
        cmd_schedule(p, int(argv[2]) if len(argv) > 2 else 8)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
