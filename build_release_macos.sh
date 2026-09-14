#!/bin/bash
# Build the macOS release: a menu-bar-only "Snipvoice.app" bundle.
#
# Mirrors build_release.bat where it applies (stage into a temp dist first, swap
# the shipping bundle only on success, keep the previous one until the swap is
# done) and drops the Windows-only steps: there is no Startup shortcut here (the
# tray toggle writes a LaunchAgent) and no dist-side snippets.json to preserve
# (user data lives in ~/.snipvoice).
#
# Usage:
#   ./build_release_macos.sh
#   PYTHON=/path/to/venv/bin/python ./build_release_macos.sh
#   CODESIGN_IDENTITY="Developer ID Application: ..." ./build_release_macos.sh
#
# Ad-hoc rebuilds and signing-identity changes invalidate the bundle's TCC
# grants (Input Monitoring / Accessibility) — see source/docs/development.md.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="Snipvoice"
BUNDLE_ID="com.snipvoice"
# A stable signing identity is what lets the bundle keep its TCC grants across
# rebuilds: ad-hoc signing has no identity, so macOS pins Input Monitoring and
# Accessibility to the binary's cdhash and every build silently revokes them.
# Try the local development identity before falling back to ad-hoc signing so
# a fresh checkout still builds without a certificate.
DEFAULT_SIGN_IDENTITY="Snipvoice Dev"
DIST_ROOT="$REPO_DIR/dist"
TARGET_APP="$DIST_ROOT/$APP_NAME.app"
PREVIOUS_APP="$DIST_ROOT/$APP_NAME.app.previous"
PYTHON="${PYTHON:-python3}"
APP_VERSION="$(sed -n '/^Version:[[:space:]]*/ { s/^Version:[[:space:]]*//; p; q; }' \
    "$REPO_DIR/source/snipvoice.pyw")"
RELEASE_CHANNEL="$(sed -n '/^Channel:[[:space:]]*/ { s/^Channel:[[:space:]]*//; p; q; }' \
    "$REPO_DIR/source/snipvoice.pyw")"

