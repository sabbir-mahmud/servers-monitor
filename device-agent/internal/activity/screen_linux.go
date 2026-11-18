package activity

import (
	"errors"
	"strings"
)

// sample on Linux, as root, through systemd-logind: the active local graphical session,
// whether it's locked and whether the desktop marked it idle. Which app is in front isn't
// available in a way that works across X11, Wayland and desktops, so time counts as
// "Desktop" (total screen time is still right).
func sample() (Use, error) {
	list, err := run("loginctl", "list-sessions", "--no-legend")
	if err != nil {
		return Use{}, err
	}
	for _, line := range strings.Split(list, "\n") {
		f := strings.Fields(line)
		if len(f) == 0 {
			continue
		}
		props, err := run("loginctl", "show-session", f[0],
			"-p", "Name", "-p", "Type", "-p", "Active", "-p", "Remote", "-p", "Class",
			"-p", "IdleHint", "-p", "LockedHint")
		if err != nil {
			continue
		}
		if u, ok := sessionUse(props); ok {
			return u, nil
		}
	}
	return Use{}, errors.New("no active graphical session")
}

// sessionUse: the Use for one `loginctl show-session` output, if it's a person at the
// screen (a local, active, graphical user session).
func sessionUse(props string) (Use, bool) {
	p := map[string]string{}
	for _, line := range strings.Split(props, "\n") {
		if k, v, ok := strings.Cut(strings.TrimSpace(line), "="); ok {
			p[k] = v
		}
	}
	graphical := p["Type"] == "x11" || p["Type"] == "wayland" || p["Type"] == "mir"
	if !graphical || p["Active"] != "yes" || p["Remote"] == "yes" || p["Class"] != "user" {
		return Use{}, false
	}
	u := Use{User: p["Name"], App: "Desktop", Locked: p["LockedHint"] == "yes"}
	if p["IdleHint"] == "yes" {
		u.Idle = IdleAfter
	}
	return u, u.User != ""
}
