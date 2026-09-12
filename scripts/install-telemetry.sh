#!/bin/sh
# Install only the optional SDK runtime, never hook sources or a DSN.
set -eu
umask 077
ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
VENV="${HOME:?HOME must be set}/.local/share/agent-hooks/venv"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"'
"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements-telemetry.txt"
"$VENV/bin/python" -c 'import sentry_sdk; print("Optional hook telemetry SDK ready: " + sentry_sdk.VERSION)'