if [[ ! "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid or missing app version in source/snipvoice.pyw: '$APP_VERSION'" >&2
    exit 1
fi
if [[ "$RELEASE_CHANNEL" != "stable" && "$RELEASE_CHANNEL" != "beta" ]]; then
    echo "Invalid or missing release channel in source/snipvoice.pyw: '$RELEASE_CHANNEL'" >&2
    exit 1
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This script builds the macOS bundle and only runs on macOS." >&2
    exit 1
fi

if ! "$PYTHON" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "PyInstaller not found for '$PYTHON'." >&2
    echo "Install it with: $PYTHON -m pip install pyinstaller" >&2
    exit 1
fi

if pgrep -f "$APP_NAME.app/Contents/MacOS/" >/dev/null 2>&1; then
    echo "\"$APP_NAME\" is currently running."
    echo "Quit it from the menu bar before rebuilding so the bundle can be replaced safely."
    exit 1
fi

WORK_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/snipvoice_build.XXXXXX")"
trap 'rm -rf "$WORK_ROOT"' EXIT

STAGING_ROOT="$WORK_ROOT/dist"
STAGED_APP="$STAGING_ROOT/$APP_NAME.app"

# --- App icon -------------------------------------------------------------
# The repo ships a 256x256 .ico (Windows). Convert that source directly instead
# of constructing a partial iconset: current iconutil rejects iconsets without
# the 512px slots, and upscaling the source would only manufacture fake detail.
ICNS="$WORK_ROOT/$APP_NAME.icns"
sips -s format icns "$REPO_DIR/source/snipvoice.ico" --out "$ICNS" >/dev/null

# --- Package --------------------------------------------------------------
echo "Packaging $APP_NAME $APP_VERSION ($RELEASE_CHANNEL) ..."
VOICE_COLLECT_ARGS=()
if ! "$PYTHON" -c "import sounddevice, soxr, transcribe_cpp, transcribe_cpp_native" >/dev/null 2>&1; then
    echo "Voice release dependencies are missing." >&2
    echo "Install them with: $PYTHON -m pip install -r source/requirements-voice.txt" >&2
    exit 1
fi
VOICE_COLLECT_ARGS=(--collect-all sounddevice --collect-all soxr --copy-metadata soxr --collect-all transcribe_cpp --collect-all transcribe_cpp_native)
"$PYTHON" -m PyInstaller --noconfirm --clean --windowed --onedir \
    --distpath "$STAGING_ROOT" \
    --workpath "$WORK_ROOT/build" \
    --specpath "$WORK_ROOT" \
    --name "$APP_NAME" \
    --icon "$ICNS" \
    --osx-bundle-identifier "$BUNDLE_ID" \
    --add-data "$REPO_DIR/source/snipvoice.ico:." \
    --add-data "$REPO_DIR/THIRD_PARTY_NOTICES.md:." \
    --add-data "$REPO_DIR/LICENSE:." \
    --hidden-import pystray._darwin \
    "${VOICE_COLLECT_ARGS[@]}" \
    --exclude-module torch \
    --exclude-module torchvision \
    --exclude-module torchaudio \
    --exclude-module cv2 \
    --exclude-module transformers \
    --exclude-module onnxruntime \
    --exclude-module scipy \
    "$REPO_DIR/source/snipvoice.pyw"

if [[ ! -d "$STAGED_APP" ]]; then
    echo "Packaging failed: no app bundle was produced. The existing dist was left unchanged." >&2
    exit 1
fi

BUNDLE_ICON_NAME="$(plutil -extract CFBundleIconFile raw \
    "$STAGED_APP/Contents/Info.plist" 2>/dev/null || true)"
BUNDLE_ICON="$STAGED_APP/Contents/Resources/$APP_NAME.icns"
if [[ "$BUNDLE_ICON_NAME" != "$APP_NAME.icns" || ! -s "$BUNDLE_ICON" ]]; then
    echo "Packaging failed: the app bundle does not contain or reference $APP_NAME.icns." >&2
    echo "The existing dist was left unchanged." >&2
    exit 1
fi

# Menu-bar-only: no Dock icon, no app menu, no window on launch. PyInstaller has
# no CLI flag for extra Info.plist keys, so it is written after the build —
# which breaks the seal PyInstaller put on the bundle, hence the re-sign below.
plutil -replace LSUIElement -bool true "$STAGED_APP/Contents/Info.plist" 2>/dev/null \
    || plutil -insert LSUIElement -bool true "$STAGED_APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$APP_VERSION" \
    "$STAGED_APP/Contents/Info.plist" 2>/dev/null \
    || plutil -insert CFBundleShortVersionString -string "$APP_VERSION" \
        "$STAGED_APP/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$APP_VERSION" \
    "$STAGED_APP/Contents/Info.plist" 2>/dev/null \
    || plutil -insert CFBundleVersion -string "$APP_VERSION" \
        "$STAGED_APP/Contents/Info.plist"
plutil -replace SnipvoiceReleaseChannel -string "$RELEASE_CHANNEL" \
    "$STAGED_APP/Contents/Info.plist" 2>/dev/null \
    || plutil -insert SnipvoiceReleaseChannel -string "$RELEASE_CHANNEL" \
        "$STAGED_APP/Contents/Info.plist"
plutil -replace NSMicrophoneUsageDescription -string "O Snipvoice usa o microfone só para o ditado local, e só quando a entrada por voz está ligada." \
    "$STAGED_APP/Contents/Info.plist" 2>/dev/null \
    || plutil -insert NSMicrophoneUsageDescription -string "O Snipvoice usa o microfone só para o ditado local, e só quando a entrada por voz está ligada." \
        "$STAGED_APP/Contents/Info.plist"

SIGN_IDENTITY="${CODESIGN_IDENTITY:-}"
if [[ -z "$SIGN_IDENTITY" ]] \
    && security find-identity -v -p codesigning 2>/dev/null | grep -qF "$DEFAULT_SIGN_IDENTITY"; then
    SIGN_IDENTITY="$DEFAULT_SIGN_IDENTITY"
fi
if [[ -z "$SIGN_IDENTITY" ]]; then
    SIGN_IDENTITY="-"
    echo "Signing ad-hoc: macOS will re-ask for Input Monitoring/Accessibility after this build."
else
    echo "Signing with identity: $SIGN_IDENTITY"
fi

if ! codesign --force --deep --sign "$SIGN_IDENTITY" "$STAGED_APP" 2>&1; then
    if [[ "$SIGN_IDENTITY" == "-" ]]; then
        echo "Ad-hoc signing failed; the bundle would not launch. dist left unchanged." >&2
        exit 1
    fi
    echo "Warning: signing with \"$SIGN_IDENTITY\" failed; falling back to ad-hoc." >&2
    echo "         errSecInternalComponent here means codesign could not reach the" >&2
    echo "         private key: run this script from your own terminal once and allow" >&2
    echo "         the keychain prompt, or add codesign to the key's partition list." >&2
    SIGN_IDENTITY="-"
    if ! codesign --force --deep --sign - "$STAGED_APP" 2>&1; then
        echo "Ad-hoc signing failed too. dist left unchanged." >&2
        exit 1
    fi
fi

# The plutil edit above breaks the seal PyInstaller applied, so an unsigned or
# half-signed bundle is not a cosmetic problem: on Apple silicon macOS kills a
# bundle whose signature does not match its Info.plist. Never promote one.
if ! codesign --verify --deep --strict "$STAGED_APP"; then
    echo "The staged bundle failed signature verification. dist left unchanged." >&2
    exit 1
fi

if ! "$STAGED_APP/Contents/MacOS/$APP_NAME" --voice-runtime-probe; then
    echo "The staged voice runtime probe failed. dist left unchanged." >&2
    exit 1
fi

# --- Promote --------------------------------------------------------------
mkdir -p "$DIST_ROOT"
rm -rf "$PREVIOUS_APP"
if [[ -d "$TARGET_APP" ]]; then
    mv "$TARGET_APP" "$PREVIOUS_APP"
fi

if ! ditto "$STAGED_APP" "$TARGET_APP"; then
    echo "Failed to promote the staged bundle into dist." >&2
    if [[ -d "$PREVIOUS_APP" ]]; then
        echo "Restoring the previous bundle..."
        rm -rf "$TARGET_APP"
        mv "$PREVIOUS_APP" "$TARGET_APP"
    fi
    exit 1
fi
rm -rf "$PREVIOUS_APP"

echo
echo "Packaging complete: $TARGET_APP"
echo "User data lives in ~/.snipvoice (override with SNIPVOICE_HOME)."
echo
if [[ "$SIGN_IDENTITY" == "-" ]]; then
    echo "The bundle is ad-hoc signed. If Gatekeeper blocks its first launch,"
    echo "right-click the app and choose Open once, or run:"
    echo "  xattr -dr com.apple.quarantine \"$TARGET_APP\""
    echo "Every ad-hoc rebuild changes the bundle's identity, so remove stale rows"
    echo "before re-granting Input Monitoring and Accessibility."
else
    echo "The bundle is signed with \"$SIGN_IDENTITY\". Keep using that identity so"
    echo "Input Monitoring and Accessibility grants survive future rebuilds."
fi
