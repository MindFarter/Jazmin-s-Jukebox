"""
Render Jazmin's JukeBox UI frames off-target (no Pi hardware) for visual QA.
Produces PNG screenshots of each screen state into sim/out/.
"""

import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "jukebox"))

PREVIEW_CONFIG_DIR = tempfile.TemporaryDirectory(
    prefix="jazmin-jukebox-preview-")
PREVIEW_CONFIG = os.path.join(PREVIEW_CONFIG_DIR.name, "jukebox.json")
os.environ["JUKEBOX_CONFIG"] = PREVIEW_CONFIG

import jukebox as jb

with open(os.path.join(ROOT, "radio", "jukebox.json")) as f:
    _cfg = json.load(f)
_cfg["preset_file"] = os.path.join(ROOT, "radio", "radio_presets.json")
_cfg["gif_path"] = os.path.join(ROOT, "jukebox", "assets", "JJukebox.gif")
with open(PREVIEW_CONFIG, "w") as f:
    json.dump(_cfg, f, indent=2)
jb.CONFIG_PATH = PREVIEW_CONFIG

OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)


class MockDisplay:
    def __init__(self):
        self.n = 0
        self.frame = None

    def blit(self, image):
        self.n += 1
        self.frame = image
        return image

    def set_backlight(self, on):
        pass

    def close(self):
        pass


class MockTouch:
    def __init__(self):
        self.events = []

    def start(self):
        pass

    def stop(self):
        pass

    def get_events(self):
        return []

    def seconds_since_touch(self):
        return 0


class MockMopidy:
    def __init__(self, state):
        self.state = state

    def get_state(self):
        return self.state

    def call(self, method, **params):
        return None

    def toggle(self):
        pass

    def next(self):
        pass

    def previous(self):
        pass

    def volume_step(self, d):
        pass

    def play_uris(self, uris):
        pass

    def library_shuffle_all(self):
        pass

    def get_playlists(self):
        return []


def make_app(state, screen="player", active_station=-1):
    app = jb.JukeBoxApp(sim_display=MockDisplay(), sim_touch=MockTouch())
    app.mopidy = MockMopidy(state)
    app.state = state
    app.screen = screen
    app.active_station = active_station
    app.undervoltage = False
    return app


RADIO_STATE = {
    "connected": True, "playback": "playing", "volume": 62, "muted": False,
    "random": False, "title": "SomaFM Groove Salad", "subtitle": "Blissed-out beats and world music",
    "uri": "https://ice2.somafm.com/groovesalad-128-mp3",
    "source": "radio", "position_ms": 0, "length_ms": 0,
}
LIB_STATE = {
    "connected": True, "playback": "playing", "volume": 45, "muted": False,
    "random": True, "title": "Addicted to Music Rock", "subtitle": "Jan Satorras",
    "uri": "file:///mnt/screen-sd/Jazz.mp3",
    "source": "library", "position_ms": 88000, "length_ms": 195000,
}
PAUSED_STATE = dict(LIB_STATE, playback="paused")

def build_frames():
    """Returns list of (name, app) after rendering each screen state."""
    frames = []

    app = make_app(RADIO_STATE, active_station=0)
    app.render(); frames.append(("player_radio", app))

    app = make_app(LIB_STATE)
    app.render(); frames.append(("player_library", app))

    app = make_app(PAUSED_STATE)
    app.render(); frames.append(("player_paused", app))

    app = make_app(RADIO_STATE, screen="settings", active_station=3)
    app.render(); frames.append(("settings", app))

    app = make_app(RADIO_STATE, active_station=1)
    app.popup = ("volume", None, time.monotonic() + 5)
    app.render(); frames.append(("volume_popup", app))

    app = make_app(RADIO_STATE, screen="settings")
    app.popup = ("confirm-poweroff", None, time.monotonic() + 5)
    app.render(); frames.append(("poweroff_popup", app))

    app = make_app(LIB_STATE, screen="splash")
    app.splash_until = time.monotonic() + 5
    app.render(); frames.append(("splash", app))
    return frames


if __name__ == "__main__":
    from PIL import Image

    frames = build_frames()

    # sim/out/*.png      -> true 320x240 output (exactly what the panel shows)
    # sim/out/2x/*.png  -> 640x480 zoom previews (the same design zoomed 2x,
    #                      so it can be judged comfortably on a large monitor)
    OUT2X = os.path.join(OUT, "2x")
    os.makedirs(OUT2X, exist_ok=True)
    for name, app in frames:
        app.display.frame.save(os.path.join(OUT, f"{name}.png"))
        if app.scale > 1:
            app.image.save(os.path.join(OUT2X, f"{name}.png"))
        print("rendered", name)

    sheet = Image.new("RGB", (320 * 4 + 40, 240 * 2 + 30), (40, 40, 40))
    for i, (name, app) in enumerate(frames):
        x = 10 + (i % 4) * 330
        y = 10 + (i // 4) * 250
        sheet.paste(app.display.frame, (x, y))
    sheet.save(os.path.join(OUT, "contact_sheet.png"))
    print("contact sheet written")
