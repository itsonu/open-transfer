#!/bin/sh
# M0 spike: build otble.app next to this script. Bluetooth permission is per app, so
# the tool must run as its own app (launched with `open`), not from a shell.
set -e
cd "$(dirname "$0")"
mkdir -p otble.app/Contents/MacOS
swiftc -O otble.swift -o otble.app/Contents/MacOS/otble
sed 's#<key>CFBundleName</key>#<key>CFBundleExecutable</key><string>otble</string><key>CFBundlePackageType</key><string>APPL</string><key>LSUIElement</key><true/><key>CFBundleName</key>#' \
    Info.plist > otble.app/Contents/Info.plist
codesign -s - -f otble.app
echo "Built otble.app. Run it and read its output:"
echo "  open -n otble.app --stdout /tmp/otble.log --stderr /tmp/otble.log --args serve   # or connect"
