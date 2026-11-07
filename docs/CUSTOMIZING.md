# Customizing Jazmin's JukeBox

The whole point of this project is that it's yours. Here's where everything lives.

## Branding

`/etc/jukebox/jukebox.json`:

```json
{
  "name": "Jazmin's JukeBox",
  ...
}
```

- `"name"` is drawn on the arch of every screen. It's split into two lines at the last space ("Jazmin's" / "JUKEBOX"). Rename freely.
- Colours (`bg`, `arch`, `red`, `gold`, `cream`, `panel`, `ink`, `blue`) are RGB lists. The jukebox look comes from gold-on-dark-red with cream cards — try `red: [180, 30, 60]` and `gold: [255, 210, 90]` for a brighter diner look, or go full neon: `bg: [8, 2, 20]`, `gold: [0, 255, 180]`.
- `dim_after_seconds`: how long before the screen sleeps (0 = never).

After editing: `sudo systemctl restart jazmin-jukebox`.

## Radio presets

`/etc/jukebox/radio_presets.json` — or use the command:

```bash
sudo jukebox-station add 'Station Name' 'https://stream.example/stream.mp3' A8 'jazz'
```

Each station has a `name`, a `genre` (shown under the name on the settings screen), and a `uri`. The settings-screen list reads this file. There are deliberately **no BBC stations** — the preset list is international (SomaFM, Rock Antenne, Radio Paradise, KEXP). Add or remove stations with the command above or by editing the JSON; restart the UI afterwards (`sudo systemctl restart jazmin-jukebox`).

**Finding stream URLs**: in Iris open TuneIn, play a station, and the stream URI shows in the queue. Many stations publish direct MP3 streams (search "station name stream url mp3").

## The UI (jukebox.py)

All drawing happens in `/opt/jukebox/jukebox.py` with Pillow — no framework, no skin PNGs, everything coded in. Useful anchors:

- `SCREEN_W, SCREEN_H` — 320x240 landscape.
- `BTN_XS / BTN_Y` — the six round transport buttons on the player screen.
- `GRID_X / GRID_Y` and `GRID_ITEMS` — the settings icon grid.
- `draw_splash()` / `draw_player()` / `draw_settings()` — one method per screen.
- `draw_popups()` — volume, info and shutdown-confirmation overlays.
- `icon_*` functions — every icon is drawn with PIL primitives (no image files to match).
- `handle_events()` / `on_tap()` — where taps and gestures map to actions.

The splash GIF lives at `/opt/jukebox/assets/JJukebox.gif` and its boot duration is `splash_seconds` in `/etc/jukebox/jukebox.json` (0 disables the splash).

Fonts live in `/opt/jukebox/fonts/`. Drop any TTF in there and reference it in the `Fonts` class. Righteous (retro) and Press Start 2P are included under the SIL OFL license.

## Display orientation

The UI is landscape by default (`rotation: 90` + touch `swap_xy`). If the image or touch appears mirrored/upside-down on your panel, tweak `flip_x` / `flip_y` in `/etc/jukebox/jukebox.json`, or use `rotation: 270` instead of 90.

## Extra music storage

The screen's own SD card (mounted at /mnt/screen-sd via the jazmin-sd overlay) is the default library. To also allow a USB stick, list it in `/etc/mopidy/mopidy.conf`:

```ini
[file]
media_dirs =
    /mnt/screen-sd|Screen SD
    /var/lib/mopidy/music|Music
    /mnt/usb|USB
```

Then `sudo systemctl restart mopidy && sudo jukebox-scan`.

## Wi-Fi and a standalone hotspot

Normal setup uses your home Wi-Fi (set in Raspberry Pi Imager). For a fully portable jukebox, you can add a fallback hotspot so phones can connect directly to the Pi anywhere:

```bash
sudo nmcli dev wifi hotspot ifname wlan0 ssid JazminsJukebox password "YOUR-UNIQUE-PASSWORD"
```

NetworkManager on Trixie makes this a one-liner; the hotspot shares the Pi's connection. Phones then open `http://10.42.0.1:6680/iris`. Note the hotspot has no internet, so radio won't work while in hotspot mode — local music will.

## Distributing your version

1. Put your modified files in a folder (the whole `jazmins-jukebox` folder *is* your fork).
2. Others install exactly like the README quick start: flash OS Lite, copy the folder, `sudo bash install.sh`.
3. For a true "flash and go" image, either clone a configured card with `dd`, or try `scripts/make-image.sh` (experimental pi-gen build).

Remember to strip your Wi-Fi credentials from a distributed image (`sudo nmcli connection delete <name>` before cloning). Mopidy's Iris page and the guest-writable Samba share are available to devices on the local network without authentication. Use a trusted, password-protected Wi-Fi network; do not configure router port forwarding for Mopidy (port 6680) or Samba (ports 137–139 and 445). A Wi-Fi hotspot password must be unique and strong; never use a shared example password.

## Project ideas

- OLED VU meter on a second SPI device
- Rotary encoder as volume knob (spare GPIOs: 5, 6, 13, 16, 19, 20, 21)
- A "credits" animation when a preset is chosen
- Spotify/YouTube Music via extra Mopidy extensions (they need accounts — deliberately not included)
