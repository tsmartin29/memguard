#!/bin/bash
# Install memguard, heavyrun, afterburner and tidy for the current user and load the launchd job.
# Hangar (the optional menu-bar app) is built separately: hangar/build.sh
#   ./install.sh              install (idempotent; keeps your existing config.json and rank.txt)
#   ./install.sh --print      only print the launchd plist that would be installed
#   ./install.sh --uninstall  unload the job and remove the plist (your ~/.config/memguard data stays)
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${MEMGUARD_HOME:-$HOME/.config/memguard}"
LABEL="com.example.memguard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
render(){ sed "s#__HOME__#$HOME#g; s#$HOME/.config/memguard#$DEST#g" "$HERE/launchd/com.example.memguard.plist.template"; }

case "${1:-}" in
  --print) render; exit 0;;
  --uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    [ -e "$PLIST" ] && mv "$PLIST" "$HOME/.Trash/$LABEL.plist.$(date +%s)"
    echo "Unloaded. Files in $DEST were left alone."; exit 0;;
esac

mkdir -p "$DEST" "$HOME/Library/LaunchAgents" "$HOME/.local/bin"
FILES="memguard.py heavyrun timing_probe.py usagegate.py usage_statusline.py afterburner.py afterburner tidy.py tidy"
for f in $FILES; do cp "$HERE/$f" "$DEST/"; chmod +x "$DEST/$f"; done
[ -e "$DEST/config.json" ] || cp "$HERE/config.example.json" "$DEST/config.json"
[ -e "$DEST/rank.txt" ]    || cp "$HERE/rank.example.txt"    "$DEST/rank.txt"
for f in heavyrun afterburner tidy; do ln -sf "$DEST/$f" "$HOME/.local/bin/$f"; done
render > "$PLIST"; plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Check it with:  python3 $DEST/memguard.py --status"
echo "Queue a heavy job with:     heavyrun \"my-session: render\" <command...>   (make sure ~/.local/bin is on your PATH)"
echo "More CPU for a while:       afterburner on 2h      Clean, save, back up:  tidy --dry-run   (set the \"tidy\" block in config.json first)"
echo "Menu-bar app (optional):    $HERE/hangar/build.sh"
