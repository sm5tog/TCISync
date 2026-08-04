# TCISync (by SM5K) — Full Setup & User Guide

This guide covers everything you need: what TCISync does, how to set up ExpertSDR3 so it can find your radio(s), and how to use every feature. It supplements the main [README](README.md).

## What TCISync is

TCISync is a small control panel that sits on top of ExpertSDR3 (SunSDR2 / Expert Electronics radios) and talks to it over **TCI** (Transceiver Control Interface, a websocket protocol built into ExpertSDR3). It's built for one specific job: **listening on one place and transmitting on another, without touching ExpertSDR3's own controls to do it.**

That covers two common situations:

- A single radio with two receivers (RX1/RX2) — listen on one VFO, transmit on the other.
- A local radio plus a second radio reached remotely over ERS — listen locally, transmit remotely (or vice versa), or any combination.

TCISync does not replace ExpertSDR3 — it runs alongside it and drives it via TCI commands.

## Prerequisites

- ExpertSDR3 must be installed and have at least one working, already-configured profile (you've connected to the radio at least once) — TCISync creates no profiles, it only reads existing ones.
- The TCI server must have been enabled in that profile at least once.
- For a remotely controlled radio (ERS): the ERS connection must already work and have been used at least once, so that `remote_<serial>.json` exists.
- Audio must be routed through the PC's headset or speakers/microphone — the radio's own audio path can't be used.
- An E-Coder is required, and it must be disabled in ExpertSDR3 (otherwise it conflicts with TCISync).

## Step-by-step setup

1. Open ExpertSDR3 and connect to your radio as normal, with **TCI enabled** (see below).
2. If you use a remote (ERS) radio too, connect to that one at least once as well, with TCI enabled there.
3. Start TCISync. On first launch it creates `tcisync_settings.json` next to itself with default values.
4. TCISync reads ExpertSDR3's own profile files automatically — no IP or port to type in anywhere. It builds one panel per receiver it finds.
5. If nothing shows up, see [Troubleshooting checklist](#troubleshooting-checklist) below.

### Enabling the TCI server in ExpertSDR3

TCI must be switched on inside ExpertSDR3 itself before TCISync can connect — TCISync only reads the port ExpertSDR3 is already using, it can't turn TCI on for you.

From the ExpertSDR3 manual: the **TCI indicator** in the main window lets you configure and activate the server directly:

- **Right-click** the TCI indicator to open its settings — enter the TCI server port here (this is the same port TCISync will read automatically from the profile file).
- **Left-click** the TCI indicator to activate the TCI server.

Once TCI is active and a profile has been used at least once in that state, TCISync will find it automatically on its next start — no need to keep ExpertSDR3's settings screen open.

### How the auto-detection works

There are two profile files per radio serial number: `<serial>.json` (local connection) and `remote_<serial>.json` (remote/ERS connection). A profile only counts as usable if its `Gui.TCI.open` flag is `true` — that's the same flag ExpertSDR3 itself sets when its TCI server is switched on. TCISync reads the TCI port from `Gui.TCI.port` and the receiver count from `GlobalProfile_.transceivers` in that same profile.

TCISync supports up to two radios at once: one local, one remote. If you run one radio locally and connect to a second one remotely via ERS, TCISync auto-detects both from their respective profile files and builds one panel per receiver automatically. A remote radio's receiver count already reflects any ERS limitation (e.g. a 2-receiver radio restricted to 1 receiver when used remotely) — this comes straight from ExpertSDR3's own profile, TCISync doesn't need to guess it.

### No profile found (first-time setup)

If TCISync can't find any ExpertSDR3 profile with `Gui.TCI.open == true`, it falls back to a generic placeholder station so the app still starts. Open ExpertSDR3, connect to your radio at least once with TCI switched on (see above), then restart TCISync.

## The core concept: ear/TX with the mouse

Every frequency cell in TCISync's window can be given two independent markers, set with the mouse:

- **Left-click** a cell → sets the **ear** (listen) marker — green ring. This is what you hear.
- **Right-click** a cell → sets the **TX** marker — orange ring. This is what you'll transmit on.

The ear and TX markers are completely independent and can sit on **different receivers, different VFOs, and even different radios** at the same time. That's the whole point of the program: click "ear" on RX1 VFO A on your local radio, click "TX" on RX2 VFO B on your remote radio — TCISync mutes/unmutes and switches everything in ExpertSDR3 to match, on both radios, automatically.

**Do not use ExpertSDR3's own split function while running TCISync.** The TX marker in TCISync *is* your split — it's a second, independent frequency you transmit on while listening somewhere else. Turning on split inside ExpertSDR3 as well creates two systems fighting to control the same thing.

The E-Coder panel can also move the ear/TX markers (button 13 by default: "Switch encoder ear/TX", see below) — the physical knob then tunes whichever of the two you've selected, without touching the mouse.

## Feature walkthrough

Right-click anywhere in the window to open the menu: **CW settings, Button mapping, LEDs, View, Help, Support, Exit.**

### Sync (the checkbox row at the bottom)

The "Sync" button and its checkboxes (Frequency, Mode, VFO B, RIT, Filter) link your two radios so a change on one is mirrored on the other, field by field — toggle whichever fields you want mirrored, independently. Turn Sync on/off with the button on the left of that row.

### E-Coder button mapping

Right-click → **Button mapping...** opens a clickable image of the E-Coder panel, laid out exactly like ExpertSDR3's own E-Coder configuration screen. Click any button to assign it a function from a dropdown — CW macro (with your own text), TUNE, toggle TX+RX listening, temporary TX listening, switch ear/TX, set WPM, RIT on/off, or unused. Every one of the 14 buttons plus the three knob-press buttons (left/center/right) can be assigned independently — there's no fixed layout, and ExpertSDR3's own panel handling for these buttons is disabled so there's no conflict.

### Status LEDs

Right-click → **LEDs...** lets you assign each of the three knob LEDs (left/right/center) to mirror the on/off state of any button's current function — so, for example, an LED can show whether TUNE or TX-listening is currently active. The main knob light and keypad backlight share one shared on/off switch.

### Manual CW keying — paddle or straight key

TCISync can key CW directly through the E-Coder's paddle contacts. Right-click → **CW settings...** lets you choose:

- **Paddle** (default) — a full iambic keyer: squeeze both contacts for alternating dit/dah, single-lever operation, adjustable dot/dash/gap timing to match your own fist.
- **Straight** — for a plain hand key wired to the manipulator: the contact's own down-time *is* the character, shaped entirely by hand, no element timing involved.

Changing key type requires a restart (TCISync tells you so in the settings dialog).

**Why this matters over a remote link:** normally, keying CW into a remote radio means your sidetone comes back to you over the network — every dit and dah arrives with the round-trip delay of the link, which makes hand-sending genuinely difficult to judge. TCISync generates its sidetone **entirely locally**, with a continuously-running audio stream and zero network path — you hear your own fist exactly as you send it, completely independent of how far away the radio actually is or how much lag the link has. The actual CW element sent to the radio is timed and compensated separately (TCI's own jitter-compensation mechanism), so what you hear locally and what gets transmitted stay correct even though they're generated in two different places.

### Compact / Classic UI, always-on-top

Right-click → **View...** lets you switch between **Compact** (the default borderless window, drag anywhere to move it, right-click to close) and **Classic** (a normal window with the OS title bar). This needs a restart to take effect. The same dialog has an "Always on top" checkbox; there's also a quick `^` button in the sync row that toggles it instantly, no restart needed.

## Troubleshooting checklist

1. ExpertSDR3 is running and the TCI indicator shows the server as active (left-click toggles it on/off).
2. You've connected to the radio at least once with TCI active — the profile file only reflects the *last* state ExpertSDR3 was in.
3. If you use both local and remote connections for the same radio, the *most recently used* one wins if there's ever a conflict (TCISync compares file modification times).
4. Check `%LOCALAPPDATA%\Expert Electronics\ExpertSDR3\` actually contains `.json` profile files matching `EED<digits>.json` or `remote_EED<digits>.json` — a `.backup_*`/`.corrupt*`/" - Copy" file is ignored on purpose.
5. Restart TCISync after changing anything in ExpertSDR3 — profiles are only read at startup.

73, Jonny — SM5K
