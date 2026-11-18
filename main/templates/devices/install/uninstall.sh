{% verbatim %}#!/bin/sh
# Stop NexGuard on this Mac or Linux computer (the agent or the check-in script) and remove
# everything it installed. The dashboard still lists the computer until someone removes it.
if [ "$(uname -s)" = Darwin ]; then
  DIR="$HOME/Library/Application Support/NexGuard"
  # Only jobs this install scheduled: launchd jobs live in the login session, not in
  # $HOME, so an install made under another HOME (tests) must not stop the real one.
  for job in com.nexguard.device com.nexguard.agent; do
    PLIST="$HOME/Library/LaunchAgents/$job.plist"
    if [ -f "$PLIST" ]; then
      launchctl bootout "gui/$(id -u)/$job" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
      rm -f "$PLIST"
    fi
  done
else
  DIR="${XDG_DATA_HOME:-$HOME/.local/share}/nexguard"
  UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  if [ -f "$UNITS/nexguard-device.timer" ]; then
    systemctl --user disable --now nexguard-device.timer >/dev/null 2>&1 || true
    rm -f "$UNITS/nexguard-device.timer" "$UNITS/nexguard-device.service"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  if [ -f "$UNITS/nexguard-agent.service" ]; then
    systemctl --user disable --now nexguard-agent.service >/dev/null 2>&1 || true
    rm -f "$UNITS/nexguard-agent.service"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  # Only this install's lines (crontab is per user, not per $HOME, like launchd above).
  if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -qF "$DIR/"; then
    crontab -l 2>/dev/null | grep -vF "$DIR/" | crontab - || true
  fi
  if [ -f "$DIR/agent.pid" ]; then
    kill "$(cat "$DIR/agent.pid")" 2>/dev/null || true
  fi
fi
if [ -d "$DIR" ]; then
  rm -rf "$DIR"
  echo "NexGuard is removed from this computer."
else
  echo "NexGuard wasn't installed for this user."
fi
{% endverbatim %}