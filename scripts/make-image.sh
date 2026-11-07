#!/usr/bin/env bash
# =============================================================================
#  Jazmin's JukeBox - OPTIONAL: build a flashable SD card image with pi-gen
#
#  EXPERIMENTAL. The supported install path is:
#      1. Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager
#      2. Run:  sudo bash install.sh
#
#  This script instead produces a ready-to-flash image with everything
#  pre-installed, so anyone can flash a card and go. It needs Docker and
#  internet access, and takes 30-60+ minutes on a typical machine.
#
#  Usage:  bash scripts/make-image.sh [output-dir]
#  The image builder prompts for a unique password for the pi account.
# =============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$PROJECT_DIR/image}"
PIGEN_DIR="$PROJECT_DIR/build/pi-gen"
FIRST_USER_PASS="${JAZMIN_FIRST_USER_PASS:-}"

if [[ -z "$FIRST_USER_PASS" ]]; then
  [[ -t 0 ]] || {
    echo "Set JAZMIN_FIRST_USER_PASS or run this script in an interactive terminal." >&2
    exit 1
  }
  read -r -s -p "Set a unique password for the pi account (12+ characters): " FIRST_USER_PASS
  echo
  read -r -s -p "Confirm password: " password_confirm
  echo
  [[ "$FIRST_USER_PASS" == "$password_confirm" ]] || {
    echo "Passwords do not match." >&2
    exit 1
  }
fi

