#!/usr/bin/env bash
# Rescan the local music library (run after uploading new files).
set -e
echo "[jukebox] scanning /var/lib/mopidy/music ..."
mopidyctl local scan
echo "[jukebox] done. New tracks are now searchable in Iris and playable on screen."
