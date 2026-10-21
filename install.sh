#!/bin/bash
# Install memguard + heavyrun for the current user and load the launchd job.
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
cp "$HERE/memguard.py" "$HERE/heavyrun" "$HERE/timing_probe.py" "$DEST/"; chmod +x "$DEST/memguard.py" "$DEST/heavyrun" "$DEST/timing_probe.py"
[ -e "$DEST/config.json" ] || cp "$HERE/config.example.json" "$DEST/config.json"
[ -e "$DEST/rank.txt" ]    || cp "$HERE/rank.example.txt"    "$DEST/rank.txt"
ln -sf "$DEST/heavyrun" "$HOME/.local/bin/heavyrun"
render > "$PLIST"; plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Check it with:  python3 $DEST/memguard.py --status"
echo "Queue a heavy job with:     heavyrun \"my-session: render\" <command...>   (make sure ~/.local/bin is on your PATH)"