[[ ${#FIRST_USER_PASS} -ge 12 ]] || {
  echo "The pi account password must be at least 12 characters." >&2
  exit 1
}
[[ "$FIRST_USER_PASS" != *$'\n'* && "$FIRST_USER_PASS" != *$'\r'* ]] || {
  echo "The pi account password cannot contain newline characters." >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || { echo "Docker is required for pi-gen builds."; exit 1; }

mkdir -p "$OUT_DIR"

# --- 1. fetch pi-gen ---------------------------------------------------------
if [[ ! -d "$PIGEN_DIR" ]]; then
  git clone --depth 1 https://github.com/RPi-Distro/pi-gen.git "$PIGEN_DIR"
fi
cd "$PIGEN_DIR"

# --- 2. our stage: stage-jukebox ---------------------------------------------
STAGE="$PIGEN_DIR/stage-jukebox"
rm -rf "$STAGE"
mkdir -p "$STAGE/01-install/files/opt/jukebox" "$STAGE/01-install/files/etc/jukebox" \
         "$STAGE/01-install/files/opt/jukebox/assets" \
         "$STAGE/01-install/files/opt/jukebox/fonts" \
         "$STAGE/01-install/files/etc/mopidy" \
         "$STAGE/01-install/files/etc/samba/conf.d" \
         "$STAGE/01-install/files/etc/systemd/system" \
         "$STAGE/01-install/files/usr/local/bin" "$STAGE/01-install/files/root"

cat > "$STAGE/prerun.sh" <<'EOF'
#!/bin/bash -e
if [ ! -d "${ROOTFS_DIR}" ]; then
   copy_previous
fi
EOF

cat > "$STAGE/01-install/run.sh" <<'EOF'
#!/bin/bash -e
# Install packages first so their configuration directories exist.
on_chroot <<'CHROOT'
apt-get update -qq
apt-get install -y --no-install-recommends \
  mopidy gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-alsa python3-pil python3-numpy python3-requests python3-spidev \
  python3-rpi.gpio samba ffmpeg fonts-dejavu-core jq device-tree-compiler raspi-utils
pip3 install --break-system-packages --no-deps Mopidy-Iris==3.70.0 Mopidy-Local==3.3.0 Mopidy-TuneIn==1.1.0
pip3 install --break-system-packages smbus2 uritools
CHROOT

mkdir -p "${ROOTFS_DIR}/opt/jukebox" "${ROOTFS_DIR}/etc/jukebox" \
  "${ROOTFS_DIR}/etc/mopidy" "${ROOTFS_DIR}/etc/samba/conf.d" \
  "${ROOTFS_DIR}/etc/systemd/system" "${ROOTFS_DIR}/usr/local/bin" \
  "${ROOTFS_DIR}/root"
cp -a files/opt/jukebox/. "${ROOTFS_DIR}/opt/jukebox/"
install -m 0644 files/etc/jukebox/* "${ROOTFS_DIR}/etc/jukebox/"
install -m 0644 files/etc/mopidy/mopidy.conf "${ROOTFS_DIR}/etc/mopidy/"
install -m 0644 files/etc/systemd/system/jukebox-ui.service \
  "${ROOTFS_DIR}/etc/systemd/system/"
install -m 0755 files/usr/local/bin/* "${ROOTFS_DIR}/usr/local/bin/"
install -m 0644 files/etc/samba/conf.d/jukebox-smb.conf \
  "${ROOTFS_DIR}/etc/samba/conf.d/"
install -m 0644 files/root/jazmin-sd.dts "${ROOTFS_DIR}/root/"

on_chroot <<'CHROOT'
grep -q "conf.d" /etc/samba/smb.conf || echo "include = /etc/samba/conf.d" >> /etc/samba/smb.conf
systemctl enable mopidy jukebox-ui smbd
CHROOT
EOF

# Overlay files
cp "$PROJECT_DIR/jukebox/"*.py "$STAGE/01-install/files/opt/jukebox/"
cp "$PROJECT_DIR/fonts/"*.ttf "$STAGE/01-install/files/opt/jukebox/fonts/"
cp "$PROJECT_DIR/fonts/OFL.txt" "$STAGE/01-install/files/opt/jukebox/fonts/"
cp "$PROJECT_DIR/jukebox/assets/"* "$STAGE/01-install/files/opt/jukebox/assets/"
cp "$PROJECT_DIR/radio/"*.json "$STAGE/01-install/files/etc/jukebox/"
cp "$PROJECT_DIR/etc/mopidy/mopidy.conf" "$STAGE/01-install/files/etc/mopidy/"
cp "$PROJECT_DIR/etc/samba/jukebox-smb.conf" "$STAGE/01-install/files/etc/samba/conf.d/"
cp "$PROJECT_DIR/etc/systemd/jazmin-jukebox.service" "$STAGE/01-install/files/etc/systemd/system/jukebox-ui.service"
cp "$PROJECT_DIR/scripts/jukebox-scan.sh" "$STAGE/01-install/files/usr/local/bin/jukebox-scan"
cp "$PROJECT_DIR/scripts/jukebox-station.sh" "$STAGE/01-install/files/usr/local/bin/jukebox-station"
cp "$PROJECT_DIR/overlays/jazmin-sd.dts" "$STAGE/01-install/files/root/jazmin-sd.dts"

# config.txt additions are applied on first boot instead (see firstrun below)
cat > "$STAGE/01-install/files/root/firstrun.sh" <<'EOF'
#!/bin/bash -e
# Applied on first boot by systemd unit below
CONFIG=/boot/firmware/config.txt
[[ -f $CONFIG ]] || CONFIG=/boot/config.txt
grep -q "jazmin jukebox" "$CONFIG" || cat >> "$CONFIG" <<'CFG'
# >>> jazmin jukebox >>>
dtparam=spi=on
dtparam=i2c_arm=on
dtparam=audio=off
dtoverlay=iqaudio-dacplus,auto_mute_amp
dtoverlay=jazmin-sd
# <<< jazmin jukebox <<<
CFG
dtc -@ -I dts -O dtb -o /boot/firmware/overlays/jazmin-sd.dtbo /root/jazmin-sd.dts \
  || dtc -I dts -O dtb -o /boot/firmware/overlays/jazmin-sd.dtbo /root/jazmin-sd.dts
mkdir -p /mnt/screen-sd
grep -q "screen-sd" /etc/fstab \
  || echo "/dev/mmcblk1p1 /mnt/screen-sd vfat nofail,x-systemd.device-timeout=5s,uid=mopidy,gid=mopidy,umask=0000 0 0" >> /etc/fstab
mkdir -p /var/lib/mopidy/music /var/lib/mopidy/playlists
chown -R mopidy:mopidy /var/lib/mopidy
usermod -aG spi,i2c,gpio pi 2>/dev/null || true
systemctl enable --now jukebox-ui 2>/dev/null || true
systemctl disable jazmin-firstboot 2>/dev/null || true
rm -f /etc/systemd/system/jazmin-firstboot.service
systemctl daemon-reload
systemctl reboot
EOF

cat > "$STAGE/01-install/files/etc/systemd/system/jazmin-firstboot.service" <<'EOF'
[Unit]
Description=Jazmin's JukeBox first-boot setup
After=network.target

[Service]
Type=oneshot
ExecStart=/bin/bash /root/firstrun.sh

[Install]
WantedBy=multi-user.target
EOF
# enable firstboot unit inside image
cat >> "$STAGE/01-install/run.sh" <<'EOF2'
on_chroot <<'CHROOT2'
systemctl enable jazmin-firstboot
CHROOT2
EOF2

# --- 3. pi-gen config ---------------------------------------------------------
cat > "$PIGEN_DIR/config" <<'EOF'
IMG_NAME=jasmins-jukebox
RELEASE=trixie
DEPLOY_COMPRESSION=xz
TARGET_HOSTNAME=jazmin
FIRST_USER_NAME=pi
ENABLE_SSH=1
STAGE_LIST="stage0 stage1 stage2 ./stage-jukebox"
EOF
printf 'FIRST_USER_PASS=%q\n' "$FIRST_USER_PASS" >> "$PIGEN_DIR/config"
chmod 600 "$PIGEN_DIR/config"
trap 'sed -i "/^FIRST_USER_PASS=/d" "$PIGEN_DIR/config"; unset FIRST_USER_PASS' EXIT

echo "Starting pi-gen build (this takes a long while)..."
./build.sh -c "$PIGEN_DIR/config"

echo ""
echo "Done. Look for the image in: $PIGEN_DIR/deploy/"
echo "Flash it with Raspberry Pi Imager -> Use custom -> the .img.xz file."
echo "Login: pi / the unique password you entered for this build."
