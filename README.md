# TrickLight

Small Windows tray app to control a **Trick-xxxx** BLE LED dimmer (the light normally used with the *TrickLight* phone app, `com.qh.onehlight`) from a PC — no phone needed.

## Features
- Turns the light **on** (at last brightness, 10% by default) when Windows starts / you log in
- Turns the light **off** on shutdown, logoff and sleep; back on after resume
- Tray icon: left-click toggles on/off
- Menu: presets 100/75/50/25/10/5%, brightness slider, exit (exit leaves the light as is)
- Auto-reconnect, single instance, log in `tricklight.log`, settings in `tricklight.json`

## Requirements
- Windows 10/11 with a Bluetooth 4.0+ (BLE) adapter
- Python 3.12 (`py -3.12`)

## Install
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```
Installs `bleak`, `pystray`, `pillow`, adds a shortcut to the Startup folder and starts the app.

Remove with `uninstall.ps1`.

Notes:
- Do **not** pair the light in Windows Bluetooth settings; the app connects directly.
- Close the phone app — the light accepts only one connection at a time.

## CLI
`tricklight.py` is a minimal command-line tool for testing:
```
py -3.12 tricklight.py scan
py -3.12 tricklight.py on
py -3.12 tricklight.py off
py -3.12 tricklight.py brightness 10
py -3.12 tricklight.py status
```

## Protocol
Plain BLE GATT, no pairing, no encryption (Triones-style controller). Device name matches `^Trick-` or `^Trick#`.

| | UUID |
|---|---|
| Write service | `0000ffd5-0000-1000-8000-00805f9b34fb` |
| Write characteristic | `0000ffd9-0000-1000-8000-00805f9b34fb` |
| Notify service | `0000ffd0-0000-1000-8000-00805f9b34fb` |
| Notify characteristic | `0000ffd4-0000-1000-8000-00805f9b34fb` |

| Command | Bytes |
|---|---|
| On | `CC 23 33` |
| Off | `CC 24 33` |
| Brightness | `56 VV 00 00 00 F0 AA` (`VV = pct * 2.55`, 10% = `19`) |
| Status query | `EF 01 77` |

Status reply starts with `66`; byte 2 is `23` (on) or `24` (off); byte 6 is the brightness value.

## License
MIT
