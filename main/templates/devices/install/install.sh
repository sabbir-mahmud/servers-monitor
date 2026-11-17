{% autoescape off %}#!/bin/sh
# {{ site_name }}: connect this Mac or Linux computer.
#
# Downloaded from {{ server }} for one pairing code. It:
#   1. shows who will see this computer, and pairs only if you say yes;
#   2. installs a small check-in that runs as you every few minutes
#      (battery, storage, system version; no administrator rights needed);
#   3. checks in once so the dashboard shows the computer straight away.
#
# To remove it later:  curl -fsSL {{ server }}/d/uninstall.sh | sh
SERVER='{{ server }}'
CODE='{{ code }}'
{% endautoescape %}{% verbatim %}VERSION='1.0.0'

set -eu

say() { printf '%s\n' "$*"; }
fail() { printf '\n%s\n\n' "Couldn't connect this computer: $*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin) PLATFORM=macos; DIR="$HOME/Library/Application Support/NexGuard" ;;
  Linux) PLATFORM=linux; DIR="${XDG_DATA_HOME:-$HOME/.local/share}/nexguard" ;;
  *) fail "this works on a Mac or Linux. On Windows, use the PowerShell line instead." ;;
esac
command -v curl >/dev/null 2>&1 || fail "curl is missing. Install it, then paste the line again."
if [ "$(id -u)" = 0 ]; then
  fail "run this as the person who uses the computer, not with sudo."
fi

# --------------------------------------------------------------------- talk to the dashboard
# Answers come back as key=value lines (Accept: text/plain), so sed is all we need.
field() { printf '%s\n' "$1" | sed -n "s/^$2=//p" | head -n 1; }

json_str() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\000-\037')"; }

api() {  # api PATH JSON [TOKEN] -> sets BODY and STATUS
  _out="$(mktemp)"
  if [ -n "${3:-}" ]; then
    STATUS="$(curl -sS --max-time 20 -o "$_out" -w '%{http_code}' \
      -H 'Content-Type: application/json' -H 'Accept: text/plain' \
      -H "Authorization: Bearer $3" -A "nexguard-device-sh/$VERSION" \
      --data-binary "$2" "$SERVER$1")" || STATUS=000
  else
    STATUS="$(curl -sS --max-time 20 -o "$_out" -w '%{http_code}' \
      -H 'Content-Type: application/json' -H 'Accept: text/plain' \
      -A "nexguard-device-sh/$VERSION" --data-binary "$2" "$SERVER$1")" || STATUS=000
  fi
  BODY="$(cat "$_out")"
  rm -f "$_out"
}

# ------------------------------------------------------------------------- ask first
say ""
say "Connecting this computer to the dashboard at $SERVER"
api /device/v1/pair "{\"code\":\"$CODE\",\"platform\":\"$PLATFORM\",\"preview\":true}"
case "$STATUS" in
  200) ;;
  000) fail "can't reach $SERVER. Check the internet connection and try again." ;;
  *) msg="$(field "$BODY" error || true)"; fail "${msg:-the dashboard said HTTP $STATUS}" ;;
esac

say ""
say "  $(field "$BODY" disclosure)"
say ""
if [ "${NEXGUARD_YES:-}" = 1 ]; then
  answer=y
elif [ -r /dev/tty ]; then
  printf 'Allow this? Type y and press Enter (or just Enter to cancel): '
  read -r answer < /dev/tty || answer=n
else
  fail "this needs an answer. Open Terminal and paste the line there."
fi
case "$answer" in
  y|Y|yes|YES|Yes) ;;
  *) say ""; say "Cancelled. Nothing was changed, and the code still works."; exit 0 ;;
esac

# ------------------------------------------------------------------------------ pair
api /device/v1/pair "{\"code\":\"$CODE\",\"platform\":\"$PLATFORM\"}"
if [ "$STATUS" != 201 ]; then
  msg="$(field "$BODY" error || true)"; fail "${msg:-the dashboard said HTTP $STATUS}"
fi
TOKEN="$(field "$BODY" token)"
INTERVAL="$(field "$BODY" checkin_seconds)"
NAME="$(field "$BODY" device_name)"
ACCOUNT="$(field "$BODY" device_account)"
# Only these characters end up in files we later run, nothing else.
case "$TOKEN" in ngd_*) ;; *) fail "the dashboard sent an unexpected answer." ;; esac
printf '%s' "$TOKEN" | grep -q '^[A-Za-z0-9_-]*$' || fail "the dashboard sent an unexpected answer."
printf '%s' "$INTERVAL" | grep -q '^[0-9][0-9]*$' || INTERVAL=300

