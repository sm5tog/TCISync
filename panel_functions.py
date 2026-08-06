#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""panel_functions.py - catalog of the E-Coder buttons' possible functions,
plus the default button mapping (Jonny's current sunsync setup).

The panel image (14 rectangular buttons) uses the same physical layout as
ExpertSDR3's own E-Coder configuration screen, but the CHOICES in the
dropdown are TCISync's/sunsync's own functions, not ESDR3's built-in ones -
ESDR3's panel control is disabled, TCISync reads the panel itself via HID
(see ecoder_hid.py/ecoder_decode.py).

Buttons are identified only by physical number in the UI (no invented
"friendly" names) - a button with no function assigned is just "unused",
not labeled with a name that happens to describe something else.
"""

FUNCTION_CATALOG = {
    "unused":              {"label": "Unused"},
    "cw_macro":            {"label": "CW macro", "param": "text", "param_default": ""},
    "tune":                {"label": "TUNE (toggle transmit)"},
    "toggle_listen_both":  {"label": "Toggle TX+RX listening"},
    "toggle_listen_tx":    {"label": "Temporary TX listening (on/off)"},
    "change_vfo":          {"label": "Switch encoder ear/TX"},
    "wpm_preset":          {"label": "Set WPM", "param": "wpm", "param_default": 20},
    "toggle_rit":          {"label": "RIT on/off"},
    "split_offset":        {"label": "Split"},
    "toggle_volume_mute":  {"label": "Volume mute/unmute"},
}

# (physical number, panel key). No display name beyond the number - see
# module docstring.
BUTTON_LAYOUT = [
    (1, "zoom_up"),
    (2, "split"),
    (3, "switch_rx"),
    (4, "rit_reset"),
    (5, "listen_ab"),
    (6, "zoom_down"),
    (7, "step_down"),
    (8, "step_up"),
    (9, "if_as_rit"),
    (10, "listen_a"),
    (11, "tune"),
    (12, "listen_b"),
    (13, "change_vfo"),
    (14, "vfob_onoff"),
]

# The three knob-press buttons (on top of the WPM/DRIVE/RIT knobs).
KNOB_PRESS_LAYOUT = [
    ("custom_vred_v_tryck", "Left knob press"),
    ("custom_vred_m_tryck", "Center knob press"),
    ("custom_vred_h_tryck", "Right knob press"),
]

# Assignment targets for the 3 status LEDs: "none" plus every button/knob-press
# key, shown by physical number/name. An LED mirrors the ON/OFF state of
# WHATEVER function currently sits on the chosen button (see
# EncoderRouter._button_active_state) - not a fixed function of its own, so
# it stays correct if the button's assignment is changed later.
LED_TARGETS = [("none", "None")]
LED_TARGETS += [(key, "Button %d" % num) for num, key in BUTTON_LAYOUT]
LED_TARGETS += list(KNOB_PRESS_LAYOUT)

DEFAULT_LED_MAP = {"left": "none", "right": "none", "center": "none"}
DEFAULT_BACKLIGHT = True   # big-knob light + keypad backlight, shared on/off

DEFAULT_PANEL_MAP = {
    "zoom_up":              {"func": "cw_macro", "param": "SM5K"},
    "split":                {"func": "unused"},
    "switch_rx":            {"func": "tune"},
    "rit_reset":            {"func": "unused"},
    "listen_ab":            {"func": "unused"},
    "zoom_down":            {"func": "cw_macro", "param": "UR 5NN"},
    "step_down":            {"func": "unused"},
    "step_up":              {"func": "unused"},
    "if_as_rit":            {"func": "unused"},
    "listen_a":             {"func": "unused"},
    "tune":                 {"func": "cw_macro", "param": "TU"},
    "listen_b":             {"func": "toggle_listen_both"},
    "change_vfo":           {"func": "change_vfo"},
    "vfob_onoff":           {"func": "toggle_listen_tx"},
    "custom_vred_v_tryck":  {"func": "wpm_preset", "param": 20},
    "custom_vred_m_tryck":  {"func": "toggle_volume_mute"},
    "custom_vred_h_tryck":  {"func": "toggle_rit"},
}
