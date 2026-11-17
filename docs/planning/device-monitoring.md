# Device monitoring roadmap

Decided on 2026-11-17:

- **Install:** a *protected* install. A parent or admin installs it once with administrator
  rights. It runs as a system service (Windows service, macOS LaunchDaemon, systemd unit),
  and only an admin can remove it. The monitored account stays a standard user. Website
  blocking needs this.
- **Agent:** a Go binary for each OS, signed and released with checksums, like the server
  agent. The one-line user-level scripts stay for health-only monitoring.
- **Who:** parent and company accounts. The consent screen on the device names every kind
  of data collected, and nothing runs hidden: employers usually have to tell staff.

## Phases

1. **Health (done):** CPU, memory, GPU, battery and storage. Latest values on `Device`,
   history in `DeviceSample`, and the Health card on the device page. It works with the
   current scripts.
2. **Go agent and protected install:**
   - Done: `device-agent/` with pairing (preview first), check-in, health readings, the
     system service on all three OSes, a root-only config, and forgetting the pairing when
     the device is removed. Tested end to end against a live dashboard; CI covers Linux,
     macOS and Windows.
   - Next: the dashboard serves the binaries (checksums pinned like the server agent), and
     the pair page gets a one-line *protected* install that runs as admin. Then tamper
     reporting (service stopped or binary changed), self-update, and code-signing for
     Windows and macOS (so SmartScreen and Gatekeeper don't warn).
3. **Website rules:** block lists and allow-only lists for each person or device, enforced
   through Chrome, Edge and Firefox managed policies, plus a hosts-file fallback. The
   dashboard sends rules in the check-in answer, and the agent reports when they're applied.
4. **Browser history:** recent visits from Chrome, Edge, Firefox and Safari (Safari needs
   Full Disk Access). Kept for a set number of days, deleted with the device.
5. **Computer location:** Windows location services; macOS CoreLocation (needs the signed
   agent and the user's permission); IP-based as a coarse fallback, labelled as such.
