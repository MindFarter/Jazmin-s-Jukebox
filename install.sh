#!/usr/bin/env bash
# =============================================================================
#  Jazmin's JukeBox - one-shot installer for Raspberry Pi OS Lite (Trixie)
#
#  Turns a plain Raspberry Pi OS Lite 64-bit install into the Jazmin's
#  JukeBox appliance:
#    - Mopidy music server (local MP3/WAV/FLAC + internet radio)
#    - Iris web UI for phones (http://<pi-ip>:6680/iris)
#    - Custom jukebox touchscreen UI on the 2-inch SPI display
#    - Samba share "jukebox-music" for uploading music from any device
#    - IQaudio DigiAMP+ audio output, SPI + I2C enabled for the display
#
#  Usage:
#    sudo bash install.sh                 # normal install
#    sudo bash install.sh --keep-hostname # do not rename host to "jazmin"
#
#  Run it again any time to update - the installer is idempotent.
# =============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JUKEBOX_NAME="Jazmin's JukeBox"
KEEP_HOSTNAME=0
[[ "${1:-}" == "--keep-hostname" ]] && KEEP_HOSTNAME=1

# Colors
G="\033[0;32m"; Y="\033[1;33m"; R="\033[0;31m"; N="\033[0m"
say()  { echo -e "${G}[$JUKEBOX_NAME]${N} $*"; }
warn() { echo -e "${Y}[$JUKEBOX_NAME]${N} $*"; }
die()  { echo -e "${R}[$JUKEBOX_NAME] ERROR:${N} $*" >&2; exit 1; }

# ----------------------------------------------------------------------------
# Pre-flight checks
# ----------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Please run with sudo:  sudo bash install.sh"

# We need to be on a Raspberry Pi OS / Debian Trixie (arm64) system
if [[ -f /etc/os-release ]]; then
  . /etc/os-release
  say "Detected: ${PRETTY_NAME:-unknown OS}"
  [[ "${VERSION_CODENAME:-}" == "trixie" ]] || warn "This installer targets Raspberry Pi OS / Debian 'trixie'. You are on '${VERSION_CODENAME:-?}' - continuing anyway."
else
  warn "Cannot read /etc/os-release - continuing anyway."
fi

[[ "$(uname -m)" == "aarch64" ]] || warn "This project targets the Pi Zero 2 W (aarch64). You are on $(uname -m)."

# Figure out the login user (service runs as this user)
LOGIN_USER="${SUDO_USER:-}"
[[ -z "$LOGIN_USER" ]] && LOGIN_USER="$(logname 2>/dev/null || true)"
[[ -z "$LOGIN_USER" || "$LOGIN_USER" == "root" ]] && LOGIN_USER="pi"
if ! id "$LOGIN_USER" >/dev/null 2>&1; then
  warn "User '$LOGIN_USER' not found, falling back to root for the UI service."
  LOGIN_USER="root"
fi
say "Installing for user: $LOGIN_USER"

# ----------------------------------------------------------------------------
# 1. Packages
# ----------------------------------------------------------------------------
say "Updating apt and installing packages (this can take a few minutes)..."
apt-get update -qq
apt-get install -y --no-install-recommends \
  mopidy \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-alsa \
  python3-pil python3-numpy python3-requests \
  python3-spidev python3-rpi.gpio \
  samba ffmpeg raspi-utils \
  fonts-dejavu-core curl wget unzip jq \
  device-tree-compiler

# mutagen reads embedded MP3 album art; small and pure-python
python3 -c "import mutagen" 2>/dev/null \
  || pip3 install --break-system-packages mutagen \
  || pip3 install mutagen || true

# smbus2 is small and pure-python; pip is the reliable source
if ! python3 -c "import smbus2" 2>/dev/null; then
  pip3 install --break-system-packages smbus2 2>/dev/null \
    || pip3 install smbus2 \
    || die "Could not install smbus2 (needed for the touchscreen)."
fi

# ----------------------------------------------------------------------------
# 2. Mopidy extensions (pinned for Mopidy 3.x on Debian Trixie)
#    NOTE: --no-deps so pip never pulls a conflicting Mopidy from PyPI -
#    Mopidy itself comes from apt.
# ----------------------------------------------------------------------------
say "Installing Mopidy extensions (Iris, Local, TuneIn)..."
PIP_FLAGS="--break-system-packages --no-deps"
pip3 install $PIP_FLAGS "Mopidy-Iris==3.70.0" \
  || die "Failed to install Mopidy-Iris"
pip3 install $PIP_FLAGS "Mopidy-Local==3.3.0" \
  || die "Failed to install Mopidy-Local"
