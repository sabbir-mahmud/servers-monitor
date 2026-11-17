{% verbatim %}#!/bin/sh
# Stop NexGuard check-ins on this Mac or Linux computer and remove everything it installed.
# The dashboard still lists the computer until someone removes it there.
if [ "$(uname -s)" = Darwin ]; then
  DIR="$HOME/Library/Application Support/NexGuard"
  PLIST="$HOME/Library/LaunchAgents/com.nexguard.device.plist"
  launchctl bootout "gui/$(id -u)/com.nexguard.device" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
else
  DIR="${XDG_DATA_HOME:-$HOME/.local/share}/nexguard"
  UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  if [ -f "$UNITS/nexguard-device.timer" ]; then
    systemctl --user disable --now nexguard-device.timer >/dev/null 2>&1 || true
    rm -f "$UNITS/nexguard-device.timer" "$UNITS/nexguard-device.service"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -q nexguard; then
    crontab -l 2>/dev/null | grep -v nexguard | crontab - || true
  fi
fi
if [ -d "$DIR" ]; then
  rm -rf "$DIR"
  echo "NexGuard is removed from this computer."
else
  echo "NexGuard wasn't installed for this user."
fi
{% endverbatim %}
