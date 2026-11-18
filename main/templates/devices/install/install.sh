{% autoescape off %}#!/bin/sh
# {{ site_name }}: connect this Mac or Linux computer.
#
# Downloaded from {{ server }} for one pairing code. It:
#   1. shows who will see this computer, and pairs only if you say yes;
#   2. installs the NexGuard agent from that dashboard as a protected service (asks for
#      this computer's administrator password): health (CPU, memory, GPU, battery,
#      storage, network, system version), and screen time, websites and website blocking
#      if the dashboard turns them on. Without an administrator, the same agent running as
#      you (no blocking); if the dashboard has no agent, a health-only check-in script;
#   3. checks in once so the dashboard shows the computer straight away.
#
# To remove it later:  curl -fsSL {{ server }}/d/uninstall.sh | sh
SERVER='{{ server }}'
CODE='{{ code }}'
{% endautoescape %}{% verbatim %}VERSION='1.0.0'

set -eu

say() { printf '%s\n' "$*"; }
fail() { printf '\n%s\n\n' "Couldn't connect this computer: $*" >&2; exit 1; }

# Full Disk Access (Mac): macOS keeps browsers' history from other programs until a person
# allows it in System Settings; no program can switch it on for itself. The running agent
# writes to access.json which browsers it still can't read, so this asks only when needed
# and sees the moment it's allowed.
no_access() { sed -n 's/.*"no_access":\[\([^]]*\)\].*/\1/p' "$1" 2>/dev/null | tr -d '"' | sed 's/,/, /g'; }
disk_access() {  # disk_access PROGRAM ACCESS_FILE
  [ "$PLATFORM" = macos ] || return 0
  ( : < /dev/tty ) 2>/dev/null || return 0
  _i=0
  until [ -f "$2" ] || [ "$_i" -ge 30 ]; do sleep 1; _i=$((_i + 1)); done
  _blocked="$(no_access "$2")"
  [ -n "$_blocked" ] || return 0
  say ""
  say "One more step, so websites show on the dashboard: allow NexGuard to read the"
  say "history of $_blocked. macOS only lets you do this yourself."
  say "System Settings (Full Disk Access) and a Finder window with \"nexguard\" open now:"
  say "  drag \"nexguard\" from Finder into the list, then make sure its switch is on."
  say "  (Or click +, press Command Shift G and paste: $1)"
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null || true
  open -R "$1" 2>/dev/null || true
  say ""
  say "Waiting for it... Press Control C to skip: everything else is done, and the device"
  say "page shows these steps until it's allowed."
  _i=0
  while [ "$_i" -lt 120 ]; do  # up to 10 minutes
    sleep 5
    _i=$((_i + 1))
    if [ -z "$(no_access "$2")" ]; then
      say ""
      say "Full Disk Access is on. Websites visited from now on will show on the dashboard."
      return 0
    fi
  done
  say ""
  say "Not allowed yet. Websites start by themselves once it is; the device page has the steps."
}

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

# ------------------------------------------------------------------- the agent, if offered
# The same program an administrator can install as a service, here running as you: it
# can't block websites (that needs administrator rights), everything else works.
AGENT=""; AGENT_TMP=""
case "$(uname -m)" in
  arm64|aarch64) ARCH=arm64 ;;
  x86_64|amd64) ARCH=amd64 ;;
  *) ARCH="" ;;
esac
GOOS=linux; [ "$PLATFORM" = macos ] && GOOS=darwin
if [ -n "$ARCH" ] && [ "${NEXGUARD_NO_AGENT:-}" != 1 ]; then
  BIN="nexguard-$GOOS-$ARCH"
  _bin="$(mktemp)"; _sums="$(mktemp)"
  if curl -fsS --max-time 300 -o "$_bin" "$SERVER/d/agent/$BIN" 2>/dev/null &&
     curl -fsS --max-time 20 -o "$_sums" "$SERVER/d/agent/SHA256SUMS" 2>/dev/null; then
    want="$(awk -v f="$BIN" '$2 == f || $2 == "*" f { print $1 }' "$_sums" | head -n 1)"
    got="$( (shasum -a 256 "$_bin" 2>/dev/null || sha256sum "$_bin") | awk '{ print $1 }')"
    if [ -n "$want" ] && [ "$want" = "$got" ]; then
      AGENT_TMP="$_bin"; _bin=""
    else
      say "(The agent download didn't match its checksum: installing the check-in script.)"
    fi
  fi
  rm -f ${_bin:+"$_bin"} "$_sums"
