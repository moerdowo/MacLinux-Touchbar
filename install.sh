#!/usr/bin/env bash
#
# install.sh -- put dfrd on the system.
#
# Everything lands under /usr/local, nothing is enabled automatically, and the
# pieces that need privileges are separate opt-in steps. Run with --uninstall
# to take it all back off.
#
#   sudo ./install.sh              files, udev rule, service unit (not enabled)
#   sudo ./install.sh --polkit     also install the polkit action
#   sudo ./install.sh --uninstall  remove everything

set -euo pipefail

LIB=/usr/local/lib/dfrd
BIN=/usr/local/bin
UDEV=/etc/udev/rules.d/99-touchbar-drm-noseat.rules
UNIT=/etc/systemd/system/dfrd.service
DESKTOP=/usr/share/applications/io.github.moerdowo.dfrd.editor.desktop
POLICY=/usr/share/polkit-1/actions/io.github.moerdowo.dfrd.policy
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

MODULES=(dfrconfig.py dfrdrain.py dfrdrm.py dfrfeeds.py dfrinput.py dfripc.py
         dfrsession.py dfrtheme.py dfrwidgets.py dfractions.py README.md)
PROGRAMS=(dfrd dfrctl dfr-editor touchbar-mode)

[ "$(id -u)" = 0 ] || { echo "install.sh: run as root (sudo ./install.sh)" >&2; exit 1; }

uninstall() {
  echo "removing dfrd"
  systemctl disable --now dfrd.service 2>/dev/null || true
  rm -f "$UNIT" "$DESKTOP" "$POLICY" "$UDEV"
  for p in "${PROGRAMS[@]}"; do rm -f "$BIN/$p"; done
  rm -rf "$LIB"
  systemctl daemon-reload 2>/dev/null || true
  udevadm control --reload-rules 2>/dev/null || true
  echo "done. Your config in ~/.config/dfrd was left alone."
}

if [ "${1:-}" = --uninstall ]; then uninstall; exit 0; fi

echo "installing to $LIB"
install -d "$LIB"
for f in "${MODULES[@]}"; do install -m 0644 "$SRC/$f" "$LIB/$f"; done
for p in "${PROGRAMS[@]}"; do
  install -m 0755 "$SRC/$p" "$LIB/$p"
  ln -sf "$LIB/$p" "$BIN/$p"
done

echo "installing the udev rule"
# Without this, systemd-logind hands the strip's DRM node to the compositor
# and takes DRM master on the session's behalf, and dfrd can never modeset it.
install -m 0644 "$SRC/99-touchbar-drm-noseat.rules" "$UDEV"
udevadm control --reload-rules

echo "installing the service unit (not enabled)"
install -m 0644 "$SRC/dfrd.service" "$UNIT"
systemctl daemon-reload

echo "installing the desktop entry"
install -d "$(dirname "$DESKTOP")"
install -m 0644 "$SRC/io.github.moerdowo.dfrd.editor.desktop" "$DESKTOP"

if [ "${1:-}" = --polkit ]; then
  echo "installing the polkit action"
  install -d "$(dirname "$POLICY")"
  install -m 0644 "$SRC/io.github.moerdowo.dfrd.policy" "$POLICY"
fi

cat <<'NOTE'

Installed. Nothing is running yet -- on purpose.

  dfrctl status                 where things stand
  sudo dfrctl mode display      hand the strip to dfrd
  sudo systemctl start dfrd     run the daemon (switches mode for you)
  dfr-editor                    design the layout

Enabling dfrd at boot is deliberately left to you. Display mode has no
keyboard interface, so if the daemon ever fails to start on a machine with no
physical function row, you would have no Escape key until you switched back:

  sudo systemctl enable dfrd    only once you trust it

A power cycle always returns the Touch Bar to keyboard mode regardless.
NOTE
