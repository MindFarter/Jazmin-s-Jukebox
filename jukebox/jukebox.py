"""
Jazmin's JukeBox - main touchscreen application (v2, landscape design).

Matches the user's design: dark background, orange outlines, green text,
vinyl-record splash with the JJukebox.gif animation, and a settings page
with source selection (MP3 from the screen's SD card / Internet Radio).

Screens:
  SPLASH    JJukebox.gif on an amber gradient, 3 seconds at boot
  PLAYER    status bar + now-playing card + 6 round buttons + progress bar
  SETTINGS  icon grid (Music/Radio/Mute/WiFi/BT/Clock/Power/Battery/Back)
            + scrollable station list + MP3 entry

Runs headless on the 320x240 (landscape) SPI display. Talks to Mopidy.
"""

import json
import os
import subprocess
import sys
import time
import threading
import urllib.parse

from PIL import Image, ImageDraw, ImageFont, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from display import ST7789
from touch import CST816D
from mopidy_client import MopidyClient, MopidyError
from albumart import AlbumArt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(BASE_DIR, "assets")
CONFIG_PATH = os.environ.get("JUKEBOX_CONFIG", "/etc/jukebox/jukebox.json")

SCREEN_W = 320   # landscape
SCREEN_H = 240
SPLASH_GIF_SIZE = 176


class ScaledDraw:
    """Anti-aliasing wrapper around ImageDraw.

    The UI is described in 320x240 coordinates but drawn onto a
    SCALE-times larger image (default 640x480), then downsampled to
    320x240 with Lanczos. Every circle, line and rounded rectangle
    comes out smooth instead of jagged, on the real panel as well as
    in screenshots.
    """

    def __init__(self, image, scale=2):
        self._d = ImageDraw.Draw(image)
        self._s = scale
        self._fonts = {}

    def _sc(self, v):
        if isinstance(v, (int, float)):
            return v * self._s
        if isinstance(v, (list, tuple)):
            return type(v)(self._sc(x) for x in v)
        return v

    def _font(self, font):
        """Return the same font at SCALE-times size."""
        path = getattr(font, "path", None)
        size = getattr(font, "size", None)
        if not path or not size:
            return font
        key = (path, size)
        if key not in self._fonts:
            try:
                self._fonts[key] = ImageFont.truetype(path, int(size * self._s))
            except OSError:
                self._fonts[key] = font
        return self._fonts[key]

    def text(self, xy, text, font=None, fill=None, anchor=None):
        self._d.text(self._sc(xy), text, font=self._font(font),
                     fill=fill, anchor=anchor)

    def textlength(self, text, font=None):
        return self._d.textlength(text, font=self._font(font)) / self._s

    def __getattr__(self, name):
        target = getattr(self._d, name)
        if not callable(target):
            return target

        def wrapper(*args, **kwargs):
            args = [self._sc(a) if isinstance(a, (list, tuple)) else a
                    for a in args]
            if isinstance(kwargs.get("width"), (int, float)):
                kwargs["width"] = max(1, int(kwargs["width"] * self._s))
            return target(*args, **kwargs)
        return wrapper

DEFAULT_CONFIG = {
    "name": "Jazmin's JukeBox",
    "preset_file": "/etc/jukebox/radio_presets.json",
    "mopidy_url": "http://127.0.0.1:6680/mopidy/rpc",
    "splash_seconds": 3.0,
    "gif_path": "/opt/jukebox/assets/JJukebox.gif",
    "music_dir": "/mnt/screen-sd",
    "dim_after_seconds": 60,
    "display": {
        "rotation": 90,
        "pins": {"dc": 17, "rst": 4, "bl": 12},
        "spi": {"bus": 0, "device": 0, "speed_hz": 40000000},
    },
    "touch": {"swap_xy": True, "flip_x": False, "flip_y": False},
    "colors": {
        "bg": (6, 6, 6),
        "orange": (255, 149, 40),
        "amber": (255, 190, 60),
        "amber_dark": (110, 72, 22),
        "green": (0, 220, 100),
        "green_dim": (0, 130, 70),
        "red": (255, 80, 60),
        "cyan": (60, 220, 200),
        "blue": (70, 150, 255),
        "white": (240, 240, 235),
        "grey": (150, 150, 150),
    },
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH) as f:
            user_cfg = json.load(f)
        for k, v in user_cfg.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        pass
    except ValueError as e:
        print(f"[jukebox] warning: bad config {CONFIG_PATH}: {e}")
    cfg["colors"] = {k: tuple(v) if isinstance(v, list) else v
                    for k, v in cfg.get("colors", {}).items()}
    return cfg


def load_presets(path):
    try:
        with open(path) as f:
            data = json.load(f)
        stations = data.get("stations", []) if isinstance(data, dict) else data
        return [s for s in stations if isinstance(s, dict) and s.get("uri")]
    except (FileNotFoundError, ValueError):
        pass
    return []


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

