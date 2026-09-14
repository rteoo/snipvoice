#!/bin/sh
set -eu

# Use the active, already provisioned Apple toolchain. Never install or fetch tools.
if [ "$(uname -s)" != Darwin ]; then
    echo "The macOS capture helper must be built on macOS with an existing Apple SDK." >&2
    exit 1
fi
if ! command -v xcrun >/dev/null 2>&1; then
    echo "Apple xcrun is unavailable. Provision the Apple command-line developer tools before building." >&2
    exit 1
fi
native_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
sdk=$(xcrun --sdk macosx --show-sdk-path)
compiler=$(xcrun --sdk macosx --find swiftc)
architecture=$(uname -m)
case "$architecture" in
    arm64|x86_64) ;;
    *) echo "Unsupported macOS build architecture: $architecture" >&2; exit 1 ;;
esac
mkdir -p "$native_dir/bin"
"$compiler" -swift-version 5 -O -sdk "$sdk" \
    -target "$architecture-apple-macosx14.4" \
    -framework Foundation -framework CoreAudio -framework AudioToolbox -framework AVFoundation \
    "$native_dir/macos_capture.swift" -o "$native_dir/bin/snipvoice-capture"

# Signing and app usage-description keys belong to the parent packaging step.
"$native_dir/bin/snipvoice-capture" --self-test >/dev/null
