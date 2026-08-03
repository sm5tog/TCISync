#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ecoder_leds.py - LED/backlight control for the E-Coder2, mapped empirically
2026-08-03 (see DOCUMENTATION.md). Output report is 2 bytes: byte 0 is the
report ID (always 0x00, has no effect), byte 1 is a bitmask:

    bit 0 (0x01) - left-knob LED (green)
    bit 1 (0x02) - right-knob LED (yellow)
    bit 2 (0x04) - center-knob LED (red)
    bit 3 (0x08) - main tuning-knob light
    bit 4 (0x10) - keypad backlight (buttons 1-14)
    bit 5-7      - unused
"""

BIT_LEFT = 0x01
BIT_RIGHT = 0x02
BIT_CENTER = 0x04
BIT_BIG_KNOB = 0x08
BIT_KEYPAD = 0x10
BIT_BACKLIGHT = BIT_BIG_KNOB | BIT_KEYPAD


class LedController:
    def __init__(self, hid):
        """hid -> an opened EcoderHid (must have been opened with write
        support, see ecoder_hid.EcoderHid.open())."""
        self.hid = hid
        self._last = None

    def set(self, left=False, right=False, center=False, backlight=False):
        """Writes the LED state. No-op (no HID write) if unchanged since the
        last call, so this is cheap to call every pump tick."""
        mask = 0
        if left:
            mask |= BIT_LEFT
        if right:
            mask |= BIT_RIGHT
        if center:
            mask |= BIT_CENTER
        if backlight:
            mask |= BIT_BACKLIGHT
        if mask == self._last:
            return
        try:
            self.hid.write(bytes([0x00, mask]))
            self._last = mask
        except OSError:
            pass   # device unplugged etc. - don't crash the app over LEDs

    def off(self):
        self.set(False, False, False, False)
