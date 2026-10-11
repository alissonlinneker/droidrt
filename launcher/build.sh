#!/bin/sh
# Builds the droidrt launcher (arm64) into launcher/build/droidrt-launcher.
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
out="$here/build"
mkdir -p "$out"

# Build and sign under a temporary name, then rename: a concurrent install copying the
# launcher never sees a half written or unsigned binary.
tmp="$out/.droidrt-launcher.$$"
trap 'rm -f "$tmp"' EXIT INT TERM

swiftc -O \
    -target arm64-apple-macos13.0 \
    -framework AppKit \
    -o "$tmp" \
    "$here/main.swift"

codesign -s - --force "$tmp"
mv -f "$tmp" "$out/droidrt-launcher"
echo "$out/droidrt-launcher"