class Fonts:
    def __init__(self):
        d = os.path.join(BASE_DIR, "fonts")
        self.header = self._load(os.path.join(d, "Righteous-Regular.ttf"), 17)
        self.header_small = self._load(os.path.join(d, "Righteous-Regular.ttf"), 13)
        self.text = self._load(os.path.join(d, "DejaVuSans-Bold.ttf"), 14)
        self.small = self._load(os.path.join(d, "DejaVuSans-Bold.ttf"), 11)
        self.tiny = self._load(os.path.join(d, "DejaVuSans-Bold.ttf"), 9)

    def _load(self, path, size):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        except OSError:
            return ImageFont.load_default()


# ---------------------------------------------------------------------------
# GIF player (the JJukebox vinyl animation)
# ---------------------------------------------------------------------------

class GifPlayer:
    """Loads the GIF once and serves pre-scaled frames."""

    def __init__(self, path, size):
        self.frames = []
        self.durations = []
        try:
            im = Image.open(path)
            n = getattr(im, "n_frames", 1)
            for i in range(n):
                im.seek(i)
                frame = im.convert("RGB").resize(
                    (size, size), Image.LANCZOS)
                self.frames.append(frame)
                self.durations.append((im.info.get("duration") or 200) / 1000.0)
        except Exception as e:
            print(f"[jukebox] gif load failed: {e}")
            self.frames = [Image.new("RGB", (size, size), (10, 6, 2))]
            self.durations = [0.2]
        self._idx = 0
        self._t = time.monotonic()

    def frame(self):
        if len(self.frames) <= 1:
            return self.frames[0]
        now = time.monotonic()
        if now - self._t >= self.durations[self._idx]:
            self._t = now
            self._idx = (self._idx + 1) % len(self.frames)
        return self.frames[self._idx]


# ---------------------------------------------------------------------------
# Small icon drawing helpers (all PIL primitives, matching the design)
# ---------------------------------------------------------------------------

def icon_prev(d, cx, cy, c):
    d.rectangle([cx - 9, cy - 7, cx - 7, cy + 7], fill=c)
    d.polygon([(cx + 9, cy - 9), (cx + 9, cy + 9), (cx - 3, cy)], fill=c)


def icon_next(d, cx, cy, c):
    d.polygon([(cx - 9, cy - 9), (cx - 9, cy + 9), (cx + 3, cy)], fill=c)
    d.rectangle([cx + 7, cy - 7, cx + 9, cy + 7], fill=c)


def icon_play(d, cx, cy, c):
    d.polygon([(cx - 6, cy - 9), (cx - 6, cy + 9), (cx + 10, cy)], fill=c)


def icon_pause(d, cx, cy, c):
    d.rectangle([cx - 8, cy - 9, cx - 2, cy + 9], fill=c)
    d.rectangle([cx + 2, cy - 9, cx + 8, cy + 9], fill=c)


def icon_stop(d, cx, cy, c):
    d.rectangle([cx - 7, cy - 7, cx + 7, cy + 7], fill=c)


def icon_loop(d, cx, cy, c):
    """Repeat symbol: two arcs with tangent arrowheads (as in the mockup)."""
    import math
    d.arc([cx - 8, cy - 8, cx + 8, cy + 8], start=-160, end=-20,
          fill=c, width=2)
    d.arc([cx - 8, cy - 8, cx + 8, cy + 8], start=20, end=160,
          fill=c, width=2)

    def arrow(theta_deg):
        th = math.radians(theta_deg)
        ex, ey = cx + 8 * math.cos(th), cy + 8 * math.sin(th)
        tx, ty = -math.sin(th), math.cos(th)     # travel direction at arc end
        px, py = -ty, tx                         # perpendicular
        tip = (ex + tx * 3, ey + ty * 3)
        b = (ex - tx * 2, ey - ty * 2)
        return [tip, (b[0] + px * 3, b[1] + py * 3),
                (b[0] - px * 3, b[1] - py * 3)]

    d.polygon(arrow(-20), fill=c)     # top arc, right end
    d.polygon(arrow(160), fill=c)     # bottom arc, left end


def icon_gear(d, cx, cy, c):
    import math
    d.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], outline=c, width=2)
    d.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill=c)
    for a in range(0, 360, 45):
        rad = math.radians(a)
        x1, y1 = cx + 7 * math.cos(rad), cy + 7 * math.sin(rad)
        x2, y2 = cx + 10 * math.cos(rad), cy + 10 * math.sin(rad)
        d.line([x1, y1, x2, y2], fill=c, width=2)


def icon_music(d, cx, cy, c):
    # two beamed eighth notes, centred on (cx, cy)
    d.ellipse([cx - 8, cy + 1, cx - 2, cy + 7], fill=c)
    d.ellipse([cx + 2, cy + 1, cx + 8, cy + 7], fill=c)
    d.rectangle([cx - 2, cy - 8, cx - 1, cy + 4], fill=c)
    d.rectangle([cx + 7, cy - 8, cx + 8, cy + 4], fill=c)
    d.rectangle([cx - 2, cy - 8, cx + 8, cy - 7], fill=c)


def icon_radio(d, cx, cy, c):
    d.rounded_rectangle([cx - 9, cy - 4, cx + 9, cy + 8], radius=2,
                        outline=c, width=2)
    d.line([cx - 5, cy - 4, cx + 2, cy - 9], fill=c, width=2)
    d.ellipse([cx - 5, cy - 1, cx, cy + 4], outline=c, width=2)
    d.line([cx + 3, cy - 1, cx + 6, cy - 1], fill=c, width=2)
    d.line([cx + 3, cy + 2, cx + 6, cy + 2], fill=c, width=2)


