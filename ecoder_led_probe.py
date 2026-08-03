#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ecoder_led_probe.py - skickar en output-rapport (2 byte, verifierat via
OutputReportByteLength) till E-Codern2 for att kartlagga backgrundsbelysning/
LED-styrning (3 lysdioder + bakgrundsbelysning enligt ExpertSDR3-manualen,
"Key Led"/"Encoder Led"). Rapportformatet ar okant, kartlaggs empiriskt genom
att prova bitmonster och observera vad som hander pa panelen.

Kor:  python ecoder_led_probe.py <hex>   t.ex. "0001", "0002", "00ff"
"""

import sys

from ecoder_hid import EcoderHid


def main():
    if len(sys.argv) != 2:
        print("Anvandning: python ecoder_led_probe.py <hex, t.ex. 0001>")
        return 1
    hexstr = sys.argv[1]
    data = bytes.fromhex(hexstr)

    h = EcoderHid()
    h.open()
    print("Enhet: '%s', output_length=%d" % (h.product, h.output_length))
    print("Skickar: %s" % data.hex())
    try:
        h.write(data)
        print("Skickat OK.")
    finally:
        h.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
