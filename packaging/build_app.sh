#!/usr/bin/env bash
#
# Assemble EmailReader.app from the Python backend + the Swift shell, then
# (optionally) code-sign, notarize and package a DMG.
#
# Usage:
#   packaging/build_app.sh                       # build unsigned .app into dist/
#   SIGN_IDENTITY="Developer ID Application: …" packaging/build_app.sh
#   SIGN_IDENTITY=… NOTARY_PROFILE=… MAKE_DMG=1 packaging/build_app.sh
#
# Requires: an active Python venv with PyInstaller, and a Swift toolchain.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Use the repo's venv without requiring it to be pre-activated: if pyinstaller isn't
# already on PATH but .venv has it, prepend .venv/bin. (Non-interactive shells don't
# inherit an activated venv, and set -e turns the resulting "command not found" into a
# silent-looking abort.) Override the location with VENV=/path/to/venv if needed.
VENV="${VENV:-$REPO_ROOT/.venv}"
if ! command -v pyinstaller >/dev/null 2>&1 && [ -x "$VENV/bin/pyinstaller" ]; then
  export PATH="$VENV/bin:$PATH"
fi
if ! command -v pyinstaller >/dev/null 2>&1; then
  echo "pyinstaller not found. Activate the venv or set VENV=/path/to/venv (looked in $VENV)." >&2
  exit 1
fi

APP_NAME="Niti"
APP_DIR="dist/${APP_NAME}.app"
CONTENTS="$APP_DIR/Contents"

echo "==> [1/5] Building Python backend (PyInstaller)"
pyinstaller packaging/email_reader_backend.spec --noconfirm --log-level WARN

# Selfcheck the FROZEN binary before assembling: import-checks lazily-imported deps
# (caldav's PEP 562 API) and opens a throwaway DB. Catches "works in dev, silently
# dead in the .app" packaging gaps at build time instead of at the user's desk.
echo "==> [1.5/5] Selfcheck of the frozen backend"
SELFCHECK_TMP="$(mktemp -d)"
EMAIL_READER_DATA_DIR="$SELFCHECK_TMP" \
DATABASE_URL="sqlite+aiosqlite:///$SELFCHECK_TMP/selfcheck.db" \
  "dist/email-reader-backend/email-reader-backend" selfcheck
rm -rf "$SELFCHECK_TMP"

echo "==> [2/5] Building Swift shell (release)"
( cd macos && swift build -c release )
SWIFT_BIN="macos/.build/release/EmailReaderApp"
[ -x "$SWIFT_BIN" ] || { echo "Swift binary not found at $SWIFT_BIN"; exit 1; }

echo "==> [3/5] Assembling $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources/backend"
cp packaging/Info.plist "$CONTENTS/Info.plist"
cp "$SWIFT_BIN" "$CONTENTS/MacOS/EmailReaderApp"
# Copy the whole onedir backend (binary + _internal) into Resources/backend.
cp -R dist/email-reader-backend/. "$CONTENTS/Resources/backend/"
# App icon (Niti monogram), generated from packaging/make_icon.py.
python packaging/make_icon.py "$CONTENTS/Resources/AppIcon.icns"
# Menu-bar status icon (Niti "N" monogram, template image).
python packaging/make_menubar_icon.py "$CONTENTS/Resources/MenuBarIcon.png"

echo "==> [4/5] Code signing"
if [ -n "${SIGN_IDENTITY:-}" ]; then
    # Sign nested binaries first (deep), then the app bundle, with hardened runtime.
    codesign --force --deep --options runtime --timestamp \
        --sign "$SIGN_IDENTITY" "$CONTENTS/Resources/backend/email-reader-backend"
    codesign --force --options runtime --timestamp \
        --sign "$SIGN_IDENTITY" "$APP_DIR"
    codesign --verify --deep --strict --verbose=2 "$APP_DIR"
else
    echo "    SIGN_IDENTITY not set -> ad-hoc signing (local use only)."
    codesign --force --deep --sign - "$APP_DIR"
fi
# Finder keeps the icon it cached for this path (a placeholder after rm -rf + rebuild)
# until the bundle's mtime changes.
touch "$APP_DIR"

echo "==> [5/5] Packaging"
if [ "${MAKE_DMG:-0}" = "1" ]; then
    DMG="dist/${APP_NAME}.dmg"
    rm -f "$DMG"
    # Stage the .app next to an /Applications symlink so the volume window offers the
    # usual drag-to-install gesture instead of leaving the user to move it by hand.
    STAGE="$(mktemp -d)"
    cp -R "$APP_DIR" "$STAGE/${APP_NAME}.app"
    ln -s /Applications "$STAGE/Applications"
    hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
    rm -rf "$STAGE"
    echo "    DMG: $DMG"
    if [ -n "${NOTARY_PROFILE:-}" ]; then
        echo "    Notarizing (this can take a few minutes)…"
        xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
        xcrun stapler staple "$DMG"
    else
        echo "    NOTARY_PROFILE not set -> skipping notarization."
    fi
fi

echo "Done: $APP_DIR"
