package config

import (
	"errors"
	"os"
	"runtime"
	"testing"
)

func TestSaveIsPrivateAndLoadRoundTrips(t *testing.T) {
	t.Setenv("NEXGUARD_CONFIG_DIR", t.TempDir())
	if _, err := Load(); !errors.Is(err, ErrNotPaired) {
		t.Fatalf("empty dir: %v", err)
	}
	if err := Save(&Config{Server: "https://m.example.com", Token: "ngd_x", Interval: 0}); err != nil {
		t.Fatal(err)
	}
	c, err := Load()
	if err != nil || c.Token != "ngd_x" || c.Interval != 300 { // nonsense interval -> default
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
