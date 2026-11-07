"""
Album art lookup for Jazmin's JukeBox.

Order of preference:
1. Mopidy library images (folder cover art indexed by Mopidy-Local)
2. Embedded ID3 art read directly from the file (mutagen)
3. cover.jpg / folder.jpg / albumart.jpg next to the file

Returns a PIL image sized for the art box (92x92), or None so the UI
falls back to the JJukebox.gif vinyl animation.
"""

import os
import urllib.parse

from PIL import Image

try:
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

CACHE_DIR = "/tmp/jukebox-art"
COVER_NAMES = ("cover.jpg", "cover.png", "folder.jpg", "folder.png",
               "albumart.jpg", "front.jpg", "art.jpg")


class AlbumArt:
    def __init__(self, mopidy, music_dir, size=92):
        self.mopidy = mopidy
        self.music_dir = music_dir
        self.size = size
        self._last_key = None
        self._last_result = None
        os.makedirs(CACHE_DIR, exist_ok=True)

    def get(self, state):
        """Return art image for the current track, or None."""
        uri = state.get("uri")
        if not uri:
            return None
        key = uri
        if key == self._last_key:
            return self._last_result
        result = self._lookup(uri, key)
        self._last_key = key
        self._last_result = result
        return result

    def _lookup(self, uri, key):
        # Mopidy's indexed images (folder art via Mopidy-Local)
        try:
            imgs = self.mopidy.call("core.library.get_images", uris=[uri]) or {}
            for image_list in imgs.values():
                for img in image_list:
                    u = img.get("uri", "")
                    if u.startswith("file://"):
                        path = urllib.parse.unquote(u[7:])
                        if os.path.exists(path):
                            return self._load(path, key)
        except Exception:
            pass

        # embedded / folder art from the file itself
        path = self._uri_to_path(uri)
        if path and os.path.exists(path):
            art = self._from_file(path, key)
            if art is not None:
                return art
        return None

    def _uri_to_path(self, uri):
        if uri.startswith("file://"):
            return urllib.parse.unquote(uri[7:])
        return None

    def _from_file(self, path, key):
        # embedded tags
        if HAS_MUTAGEN:
            try:
                mf = MutagenFile(path)
                if mf is not None and getattr(mf, "tags", None):
                    for tag in ("APIC:", "covr"):
                        if tag in mf.tags:
                            data = mf.tags[tag]
                            if isinstance(data, list):
                                data = data[0]
                            raw = getattr(data, "data", None) or data
                            if isinstance(raw, bytes) and raw[:3] == b"\xff\xd8\xff":
                                return self._from_bytes(raw, key)
                            if isinstance(raw, bytes):
                                return self._from_bytes(raw, key)
            except Exception:
                pass
        # sibling cover images
        folder = os.path.dirname(path)
        for name in COVER_NAMES:
            p = os.path.join(folder, name)
            if os.path.exists(p):
                return self._load(p, key)
        return None

    def _load(self, path, key):
        try:
            with open(path, "rb") as f:
                return self._from_bytes(f.read(), key)
        except OSError:
            return None

    def _from_bytes(self, data, key):
        import io
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            return None
        return self._frame(img)

    def _frame(self, img):
        # center-crop to square, resize to art box
        w, h = img.size
        s = min(w, h)
        img = img.crop(((w - s) // 2, (h - s) // 2,
                        (w - s) // 2 + s, (h - s) // 2 + s))
        return img.resize((self.size, self.size), Image.LANCZOS)