# ---------------------------------------------------------------------------- install
mkdir -p "$DIR"
chmod 700 "$DIR"
umask 077
cat > "$DIR/config" <<EOF
SERVER='$SERVER'
TOKEN='$TOKEN'
INTERVAL='$INTERVAL'
EOF

cat > "$DIR/checkin.sh" <<'CHECKIN'
#!/bin/sh
# NexGuard check-in: sends battery, storage and the system version to the dashboard.
# Runs every 5 minutes; actually reports as often as the dashboard asks (INTERVAL).
VERSION='1.0.0'
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/config"

now="$(date +%s)"
last="$(cat "$DIR/last" 2>/dev/null || echo 0)"
[ "${1:-}" = now ] || [ $((now - last)) -ge $((INTERVAL - 30)) ] || exit 0

json_str() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\000-\037')"; }
first() { head -n 1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'; }

pct=""; plugged=""
if [ "$(uname -s)" = Darwin ]; then
  platform=macos
  os="macOS $(sw_vers -productVersion 2>/dev/null)"
  name="$(scutil --get ComputerName 2>/dev/null || hostname)"
  chip="$(sysctl -n machdep.cpu.brand_string 2>/dev/null | first)"
  model="Mac${chip:+ · $chip}"
  batt="$(pmset -g batt 2>/dev/null || true)"
  pct="$(printf '%s\n' "$batt" | sed -n 's/.*[^0-9]\([0-9][0-9]*\)%.*/\1/p' | head -n 1)"
  if printf '%s\n' "$batt" | head -n 1 | grep -q 'AC Power'; then plugged=true; else plugged=false; fi
  disk=/System/Volumes/Data; [ -d "$disk" ] || disk=/
else
  platform=linux
  os="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | tr -d '"' | first)"
  [ -n "$os" ] || os="Linux $(uname -r)"
  name="$(hostname 2>/dev/null | cut -d. -f1)"
  vendor="$(first 2>/dev/null < /sys/devices/virtual/dmi/id/sys_vendor || true)"
  product="$(first 2>/dev/null < /sys/devices/virtual/dmi/id/product_name || true)"
  case "$product" in ""|"To Be Filled By O.E.M."|"System Product Name"|"Default string") product="$(uname -m)" ;; esac
  case "$product" in *"$vendor"*) model="$product" ;; *) model="${vendor:+$vendor }$product" ;; esac
  for b in /sys/class/power_supply/BAT*; do
    [ -r "$b/capacity" ] || continue
    pct="$(first < "$b/capacity")"
    if [ "$(first 2>/dev/null < "$b/status")" = Discharging ]; then plugged=false; else plugged=true; fi
    break
  done
  disk=/
fi
storage="$(df -kP "$disk" 2>/dev/null | awk 'NR==2 { printf "{\"total\":%.0f,\"free\":%.0f}", $2 * 1024, $4 * 1024 }')"

json="{\"platform\":\"$platform\",\"os_version\":$(json_str "$os"),\"model\":$(json_str "$model"),\"hostname\":$(json_str "$name"),\"app_version\":\"nexguard-device-sh $VERSION\""
[ -n "$pct" ] && json="$json,\"battery\":{\"percent\":$pct,\"charging\":$plugged}"
[ -n "$storage" ] && json="$json,\"storage\":$storage"
json="$json}"

out="$(mktemp)"
code="$(curl -sS --max-time 20 -o "$out" -w '%{http_code}' \
  -H 'Content-Type: application/json' -H 'Accept: text/plain' \
  -H "Authorization: Bearer $TOKEN" -A "nexguard-device-sh/$VERSION" \
  --data-binary "$json" "$SERVER/device/v1/checkin" 2>"$DIR/error")" || code=000
answer="$(cat "$out")"; rm -f "$out"

case "$code" in
  200)
    echo "$now" > "$DIR/last"
    rm -f "$DIR/error"
    new="$(printf '%s\n' "$answer" | sed -n 's/^checkin_seconds=//p' | head -n 1)"
    if printf '%s' "$new" | grep -q '^[0-9][0-9]*$' && [ "$new" != "$INTERVAL" ]; then
      sed "s/^INTERVAL=.*/INTERVAL='$new'/" "$DIR/config" > "$DIR/config.new" && mv "$DIR/config.new" "$DIR/config"
    fi
    ;;
  401)
    # Removed on the dashboard (or paired again elsewhere): stop for good, tidy up.
    sh "$DIR/uninstall.sh" quiet
    ;;
  *)
    printf '%s %s\n' "$(date)" "HTTP $code $answer" >> "$DIR/error"
    ;;