def icon_speaker(d, cx, cy, c):
    d.polygon([(cx - 9, cy - 3), (cx - 4, cy - 3), (cx + 1, cy - 8),
               (cx + 1, cy + 8), (cx - 4, cy + 3), (cx - 9, cy + 3)], fill=c)
    d.arc([cx + 2, cy - 5, cx + 10, cy + 5], start=-50, end=50, fill=c, width=2)


def icon_mute(d, cx, cy, c):
    d.polygon([(cx - 9, cy - 3), (cx - 4, cy - 3), (cx + 1, cy - 8),
               (cx + 1, cy + 8), (cx - 4, cy + 3), (cx - 9, cy + 3)], fill=c)
    d.line([cx + 5, cy - 5, cx + 11, cy + 5], fill=c, width=2)
    d.line([cx + 11, cy - 5, cx + 5, cy + 5], fill=c, width=2)


def icon_wifi(d, cx, cy, c, strength=3):
    d.ellipse([cx - 2, cy + 3, cx + 2, cy + 7], fill=c)
    for i in range(strength):
        r = 4 + i * 4
        d.arc([cx - r, cy + 4 - r, cx + r, cy + 4 + r], start=225, end=315,
              fill=c, width=2)


def icon_bt(d, cx, cy, c):
    # Bluetooth rune: spine + zigzag whose middle strokes cross the spine
    sx = cx - 4
    d.line([(sx, cy - 8), (sx, cy + 8)], fill=c, width=2)
    d.line([(sx, cy - 8), (cx + 4, cy - 4)], fill=c, width=2)
    d.line([(cx + 4, cy - 4), (cx - 6, cy)], fill=c, width=2)
    d.line([(cx - 6, cy), (cx + 4, cy + 4)], fill=c, width=2)
    d.line([(cx + 4, cy + 4), (sx, cy + 8)], fill=c, width=2)


def icon_battery(d, cx, cy, c, pct=0):
    d.rounded_rectangle([cx - 10, cy - 5, cx + 8, cy + 5], radius=2,
                        outline=c, width=2)
    d.rectangle([cx + 9, cy - 2, cx + 11, cy + 2], fill=c)
    w = int(16 * pct / 100)
    if w > 0:
        d.rectangle([cx - 8, cy - 3, cx - 8 + w, cy + 3], fill=c)


def icon_power(d, cx, cy, c):
    d.arc([cx - 8, cy - 8, cx + 8, cy + 8], start=-60, end=240,
          fill=c, width=2)
    d.line([cx, cy - 10, cx, cy + 1], fill=c, width=2)


def icon_clock(d, cx, cy, c):
    d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], outline=c, width=2)
    d.line([cx, cy, cx, cy - 5], fill=c, width=2)
    d.line([cx, cy, cx + 4, cy + 2], fill=c, width=2)


def icon_back(d, cx, cy, c):
    d.line([cx + 5, cy, cx - 8, cy], fill=c, width=2)
    d.line([cx - 8, cy, cx - 1, cy - 7], fill=c, width=2)
    d.line([cx - 8, cy, cx - 1, cy + 7], fill=c, width=2)


# ---------------------------------------------------------------------------
# Button geometry
# ---------------------------------------------------------------------------

def hit(x, y, box):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def circle_hit(x, y, cx, cy, r=26):
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


BTN_Y = 178          # player transport row
BTN_XS = [30, 82, 134, 186, 238, 290]
(BTN_PREV, BTN_PLAY, BTN_STOP, BTN_NEXT, BTN_SHUF, BTN_SET) = range(6)

ART_BOX = (16, 38, 112, 134)      # album art / GIF
CARD = (8, 30, 312, 146)
PROG = (12, 210, 308, 228)

