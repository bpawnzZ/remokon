#!/usr/bin/env bash
# Build one executable file: a zipapp with a python3 shebang. It carries its own
# code; GTK4/libadwaita/PyGObject come from the system (they cannot be bundled
# without vendoring a toolkit this app does not own).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$here/dist/remokon"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cp "$here/kodi.py" "$here/ui.py" "$tmp/"
cat > "$tmp/__main__.py" <<'PY'
import sys

from ui import main

raise SystemExit(main(sys.argv))
PY

mkdir -p "$here/dist"
rm -f "$out"
/usr/bin/python3 -m zipapp "$tmp" -o "$out" -p "/usr/bin/python3"
chmod 755 "$out"
"$out" --check