esac
CHECKIN

cat > "$DIR/uninstall.sh" <<'UNINSTALL'
#!/bin/sh
# Stop NexGuard check-ins on this computer and remove everything it installed.
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
rm -rf "$DIR"
[ "${1:-}" = quiet ] || echo "NexGuard is removed from this computer."
UNINSTALL
chmod 700 "$DIR/checkin.sh" "$DIR/uninstall.sh"

# ------------------------------------------------------------------- run every 5 minutes
if [ "${NEXGUARD_NO_SCHEDULE:-}" = 1 ]; then
  # Pair and check in once, but don't schedule anything (tests; or run checkin.sh yourself).
  SCHEDULE="only when you run $DIR/checkin.sh"
elif [ "$PLATFORM" = macos ]; then
  PLIST="$HOME/Library/LaunchAgents/com.nexguard.device.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  umask 022
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.nexguard.device</string>
  <key>ProgramArguments</key>
  <array><string>/bin/sh</string><string>$DIR/checkin.sh</string></array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>/dev/null</string>
</dict>
</plist>
EOF
  JOB="gui/$(id -u)/com.nexguard.device"
  launchctl bootout "$JOB" 2>/dev/null || true
  # bootout finishes in the background: right after it, bootstrap often fails with
  # "Input/output error". Try a few times, then the old `load`, and check it took.
  tries=0
  until launchctl print "$JOB" >/dev/null 2>&1 || [ "$tries" -ge 5 ]; do
    tries=$((tries + 1))
    LAUNCH_ERR=$(launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>&1) || sleep 1
  done
  launchctl print "$JOB" >/dev/null 2>&1 || launchctl load -w "$PLIST" 2>/dev/null || true
  SCHEDULE="every 5 minutes while you're signed in"
  if ! launchctl print "$JOB" >/dev/null 2>&1; then
    NOT_SCHEDULED="macOS didn't start the background check-in (${LAUNCH_ERR:-no reason given}). It starts by itself the next time you sign in to this Mac. If it still doesn't, allow \"sh\" in System Settings > General > Login Items."
  fi
elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$UNITS"
  umask 022
  cat > "$UNITS/nexguard-device.service" <<EOF
[Unit]
Description=NexGuard device check-in

[Service]
Type=oneshot
ExecStart=/bin/sh "$DIR/checkin.sh"
EOF
  cat > "$UNITS/nexguard-device.timer" <<EOF
[Unit]
Description=NexGuard device check-in every 5 minutes

[Timer]
OnStartupSec=1min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now nexguard-device.timer >/dev/null 2>&1 || true
  SCHEDULE="every 5 minutes while you're signed in"
  if ! systemctl --user is-active --quiet nexguard-device.timer; then
    NOT_SCHEDULED="the background check-in didn't start. Check it with: systemctl --user status nexguard-device.timer"
  fi
elif command -v crontab >/dev/null 2>&1; then
  { crontab -l 2>/dev/null | grep -v nexguard || true
    echo "*/5 * * * * /bin/sh '$DIR/checkin.sh' >/dev/null 2>&1 # nexguard"
    echo "@reboot /bin/sh '$DIR/checkin.sh' >/dev/null 2>&1 # nexguard"
  } | crontab -
  SCHEDULE="every 5 minutes"
else
  fail "this computer has neither systemd nor cron to run the check-in."
fi

# ------------------------------------------------------------------- first check-in
sh "$DIR/checkin.sh" now
say ""
if [ -s "$DIR/error" ]; then
  say "Paired as \"$NAME\", but the first check-in failed. It will try again $SCHEDULE."
else
  say "Done. This computer is connected as \"$NAME\" in $ACCOUNT."
  say "It reports $SCHEDULE. You can close this window."
fi
if [ -n "${NOT_SCHEDULED:-}" ]; then
  say ""
  say "Note: $NOT_SCHEDULED"
fi
say "To remove it later, paste:  curl -fsSL $SERVER/d/uninstall.sh | sh"
say ""
{% endverbatim %}
