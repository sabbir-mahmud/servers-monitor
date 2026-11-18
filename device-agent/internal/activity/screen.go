package activity

import (
	"context"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

const (
	// SampleEvery: each sample counts this much time to the app in front.
	SampleEvery = time.Minute
	// IdleAfter: no keyboard or mouse for this long and the computer counts as not in use
	// (a video playing still counts on Mac and Windows: the system reports it as activity
	// only through input, so long films may be under-counted).
	IdleAfter = 2 * time.Minute
)

// Use is one look at the computer: who is signed in at the screen, and what's in front.
type Use struct {
	User   string
	App    string
	Idle   time.Duration
	Locked bool
}

// Active: someone is signed in, the screen is unlocked and they used it recently.
func (u Use) Active() bool {
	return u.User != "" && !u.Locked && u.Idle < IdleAfter
}

// SampleOnce looks at the screen and counts a minute if it's in use. Errors (nobody
// signed in, a helper missing) count nothing.
func SampleOnce(now time.Time) {
	if !CurrentControls().ScreenTime {
		return
	}
	u, err := sample()
	if err != nil || !u.Active() {
		return
	}
	AddScreen(now.Format("2006-01-02"), u.User, friendlyApp(u.App), int(SampleEvery/time.Second))
}

// friendlyApp: "chrome.exe" -> "Chrome", "/usr/bin/firefox" -> "Firefox".
func friendlyApp(raw string) string {
	name := strings.TrimSpace(raw)
	if name == "" {
		return "Unknown"
	}
	base := strings.ToLower(filepath.Base(strings.ReplaceAll(name, `\`, "/")))
	base = strings.TrimSuffix(base, ".exe")
	if nice, ok := appNames[base]; ok {
		return nice
	}
	if strings.ContainsAny(name, `/\`) || strings.HasSuffix(strings.ToLower(name), ".exe") {
		if base == "" {
			return "Unknown"
		}
		return strings.ToUpper(base[:1]) + base[1:]
	}
	return name // macOS already gives the display name ("Google Chrome")
}

var appNames = map[string]string{
	"chrome": "Google Chrome", "msedge": "Microsoft Edge", "firefox": "Firefox",
	"brave": "Brave", "opera": "Opera", "vivaldi": "Vivaldi", "explorer": "File Explorer",
	"code": "Visual Studio Code", "winword": "Word", "excel": "Excel",
	"powerpnt": "PowerPoint", "outlook": "Outlook", "teams": "Microsoft Teams", "ms-teams": "Microsoft Teams",
	"discord": "Discord", "spotify": "Spotify", "steam": "Steam", "steamwebhelper": "Steam",
	"minecraft": "Minecraft", "javaw": "Java app (Minecraft?)", "robloxplayerbeta": "Roblox",
	"whatsapp": "WhatsApp", "telegram": "Telegram", "zoom": "Zoom", "notepad": "Notepad",
	"vlc": "VLC", "lockapp": "Lock screen", "applicationframehost": "Windows app",
}

// run a helper for at most 5 seconds.
func run(name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, name, args...).Output()
	return strings.TrimSpace(string(out)), err
}
