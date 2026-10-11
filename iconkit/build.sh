#!/bin/bash
# Builds the CoreGraphics icon compositor used by iconkit.py.
#
# The binary is linked under a unique temporary name and then renamed over
# build/compose, so a concurrent build or a running compose never sees a half
# written file. iconkit.py also serializes builds with a lock on build/.lock.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
SWIFTC="$(command -v swiftc || xcrun --find swiftc)"
TMP="$(mktemp build/compose.XXXXXX)"
trap 'rm -f "$TMP"' EXIT
"$SWIFTC" -O -o "$TMP" compose.swift
chmod 755 "$TMP"
mv -f "$TMP" build/compose
trap - EXIT
