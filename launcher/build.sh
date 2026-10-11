#!/bin/sh
# Builds the droidrt launcher (arm64) into launcher/build/droidrt-launcher.
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
out="$here/build"
mkdir -p "$out"

swiftc -O \
    -target arm64-apple-macos13.0 \
    -framework AppKit \
    -o "$out/droidrt-launcher" \
    "$here/main.swift"

codesign -s - --force "$out/droidrt-launcher"
echo "$out/droidrt-launcher"
