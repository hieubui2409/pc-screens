#!/usr/bin/env bash
# =============================================================================
# One-time root setup for the two internal PC screens
#   1. Lian Li 8.8" Universal Screen   (USB 1cbe:a088 + ARGB frame 0416:8050)
#   2. Jungle Leopard / HONGTAI 9.16"  (USB 33c3:7788, CDC-ACM -> /dev/ttyACM0)
#
# Everything else in this project runs unprivileged. This script only does the
# parts the kernel genuinely requires root for: installing packages, writing
# udev rules, and building the evdi kernel module.
#
# Run with:  sudo bash setup-root.sh
# Idempotent — safe to re-run.
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "$USER")}"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root:  sudo bash $0" >&2
  exit 1
fi

log "Target user: $TARGET_USER"

# -----------------------------------------------------------------------------
# 1. Build dependencies for lian-li-linux (Rust daemon + Tauri GUI)
# -----------------------------------------------------------------------------
log "Installing build dependencies (apt)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# Core: libusb/udev transport, ffmpeg encode, evdi virtual display, build tools
CORE_PKGS=(
  libusb-1.0-0-dev libudev-dev libfontconfig-dev
  libxkbcommon-dev libwayland-dev libx11-dev libinput-dev libdrm-dev
  libgl-dev libegl-dev clang cmake pkg-config ffmpeg nasm npm
  libavcodec-dev libavformat-dev libswscale-dev libavutil-dev
  libevdi0-dev
)
# GUI only (Tauri/WebKit). Split out so a failure here doesn't block the daemon.
GUI_PKGS=(
  libwebkit2gtk-4.1-dev libglib2.0-dev libgtk-3-dev libsoup-3.0-dev
  libayatana-appindicator3-dev librsvg2-dev
)

if apt-get install -y "${CORE_PKGS[@]}"; then
  ok "core build deps installed"
else
  warn "some core packages failed — the Rust daemon may not build"
fi

if apt-get install -y "${GUI_PKGS[@]}"; then
  ok "GUI deps installed"
else
  warn "GUI deps failed — daemon + CLI still usable, Tauri GUI will not build"
fi

# -----------------------------------------------------------------------------
# 2. evdi kernel module — turns the Lian Li panel into a REAL second monitor
#    (this is what makes it behave like it does under Windows/L-Connect)
# -----------------------------------------------------------------------------
log "Installing evdi kernel module (DKMS)"
if apt-get install -y evdi-dkms; then
  if modprobe evdi 2>/dev/null; then
    ok "evdi module built and loaded"
    echo evdi > /etc/modules-load.d/evdi.conf
    ok "evdi set to auto-load at boot"
  else
    warn "evdi installed but failed to load on kernel $(uname -r)"
    warn "DKMS build log: /var/lib/dkms/evdi/*/build/make.log"
    warn "Desktop-mode (real monitor) will be unavailable; image streaming still works"
  fi
else
  warn "evdi-dkms failed to install — desktop mode unavailable, streaming still works"
fi

# -----------------------------------------------------------------------------
# 3. udev rules — non-root access to both screens
# -----------------------------------------------------------------------------
log "Installing udev rules"

# Group used by the upstream lian-li-linux packaging.
if ! getent group lianli >/dev/null; then
  groupadd -r lianli && ok "created group 'lianli'"
fi
if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx lianli; then
  usermod -aG lianli "$TARGET_USER" && ok "added $TARGET_USER to group 'lianli'"
fi
# /dev/ttyACM0 is root:dialout by default — needed for the Jungle Leopard panel
# even before udev ACLs apply.
if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx dialout; then
  usermod -aG dialout "$TARGET_USER" && ok "added $TARGET_USER to group 'dialout'"
fi

install -m 0644 "$HERE/udev/99-pc-screens.rules" /etc/udev/rules.d/99-pc-screens.rules
ok "/etc/udev/rules.d/99-pc-screens.rules"

if [[ -f "$HERE/lian-li-linux/packaging/udev/60-lianli.rules" ]]; then
  install -m 0644 "$HERE/lian-li-linux/packaging/udev/60-lianli.rules" \
    /etc/udev/rules.d/60-lianli.rules
  ok "/etc/udev/rules.d/60-lianli.rules (full Lian Li device set)"
fi

udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --subsystem-match=tty --subsystem-match=hidraw
ok "udev rules reloaded and re-triggered"

# -----------------------------------------------------------------------------
# 4. Verify
# -----------------------------------------------------------------------------
log "Verifying device permissions"
sleep 1
for dev in /dev/ttyACM0 /dev/jungleleopard; do
  [[ -e $dev ]] && printf '  %-22s %s\n' "$dev" "$(stat -c '%A %U:%G' "$dev")"
done
for pair in 1cbe:a088 0416:8050 33c3:7788; do
  vid=${pair%%:*}; pid=${pair##*:}
  line=$(lsusb -d "$pair" 2>/dev/null) || continue
  bus=$(echo "$line" | sed -E 's/Bus ([0-9]+) Device ([0-9]+).*/\1/')
  devn=$(echo "$line" | sed -E 's/Bus ([0-9]+) Device ([0-9]+).*/\2/')
  node="/dev/bus/usb/$bus/$devn"
  printf '  %-22s %s  (%s)\n' "$pair" "$(stat -c '%A %U:%G' "$node" 2>/dev/null)" "$node"
done

echo
log "Done."
echo "  Group changes ('lianli', 'dialout') need a re-login to take effect for"
echo "  new shells, but the udev 'uaccess' ACL applies to your current session"
echo "  immediately — the verification above shows the live state."
