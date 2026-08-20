#!/usr/bin/env bash
# Thin wrapper. The real work is in setup.py, which also runs on Windows.
cd "$(dirname "$0")" || exit 1
exec python3 setup.py "$@"
