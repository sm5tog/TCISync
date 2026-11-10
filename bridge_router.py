#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge_router.py - routingreglerna för WSJT-bron (ren logik, inga sockets, inga trådar).

WSJT-X tror sig prata med EN radio med EN mottagare ("TCI Client RX1", mottagare 0).
Bron delar upp det på två radiocellar (se Mapping):
  öra-cellen (ear)  = där RX-ljudet kommer ifrån
  TX-cellen (tx)    = dit PTT, TX-ljud och sändningsparametrar går

Varje TCI-kommando tillhör en grupp:
  RX   - bara öra-cellen            (ljudström, S-meter, mute, AGC ...)
  TX   - bara TX-cellen             (PTT, drive, split, XIT, start/stop ...) - även okända kommandon
  BOTH - båda cellerna              (mode, RIT ...)
WSJT-X kör alltid split: dialen (VFO A) är det man hör och hör till öra-cellen, sändfrekvensen
(VFO B) och split hör till TX-cellen.
Klientens mottagarnummer (crx) är 0 för Rig 'TCI Client RX1' och 1 för 'TCI Client RX2' (0.22.0);
routern översätter till respektive cells eget mottagarnummer, åt båda hållen. Ett annat nummer
än crx från klienten skickas oförändrat. RX_ENABLE från klienten skickas aldrig till en radio.
"""

from collections import namedtuple

Mapping = namedtuple("Mapping", "ear_key ear_rx tx_key tx_rx")

RX_GROUP = {"AUDIO_START", "AUDIO_STOP", "AUDIO_SAMPLERATE", "RX_SMETER", "RX_SENSORS",
            "RX_SENSORS_ENABLE", "RX_CHANNEL_SENSORS", "RX_MUTE", "VOLUME", "MUTE",
            "SQL_ENABLE", "SQL_LEVEL", "AGC_MODE", "AGC_GAIN", "RX_FILTER_BAND", "RX_ENABLE",
            "VFO"}          # VFO kanal 0 (dialen); kanal 1 (VFO B) fångas av CHANNEL_ARG-regeln i group_of
BOTH_GROUP = {"MODULATION", "DDS", "IF", "RIT_ENABLE", "RIT_OFFSET", "RX_CHANNEL_ENABLE"}
# Kommandon vars första argument är mottagar-/transceivernumret (resten har bool/dB/Hz först).
ARG0_RX = {"AUDIO_START", "AUDIO_STOP", "RX_SMETER", "RX_SENSORS", "RX_CHANNEL_SENSORS",
           "RX_MUTE", "SQL_ENABLE", "SQL_LEVEL", "AGC_MODE", "AGC_GAIN", "RX_FILTER_BAND",
           "RX_ENABLE", "VFO", "MODULATION", "DDS", "IF", "RIT_ENABLE", "RIT_OFFSET",
           "RX_CHANNEL_ENABLE", "TRX", "TX_ENABLE", "DRIVE", "TUNE_DRIVE", "TUNE",
           "SPLIT_ENABLE", "XIT_ENABLE", "XIT_OFFSET", "TX_SENSORS", "TX_POWER", "TX_SWR"}
CHANNEL_ARG = {"VFO", "RX_CHANNEL_ENABLE"}   # args[1] = kanal; kanal 1 (VFO B = sändfrekvens) hör till TX-cellen


def split_commands(text):
    """'a:1,2;b:3;' -> [('a', ['1','2']), ('b', ['3'])]. Namnet behåller sin skiftläge."""
    out = []
    for raw in text.split(";"):
        raw = raw.strip()
        if not raw:
            continue
        name, _, rest = raw.partition(":")
        out.append((name.strip(), [a.strip() for a in rest.split(",")] if rest else []))
    return out


def format_command(name, args):
    return "%s:%s;" % (name, ",".join(args)) if args else "%s;" % name


def group_of(name, args):
    n = name.upper()
    if n in CHANNEL_ARG and len(args) >= 2 and args[1] == "1":
        return "TX"
    if n in RX_GROUP:
        return "RX"
    if n in BOTH_GROUP:
        return "BOTH"
    return "TX"


def client_command(text, m, crx="0"):
    """Ett meddelande från WSJT-X -> lista [(radio_key, kommandotext)] att skicka vidare.
    crx = klientens egna mottagarnummer ("0" = Rig 'TCI Client RX1', "1" = 'TCI Client RX2')."""
    out = []
    for name, args in split_commands(text):
        n = name.upper()
        if n == "RX_ENABLE":      # bron svarar själv att klientens mottagare är på; skickas aldrig till en radio
            continue
        g = group_of(name, args)
        targets = []
        if g in ("RX", "BOTH"):
            targets.append((m.ear_key, m.ear_rx))
        if g in ("TX", "BOTH"):
            targets.append((m.tx_key, m.tx_rx))
        for key, rx in dict.fromkeys(targets):
            a = list(args)
            if n in ARG0_RX and a and a[0] == crx:
                a[0] = str(rx)
            item = (key, format_command(name, a))
            if item not in out:
                out.append(item)
    return out


def hello_message(radio_key, text, m):
    """Hello-text från TX-cellen -> text till WSJT-X, med varje mottagarnumrerat kommando både som
    mottagare 0 och som mottagare 1, så Rig 'TCI Client RX1' och 'TCI Client RX2' båda hittar sin
    mottagare. Övriga kommandon går med en gång. Returnerar en lista med ett kommando per element
    (tom om inget hör hemma i klientens bild)."""
    out = []
    for name, args in split_commands(text):
        single = format_command(name, args)
        for crx in ("0", "1") if name.upper() in ARG0_RX else ("0",):
            part = radio_message(radio_key, single, m, crx)
            if part:
                out.append(part)
    return out


def radio_message(radio_key, text, m, crx="0"):
    """Ett textmeddelande från en radio -> text till WSJT-X (mottagarnummer översatt till klientens
    nummer crx, "0" = RX1 och "1" = RX2), eller None om det inte hör hemma i klientens bild
    (fel radio eller fel mottagare)."""
    out = []
    for name, args in split_commands(text):
        n = name.upper()
        g = group_of(name, args)
        src_key, src_rx = (m.ear_key, m.ear_rx) if g == "RX" else (m.tx_key, m.tx_rx)
        if radio_key != src_key:
            continue
        a = list(args)
        if n in ARG0_RX and a:
            if a[0] != str(src_rx):
                continue
            a[0] = crx
        out.append(format_command(name, a))
    return "".join(out) or None