pip3 install $PIP_FLAGS "Mopidy-TuneIn==1.1.0" \
  || die "Failed to install Mopidy-TuneIn"
# uritools is needed by Mopidy-Local but is not an apt package
python3 -c "import uritools" 2>/dev/null \
  || pip3 install --break-system-packages uritools || true

# ----------------------------------------------------------------------------
# 3. Directories
# ----------------------------------------------------------------------------
say "Creating directories..."
mkdir -p /opt/jukebox/fonts /opt/jukebox/assets
mkdir -p /etc/jukebox
mkdir -p /var/lib/mopidy/music /var/lib/mopidy/playlists
mkdir -p /mnt/screen-sd

# ----------------------------------------------------------------------------
# 4. Install the UI application
# ----------------------------------------------------------------------------
say "Installing the jukebox UI to /opt/jukebox ..."
install -m 0644 "$PROJECT_DIR/jukebox/"*.py /opt/jukebox/
install -m 0644 "$PROJECT_DIR/jukebox/assets/"*.* /opt/jukebox/assets/ 2>/dev/null || true
install -m 0644 "$PROJECT_DIR/fonts/"*.ttf /opt/jukebox/fonts/ 2>/dev/null || true
install -m 0644 "$PROJECT_DIR/fonts/OFL.txt" /opt/jukebox/fonts/ 2>/dev/null || true
# body font from the system dejavu package
if [[ -f /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf ]]; then
  install -m 0644 /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf /opt/jukebox/fonts/
fi

# Config + radio presets (never overwrite user-edited files)
if [[ -f /etc/jukebox/jukebox.json ]]; then
  say "Keeping existing /etc/jukebox/jukebox.json"
else
  install -m 0644 "$PROJECT_DIR/radio/jukebox.json" /etc/jukebox/jukebox.json
fi
if [[ -f /etc/jukebox/radio_presets.json ]]; then
  say "Keeping existing /etc/jukebox/radio_presets.json"
else
  install -m 0644 "$PROJECT_DIR/radio/radio_presets.json" /etc/jukebox/radio_presets.json
fi

# ----------------------------------------------------------------------------
# 5. Mopidy configuration
# ----------------------------------------------------------------------------
say "Configuring Mopidy..."
if [[ -f /etc/mopidy/mopidy.conf ]] && ! grep -q "Jazmin's JukeBox" /etc/mopidy/mopidy.conf; then
  cp /etc/mopidy/mopidy.conf "/etc/mopidy/mopidy.conf.bak.$(date +%Y%m%d%H%M%S)"
fi
install -m 0644 "$PROJECT_DIR/etc/mopidy/mopidy.conf" /etc/mopidy/mopidy.conf

chown -R mopidy:mopidy /var/lib/mopidy

# ----------------------------------------------------------------------------
# 6. Enable SPI + I2C and the IQaudio DigiAMP+ overlay
# ----------------------------------------------------------------------------
say "Enabling SPI, I2C and the DigiAMP+ audio overlay..."
CONFIG=/boot/firmware/config.txt
[[ -f $CONFIG ]] || CONFIG=/boot/config.txt
[[ -f $CONFIG ]] || die "Cannot find config.txt (expected /boot/firmware/config.txt)"

# raspi-config way (cleanest)
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_spi 0 >/dev/null 2>&1 || true
  raspi-config nonint do_i2c 0 >/dev/null 2>&1 || true
fi

# Managed block (idempotent)
sed -i '/# >>> jazmin jukebox >>>/,/# <<< jazmin jukebox <<</d' "$CONFIG"
cat >> "$CONFIG" <<'EOF'
# >>> jazmin jukebox >>>
# Jazmin's JukeBox - landscape display (SPI + I2C), DigiAMP+ audio,
# and the SD card slot on the 2-inch screen module (jazmin-sd overlay)
dtparam=spi=on
dtparam=i2c_arm=on
dtparam=audio=off
dtoverlay=iqaudio-dacplus,auto_mute_amp
dtoverlay=jazmin-sd
# <<< jazmin jukebox <<<
EOF

# ----------------------------------------------------------------------------
# 6b. Screen SD card slot: compile + install the jazmin-sd overlay
# ----------------------------------------------------------------------------
say "Setting up the screen's SD card slot (jazmin-sd overlay)..."
if command -v dtc >/dev/null 2>&1; then
  dtc -@ -I dts -O dtb -o /boot/firmware/overlays/jazmin-sd.dtbo \
    "$PROJECT_DIR/overlays/jazmin-sd.dts" 2>/dev/null \
    || dtc -I dts -O dtb -o /boot/firmware/overlays/jazmin-sd.dtbo \
       "$PROJECT_DIR/overlays/jazmin-sd.dts" 2>/dev/null \
    || warn "Could not compile jazmin-sd overlay (music SD on screen will not work)"
