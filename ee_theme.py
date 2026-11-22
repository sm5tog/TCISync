#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ee_theme.py - Färger, typsnitt och mått för TCISync (ExpertSDR3-look).

All utseendekonfiguration bor här så cockpiten och kommande moduler (encoder-
styrning, sliders m.m.) delar samma look. Inget beteende, bara stil.

Färgtokens, typsnitt och mått är avlästa ur en ExpertSDR3-skärmdump (SunSDR2 DX);
se ee_palette_reference.md för källa och brasklapp (närmevärden, kan pipett-trimmas).
"""

# --- Färgpalett (verbatim ur ee_palette_reference.md) --------------------------
PALETTE = {
    "bg_app": "#0e0e10", "bg_surface": "#1a1a1d", "bg_control": "#242429", "bg_black": "#000000",
    "accent_green": "#3f9142", "accent_green_bright": "#7cff6a",
    "accent_blue": "#2b6ca6", "accent_blue_bright": "#3a9ad9",
    "accent_red": "#cc2b2b", "accent_orange": "#d38b2a", "accent_orange_bright": "#ffc24a",
    "bg_heard": "#2f6b35",
    "text_primary": "#e6e6e6", "text_secondary": "#9aa3b2", "text_disabled": "#5a5f68",
    "text_freq": "#ffffff", "text_freq_secondary": "#b8beca",
    "btn_idle_bg": "#2a2a2e", "btn_idle_border": "#3a3a3e", "btn_hover_bg": "#34343a",
    "btn_pressed_bg": "#202024", "btn_active_bg": "#3f9142", "btn_selected_bg": "#2b6ca6",
    "btn_text": "#dcdcdc", "btn_text_on_accent": "#ffffff",
    "status_connected": "#3f9142", "status_disconnected": "#6a6a6a", "status_tx": "#cc2b2b",
    "border": "#333336", "divider": "#2a2a2d",
    "spectrum_trace": "#5cc85c", "spectrum_fill": "#12200f", "spectrum_axis": "#8a8a8a",
    "vfo_line": "#cc2b2b", "tune_marker": "#3a9ad9",
}

FONT = {"family": "Segoe UI", "freq_family": "Consolas",
        "label": 9, "small": 8, "body": 10, "split": 12, "freq_main": 28, "freq_secondary": 14}

METRICS = {"btn_height": 22, "btn_min_width": 40, "gap": 2, "radius": 3,
           "panel_pad": 8, "cell_pad": 6}


def color(token):
    """Slå upp en färg ur paletten (KeyError avslöjar felstavade token direkt)."""
    return PALETTE[token]
