package activity

import (
	"os"
	"testing"
	"time"
)

func TestMacParsers(t *testing.T) {
	if got := hidIdle(`    | |     "HIDIdleTime" = 1738442583` + "\n"); got != 1738442583*time.Nanosecond {
		t.Errorf("hidIdle = %v", got)
	}
	if got := hidIdle("nothing"); got != IdleAfter {
		t.Errorf("unknown idle must count as idle, got %v", got)
	}
	out := "\"Google Chrome\" ASN:0x0-0x36036: (in front) \n    bundleID=[ NULL ]"
	if got := quotedName(out); got != "Google Chrome" {
		t.Errorf("quotedName = %q", got)
	}
	if got := quotedName(`"LSDisplayName"="Mail"`); got != "Mail" {
		t.Errorf("quotedName = %q", got)
	}
}

// NEXGUARD_LIVE=1 go test -run Live ./internal/activity: a real look at this Mac's screen.
func TestLiveSampleOnThisMac(t *testing.T) {
	if os.Getenv("NEXGUARD_LIVE") != "1" {
		t.Skip("set NEXGUARD_LIVE=1 to sample this Mac")
	}
	u, err := sample()
	if err != nil || u.User == "" || u.App == "" {
		t.Fatalf("%+v %v", u, err)
	}
	t.Logf("user=%s app=%q idle=%s locked=%v active=%v", u.User, u.App, u.Idle, u.Locked, u.Active())
}

func TestLiveAccessOnThisMac(t *testing.T) {
	if os.Getenv("NEXGUARD_LIVE") != "1" {
		t.Skip("set NEXGUARD_LIVE=1 to check this Mac")
	}
	t.Setenv("NEXGUARD_CONFIG_DIR", t.TempDir())
	t.Setenv("NEXGUARD_USER", "1")
	t.Logf("refused: %v", CheckAccess())
}