else
  warn "dtc not found - screen SD slot overlay not installed"
fi

# mount the screen SD card at /mnt/screen-sd (no fail if no card)
if ! grep -q "screen-sd" /etc/fstab; then
  echo "/dev/mmcblk1p1 /mnt/screen-sd vfat nofail,x-systemd.device-timeout=5s,uid=mopidy,gid=mopidy,umask=0000 0 0" >> /etc/fstab || true
fi

# ----------------------------------------------------------------------------
# 7. Systemd service for the touchscreen UI
# ----------------------------------------------------------------------------
say "Installing the jukebox UI service..."
sed "s/^User=pi$/User=$LOGIN_USER/" "$PROJECT_DIR/etc/systemd/jazmin-jukebox.service" \
  > /etc/systemd/system/jazmin-jukebox.service

# Make sure the service user can reach SPI/I2C/GPIO
for grp in spi i2c gpio video; do
  if getent group "$grp" >/dev/null 2>&1 && id -u "$LOGIN_USER" >/dev/null 2>&1; then
    usermod -aG "$grp" "$LOGIN_USER" 2>/dev/null || true
  fi
done

systemctl daemon-reload
systemctl enable --now mopidy.service
systemctl enable --now jazmin-jukebox.service

# ----------------------------------------------------------------------------
# 8. Samba share for music uploads (phone/PC drag & drop)
# ----------------------------------------------------------------------------
say "Configuring the jukebox-music Samba share..."
if [[ -d /etc/samba/conf.d ]]; then
  install -m 0644 "$PROJECT_DIR/etc/samba/jukebox-smb.conf" /etc/samba/conf.d/
  grep -q "conf.d" /etc/samba/smb.conf || cat >> /etc/samba/smb.conf <<'EOF'
include = /etc/samba/conf.d
EOF
else
  cat >> /etc/samba/smb.conf <<'EOF'

[jukebox-music]
   comment = Jazmin's JukeBox music upload
   path = /var/lib/mopidy/music
   read only = no
   guest ok = yes
   force user = mopidy
   create mask = 0644
   directory mask = 0755
EOF
fi
systemctl enable --now smbd 2>/dev/null || true
systemctl restart smbd 2>/dev/null || true

# ----------------------------------------------------------------------------
# 9. Helper commands on PATH
# ----------------------------------------------------------------------------
say "Installing helper commands (jukebox-scan, jukebox-station)..."
install -m 0755 "$PROJECT_DIR/scripts/jukebox-scan.sh" /usr/local/bin/jukebox-scan
install -m 0755 "$PROJECT_DIR/scripts/jukebox-station.sh" /usr/local/bin/jukebox-station

# ----------------------------------------------------------------------------
# 10. Hostname (optional)
# ----------------------------------------------------------------------------
if [[ $KEEP_HOSTNAME -eq 0 ]]; then
  if [[ "$(hostname)" != "jazmin" ]]; then
    say "Setting hostname to 'jazmin' (use --keep-hostname to skip)..."
    hostnamectl set-hostname jazmin 2>/dev/null || hostname jazmin || true
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\tjazmin/" /etc/hosts 2>/dev/null || true
  fi
fi

# ----------------------------------------------------------------------------
# 11. First music scan
# ----------------------------------------------------------------------------
say "Scanning local music library (if any)..."
mopidyctl local scan 2>&1 | tail -3 || true

# ----------------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------------
IP_ADDRESSES=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | head -3 | tr '\n' ' ')
echo ""
echo -e "${G}=============================================================${N}"
echo -e "${G}   ♪ $JUKEBOX_NAME is installed ♪${N}"
echo -e "${G}=============================================================${N}"
echo ""
echo "  Phone / web interface : http://jazmin.local:6680/iris"
if [[ -n "$IP_ADDRESSES" ]]; then
  echo "  (or by IP)            : http://${IP_ADDRESSES}:6680/iris"
fi
echo "  Music upload share    : \\\\jazmin.local\jukebox-music  (Samba, no password)"
echo "  Screen                : the jukebox UI starts automatically on boot"
echo ""
echo "  Add a radio station   : sudo jukebox-station add 'Name' 'http://stream.url'"
echo "  Rescan local music   : sudo jukebox-scan"
echo ""
echo -e "${Y}  Reboot to activate SPI/I2C and the DigiAMP+ audio output:${N}"
echo -e "${Y}    sudo reboot${N}"
echo ""