fi

# ----------------------------------------------- as an administrator: blocking included
# The protected service: it can block websites, and someone without the administrator
# password can't stop or remove it. `nexguard install` copies itself where only an
# administrator can change it.
ADMIN=""
if [ -n "$AGENT_TMP" ] && [ "${NEXGUARD_USER_INSTALL:-}" != 1 ] &&
   command -v sudo >/dev/null 2>&1 && ( : < /dev/tty ) 2>/dev/null; then
  SAID_HELLO=1
  say ""
  say "Connecting this computer to the dashboard at $SERVER"
  say ""
  say "NexGuard installs as a protected service, so it can also block websites."
  say "Enter the password you use to sign in to this computer (it needs to be an"
  say "administrator account). Without it, NexGuard installs for you only, with no blocking."
  chmod 755 "$AGENT_TMP"
  if sudo -p "Password for %u: " -v < /dev/tty; then
    ADMIN=1
  else
    say ""
    say "No administrator password: installing for you only, without website blocking."
  fi
fi
if [ -n "$ADMIN" ]; then
  # The agent shows what the dashboard will see (blocking included) and asks.
  ok=0; sudo "$AGENT_TMP" pair "$SERVER" "$CODE" < /dev/tty || ok=$?
  if [ "$ok" != 0 ]; then
    rm -f "$AGENT_TMP"
    say ""
    say "Nothing was installed. If you said no, the code still works."
    exit 1
  fi
  SYSTEM_DIR="/Library/Application Support/NexGuard"
  [ "$PLATFORM" = macos ] && { sudo rm -f "$SYSTEM_DIR/access.json" || true; }  # the new service writes it
  if ! sudo "$AGENT_TMP" install < /dev/null; then
    rm -f "$AGENT_TMP"
    fail "it's paired, but the service didn't start. Paste the line again to retry."
  fi
  rm -f "$AGENT_TMP"
  # Replace what ran as you before (the check-in script, or the agent without blocking).
  if [ -f "$DIR/uninstall.sh" ]; then sh "$DIR/uninstall.sh" quiet || true; fi
  say ""
  say "Done. NexGuard runs as a protected service on this computer and can block websites."
  disk_access "$SYSTEM_DIR/nexguard" "$SYSTEM_DIR/access.json"
  say ""
  say "You can close this window. To remove it later, paste:  curl -fsSL $SERVER/d/uninstall.sh | sh"
  say ""
  exit 0
fi

# --------------------------------------------- no administrator: the agent, running as you
if [ -n "$AGENT_TMP" ]; then
  if [ -z "${SAID_HELLO:-}" ]; then
    say ""
    say "Connecting this computer to the dashboard at $SERVER"
  fi
  NEW_DIR=""; [ -d "$DIR" ] || NEW_DIR=1
  mkdir -p "$DIR"
  chmod 700 "$DIR"
  chmod 700 "$AGENT_TMP" && mv "$AGENT_TMP" "$DIR/nexguard.new" && mv "$DIR/nexguard.new" "$DIR/nexguard" ||
    fail "couldn't save the agent in $DIR."
  AGENT="$DIR/nexguard"
  agent() { "$AGENT" --user --dir "$DIR" "$@"; }
  # The agent shows what the dashboard will see (screen time and websites included, if
  # they can be turned on for this computer) and asks. It reads the answer from the
  # terminal, never from this script's input.
  if [ "${NEXGUARD_YES:-}" = 1 ]; then
    ok=0; agent pair --yes "$SERVER" "$CODE" < /dev/null || ok=$?
  elif [ -r /dev/tty ]; then
    ok=0; agent pair "$SERVER" "$CODE" < /dev/tty || ok=$?
  else
    ok=1; say "This needs an answer. Open Terminal and paste the line there."
  fi
  if [ "$ok" != 0 ]; then
    rm -f "$AGENT"
    [ -n "$NEW_DIR" ] && rm -rf "$DIR"
    say ""
    say "Nothing was installed. If you said no, the code still works."
    exit 1
  fi
