#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""panel_functions.py - katalog över E-Coder-knapparnas möjliga funktioner samt
förvald knappmappning (Jonnys nuvarande sunsync-uppsättning).

Panelbilden (14 rektangulära knappar) är samma fysiska layout som ExpertSDR3:s
egen E-Coder-konfigurationsskärm, men VALEN i dropdownen är TCISyncs/sunsyncs
egna funktioner, inte ESDR3:s inbyggda - ESDR3:s panelstyrning är avstängd,
TCISync läser panelen själv via HID (se ecoder_hid.py/ecoder_decode.py).
"""

FUNCTION_CATALOG = {
    "unused":              {"label": "Ledig"},
    "cw_macro":            {"label": "CW-makro", "param": "text", "param_default": ""},
    "tune":                {"label": "TUNE (växla sändning)"},
    "toggle_listen_both":  {"label": "Växla TX+RX-lyssning"},
    "toggle_listen_tx":    {"label": "Tillfällig TX-lyssning (på/av)"},
    "change_vfo":          {"label": "Växla encoder öra/TX"},
    "wpm_preset":          {"label": "Sätt WPM", "param": "wpm", "param_default": 20},
    "toggle_rit":          {"label": "RIT på/av"},
}

# (fysiskt nummer, panel-nyckel, ESDR3:s namn för samma fysiska plats - bara
# för visning i UI:t, ingen funktionell koppling till ESDR3)
BUTTON_LAYOUT = [
    (1, "zoom_up", "Zoom Up"),
    (2, "split", "Split On/Off"),
    (3, "switch_rx", "Switch RX1/RX2"),
    (4, "rit_reset", "RIT Reset"),
    (5, "listen_ab", "Listen A,B"),
    (6, "zoom_down", "Zoom Down"),
    (7, "step_down", "Step Down"),
    (8, "step_up", "Step Up"),
    (9, "if_as_rit", "IF as RIT"),
    (10, "listen_a", "Listen A"),
    (11, "tune", "TUNE"),
    (12, "listen_b", "Listen B"),
    (13, "change_vfo", "Change VFOA/VFOB"),
    (14, "vfob_onoff", "VFOB On/Off"),
]

# Vredtryck (V/M/H, de tre knapparna ovanpå WPM/DRIVE/RIT-vreden) - inte med i
# panelbilden än, men samma funktionskatalog/mappningsformat.
KNOB_PRESS_LAYOUT = [
    ("custom_vred_v_tryck", "Vänster vred-tryck (WPM)"),
    ("custom_vred_m_tryck", "Mitten vred-tryck (DRIVE)"),
    ("custom_vred_h_tryck", "Höger vred-tryck (RIT)"),
]

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
    "custom_vred_m_tryck":  {"func": "unused"},
    "custom_vred_h_tryck":  {"func": "toggle_rit"},
}
