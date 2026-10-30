#!/bin/bash
# Build Hangar.app, the menu-bar remote for memguard (afterburner, tidy, save, backup). Ad-hoc signed, LSUIElement, no Xcode project.
#   ./build.sh                 build into ./build and install to ~/Applications/Hangar.app (an old copy goes to the Trash, never rm)
#   ./build.sh --no-install    build only (HANGAR_OUT=<dir> picks the build folder)
# Needs the Xcode command line tools (swiftc) and macOS 13+.
# Screenshots without a menu-bar item: HANGAR_SHOT=<dir> <app>/Contents/MacOS/Hangar
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"; OUT="${HANGAR_OUT:-$HERE/build}"; APP="$OUT/Hangar.app"; DEST="$HOME/Applications/Hangar.app"
BUNDLE_ID="${HANGAR_BUNDLE_ID:-com.example.memguard.hangar}"
mkdir -p "$OUT"; [ -e "$APP" ] && mv "$APP" "$HOME/.Trash/Hangar.app.build.$(date +%Y%m%d-%H%M%S)"
mkdir -p "$APP/Contents/MacOS"
nice -n 10 swiftc -O -o "$APP/Contents/MacOS/Hangar" "$HERE/Hangar.swift" 2>&1 | grep -v "^$" || true
[ -x "$APP/Contents/MacOS/Hangar" ] || { echo "build failed"; exit 1; }
cat > "$APP/Contents/Info.plist" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Hangar</string>
<key>CFBundleDisplayName</key><string>Hangar</string>
<key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
<key>CFBundleExecutable</key><string>Hangar</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
P
plutil -lint "$APP/Contents/Info.plist" >/dev/null
codesign --force --sign - "$APP"
if [ "${1:-}" = "--no-install" ]; then echo "built $APP"; exit 0; fi
mkdir -p "$HOME/Applications"
if [ -e "$DEST" ]; then pkill -x Hangar 2>/dev/null || true; mv "$DEST" "$HOME/.Trash/Hangar.app.$(date +%Y%m%d-%H%M%S)"; fi
mv "$APP" "$DEST"
echo "installed $DEST (open it: a plane appears in the menu bar)"
