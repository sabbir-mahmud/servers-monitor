package config

import (
	"errors"
	"os"
	"runtime"
	"testing"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/api"
)

func TestSaveIsPrivateAndLoadRoundTrips(t *testing.T) {
	t.Setenv("NEXGUARD_CONFIG_DIR", t.TempDir())
	if _, err := Load(); !errors.Is(err, ErrNotPaired) {
		t.Fatalf("empty dir: %v", err)
	}
	login := api.Login{URL: "rediss://sm-d-1:pw@relay.example.com:6379", Inbox: "sm:d:1:in"}
	if err := Save(&Config{Relay: login, Key: "ab", Interval: 0}); err != nil {
		t.Fatal(err)
	}
	c, err := Load()
	if err != nil || c.Key != "ab" || c.Relay != login || c.Interval != 300 { // nonsense interval -> default
		t.Fatalf("%+v %v", c, err)
	}
	if st, _ := os.Stat(Path()); runtime.GOOS != "windows" && st.Mode().Perm() != 0o600 {
		t.Fatalf("config mode %v, want 0600", st.Mode().Perm())
	}
	if err := Remove(); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(); !errors.Is(err, ErrNotPaired) {
		t.Fatal("still paired after Remove")
	}
}

func TestAnHTTPPairingMustBePairedAgain(t *testing.T) {
	t.Setenv("NEXGUARD_CONFIG_DIR", t.TempDir())
	_ = os.WriteFile(Path(), []byte(`{"server":"https://m.example.com","token":"ngd_x","interval":300}`), 0o600)
	if _, err := Load(); !errors.Is(err, ErrOldPairing) || !errors.Is(err, ErrNotPaired) {
		t.Fatalf("got %v", err)
	}
}
