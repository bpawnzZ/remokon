#!/bin/sh
# Run the desktop client. The INTERPRETER MATTERS: `python3` on PATH here is a
# miniforge build with no `gi`; the system python is the one that has GTK4.
exec /usr/bin/python3 "$(dirname "$0")/ui.py" "$@"