# ------------------------------------------------------------------------ files
mkdir -p "$DIR"
chmod 700 "$DIR"
cat > "$DIR/uninstall.sh" <<'UNINSTALL'
#!/bin/sh
# Stop NexGuard on this computer (the agent or the check-in script) and remove its files.
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
rm -rf "$DIR"
[ "${1:-}" = quiet ] || echo "NexGuard is removed from this computer."
UNINSTALL
chmod 700 "$DIR/uninstall.sh"


  # Replace an earlier check-in script, if this computer had one.
  UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  if [ "$PLATFORM" = macos ] && [ -f "$HOME/Library/LaunchAgents/com.nexguard.device.plist" ]; then
    launchctl bootout "gui/$(id -u)/com.nexguard.device" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/com.nexguard.device.plist"
  fi
  if [ "$PLATFORM" = linux ] && [ -f "$UNITS/nexguard-device.timer" ]; then
    systemctl --user disable --now nexguard-device.timer >/dev/null 2>&1 || true
    rm -f "$UNITS/nexguard-device.timer" "$UNITS/nexguard-device.service"
  fi
  if [ "$PLATFORM" = linux ] && command -v crontab >/dev/null 2>&1 &&
     crontab -l 2>/dev/null | grep -qF "$DIR/checkin.sh"; then
    crontab -l 2>/dev/null | grep -vF "$DIR/checkin.sh" | crontab - || true
  fi
  rm -f "$DIR/checkin.sh" "$DIR/checkin.sh.bak-"* "$DIR/config" "$DIR/last" "$DIR/error"

  agent checkin >/dev/null 2>&1 || FIRST_FAILED=1

  if [ "${NEXGUARD_NO_SCHEDULE:-}" = 1 ]; then
    SCHEDULE="only when you run it yourself"
  elif [ "$PLATFORM" = macos ]; then
    PLIST="$HOME/Library/LaunchAgents/com.nexguard.agent.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    umask 022
    cat > "$PLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.nexguard.agent</string>
  <key>ProgramArguments</key>
  <array><string>$AGENT</string><string>--user</string><string>--dir</string><string>$DIR</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$DIR/agent.log</string>
</dict>
</plist>
PLIST_END
    JOB="gui/$(id -u)/com.nexguard.agent"
    launchctl bootout "$JOB" 2>/dev/null || true
    rm -f "$DIR/access.json"  # the agent starting now writes it
    tries=0
    until launchctl print "$JOB" >/dev/null 2>&1 || [ "$tries" -ge 5 ]; do
      tries=$((tries + 1))
      LAUNCH_ERR=$(launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>&1) || sleep 1
    done
    launchctl print "$JOB" >/dev/null 2>&1 || launchctl load -w "$PLIST" 2>/dev/null || true
    SCHEDULE="while you're signed in"
    if ! launchctl print "$JOB" >/dev/null 2>&1; then
      NOT_SCHEDULED="macOS didn't start the agent (${LAUNCH_ERR:-no reason given}). It starts by itself the next time you sign in. If it still doesn't, allow \"nexguard\" in System Settings > General > Login Items."
    fi
  elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$UNITS"
    umask 022
    cat > "$UNITS/nexguard-agent.service" <<UNIT_END
[Unit]
Description=NexGuard agent (as you, no administrator rights)

[Service]
ExecStart="$AGENT" --user --dir "$DIR" run
Restart=always
RestartSec=60

