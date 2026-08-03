#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ecoder_decode.py - oversatter raa E-Coder-HID-rapporter till namngivna handelser
med hjalp av en inlard karta (ecoder_map.json).

Delas av testverktyget (ecoder_test.py) och den kommande HID->TCI-oversattaren.

Decoder.feed(report) returnerar en lista handelser:
    (key, "press", None)     - knapp nedtryckt
    (key, "release", None)   - knapp slappt
    (key, "turn", delta)     - vred vridet, delta ar signerat (+ medurs, - moturs)
"""

import json
import os


def load_mapping(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "ecoder_map.json")
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    return doc.get("controls", {}), doc.get("device", {})


class Decoder:
    def __init__(self, mapping):
        self.mapping = mapping
        self.state = {}   # key -> bool (nedtryckt)

    def feed(self, report):
        events = []
        for key, m in self.mapping.items():
            b = m.get("byte")
            if b is None or b >= len(report):
                continue
            if m["type"] == "button":
                mask = m.get("mask", 0)
                on = (report[b] & mask) == mask and mask != 0
                was = self.state.get(key, False)
                if on and not was:
                    events.append((key, "press", None))
                elif was and not on:
                    events.append((key, "release", None))
                self.state[key] = on
            elif m["type"] == "encoder":
                v = report[b]
                if v:
                    delta = v - 256 if v >= 128 else v   # signerad int8
                    events.append((key, "turn", delta))
        return events
