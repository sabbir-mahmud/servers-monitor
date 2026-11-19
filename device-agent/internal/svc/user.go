package svc

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
)

// The names the dashboard's connect scripts use for the same thing, so either can replace
// the other and its uninstall script removes both.
const (
	userLabel = "com.nexguard.agent" // macOS LaunchAgent
	userUnit  = "nexguard-agent"     // Linux systemd user service, XDG autostart entry
	userTask  = "NexGuard agent"     // Windows scheduled task
)

// UserBinary is where a per-person install keeps its copy of the agent: in its own folder,
// so the program that installed it (the desktop app, a download) can move or update.
func UserBinary() string {
	name := "nexguard"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	return filepath.Join(config.Dir(), name)
}

// InstallUser sets the agent up to run as the signed-in person, without administrator
// rights: a LaunchAgent on macOS, a systemd user service (or an autostart entry) on Linux,
// a sign-in task on Windows. It starts now and again at every sign-in. Run again, it
// updates the copy and restarts it. It does health, screen time and websites; blocking
// sites needs the administrator install.
func InstallUser(version string) error {
	if !config.UserMode() {
		return errors.New("use --user for an install without administrator rights")
	}
	if _, err := config.Load(); errors.Is(err, config.ErrNotPaired) {
		return errors.New("pair first: nexguard --user --dir <folder> pair <dashboard> <code>")
	}
	if err := os.MkdirAll(config.Dir(), 0o700); err != nil {
		return fmt.Errorf("can't create %s: %w", config.Dir(), err)
	}
	exe := UserBinary()
	if err := copySelf(exe); err != nil {
		return fmt.Errorf("couldn't copy the agent to %s: %w", exe, err)
	}
	if err := registerUser(exe, config.Dir()); err != nil {
		return err
	}
	fmt.Printf("Installed NexGuard %s for you. It runs while you're signed in.\n", version)
	return nil
}
