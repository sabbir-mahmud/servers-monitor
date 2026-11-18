package activity

import (
	"errors"
	"os"
	"os/user"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
)

// sample on a Mac, as root: the console's owner is who's signed in at the screen; idle
// time comes from the input system; the app in front from that user's session.
func sample() (Use, error) {
	info, err := os.Stat("/dev/console")
	if err != nil {
		return Use{}, err
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok || st.Uid == 0 {
		return Use{}, errors.New("nobody signed in") // root owns it at the login window
	}
	if config.UserMode() && int(st.Uid) != os.Getuid() {
		return Use{}, errors.New("someone else is at the screen") // fast user switching
	}
	u, err := user.LookupId(strconv.Itoa(int(st.Uid)))
	if err != nil {
		return Use{}, err
	}
	out, _ := run("ioreg", "-c", "IOHIDSystem", "-d", "4")
	use := Use{User: u.Username, Idle: hidIdle(out)}
	use.App = frontApp(strconv.Itoa(int(st.Uid)))
	use.Locked = use.App == "loginwindow" // the lock screen belongs to loginwindow
	return use, nil
}

// hidIdle: "HIDIdleTime" = 1738442583 (nanoseconds). Unknown counts as idle.
func hidIdle(out string) time.Duration {
	i := strings.Index(out, `"HIDIdleTime" = `)
	if i < 0 {
		return IdleAfter
	}
	f := strings.Fields(out[i+len(`"HIDIdleTime" = `):])
	if len(f) == 0 {
		return IdleAfter
	}
	ns, err := strconv.ParseInt(f[0], 10, 64)
	if err != nil {
		return IdleAfter
	}
	return time.Duration(ns)
}

// frontApp: the name lsappinfo reports, run in the user's GUI session.
func frontApp(uid string) string {
	ask := func(args ...string) (string, error) {
		if config.UserMode() { // already in the person's session (asuser needs root)
			return run("/usr/bin/lsappinfo", args...)
		}
		return run("launchctl", append([]string{"asuser", uid, "/usr/bin/lsappinfo"}, args...)...)
	}
	asn, err := ask("front")
	if err != nil || asn == "" {
		return ""
	}
	out, err := ask("info", "-only", "name", asn)
	if err != nil {
		return ""
	}
	return quotedName(out)
}

// quotedName: `"Google Chrome" ASN:0x0-0x1a01a: (in front)` or `"LSDisplayName"="Mail"`.
func quotedName(out string) string {
	if i := strings.Index(out, `"LSDisplayName"="`); i >= 0 {
		rest := out[i+len(`"LSDisplayName"="`):]
		if j := strings.Index(rest, `"`); j >= 0 {
			return rest[:j]
		}
	}
	if strings.HasPrefix(out, `"`) {
		if j := strings.Index(out[1:], `"`); j >= 0 {
			return out[1 : j+1]
		}
	}
	return ""
}