[Install]
WantedBy=default.target
UNIT_END
    systemctl --user daemon-reload
    systemctl --user enable --now nexguard-agent.service >/dev/null 2>&1 || true
    SCHEDULE="while you're signed in"
    if ! systemctl --user is-active --quiet nexguard-agent.service; then
      NOT_SCHEDULED="the agent didn't start. Check it with: systemctl --user status nexguard-agent"
    fi
  elif command -v crontab >/dev/null 2>&1; then
    { crontab -l 2>/dev/null | grep -vF "$DIR/" || true
      echo "@reboot '$AGENT' --user --dir '$DIR' run >/dev/null 2>&1 & echo \$! > '$DIR/agent.pid' # nexguard"
    } | crontab -
    nohup "$AGENT" --user --dir "$DIR" run >/dev/null 2>&1 &
    echo $! > "$DIR/agent.pid"
    SCHEDULE="from now on, and again after each restart"
  else
    fail "this computer has neither systemd nor cron to keep the agent running."
  fi

  say ""
  if [ -n "${FIRST_FAILED:-}" ]; then
    say "${PAIRED:-Paired}, but the first check-in failed. It will try again by itself."
  else
    say "Done. ${PAIRED:-This computer is connected.}"
    say "The agent runs $SCHEDULE. You can close this window."
  fi
  if [ "${NEXGUARD_NO_SCHEDULE:-}" != 1 ] && [ -z "${NOT_SCHEDULED:-}" ]; then
    disk_access "$AGENT" "$DIR/access.json"
  fi
  if [ -n "${NOT_SCHEDULED:-}" ]; then
    say ""
    say "Note: $NOT_SCHEDULED"
  fi
  say "To remove it later, paste:  curl -fsSL $SERVER/d/uninstall.sh | sh"
  say ""
  exit 0
fi


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

# ------------------------------------------------------------------------ files
mkdir -p "$DIR"
chmod 700 "$DIR"
cat > "$DIR/uninstall.sh" <<'UNINSTALL'
#!/bin/sh
# Stop NexGuard on this computer (the agent or the check-in script) and remove its files.
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
rm -rf "$DIR"
[ "${1:-}" = quiet ] || echo "NexGuard is removed from this computer."
UNINSTALL
chmod 700 "$DIR/uninstall.sh"

# ---------------------------------------------- no agent: the health-only check-in script
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
# NexGuard check-in: sends CPU, memory, GPU, battery and storage use, the network kind
# (and Wi-Fi name where allowed) and the system version to the dashboard. Runs every 5 minutes; reports as often as it asks (INTERVAL).
VERSION='1.2.0'
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/config"

now="$(date +%s)"
last="$(cat "$DIR/last" 2>/dev/null || echo 0)"
[ "${1:-}" = now ] || [ $((now - last)) -ge $((INTERVAL - 30)) ] || exit 0

