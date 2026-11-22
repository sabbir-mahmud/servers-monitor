{% autoescape off %}#!/bin/sh
# {{ site_name }}: connect this Mac or Linux computer.
#
# Downloaded from {{ server }} for one pairing code. It:
#   1. shows who will see this computer, and pairs only if you say yes;
#   2. installs the NexGuard agent from that dashboard as a protected service (asks for
#      this computer's administrator password): health (CPU, memory, GPU, battery,
#      storage, network, system version), and screen time, websites and website blocking
#      if the dashboard turns them on. Without an administrator, the same agent running as
#      you (no blocking);
#   3. checks in once so the dashboard shows the computer straight away.
#
# The agent pairs and checks in through the dashboard's Redis relay (RELAY below: a login
# that can only send pairing requests), never to the dashboard itself.
#
# To remove it later:  curl -fsSL {{ server }}/d/uninstall.sh | sh
SERVER='{{ server }}'
CODE='{{ code }}'
RELAY='{{ relay }}'
{% endautoescape %}{% verbatim %}
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
[ -n "$RELAY" ] || fail "the dashboard can't take computers right now (its Redis relay isn't reachable). Try again later."

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
      fail "the agent download didn't match its checksum. Paste the line again."
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
  ok=0; sudo "$AGENT_TMP" pair "$RELAY" "$CODE" < /dev/tty || ok=$?
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
    ok=0; agent pair --yes "$RELAY" "$CODE" < /dev/null || ok=$?
  elif [ -r /dev/tty ]; then
    ok=0; agent pair "$RELAY" "$CODE" < /dev/tty || ok=$?
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

fail "this dashboard has no NexGuard agent for this computer ($(uname -s) $(uname -m)). Ask whoever runs it to build the agent (make device-agent)."
{% endverbatim %}
