#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tci_frames.py - binära TCI-strömblock (tci_protocol.txt §3.4).

Ett enda ansvar: läsa och skriva headern i ett strömblock. Headern är 16 st
little-endian uint32 (64 byte): receiver, sample_rate, format, codec, crc, length,
type, channels, reserv[8]. Ingen nätverkskod och ingen routing här.
"""

import struct

HEADER_SIZE = 64
FLOAT32 = 3                 # SampleType.FLOAT32 i fältet `format`

IQ_STREAM = 0
RX_AUDIO_STREAM = 1
TX_AUDIO_STREAM = 2
TX_CHRONO = 3
LINEOUT_STREAM = 4

_RECEIVER = struct.Struct("<I")
_TYPE = struct.Struct("<I")


def stream_type(data):
    """Strömtyp (fältet `type`, byte 24..27) eller None om blocket är för kort."""
    if len(data) < HEADER_SIZE:
        return None
    return _TYPE.unpack_from(data, 24)[0]


def receiver(data):
    return _RECEIVER.unpack_from(data, 0)[0]


def with_receiver(data, rx):
    """Kopia av blocket med fältet `receiver` (byte 0..3) satt till rx."""
    buf = bytearray(data)
    _RECEIVER.pack_into(buf, 0, rx)
    return bytes(buf)


def peak_level(data):
    """Största absolutvärdet i ett float32-ljudblock (0.0-1.0 för normalt ljud), eller None om
    blocket inte är float32 eller inte går att tolka. Används bara för diagnostikloggning."""
    try:
        import numpy as np
        if len(data) < HEADER_SIZE or struct.unpack_from("<I", data, 8)[0] != FLOAT32:
            return None
        n = min(struct.unpack_from("<I", data, 20)[0], (len(data) - HEADER_SIZE) // 4)
        if n <= 0:
            return 0.0
        return float(np.max(np.abs(np.frombuffer(data, dtype="<f4", count=n, offset=HEADER_SIZE))))
    except Exception:
        return None
