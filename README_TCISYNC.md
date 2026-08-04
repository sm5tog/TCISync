# ExpertSDR3 / TCI Setup — TCISync

This guide covers how to configure **ExpertSDR3** so TCISync can find and control your radio(s). It supplements the main [README](README.md).

## Förutsättningar

- ExpertSDR3 måste vara installerat och ha minst en fungerande, redan konfigurerad profil (anslutning till radion har gjorts minst en gång) — TCISync skapar inga profiler, bara läser befintliga.
- TCI-servern måste ha varit aktiverad i den profilen minst en gång.
- Vid fjärrstyrd radio (ERS): ERS-anslutningen måste redan fungera och ha använts minst en gång, så att `remote_<serial>.json` finns.
- Ljud måste vara konfigurerat att gå via PC:ns headset eller högtalare/mikrofon — radions egen ljudväg kan inte användas.
- En E-Coder krävs, och den måste vara avaktiverad i ExpertSDR3 (annars uppstår konflikt med TCISync).

## How it works

TCISync talks to ExpertSDR3 over **TCI** (Transceiver Control Interface, a websocket protocol built into ExpertSDR3). It does **not** need any IP/port typed in by hand — on every start it reads ExpertSDR3's own local profile files from:

```
%LOCALAPPDATA%\Expert Electronics\ExpertSDR3\*.json
```

There are two profile files per radio serial number: `<serial>.json` (local connection) and `remote_<serial>.json` (remote/ERS connection). A profile only counts as usable if its `Gui.TCI.open` flag is `true` — that's the same flag ExpertSDR3 itself sets when its TCI server is switched on. TCISync reads the TCI port from `Gui.TCI.port` and the receiver count from `GlobalProfile_.transceivers` in that same profile.

TCISync supports up to two radios at once: one local, one remote.

## Enabling the TCI server in ExpertSDR3

TCI must be switched on inside ExpertSDR3 itself before TCISync can connect — TCISync only reads the port ExpertSDR3 is already using, it can't turn TCI on for you.

From the ExpertSDR3 manual: the **TCI indicator** in the main window lets you configure and activate the server directly:

- **Right-click** the TCI indicator to open its settings — enter the TCI server port here (this is the same port TCISync will read automatically from the profile file).
- **Left-click** the TCI indicator to activate the TCI server.

Once TCI is active and a profile has been used at least once in that state, TCISync will find it automatically on its next start — no need to keep ExpertSDR3's settings screen open.

## Two radios (local + remote)

If you run one radio locally and connect to a second one remotely via ERS, TCISync auto-detects both from their respective profile files (`<serial>.json` for local, `remote_<serial>.json` for remote) and builds one panel per receiver automatically. A remote radio's receiver count already reflects any ERS limitation (e.g. a 2-receiver radio restricted to 1 receiver when used remotely) — this comes straight from ExpertSDR3's own profile, TCISync doesn't need to guess it.

## No profile found (first-time setup)

If TCISync can't find any ExpertSDR3 profile with `Gui.TCI.open == true`, it falls back to a generic placeholder station so the app still starts. Open ExpertSDR3, connect to your radio at least once with TCI switched on (see above), then restart TCISync.

## Troubleshooting checklist

1. ExpertSDR3 is running and the TCI indicator shows the server as active (left-click toggles it on/off).
2. You've connected to the radio at least once with TCI active — the profile file only reflects the *last* state ExpertSDR3 was in.
3. If you use both local and remote connections for the same radio, the *most recently used* one wins if there's ever a conflict (TCISync compares file modification times).
4. Check `%LOCALAPPDATA%\Expert Electronics\ExpertSDR3\` actually contains `.json` profile files matching `EED<digits>.json` or `remote_EED<digits>.json` — a `.backup_*`/`.corrupt*`/" - Copy" file is ignored on purpose.
5. Restart TCISync after changing anything in ExpertSDR3 — profiles are only read at startup.

73, Jonny — SM5TOG
