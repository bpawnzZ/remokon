#!/usr/bin/env bash
# Install Remokon for the current user: one file on PATH, an icon, a menu entry.
# Uninstall: rm ~/.local/bin/remokon and the two files under ~/.local/share.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
prefix="${PREFIX:-$HOME/.local}"

# The interpreter is pinned because GTK bindings live in the system python; a
# conda/miniforge python3 comes first on PATH and has no `gi`.
if ! /usr/bin/python3 -c 'import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")' 2>/dev/null; then
  echo "Missing GTK4 bindings. On Arch: sudo pacman -S gtk4 libadwaita python-gobject" >&2
  exit 1
fi

"$here/build.sh"
install -Dm755 "$here/dist/remokon" "$prefix/bin/remokon"
install -Dm644 "$here/remokon.svg" \
  "$prefix/share/icons/hicolor/scalable/apps/remokon.svg"
install -Dm644 "$here/remokon.desktop" \
  "$prefix/share/applications/remokon.desktop"
mkdir -p "$HOME/.config/remokon"
chmod 700 "$HOME/.config/remokon"

command -v update-desktop-database >/dev/null && \
  update-desktop-database "$prefix/share/applications" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && \
  gtk-update-icon-cache -qtf "$prefix/share/icons/hicolor" 2>/dev/null || true

echo "installed: $prefix/bin/remokon"
echo "settings:  $HOME/.config/remokon/config.json (or the gear in the app)"