json_str() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\000-\037')"; }
first() { head -n 1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'; }

pct=""; plugged=""; cpu=""; mem_total=""; mem_used=""; gpu=""; net=""; net_name=""
num() { grep -E '^[0-9]+([.][0-9]+)?$' | head -n 1; }  # a plain number, or nothing
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
  # CPU: the second of two one-second samples (the first covers the time since boot).
  cpu="$(top -l 2 -n 0 -s 1 2>/dev/null | sed -n 's/.*CPU usage:.* \([0-9.]*\)% idle.*/\1/p' | tail -n 1 \
    | awk 'NF { printf "%.1f", 100 - $1 }' | num)"
  # Memory "used" as Activity Monitor counts it: active + wired + compressed pages.
  mem_total="$(sysctl -n hw.memsize 2>/dev/null | num)"
  mem_used="$(vm_stat 2>/dev/null | awk '
    /page size of/ { ps = $8 } /Pages active/ { a = $3 } /Pages wired down/ { w = $4 }
    /Pages occupied by compressor/ { c = $5 }
    END { gsub(/[.]/, "", a); gsub(/[.]/, "", w); gsub(/[.]/, "", c); if (ps) printf "%.0f", (a + w + c) * ps }' | num)"
  gpu="$(ioreg -r -d 1 -c IOAccelerator 2>/dev/null | sed -n 's/.*"Device Utilization %"=\([0-9]*\).*/\1/p' | num)"
  # Network: the interface carrying the default route, Wi-Fi or wired. macOS 14+ hides the
  # Wi-Fi name ("<redacted>") from apps without location access: then only the kind is sent.
  ifc="$(route -n get default 2>/dev/null | awk '/interface:/ { print $2; exit }')"
  if [ -z "$ifc" ]; then
    net=none
  elif networksetup -listallhardwareports 2>/dev/null | grep -A1 -E '^Hardware Port: (Wi-Fi|AirPort)$' | grep -qx "Device: $ifc"; then
    net=wifi
    net_name="$(ipconfig getsummary "$ifc" 2>/dev/null | sed -n 's/^ *SSID : //p' | first)"
    case "$net_name" in ""|"<redacted>") net_name="" ;; esac
  elif printf '%s' "$ifc" | grep -qE '^(en|bridge)[0-9]'; then
    net=ethernet  # VPN tunnels (utun) and others: kind unknown, leave it out
  fi
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
  cpu_now() { awk '/^cpu / { print $2 + $3 + $4 + $5 + $6 + $7 + $8 + $9, $5 + $6 }' /proc/stat 2>/dev/null; }
  a="$(cpu_now)"; sleep 1; b="$(cpu_now)"
  cpu="$(printf '%s %s\n' "$a" "$b" | awk 'NF == 4 && $3 > $1 { printf "%.1f", 100 * (1 - ($4 - $2) / ($3 - $1)) }' | num)"
  mem_total="$(awk '/^MemTotal:/ { printf "%.0f", $2 * 1024 }' /proc/meminfo 2>/dev/null | num)"
  mem_used="$(awk '/^MemTotal:/ { t = $2 } /^MemAvailable:/ { f = $2 } END { if (t) printf "%.0f", (t - f) * 1024 }' /proc/meminfo 2>/dev/null | num)"
  for g in /sys/class/drm/card*/device/gpu_busy_percent; do  # AMD (and some Intel)
    [ -r "$g" ] && gpu="$(first < "$g" | num)" && [ -n "$gpu" ] && break
  done
  if [ -z "$gpu" ] && command -v nvidia-smi >/dev/null 2>&1; then
    gpu="$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | first | num)"
  fi
  ifc="$(ip route show default 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }')"
  if [ -z "$ifc" ]; then
    command -v ip >/dev/null 2>&1 && net=none
  elif [ -d "/sys/class/net/$ifc/wireless" ] || [ -d "/sys/class/net/$ifc/phy80211" ]; then
    net=wifi
    net_name="$(iwgetid -r 2>/dev/null | first)"
    [ -n "$net_name" ] || net_name="$(nmcli -t -f active,ssid dev wifi 2>/dev/null | sed -n 's/^yes://p' | first)"
  elif printf '%s' "$ifc" | grep -qE '^(ww|wwan|rmnet)'; then
    net=cellular
  elif [ -e "/sys/class/net/$ifc/device" ]; then
    net=ethernet  # a physical NIC; tunnels and bridges have no device link
  fi
fi
storage="$(df -kP "$disk" 2>/dev/null | awk 'NR==2 { printf "{\"total\":%.0f,\"free\":%.0f}", $2 * 1024, $4 * 1024 }')"

json="{\"platform\":\"$platform\",\"os_version\":$(json_str "$os"),\"model\":$(json_str "$model"),\"hostname\":$(json_str "$name"),\"app_version\":\"nexguard-device-sh $VERSION\""
[ -n "$pct" ] && json="$json,\"battery\":{\"percent\":$pct,\"charging\":$plugged}"
[ -n "$storage" ] && json="$json,\"storage\":$storage"
[ -n "$cpu" ] && json="$json,\"cpu\":{\"percent\":$cpu}"
[ -n "$mem_total" ] && [ -n "$mem_used" ] && json="$json,\"memory\":{\"total\":$mem_total,\"used\":$mem_used}"
[ -n "$gpu" ] && json="$json,\"gpu\":{\"percent\":$gpu}"
[ -n "$net" ] && json="$json,\"network\":{\"type\":\"$net\",\"name\":$(json_str "$net_name")}"
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

chmod 700 "$DIR/checkin.sh"

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
