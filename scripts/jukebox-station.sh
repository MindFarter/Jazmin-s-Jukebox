#!/usr/bin/env bash
# Manage the radio stations shown in the JukeBox settings screen
# (stored in /etc/jukebox/radio_presets.json)
#
#   jukebox-station list
#   jukebox-station add 'Station Name' 'https://stream.url' [A5] ['genre']
#   jukebox-station remove A3
set -e
PRESETS=/etc/jukebox/radio_presets.json

cmd="${1:-}"
[[ -z "$cmd" || ! -f "$PRESETS" ]] && { echo "usage: jukebox-station list|add|remove  ($PRESETS missing?)"; exit 1; }

case "$cmd" in
  list)
    jq -r '.stations[] | "\(.label)\t\(.name)  [\(.genre // "radio")]\n\t\(.uri)"' "$PRESETS"
    ;;
  add)
    name="${2:?station name required}"
    uri="${3:?stream URL required}"
    slot="${4:-}"
    genre="${5:-radio}"
    python3 - "$PRESETS" "$name" "$uri" "$slot" "$genre" <<'PY'
import json, sys
path, name, uri, slot, genre = sys.argv[1:6]
with open(path) as f:
    data = json.load(f)
stations = data["stations"]
if slot:
    idx = next((i for i, s in enumerate(stations) if s["label"].lower() == slot.lower()), None)
    if idx is None:
        sys.exit(f"no slot labelled {slot} - use a label like A5")
    stations[idx] = {"label": slot.upper(), "name": name, "genre": genre, "uri": uri}
else:
    used = {s["label"] for s in stations}
    label = next((f"A{i}" for i in range(1, 9) if f"A{i}" not in used), f"A{len(stations)+1}")
    stations.append({"label": label, "name": name, "genre": genre, "uri": uri})
with open(path, "w") as f:
    json.dump(data, f, indent=2)
print("saved")
PY
    echo "[jukebox] added. Restart the UI to see it: sudo systemctl restart jazmin-jukebox"
    ;;
  remove)
    label="${2:?slot label required, e.g. A3}"
    python3 - "$PRESETS" "$label" <<'PY'
import json, sys
path, label = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = json.load(f)
data["stations"] = [s for s in data["stations"] if s["label"].lower() != label.lower()]
with open(path, "w") as f:
    json.dump(data, f, indent=2)
print("saved")
PY
    echo "[jukebox] removed. Restart the UI: sudo systemctl restart jazmin-jukebox"
    ;;
  *)
    echo "usage: jukebox-station list|add 'Name' 'url' [slot] ['genre'] | remove A3"
    exit 1
    ;;
esac
