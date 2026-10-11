#!/bin/bash
# Builds the CoreGraphics icon compositor used by iconkit.py.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
SWIFTC="$(command -v swiftc || xcrun --find swiftc)"
"$SWIFTC" -O -o build/compose.tmp compose.swift
mv build/compose.tmp build/compose
