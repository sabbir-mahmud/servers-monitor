//go:build !windows

package block

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"time"
)

// write replaces the hosts file atomically, keeping it world-readable (every program on
// the computer resolves names through it).
func write(path string, data []byte) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".hosts.nexguard.")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

func flushDNS() {
	if os.Getenv("NEXGUARD_HOSTS") != "" {
		return // tests
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if runtime.GOOS == "darwin" {
		_ = exec.CommandContext(ctx, "dscacheutil", "-flushcache").Run()
		_ = exec.CommandContext(ctx, "killall", "-HUP", "mDNSResponder").Run()
		return
	}
	if _, err := exec.LookPath("resolvectl"); err == nil {
		_ = exec.CommandContext(ctx, "resolvectl", "flush-caches").Run()
	}
}
