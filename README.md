# TCISync (by SM5K)

A lightweight Windows control panel for **Expert Electronics** radios (SunSDR2/ExpertSDR3) via **TCI**, for anyone running a single radio or mixing a local + remote (ERS) setup.

![status](https://img.shields.io/badge/status-active-brightgreen)

## Why TCISync

If you run more than one ExpertSDR3 instance — a local radio plus a remote one over ERS, or several remote radios — but only own ONE set of physical controls (one E-Coder panel, one headset, one CW paddle/key, one PTT pedal), switching which instance or receiver those controls actually act on gets clumsy fast. Normally that means clicking around in each separate ExpertSDR3 window by hand every time you want to listen or transmit somewhere else.

TCISync solves exactly that: a single mouse click moves the ear (listen) and TX focus freely between any receiver on any instance. Your PTT pedal, E-Coder panel, and CW key automatically follow whichever instance is currently TX — no manual switching in ExpertSDR3 itself, ever.

It also solves a problem that otherwise makes manual CW over a remote link impractical: normally your sidetone comes back over the network with the link's round-trip delay, making hand-sending very hard to judge. TCISync generates its sidetone entirely locally with zero network delay, so manual keying works perfectly even on a remotely controlled radio.

## Features

- Auto-detects your radio(s) from ExpertSDR3's own profile files — no IP/port to type in, see [ExpertSDR3 / TCI setup guide](README_TCISYNC.md)
- Click-to-set ear (listen) and TX markers per VFO, per receiver
- Frequency/mode/VFO B/RIT/filter sync between radios, toggleable per field
- Fully configurable E-Coder2 panel button mapping — clickable panel image, any button to any function
- Status LEDs individually assignable to mirror any button's on/off state
- Manual CW via an E-Coder paddle (iambic keyer) with local, zero-latency sidetone auto-matched to ExpertSDR3's own audio output
- Compact (borderless) or Classic (normal window frame) UI, switchable
- Always-on-top, toggleable live (View menu or the quick `^` button)

## Usage

Right-click anywhere in the window for the menu (CW settings, Button mapping, LEDs, View, Support, Exit). Left-click a frequency cell to set the ear (listen) marker, right-click to set the TX marker.

## Requirements

- Windows (tested)
- Python 3.9+ (only needed if running from source; the compiled `.exe` has no dependencies)
- ExpertSDR3, with TCI enabled — see [ExpertSDR3 / TCI setup guide](README_TCISYNC.md)
- An Expert Electronics E-Coder2 panel (optional — TCISync runs without one, just without panel button control)

## Running from source

```
python tcisync.py
```

On first run it creates `tcisync_settings.json` next to the script with default values.

## Compiled EXE

Pre-built Windows executables are published on the [Releases page](https://github.com/sm5tog/TCISync/releases) — no Python installation required.

## Support this project

If this saved you some time or money, consider [buying me a coffee via PayPal](https://www.paypal.com/donate/?hosted_button_id=5VX8NE7LQEGF4). Entirely optional — the software is free either way.

73, Jonny — SM5K
