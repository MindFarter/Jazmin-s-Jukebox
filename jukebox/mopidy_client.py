"""
Minimal Mopidy JSON-RPC client for Jazmin's JukeBox.

Talks to Mopidy's HTTP extension over localhost (default
http://127.0.0.1:6680/mopidy/rpc) using simple JSON-RPC 2.0 calls.
"""

import json
import random
import threading

try:
    import urllib.request as _urlreq
except ImportError:  # pragma: no cover
    import urllib2 as _urlreq  # type: ignore


class MopidyError(Exception):
    pass


class MopidyClient:
    def __init__(self, url="http://127.0.0.1:6680/mopidy/rpc", timeout=5.0):
        self.url = url
        self.timeout = timeout
        self._id = 0
        self._id_lock = threading.Lock()

    def call(self, method, **params):
        with self._id_lock:
            self._id += 1
            rpc_id = self._id
        payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params:
            payload["params"] = params
        body = json.dumps(payload).encode("utf-8")
        req = _urlreq.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json"})
        try:
            with _urlreq.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise MopidyError(f"RPC {method} failed: {exc}") from exc
        if "error" in data:
            raise MopidyError(f"RPC {method}: {data['error'].get('message')}")
        return data.get("result")

    # -- convenience wrappers -------------------------------------------------
    def get_state(self):
        """One snapshot of everything the UI shows."""
        state = {
            "connected": False,
            "playback": "stopped",      # playing | paused | stopped
            "volume": 0,
            "muted": False,
            "random": False,
            "title": "",
            "subtitle": "",
            "uri": None,
            "source": "",              # radio | library | playlist | ""
            "position_ms": 0,
            "length_ms": 0,
            "tracklist_len": 0,
        }
        try:
            pb = self.call("core.playback.get_state")
            state["playback"] = pb
            state["connected"] = True
        except MopidyError:
            return state

        try:
            state["volume"] = self.call("core.mixer.get_volume") or 0
        except MopidyError:
            pass
        try:
            state["muted"] = bool(self.call("core.mixer.get_mute"))
        except MopidyError:
            pass
        try:
            state["random"] = bool(self.call("core.tracklist.get_random"))
        except MopidyError:
            pass

        track = None
        try:
            track = self.call("core.playback.get_current_track")
        except MopidyError:
            pass

        title, subtitle, source = "", "", ""
        if track is not None:
            name = track.get("name") or ""
            artists = ", ".join(a.get("name", "") for a in track.get("artists", []) if a.get("name"))
            uri = track.get("uri", "")
            state["uri"] = uri

            if uri.startswith("tunein:"):
                source = "radio"
                title = name
                try:
                    stream_title = self.call("core.playback.get_stream_title")
                    if stream_title:
                        title = name
                        subtitle = stream_title
                except MopidyError:
                    pass
            else:
                source = "library"
                title = name
                subtitle = artists

        # radio preset mode: URL streams (m3u/xspf/tunein) usually surface via
        # stream title; a direct stream URI is our radio case
        if track is not None:
            uri = track.get("uri", "")
            if uri.startswith("http"):
                source = "radio"
                try:
                    stream_title = self.call("core.playback.get_stream_title")
                except MopidyError:
                    stream_title = None
                if stream_title:
                    subtitle = stream_title
                if not title:
                    title = "Internet Radio"

        state["title"] = title
        state["subtitle"] = subtitle
        state["source"] = source

        try:
            state["position_ms"] = self.call("core.playback.get_time_position") or 0
        except MopidyError:
            pass
        if track:
            state["length_ms"] = track.get("length") or 0

        try:
            tl = self.call("core.tracklist.get_length")
            state["tracklist_len"] = tl or 0
        except MopidyError:
            pass

        return state

    # -- transport ------------------------------------------------------------
    def play(self):
        self.call("core.playback.play")

    def pause(self):
        self.call("core.playback.pause")

    def toggle(self):
        """Play/pause toggle (Mopidy has no single toggle RPC)."""
        state = self.call("core.playback.get_state")
        if state == "playing":
            self.call("core.playback.pause")
        else:
            self.call("core.playback.play")

    def next(self):
        self.call("core.playback.next")

    def previous(self):
        self.call("core.playback.previous")

    def set_volume(self, vol):
        vol = max(0, min(100, int(vol)))
        self.call("core.mixer.set_volume", volume=vol)

    def volume_step(self, delta):
        try:
            cur = self.call("core.mixer.get_volume") or 0
            self.set_volume(cur + delta)
        except MopidyError:
            pass

    # -- content --------------------------------------------------------------
    def play_uris(self, uris, shuffle=False):
        """Replace the tracklist with uris and start playback."""
        if shuffle:
            uris = list(uris)
            random.shuffle(uris)
        self.call("core.tracklist.clear")
        if uris:
            self.call("core.tracklist.add", uris=uris)
            self.call("core.playback.play")

    def play_preset(self, uri):
        self.play_uris([uri])

    def library_shuffle_all(self):
        """Add every local track to the tracklist, shuffled."""
        uris = self._collect_local_tracks()
        if not uris:
            raise MopidyError("No local music found. Upload files to the "
                              "jukebox-music share and run: sudo jukebox-scan")
        self.play_uris(uris, shuffle=True)

    def _collect_local_tracks(self):
        uris = []
        self._browse_into("local:directory", uris, depth=6)
        return uris

    def _browse_into(self, uri, out, depth=0):
        if depth < 0:
            return
        try:
            refs = self.call("core.library.browse", uri=uri) or []
        except MopidyError:
            return
        for ref in refs:
            rtype = ref.get("type")
            ruri = ref.get("uri", "")
            if rtype == "track":
                out.append(ruri)
            elif rtype == "directory" and ruri.startswith("local:"):
                self._browse_into(ruri, out, depth - 1)

    def get_playlists(self):
        try:
            refs = self.call("core.playlists.as_list") or []
            return [r for r in refs if r.get("name")]
        except MopidyError:
            return []

    def get_playlist_tracks(self, playlist_uri):
        try:
            refs = self.call("core.playlists.get_items", uri=playlist_uri) or []
            return [r["uri"] for r in refs if r.get("type") == "track"]
        except MopidyError:
            return []