GRID_X = [32, 80, 128]           # settings icon grid (fills the left column)
GRID_Y = [78, 134, 190]
# (cx, cy) index order: Music Radio Mute / WiFi BT Clock / Power Battery Back
STATION_PANEL = (156, 8, 314, 232)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class JukeBoxApp:
    def __init__(self, sim_display=None, sim_touch=None):
        self.cfg = load_config()
        self.fonts = Fonts()
        preset_file = self.cfg.get("preset_file", DEFAULT_CONFIG["preset_file"])
        if not os.path.isfile(preset_file):
            fallback = DEFAULT_CONFIG["preset_file"]
            if os.path.isfile(fallback):
                print(f"[jukebox] preset file {preset_file} not found; "
                      f"using {fallback}")
                preset_file = fallback
        self.presets = load_presets(preset_file)
        self.state = {"connected": False, "playback": "stopped", "volume": 0,
                      "title": "", "subtitle": "", "source": "",
                      "position_ms": 0, "length_ms": 0, "muted": False,
                      "random": False}
        self.screen = "splash"
        self.splash_until = time.monotonic() + self.cfg.get("splash_seconds", 3.0)
        self.active_station = -1
        self.station_scroll = 0
        self.popup = None            # (kind, data, until)
        self.eq_phase = 0.0
        self.wifi_up = False
        self.undervoltage = None
        self._undervoltage_warning_logged = False
        self._dimmed = False
        self._last_activity = time.monotonic()
        self._stop = False

        self.sim = sim_display is not None

        # display (landscape)
        if sim_display is not None:
            self.display = sim_display
        else:
            dcfg = self.cfg["display"]
            self.display = ST7789(
                width=240, height=320,
                rotation=dcfg.get("rotation", 90),
                spi_bus=dcfg.get("spi", {}).get("bus", 0),
                spi_device=dcfg.get("spi", {}).get("device", 0),
                spi_speed_hz=dcfg.get("spi", {}).get("speed_hz", 40000000),
                pin_dc=dcfg.get("pins", {}).get("dc", 17),
                pin_rst=dcfg.get("pins", {}).get("rst", 4),
                pin_bl=dcfg.get("pins", {}).get("bl", 12))

        if sim_touch is not None:
            self.touch = sim_touch
        else:
            tcfg = self.cfg.get("touch", {})
            self.touch = CST816D(width=SCREEN_W, height=SCREEN_H,
                                 swap_xy=tcfg.get("swap_xy", True),
                                 flip_x=tcfg.get("flip_x", False),
                                 flip_y=tcfg.get("flip_y", False))

        self.mopidy = MopidyClient(url=self.cfg.get(
            "mopidy_url", "http://127.0.0.1:6680/mopidy/rpc"))

        # render at 2x and downsample -> anti-aliased UI (see ScaledDraw)
        self.scale = max(1, int(self.cfg.get("render_scale", 2)))
        self.art = AlbumArt(self.mopidy, self.cfg.get("music_dir"),
                            size=92 * self.scale)

        gif_path = self.cfg.get(
            "gif_path", os.path.join(ASSETS, "JJukebox.gif"))
        if not os.path.isfile(gif_path):
            fallback = os.path.join(ASSETS, "JJukebox.gif")
            if os.path.isfile(fallback):
                print(f"[jukebox] GIF {gif_path} not found; using {fallback}")
                gif_path = fallback
        self.gif = GifPlayer(gif_path, SPLASH_GIF_SIZE * self.scale)

        self.image = Image.new("RGB", (SCREEN_W * self.scale,
                                       SCREEN_H * self.scale), (0, 0, 0))
        self.draw = ScaledDraw(self.image, self.scale)

    def _paste(self, img, xy):
        s = self.scale
        self.image.paste(img, (int(xy[0] * s), int(xy[1] * s)))

    # -- background services ---------------------------------------------------
    def _poll_loop(self):
        while not self._stop:
            try:
                self.state = self.mopidy.get_state()
            except Exception:
                self.state = {"connected": False, "playback": "stopped",
                              "volume": 0, "title": "", "subtitle": "",
                              "source": "", "position_ms": 0,
                              "length_ms": 0, "muted": False, "random": False}
            try:
                with open("/sys/class/net/wlan0/operstate") as f:
                    self.wifi_up = f.read().strip() == "up"
            except OSError:
                self.wifi_up = False
            self.undervoltage = self._read_undervoltage()
            time.sleep(1.0)

    def _read_undervoltage(self):
        try:
            result = subprocess.run(
                ["vcgencmd", "get_throttled"], check=True, capture_output=True,
                text=True, timeout=2)
            name, value = result.stdout.strip().split("=", 1)
            if name != "throttled":
                raise ValueError(f"unexpected vcgencmd output: {result.stdout!r}")
            throttled = int(value, 16)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            if not self._undervoltage_warning_logged:
                print(f"[jukebox] warning: cannot read Pi undervoltage status: {exc}")
                self._undervoltage_warning_logged = True
            return None
        self._undervoltage_warning_logged = False
        return bool(throttled & 0x1)

    # -- splash ------------------------------------------------------------------
    def draw_splash(self):
        # amber radial gradient (darker at edges and bottom) via numpy,
        # rendered at render_scale for a smooth result
        import numpy as np
        s = self.scale
        cx, cy = SCREEN_W / 2, SCREEN_H / 2 - 10
        ys, xs = np.mgrid[0:SCREEN_H * s, 0:SCREEN_W * s].astype("float32")
        dist = np.sqrt(((xs - cx * s) / (210.0 * s)) ** 2 +
                       ((ys - cy * s) / (190.0 * s)) ** 2)
        t = np.clip(dist, 0, 1)
        # center (150,100,34) -> edge (46,28,10); extra darkening at bottom
        r = 150 - 104 * t
        g = 100 - 70 * t
        b = 34 - 24 * t
        bottom = np.clip((ys - SCREEN_H * s * 0.55) / (SCREEN_H * s * 0.45), 0, 1)
        r = r * (1 - 0.25 * bottom)
        g = g * (1 - 0.25 * bottom)
        b = b * (1 - 0.25 * bottom)
        arr = np.stack([r, g, b], axis=-1).astype("uint8")
        bg = Image.fromarray(arr, "RGB")
        # Keep the larger splash artwork centered with a clear screen margin.
        gif_frame = self.gif.frame()
        mask = Image.new("L", gif_frame.size, 0)
        ImageDraw.Draw(mask).ellipse([0, 0, gif_frame.size[0] - 1,
                                     gif_frame.size[1] - 1], fill=255)
        x = (SCREEN_W - SPLASH_GIF_SIZE) // 2
        y = (SCREEN_H - SPLASH_GIF_SIZE) // 2
        bg.paste(gif_frame, (x * s, y * s), mask)
        self.image = bg
        self.draw = ScaledDraw(self.image, s)

    # -- player screen --------------------------------------------------------------
    def draw_player(self):
        c = self.cfg["colors"]
        d = self.draw
        d.rectangle([0, 0, SCREEN_W, SCREEN_H], fill=c["bg"])

        # --- status bar (ON = green, OFF = grey)
        grey, green = c["grey"], c["green"]
        icon_music(d, 14, 13, green if self.state.get("source") == "library" else grey)
        icon_radio(d, 40, 13, green if self.state.get("source") == "radio" else grey)
        if self.state.get("muted"):
            icon_mute(d, 66, 13, grey)
        else:
            icon_speaker(d, 66, 13, green)
        # clock
        try:
            clock = time.strftime("%I:%M %p").lstrip("0")
        except Exception:
            clock = ""
        d.text((SCREEN_W // 2, 13), clock, font=self.fonts.text,
               fill=c["amber"], anchor="mm")
        # right side
        icon_wifi(d, 236, 13, green if self.wifi_up else grey)
        icon_bt(d, 254, 13, grey)          # no Bluetooth in the jukebox -> off
        if self.undervoltage is True:
            power_label, power_color = "LOW", c["red"]
        elif self.undervoltage is False:
            power_label, power_color = "OK", green
        else:
            power_label, power_color = "--", grey
        d.text((292, 13), power_label, font=self.fonts.tiny, fill=power_color,
               anchor="rm")
        icon_battery(d, 307, 13, power_color, pct=0)
        d.line([0, 26, SCREEN_W, 26], fill=c["orange"], width=1)

        # --- now playing card
        d.rounded_rectangle(CARD, radius=10, outline=c["orange"], width=2,
                            fill=(10, 8, 6))
        # album art box (GIF when no art)
        d.rounded_rectangle(ART_BOX, radius=6, outline=c["orange"], width=2)
        art = self.art.get(self.state)
        if art is not None:
            self._paste(art, (ART_BOX[0] + 2, ART_BOX[1] + 2))
        else:
            gif_frame = self.gif.frame().resize(
                (92 * self.scale, 92 * self.scale), Image.LANCZOS)
            self._paste(gif_frame, (ART_BOX[0] + 2, ART_BOX[1] + 2))

        # header + track info
        src = self.state.get("source")
        header = "Now Playing:  ♫ SD Card" if src == "library" else \
                 "Now Playing:  Internet Radio"
        if not src:
            header = "Now Playing"
        d.text((122, 46), header, font=self.fonts.small, fill=c["orange"])
        title = self.state.get("title") or "Nothing playing"
        subtitle = self.state.get("subtitle") or ""
        if not self.state.get("connected"):
            title = "Starting up…"
            subtitle = "Waiting for music service"
        d.text((122, 68), self._fit(title, self.fonts.text, 182),
               font=self.fonts.text, fill=c["green"])
        d.text((122, 90), self._fit(subtitle, self.fonts.small, 182),
               font=self.fonts.small, fill=c["green_dim"])

        # waveform equaliser (baseline y=126 so the time fits below it)
        import math
        x0 = 122
        for i in range(24):
            bx = x0 + i * 8
            phase = self.eq_phase + i * 0.55
            hgt = 4 + 14 * (0.5 + 0.5 * math.sin(phase))
            if self.state.get("playback") != "playing":
                hgt = 3
            col = c["blue"] if i % 3 == 0 else (c["red"] if i % 3 == 1 else c["cyan"])
            d.rectangle([bx, 126 - hgt, bx + 5, 126], fill=col)
        self.eq_phase += 0.55

        # track time inside the card, below the analyser
        length = self.state.get("length_ms") or 0
        pos = self.state.get("position_ms") or 0
        mm, ss = divmod(int(pos / 1000), 60)
        tstr = f"{mm:02d}:{ss:02d}"
        if length > 0:
            tmm, tss = divmod(int(length / 1000), 60)
            tstr += f" / {tmm:02d}:{tss:02d}"
        d.text((217, 138), tstr, font=self.fonts.small, fill=c["green"],
               anchor="mm")

        # --- transport buttons: border always orange, icon green when ON
        for i, bx in enumerate(BTN_XS):
            cy = BTN_Y
            d.ellipse([bx - 23, cy - 23, bx + 23, cy + 23],
                      outline=c["orange"], width=2)
            if i == BTN_PLAY:
                if self.state.get("playback") == "playing":
                    icon_pause(d, bx, cy, c["green"])
                else:
                    icon_play(d, bx, cy, c["orange"])
            elif i == BTN_SHUF:
                if src == "radio":
                    col = c["grey"]      # not applicable to radio
                elif self.state.get("random"):
                    col = c["green"]
                else:
                    col = c["orange"]
                icon_loop(d, bx, cy, col)
            else:
                icon = [icon_prev, None, icon_stop, icon_next,
                        None, icon_gear][i]
                icon(d, bx, cy, c["orange"])

        # --- progress bar
        d.rounded_rectangle(PROG, radius=8, fill=c["amber_dark"])
        length = self.state.get("length_ms") or 0
        pos = self.state.get("position_ms") or 0
        if length > 0:
            frac = min(1.0, pos / length)
        elif src == "radio":
            frac = 1.0
        else:
            frac = 0.0
        if frac > 0:
            d.rounded_rectangle([PROG[0], PROG[1],
                                 PROG[0] + int((PROG[2] - PROG[0]) * frac),
                                 PROG[3]], radius=8, fill=c["amber"])
            hx = PROG[0] + int((PROG[2] - PROG[0]) * frac)
            hx = max(PROG[0] + 6, min(hx, PROG[2] - 6))
            d.ellipse([hx - 6, (PROG[1] + PROG[3]) // 2 - 6,
                       hx + 6, (PROG[1] + PROG[3]) // 2 + 6], fill=c["amber"])

    # -- settings screen ---------------------------------------------------------------
    GRID_ITEMS = [
        ("Music", "music"), ("Radio", "radio"), ("Mute", "mute"),
        ("WiFi", "wifi"), ("BT", "bt"), ("Clock", "clock"),
        ("Power", "power"), ("Battery", "battery"), ("Back", "back"),
    ]

    def draw_settings(self):
        c = self.cfg["colors"]
        d = self.draw
        d.rectangle([0, 0, SCREEN_W, SCREEN_H], fill=c["bg"])

        # header box
        d.rounded_rectangle([8, 6, 148, 44], radius=8, outline=c["orange"],
                            width=2, fill=(14, 10, 6))
        d.text((78, 17), "Jazmin's Jukebox", font=self.fonts.header_small,
               fill=c["orange"], anchor="mm")
        d.text((78, 34), "Settings", font=self.fonts.small, fill=c["amber"],
               anchor="mm")

        # 3x3 grid: border always orange, icon green when ON
        icons = {"music": icon_music, "radio": icon_radio, "mute": icon_mute,
                 "wifi": icon_wifi, "bt": icon_bt, "clock": icon_clock,
                 "power": icon_power, "battery": icon_battery, "back": icon_back}
        states = {
            "music": self.state.get("source") == "library",
            "radio": self.state.get("source") == "radio",
            "mute": self.state.get("muted"),
            "wifi": self.wifi_up,
            "bt": False,
            "battery": self.undervoltage is False,
        }
        for idx, (label, key) in enumerate(self.GRID_ITEMS):
            col, row = idx % 3, idx // 3
            cx, cy = GRID_X[col], GRID_Y[row]
            col_on = c["green"] if states.get(key) else c["orange"]
            if key == "battery":
                if self.undervoltage is True:
                    col_on = c["red"]
                elif self.undervoltage is None:
                    col_on = c["grey"]
            d.ellipse([cx - 19, cy - 19, cx + 19, cy + 19],
                      outline=c["orange"], width=2)
            icons[key](d, cx, cy, col_on)
            d.text((cx, cy + 30), label, font=self.fonts.small,
                   fill=c["green"], anchor="mm")

        # --- station list panel (4 visible stations + pinned MP3 row)
        d.rounded_rectangle(STATION_PANEL, radius=10, outline=c["orange"],
                            width=2, fill=(10, 8, 6))
        n_visible = 4
        max_scroll = max(0, len(self.presets) - n_visible)
        self.station_scroll = max(0, min(self.station_scroll, max_scroll))
        for i in range(n_visible):
            idx = i + self.station_scroll
            if idx >= len(self.presets):
                break
            st = self.presets[idx]
            y = 16 + i * 42
            active = (idx == self.active_station
                      and self.state.get("playback") == "playing")
            if active:
                d.rounded_rectangle([160, y - 4, 306, y + 34], radius=6,
                                   outline=c["amber"], width=1)
            d.text((164, y + 9), self._fit(st.get("name", ""), self.fonts.small, 138),
                   font=self.fonts.small, anchor="lm",
                   fill=c["amber"] if active else c["green"])
            d.text((164, y + 25), self._fit(st.get("genre", "radio"), self.fonts.tiny, 138),
                   font=self.fonts.tiny, anchor="lm", fill=c["green_dim"])
        # pinned MP3 entry at the bottom of the panel
        lib_active = (self.state.get("source") == "library"
                      and self.state.get("playback") == "playing")
        d.line([162, 182, 308, 182], fill=(50, 40, 24), width=1)
        if lib_active:
            d.rounded_rectangle([160, 186, 306, 228], radius=6,
                               outline=c["amber"], width=1)
        d.text((164, 200), "MP3 files", font=self.fonts.small, anchor="lm",
               fill=c["amber"] if lib_active else c["green"])
        d.text((164, 216), "Screen SD card", font=self.fonts.tiny, anchor="lm",
               fill=c["green_dim"])
        # scrollbar
        if max_scroll > 0:
            d.rectangle([310, 12, 312, 172], fill=(30, 24, 16))
            sb_h = 160 * n_visible / len(self.presets)
            sb_y = 12 + (160 - sb_h) * self.station_scroll / max_scroll
            d.rectangle([310, sb_y, 312, sb_y + sb_h], fill=c["orange"])

    # -- popups -----------------------------------------------------------------------
    def draw_popups(self):
        if not self.popup:
            return
        kind, data, until = self.popup
        if time.monotonic() > until:
            self.popup = None
            return
        c = self.cfg["colors"]
        d = self.draw
        if kind == "volume":
            d.rectangle([60, 100, 260, 150], fill=(8, 6, 4))
            d.rounded_rectangle([60, 100, 260, 150], radius=8,
                                outline=c["orange"], width=2)
            vol = self.state.get("volume", 0)
            d.text((160, 112), f"VOLUME {vol}%", font=self.fonts.small,
                   fill=c["amber"], anchor="mm")
            d.rectangle([76, 126, 244, 138], fill=(50, 40, 20))
            d.rectangle([76, 126, 76 + int(168 * vol / 100), 138],
                        fill=c["amber"])
        elif kind == "confirm-poweroff":
            d.rectangle([40, 70, 280, 170], fill=(8, 6, 4))
            d.rounded_rectangle([40, 70, 280, 170], radius=10,
                                outline=c["red"], width=2)
            d.text((160, 90), "Shut Down", font=self.fonts.text,
                   fill=c["red"], anchor="mm")
            d.text((160, 108), "Jazmin's JukeBox?", font=self.fonts.small,
                   fill=c["white"], anchor="mm")
            d.rounded_rectangle([56, 124, 136, 156], radius=6, outline=c["red"],
                                width=2)
            d.text((96, 140), "YES", font=self.fonts.small, fill=c["red"],
                   anchor="mm")
            d.rounded_rectangle([184, 124, 264, 156], radius=6,
                                outline=c["green"], width=2)
            d.text((224, 140), "NO", font=self.fonts.small, fill=c["green"],
                   anchor="mm")
        elif kind == "info":
            d.rectangle([40, 80, 280, 160], fill=(8, 6, 4))
            d.rounded_rectangle([40, 80, 280, 160], radius=8,
                                outline=c["orange"], width=2)
            d.text((160, 100), data.get("title", ""), font=self.fonts.small,
                   fill=c["amber"], anchor="mm")
            d.text((160, 122), data.get("body", ""), font=self.fonts.tiny,
                   fill=c["green"], anchor="mm")

    # -- helpers -----------------------------------------------------------------------
    def _fit(self, text, font, width):
        if not text:
            return ""
        if self.draw.textlength(text, font=font) <= width:
            return text
        while text and self.draw.textlength(text + "…", font=font) > width:
            text = text[:-1]
        return text + "…"

    def render(self):
        if self.screen == "splash":
            self.draw_splash()
            if time.monotonic() >= self.splash_until:
                self.screen = "player"
        elif self.screen == "player":
            self.draw_player()
        else:
            self.draw_settings()
        self.draw_popups()
        frame = self.image
        if self.scale > 1:
            frame = self.image.resize((SCREEN_W, SCREEN_H), Image.LANCZOS)
        self.display.blit(frame)

    # -- actions -----------------------------------------------------------------------
    def play_station(self, idx):
        if 0 <= idx < len(self.presets):
            st = self.presets[idx]
            print(f"[jukebox] station {idx + 1}: {st.get('name')}")
            self.mopidy.play_uris([st["uri"]])
            self.active_station = idx

    def play_sd_music(self):
        try:
            self.mopidy.library_shuffle_all()
        except MopidyError as e:
            self.popup = ("info", {"title": "MP3 — Screen SD card",
                                   "body": "No files found on the card."}, time.monotonic() + 2.5)

    # -- events -----------------------------------------------------------------------
    def handle_events(self, events):
        for ev in events:
            self._last_activity = time.monotonic()
            if self._dimmed:
                self.display.set_backlight(True)
                self._dimmed = False
            if self.screen == "splash":
                self.splash_until = 0  # any touch skips the splash
                continue
            if ev.kind == "swipe_up":
                if self.screen == "settings":
                    self.station_scroll = max(0, self.station_scroll - 1)
                else:
                    self.mopidy.volume_step(+10)
                    self._volume_popup()
                continue
            if ev.kind == "swipe_down":
                if self.screen == "settings":
                    self.station_scroll += 1
                else:
                    self.mopidy.volume_step(-10)
                    self._volume_popup()
                continue
            if ev.kind == "swipe_left" and self.screen == "player":
                self.mopidy.next(); continue
            if ev.kind == "swipe_right" and self.screen == "player":
                self.mopidy.previous(); continue
            if ev.kind == "double_tap" and self.screen == "player":
                self.mopidy.toggle(); continue
            if ev.kind in ("tap", "long_press"):
                self.on_tap(ev.x, ev.y)

    def _volume_popup(self):
        self.popup = ("volume", None, time.monotonic() + 1.5)

    def on_tap(self, x, y):
        # popups that swallow taps
        if self.popup:
            kind = self.popup[0]
            if kind == "confirm-poweroff":
                if hit(x, y, (56, 124, 136, 156)):
                    self.popup = ("info", {"title": "Shutting down…",
                                           "body": "It is safe to unplug when the light goes out."},
                                  time.monotonic() + 30)
                    subprocess.Popen(["sudo", "systemctl", "poweroff"])
                elif hit(x, y, (184, 124, 264, 156)):
                    self.popup = None
                return
            if kind in ("volume", "info"):
                self.popup = None
            return

        if self.screen == "player":
            self._on_tap_player(x, y)
        elif self.screen == "settings":
            self._on_tap_settings(x, y)

    def _on_tap_player(self, x, y):
        for i, bx in enumerate(BTN_XS):
            if circle_hit(x, y, bx, BTN_Y):
                if i == BTN_PREV:
                    self.mopidy.previous()
                elif i == BTN_PLAY:
                    self.mopidy.toggle()
                elif i == BTN_STOP:
                    self.mopidy.call("core.playback.stop")
                elif i == BTN_NEXT:
                    self.mopidy.next()
                elif i == BTN_SHUF:
                    try:
                        new = not self.state.get("random")
                        self.mopidy.call("core.tracklist.set_random", value=new)
                        self.state["random"] = new
                    except MopidyError:
                        pass
                elif i == BTN_SET:
                    self.screen = "settings"
                return
        # speaker icon = mute toggle
        if circle_hit(x, y, 58, 13, 14):
            try:
                muted = not self.state.get("muted")
                self.mopidy.call("core.mixer.set_mute", muted=muted)
                self.state["muted"] = muted
            except MopidyError:
                pass

    def _on_tap_settings(self, x, y):
        # grid buttons
        for idx, (label, key) in enumerate(self.GRID_ITEMS):
            col, row = idx % 3, idx // 3
            cx, cy = GRID_X[col], GRID_Y[row]
            if circle_hit(x, y, cx, cy, 24):
                self._grid_action(key)
                return
        # station list rows (4 visible, 42px each) + pinned MP3 entry
        if hit(x, y, STATION_PANEL):
            if y >= 182:                      # pinned MP3 entry
                self.play_sd_music()
                self.screen = "player"
                return
            rel_y = y - 12
            idx = self.station_scroll + rel_y // 42
            if rel_y >= 0 and idx < len(self.presets):
                self.play_station(idx)
                self.screen = "player"
        # anywhere outside grid/list: nothing

    def _grid_action(self, key):
        if key == "music":
            self.play_sd_music()
            self.screen = "player"
        elif key == "radio":
            if self.presets:
                self.play_station(0 if self.active_station < 0
                                  else self.active_station)
            self.screen = "player"
        elif key == "mute":
            try:
                muted = not self.state.get("muted")
                self.mopidy.call("core.mixer.set_mute", muted=muted)
                self.state["muted"] = muted
            except MopidyError:
                pass
        elif key == "wifi":
            self.popup = ("info", {"title": "Wi-Fi",
                                   "body": "connected" if self.wifi_up else "not connected"},
                          time.monotonic() + 2.0)
        elif key == "bt":
            self.popup = ("info", {"title": "Bluetooth",
                                   "body": "not used by the jukebox"},
                          time.monotonic() + 2.0)
        elif key == "clock":
            self.popup = ("info", {"title": time.strftime("%I:%M %p").lstrip("0"),
                                   "body": time.strftime("%a %d %b %Y")},
                          time.monotonic() + 2.5)
        elif key == "power":
            self.popup = ("confirm-poweroff", None, time.monotonic() + 15)
        elif key == "battery":
            if self.undervoltage is True:
                title, body = "Low Supply Warning", "Check battery and wiring"
            elif self.undervoltage is False:
                title, body = "Power Supply", "No low-voltage warning"
            else:
                title, body = "Power Monitor", "Pi status unavailable"
            self.popup = ("info", {"title": title, "body": body},
                          time.monotonic() + 2.0)
        elif key == "back":
            self.screen = "player"

    # -- dimming -----------------------------------------------------------------------
    def _handle_dimming(self):
        if not self.cfg.get("dim_after_seconds"):
            return
        idle = time.monotonic() - self._last_activity
        if not self._dimmed and idle > self.cfg["dim_after_seconds"]:
            self.display.set_backlight(False)
            self._dimmed = True

    # -- main loop -----------------------------------------------------------------------
    def run(self):
        print("[jukebox] Jazmin's JukeBox UI starting")
        self.touch.start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        last_render = 0.0
        try:
            while not self._stop:
                events = self.touch.get_events()
                if events:
                    self.handle_events(events)
                self._handle_dimming()
                now = time.monotonic()
                interval = 0.12 if not self._dimmed else 0.5
                if self.screen == "splash":
                    interval = 0.05
                if now - last_render >= interval:
                    self.render()
                    last_render = now
                time.sleep(0.02)
        except KeyboardInterrupt:
            pass
        finally:
            self.touch.stop()
            print("[jukebox] bye")

    def stop(self):
        self._stop = True


def main():
    app = JukeBoxApp()
    app.run()


if __name__ == "__main__":
    main()
